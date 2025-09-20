# /modules/market_data_publishing/filtered_market_data_publisher.py
import logging
import uuid
from typing import Any, Dict, Optional, List

from interfaces.data.services import IMarketDataPublisher, IMarketDataReceiver
from interfaces.logging.services import ICorrelationLogger

logger = logging.getLogger(__name__)

class FilteredMarketDataPublisher(IMarketDataPublisher, IMarketDataReceiver):
    """
    Publishes pre-filtered market data (quotes and trades) to external consumers
    using the TelemetryService for robust, buffered delivery.
    This service assumes data passed to it is already vetted for relevance.
    """
    def __init__(self,
                 config_service: Any, # For target Redis stream names
                 correlation_logger: ICorrelationLogger
                ):
        self.logger = logger
        self.config_service = config_service
        self._correlation_logger = correlation_logger
        self._is_properly_initialized = False

        if not self._correlation_logger:
            self.logger.critical("FilteredMarketDataPublisher: CorrelationLogger not provided. "
                                 "Service cannot publish.")
            return

        # Stream names for routing to correct ZMQ socket/mmap
        self._stream_market_trades_filtered = getattr(self.config_service, 'redis_stream_filtered_trades', 'testrade:filtered:market-trades')
        self._stream_market_quotes_filtered = getattr(self.config_service, 'redis_stream_filtered_quotes', 'testrade:filtered:market-quotes')

        self._is_properly_initialized = True
        self.logger.info("FilteredMarketDataPublisher initialized with CorrelationLogger.")

    def publish_market_data(self,
                            symbol: str,
                            data_type: str, # Expected "QUOTE" or "TRADE"
                            payload_dict: Dict[str, Any], # This is the INNER payload (e.g., quote/trade fields)
                            source_event_metadata: Dict[str, Any]):
        """
        Publishes the given market data using BulletproofBabysitterIPCClient.
        The payload_dict here is the core data, which will be wrapped by
        _create_redis_message_json into the standard metadata/payload structure.
        """
        if not self._is_properly_initialized:
            self.logger.error(f"FMP: Service not initialized. Dropping data for {symbol} ({data_type}).")
            return

        symbol_upper = symbol.upper()
        self.logger.debug(f"FMP_PUBLISH_ENTRY: Publishing pre-filtered data for {symbol_upper} - Type: {data_type}.")

        # Add indicative metadata to the inner payload (optional)
        # It's better to do this on a copy if the original payload_dict might be reused/cached.
        final_inner_payload = payload_dict.copy()
        final_inner_payload['is_globally_relevant_for_ipc'] = True # Indicates it passed ActiveSymbolsService check
        final_inner_payload['filter_source_trigger'] = "ActiveSymbolsService_via_PriceRepository"

        external_event_type_for_wrapper = ""
        target_redis_stream_for_routing = "" # This name is used by Bulletproof client for socket/mmap routing

        if data_type == "QUOTE":
            # This event_type is for the OUTER wrapper that BulletproofIPC expects via _create_redis_message_json
            external_event_type_for_wrapper = getattr(self.config_service, 'EVENT_TYPE_FILTERED_QUOTE', "TESTRADE_MARKET_QUOTE_FILTERED")
            target_redis_stream_for_routing = self._stream_market_quotes_filtered
        elif data_type == "TRADE":
            external_event_type_for_wrapper = getattr(self.config_service, 'EVENT_TYPE_FILTERED_TRADE', "TESTRADE_MARKET_TRADE_FILTERED")
            target_redis_stream_for_routing = self._stream_market_trades_filtered
        else:
            self.logger.warning(f"FMP: Unknown data_type '{data_type}' for symbol {symbol_upper}. Cannot publish.")
            return

        try:
            correlation_id = source_event_metadata.get("correlationId", str(uuid.uuid4()))
            causation_id = source_event_metadata.get("eventId")
            
            # Add correlation metadata to payload
            final_inner_payload['correlation_id'] = correlation_id
            if causation_id:
                final_inner_payload['causation_id'] = causation_id

            # Old telemetry service code - replaced with correlation logger below
            """
            self._telemetry_service.enqueue(
                source_component="FilteredMarketDataPublisher",
                event_type=external_event_type_for_wrapper,
                payload=final_inner_payload,
                stream_override=target_redis_stream_for_routing,
                origin_timestamp_s=source_event_metadata.get("origin_timestamp_s")
            )
            """
            # Use correlation logger for market data events
            if self._correlation_logger:
                # Include origin timestamp if available
                origin_timestamp = source_event_metadata.get("origin_timestamp_s")
                payload_with_timestamp = {
                    "event_type": external_event_type_for_wrapper,
                    "symbol": symbol_upper,
                    "data_type": data_type,
                    **final_inner_payload
                }
                if origin_timestamp is not None:
                    payload_with_timestamp["origin_timestamp_s"] = origin_timestamp
                    
                self._correlation_logger.log_event(
                    source_component="FilteredMarketDataPublisher",
                    event_name="MarketDataPublished",
                    payload=payload_with_timestamp
                )
            
            self.logger.debug(f"FMP_SENT: Filtered data for {symbol_upper} queued via CorrelationLogger")

        except Exception as e:
            self.logger.error(f"FMP_SEND_FAIL: Failed to send filtered market data for {symbol_upper} ({data_type}): {e}", exc_info=True)

    def start(self):
        if not self._is_properly_initialized:
            self.logger.error("FilteredMarketDataPublisher cannot start, not properly initialized.")
            return
        self.logger.info("FilteredMarketDataPublisher started.")

    def stop(self):
        self.logger.info("FilteredMarketDataPublisher stopping.")
        # No specific resources to release here other than what Python GC handles.
        # The BulletproofIPCClient is managed by ApplicationCore.
    
    def publish_price_update(self, symbol: str, price_data: Dict[str, Any]) -> None:
        """
        Publishes a price update via the correlation logger.
        This method implements the abstract method from IMarketDataPublisher.
        """
        # Determine if this is a quote or trade based on the data
        if 'ask' in price_data or 'bid' in price_data:
            data_type = "QUOTE"
        else:
            data_type = "TRADE"
        
        # Use existing publish_market_data method
        source_metadata = {
            'correlationId': price_data.get('correlation_id', str(uuid.uuid4())),
            'eventId': price_data.get('event_id', str(uuid.uuid4()))
        }
        self.publish_market_data(symbol, data_type, price_data, source_metadata)

    def on_trade(self, symbol: str, price: float, size: int, timestamp: float, conditions: Optional[List[str]] = None, **kwargs):
        """
        Implements IMarketDataReceiver interface for receiving trade data from MarketDataFilterService.
        Converts trade data to the format expected by publish_market_data.
        """
        
        # Build trade payload dict
        trade_payload = {
            'symbol': symbol.upper(),
            'price': price,
            'size': size,
            'timestamp': timestamp
        }
        
        # Add conditions if provided
        if conditions:
            trade_payload['conditions'] = conditions
            
        # Add any additional kwargs
        trade_payload.update(kwargs)
        
        # Build source metadata
        source_metadata = {
            'correlationId': kwargs.get('correlation_id', str(uuid.uuid4())),
            'eventId': kwargs.get('event_id', str(uuid.uuid4())),
            'origin_timestamp_s': timestamp
        }
        
        # Use existing publish_market_data method
        self.publish_market_data(symbol, "TRADE", trade_payload, source_metadata)

    def on_quote(self, symbol: str, bid: float, ask: float, bid_size: int, ask_size: int, timestamp: float, conditions: Optional[List[str]] = None, **kwargs):
        """
        Implements IMarketDataReceiver interface for receiving quote data from MarketDataFilterService.
        Converts quote data to the format expected by publish_market_data.
        """
        
        # Build quote payload dict
        quote_payload = {
            'symbol': symbol.upper(),
            'bid': bid,
            'ask': ask,
            'bid_size': bid_size,
            'ask_size': ask_size,
            'timestamp': timestamp
        }
        
        # Add conditions if provided
        if conditions:
            quote_payload['conditions'] = conditions
            
        # Add any additional kwargs
        quote_payload.update(kwargs)
        
        # Build source metadata
        source_metadata = {
            'correlationId': kwargs.get('correlation_id', str(uuid.uuid4())),
            'eventId': kwargs.get('event_id', str(uuid.uuid4())),
            'origin_timestamp_s': timestamp
        }
        
        # Use existing publish_market_data method
        self.publish_market_data(symbol, "QUOTE", quote_payload, source_metadata)
