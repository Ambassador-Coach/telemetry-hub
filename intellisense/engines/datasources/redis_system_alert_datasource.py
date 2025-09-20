"""
Redis System Alert Data Source for IntelliSense

Provides a Redis-based data source that consumes from TESTRADE's system alert events stream
and yields SystemAlertTimelineEvent objects for the IntelliSense system.
"""

import logging
import uuid
from typing import Iterator, Optional, Dict, Any, Callable, TYPE_CHECKING
from queue import SimpleQueue, Empty

from intellisense.core.interfaces import IDataSource
from intellisense.core.types import SystemAlertTimelineEvent, TestSessionConfig
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class RedisSystemAlertDataSource(IDataSource):
    """
    Data source that consumes system alert events
    from TESTRADE Redis streams and yields SystemAlertTimelineEvents.
    """
    def __init__(self, session_config: 'TestSessionConfig', pds_gsi_provider: Callable[[], int]):
        self.session_config = session_config
        self._pds_gsi_provider = pds_gsi_provider
        self._is_active = False

        self._event_queue: 'SimpleQueue[Optional[SystemAlertTimelineEvent]]' = SimpleQueue()
        self._consumer: Optional[RedisStreamConsumerBase] = None

        self._initialize_consumer()
        logger.info("RedisSystemAlertDataSource initialized.")

    def _initialize_consumer(self):
        """Initialize Redis stream consumer for system alert events."""
        try:
            consumer_name = f"{self.session_config.redis_consumer_name_prefix}_{uuid.uuid4().hex[:8]}_system_alert"
            
            self._consumer = RedisStreamConsumerBase(
                redis_host=self.session_config.redis_host,
                redis_port=self.session_config.redis_port,
                redis_db=self.session_config.redis_db,
                redis_password=self.session_config.redis_password,
                stream_name=self.session_config.redis_stream_system_alerts,
                consumer_group_name=self.session_config.redis_consumer_group_name,
                consumer_name=consumer_name,
                message_handler=self._on_redis_message_received,
                logger_instance=logger
            )
            logger.info(f"System alert consumer initialized for stream: {self.session_config.redis_stream_system_alerts}")
        except Exception as e:
            logger.error(f"Failed to initialize system alert consumer: {e}", exc_info=True)

    def _on_redis_message_received(self, message_id: str, parsed_data: Dict[str, Any]) -> bool:
        """Handle incoming Redis messages and convert to SystemAlertTimelineEvent."""
        try:
            # Use the from_redis_message static method to create the event
            event = SystemAlertTimelineEvent.from_redis_message(parsed_data, self._pds_gsi_provider())
            
            if event:
                self._event_queue.put(event)
                logger.debug(f"System alert event queued: {event.data.severity} - {event.data.alert_message}")
                return True
            else:
                logger.debug(f"System alert message filtered out or failed parsing: {message_id}")
                return True  # Still acknowledge the message
        except Exception as e:
            logger.error(f"Error processing system alert message {message_id}: {e}", exc_info=True)
            return False

    def is_active(self) -> bool:
        """Check if data source is currently active."""
        return self._is_active

    def start(self) -> None:
        """Start the data source."""
        if self._is_active:
            logger.warning("RedisSystemAlertDataSource is already active")
            return

        try:
            if self._consumer:
                self._consumer.start_consuming()
                self._is_active = True
                logger.info("RedisSystemAlertDataSource started successfully")
            else:
                logger.error("Cannot start RedisSystemAlertDataSource: consumer not initialized")
        except Exception as e:
            logger.error(f"Failed to start RedisSystemAlertDataSource: {e}", exc_info=True)

    def stop(self) -> None:
        """Stop the data source."""
        if not self._is_active:
            return

        try:
            if self._consumer:
                self._consumer.stop_consuming()
            
            # Signal end of stream
            self._event_queue.put(None)
            self._is_active = False
            logger.info("RedisSystemAlertDataSource stopped successfully")
        except Exception as e:
            logger.error(f"Error stopping RedisSystemAlertDataSource: {e}", exc_info=True)

    def load_timeline_data(self) -> bool:
        """Loads data for replay. Returns True on success. No-op for live sources."""
        # This is a live data source, so no loading is required
        return True

    def get_data_stream(self) -> Iterator[SystemAlertTimelineEvent]:
        """
        Get a stream of system alert timeline events.
        This yields live events from the Redis stream.
        """
        while True:
            try:
                # Block for a short time to get events
                event = self._event_queue.get(timeout=1.0)
                
                if event is None:
                    # End of stream signal
                    break
                    
                yield event
                
            except Empty:
                # Timeout occurred, check if we should continue
                if not self._is_active:
                    break
                continue
            except Exception as e:
                logger.error(f"Error in system alert data stream: {e}", exc_info=True)
                if not self._is_active:
                    break
                continue

        logger.info("System alert data stream ended")
