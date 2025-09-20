"""
Position Enrichment Service for TESTRADE

This service consumes position updates from the Position Manager and enriches them
with market data (bid, ask, last, volume) before republishing to the GUI backend.
Uses the DI container for proper dependency injection.
"""

import json
import logging
import threading
import time
import uuid
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

from interfaces.data.services import IPriceProvider
from interfaces.core.services import IEventBus
from interfaces.core.services import IConfigService
from interfaces.logging.services import ICorrelationLogger
from core.events import PositionUpdateEvent, PositionUpdateEventData

logger = logging.getLogger(__name__)

@dataclass
class EnrichedPositionData:
    """Enhanced position data with market information"""
    # Core position data
    symbol: str
    is_open: bool
    quantity: float
    average_price: float
    realized_pnl: float
    last_update_timestamp: float
    last_update_source: str
    strategy: str
    total_fills_count: int
    position_opened_timestamp: Optional[float]
    
    # Market data enrichment
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    last_price: Optional[float] = None
    total_volume: Optional[int] = None
    market_data_timestamp: Optional[float] = None
    
    # Data availability flags
    market_data_available: bool = False
    hierarchy_data_available: bool = True
    
    # Parent/child hierarchy (if applicable)
    is_parent: Optional[bool] = None
    is_child: Optional[bool] = None
    parent_trade_id: Optional[str] = None
    trade_id: Optional[str] = None


class PositionEnrichmentService:
    """
    Service that enriches position updates with market data using DI container.
    
    Consumes from: testrade:position-updates
    Publishes to: testrade:enriched-position-updates (consumed by GUI backend)
    """
    
    def __init__(self, config_service: IConfigService, price_provider: IPriceProvider, event_bus: IEventBus, correlation_logger: Optional[ICorrelationLogger] = None):
        self.logger = logger
        self.config_service = config_service
        self.price_provider = price_provider
        self.event_bus = event_bus
        self._correlation_logger = correlation_logger

        # Check for correlation logger for publishing
        if not self._correlation_logger:
            self.logger.critical("PositionEnrichmentService: CorrelationLogger not provided. Publishing enriched data will fail.")
            self._is_properly_initialized_for_publishing = False
        else:
            self._is_properly_initialized_for_publishing = True
            self.logger.info("PositionEnrichmentService: CorrelationLogger available for publishing")

        # Configuration for the stream PES publishes to
        self._output_stream = getattr(config_service, 'redis_stream_enriched_position_updates', 'testrade:enriched-position-updates')

        # Service lifecycle flag
        self._is_running = False

        # Subscribe to PositionUpdateEvent from the internal EventBus
        if self.event_bus:
            # CRITICAL FIX: Store handler reference to prevent WeakSet garbage collection
            self._position_update_handler = self._handle_position_update_from_event_bus
            self.event_bus.subscribe(PositionUpdateEvent, self._position_update_handler)
            self.logger.info("PositionEnrichmentService: Subscribed to internal PositionUpdateEvent.")
            self.logger.critical(f"PES_HANDLER_FIX: Stored handler reference ID={id(self._position_update_handler)} to prevent WeakSet GC")
        else:
            self.logger.error("PositionEnrichmentService: EventBus not provided. Cannot subscribe to position updates.")

        # Market data cache for performance
        self._market_data_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.RLock()
        self._cache_ttl_seconds = 30  # Cache market data for 30 seconds

        self.logger.info(f"PositionEnrichmentService initialized (EventBus-driven). Output stream: {self._output_stream}")
    
    def start(self):
        """Start the Position Enrichment Service."""
        if not (self.event_bus and self.price_provider):
            self.logger.error("PositionEnrichmentService cannot start properly: missing critical dependencies (event_bus or price_provider).")
            self._is_running = False
            return

        self._is_running = True
        self.logger.info("PositionEnrichmentService started and is listening for internal PositionUpdateEvents.")
    
    def stop(self):
        """Stop the Position Enrichment Service."""
        self._is_running = False
        # Unsubscribe from EventBus
        if self.event_bus and hasattr(self, '_position_update_handler'):
            try:
                self.event_bus.unsubscribe(PositionUpdateEvent, self._position_update_handler)
                self.logger.info("PositionEnrichmentService: Unsubscribed from internal PositionUpdateEvent.")
                self.logger.critical(f"PES_HANDLER_FIX: Unsubscribed handler reference ID={id(self._position_update_handler)}")
            except Exception as e:
                self.logger.error(f"PositionEnrichmentService: Error unsubscribing from EventBus: {e}")

        self.logger.info("PositionEnrichmentService stopped.")
    

    


    def _handle_position_update_from_event_bus(self, event: PositionUpdateEvent):
        """
        Handles PositionUpdateEvent received from the internal EventBus.
        Enriches with market data and publishes to an external Redis stream.
        """
        # CRITICAL DEBUG: Log that handler is being called
        self.logger.critical(f"PES Event Handler: CALLED! Handler ID={id(self._handle_position_update_from_event_bus)}, Event ID={getattr(event, 'event_id', 'N/A')}")

        if not self._is_running:
            self.logger.debug("PES: Service not running, ignoring PositionUpdateEvent.")
            return

        try:
            position_event_data: PositionUpdateEventData = event.data

            symbol = position_event_data.symbol.upper()
            if not symbol:
                self.logger.warning(f"PES Event Handler: PositionUpdateEvent missing symbol: {position_event_data}")
                return

            self.logger.info(f"PES Event Handler: Processing PositionUpdateEvent for {symbol}. IsOpen: {position_event_data.is_open}, Qty: {position_event_data.quantity}")

            # Get market data for this symbol
            market_data = self._get_market_data(symbol)

            # Create enriched position data using fields from PositionUpdateEventData
            enriched_data = EnrichedPositionData(
                symbol=symbol,
                is_open=position_event_data.is_open,
                quantity=position_event_data.quantity,
                average_price=position_event_data.average_price,
                realized_pnl=position_event_data.realized_pnl_session if position_event_data.realized_pnl_session is not None else 0.0,
                last_update_timestamp=position_event_data.last_update_timestamp,
                last_update_source=position_event_data.update_source_trigger or "EVENT_BUS",
                strategy=position_event_data.strategy or "unknown",
                total_fills_count=position_event_data.total_fills_count,
                position_opened_timestamp=position_event_data.position_opened_timestamp,

                bid_price=market_data.get("bid"),
                ask_price=market_data.get("ask"),
                last_price=market_data.get("last"),
                total_volume=market_data.get("volume"),
                market_data_timestamp=market_data.get("timestamp"),

                market_data_available=bool(market_data.get("bid") or market_data.get("ask") or market_data.get("last")),
                hierarchy_data_available=True,
                trade_id=position_event_data.position_uuid or symbol
            )

            # Capture correlation context
            master_correlation_id = getattr(event, 'correlation_id', None)
            # The ID of the current event is the cause of the next event
            causation_id_for_next_step = getattr(event, 'event_id', None)
            
            if not master_correlation_id:
                self.logger.error(f"CRITICAL: Missing correlation_id on event {type(event).__name__}. This breaks end-to-end tracing!")
                master_correlation_id = "MISSING_CORRELATION_ID"

            self._publish_enriched_position_update(enriched_data,
                                                   master_correlation_id,
                                                   causation_id_for_next_step)

            self.logger.debug(f"PES Event Handler: Enriched and published for {symbol}. Bid={market_data.get('bid')}, Ask={market_data.get('ask')}")

        except AttributeError as ae:
             self.logger.error(f"PES Event Handler: AttributeError processing PositionUpdateEvent. Data might be unexpected type. Event data: {event.data}. Error: {ae}", exc_info=True)
        except Exception as e:
            self.logger.error(f"PES Event Handler: Error processing PositionUpdateEvent: {e}", exc_info=True)


    
    def _get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Get market data for a symbol with caching"""
        with self._cache_lock:
            # Check cache first
            cached_data = self._market_data_cache.get(symbol)
            current_time = time.time()
            
            if cached_data and (current_time - cached_data.get("cache_time", 0)) < self._cache_ttl_seconds:
                return cached_data
            
            # Fetch fresh market data
            market_data = {}
            try:
                if self.price_provider:
                    # Get bid/ask prices
                    bid = self.price_provider.get_bid_price(symbol, allow_api_fallback=False)
                    ask = self.price_provider.get_ask_price(symbol, allow_api_fallback=False)
                    last = self.price_provider.get_latest_price(symbol, allow_api_fallback=False)
                    
                    if bid and bid > 0:
                        market_data["bid"] = bid
                    if ask and ask > 0:
                        market_data["ask"] = ask
                    if last and last > 0:
                        market_data["last"] = last
                    
                    # Volume data (if available)
                    if hasattr(self.price_provider, 'get_volume'):
                        volume = self.price_provider.get_volume(symbol)
                        if volume and volume > 0:
                            market_data["volume"] = volume
                    
                    market_data["timestamp"] = current_time
                    market_data["cache_time"] = current_time
                    
                    # Cache the data
                    self._market_data_cache[symbol] = market_data
                    
            except Exception as e:
                self.logger.debug(f"Could not fetch market data for {symbol}: {e}")
            
            return market_data
    
    def _publish_enriched_position_update(self,
                                          enriched_data: EnrichedPositionData,
                                          master_correlation_id: str,
                                          causation_id: Optional[str]):
        """Publish the enriched position update to the output stream via BabysitterIPCClient."""
        if not self._is_properly_initialized_for_publishing:
            self.logger.error(f"PES: Cannot publish enriched data for {enriched_data.symbol}, publishing components not initialized.")
            return
        try:
            payload_dict = asdict(enriched_data)

            # TANK mode is now handled automatically by TelemetryService

            # Use correlation logger for clean, fire-and-forget publishing
            telemetry_data = {
                "payload": payload_dict,
                "correlation_id": master_correlation_id,
                "causation_id": causation_id
            }
            
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionEnrichmentService",
                    event_name="EnrichedPositionUpdate",
                    payload=telemetry_data,
                    stream_override=self._output_stream
                )
            self.logger.debug(f"PES: Enriched position update for {enriched_data.symbol} sent to stream '{self._output_stream}' (CorrID: {correlation_id})")

        except Exception as e:
            self.logger.error(f"PES: Error publishing enriched position update for {enriched_data.symbol}: {e}", exc_info=True)
