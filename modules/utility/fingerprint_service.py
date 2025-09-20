"""
Smart Fingerprint Service for Context-Aware Duplicate Detection

This service provides intelligent, state-machine-based duplicate detection. It tracks the
last successful action taken for a symbol and uses that context to prevent redundant
signals, while allowing for new, logical actions like ADD or REDUCE.

FUZZY'S ARCHITECTURAL NOTES:
- Stateful service managed by the DI container.
- Subscribes to OrderFilledEvent and OrderStatusUpdateEvent for smart state transitions.
- The "cache" is now a state machine tracking the last valid action.
- Thread-safe for high-frequency trading environments.
"""

import logging
import threading
import time
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass

from interfaces.utility.services import IFingerprintService
from interfaces.core.services import IEventBus, ILifecycleService
from core.events import OrderFilledEvent, OrderStatusUpdateEvent
from utils.decorators import log_function_call

logger = logging.getLogger(__name__)


@dataclass
class LastActionState:
    """Tracks the last action sent for a symbol."""
    fingerprint: tuple
    timestamp: float
    order_id: Optional[str] = None
    

class FingerprintService(IFingerprintService, ILifecycleService):
    """
    Smart fingerprint service that uses a state-machine approach to prevent
    logically redundant trading signals.
    """
    
    def __init__(self, event_bus: IEventBus, config_service=None):
        """
        Initialize the smart fingerprint service.
        """
        self._event_bus = event_bus
        self._config_service = config_service
        self.logger = logger
        
        # This cache now stores the last successfully *sent* action's fingerprint.
        # Structure: {symbol: LastActionState}
        self._last_sent_action_cache: Dict[str, LastActionState] = {}
        self._cache_lock = threading.RLock()
        
        # Fallback expiry for stale "SENT" orders that never get a fill update.
        self._stale_order_expiry_seconds = float(
            getattr(config_service, 'FINGERPRINT_STALE_ORDER_EXPIRY_SEC', 30.0) if config_service else 30.0
        )
        
        # Performance tracking
        self._stats = {
            'duplicates_suppressed': 0,
            'new_actions_allowed': 0,
            'stale_orders_cleared': 0,
            'fills_cleared': 0,
            'rejected_cleared': 0,
            'cancelled_cleared': 0
        }
        self._stats_lock = threading.Lock()
        
        # Service state
        self._is_running = False
        self._subscriptions_active = False
        
        self.logger.info(f"FingerprintService initialized with stale order expiry: {self._stale_order_expiry_seconds}s")
    
    @log_function_call('fingerprint_service')
    def start(self) -> None:
        """Start the fingerprint service and subscribe to events."""
        if self._is_running:
            self.logger.warning("FingerprintService already running.")
            return
            
        try:
            # Subscribe to order events
            self._event_bus.subscribe(OrderStatusUpdateEvent, self._handle_order_status_update)
            self._event_bus.subscribe(OrderFilledEvent, self._handle_order_filled)
            self._subscriptions_active = True
            self._is_running = True
            self.logger.info("FingerprintService started and subscribed to order events.")
        except Exception as e:
            self.logger.error(f"Failed to start FingerprintService: {e}", exc_info=True)
            raise
    
    @log_function_call('fingerprint_service')
    def stop(self) -> None:
        """Stop the fingerprint service and clean up."""
        if not self._is_running:
            return
            
        try:
            # Clear cache on shutdown
            with self._cache_lock:
                self._last_sent_action_cache.clear()
            
            self._subscriptions_active = False
            self._is_running = False
            self.logger.info("FingerprintService stopped successfully.")
        except Exception as e:
            self.logger.error(f"Error stopping FingerprintService: {e}", exc_info=True)
    
    def is_ready(self) -> bool:
        """Check if the service is ready."""
        return self._is_running and self._subscriptions_active

    @log_function_call('fingerprint_service')
    def is_duplicate(self, fingerprint: tuple, context: Optional[dict] = None) -> bool:
        """
        NEW INTERFACE: Checks if a new signal is a logical duplicate of the last action taken for a symbol.
        
        Args:
            fingerprint: The fingerprint tuple (symbol, action, quantity, cost)
            context: Optional context dict containing additional info
            
        Returns:
            True if the new signal should be suppressed.
        """
        if not fingerprint or len(fingerprint) < 1:
            self.logger.warning(f"Invalid fingerprint for duplicate check: {fingerprint}")
            return False
            
        # Extract symbol from fingerprint (first element)
        symbol = fingerprint[0] if isinstance(fingerprint[0], str) else str(fingerprint[0])
        symbol_upper = symbol.upper()
        
        with self._cache_lock:
            if symbol_upper not in self._last_sent_action_cache:
                # No previous action has been sent for this symbol. It's not a duplicate.
                with self._stats_lock:
                    self._stats['new_actions_allowed'] += 1
                self.logger.info(f"[{symbol_upper}] New action allowed. Fingerprint {fingerprint} (no prior action).")
                return False

            last_state = self._last_sent_action_cache[symbol_upper]
            
            # THE SMART LOGIC
            
            # Rule 1: Is the proposed action exactly the same as the last one we sent?
            if fingerprint == last_state.fingerprint:
                # Yes. Is the last action stale?
                age_seconds = time.time() - last_state.timestamp
                if age_seconds > self._stale_order_expiry_seconds:
                    # The last order seems lost or stuck. Clear it and allow this new one.
                    with self._stats_lock:
                        self._stats['stale_orders_cleared'] += 1
                        self._stats['new_actions_allowed'] += 1
                    self.logger.warning(f"[{symbol_upper}] Stale duplicate detected (age: {age_seconds:.1f}s). "
                                      f"Clearing stale order {last_state.order_id} and allowing new signal.")
                    # We will allow the new action to proceed by returning False.
                    # The `update` call will overwrite the stale entry.
                    return False
                else:
                    # The last action is recent. Suppress this identical signal.
                    with self._stats_lock:
                        self._stats['duplicates_suppressed'] += 1
                    self.logger.info(f"[{symbol_upper}] DUPLICATE SUPPRESSED. Fingerprint {fingerprint} "
                                   f"matches recent action for order {last_state.order_id} (age: {age_seconds:.1f}s).")
                    return True
            
            # Rule 2: The proposed action is different from the last one.
            # (e.g., last was ADD, new is REDUCE). This is a new, valid step.
            with self._stats_lock:
                self._stats['new_actions_allowed'] += 1
            self.logger.info(f"[{symbol_upper}] New action allowed. Fingerprint {fingerprint} "
                           f"differs from last action {last_state.fingerprint}.")
            return False

    @log_function_call('fingerprint_service')
    def update(self, fingerprint: tuple, signal_data: dict) -> None:
        """
        Updates the cache with the fingerprint of the order that was just sent.
        This is called AFTER an order is successfully published.
        
        Args:
            fingerprint: The fingerprint tuple (symbol, action, quantity, cost)
            signal_data: Dict containing order_id and other signal metadata
        """
        if not fingerprint or len(fingerprint) < 1:
            self.logger.warning(f"Invalid fingerprint for update: {fingerprint}")
            return
            
        # Extract symbol and order_id
        symbol = fingerprint[0] if isinstance(fingerprint[0], str) else str(fingerprint[0])
        symbol_upper = symbol.upper()
        order_id = signal_data.get('order_id') or signal_data.get('request_id') or 'UNKNOWN'
        
        with self._cache_lock:
            self.logger.info(f"[{symbol_upper}] Updating last sent action. "
                           f"Fingerprint: {fingerprint}, Order ID: {order_id}")
            self._last_sent_action_cache[symbol_upper] = LastActionState(
                fingerprint=fingerprint,
                timestamp=time.time(),
                order_id=order_id
            )

    def _handle_order_filled(self, event: OrderFilledEvent) -> None:
        """
        Handle order filled events - clear the fingerprint to allow new actions.
        """
        try:
            if not event.data or not hasattr(event.data, 'symbol'):
                return
                
            symbol = event.data.symbol.upper()
            order_id = getattr(event.data, 'order_id', None) or getattr(event.data, 'id', None)
            
            with self._cache_lock:
                if symbol in self._last_sent_action_cache:
                    last_state = self._last_sent_action_cache[symbol]
                    
                    # Clear regardless of order_id match - a fill means we can take new action
                    del self._last_sent_action_cache[symbol]
                    
                    with self._stats_lock:
                        self._stats['fills_cleared'] += 1
                        
                    self.logger.info(f"[{symbol}] Order FILLED. Cleared fingerprint cache "
                                   f"(was tracking order {last_state.order_id}).")
                    
        except Exception as e:
            self.logger.error(f"Error handling order filled event: {e}", exc_info=True)

    def _handle_order_status_update(self, event: OrderStatusUpdateEvent) -> None:
        """
        Handles order status updates to clear the state, allowing for new actions.
        This is the key to making the system "smart".
        """
        try:
            if not event.data:
                return

            # Extract order details
            symbol = getattr(event.data, 'symbol', '').upper()

            # FUZZY'S CRITICAL FIX: Handle both string and enum status types
            raw_status = getattr(event.data, 'status', '')
            if hasattr(raw_status, 'value'):
                # It's an enum - get the string value
                status = str(raw_status.value).lower()
            elif hasattr(raw_status, 'name'):
                # It's an enum - get the name
                status = str(raw_status.name).lower()
            else:
                # It's already a string
                status = str(raw_status).lower()

            order_id = getattr(event.data, 'order_id', None) or getattr(event.data, 'id', None)

            if not symbol:
                return

            # We care about terminal states that "complete" a trade idea
            # Handle various status formats (enum values, names, and string variations)
            terminal_statuses = [
                'filled', 'canceled', 'cancelled', 'rejected', 'expired',
                'pendingsubmission', 'pending_submission',  # Sometimes these are terminal failures
                'ls_rejection_from_broker', 'rejected_by_broker'
            ]

            if any(term_status in status for term_status in terminal_statuses):
                with self._cache_lock:
                    if symbol in self._last_sent_action_cache:
                        last_state = self._last_sent_action_cache[symbol]

                        # Clear the cache entry
                        del self._last_sent_action_cache[symbol]

                        # Update stats based on status
                        with self._stats_lock:
                            if 'filled' in status:
                                self._stats['fills_cleared'] += 1
                            elif any(cancel_term in status for cancel_term in ['cancel', 'cancelled']):
                                self._stats['cancelled_cleared'] += 1
                            elif 'reject' in status:
                                self._stats['rejected_cleared'] += 1

                        self.logger.info(f"[{symbol}] Order {status.upper()}. "
                                       f"Cleared fingerprint cache (was tracking order {last_state.order_id}).")

        except Exception as e:
            self.logger.error(f"Error handling order status update: {e}", exc_info=True)

    def get_cache_stats(self) -> dict:
        """Get performance statistics for monitoring."""
        with self._stats_lock:
            stats = self._stats.copy()
        
        with self._cache_lock:
            stats['cached_symbols'] = len(self._last_sent_action_cache)
            stats['active_orders'] = [
                {
                    'symbol': symbol,
                    'fingerprint': state.fingerprint,
                    'order_id': state.order_id,
                    'age_seconds': time.time() - state.timestamp
                }
                for symbol, state in self._last_sent_action_cache.items()
            ]
        
        return stats

    def invalidate_by_symbol(self, symbol: str) -> None:
        """
        Manually invalidate the fingerprint for a symbol.
        
        Args:
            symbol: Symbol to clear from cache
        """
        symbol_upper = symbol.upper()
        
        with self._cache_lock:
            if symbol_upper in self._last_sent_action_cache:
                last_state = self._last_sent_action_cache[symbol_upper]
                del self._last_sent_action_cache[symbol_upper]
                self.logger.info(f"[{symbol_upper}] Manually invalidated fingerprint "
                               f"(was tracking order {last_state.order_id}).")