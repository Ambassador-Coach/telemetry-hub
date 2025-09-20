"""
Enhanced Price Repository

This module provides an enhanced version of the PriceRepository that integrates
with the IntelliSense capture interfaces for correlation data capture.
This version is designed to work with event-based market data processing.
"""

import logging
import time
import threading
from typing import List, Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from utils.global_config import GlobalConfig
    from core.interfaces import IEventBus
    # Import actual base class if it exists
    # from modules.price_fetching.price_repository import PriceRepository as ActualBasePriceRepository
    # Actual market data types from your system
    from modules.price_fetching.data_types import MarketDataTickEventData, MarketDataQuoteEventData
else:
    # Runtime placeholders for missing types
    class IntelliSenseApplicationCore: pass
    class GlobalConfig: pass
    class IEventBus: pass

# Placeholder classes for market data event types (would normally import from modules.price_fetching)
class MarketDataTickEventData:
    """Placeholder for market data tick event data."""
    def __init__(self, symbol, price, volume, timestamp, **kwargs):
        self.symbol = symbol
        self.price = price
        self.volume = volume
        self.timestamp = timestamp
        self.extra = kwargs
    
    def to_dict(self):
        return {
            'symbol': self.symbol,
            'price': self.price,
            'volume': self.volume,
            'timestamp': self.timestamp,
            **self.extra
        }


class MarketDataQuoteEventData:
    """Placeholder for market data quote event data."""
    def __init__(self, symbol, bid_price, ask_price, bid_size, ask_size, timestamp, **kwargs):
        self.symbol = symbol
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.bid_size = bid_size
        self.ask_size = ask_size
        self.timestamp = timestamp
        self.extra = kwargs
    
    def to_dict(self):
        return {
            'symbol': self.symbol,
            'bid_price': self.bid_price,
            'ask_price': self.ask_price,
            'bid_size': self.bid_size,
            'ask_size': self.ask_size,
            'timestamp': self.timestamp,
            **self.extra
        }


class PriceRepository:
    """Placeholder base class for price repository."""
    def __init__(self, *args, **kwargs):
        logger.info("Base PriceRepository initialized (placeholder).")
    
    def on_trade(self, trade_data: MarketDataTickEventData) -> None:
        symbol = getattr(trade_data, 'symbol', trade_data.get('symbol', 'N/A') if isinstance(trade_data, dict) else 'N/A')
        logger.debug(f"Base PriceRepo on_trade: {symbol} (placeholder)")

    def on_quote(self, quote_data: MarketDataQuoteEventData) -> None:
        symbol = getattr(quote_data, 'symbol', quote_data.get('symbol', 'N/A') if isinstance(quote_data, dict) else 'N/A')
        logger.debug(f"Base PriceRepo on_quote: {symbol} (placeholder)")


# Import capture interfaces
from intellisense.capture.interfaces import IPriceDataListener
from .metrics_collector import ComponentCaptureMetrics

logger = logging.getLogger(__name__)


class EnhancedPriceRepository(PriceRepository):
    """Price repository enhanced with data capture observation."""

    def __init__(self,
                 app_core_ref_for_intellisense: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig',
                 original_event_bus: 'IEventBus',
                 *args, **kwargs):
        """Initialize with ComponentFactory dependencies."""
        # Call super with arguments the base class expects
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         *args, **kwargs)

        self.logger = logging.getLogger(__name__ + ".EnhancedPriceRepository") # Instance logger
        self._app_core_ref = app_core_ref_for_intellisense
        self._price_data_listeners: List[IPriceDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()

        # Ensure caches are initialized for is_price_feed_active
        self._price_cache: Dict[str, Dict[str, Any]] = {} # Stores last trade: {'price': float, 'timestamp': float, 'volume': float}
        self._bid_cache: Dict[str, Dict[str, Any]] = {}   # Stores best bid: {'bid_price': float, 'bid_size': int, 'timestamp': float}
        self._ask_cache: Dict[str, Dict[str, Any]] = {}   # Stores best ask: {'ask_price': float, 'ask_size': int, 'timestamp': float}
        self._cache_lock = threading.RLock()
        self.logger.info(f"EnhancedPriceRepository constructed. AppCoreRef: {type(self._app_core_ref).__name__}")
    
    def add_price_data_listener(self, listener: IPriceDataListener) -> None:
        """Add a price data listener for capture observation."""
        with self._listeners_lock:
            if listener not in self._price_data_listeners:
                self._price_data_listeners.append(listener)
                logger.debug(f"Registered price data listener: {type(listener).__name__}")
    
    def remove_price_data_listener(self, listener: IPriceDataListener) -> None:
        """Remove a price data listener."""
        with self._listeners_lock:
            try:
                self._price_data_listeners.remove(listener)
                logger.debug(f"Unregistered price data listener: {type(listener).__name__}")
            except ValueError:
                logger.warning(f"Attempted to unregister Price listener not found: {type(listener).__name__}")

    def is_price_feed_active(self, symbol: str, staleness_threshold_s: float = 5.0) -> bool:
        """
        Checks if the price feed for the given symbol is considered active
        by looking at the recency of updates in its internal caches
        (_price_cache for trades, _bid_cache, _ask_cache for quotes).

        Args:
            symbol: Trading symbol to check.
            staleness_threshold_s: Maximum age in seconds for data to be considered fresh.

        Returns:
            True if recent trade or quote data exists for the symbol, False otherwise.
        """
        symbol_upper = symbol.upper()
        now = time.time()
        latest_update_timestamp = 0.0

        with self._cache_lock:
            trade_entry = self._price_cache.get(symbol_upper)
            bid_entry = self._bid_cache.get(symbol_upper)
            ask_entry = self._ask_cache.get(symbol_upper)

            if trade_entry and isinstance(trade_entry, dict):
                latest_update_timestamp = max(latest_update_timestamp, trade_entry.get('timestamp', 0.0))

            if bid_entry and isinstance(bid_entry, dict):
                latest_update_timestamp = max(latest_update_timestamp, bid_entry.get('timestamp', 0.0))

            if ask_entry and isinstance(ask_entry, dict):
                latest_update_timestamp = max(latest_update_timestamp, ask_entry.get('timestamp', 0.0))

        if latest_update_timestamp == 0.0:
            self.logger.debug(f"Price feed for {symbol_upper} considered INACTIVE (no cached data).")
            return False

        age_s = now - latest_update_timestamp
        if age_s <= staleness_threshold_s:
            self.logger.debug(f"Price feed for {symbol_upper} considered ACTIVE (last update {age_s:.2f}s ago, threshold {staleness_threshold_s}s).")
            return True
        else:
            self.logger.debug(f"Price feed for {symbol_upper} considered STALE (last update {age_s:.2f}s ago, threshold {staleness_threshold_s}s).")
            return False

    def get_bid_price(self, symbol: str, allow_api_fallback: bool = False) -> Optional[float]:
        """Get the current bid price for a symbol."""
        symbol_upper = symbol.upper()
        with self._cache_lock:
            bid_entry = self._bid_cache.get(symbol_upper)
            if bid_entry and bid_entry.get('bid_price') is not None:
                return float(bid_entry['bid_price'])

        # Fallback to super class if available
        if hasattr(super(), 'get_bid_price'):
            try:
                return super().get_bid_price(symbol, allow_api_fallback)
            except Exception as e:
                logger.error(f"Error getting bid price from super class for {symbol}: {e}")

        return None

    def get_latest_price(self, symbol: str, allow_api_fallback: bool = False) -> Optional[float]:
        """Get the latest price for a symbol."""
        symbol_upper = symbol.upper()
        with self._cache_lock:
            price_entry = self._price_cache.get(symbol_upper)
            if price_entry and price_entry.get('price') is not None:
                return float(price_entry['price'])

        # Fallback to super class if available
        if hasattr(super(), 'get_latest_price'):
            try:
                return super().get_latest_price(symbol, allow_api_fallback)
            except Exception as e:
                logger.error(f"Error getting latest price from super class for {symbol}: {e}")

        return None

    def get_ask_price(self, symbol: str, allow_api_fallback: bool = False) -> Optional[float]:
        """Get the current ask price for a symbol."""
        symbol_upper = symbol.upper()
        with self._cache_lock:
            ask_entry = self._ask_cache.get(symbol_upper)
            if ask_entry and ask_entry.get('ask_price') is not None:
                return float(ask_entry['ask_price'])

        # Fallback to super class if available
        if hasattr(super(), 'get_ask_price'):
            try:
                return super().get_ask_price(symbol, allow_api_fallback)
            except Exception as e:
                logger.error(f"Error getting ask price from super class for {symbol}: {e}")

        return None

    def on_trade(self, trade_data_event_obj: Any) -> None:
        """Enhanced trade processing with capture observation, including correlation ID/tracking_dict propagation."""
        trade_data_dict = self._convert_to_dict(trade_data_event_obj) # Original event data as dict

        self._update_price_cache_from_trade(trade_data_dict) # For is_price_feed_active

        if not self._price_data_listeners:
            # If PriceRepository base class has an on_trade, call it.
            # If trade_data_event_obj is not what super() expects, this might need adjustment.
            if hasattr(super(), 'on_trade'):
                return super().on_trade(trade_data_event_obj)
            return

        processing_start_ns = time.perf_counter_ns()
        processing_error: Optional[Exception] = None
        result = None

        try:
            # Call super() if the base PriceRepository actually processes the trade
            # and this enhanced version is just for observation.
            if hasattr(super(), 'on_trade'):
                result = super().on_trade(trade_data_event_obj)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns,
                success=(processing_error is None)
            )

            payload_for_listener = trade_data_dict.copy() # Start with all original fields
            payload_for_listener.update({
                'price_event_type': 'TRADE',
                'symbol': trade_data_dict.get('symbol', 'N/A'),
                'price': trade_data_dict.get('price'),
                'volume': trade_data_dict.get('volume', trade_data_dict.get('size')),
                'timestamp': trade_data_dict.get('timestamp', time.time()),
                'conditions': trade_data_dict.get('conditions'),
                'processing_timestamp': time.time(), # Timestamp of this enhanced component's processing
                # 'processing_latency_ns' will be passed as a separate arg to the listener
                'processing_success': processing_error is None
            })

            if processing_error:
                payload_for_listener['processing_error'] = str(processing_error)

            # --- NEW: Propagate correlation_id and tracking_dict if present ---
            # These would typically be on simulated/injected price events.
            if 'correlation_id' in trade_data_dict:
                payload_for_listener['correlation_id'] = trade_data_dict['correlation_id']
                logger.debug(f"EnhancedPriceRepo (Trade): Propagating correlation_id: {trade_data_dict['correlation_id']}")
            if 'tracking_dict' in trade_data_dict:
                payload_for_listener['tracking_dict'] = trade_data_dict['tracking_dict'] # Pass as is
                logger.debug(f"EnhancedPriceRepo (Trade): Propagating tracking_dict: {trade_data_dict['tracking_dict'].get('master_tracking_id')}")
            # --- END NEW ---

            self._notify_trade_listeners(payload_for_listener, processing_latency_ns)

        if processing_error:
            raise processing_error
        return result
    
    def on_quote(self, quote_data_event_obj: Any) -> None:
        """Enhanced quote processing with capture observation, including correlation ID/tracking_dict propagation."""
        quote_data_dict = self._convert_to_dict(quote_data_event_obj)

        self._update_quote_cache_from_quote(quote_data_dict) # For is_price_feed_active

        if not self._price_data_listeners:
            if hasattr(super(), 'on_quote'):
                return super().on_quote(quote_data_event_obj)
            return

        processing_start_ns = time.perf_counter_ns()
        processing_error: Optional[Exception] = None
        result = None

        try:
            if hasattr(super(), 'on_quote'):
                result = super().on_quote(quote_data_event_obj)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns,
                success=(processing_error is None)
            )

            payload_for_listener = quote_data_dict.copy()
            payload_for_listener.update({
                'price_event_type': 'QUOTE_BOTH', # Or be more specific if possible
                'symbol': quote_data_dict.get('symbol', 'N/A'),
                'bid': quote_data_dict.get('bid_price', quote_data_dict.get('bid')),
                'ask': quote_data_dict.get('ask_price', quote_data_dict.get('ask')),
                'bid_size': quote_data_dict.get('bid_size'),
                'ask_size': quote_data_dict.get('ask_size'),
                'timestamp': quote_data_dict.get('timestamp', time.time()),
                'processing_timestamp': time.time(),
                'processing_success': processing_error is None
            })

            if processing_error:
                payload_for_listener['processing_error'] = str(processing_error)

            # --- NEW: Propagate correlation_id and tracking_dict if present ---
            if 'correlation_id' in quote_data_dict:
                payload_for_listener['correlation_id'] = quote_data_dict['correlation_id']
                logger.debug(f"EnhancedPriceRepo (Quote): Propagating correlation_id: {quote_data_dict['correlation_id']}")
            if 'tracking_dict' in quote_data_dict:
                payload_for_listener['tracking_dict'] = quote_data_dict['tracking_dict']
                logger.debug(f"EnhancedPriceRepo (Quote): Propagating tracking_dict: {quote_data_dict['tracking_dict'].get('master_tracking_id')}")
            # --- END NEW ---

            self._notify_quote_listeners(payload_for_listener, processing_latency_ns)

        if processing_error:
            raise processing_error
        return result

    def _update_price_cache_from_trade(self, trade_data: Dict[str, Any]): # Conceptual
        """Update price cache from trade data with timestamp for feed monitoring."""
        symbol = trade_data.get('symbol')
        if symbol:
            with self._cache_lock:
                self._price_cache[symbol.upper()] = {
                    'price': trade_data.get('price'),
                    'timestamp': trade_data.get('timestamp', time.time()), # CRITICAL: Store timestamp
                    'volume': trade_data.get('volume')
                }

    def _update_quote_cache_from_quote(self, quote_data: Dict[str, Any]): # Conceptual
        """Update bid/ask cache from quote data with timestamps for feed monitoring."""
        symbol = quote_data.get('symbol')
        if symbol:
            with self._cache_lock:
                if quote_data.get('bid_price') is not None:
                    self._bid_cache[symbol.upper()] = {
                        'bid_price': quote_data.get('bid_price'),
                        'bid_size': quote_data.get('bid_size'),
                        'timestamp': quote_data.get('timestamp', time.time()) # CRITICAL
                    }
                if quote_data.get('ask_price') is not None:
                     self._ask_cache[symbol.upper()] = {
                        'ask_price': quote_data.get('ask_price'),
                        'ask_size': quote_data.get('ask_size'),
                        'timestamp': quote_data.get('timestamp', time.time()) # CRITICAL
                    }

    def _convert_to_dict(self, event_obj: Any) -> Dict[str, Any]:
        """Convert event object to dictionary for payload construction."""
        if hasattr(event_obj, 'to_dict'):
            return event_obj.to_dict()
        elif isinstance(event_obj, dict):
            return event_obj
        else:
            # Fallback: extract attributes from object
            return {
                attr: getattr(event_obj, attr) 
                for attr in dir(event_obj) 
                if not callable(getattr(event_obj, attr)) and not attr.startswith("__")
            }

    def _notify_trade_listeners(self, trade_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered price data listeners about trade data."""
        if not self._price_data_listeners:
            return
            
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._price_data_listeners)
        
        for listener in listeners_snapshot:
            try:
                listener.on_trade_data("EnhancedPriceRepository", trade_payload, processing_latency_ns)
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying Price trade listener {type(listener).__name__}: {e}", exc_info=True)
    
    def _notify_quote_listeners(self, quote_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered price data listeners about quote data."""
        if not self._price_data_listeners:
            return
            
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._price_data_listeners)
        
        for listener in listeners_snapshot:
            try:
                listener.on_quote_data("EnhancedPriceRepository", quote_payload, processing_latency_ns)
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying Price quote listener {type(listener).__name__}: {e}", exc_info=True)

    def get_capture_metrics(self) -> Dict[str, Any]:
        """Get comprehensive price processing and capture metrics."""
        metrics = self._capture_metrics.get_metrics()
        metrics['listener_count'] = len(self._price_data_listeners)
        metrics['component_type'] = 'PriceRepository'
        return metrics


