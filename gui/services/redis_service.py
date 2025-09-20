# gui/services/redis_service.py - Redis Connection Service

"""
Redis Service Implementation
============================
Manages Redis connections with resilience, monitoring, and proper error handling.

Features:
- Connection pooling
- Automatic reconnection
- Health monitoring
- Performance metrics
- Pub/Sub support
- Stream operations
"""

import asyncio
import logging
import time
import redis
import redis.asyncio as aioredis
from typing import Optional, Dict, Any, List, Tuple
from contextlib import asynccontextmanager

from .base_service import BaseService, ServiceState

logger = logging.getLogger(__name__)


class RedisService(BaseService):
    """
    Redis service with connection pooling and resilience.
    
    Features:
    - Sync and async Redis clients
    - Connection pooling
    - Automatic reconnection
    - Health monitoring
    - Performance tracking
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__("RedisService", config)
        
        # Configuration
        self.host = config.get("REDIS_HOST", "localhost")
        self.port = int(config.get("REDIS_PORT", 6379))
        self.db = int(config.get("REDIS_DB", 0))
        self.password = config.get("REDIS_PASSWORD")
        self.decode_responses = config.get("REDIS_DECODE_RESPONSES", True)
        self.max_connections = int(config.get("REDIS_MAX_CONNECTIONS", 50))
        self.socket_timeout = int(config.get("REDIS_SOCKET_TIMEOUT", 5))
        self.socket_connect_timeout = int(config.get("REDIS_CONNECT_TIMEOUT", 5))
        self.retry_on_timeout = config.get("REDIS_RETRY_ON_TIMEOUT", True)
        
        # Clients
        self._sync_client: Optional[redis.Redis] = None
        self._async_client: Optional[aioredis.Redis] = None
        self._sync_pool: Optional[redis.ConnectionPool] = None
        self._async_pool: Optional[aioredis.ConnectionPool] = None
        
        # Metrics
        self._operation_count = 0
        self._error_count = 0
        self._last_operation_time = 0
        self._reconnect_count = 0
        
    async def _do_initialize(self) -> None:
        """Initialize Redis connections"""
        # Create connection pools
        self._sync_pool = redis.ConnectionPool(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password,
            decode_responses=self.decode_responses,
            max_connections=self.max_connections,
            socket_timeout=self.socket_timeout,
            socket_connect_timeout=self.socket_connect_timeout,
            retry_on_timeout=self.retry_on_timeout
        )
        
        self._async_pool = aioredis.ConnectionPool(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password,
            decode_responses=self.decode_responses,
            max_connections=self.max_connections,
            socket_timeout=self.socket_timeout,
            socket_connect_timeout=self.socket_connect_timeout,
            retry_on_timeout=self.retry_on_timeout
        )
        
        # Create clients
        self._sync_client = redis.Redis(connection_pool=self._sync_pool)
        self._async_client = aioredis.Redis(connection_pool=self._async_pool)
        
        # Test connections
        await self._test_connections()
        
        self.logger.info(f"✅ Redis connected: {self.host}:{self.port} (DB: {self.db})")
        
    async def _do_shutdown(self) -> None:
        """Shutdown Redis connections"""
        try:
            if self._async_client:
                await self._async_client.close()
                await self._async_pool.disconnect()
                
            if self._sync_client:
                self._sync_client.close()
                self._sync_pool.disconnect()
                
        except Exception as e:
            self.logger.error(f"Error closing Redis connections: {e}")
            
    async def _do_health_check(self) -> bool:
        """Check Redis health"""
        try:
            # Try async ping
            if self._async_client:
                await self._async_client.ping()
                
            # Update health metadata
            self.health.metadata = {
                "host": self.host,
                "port": self.port,
                "connected": True,
                "operations": self._operation_count,
                "errors": self._error_count,
                "reconnects": self._reconnect_count
            }
            
            return True
            
        except Exception as e:
            self.health.metadata["connected"] = False
            self.health.metadata["error"] = str(e)
            return False
            
    async def _test_connections(self) -> None:
        """Test Redis connections"""
        # Test sync connection
        self._sync_client.ping()
        
        # Test async connection
        await self._async_client.ping()
        
    def get_sync_client(self) -> redis.Redis:
        """Get synchronous Redis client"""
        if not self._sync_client:
            raise RuntimeError("Redis service not initialized")
        return self._sync_client
        
    def get_async_client(self) -> aioredis.Redis:
        """Get asynchronous Redis client"""
        if not self._async_client:
            raise RuntimeError("Redis service not initialized")
        return self._async_client
        
    @asynccontextmanager
    async def get_connection(self):
        """Get a Redis connection from the pool"""
        conn = None
        try:
            conn = await self._async_pool.get_connection("_")
            yield conn
        finally:
            if conn:
                await self._async_pool.release(conn)
                
    # High-level operations with metrics and error handling
    
    async def xadd(self, stream: str, data: Dict[str, Any], maxlen: Optional[int] = None) -> str:
        """Add message to stream with metrics"""
        start_time = time.time()
        
        try:
            message_id = await self._async_client.xadd(
                name=stream,
                fields=data,
                maxlen=maxlen
            )
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("xadd.latency", time.time() - start_time, {"stream": stream})
            
            return message_id
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("xadd.error", 1, {"stream": stream, "error": type(e).__name__})
            self.logger.error(f"Error in xadd to {stream}: {e}")
            raise
            
    async def xread(self, streams: Dict[str, str], count: int = None, block: int = None) -> List[Tuple[str, List[Tuple[str, Dict[str, str]]]]]:
        """Read from streams with metrics"""
        start_time = time.time()
        
        try:
            result = await self._async_client.xread(
                streams=streams,
                count=count,
                block=block
            )
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("xread.latency", time.time() - start_time)
            self.record_metric("xread.messages", sum(len(messages) for _, messages in result))
            
            return result
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("xread.error", 1, {"error": type(e).__name__})
            self.logger.error(f"Error in xread: {e}")
            raise
            
    async def xrevrange(self, stream: str, max: str = "+", min: str = "-", count: Optional[int] = None) -> List[Tuple[str, Dict[str, str]]]:
        """Read stream in reverse with metrics"""
        start_time = time.time()
        
        try:
            result = await self._async_client.xrevrange(
                name=stream,
                max=max,
                min=min,
                count=count
            )
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("xrevrange.latency", time.time() - start_time, {"stream": stream})
            
            return result
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("xrevrange.error", 1, {"stream": stream, "error": type(e).__name__})
            self.logger.error(f"Error in xrevrange from {stream}: {e}")
            raise
            
    async def get(self, key: str) -> Optional[str]:
        """Get value with metrics"""
        start_time = time.time()
        
        try:
            value = await self._async_client.get(key)
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("get.latency", time.time() - start_time)
            
            return value
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("get.error", 1, {"error": type(e).__name__})
            self.logger.error(f"Error getting key {key}: {e}")
            raise
            
    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        """Set value with metrics"""
        start_time = time.time()
        
        try:
            result = await self._async_client.set(key, value, ex=ex)
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("set.latency", time.time() - start_time)
            
            return result
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("set.error", 1, {"error": type(e).__name__})
            self.logger.error(f"Error setting key {key}: {e}")
            raise
            
    async def hgetall(self, key: str) -> Dict[str, str]:
        """Get all hash fields with metrics"""
        start_time = time.time()
        
        try:
            result = await self._async_client.hgetall(key)
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("hgetall.latency", time.time() - start_time)
            self.record_metric("hgetall.fields", len(result))
            
            return result
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("hgetall.error", 1, {"error": type(e).__name__})
            self.logger.error(f"Error in hgetall for {key}: {e}")
            raise
            
    async def publish(self, channel: str, message: str) -> int:
        """Publish message with metrics"""
        start_time = time.time()
        
        try:
            count = await self._async_client.publish(channel, message)
            
            self._operation_count += 1
            self._last_operation_time = time.time()
            self.record_metric("publish.latency", time.time() - start_time, {"channel": channel})
            self.record_metric("publish.subscribers", count)
            
            return count
            
        except Exception as e:
            self._error_count += 1
            self.record_metric("publish.error", 1, {"channel": channel, "error": type(e).__name__})
            self.logger.error(f"Error publishing to {channel}: {e}")
            raise
            
    def is_connected(self) -> bool:
        """Check if Redis is connected"""
        try:
            if self._sync_client:
                self._sync_client.ping()
                return True
        except:
            pass
        return False
        
    async def is_connected_async(self) -> bool:
        """Check if Redis is connected (async)"""
        try:
            if self._async_client:
                await self._async_client.ping()
                return True
        except:
            pass
        return False