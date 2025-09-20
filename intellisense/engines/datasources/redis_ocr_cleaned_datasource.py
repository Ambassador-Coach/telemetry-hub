#!/usr/bin/env python3
"""
Redis Cleaned OCR Data Source for IntelliSense

Consumes cleaned OCR snapshot events from TESTRADE Redis streams.
"""

import logging
from typing import Iterator, Optional, Callable, Dict, Any
from queue import SimpleQueue, Empty
from intellisense.config.session_config import TestSessionConfig
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase
from intellisense.core.types import OCRTimelineEvent
from intellisense.core.interfaces import IOCRDataSource

logger = logging.getLogger(__name__)

class RedisCleanedOCRDataSource(IOCRDataSource):
    """
    Redis data source for cleaned OCR snapshot events.
    
    Consumes from testrade:cleaned-ocr-snapshots stream and converts
    Redis messages to OCRTimelineEvent objects.
    """
    
    def __init__(self, session_config: TestSessionConfig, gsi_provider: Callable[[], int]):
        """
        Initialize Redis cleaned OCR data source.
        
        Args:
            session_config: Session configuration with Redis settings
            gsi_provider: Function to get next global sequence ID
        """
        self.session_config = session_config
        self.gsi_provider = gsi_provider
        self.logger = logger

        # Internal event queue for decoupling Redis I/O from iteration
        self._event_queue: SimpleQueue[Optional[OCRTimelineEvent]] = SimpleQueue()

        # Create Redis stream consumer for cleaned OCR snapshots
        import uuid
        consumer_name = f"pds_consumer_cleanedocr_{uuid.uuid4().hex[:4]}"
        self.consumer = RedisStreamConsumerBase(
            stream_name=session_config.redis_stream_cleaned_ocr,
            consumer_group_name="intellisense-group",
            consumer_name=consumer_name,
            redis_host=session_config.redis_host,
            redis_port=session_config.redis_port,
            redis_db=session_config.redis_db,
            redis_password=session_config.redis_password,
            message_handler=self._on_cleaned_ocr_message_received
        )
        
        self.logger.info(f"RedisCleanedOCRDataSource initialized for stream: '{session_config.redis_stream_cleaned_ocr}', consumer: '{self.consumer.consumer_name}'")

    def _on_cleaned_ocr_message_received(self, message_id: str, message_data: Dict[str, Any]):
        """
        Internal message handler for Redis stream messages.
        Called by RedisStreamConsumerBase when new messages arrive.
        """
        try:
            timeline_event = OCRTimelineEvent.from_redis_message(
                message_data,
                self.gsi_provider()
            )
            if timeline_event:
                self._event_queue.put(timeline_event)
                self.logger.debug(f"Queued cleaned OCR event: {timeline_event.event_type}, GSI: {timeline_event.global_sequence_id}")
            else:
                self.logger.warning(f"Failed to parse cleaned OCR Redis message ID {message_id}")
        except Exception as e:
            self.logger.error(f"Error processing cleaned OCR Redis message ID {message_id}: {e}", exc_info=True)
    
    def start(self):
        """Start consuming cleaned OCR snapshot events from Redis stream."""
        self.logger.info("Starting RedisCleanedOCRDataSource consumption...")
        self.consumer.start_consuming()

    def stop(self):
        """Stop consuming cleaned OCR snapshot events."""
        self.logger.info("Stopping RedisCleanedOCRDataSource consumption...")
        self.consumer.stop_consuming()
        # Add sentinel to unblock get_data_stream iterator
        self._event_queue.put(None)
    
    def get_data_stream(self) -> Iterator[OCRTimelineEvent]:
        """
        Get iterator of cleaned OCR timeline events from Redis stream.

        Yields:
            OCRTimelineEvent: Cleaned OCR snapshot events
        """
        if not self.is_active() and self._event_queue.empty():
            self.logger.warning("Attempted to get stream from inactive/empty RedisCleanedOCRDataSource.")
            return iter([])

        while self.is_active() or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.1)  # Short timeout
                if event is None:  # Sentinel on stop
                    break
                if isinstance(event, OCRTimelineEvent):  # Should always be true
                    yield event
            except Empty:
                if not self.is_active() and self._event_queue.empty():
                    break  # Consumer stopped and queue drained
                continue
            except Exception as e:
                self.logger.error(f"Error in RedisCleanedOCRDataSource get_data_stream: {e}", exc_info=True)
                break

        self.logger.info("RedisCleanedOCRDataSource get_data_stream iterator finished.")
    
    def is_active(self) -> bool:
        """Check if the data source is actively consuming."""
        return self.consumer._is_consuming if hasattr(self.consumer, '_is_consuming') else False
    
    def get_status(self) -> dict:
        """Get status information about the data source."""
        return {
            'stream_name': self.session_config.redis_stream_cleaned_ocr,
            'consumer_name': self.consumer.consumer_name,
            'consumer_group': self.consumer.consumer_group_name,
            'active': self.is_active()
        }

    # Required abstract methods from IOCRDataSource (get_data_stream implemented above)

    def load_timeline_data(self) -> bool:
        """Load timeline data (no-op for live Redis streams)."""
        return True  # Redis streams are live, no loading needed

    def get_replay_progress(self) -> dict:
        """Get replay progress (not applicable for live Redis streams)."""
        return {
            'source_type': 'redis_live',
            'total_events': 0,  # Unknown for live streams
            'events_loaded': 0,  # Not applicable
            'progress_percentage_loaded': 100.0  # Always "loaded" for live streams
        }
