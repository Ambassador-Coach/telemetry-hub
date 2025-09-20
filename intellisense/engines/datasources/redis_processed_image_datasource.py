#!/usr/bin/env python3
"""
RedisProcessedImageDataSource - Data source for processed ROI image frames from Redis streams.

This module implements the data source for consuming processed ROI image frames from
TESTRADE's Redis stream 'testrade:processed-roi-image-frames'. It implements both
IDataSource and IVisualDataSource interfaces for compatibility with the IntelliSense
data pipeline.
"""

import logging
import threading
import time
import uuid
from queue import SimpleQueue, Empty
from typing import Iterator, Optional, Dict, Any, List, Callable

from intellisense.core.interfaces import IDataSource, IVisualDataSource
from intellisense.core.types import ProcessedImageFrameTimelineEvent, VisualTimelineEvent, TimelineEvent
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase

logger = logging.getLogger(__name__)

class RedisProcessedImageDataSource(IVisualDataSource, RedisStreamConsumerBase):
    """
    Data source for processed ROI image frames from TESTRADE Redis streams.

    This class consumes from 'testrade:processed-roi-image-frames' and converts
    Redis messages to ProcessedImageFrameTimelineEvent objects. It implements
    both IDataSource and IVisualDataSource interfaces for compatibility.
    """

    def __init__(self,
                 redis_host: str = "127.0.0.1",
                 redis_port: int = 6379,
                 redis_db: int = 0,
                 stream_name: str = "testrade:processed-roi-image-frames",
                 consumer_group: str = "intellisense_processed_image_group",
                 consumer_name: str = "intellisense_processed_image_consumer",
                 max_queue_size: int = 1000):
        """
        Initialize the Redis processed image data source.

        Args:
            redis_host: Redis server hostname
            redis_port: Redis server port
            redis_db: Redis database number
            stream_name: Name of the Redis stream to consume from
            consumer_group: Consumer group name for Redis streams
            consumer_name: Consumer name within the group
            max_queue_size: Maximum size of the internal event queue
        """
        # Initialize base class
        super().__init__(
            redis_host=redis_host,
            redis_port=redis_port,
            redis_db=redis_db,
            stream_name=stream_name,
            consumer_group_name=consumer_group,
            consumer_name=consumer_name
        )

        self.logger = logging.getLogger(f"{__name__}.RedisProcessedImageDataSource")

        # Event queue for thread-safe communication
        self.event_queue: SimpleQueue[ProcessedImageFrameTimelineEvent] = SimpleQueue()
        self.max_queue_size = max_queue_size

        # State management
        self._is_active = False
        self._consumer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Global sequence ID for PDS integration
        self._current_gsi = 0
        self._gsi_lock = threading.Lock()

        # Statistics
        self.events_processed = 0
        self.events_queued = 0
        self.parse_errors = 0

        self.logger.info(f"RedisProcessedImageDataSource initialized for stream '{stream_name}'")

    def _get_next_gsi(self) -> int:
        """Get the next global sequence ID in a thread-safe manner."""
        with self._gsi_lock:
            self._current_gsi += 1
            return self._current_gsi

    def _on_redis_message_received(self, stream_name: str, message_id: str, fields: Dict[str, Any]) -> None:
        """
        Handle incoming Redis messages and convert to ProcessedImageFrameTimelineEvent.

        This method is called by the base RedisStreamConsumerBase when a new message
        is received from the Redis stream.

        Args:
            stream_name: Name of the Redis stream
            message_id: Redis message ID
            fields: Message fields from Redis
        """
        try:
            # Check queue size to prevent memory issues
            if self.event_queue.qsize() >= self.max_queue_size:
                self.logger.warning(f"Event queue full ({self.max_queue_size}), dropping oldest events")
                # Remove some old events to make space
                for _ in range(min(100, self.max_queue_size // 10)):
                    try:
                        self.event_queue.get_nowait()
                    except Empty:
                        break

            # Convert Redis message to ProcessedImageFrameTimelineEvent
            redis_msg_dict = {
                "id": message_id,
                "stream": stream_name,
                "metadata": fields.get("metadata", {}),
                "payload": fields.get("payload", {})
            }

            # Parse metadata and payload if they're JSON strings
            if isinstance(redis_msg_dict["metadata"], str):
                import json
                try:
                    redis_msg_dict["metadata"] = json.loads(redis_msg_dict["metadata"])
                except json.JSONDecodeError as e:
                    self.logger.error(f"Failed to parse metadata JSON: {e}")
                    redis_msg_dict["metadata"] = {}

            if isinstance(redis_msg_dict["payload"], str):
                import json
                try:
                    redis_msg_dict["payload"] = json.loads(redis_msg_dict["payload"])
                except json.JSONDecodeError as e:
                    self.logger.error(f"Failed to parse payload JSON: {e}")
                    redis_msg_dict["payload"] = {}

            # Generate global sequence ID
            pds_gsi = self._get_next_gsi()

            # Create ProcessedImageFrameTimelineEvent
            event = ProcessedImageFrameTimelineEvent.from_redis_message(redis_msg_dict, pds_gsi)

            if event is not None:
                # Add to queue
                self.event_queue.put(event)
                self.events_processed += 1
                self.events_queued += 1

                self.logger.debug(f"Processed image event queued: {event.correlation_id} (GSI: {pds_gsi})")
            else:
                self.parse_errors += 1
                self.logger.warning(f"Failed to create ProcessedImageFrameTimelineEvent from message {message_id}")

        except Exception as e:
            self.parse_errors += 1
            self.logger.error(f"Error processing Redis message {message_id}: {e}", exc_info=True)

    # IDataSource interface implementation
    def is_active(self) -> bool:
        """Check if data source is currently active."""
        return self._is_active and self._consumer_thread is not None and self._consumer_thread.is_alive()

    def start(self) -> None:
        """Start the data source and begin consuming from Redis stream."""
        if self._is_active:
            self.logger.warning("RedisProcessedImageDataSource already active")
            return

        try:
            self.logger.info("Starting RedisProcessedImageDataSource...")

            # Reset stop event
            self._stop_event.clear()

            # Start Redis consumer in background thread
            self._consumer_thread = threading.Thread(
                target=self._consumer_thread_main,
                name="RedisProcessedImageConsumer",
                daemon=True
            )

            self._is_active = True
            self._consumer_thread.start()

            self.logger.info("RedisProcessedImageDataSource started successfully")

        except Exception as e:
            self._is_active = False
            self.logger.error(f"Failed to start RedisProcessedImageDataSource: {e}", exc_info=True)
            raise

    def stop(self) -> None:
        """Stop the data source and disconnect from Redis stream."""
        if not self._is_active:
            self.logger.debug("RedisProcessedImageDataSource already stopped")
            return

        try:
            self.logger.info("Stopping RedisProcessedImageDataSource...")

            # Signal stop
            self._stop_event.set()
            self._is_active = False

            # Wait for consumer thread to finish
            if self._consumer_thread and self._consumer_thread.is_alive():
                self._consumer_thread.join(timeout=5.0)
                if self._consumer_thread.is_alive():
                    self.logger.warning("Consumer thread did not stop within timeout")

            # Stop base Redis consumer
            super().stop()

            self.logger.info("RedisProcessedImageDataSource stopped")

        except Exception as e:
            self.logger.error(f"Error stopping RedisProcessedImageDataSource: {e}", exc_info=True)

    def _consumer_thread_main(self) -> None:
        """Main loop for the Redis consumer thread."""
        self.logger.info("Redis consumer thread started")

        try:
            # Start the base Redis consumer
            super().start()

            # Keep thread alive while active
            while not self._stop_event.is_set():
                time.sleep(0.1)

        except Exception as e:
            self.logger.error(f"Error in consumer thread: {e}", exc_info=True)
        finally:
            self.logger.info("Redis consumer thread finished")

    def load_timeline_data(self) -> List[Any]:
        """Load timeline data (not applicable for live Redis streams)."""
        self.logger.warning("load_timeline_data not applicable for live Redis streams")
        return []

    def get_replay_progress(self) -> float:
        """Get replay progress (not applicable for live Redis streams)."""
        return 100.0  # Always "complete" for live streams

    # IVisualDataSource interface implementation
    def get_visual_stream(self) -> Iterator[VisualTimelineEvent]:
        """
        Get a stream of visual timeline events.

        This method yields ProcessedImageFrameTimelineEvent objects as VisualTimelineEvent
        base types for compatibility with the visual data pipeline.

        Yields:
            VisualTimelineEvent: Processed image frame events
        """
        self.logger.info("Starting visual stream from processed image data source")

        while self.is_active() or not self.event_queue.empty():
            try:
                # Get event from queue with timeout
                event = self.event_queue.get(timeout=1.0)
                self.events_queued -= 1

                # Yield as VisualTimelineEvent base type
                yield event

            except Empty:
                # No events available, continue if still active
                if not self.is_active():
                    break
                continue
            except Exception as e:
                self.logger.error(f"Error in visual stream: {e}", exc_info=True)
                break

        self.logger.info("Visual stream ended")

    # IDataSource delegation for compatibility
    def get_data_stream(self) -> Iterator[ProcessedImageFrameTimelineEvent]:
        """
        Get a stream of ProcessedImageFrameTimelineEvent objects.

        This method provides direct access to the processed image events
        for use by ProductionDataCaptureSession and other components.

        Yields:
            ProcessedImageFrameTimelineEvent: Processed image frame events
        """
        self.logger.info("Starting data stream from processed image data source")

        while self.is_active() or not self.event_queue.empty():
            try:
                # Get event from queue with timeout
                event = self.event_queue.get(timeout=1.0)
                self.events_queued -= 1

                yield event

            except Empty:
                # No events available, continue if still active
                if not self.is_active():
                    break
                continue
            except Exception as e:
                self.logger.error(f"Error in data stream: {e}", exc_info=True)
                break

        self.logger.info("Data stream ended")

    def get_statistics(self) -> Dict[str, Any]:
        """Get data source statistics."""
        return {
            "events_processed": self.events_processed,
            "events_queued": self.events_queued,
            "parse_errors": self.parse_errors,
            "queue_size": self.event_queue.qsize(),
            "max_queue_size": self.max_queue_size,
            "is_active": self.is_active(),
            "current_gsi": self._current_gsi
        }
