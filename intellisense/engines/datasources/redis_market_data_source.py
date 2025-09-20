"""
Redis Market Data Source for IntelliSense

Provides a Redis-based market data source that consumes from TESTRADE's market data streams
(trades and quotes) and yields PriceTimelineEvent objects for the IntelliSense system.
"""

import logging
import uuid  # For unique consumer names
from typing import Iterator, Optional, Dict, Any, List, TYPE_CHECKING, Callable
from queue import SimpleQueue, Empty  # For internal event buffering

from intellisense.core.interfaces import IPriceDataSource
from intellisense.core.types import PriceTimelineEvent, TestSessionConfig
from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase

if TYPE_CHECKING:
    from queue import SimpleQueue as GenericSimpleQueue  # For better type hinting if Python version supports

logger = logging.getLogger(__name__)


class RedisMarketDataDataSource(IPriceDataSource):
    """
    Data source that consumes market data (trades and quotes)
    from TESTRADE Redis streams and yields PriceTimelineEvents.
    It can manage two separate consumers for trades and quotes if needed.
    """
    def __init__(self, session_config: 'TestSessionConfig', pds_gsi_provider: Callable[[], int]):
        """
        Initializes the RedisMarketDataDataSource.

        Args:
            session_config: TestSessionConfig with Redis and stream name details.
            pds_gsi_provider: Callable to get the next global sequence ID.
        """
        self.session_config = session_config
        self._pds_gsi_provider = pds_gsi_provider
        self._is_active = False

        self._event_queue: 'GenericSimpleQueue[Optional[PriceTimelineEvent]]' = SimpleQueue()
        self._consumers: List[RedisStreamConsumerBase] = []

        self._initialize_consumers()
        logger.info("RedisMarketDataDataSource initialized.")

    def _initialize_consumers(self):
        """Initializes RedisStreamConsumerBase instances for trades and quotes streams."""
        consumer_prefix = self.session_config.redis_consumer_name_prefix
        group_name = self.session_config.redis_consumer_group_name

        # Consumer for Trade Data
        trade_stream_name = self.session_config.redis_stream_market_trades
        if trade_stream_name:
            trade_consumer_name = f"{consumer_prefix}_mkttrades_{uuid.uuid4().hex[:4]}"
            trade_consumer = RedisStreamConsumerBase(
                redis_host=self.session_config.redis_host,
                redis_port=self.session_config.redis_port,
                redis_db=self.session_config.redis_db,
                redis_password=self.session_config.redis_password,
                stream_name=trade_stream_name,
                consumer_group_name=group_name,
                consumer_name=trade_consumer_name,
                message_handler=lambda mid, mdata: self._on_redis_message_received(trade_stream_name, mid, mdata),
                start_id='$'  # Default to live tailing for market data
            )
            self._consumers.append(trade_consumer)
            logger.info(f"Trade data consumer configured for stream: '{trade_stream_name}', consumer: '{trade_consumer_name}'")
        else:
            logger.warning("Market data trade stream name not configured. No trade data will be consumed.")

        # Consumer for Quote Data
        quote_stream_name = self.session_config.redis_stream_market_quotes
        if quote_stream_name:
            quote_consumer_name = f"{consumer_prefix}_mktquotes_{uuid.uuid4().hex[:4]}"
            quote_consumer = RedisStreamConsumerBase(
                redis_host=self.session_config.redis_host,
                redis_port=self.session_config.redis_port,
                redis_db=self.session_config.redis_db,
                redis_password=self.session_config.redis_password,
                stream_name=quote_stream_name,
                consumer_group_name=group_name,
                consumer_name=quote_consumer_name,
                message_handler=lambda mid, mdata: self._on_redis_message_received(quote_stream_name, mid, mdata),
                start_id='$'  # Default to live tailing
            )
            self._consumers.append(quote_consumer)
            logger.info(f"Quote data consumer configured for stream: '{quote_stream_name}', consumer: '{quote_consumer_name}'")
        else:
            logger.warning("Market data quote stream name not configured. No quote data will be consumed.")

    def _on_redis_message_received(self, stream_name: str, message_id: str, message_data: Dict[str, Any]):
        """Callback for RedisStreamConsumerBase when a new message arrives from any market data stream."""
        # logger.debug(f"RedisMarketDataDataSource received from '{stream_name}', ID {message_id}")
        gsi = self._pds_gsi_provider()
        timeline_event = PriceTimelineEvent.from_redis_message(message_data, gsi)
        if timeline_event:
            self._event_queue.put(timeline_event)
        else:
            logger.warning(f"Failed to parse Redis message ID {message_id} from stream '{stream_name}' into PriceTimelineEvent.")

    def is_active(self) -> bool:
        return self._is_active

    def start(self) -> None:
        if self._is_active:
            logger.warning("RedisMarketDataDataSource already started.")
            return
        if not self._consumers:
            logger.warning("RedisMarketDataDataSource has no consumers configured. Cannot start.")
            return

        logger.info("Starting RedisMarketDataDataSource consumption for all configured streams...")
        for consumer in self._consumers:
            consumer.start_consuming()
        self._is_active = True

    def stop(self) -> None:
        if not self._is_active:
            logger.info("RedisMarketDataDataSource already stopped or not started.")
            return
        logger.info("Stopping RedisMarketDataDataSource consumption...")
        for consumer in self._consumers:
            consumer.stop_consuming()
        self._is_active = False
        self._event_queue.put(None)  # Sentinel to unblock iterator
        logger.info("RedisMarketDataDataSource stopped.")

    def load_timeline_data(self) -> bool:
        logger.info("RedisMarketDataDataSource: 'load_timeline_data' called. Market data streams will be consumed based on consumer start_id (typically live tailing).")
        return True  # Ready to stream

    def get_price_stream(self) -> Iterator[PriceTimelineEvent]:
        if not self._is_active and self._event_queue.empty():  # Check queue too if stopping
            logger.warning("Attempted to get data stream from inactive/empty RedisMarketDataDataSource.")
            return iter([])

        while self._is_active or not self._event_queue.empty():
            try:
                event = self._event_queue.get(timeout=0.1)
                if event is None: break
                if isinstance(event, PriceTimelineEvent):
                    yield event
            except Empty:
                if not self._is_active and self._event_queue.empty(): break
                continue
            except Exception as e:
                logger.error(f"Error in RedisMarketDataDataSource get_price_stream: {e}", exc_info=True)
                break
        logger.info("RedisMarketDataDataSource get_price_stream iterator finished.")

    def get_replay_progress(self) -> Dict[str, Any]:
        # For live streams, progress is continuous and less defined than file replay
        processed_count = self._event_queue.qsize()  # Approximate items waiting in IntelliSense queue
        return {
            'source_type': 'RedisMarketData',
            'total_events': -1,
            'events_loaded': -1,  # Or messages_processed_by_consumers (needs stats from RedisStreamConsumerBase)
            'progress_percentage_loaded': 0.0
        }

    def cleanup(self) -> None:
        self.stop()
        logger.info("RedisMarketDataDataSource cleaned up.")
