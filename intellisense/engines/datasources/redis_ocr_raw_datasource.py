"""
Redis Raw OCR Data Source for IntelliSense

Provides a Redis-based OCR data source that consumes from TESTRADE's raw OCR events stream
and yields OCRTimelineEvent objects for the IntelliSense system.
"""

import logging
import uuid
from typing import Iterator, Optional, Dict, Any, TYPE_CHECKING, Callable
from queue import SimpleQueue, Empty

from intellisense.core.interfaces import IOCRDataSource
from intellisense.core.types import OCRTimelineEvent, TestSessionConfig
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase

if TYPE_CHECKING:
    pass  # SimpleQueue imported directly for better compatibility

logger = logging.getLogger(__name__)


class RedisRawOCRDataSource(IOCRDataSource):
    """
    Data source that consumes raw OCR data (OCRParsedData equivalent)
    from a TESTRADE Redis stream and yields OCRTimelineEvents.
    """

    def __init__(self, session_config: TestSessionConfig, pds_gsi_provider: Callable[[], int]):
        """
        Initializes the RedisRawOCRDataSource.

        Args:
            session_config: The TestSessionConfig containing Redis connection details
                            and the specific stream name for raw OCR data.
            pds_gsi_provider: A callable that provides the next global sequence ID
                              from ProductionDataCaptureSession.
        """
        self.session_config = session_config
        self._pds_gsi_provider = pds_gsi_provider  # To assign global_sequence_id to events
        self._is_active = False

        # Internal queue to decouple Redis I/O thread from iteration by EpochTimelineGenerator
        # This allows the Redis consumer to keep fetching while timeline generator processes.
        self._event_queue: SimpleQueue[Optional[OCRTimelineEvent]] = SimpleQueue()

        self.consumer_name = f"{session_config.redis_consumer_name_prefix}_rawocr_{uuid.uuid4().hex[:4]}"

        self._consumer = RedisStreamConsumerBase(
            redis_host=session_config.redis_host,
            redis_port=session_config.redis_port,
            redis_db=session_config.redis_db,
            redis_password=session_config.redis_password,
            stream_name=session_config.redis_stream_raw_ocr,  # Use specific stream from config
            consumer_group_name=session_config.redis_consumer_group_name,
            consumer_name=self.consumer_name,
            message_handler=self._on_redis_message_received,
            start_id='0'  # Start from beginning for thread-based consumption
        )
        logger.info(f"RedisRawOCRDataSource initialized for stream: '{session_config.redis_stream_raw_ocr}', consumer: '{self.consumer_name}'")

    def _on_redis_message_received(self, message_id: str, message_data: Dict[str, Any]):
        """Callback for RedisStreamConsumerBase when a new message arrives."""
        # logger.debug(f"RedisRawOCRDataSource received raw message from Redis: ID {message_id}")
        gsi = self._pds_gsi_provider()
        timeline_event = OCRTimelineEvent.from_redis_message(message_data, gsi)
        if timeline_event:
            self._event_queue.put(timeline_event)
        else:
            logger.warning(f"Failed to parse Redis message ID {message_id} into OCRTimelineEvent. Data: {str(message_data)[:200]}")

    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> None:
        if self._is_active:
            logger.warning("RedisRawOCRDataSource already started.")
            return
        logger.info("Starting RedisRawOCRDataSource consumption...")
        self._consumer.start_consuming()  # Starts the background thread
        self._is_active = True

    def stop(self) -> None:
        if not self._is_active:
            logger.info("RedisRawOCRDataSource already stopped or not started.")
            return
        logger.info("Stopping RedisRawOCRDataSource consumption...")
        self._consumer.stop_consuming()
        self._is_active = False
        # Put a None sentinel to unblock the iterator if it's waiting
        self._event_queue.put(None)
        logger.info("RedisRawOCRDataSource stopped.")

    def load_timeline_data(self) -> bool:
        """
        For Redis-based sources, "loading" means ensuring the connection
        and subscription are ready. Actual data is streamed.
        If replaying historical data from a stream, this might involve setting
        the consumer to start from a specific ID or '0'.
        """
        # The RedisStreamConsumerBase handles connection in start_consuming.
        # For now, this method can just return True if the consumer is configured.
        # If we need to read entire stream history for "replay", consumer's start_id = '0' handles it.
        logger.info(f"RedisRawOCRDataSource: 'load_timeline_data' called. Stream '{self.session_config.redis_stream_raw_ocr}' will be consumed from specified start_id.")
        return True  # Assume ready to stream

    def get_data_stream(self) -> Iterator[OCRTimelineEvent]:
        """
        Yields OCRTimelineEvents as they are received from the Redis stream
        via the internal queue.
        """
        if not self._is_active:
            logger.warning("Attempted to get data stream from inactive RedisRawOCRDataSource.")
            return iter([])  # Return empty iterator

        while self._is_active or not self._event_queue.empty():  # Process queue even if stopping
            try:
                event = self._event_queue.get(timeout=0.1)  # Short timeout to be responsive to stop
                if event is None:  # Sentinel for stop
                    logger.debug("RedisRawOCRDataSource: Stop sentinel received in get_data_stream.")
                    break
                if isinstance(event, OCRTimelineEvent):
                    yield event
                # else: Malformed item in queue, logged by _on_redis_message_received
            except Empty:  # queue.Empty renamed to Empty in Python 3.7+
                if not self._is_active and self._event_queue.empty():
                    break  # Consumer stopped and queue is drained
                continue  # Continue waiting if active
            except Exception as e:
                logger.error(f"Error in RedisRawOCRDataSource get_data_stream: {e}", exc_info=True)
                break  # Exit loop on unexpected error
        logger.info("RedisRawOCRDataSource get_data_stream iterator finished.")

    def get_replay_progress(self) -> Dict[str, Any]:
        """
        For a live Redis stream, "progress" is less defined than file replay.
        Could report number of messages processed from queue or consumer stats.
        """
        # This is a placeholder. True progress for a live stream is continuous.
        # Could potentially query Redis for stream length and messages processed by consumer group.
        return {
            'source_type': 'RedisRawOCR',
            'total_events': -1,  # Indicates a live stream or unknown total
            'events_loaded': -1,  # Or track messages processed from queue
            'progress_percentage_loaded': 0.0  # Not applicable in the same way as file replay
        }

    def cleanup(self) -> None:
        """Ensures the consumer is stopped."""
        self.stop()
        logger.info("RedisRawOCRDataSource cleaned up.")
