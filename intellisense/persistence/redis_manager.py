import logging
import threading
import json
from typing import Optional, Dict, Any, List, Set

# Try to import redis, fallback to mock if not available
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    print("⚠️ Redis module not available - using fallback implementation")
    REDIS_AVAILABLE = False
    # Create a mock redis module for fallback
    class MockRedis:
        class Redis:
            def __init__(self, *args, **kwargs):
                pass
            def ping(self):
                raise ConnectionError("Redis not available")
            def set(self, *args, **kwargs):
                return False
            def get(self, *args, **kwargs):
                return None
            def delete(self, *args, **kwargs):
                return 0
            def sadd(self, *args, **kwargs):
                return 0
            def srem(self, *args, **kwargs):
                return 0
            def smembers(self, *args, **kwargs):
                return set()
            def mget(self, *args, **kwargs):
                return []
            def info(self):
                return {}
        class exceptions:
            class ConnectionError(Exception):
                pass
    redis = MockRedis()

logger = logging.getLogger(__name__)

class RedisManager:
    """
    Manages connection to Redis and provides basic Get/Set operations for IntelliSense.
    """
    _instance: Optional['RedisManager'] = None
    _lock = threading.Lock() # For singleton instantiation, if used this way

    def __init__(self, host: str = '127.0.0.1', port: int = 6379, db: int = 0, password: Optional[str] = None):  # Changed default from 'localhost'
        """
        Initializes the Redis client.
        Connection details should ideally come from configuration.
        """
        try:
            # decode_responses=True means redis-py will decode from bytes to str automatically
            self.redis_client = redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)
            self.redis_client.ping() # Verify connection
            logger.info(f"Successfully connected to Redis at {host}:{port}, DB: {db}")
        except (redis.exceptions.ConnectionError, ConnectionError) as e:
            logger.error(f"Failed to connect to Redis at {host}:{port}, DB: {db}. Error: {e}")
            # IntelliSense might need to operate in a degraded mode (e.g., file-based only)
            # or this could be a fatal error for persistence.
            # For now, allow instantiation but operations will fail.
            self.redis_client = None # Mark client as unusable
            print(f"⚠️ Redis connection failed - operating in degraded mode: {e}")
            # Don't raise - allow graceful degradation

    # --- Key Management for Sessions ---
    SESSION_INDEX_KEY = "intellisense:sessions" # A Redis Set storing all session_ids
    
    def _get_session_key(self, session_id: str) -> str:
        """Generates the Redis key for a specific session's data."""
        return f"intellisense:session:{session_id}"

    # --- Generic Get/Set (could be expanded) ---
    def set_json(self, key: str, data: Dict[str, Any], expiry_seconds: Optional[int] = None) -> bool:
        """Serializes a dictionary to JSON and stores it in Redis."""
        if not self.redis_client: return False
        try:
            self.redis_client.set(key, json.dumps(data), ex=expiry_seconds)
            return True
        except Exception as e:
            logger.error(f"Redis SET error for key '{key}': {e}", exc_info=True)
            return False

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieves a JSON string from Redis and deserializes it to a dictionary."""
        if not self.redis_client: return None
        try:
            json_data = self.redis_client.get(key)
            if json_data:
                return json.loads(json_data)
            return None
        except Exception as e:
            logger.error(f"Redis GET error or JSON parse error for key '{key}': {e}", exc_info=True)
            return None

    def delete_key(self, key: str) -> bool:
        if not self.redis_client: return False
        try:
            return self.redis_client.delete(key) > 0
        except Exception as e:
            logger.error(f"Redis DELETE error for key '{key}': {e}", exc_info=True)
            return False

    # --- Session Specific Methods ---
    def add_session_to_index(self, session_id: str) -> bool:
        if not self.redis_client: return False
        try:
            self.redis_client.sadd(self.SESSION_INDEX_KEY, session_id)
            return True
        except Exception as e:
            logger.error(f"Redis SADD error for session index '{self.SESSION_INDEX_KEY}': {e}", exc_info=True)
            return False
            
    def remove_session_from_index(self, session_id: str) -> bool:
        if not self.redis_client: return False
        try:
            self.redis_client.srem(self.SESSION_INDEX_KEY, session_id)
            return True
        except Exception as e:
            logger.error(f"Redis SREM error for session index '{self.SESSION_INDEX_KEY}': {e}", exc_info=True)
            return False

    def get_all_session_ids_from_index(self) -> Set[str]:
        if not self.redis_client: return set()
        try:
            session_ids = self.redis_client.smembers(self.SESSION_INDEX_KEY)
            return session_ids if session_ids else set() # Ensure returns set even if empty from Redis
        except Exception as e:
            logger.error(f"Redis SMEMBERS error for session index '{self.SESSION_INDEX_KEY}': {e}", exc_info=True)
            return set()

    def save_session_data(self, session_id: str, session_data_dict: Dict[str, Any], expiry_seconds: Optional[int] = None) -> bool:
        """Saves a session's data (as JSON string) and adds its ID to the index."""
        session_key = self._get_session_key(session_id)
        if self.set_json(session_key, session_data_dict, expiry_seconds):
            return self.add_session_to_index(session_id)
        return False

    def load_session_data(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Loads a session's data from Redis."""
        session_key = self._get_session_key(session_id)
        return self.get_json(session_key)

    def delete_session_data(self, session_id: str) -> bool:
        """Deletes a session's data and removes its ID from the index."""
        session_key = self._get_session_key(session_id)
        # It's important to try to remove from index even if key deletion fails, and vice-versa.
        # For atomicity, one might use a Lua script or MULTI/EXEC if critical.
        # For simplicity now, do them sequentially.
        key_deleted = self.delete_key(session_key)
        index_removed = self.remove_session_from_index(session_id)
        return key_deleted and index_removed # Both should succeed for overall success

    # Optional: A method to get summary for multiple sessions (e.g., for list_sessions)
    def load_multiple_session_summaries(self, session_ids: List[str]) -> Dict[str, Optional[Dict[str,Any]]]:
        if not self.redis_client or not session_ids: return {}
        results = {}
        # Note: MGET for JSON strings. If using Hashes, HMGET would be more efficient for specific fields.
        # For JSON strings, MGET is fine.
        keys_to_fetch = [self._get_session_key(sid) for sid in session_ids]
        try:
            json_strings = self.redis_client.mget(keys_to_fetch)
            for i, sid in enumerate(session_ids):
                if json_strings[i]:
                    try:
                        results[sid] = json.loads(json_strings[i])
                    except json.JSONDecodeError:
                        logger.error(f"JSON decode error for session summary {sid}")
                        results[sid] = None # Or some error indicator
                else:
                    results[sid] = None # Not found
        except Exception as e:
            logger.error(f"Redis MGET error for session summaries: {e}", exc_info=True)
        return results

    @classmethod
    def get_default_instance(cls) -> 'RedisManager':
        """Provides a default singleton instance (optional pattern)."""
        # This is a simple way to get a shared instance.
        # In a larger app, this might be managed by a DI container or AppConfig.
        with cls._lock:
            if cls._instance is None:
                # TODO: These connection details should come from a central config.
                logger.info("Creating default RedisManager instance (localhost:6379, DB 0).")
                cls._instance = cls(host='localhost', port=6379, db=0)
            return cls._instance

    def health_check(self) -> bool:
        """Check if Redis connection is healthy."""
        if not self.redis_client:
            return False
        try:
            self.redis_client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            return False

    def get_connection_info(self) -> Dict[str, Any]:
        """Get Redis connection information for diagnostics."""
        if not self.redis_client:
            return {"status": "disconnected", "client": None}

        try:
            info = self.redis_client.info()
            return {
                "status": "connected",
                "redis_version": info.get("redis_version"),
                "connected_clients": info.get("connected_clients"),
                "used_memory_human": info.get("used_memory_human"),
                "uptime_in_seconds": info.get("uptime_in_seconds")
            }
        except Exception as e:
            logger.error(f"Failed to get Redis info: {e}")
            return {"status": "error", "error": str(e)}
