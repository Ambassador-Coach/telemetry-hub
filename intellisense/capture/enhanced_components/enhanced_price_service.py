"""
Enhanced Price Services

This module provides enhanced versions of price-related services that integrate
with the IntelliSense capture interfaces for correlation data capture.
"""

import logging
import time
import threading
from typing import List, Dict, Any, Optional

# Placeholder classes for price data types (would normally import from modules.price)
class TradeData:
    """Placeholder for trade data."""
    def __init__(self, symbol: str, price: float, volume: int, timestamp: float):
        self.symbol = symbol
        self.price = price
        self.volume = volume
        self.timestamp = timestamp


class QuoteData:
    """Placeholder for quote data."""
    def __init__(self, symbol: str, bid: float, ask: float, bid_size: int, ask_size: int, timestamp: float):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.bid_size = bid_size
        self.ask_size = ask_size
        self.timestamp = timestamp


class PriceRepository:
    """Placeholder base class for price repository."""
    def __init__(self, *args, **kwargs):
        logger.info("Base PriceRepository initialized (placeholder).")
    
    def process_trade(self, trade_data: TradeData) -> bool:
        """Process trade data. Returns True if successful."""
        logger.debug(f"Base PriceRepository processing trade: {trade_data.symbol} @ {trade_data.price}")
        # Simulate some processing
        return trade_data.price > 0  # Simple validation
    
    def process_quote(self, quote_data: QuoteData) -> bool:
        """Process quote data. Returns True if successful."""
        logger.debug(f"Base PriceRepository processing quote: {quote_data.symbol} {quote_data.bid}/{quote_data.ask}")
        # Simulate some processing
        return quote_data.bid > 0 and quote_data.ask > quote_data.bid


# Import capture interfaces
from intellisense.capture.interfaces import IPriceDataListener
from .metrics_collector import ComponentCaptureMetrics

logger = logging.getLogger(__name__)


class EnhancedPriceRepository(PriceRepository):
    """Price repository enhanced with data capture observation."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._price_data_listeners: List[IPriceDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()
        logger.info("EnhancedPriceRepository initialized.")
    
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
                logger.warning(f"Attempted to unregister price listener not found: {type(listener).__name__}")

    def process_trade(self, trade_data: TradeData) -> bool:
        """Enhanced trade processing with capture observation."""
        if not self._price_data_listeners:  # Optimization: no listeners, just call super
            return super().process_trade(trade_data)

        processing_start_ns = time.perf_counter_ns()
        processing_result = False
        processing_error: Optional[Exception] = None
        
        try:
            processing_result = super().process_trade(trade_data)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns, 
                success=(processing_error is None and processing_result)
            )

            trade_payload = {
                'price_event_type': 'TRADE',
                'symbol': trade_data.symbol,
                'price': trade_data.price,
                'volume': trade_data.volume,
                'trade_timestamp': trade_data.timestamp,
                'processing_timestamp': time.time(),
                'processing_latency_ns': processing_latency_ns,
                'processing_success': processing_result,
                'processing_error': str(processing_error) if processing_error else None
            }
            
            self._notify_price_listeners_trade(
                source_component_name='PriceRepository',
                trade_data=trade_payload,
                processing_latency_ns=processing_latency_ns
            )

        if processing_error:
            raise processing_error  # Re-raise after notification
        return processing_result

    def process_quote(self, quote_data: QuoteData) -> bool:
        """Enhanced quote processing with capture observation."""
        if not self._price_data_listeners:  # Optimization: no listeners, just call super
            return super().process_quote(quote_data)

        processing_start_ns = time.perf_counter_ns()
        processing_result = False
        processing_error: Optional[Exception] = None
        
        try:
            processing_result = super().process_quote(quote_data)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns, 
                success=(processing_error is None and processing_result)
            )

            quote_payload = {
                'price_event_type': 'QUOTE',
                'symbol': quote_data.symbol,
                'bid': quote_data.bid,
                'ask': quote_data.ask,
                'bid_size': quote_data.bid_size,
                'ask_size': quote_data.ask_size,
                'quote_timestamp': quote_data.timestamp,
                'processing_timestamp': time.time(),
                'processing_latency_ns': processing_latency_ns,
                'processing_success': processing_result,
                'processing_error': str(processing_error) if processing_error else None
            }
            
            self._notify_price_listeners_quote(
                source_component_name='PriceRepository',
                quote_data=quote_payload,
                processing_latency_ns=processing_latency_ns
            )

        if processing_error:
            raise processing_error  # Re-raise after notification
        return processing_result

    def _notify_price_listeners_trade(self, source_component_name: str, 
                                    trade_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered price data listeners about trade data."""
        if not self._price_data_listeners:
            return

        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._price_data_listeners)  # Create a copy
        
        for listener in listeners_snapshot:
            try:
                listener.on_trade_data(
                    source_component_name=source_component_name,
                    trade_data=trade_data,
                    processing_latency_ns=processing_latency_ns
                )
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying price listener {type(listener).__name__} for trade: {e}", exc_info=True)

    def _notify_price_listeners_quote(self, source_component_name: str, 
                                     quote_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered price data listeners about quote data."""
        if not self._price_data_listeners:
            return

        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._price_data_listeners)  # Create a copy
        
        for listener in listeners_snapshot:
            try:
                listener.on_quote_data(
                    source_component_name=source_component_name,
                    quote_data=quote_data,
                    processing_latency_ns=processing_latency_ns
                )
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying price listener {type(listener).__name__} for quote: {e}", exc_info=True)

    def get_capture_metrics(self) -> Dict[str, Any]:
        """Get comprehensive price processing and capture metrics."""
        metrics = self._capture_metrics.get_metrics()
        metrics['listener_count'] = len(self._price_data_listeners)
        metrics['component_type'] = 'PriceRepository'
        return metrics
