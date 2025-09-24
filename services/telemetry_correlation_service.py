"""
PROJECT UNFUZZIFY: Telemetry Correlation Service (TCS)

The UNFUZZIFIER - Transforms disparate stream chaos into perfect chronological order.

TODO: Configure supervisor to start this service in the container:
  - Add to /etc/supervisor/conf.d/telemetry.conf
  - Command: python3 /home/telemetry/telemetry-hub/services/telemetry_correlation_service.py
  - Ensure Redis is available before starting
  - Consider dependency on babysitter service

This service:
1. Subscribes to ALL 29+ Redis streams as pure observer
2. Normalizes timestamp formats and correlation IDs
3. Sorts events by nanosecond precision timestamps
4. Publishes unified timeline to testrade:unified-timeline

Architecture:
- Pure Observer Pattern: Zero impact on trading operations
- Time-Windowed Buffering: 100ms window for nanosecond sorting
- Correlation Chain Reconstruction: Links related events by correlation ID
- Unified Output Format: Single stream for GUI and IntelliSense consumption
"""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Set
from sortedcontainers import SortedDict

import redis.asyncio as redis
from utils.thread_safe_uuid import get_thread_safe_uuid
from utils.global_config import GlobalConfig
from interfaces.services.telemetry_correlation_service import ITelemetryCorrelationService, TCSStats


logger = logging.getLogger(__name__)


@dataclass
class UnifiedEvent:
    """Normalized event structure for unified timeline"""
    timestamp_ns: int                    # Nanosecond precision timestamp
    correlation_id: Optional[str]        # Correlation ID for event chaining
    stream_source: str                   # Original Redis stream name
    message_id: str                      # Original Redis message ID
    event_type: str                      # Normalized event type
    source_component: str                # Source component name
    normalized_payload: Dict[str, Any]   # Normalized payload structure
    raw_data: Dict[str, Any]            # Original message for debugging
    sequence_number: int = field(default=0)  # Global sequence for ordering


class TelemetryCorrelationService(ITelemetryCorrelationService):
    """
    PROJECT UNFUZZIFY: The core service that eliminates timeline chaos
    
    Consumes all TESTRADE Redis streams and produces a single, perfectly
    ordered timeline stream with nanosecond precision.
    """
    
    def __init__(self, config: GlobalConfig, redis_client: redis.Redis):
        self.config = config
        self.redis_client = redis_client
        
        # Time-windowed buffer for nanosecond sorting (100ms window)
        self.event_buffer = SortedDict()  # timestamp_ns -> List[UnifiedEvent]
        self.buffer_window_ns = 100_000_000  # 100ms in nanoseconds
        
        # Correlation tracking for event chain reconstruction
        self.correlation_cache: Dict[str, List[UnifiedEvent]] = defaultdict(list)
        
        # Global sequence counter for perfect ordering
        self.global_sequence = 0
        
        # Service state
        self.running = False
        self.stop_event = asyncio.Event()
        
        # Performance metrics
        self.events_processed = 0
        self.events_published = 0
        self.last_flush_time = time.perf_counter_ns()
        
        # Get all TESTRADE streams to monitor
        self.monitored_streams = self._get_all_testrade_streams()
        
        logger.info(f"TCS initialized - monitoring {len(self.monitored_streams)} streams")
    
    def _get_all_testrade_streams(self) -> List[str]:
        """Get all TESTRADE Redis stream names from config"""
        streams = []
        
        # Core streams
        streams.extend([
            self.config.redis_stream_raw_ocr,
            self.config.redis_stream_cleaned_ocr,
            self.config.redis_stream_order_requests,
            self.config.redis_stream_validated_orders,
            self.config.redis_stream_order_fills,
            self.config.redis_stream_order_status,
            self.config.redis_stream_order_rejections,
            self.config.redis_stream_market_ticks,
            self.config.redis_stream_market_quotes,
            self.config.redis_stream_market_trades,
            self.config.redis_stream_filtered_market_quotes,
            self.config.redis_stream_risk_actions,
            self.config.redis_stream_position_updates,
            self.config.redis_stream_enriched_position_updates,
            self.config.redis_stream_broker_raw_messages,
            self.config.redis_stream_broker_errors,
            self.config.redis_stream_trade_lifecycle_events,
            self.config.redis_stream_trading_state_changes,
            self.config.redis_stream_config_changes,
            self.config.redis_stream_maf_decisions,
            self.config.redis_stream_ocr_conditioning_decisions,
            self.config.redis_stream_signal_decisions,
            self.config.redis_stream_execution_decisions,
        ])
        
        # Health and command streams
        streams.extend([
            self.config.redis_stream_commands_from_gui,
            self.config.redis_stream_responses_to_gui,
            self.config.redis_stream_core_health,
            self.config.redis_stream_babysitter_health,
            self.config.redis_stream_redis_instance_health,
            self.config.redis_stream_ipc_alerts,
        ])
        
        # Filter out None values and duplicates
        return list(set(filter(None, streams)))
    
    def _extract_timestamp_ns(self, raw_data: Dict[str, Any]) -> int:
        """
        Surgically extracts the highest-precision timestamp available.
        This is the core of the "Unfuzzify" mission.
        """
        payload = raw_data.get("payload", {})
        metadata = raw_data.get("metadata", {})

        # PRIORITY 1: The Golden Timestamp (Actual Event Time)
        # Check the payload first for the most accurate source-of-truth timestamps.
        if isinstance(payload, dict):
            # The absolute best-case scenario for OCR events
            if ts := payload.get("t0_capture_ns"):
                return int(ts)
            # Other OCR pipeline timestamps
            ocr_timestamp_fields = ['t1_capture_done_ns', 't2_preproc_done_ns',
                                   't3_contours_done_ns', 't4_tess_done_ns']
            for field in ocr_timestamp_fields:
                if ts := payload.get(field):
                    return int(ts)
            # A common pattern for broker/price events
            if ts := payload.get("timestamp_ns"):
                return int(ts)

        # PRIORITY 2: Metadata Timestamp (TCS Ingest Time)
        # This is less accurate but better than nothing.
        if isinstance(metadata, dict):
            if ts := metadata.get("timestamp_ns"):
                return int(ts)
            if ts := metadata.get("timestampNanos"): # Legacy support
                return int(ts)

        # PRIORITY 3: OCR-specific nanosecond timestamps (direct in message - legacy)
        ocr_timestamp_fields = ['t0_capture_ns', 't1_capture_done_ns', 't2_preproc_done_ns',
                               't3_contours_done_ns', 't4_tess_done_ns']
        for field in ocr_timestamp_fields:
            if ts := raw_data.get(field):
                return int(ts)

        # PRIORITY 4: Fallback Timestamps (Lower Precision)
        # Convert from other units if nanoseconds are not available.
        if isinstance(payload, dict):
            if ts := payload.get("epoch_timestamp_s"):
                return int(float(ts) * 1_000_000_000)
            if ts := payload.get("timestamp"): # Often in seconds or ms
                ts_float = float(ts)
                # Heuristic to check if it's seconds or milliseconds
                if ts_float > 1_000_000_000_000: # Likely milliseconds
                    return int(ts_float * 1_000_000)
                else: # Likely seconds
                    return int(ts_float * 1_000_000_000)

        # Legacy payload timestamp
        if ts_ns := raw_data.get("timestamp_ns"):
            return int(ts_ns)

        # Convert epoch timestamp from metadata
        if isinstance(metadata, dict):
            if epoch_ts := metadata.get("epoch_timestamp_s"):
                return int(epoch_ts * 1_000_000_000)

        # OCR frame timestamp (milliseconds to nanoseconds)
        if frame_ts := raw_data.get("frame_timestamp"):
            return int(frame_ts * 1_000_000)

        # GUI millisecond timestamp
        if ts_ms := raw_data.get("timestamp"):
            return int(ts_ms * 1_000_000)

        # PRIORITY 5: DESPERATION FALLBACK (Last Resort)
        # If no timestamp is found anywhere, use current time and log appropriately.
        available_fields = list(raw_data.keys())
        if set(available_fields) == {'metadata', 'payload'}:
            # Health stream format - use current time silently
            return time.perf_counter_ns()
        else:
            # Other streams - log critical warning for chronology compromise
            payload_keys = list(payload.keys()) if isinstance(payload, dict) else []
            logger.critical(f"CRITICAL: No valid timestamp found in message from stream '{metadata.get('originalStream')}'. Using current time. CHRONOLOGY IS COMPROMISED FOR THIS EVENT. Payload keys: {payload_keys}")
            return time.perf_counter_ns()
    
    def _extract_correlation_id(self, raw_data: Dict[str, Any]) -> Optional[str]:
        """Extract correlation ID with pattern matching"""
        # Standard metadata location
        if metadata := raw_data.get("metadata"):
            if corr_id := metadata.get("correlationId"):
                return str(corr_id)

        # Direct field location (OCR messages)
        if corr_id := raw_data.get("correlation_id"):
            return str(corr_id)

        # Nested payload location
        if payload := raw_data.get("payload"):
            if corr_id := payload.get("correlation_id"):
                return str(corr_id)

        return None
    
    def _extract_event_type(self, raw_data: Dict[str, Any]) -> str:
        """Extract and normalize event type"""
        # Standard metadata location
        if metadata := raw_data.get("metadata"):
            if event_type := metadata.get("eventType"):
                return str(event_type)
        
        # Legacy payload location
        if event_type := raw_data.get("event_type"):
            return str(event_type)
        
        # Command type for GUI streams
        if command_type := raw_data.get("command_type"):
            return f"GUI_COMMAND_{command_type.upper()}"
        
        return "UNKNOWN_EVENT_TYPE"
    
    def _extract_source_component(self, raw_data: Dict[str, Any]) -> str:
        """Extract source component with fallback"""
        # Standard metadata location
        if metadata := raw_data.get("metadata"):
            if source := metadata.get("sourceComponent"):
                return str(source)
        
        # Legacy source field
        if source := raw_data.get("source"):
            return str(source)
        
        return "UNKNOWN_SOURCE"
    
    def _normalize_payload(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize payload structure for unified format"""
        # If already has payload structure, use it
        if payload := raw_data.get("payload"):
            return payload
        
        # Otherwise, create payload from raw data (excluding metadata)
        normalized = {}
        for key, value in raw_data.items():
            if key not in ["metadata"]:
                normalized[key] = value
        
        return normalized

    def _normalize_event(self, stream_name: str, message_id: str, raw_data: Dict[str, Any]) -> UnifiedEvent:
        """
        Surgical normalization of ANY stream message into unified format
        """
        # Extract all components with fallback chains
        timestamp_ns = self._extract_timestamp_ns(raw_data)
        correlation_id = self._extract_correlation_id(raw_data)
        event_type = self._extract_event_type(raw_data)
        source_component = self._extract_source_component(raw_data)
        normalized_payload = self._normalize_payload(raw_data)

        # Assign global sequence number
        self.global_sequence += 1

        # Create unified event
        return UnifiedEvent(
            timestamp_ns=timestamp_ns,
            correlation_id=correlation_id,
            stream_source=stream_name,
            message_id=message_id,
            event_type=event_type,
            source_component=source_component,
            normalized_payload=normalized_payload,
            raw_data=raw_data,
            sequence_number=self.global_sequence
        )

    def _add_to_buffer(self, event: UnifiedEvent):
        """Add event to time-windowed buffer for nanosecond sorting"""
        timestamp_ns = event.timestamp_ns

        # Add to sorted buffer
        if timestamp_ns not in self.event_buffer:
            self.event_buffer[timestamp_ns] = []
        self.event_buffer[timestamp_ns].append(event)

        # Add to correlation cache if correlation ID exists
        if event.correlation_id:
            self.correlation_cache[event.correlation_id].append(event)

        self.events_processed += 1

    def _flush_ready_events(self, current_time_ns: int):
        """
        Flush events that are outside the buffer window (ready for publishing)
        """
        cutoff_time = current_time_ns - self.buffer_window_ns
        ready_timestamps = []

        # Find timestamps ready for flushing
        for timestamp_ns in self.event_buffer:
            if timestamp_ns <= cutoff_time:
                ready_timestamps.append(timestamp_ns)
            else:
                break  # SortedDict is ordered, so we can break early

        # Flush ready events in chronological order
        for timestamp_ns in ready_timestamps:
            events = self.event_buffer.pop(timestamp_ns)

            # Sort events with same timestamp by sequence number
            events.sort(key=lambda e: e.sequence_number)

            # Publish each event
            for event in events:
                asyncio.create_task(self._publish_unified_event(event))

    async def _publish_unified_event(self, event: UnifiedEvent):
        """
        Publish event to testrade:unified-timeline with perfect structure
        """
        try:
            # Build correlation chain position if correlation ID exists
            chain_position = 0
            chain_length = 0
            if event.correlation_id and event.correlation_id in self.correlation_cache:
                chain_events = self.correlation_cache[event.correlation_id]
                chain_length = len(chain_events)
                # Find position in chain (sorted by timestamp)
                sorted_chain = sorted(chain_events, key=lambda e: e.timestamp_ns)
                chain_position = next((i for i, e in enumerate(sorted_chain) if e.sequence_number == event.sequence_number), 0)

            # Create unified timeline message
            unified_message = {
                "metadata": {
                    "eventId": get_thread_safe_uuid(),
                    "correlationId": event.correlation_id,
                    "timestamp_ns": event.timestamp_ns,
                    "epoch_timestamp_s": time.time(),
                    "eventType": "UNIFIED_TIMELINE_EVENT",
                    "sourceComponent": "TelemetryCorrelationService",
                    "originalStream": event.stream_source,
                    "originalEventType": event.event_type,
                    "originalSourceComponent": event.source_component
                },
                "payload": {
                    "unified_event": event.normalized_payload,
                    "chronological_sequence": event.sequence_number,
                    "correlation_chain_position": chain_position,
                    "correlation_chain_length": chain_length,
                    "original_message_id": event.message_id,
                    "processing_timestamp_ns": time.perf_counter_ns()
                }
            }

            # Publish to unified timeline stream (serialize to JSON)
            await self.redis_client.xadd(
                self.config.redis_stream_unified_timeline,
                {"json_payload": json.dumps(unified_message)},
                maxlen=self.config.redis_stream_max_length,
                approximate=True
            )

            self.events_published += 1

        except Exception as e:
            logger.error(f"Failed to publish unified event: {e}", exc_info=True)

    async def _process_stream_message(self, stream_name: str, message_id: str, fields: Dict[str, Any]):
        """Process a single message from any Redis stream"""
        try:
            # Handle different message formats
            raw_data = {}

            # Check for JSON payload field (standard format)
            if json_payload := fields.get('json_payload'):
                if isinstance(json_payload, bytes):
                    json_payload = json_payload.decode('utf-8')
                raw_data = json.loads(json_payload)

            # Check for direct field format (legacy)
            elif fields:
                raw_data = {k.decode('utf-8') if isinstance(k, bytes) else k:
                           v.decode('utf-8') if isinstance(v, bytes) else v
                           for k, v in fields.items()}

            # Check for associated binary blobs
            correlation_id = raw_data.get('correlation_id')
            if correlation_id:
                # Fetch any binary blobs associated with this message
                blob_keys = await self.redis_client.keys(f'binary_blob:{correlation_id}:part_*')
                if blob_keys:
                    blobs = []
                    for key in blob_keys:
                        blob_data = await self.redis_client.get(key)
                        if blob_data:
                            blobs.append(blob_data)

                    # Add blobs to raw_data
                    if blobs:
                        raw_data['binary_blobs'] = blobs
                        logger.debug(f"Fetched {len(blobs)} binary blobs for correlation_id: {correlation_id}")

                    # DO NOT DELETE - Let other consumers read them, TTL will expire them
                    # Pure observer pattern - we only read, never modify

            # Normalize the event
            unified_event = self._normalize_event(stream_name, message_id, raw_data)

            # Add to buffer for sorting
            self._add_to_buffer(unified_event)

        except Exception as e:
            logger.error(f"Failed to process message from {stream_name}: {e}", exc_info=True)

    async def _monitor_stream(self, stream_name: str):
        """Monitor a single Redis stream for new messages"""
        last_id = '$'  # Start from newest messages

        while not self.stop_event.is_set():
            try:
                # Read new messages from stream
                messages = await self.redis_client.xread(
                    {stream_name: last_id},
                    count=100,  # Process in batches
                    block=1000  # 1 second timeout
                )

                if messages:
                    for stream, stream_messages in messages:
                        for message_id, fields in stream_messages:
                            await self._process_stream_message(
                                stream_name,
                                message_id.decode('utf-8'),
                                fields
                            )
                            last_id = message_id

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error monitoring stream {stream_name}: {e}", exc_info=True)
                await asyncio.sleep(1)  # Brief pause before retry

    async def _buffer_flush_loop(self):
        """Periodic buffer flush to maintain timeline ordering"""
        while not self.stop_event.is_set():
            try:
                current_time = time.perf_counter_ns()

                # Flush ready events
                self._flush_ready_events(current_time)

                # Log performance metrics every 10 seconds
                if current_time - self.last_flush_time > 10_000_000_000:  # 10 seconds
                    logger.info(
                        f"TCS Performance: {self.events_processed} processed, "
                        f"{self.events_published} published, "
                        f"{len(self.event_buffer)} buffered, "
                        f"{len(self.correlation_cache)} correlations"
                    )
                    self.last_flush_time = current_time

                # Sleep for 10ms (buffer flush frequency)
                await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in buffer flush loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def start(self):
        """Start the TCS service"""
        if self.running:
            logger.warning("TCS already running")
            return

        logger.info("Starting Telemetry Correlation Service (PROJECT UNFUZZIFY)")
        self.running = True
        self.stop_event.clear()

        # Create tasks for all stream monitors
        tasks = []

        # Monitor all TESTRADE streams
        for stream_name in self.monitored_streams:
            task = asyncio.create_task(self._monitor_stream(stream_name))
            tasks.append(task)
            logger.debug(f"Started monitoring stream: {stream_name}")

        # Start buffer flush loop
        flush_task = asyncio.create_task(self._buffer_flush_loop())
        tasks.append(flush_task)

        logger.info(f"TCS started - monitoring {len(self.monitored_streams)} streams")

        try:
            # Wait for all tasks to complete
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("TCS service cancelled")
        finally:
            # Cancel all tasks
            for task in tasks:
                if not task.done():
                    task.cancel()

            # Wait for tasks to finish cancellation
            await asyncio.gather(*tasks, return_exceptions=True)

            self.running = False
            logger.info("TCS service stopped")

    async def stop(self):
        """Stop the TCS service"""
        if not self.running:
            return

        logger.info("Stopping Telemetry Correlation Service")
        self.stop_event.set()

        # Final flush of remaining events
        current_time = time.perf_counter_ns()
        self._flush_ready_events(current_time + self.buffer_window_ns)  # Flush everything

        logger.info(
            f"TCS stopped - Final stats: {self.events_processed} processed, "
            f"{self.events_published} published"
        )

    def get_stats(self) -> TCSStats:
        """Get TCS performance statistics"""
        return TCSStats(
            running=self.running,
            events_processed=self.events_processed,
            events_published=self.events_published,
            events_buffered=len(self.event_buffer),
            correlations_tracked=len(self.correlation_cache),
            monitored_streams=len(self.monitored_streams),
            buffer_window_ms=self.buffer_window_ns / 1_000_000,
            global_sequence=self.global_sequence
        )

    def is_running(self) -> bool:
        """Check if TCS is currently running"""
        return self.running

    def get_monitored_streams(self) -> List[str]:
        """Get list of monitored Redis streams"""
        return self.monitored_streams.copy()

    def get_correlation_chain(self, correlation_id: str) -> List[Dict[str, Any]]:
        """Get complete event chain for a correlation ID"""
        if correlation_id not in self.correlation_cache:
            return []

        events = self.correlation_cache[correlation_id]

        # Sort by timestamp for chronological order
        sorted_events = sorted(events, key=lambda e: e.timestamp_ns)

        # Convert to dict format
        return [
            {
                "timestamp_ns": event.timestamp_ns,
                "sequence_number": event.sequence_number,
                "stream_source": event.stream_source,
                "event_type": event.event_type,
                "source_component": event.source_component,
                "message_id": event.message_id,
                "payload": event.normalized_payload
            }
            for event in sorted_events
        ]
