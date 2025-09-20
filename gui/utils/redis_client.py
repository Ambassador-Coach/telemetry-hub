# gui/utils/redis_client.py - GUI-specific Redis client (INDEPENDENT)
import os
import redis
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class RedisClientManager:
    """
    GUI-specific Redis client manager
    Self-contained, no dependencies on TESTRADE core
    """
    _instance = None
    
    def __init__(self):
        self.host = os.getenv('REDIS_HOST', 'localhost')
        self.port = int(os.getenv('REDIS_PORT', 6379))
        self.db = int(os.getenv('REDIS_DB', 0))
        self._client = None
        
    @classmethod
    def get_instance(cls):
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def get_client(self) -> Optional[redis.Redis]:
        """Get Redis client instance"""
        if self._client is None:
            try:
                self._client = redis.Redis(
                    host=self.host,
                    port=self.port,
                    db=self.db,
                    decode_responses=True
                )
                # Test connection
                self._client.ping()
                logger.info(f"✅ Redis client connected to {self.host}:{self.port}")
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                self._client = None
                raise
        return self._client
    
    def close(self):
        """Close Redis connection"""
        if self._client:
            try:
                self._client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")
            finally:
                self._client = None