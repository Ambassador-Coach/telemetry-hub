"""
TelemetryStreamingService - Centralized telemetry streaming service for safe, non-blocking IPC publishing.

This service implements the "Telemetry Airlock" pattern to prevent blocking on IPC operations.
It pulls telemetry data from a central queue and publishes it in batches to Redis via the 
BulletproofBabysitterIPCClient.
"""

import logging
import threading
import time
import json
import queue
from typing import Any, Dict, List, Optional
from collections import defaultdict

from interfaces.core.telemetry_interfaces import ITelemetryService
from interfaces.core.services import IBulletproofBabysitterIPCClient, IConfigService

logger = logging.getLogger(__name__)


class TelemetryStreamingService(ITelemetryService):
    """
    Centralized service for streaming telemetry data to Redis in a non-blocking manner.
    This is the canonical implementation of ITelemetryService.
    
    This service:
    1. Pulls telemetry payloads from a central queue
    2. Batches them by stream/type for efficiency
    3. Publishes them to Redis via IPC in a throttled manner
    4. Ensures the main application never blocks on telemetry operations
    """
    
    def __init__(self, ipc_client: IBulletproofBabysitterIPCClient, config: IConfigService):
        """
        Initialize the TelemetryStreamingService.
        
        Args:
            ipc_client: The client for communicating with the Babysitter process.
            config: The global configuration service.
        """
        # The telemetry_queue is now an *internal detail* of this service, not an injected dependency.
        self.telemetry_queue = queue.Queue(maxsize=10000)
        self.ipc_client = ipc_client
        self.config = config
        
        # Read parameters from the config object - OPTIMIZED FOR REAL-TIME TRADING
        self.batch_size = getattr(config, 'TELEMETRY_BATCH_SIZE', 200)  # Increased for better throughput
        self.batch_timeout_sec = getattr(config, 'TELEMETRY_BATCH_TIMEOUT_SEC', 0.05)  # 50ms for near real-time
        self.max_publish_rate_per_sec = getattr(config, 'TELEMETRY_MAX_PUBLISH_RATE_PER_SEC', 500)  # 25x increase for real-time trading
        self.health_check_interval_sec = getattr(config, 'TELEMETRY_HEALTH_CHECK_INTERVAL_SEC', 1.0)
        
        # Threading
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        
        # Express Lane state
        self._channel_health_cache: Dict[str, bool] = {}
        self._last_health_check_time = 0.0
        self._express_lane_enabled = True  # Can be disabled for testing
        
        # Metrics
        self._total_messages_processed = 0
        self._total_messages_dropped = 0
        self._total_batches_published = 0
        self._express_lane_hits = 0
        self._durable_lane_hits = 0
        self._last_publish_time = 0.0
        
        # Rate limiting
        self._min_interval_between_publishes = 1.0 / self.max_publish_rate_per_sec
        
        logger.info(f"TelemetryStreamingService initialized with EXPRESS LANE + REAL-TIME TRADING optimizations. "
                   f"batch_size={self.batch_size}, timeout={self.batch_timeout_sec}s, max_rate={self.max_publish_rate_per_sec}/s")
        
        # Bulk data handling
        self._bulk_data_threshold = 10000  # 10KB threshold for bulk data
        self._bulk_data_fields = ['frame_base64', 'processed_frame_base64', 'raw_frame_b64', 'image_data']

    def _extract_bulk_data(self, payload: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
        """
        Extract large binary data (like images) from payload and publish separately.
        Returns modified payload with bulk data references.
        """
        modified_payload = payload.copy()
        bulk_items = []
        
        def extract_from_dict(data: Dict[str, Any], path: str = "") -> None:
            """Recursively extract bulk data from nested dictionaries."""
            for key, value in list(data.items()):
                current_path = f"{path}.{key}" if path else key
                
                # Check if this is a bulk data field
                if key in self._bulk_data_fields and isinstance(value, str) and len(value) > self._bulk_data_threshold:
                    # Generate bulk reference
                    bulk_ref = f"bulk_{correlation_id}_{key}_{len(bulk_items)}"
                    
                    # Store bulk data info
                    bulk_items.append({
                        'reference': bulk_ref,
                        'field': key,
                        'path': current_path,
                        'data': value,
                        'size': len(value)
                    })
                    
                    # Replace with reference
                    data[key] = f"{{bulk_ref:{bulk_ref}}}"
                    
                    logger.info(f"[BULK DATA] Extracted {key} ({len(value)} bytes) -> {bulk_ref} for stream: testrade:bulk-data")
                    
                elif isinstance(value, dict):
                    extract_from_dict(value, current_path)
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, dict):
                            extract_from_dict(item, f"{current_path}[{i}]")
        
        # Extract bulk data from payload
        extract_from_dict(modified_payload)
        
        # If we have bulk data, publish it separately
        if bulk_items:
            logger.info(f"[BULK DATA] Processing {len(bulk_items)} bulk data items from payload")
            for item in bulk_items:
                bulk_payload = {
                    'reference': item['reference'],
                    'field': item['field'],
                    'path': item['path'],
                    'data': item['data'],
                    'size': item['size'],
                    'correlation_id': correlation_id,
                    'timestamp': time.time()
                }
                
                # Enqueue bulk data to a separate stream
                bulk_item = {
                    'source_service': 'TelemetryService',
                    'event_type': 'BULK_DATA',
                    'data': bulk_payload,
                    'stream': 'testrade:bulk-data',
                    'origin_timestamp_s': time.time()
                }
                
                try:
                    self.telemetry_queue.put_nowait(bulk_item)
                    logger.info(f"[BULK DATA] Enqueued {item['field']} ({item['size']} bytes) with reference: {item['reference']}")
                except queue.Full:
                    logger.warning(f"[BULK DATA] Queue full, dropping bulk data: {item['reference']}")
        
        return modified_payload

    # Implementation of ITelemetryService interface methods
    def enqueue(self, source_component: str, event_type: str, payload: Dict[str, Any], stream_override: Optional[str] = None, origin_timestamp_s: Optional[float] = None) -> bool:
        """Non-blocking call to enqueue telemetry data. Implements ITelemetryService."""
        try:
            # Extract bulk data if correlation_id is available
            import uuid
            correlation_id = str(uuid.uuid4())
            processed_payload = self._extract_bulk_data(payload, correlation_id)
            
            # This item structure matches what the _worker_loop expects.
            item = {
                'source_service': source_component,
                'event_type': event_type,
                'data': processed_payload, # The worker expects the payload under the 'data' key
                'stream': stream_override,
                'origin_timestamp_s': origin_timestamp_s
            }
            self.telemetry_queue.put_nowait(item)
            return True
        except queue.Full:
            # Log that a message was dropped. This is a critical self-monitoring event.
            self._total_messages_dropped += 1
            logger.warning(f"Telemetry queue is full. Dropping event from {source_component}.")
            
            # Create a new event about the dropped message and try to enqueue IT.
            # This has a small chance of also being dropped but is worth trying.
            try:
                dropped_event_payload = {
                    "event_type": "TELEMETRY_QUEUE_OVERRUN",
                    "source_component_dropped": source_component,
                    "event_type_dropped": event_type,
                    "current_queue_size": self.telemetry_queue.qsize(),
                    "total_dropped_since_start": self._total_messages_dropped
                }
                self.enqueue(
                    source_component="TelemetryStreamingService",
                    event_type="SYSTEM_ALERT",
                    payload=dropped_event_payload,
                    stream_override="testrade:health:core"
                )
            except queue.Full:
                logger.error("Telemetry queue is completely full. Cannot even log the dropped message event.")
            
            return False
        except Exception as e:
            logger.error(f"Error enqueueing telemetry data from {source_component}: {e}")
            return False

    def publish_bootstrap_data(self, config_dict: Dict[str, Any]) -> None:
        """Enqueues bootstrap config data for publishing."""
        self.enqueue(
            source_component="ApplicationCore",
            event_type="BOOTSTRAP_CONFIG",
            payload={"config_snapshot": config_dict},
            stream_override="testrade:config-changes"
        )
        
    def publish_health_data(self, health_data: Dict[str, Any]) -> None:
        """Enqueues health data for publishing."""
        self.enqueue(
            source_component="HealthMonitor",
            event_type="CORE_HEALTH_STATUS",
            payload=health_data,
            stream_override="testrade:health:core"
        )

    def publish_command_response(self, command_data: Dict[str, Any]) -> None:
        """Enqueues a command response for publishing."""
        self.enqueue(
            source_component="CommandListener",
            event_type="COMMAND_RESPONSE",
            payload=command_data,
            stream_override="testrade:responses:to_gui"
        )

    def publish_raw_ocr_data(self, ocr_data: Dict[str, Any]) -> None:
        """Enqueues raw OCR data for publishing."""
        self.enqueue(
            source_component="OCRService",
            event_type="RAW_OCR_DATA",
            payload=ocr_data,
            stream_override=ocr_data.get('target_stream', 'testrade:raw-ocr-events')
        )
    
    def start(self) -> None:
        """Start the telemetry streaming worker thread."""
        if self._is_running:
            logger.warning("TelemetryStreamingService already running")
            return
            
        self._is_running = True
        self._stop_event.clear()
        
        # Start worker thread with low priority
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="TelemetryStreamer",
            daemon=True
        )
        self._worker_thread.start()
        
        logger.info("TelemetryStreamingService started")
    
    def stop(self) -> None:
        """Stop the telemetry streaming worker thread."""
        if not self._is_running:
            return
            
        logger.info("Stopping TelemetryStreamingService...")
        self._is_running = False
        self._stop_event.set()
        
        # Wait for worker to finish with timeout
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)
            if self._worker_thread.is_alive():
                logger.warning("TelemetryStreamingService worker thread did not stop cleanly")
        
        logger.info(f"TelemetryStreamingService stopped. Stats: "
                   f"processed={self._total_messages_processed}, "
                   f"dropped={self._total_messages_dropped}, "
                   f"batches={self._total_batches_published}, "
                   f"express_lane_hits={self._express_lane_hits}, "
                   f"durable_lane_hits={self._durable_lane_hits}")
    
    def _update_channel_health_cache(self) -> None:
        """Update cached channel health status from IPC client."""
        current_time = time.time()
        if current_time - self._last_health_check_time < self.health_check_interval_sec:
            return  # Not time to check yet
            
        try:
            # Get channel clogged status from IPC client
            if hasattr(self.ipc_client, 'channel_clogged_status'):
                for socket_type, is_clogged in self.ipc_client.channel_clogged_status.items():
                    self._channel_health_cache[socket_type] = not is_clogged  # Healthy = not clogged
                    
            self._last_health_check_time = current_time
            
        except Exception as e:
            logger.error(f"Error updating channel health cache: {e}", exc_info=True)
    
    def _is_channel_healthy(self, stream: str) -> bool:
        """Check if channel is healthy for express lane."""
        # Map stream to socket type
        socket_type = self._get_socket_type_for_stream(stream)
        
        # Return cached health status, default to False (use durable lane) if unknown
        return self._channel_health_cache.get(socket_type, False)
    
    def _get_socket_type_for_stream(self, stream: str) -> str:
        """Map Redis stream name to IPC socket type."""
        # Check if stream contains certain keywords to determine socket type
        stream_lower = stream.lower()
        
        if any(keyword in stream_lower for keyword in ['order', 'trade', 'position', 'risk']):
            return 'trading'
        elif any(keyword in stream_lower for keyword in ['health', 'error', 'system']):
            return 'system'
        else:
            return 'bulk'  # Default for OCR, telemetry, etc.
    
    def _worker_loop(self) -> None:
        """Main worker loop that pulls from queue and publishes batches."""
        logger.info("TelemetryStreamingService worker started")
        
        # Batch accumulator: stream_name -> list of messages
        batch_accumulator: Dict[str, List[str]] = defaultdict(list)
        last_batch_time = time.time()
        
        while not self._stop_event.is_set():
            try:
                # Update channel health cache periodically
                self._update_channel_health_cache()
                
                # Try to get items from queue with timeout
                timeout = max(0.1, self.batch_timeout_sec - (time.time() - last_batch_time))
                
                try:
                    payload = self.telemetry_queue.get(timeout=timeout)
                except queue.Empty:
                    # Timeout - check if we should publish partial batches
                    if time.time() - last_batch_time >= self.batch_timeout_sec:
                        if batch_accumulator:
                            self._publish_batches(batch_accumulator)
                            batch_accumulator.clear()
                            last_batch_time = time.time()
                    continue
                
                # Process the payload
                try:
                    # Convert payload to Redis message
                    redis_msg = self._create_redis_message(payload)
                    if redis_msg:
                        stream_name = payload.get('stream', self._determine_stream(payload))
                        batch_accumulator[stream_name].append(redis_msg)
                        self._total_messages_processed += 1
                        
                        # Check if any batch is full
                        for stream, messages in batch_accumulator.items():
                            if len(messages) >= self.batch_size:
                                self._publish_batch(stream, messages)
                                batch_accumulator[stream] = []
                                last_batch_time = time.time()
                                
                except Exception as e:
                    logger.error(f"Error processing telemetry payload: {e}", exc_info=True)
                    self._total_messages_dropped += 1
                    
            except Exception as e:
                logger.error(f"Unexpected error in telemetry worker loop: {e}", exc_info=True)
                time.sleep(1.0)  # Prevent tight loop on persistent errors
        
        # Publish any remaining messages before exiting
        if batch_accumulator:
            self._publish_batches(batch_accumulator)
        
        logger.info("TelemetryStreamingService worker stopped")
    
    def _create_redis_message(self, item: Dict[str, Any]) -> Optional[str]:
        """
        Convert a telemetry item to a Redis message string.
        
        This method prioritizes data delivery. If correlation metadata is missing or malformed,
        it will log a CRITICAL error but will construct a best-effort metadata block to ensure
        the core payload is not lost.
        """
        try:
            # TANK MODE Check: If IPC data dump is disabled, do nothing.
            if hasattr(self.config, 'ENABLE_IPC_DATA_DUMP') and not self.config.ENABLE_IPC_DATA_DUMP:
                return None

            import uuid

            # Extract required fields from telemetry item (this is the standard path)
            source_component = item.get('source_service', 'UnknownComponent')
            event_type = item.get('event_type', 'Unknown')
            data = item.get('data', {})

            # Check if data is already a formatted JSON string (from legacy services)
            if isinstance(data, str):
                # Already formatted, just return it
                return data

            # SPECIAL CASE: Check if the data contains a pre-formed metadata block
            # This indicates a message that was already properly formatted by another service
            if isinstance(data, dict) and isinstance(data.get('metadata'), dict):
                # This is a fully-formed message, just serialize and return
                return json.dumps(data)

            # STANDARD PATH: Build metadata from telemetry item components
            # Check for special correlation_id override in the data
            master_correlation_id = data.pop('__correlation_id', None) if isinstance(data, dict) else None
            causation_id = data.pop('__causation_id', None) if isinstance(data, dict) else None
            
            # Fallback to existing correlation_id if special key isn't present
            if not master_correlation_id and isinstance(data, dict):
                master_correlation_id = data.get('correlation_id')
            
            # If still no correlation ID, this is where we log the CRITICAL error and repair
            if not master_correlation_id:
                logger.critical(
                    f"[DATA_CONTRACT_VIOLATION] Message from '{source_component}' is missing correlation metadata. "
                    f"A repaired correlation ID will be created, but this indicates a SERIOUS bug in the source component."
                )
                master_correlation_id = f"REPAIRED_{str(uuid.uuid4())}"

            # Extract origin_timestamp_s from the item for Golden Timestamp implementation
            origin_timestamp_s = item.get('origin_timestamp_s')
            
            # Create standardized Redis message format (matching original app_core format)
            message = {
                "metadata": {
                    "eventType": f"TESTRADE_{event_type}" if not event_type.startswith('TESTRADE_') else event_type,
                    "sourceComponent": source_component,
                    "timestamp_ns": time.perf_counter_ns(),  # High-precision nanosecond timestamp
                    "epoch_timestamp_s": time.time(),       # Float epoch seconds (standard field)
                    "correlationId": master_correlation_id,
                    "causationId": causation_id or (data.get('causation_id') if isinstance(data, dict) else None),
                    "msgId": str(uuid.uuid4()),
                    "originTimestampS": origin_timestamp_s  # Golden Timestamp support
                },
                "payload": data
            }
            
            return json.dumps(message)
                
        except Exception as e:
            logger.error(f"Fatal error creating Redis message, data may be lost: {e}", exc_info=True)
            # This is a different class of error (e.g., JSON serialization), so we still drop.
            self._total_messages_dropped += 1
            return None
    
    def _determine_stream(self, payload: Dict[str, Any]) -> str:
        """Determine the target Redis stream based on payload type."""
        event_type = payload.get('event_type', '').lower()
        
        # Map event types to streams
        stream_mapping = {
            'risk': 'testrade:risk-actions',
            'position': 'testrade:position-updates',
            'order': 'testrade:order-events',
            'signal': 'testrade:signal-decisions',
            'health': 'testrade:service-health',
            'account': 'testrade:account-updates',
            'validation': 'testrade:validation-decisions',
            'error': 'testrade:system-errors'
        }
        
        # Find matching stream
        for key, stream in stream_mapping.items():
            if key in event_type:
                return getattr(self.config, f'redis_stream_{key}', stream)
        
        # Default stream
        return getattr(self.config, 'redis_stream_telemetry_default', 'testrade:telemetry')
    
    def _publish_batch(self, stream: str, messages: List[str]) -> None:
        """Publish a batch of messages using Express Lane or Durable Lane."""
        # Rate limiting
        time_since_last_publish = time.time() - self._last_publish_time
        if time_since_last_publish < self._min_interval_between_publishes:
            time.sleep(self._min_interval_between_publishes - time_since_last_publish)
        
        try:
            # Express Lane decision
            use_express_lane = (self._express_lane_enabled and 
                               self._is_channel_healthy(stream))
            
            success_count = 0
            
            if use_express_lane:
                # EXPRESS LANE: Write directly to IPC client's in-memory pre-buffer
                socket_type = self._get_socket_type_for_stream(stream)
                
                if hasattr(self.ipc_client, 'memory_pre_buffers'):
                    pre_buffer = self.ipc_client.memory_pre_buffers.get(socket_type)
                    
                    if pre_buffer:
                        for message in messages:
                            try:
                                # Package message like IPC client expects
                                prebuffer_message = {
                                    'stream_name': stream,
                                    'data_json': message,
                                    'timestamp': time.time()
                                }
                                pre_buffer.put_nowait(prebuffer_message)
                                success_count += 1
                                self._express_lane_hits += 1
                            except queue.Full:
                                # Pre-buffer full, fall back to durable lane
                                logger.warning(f"Express lane pre-buffer full for {socket_type}, falling back to durable lane")
                                if self._publish_via_durable_lane(stream, message):
                                    success_count += 1
                                    self._durable_lane_hits += 1
                                else:
                                    self._total_messages_dropped += 1
                    else:
                        # Pre-buffer not available, use durable lane
                        use_express_lane = False
                else:
                    # Old IPC client without pre-buffers, use durable lane
                    use_express_lane = False
            
            if not use_express_lane:
                # DURABLE LANE: Use standard IPC client send_data (goes to mmap)
                for message in messages:
                    if self._publish_via_durable_lane(stream, message):
                        success_count += 1
                        self._durable_lane_hits += 1
                    else:
                        self._total_messages_dropped += 1
            
            if success_count > 0:
                lane_used = "express" if use_express_lane else "durable"
                logger.debug(f"Published {success_count}/{len(messages)} messages to {stream} via {lane_used} lane")
                self._total_batches_published += 1
            
        except Exception as e:
            logger.error(f"Error publishing batch to {stream}: {e}", exc_info=True)
            self._total_messages_dropped += len(messages)
        
        self._last_publish_time = time.time()
    
    def _publish_via_durable_lane(self, stream: str, message: str) -> bool:
        """Publish a single message via the durable lane (standard IPC)."""
        try:
            # OPTIMIZATION: Check if IPC client supports direct dict sending
            if hasattr(self.ipc_client, 'send_data_dict'):
                # Parse JSON once and send dict directly - eliminates double serialization!
                import json
                try:
                    data_dict = json.loads(message)
                    return self.ipc_client.send_data_dict(stream, data_dict)
                except json.JSONDecodeError:
                    # Fall back to string method if JSON is invalid
                    pass
            
            # Standard path: send JSON string
            return self.ipc_client.send_data(stream, message)
        except Exception as e:
            logger.error(f"Error in durable lane publish: {e}")
            return False
    
    def _publish_batches(self, batches: Dict[str, List[str]]) -> None:
        """Publish all accumulated batches."""
        for stream, messages in batches.items():
            if messages:
                self._publish_batch(stream, messages)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics including Express Lane metrics."""
        total_published = self._express_lane_hits + self._durable_lane_hits
        express_lane_percentage = (self._express_lane_hits / total_published * 100 
                                  if total_published > 0 else 0)
        
        return {
            'is_running': self._is_running,
            'queue_size': self.telemetry_queue.qsize(),
            'total_processed': self._total_messages_processed,
            'total_dropped': self._total_messages_dropped,
            'total_batches': self._total_batches_published,
            'express_lane_hits': self._express_lane_hits,
            'durable_lane_hits': self._durable_lane_hits,
            'express_lane_percentage': express_lane_percentage,
            'channel_health': dict(self._channel_health_cache)
        }