"""
Redis Broker Data Source for IntelliSense

Provides a Redis-based broker data source that consumes from TESTRADE's order lifecycle streams
and yields BrokerTimelineEvent objects for the IntelliSense system.
"""

import logging
import uuid
from typing import Iterator, Optional, Dict, Any, List, Callable, TYPE_CHECKING
from queue import SimpleQueue, Empty

from intellisense.core.interfaces import IBrokerDataSource
from intellisense.core.types import BrokerTimelineEvent, TestSessionConfig
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase

if TYPE_CHECKING:
    from queue import SimpleQueue as GenericSimpleQueue

logger = logging.getLogger(__name__)


class RedisBrokerDataSource(IBrokerDataSource):
    """
    Data source that consumes broker/order lifecycle events
    (requests, validated, fills, statuses, rejections)
    from TESTRADE Redis streams and yields BrokerTimelineEvents.
    """
    def __init__(self, session_config: 'TestSessionConfig', pds_gsi_provider: Callable[[], int]):
        self.session_config = session_config
        self._pds_gsi_provider = pds_gsi_provider
        self._is_active = False

        self._event_queue: 'GenericSimpleQueue[Optional[BrokerTimelineEvent]]' = SimpleQueue()
        self._consumers: List[RedisStreamConsumerBase] = []

        self._initialize_consumers()
        logger.info("RedisBrokerDataSource initialized.")

    def _initialize_consumers(self):
        """Initializes RedisStreamConsumerBase instances for relevant order lifecycle streams."""
        consumer_prefix = self.session_config.redis_consumer_name_prefix
        group_name = self.session_config.redis_consumer_group_name

        stream_keys_in_config = {
            "order_requests": self.session_config.redis_stream_order_requests,
            "validated_orders": self.session_config.redis_stream_validated_orders,
            "fills": self.session_config.redis_stream_fills,
            "status_updates": self.session_config.redis_stream_status,
            "rejections": self.session_config.redis_stream_rejections,
        }

        for stream_type, stream_name_val in stream_keys_in_config.items():
            if stream_name_val:  # Check if stream name is configured
                consumer_name = f"{consumer_prefix}_broker_{stream_type}_{uuid.uuid4().hex[:4]}"
                consumer = RedisStreamConsumerBase(
                    redis_host=self.session_config.redis_host,
                    redis_port=self.session_config.redis_port,
                    redis_db=self.session_config.redis_db,
                    redis_password=self.session_config.redis_password,
                    stream_name=stream_name_val,
                    consumer_group_name=group_name,  # Use same group for all related streams or per stream? For now, same.
                    consumer_name=consumer_name,
                    message_handler=lambda mid, mdata, sn=stream_name_val: self._on_redis_message_received(sn, mid, mdata),
                    start_id='$'  # Default to live tailing
                )
                self._consumers.append(consumer)
                logger.info(f"Broker data consumer configured for stream: '{stream_name_val}' ({stream_type}), consumer: '{consumer_name}'")
            else:
                logger.warning(f"Broker data stream '{stream_type}' not configured in session_config. Will not consume.")

    def _on_redis_message_received(self, stream_name: str, message_id: str, message_data: Dict[str, Any]):
        gsi = self._pds_gsi_provider()
        timeline_event = BrokerTimelineEvent.from_redis_message(message_data, gsi)
        if timeline_event:
            self._event_queue.put(timeline_event)
        else:
            logger.warning(f"Failed to parse Redis message ID {message_id} from stream '{stream_name}' into BrokerTimelineEvent.")

    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> None:
        if self._is_active: logger.warning("RedisBrokerDataSource already started."); return
        if not self._consumers: logger.warning("RedisBrokerDataSource has no consumers configured."); return

        logger.info("Starting RedisBrokerDataSource consumption for all configured streams...")
        for consumer in self._consumers:
            consumer.start_consuming()
        self._is_active = True

    def stop(self) -> None:
        if not self._is_active: logger.info("RedisBrokerDataSource already stopped or not started."); return
        logger.info("Stopping RedisBrokerDataSource consumption...")
        for consumer in self._consumers:
            consumer.stop_consuming()
        self._is_active = False
        self._event_queue.put(None)
        logger.info("RedisBrokerDataSource stopped.")

    def load_timeline_data(self) -> bool:
        logger.info("RedisBrokerDataSource: 'load_timeline_data' called. Streams will be consumed based on consumer start_id.")
        return True

    def get_response_stream(self) -> Iterator[BrokerTimelineEvent]:  # Changed from get_broker_stream for IBrokerDataSource
        if not self._is_active and self._event_queue.empty():
            logger.warning("Attempted to get data stream from inactive/empty RedisBrokerDataSource.")
            return iter([])

        while self._is_active or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.1)
                if event is None: break
                if isinstance(event, BrokerTimelineEvent):
                    yield event
            except Empty:
                if not self._is_active and self._event_queue.empty(): break
                continue
            except Exception as e:
                logger.error(f"Error in RedisBrokerDataSource get_response_stream: {e}", exc_info=True)
                break
        logger.info("RedisBrokerDataSource get_response_stream iterator finished.")

    def set_delay_profile(self, profile_name: str) -> bool:
        """Not applicable for Redis live stream consumption. Placeholder for interface."""
        logger.info(f"RedisBrokerDataSource: 'set_delay_profile' called with '{profile_name}'. No-op for Redis streams.")
        return True  # Or False, depending on desired behavior for non-applicable methods

    def get_replay_progress(self) -> Dict[str, Any]:
        return {
            'source_type': 'RedisBrokerLifecycle',
            'total_events': -1, 'events_loaded': -1, 'progress_percentage_loaded': 0.0
        }

    def cleanup(self) -> None:
        self.stop()
        logger.info("RedisBrokerDataSource cleaned up.")
