# intellisense/capture/redis_stream_consumer_base.py

import asyncio
import threading
import logging
import inspect
import time
from typing import Callable, Optional, Any, Dict
from concurrent.futures import ThreadPoolExecutor
import redis

class RedisStreamConsumerBase:
    """
    Base class for Redis stream consumers with proper async/sync handler support.
    Handles the async/sync boundary correctly for FastAPI integration.
    """
    
    def __init__(
        self,
        redis_host: str = "127.0.0.1",  # Changed from "localhost" to avoid DNS resolution latency
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: Optional[str] = None,
        stream_name: str = "",
        consumer_group_name: str = "default_group",
        consumer_name: str = "default_consumer",
        message_handler: Optional[Callable] = None,
        main_event_loop: Optional[asyncio.AbstractEventLoop] = None,
        logger_instance: Optional[logging.Logger] = None,
        start_id: str = '$',
        low_latency_mode: bool = False
    ):
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.redis_password = redis_password
        self.stream_name = stream_name
        self.consumer_group_name = consumer_group_name
        self.consumer_name = consumer_name
        self._message_handler = message_handler
        self.main_event_loop = main_event_loop  # FastAPI's event loop
        self.logger = logger_instance or logging.getLogger(__name__)
        self._start_id = start_id
        self.low_latency_mode = low_latency_mode  # Phase 2: N-1 lag elimination
        
        # Redis connection
        self.redis_client = None
        
        # Threading control
        self._stop_event = threading.Event()
        self._consumer_thread = None
        self._thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="redis_consumer")
        self._is_consuming = False
        
    def set_message_handler(self, handler: Callable):
        """Set the message handler function (can be sync or async)"""
        self._message_handler = handler
        
    def start(self) -> bool:
        """Start the Redis stream consumer in a separate thread"""
        try:
            # Initialize Redis connection
            self.redis_client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                db=self.redis_db,
                password=self.redis_password,
                decode_responses=True
            )
            
            # Test connection and measure latency to track DNS resolution improvements
            import time
            connection_start_time = time.time()
            self.redis_client.ping()
            connection_latency_ms = (time.time() - connection_start_time) * 1000

            self.logger.info(f"Connected to Redis at {self.redis_host}:{self.redis_port}. Connection latency: {connection_latency_ms:.2f}ms")

            # Log high latency as warning to track DNS resolution issues
            if connection_latency_ms > 100:
                self.logger.warning(f"High Redis connection latency detected: {connection_latency_ms:.2f}ms (threshold: 100ms)")
            
            # Create consumer group if it doesn't exist
            try:
                self.redis_client.xgroup_create(
                    self.stream_name,
                    self.consumer_group_name,
                    id='0',
                    mkstream=True
                )
                self.logger.info(f"Created consumer group '{self.consumer_group_name}' for stream '{self.stream_name}'")
            except redis.exceptions.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    self.logger.info(f"Consumer group '{self.consumer_group_name}' already exists")
                elif "NOGROUP" in str(e):
                    self.logger.warning(f"Stream '{self.stream_name}' does not exist yet. Consumer will wait for stream creation.")
                else:
                    raise
            
            # Start consumer thread
            self._stop_event.clear()
            self._is_consuming = True
            self._consumer_thread = threading.Thread(
                target=self._consumption_loop,
                name=f"redis_consumer_{self.stream_name}",
                daemon=True
            )
            self._consumer_thread.start()
            
            self.logger.info(f"Started Redis consumer for stream '{self.stream_name}'")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to start Redis consumer: {e}")
            return False
    
    def stop(self):
        """Stop the Redis stream consumer"""
        self.logger.info(f"Stopping Redis consumer for stream '{self.stream_name}'")
        self._stop_event.set()
        self._is_consuming = False
        
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=5.0)
            if self._consumer_thread.is_alive():
                self.logger.warning("Consumer thread did not stop gracefully")
        
        # Shutdown thread pool
        self._thread_pool.shutdown(wait=True)
        
        # Close Redis connection
        if self.redis_client:
            self.redis_client.close()
            
        self.logger.info("Redis consumer stopped")
    
    def stop_consuming(self, wait_timeout_s: float = 5.0):
        """Alias for stop() method for backward compatibility"""
        self.stop()
    
    def start_consuming(self):
        """Alias for start() method for backward compatibility"""
        return self.start()
    
    def _consumption_loop(self):
        """Main consumption loop running in a separate thread"""
        self.logger.info(f"Starting consumption loop for stream '{self.stream_name}'")
        
        while not self._stop_event.is_set():
            try:
                # Read messages from stream with optimized settings for low latency
                if self.low_latency_mode:
                    # Phase 2: N-1 lag elimination - single message processing
                    messages = self.redis_client.xreadgroup(
                        self.consumer_group_name,
                        self.consumer_name,
                        {self.stream_name: '>'},
                        count=1,      # Single message for immediate processing
                        block=100     # 100ms timeout for low latency
                    )
                else:
                    # Standard batching mode
                    messages = self.redis_client.xreadgroup(
                        self.consumer_group_name,
                        self.consumer_name,
                        {self.stream_name: '>'},
                        count=10,
                        block=1000  # 1 second timeout
                    )
                
                if messages:
                    for stream, stream_messages in messages:
                        for message_id, fields in stream_messages:
                            message_processed_successfully = False
                            try:
                                # Parse message data
                                parsed_data = self._parse_message(fields)

                                # Handle the message (async-safe)
                                # _handle_message_safe now returns True for success, False for failure
                                if self._handle_message_safe(message_id, parsed_data):
                                    message_processed_successfully = True

                            except Exception as e_parse_handle: # Catch errors from parsing or unforeseen issues in _handle_message_safe setup
                                self.logger.error(f"Error parsing or initiating handling for message {message_id} from stream {self.stream_name}: {e_parse_handle}", exc_info=True)
                                # message_processed_successfully remains False

                            # Acknowledge message ONLY if processed successfully
                            if message_processed_successfully:
                                try:
                                    self.redis_client.xack(
                                        self.stream_name,
                                        self.consumer_group_name,
                                        message_id
                                    )
                                    self.logger.debug(f"Successfully processed and ACKed message {message_id} from stream {self.stream_name}")
                                except Exception as e_ack:
                                    self.logger.error(f"Failed to XACK message {message_id} from stream {self.stream_name} even after successful processing: {e_ack}", exc_info=True)
                                    # This is a bad state - processed but couldn't ACK. Might lead to reprocessing.
                            else:
                                self.logger.warning(f"Processing FAILED for message {message_id} from stream {self.stream_name}. Message NOT ACKed. It will be redelivered by Redis.")
                                # Add a small delay to prevent tight retry loops on consistently failing messages.
                                # This sleep is in the consumer's dedicated thread, so it's okay.
                                time.sleep(0.1)
                                
            except redis.exceptions.ConnectionError as e:
                self.logger.error(f"Redis connection error: {e}")
                self._stop_event.wait(5.0)  # Wait before retry

            except redis.exceptions.ResponseError as e:
                if "NOGROUP" in str(e):
                    # Only log once every 60 seconds to reduce noise
                    if not hasattr(self, '_last_nogroup_log') or (time.time() - self._last_nogroup_log) > 60:
                        self.logger.info(f"Waiting for stream '{self.stream_name}' to be created by ApplicationCore...")
                        self._last_nogroup_log = time.time()
                    self._stop_event.wait(15.0)  # Wait longer for stream to be created
                else:
                    self.logger.error(f"Redis response error: {e}")
                    self._stop_event.wait(1.0)

            except Exception as e:
                self.logger.error(f"Error in consumption loop: {e}")
                self._stop_event.wait(1.0)  # Brief pause before continuing
        
        self.logger.info("Consumption loop stopped")
    
    def _handle_message_safe(self, message_id: str, parsed_data: Dict[str, Any]) -> bool:
        """
        Handle message with proper async/sync boundary management.
        Returns True if handler processing was successful, False otherwise.
        """
        if not self._message_handler:
            self.logger.debug(f"No message handler set for message {message_id}, stream {self.stream_name}.")
            return True # No handler means "successfully processed" in the sense that there was nothing to do.

        try:
            if inspect.iscoroutinefunction(self._message_handler):
                if self.main_event_loop and not self.main_event_loop.is_closed():
                    future = asyncio.run_coroutine_threadsafe(
                        self._message_handler(message_id, parsed_data),
                        self.main_event_loop
                    )
                    try:
                        # Wait for completion with a timeout (e.g., 5 seconds)
                        # This timeout is crucial. If the handler is too slow,
                        # we consider it a failure for this attempt.
                        future.result(timeout=5.0)
                        return True  # Async handler completed successfully within timeout
                    except asyncio.TimeoutError: # Specifically catch asyncio.TimeoutError
                        self.logger.warning(f"Async handler timed out for message {message_id} in stream {self.stream_name}.")
                        return False # Handler timed out
                    except Exception as e_async_handler:
                        self.logger.error(f"Async handler for message {message_id} in stream {self.stream_name} raised an exception: {e_async_handler}", exc_info=True)
                        return False # Handler raised an exception
                else:
                    self.logger.error(f"No main event loop available for async handler for message {message_id}, stream {self.stream_name}. Cannot process.")
                    return False # Cannot process
            else:
                # Handler is synchronous, call directly
                self._message_handler(message_id, parsed_data)
                return True  # Sync handler completed (exceptions caught by outer try-except)
        except Exception as e_sync_handler:
            # This catches exceptions from synchronous handlers or issues in the setup above
            self.logger.error(f"Error calling/setting up message handler for message {message_id} in stream {self.stream_name}: {e_sync_handler}", exc_info=True)
            return False # Handler failed
    
    def _parse_message(self, fields: Dict[str, str]) -> Dict[str, Any]:
        """
        Parse Redis stream message fields.
        Override this method in subclasses for custom parsing.
        """
        try:
            # Handle JSON data if present
            if 'json_payload' in fields:
                import json
                return {'json_payload': fields['json_payload']}
            elif 'data' in fields:
                import json
                return json.loads(fields['data'])
            else:
                # Return fields as-is
                return dict(fields)
        except json.JSONDecodeError:
            # Fallback to raw fields
            return dict(fields)
        except Exception as e:
            self.logger.error(f"Error parsing message fields: {e}")
            return dict(fields)
