# /modules/active_symbols/active_symbols_service.py
import logging
import threading
from typing import Any, Dict, Set, Optional
from collections import defaultdict

from interfaces.core.services import IEventBus
from interfaces.logging.services import ICorrelationLogger
from core.events import PositionUpdateEvent, OpenOrderSubmittedEvent, OrderStatusUpdateEvent, ActiveSymbolSetChangedEvent, ActiveSymbolSetChangedData, SymbolPublishingStateChangedEvent, SymbolActivatedEvent, SymbolActivatedEventData, SymbolDeactivatedEvent, SymbolDeactivatedEventData
from data_models import OrderLSAgentStatus

logger = logging.getLogger(__name__)

class ActiveSymbolsService:
    """
    Manages the set of symbols that are currently active (due to an open position)
    or have a pending open order. This service subscribes to relevant trading events
    to keep its state synchronized.
    Provides a thread-safe method to check if a symbol is relevant for publishing.
    """

    def __init__(self, 
                 event_bus: IEventBus,
                 correlation_logger: ICorrelationLogger,
                 position_manager_ref: Optional[Any] = None,
                 price_fetching_service_ref: Optional[Any] = None): # <<< NEW PARAMETER
        self.logger = logger
        self.logger.critical("### [DI_TRACE] ActiveSymbolsService: __init__ called. PositionManager is currently None. ###")
        self.event_bus = event_bus
        self._correlation_logger = correlation_logger

        # _active_symbols: Symbols with a confirmed open position.
        self._active_symbols: Set[str] = set()
        # _pending_open_symbols: Symbols with an open order submitted but not yet terminal (filled, cancelled, rejected).
        self._pending_open_symbols: Set[str] = set()
        # _pending_open_orders_by_symbol: Tracks specific order IDs that are pending open for each symbol.
        # This helps manage scenarios where multiple open orders for the same symbol might exist.
        self._pending_open_orders_by_symbol: Dict[str, Set[str]] = defaultdict(set)

        self._state_lock = threading.RLock()  # Protects all three state variables
        self.position_manager_ref = position_manager_ref  # Store reference for later use
        self.price_fetching_service_ref = price_fetching_service_ref # <<< STORE THE REFERENCE

        self._subscribe_to_events()
        self.logger.info("ActiveSymbolsService initialized and subscribed to events.")
        
    def bootstrap_with_initial_symbols(self, initial_symbols: Set[str]):
        """Bootstrap the service with initial symbols from configuration."""
        if initial_symbols:
            with self._state_lock:
                # Add initial symbols as "pending" since we don't know their position status yet
                self._pending_open_symbols.update(symbol.upper() for symbol in initial_symbols)
            self.logger.info(f"ActiveSymbolsService: Bootstrapped with {len(initial_symbols)} initial symbols: {sorted(initial_symbols)}")
            
            # Log bootstrap event for correlation
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="ActiveSymbolsService",
                    event_name="SymbolsBootstrapped",
                    payload={
                        "symbol_count": len(initial_symbols),
                        "symbols": sorted(list(initial_symbols)),
                        "source": "configuration"
                    }
                )
            
            # Defer data feed subscriptions until service references are available
            # This prevents warnings during DI container initialization
            self.logger.info("ActiveSymbolsService: Symbol subscriptions will be handled after service wiring is complete")
        else:
            self.logger.info("ActiveSymbolsService: No initial symbols provided for bootstrap")

        if self.position_manager_ref: # Bootstrap with current positions if PM is provided
            self.bootstrap_from_position_manager(self.position_manager_ref)
        else:
            self.logger.info("ActiveSymbolsService: PositionManager will be provided after DI container setup. "
                             "Bootstrap will occur during ApplicationCore initialization.")
        
        # <<< --- NEW LOGIC --- >>>
        if self.price_fetching_service_ref:
            self.logger.info("ActiveSymbolsService: PriceFetchingService reference provided.")
        else:
            self.logger.warning("ActiveSymbolsService: PriceFetchingService reference NOT provided. Dynamic subscriptions will not work.")
        # <<< --- END NEW LOGIC --- >>>


    def _subscribe_to_events(self):
        """Subscribes to necessary events on the event bus."""
        # Keep strong references to handlers
        self._position_update_handler_ref = self._handle_position_update_event
        self._open_order_submitted_handler_ref = self._handle_open_order_submitted
        self._order_status_update_handler_ref = self._handle_order_status_update
        self._symbol_publishing_state_changed_handler_ref = self._handle_symbol_publishing_state_changed

        self.event_bus.subscribe(PositionUpdateEvent, self._position_update_handler_ref)
        self.event_bus.subscribe(OpenOrderSubmittedEvent, self._open_order_submitted_handler_ref)
        self.event_bus.subscribe(OrderStatusUpdateEvent, self._order_status_update_handler_ref)
        self.event_bus.subscribe(SymbolPublishingStateChangedEvent, self._symbol_publishing_state_changed_handler_ref)
        self.logger.info("ActiveSymbolsService: Subscriptions to PositionUpdateEvent, "
                         "OpenOrderSubmittedEvent, OrderStatusUpdateEvent, and SymbolPublishingStateChangedEvent are active.")

    def bootstrap_from_position_manager(self, position_manager: Any):
        """
        Initializes the active symbols based on the current state from PositionManager.
        Should be called once at startup.
        """
        self.logger.critical("ASS_BOOTSTRAP_FROM_PM_START: Bootstrapping active symbols from PositionManager...")
        try:
            # Assuming position_manager has a method to get all current positions
            # And each position data has 'symbol' and 'is_open' (or 'quantity'/'shares')
            self.logger.critical("ASS_CALLING_PM_SNAPSHOT: About to call position_manager.get_all_positions_snapshot()")
            current_positions = position_manager.get_all_positions_snapshot() # Or similar method
            self.logger.critical(f"ASS_PM_SNAPSHOT_RESULT: Received {len(current_positions)} positions from PositionManager: {list(current_positions.keys())}")
            with self._state_lock:
                self._active_symbols.clear()
                for symbol_upper, pos_data in current_positions.items():
                    # Ensure symbol is uppercase
                    symbol_val = symbol_upper.upper()
                    # Check if position is open (e.g., non-zero quantity/shares or an explicit 'is_open' flag)
                    is_open = pos_data.get('open', False) or \
                                (abs(pos_data.get('quantity', 0.0)) > 1e-9) or \
                                (abs(pos_data.get('shares', 0.0)) > 1e-9)
                    if is_open:
                        self._active_symbols.add(symbol_val)
                        self.logger.critical(f"ASS_BOOTSTRAP_SYMBOL_ADDED: '{symbol_val}' added to active symbols during bootstrap.")
                        # Trigger subscription for this symbol
                        self._subscribe_to_data_feed(symbol_val)
                self.logger.critical(f"ASS_BOOTSTRAP_COMPLETE: Bootstrapped with {len(self._active_symbols)} active symbols: {list(self._active_symbols)}")
                
                # Publish FULL_UPDATE event after bootstrap if any symbols were added
                if len(self._active_symbols) > 0:
                    self._publish_active_symbol_set_changed("FULL_UPDATE", "", "BOOTSTRAP_FROM_POSITION_MANAGER")
                
                # Log position manager bootstrap event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="ActiveSymbolsService",
                        event_name="PositionManagerBootstrapComplete",
                        payload={
                            "active_symbol_count": len(self._active_symbols),
                            "active_symbols": sorted(list(self._active_symbols)),
                            "total_positions_received": len(current_positions)
                        }
                    )
        except AttributeError:
            self.logger.error("ActiveSymbolsService: PositionManager does not have a 'get_all_positions_snapshot' "
                              "or similar method, or position data format is unexpected. Bootstrap failed.")
        except Exception as e:
            self.logger.error(f"ActiveSymbolsService: Error during bootstrap from PositionManager: {e}", exc_info=True)

    def bootstrap_with_current_positions(self):
        """
        Bootstrap method that can be called after initialization when PositionManager becomes available.
        This is called by ApplicationCore after DI container setup is complete.
        """
        if self.position_manager_ref:
            self.logger.critical("ASS_BOOTSTRAP_START: Bootstrap requested with available PositionManager reference.")
            self.bootstrap_from_position_manager(self.position_manager_ref)
            self.logger.critical("ASS_BOOTSTRAP_FINISHED: Bootstrap completed successfully.")
        else:
            self.logger.critical("ASS_BOOTSTRAP_FAILED: Bootstrap requested but no PositionManager reference available.")

    def set_position_manager_ref(self, position_manager: Any):
        """Set the PositionManager reference for later bootstrap."""
        self.logger.critical("### [DI_TRACE] ActiveSymbolsService: set_position_manager_ref called. PositionManager is now being set. ###")
        self.logger.critical(f"ASS_SET_PM_REF: PositionManager reference set: {position_manager is not None}")
        self.position_manager_ref = position_manager

    def set_price_fetching_service_ref(self, price_fetching_service: Any):
        """Set the PriceFetchingService reference for data feed subscriptions."""
        self.logger.critical("### [DI_TRACE] ActiveSymbolsService: set_price_fetching_service_ref called. PriceFetchingService is now being set. ###")
        self.logger.info(f"ASS_SET_PFS_REF: PriceFetchingService reference set: {price_fetching_service is not None}")
        self.price_fetching_service_ref = price_fetching_service
        self.logger.info("ASS: PriceFetchingService should already be subscribed to all CSV symbols. ActiveSymbolsService only tracks which symbols are active for trading.")

    def _handle_open_order_submitted(self, event: OpenOrderSubmittedEvent):
        """Adds a symbol to pending and subscribes to its market data."""
        try:
            symbol = event.data.symbol.upper()
            local_order_id = str(event.data.local_order_id)
            
            # --- We need to know if this is a genuinely new symbol ---
            is_newly_relevant = False
            with self._state_lock:
                if not self.is_relevant_for_publishing(symbol):
                    is_newly_relevant = True
                
                self._pending_open_symbols.add(symbol)
                self._pending_open_orders_by_symbol[symbol].add(local_order_id)
            
            # --- If it's new, subscribe to the data feed and publish activation event ---
            if is_newly_relevant:
                self._subscribe_to_data_feed(symbol)
                # Publish specific symbol activation event for Bloom filter
                self.event_bus.publish(SymbolActivatedEvent(
                    data=SymbolActivatedEventData(
                        symbol=symbol,
                        reason="ORDER_SUBMITTED"
                    )
                ))

            self.logger.info(f"ASS_PENDING_ADD: Symbol '{symbol}' added to PENDING OPEN.")
            
            # Log open order event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="ActiveSymbolsService",
                    event_name="SymbolAddedToPending",
                    payload={
                        "symbol": symbol,
                        "local_order_id": local_order_id,
                        "is_newly_relevant": is_newly_relevant,
                        "pending_order_count": len(self._pending_open_orders_by_symbol.get(symbol, set()))
                    }
                )
        except Exception as e:
            self.logger.error(f"ASS: Error in _handle_open_order_submitted: {e}", exc_info=True)

    def _handle_position_update_event(self, event: PositionUpdateEvent):
        """Updates active lists and subscribes to market data if newly active."""
        try:
            # Ensure event.data is the payload object (e.g., PositionUpdateEventData)
            symbol = event.data.symbol.upper()
            # Determine if open based on quantity or an explicit flag
            is_open = abs(getattr(event.data, 'quantity', 0.0)) > 1e-9 or \
                      getattr(event.data, 'is_open', False)

            is_newly_relevant = False
            symbol_set_changed = False
            change_type = ""
            
            with self._state_lock:
                if is_open and not self.is_relevant_for_publishing(symbol):
                    is_newly_relevant = True
                
                # Track if symbol was already in active set before changes
                was_in_active_set = symbol in self._active_symbols

                if is_open:
                    self._active_symbols.add(symbol)
                    self.logger.info(f"ASS_ACTIVE_ADD: Symbol '{symbol}' ADDED/CONFIRMED in ACTIVE list. "
                                     f"Active: {list(self._active_symbols)}")
                    
                    # Check if this was actually a new addition
                    if not was_in_active_set:
                        symbol_set_changed = True
                        change_type = "SYMBOL_ADDED"
                    
                    # If it's now active, it's no longer just pending
                    if symbol in self._pending_open_symbols:
                        self._pending_open_symbols.discard(symbol)
                        self.logger.info(f"ASS_PENDING_CLEAR_ON_ACTIVE: Symbol '{symbol}' moved from pending to active.")
                    if symbol in self._pending_open_orders_by_symbol:
                        del self._pending_open_orders_by_symbol[symbol] # Clear all specific pending orders
                        self.logger.info(f"ASS_PENDING_ORDERS_CLEAR_ON_ACTIVE: Cleared specific pending orders for '{symbol}'.")
                else: # Position is closed
                    self._active_symbols.discard(symbol)
                    self.logger.info(f"ASS_ACTIVE_REMOVE: Symbol '{symbol}' REMOVED from ACTIVE list. "
                                     f"Active: {list(self._active_symbols)}")
                    
                    # Check if this was actually a removal
                    if was_in_active_set:
                        symbol_set_changed = True
                        change_type = "SYMBOL_REMOVED"
                    
                    # If closed, and was pending, ensure it's fully cleared from pending states too.
                    # (This might be redundant if _handle_order_status_update handles terminal states for pending orders well)
                    if symbol in self._pending_open_symbols:
                        self._pending_open_symbols.discard(symbol)
                        self.logger.info(f"ASS_PENDING_CLEAR_ON_CLOSE: Symbol '{symbol}' also removed from PENDING due to close.")
                    if symbol in self._pending_open_orders_by_symbol:
                        del self._pending_open_orders_by_symbol[symbol]
                        self.logger.info(f"ASS_PENDING_ORDERS_CLEAR_ON_CLOSE: Cleared specific pending orders for '{symbol}' due to close.")
                
                # Publish specific events if the active symbol set actually changed
                if symbol_set_changed:
                    if is_open:
                        # Symbol became active (position opened)
                        self.event_bus.publish(SymbolActivatedEvent(
                            data=SymbolActivatedEventData(
                                symbol=symbol,
                                reason="POSITION_OPENED"
                            )
                        ))
                    else:
                        # Symbol became inactive (position closed)
                        self.event_bus.publish(SymbolDeactivatedEvent(
                            data=SymbolDeactivatedEventData(
                                symbol=symbol,
                                reason="POSITION_CLOSED"
                            )
                        ))
                    
                    # Keep legacy event for backward compatibility if needed
                    reason = "POSITION_OPENED" if is_open else "POSITION_CLOSED"
                    self._publish_active_symbol_set_changed(change_type, symbol, reason)

            # --- If it's new, subscribe to the data feed and publish activation event ---
            if is_newly_relevant:
                self._subscribe_to_data_feed(symbol)
                # Publish activation event if this is a newly relevant symbol (not covered by symbol_set_changed above)
                if not symbol_set_changed:  # Avoid duplicate events
                    self.event_bus.publish(SymbolActivatedEvent(
                        data=SymbolActivatedEventData(
                            symbol=symbol,
                            reason="POSITION_OPENED"
                        )
                    ))
                
            self.logger.info(f"ASS_POS_UPDATE_RECV: Processed PositionUpdateEvent for '{symbol}'. IsOpen={is_open}")
            
            # Log position update event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="ActiveSymbolsService",
                    event_name="PositionUpdateProcessed",
                    payload={
                        "symbol": symbol,
                        "is_open": is_open,
                        "is_newly_relevant": is_newly_relevant,
                        "active_symbol_count": len(self._active_symbols),
                        "pending_symbol_count": len(self._pending_open_symbols)
                    }
                )
        except Exception as e:
            self.logger.error(f"ASS: Error in _handle_position_update_event: {e}", exc_info=True)

    def _handle_order_status_update(self, event: OrderStatusUpdateEvent):
        """Cleans up pending lists if an open order reaches a terminal state (filled, cancelled, rejected)."""
        try:
            data = event.data
            symbol = data.symbol.upper()
            local_order_id = str(data.local_order_id)

            with self._state_lock:
                # Only process if we are tracking this specific order as pending for this symbol
                if not (symbol in self._pending_open_orders_by_symbol and \
                        local_order_id in self._pending_open_orders_by_symbol[symbol]):
                    return # Not relevant to our pending open tracking

                order_status = data.status
                status_name_upper = "UNKNOWN_STATUS" # For logging if needed
                is_terminal_failure = False
                is_filled = False
                is_other_terminal = False # For non-fill, non-failure terminal states

                if isinstance(order_status, str):
                    status_name_upper = order_status.upper()
                    # Map known strings to enum members or handle directly
                    if status_name_upper == OrderLSAgentStatus.CANCELLED.value.upper():
                        is_terminal_failure = True
                    elif status_name_upper == OrderLSAgentStatus.REJECTED.value.upper():
                        is_terminal_failure = True
                    elif status_name_upper == OrderLSAgentStatus.EXPIRED.value.upper():
                        is_other_terminal = True # EXPIRED is terminal but not a "failure" for pending open orders
                    elif status_name_upper == OrderLSAgentStatus.LS_KILLED.value.upper():
                        is_terminal_failure = True # KILLED is considered a failure
                    elif status_name_upper == OrderLSAgentStatus.DONE_FOR_DAY.value.upper():
                        is_other_terminal = True # DONE_FOR_DAY is terminal but not a failure
                    elif status_name_upper == OrderLSAgentStatus.STOPPED.value.upper():
                        is_other_terminal = True # STOPPED is terminal but not a failure
                    elif status_name_upper == OrderLSAgentStatus.FILLED.value.upper():
                        is_filled = True
                    else:
                        self.logger.warning(f"ASS: Unhandled string order status '{status_name_upper}' for order {local_order_id} of symbol {symbol}.")
                        # For safety, ignore unhandled strings and not proceed with this event
                        return

                elif isinstance(order_status, OrderLSAgentStatus): # Check if it's already an enum instance
                    status_name_upper = order_status.name # For logging
                    is_terminal_failure = order_status in [
                        OrderLSAgentStatus.CANCELLED,
                        OrderLSAgentStatus.REJECTED,
                        OrderLSAgentStatus.LS_KILLED
                    ]
                    is_filled = (order_status == OrderLSAgentStatus.FILLED)
                    is_other_terminal = order_status in [
                        OrderLSAgentStatus.EXPIRED,
                        OrderLSAgentStatus.DONE_FOR_DAY,
                        OrderLSAgentStatus.STOPPED
                    ]
                    # Handle non-terminal intermediate statuses (these don't change pending status)
                    is_intermediate_status = order_status in [
                        OrderLSAgentStatus.SENT_TO_LIGHTSPEED,
                        OrderLSAgentStatus.PENDING_SUBMISSION,
                        OrderLSAgentStatus.LS_SUBMITTED,
                        OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE,
                        OrderLSAgentStatus.PARTIALLY_FILLED,
                        OrderLSAgentStatus.PENDING_CANCEL,
                        OrderLSAgentStatus.CANCEL_REJECTED
                    ]
                else:
                    self.logger.warning(f"ASS: Unknown order status type for order {local_order_id} of symbol {symbol}: {type(order_status)} - {order_status}")
                    return

                # Handle intermediate statuses - log but don't process as terminal
                if isinstance(order_status, OrderLSAgentStatus) and is_intermediate_status:
                    self.logger.debug(f"ASS: Intermediate order status '{status_name_upper}' for order {local_order_id} of symbol {symbol} - no action needed.")
                    return  # Don't process intermediate statuses

                is_terminal_event = is_terminal_failure or is_filled or is_other_terminal

                if is_terminal_event:
                    self.logger.info(f"ASS_PENDING_ORDER_TERMINAL: Pending open order {local_order_id} for '{symbol}' reached terminal state: {status_name_upper}.")
                    self._pending_open_orders_by_symbol[symbol].discard(local_order_id)

                    if not self._pending_open_orders_by_symbol[symbol]:
                        del self._pending_open_orders_by_symbol[symbol]
                        # If it's a non-fill terminal event AND the symbol is not already active via a PositionUpdateEvent
                        if (is_terminal_failure or is_other_terminal) and not is_filled and symbol not in self._active_symbols:
                            self._pending_open_symbols.discard(symbol)
                            self.logger.info(f"ASS_PENDING_SYMBOL_CLEAR: All pending orders for '{symbol}' resolved (last was {status_name_upper}). "
                                             f"Removed from general pending list. Pending symbols: {list(self._pending_open_symbols)}")
                        elif is_filled:
                            self.logger.info(f"ASS_PENDING_SYMBOL_FILLED: Last pending order for '{symbol}' was FILLED. "
                                             f"'{symbol}' remains in general pending until PositionUpdateEvent confirms active status.")
                        # else: (symbol is already active, or it was a fill - handled by PositionUpdateEvent)
                        
                    # Log order status terminal event
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="ActiveSymbolsService",
                            event_name="OrderTerminalStateProcessed",
                            payload={
                                "symbol": symbol,
                                "local_order_id": local_order_id,
                                "status": status_name_upper,
                                "is_filled": is_filled,
                                "is_terminal_failure": is_terminal_failure,
                                "remaining_pending_orders": len(self._pending_open_orders_by_symbol.get(symbol, set())),
                                "symbol_still_pending": symbol in self._pending_open_symbols
                            }
                        )


        except Exception as e:
            self.logger.error(f"ASS: Error in _handle_order_status_update: {e}", exc_info=True)

    def is_relevant_for_publishing(self, symbol: str) -> bool:
        """
        Checks if a symbol is either in an active position or has a pending open order.
        This read is designed to be fast and "lock-free" (relies on GIL for dict/set "in" atomicity).
        """
        symbol_upper = symbol.upper()
        # Check active symbols first (most common case for established positions)
        if symbol_upper in self._active_symbols:
            return True
        # Then check if it's in the general pending list
        if symbol_upper in self._pending_open_symbols:
            return True
        return False

    def _subscribe_to_data_feed(self, symbol: str):
        """
        Tells the PriceFetchingService to subscribe to a symbol.
        
        This enables proactive subscriptions for newly discovered symbols from OCR.
        """
        self.logger.info(f"ASS: Requesting data feed subscription for '{symbol}'.")
        
        if self.price_fetching_service_ref and hasattr(self.price_fetching_service_ref, 'subscribe_symbol'):
            try:
                self.logger.info(f"ASS: Requesting dynamic subscription for '{symbol}'.")
                self.price_fetching_service_ref.subscribe_symbol(symbol)
                self.logger.info(f"ASS: Successfully subscribed to data feed for '{symbol}'.")
            except Exception as e:
                self.logger.error(f"ASS: Failed to call subscribe_symbol for '{symbol}': {e}", exc_info=True)
        else:
            self.logger.warning(f"ASS: Cannot dynamically subscribe to '{symbol}', PriceFetchingService reference not available or missing subscribe_symbol method.")

    def get_all_symbols(self) -> Set[str]:
        """Returns all symbols that are currently active or have pending orders."""
        with self._state_lock:
            return self._active_symbols.union(self._pending_open_symbols)
    
    def get_active_symbols(self) -> Set[str]:
        """Returns only symbols with confirmed active positions."""
        with self._state_lock:
            return self._active_symbols.copy()
    
    def get_current_active_symbols(self) -> Set[str]:
        """
        Returns current active symbols for bootstrap. 
        Alias for get_active_symbols() for clarity in MarketDataFilterService bootstrap.
        """
        return self.get_active_symbols()
    
    def get_diagnostics(self) -> Dict[str, list]:
        """Returns a snapshot of the internal state for diagnostics."""
        with self._state_lock:
            return {
                "active_symbols": sorted(list(self._active_symbols)),
                "pending_open_symbols": sorted(list(self._pending_open_symbols)),
                "pending_open_orders_by_symbol": {
                    s: sorted(list(ids)) for s, ids in self._pending_open_orders_by_symbol.items()
                }
            }

    def activate_symbol(self, symbol: str):
        """
        Public method to proactively activate a symbol, add it to the allow-list,
        and trigger a data feed subscription. Called by services that discover
        new, relevant symbols, like the OCR conditioner.
        """
        symbol_upper = symbol.upper()
        self.logger.info(f"ASS: Proactive activation requested for '{symbol_upper}'.")
        is_newly_relevant = False
        with self._state_lock:
            if not self.is_relevant_for_publishing(symbol_upper):
                is_newly_relevant = True
                # Add to a general "pending" or "active" list immediately.
                # Adding to pending is safer until a position is confirmed.
                self._pending_open_symbols.add(symbol_upper)

        if is_newly_relevant:
            # The original logic you had for this is perfect.
            self._subscribe_to_data_feed(symbol_upper)
            
            # Log proactive symbol activation
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="ActiveSymbolsService",
                    event_name="ProactiveSymbolActivation",
                    payload={
                        "symbol": symbol_upper,
                        "total_active_symbols": len(self._active_symbols),
                        "total_pending_symbols": len(self._pending_open_symbols)
                    }
                )

    def _publish_active_symbol_set_changed(self, change_type: str, changed_symbol: str = "", reason: str = ""):
        """
        Publishes ActiveSymbolSetChangedEvent when the active symbol set changes.
        
        Args:
            change_type: "SYMBOL_ADDED", "SYMBOL_REMOVED", or "FULL_UPDATE"
            changed_symbol: The specific symbol that changed (if applicable)
            reason: Reason for the change (e.g., "POSITION_OPENED", "POSITION_CLOSED")
        """
        # --- DIAGNOSTIC LOG ---
        self.logger.critical(
            f"### [ASS_PUBLISH_EVENT] Attempting to PUBLISH ActiveSymbolSetChangedEvent. "
            f"Symbol: {changed_symbol}, Reason: {reason} ###"
        )
        # --- END DIAGNOSTIC LOG ---
        try:
            # Get current active symbols as immutable frozenset for memory efficiency
            current_active_symbols = frozenset(self._active_symbols)
            
            # Create event data with frozenset for memory optimization
            event_data = ActiveSymbolSetChangedData(
                active_symbols=current_active_symbols,
                change_type=change_type,
                changed_symbol=changed_symbol,
                reason=reason
            )
            
            # Create and publish event
            event = ActiveSymbolSetChangedEvent(data=event_data)
            self.event_bus.publish(event)
            
            self.logger.info(f"ASS_EVENT_PUBLISHED: ActiveSymbolSetChangedEvent published - "
                           f"Type: {change_type}, Symbol: {changed_symbol}, "
                           f"ActiveCount: {len(current_active_symbols)}, Reason: {reason}")
            
            # Log to correlation logger if available
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="ActiveSymbolsService",
                    event_name="ActiveSymbolSetChanged",
                    payload={
                        "change_type": change_type,
                        "changed_symbol": changed_symbol,
                        "reason": reason,
                        "active_symbol_count": len(current_active_symbols),
                        "active_symbols": sorted(list(current_active_symbols))
                    }
                )
                
        except Exception as e:
            self.logger.error(f"ASS: Error publishing ActiveSymbolSetChangedEvent: {e}", exc_info=True)

    def _handle_symbol_publishing_state_changed(self, event: SymbolPublishingStateChangedEvent):
        """
        Handles SymbolPublishingStateChangedEvent from PositionManager.
        Converts individual symbol state changes into ActiveSymbolSetChangedEvent.
        """
        try:
            symbol = event.data.symbol.upper()
            should_publish = event.data.should_publish
            reason = event.data.reason
            
            self.logger.info(f"ASS_SYMBOL_STATE_RECV: Received SymbolPublishingStateChangedEvent for {symbol} - "
                           f"ShouldPublish: {should_publish}, Reason: {reason}")
            
            # Update active symbols based on publishing state
            symbol_was_active = symbol in self._active_symbols
            
            if should_publish and not symbol_was_active:
                # Symbol should be active but isn't - add it
                self._active_symbols.add(symbol)
                self.logger.info(f"ASS_SYMBOL_ADDED: Added {symbol} to active symbols (reason: {reason})")
                self._publish_active_symbol_set_changed(
                    change_type="SYMBOL_ADDED",
                    changed_symbol=symbol,
                    reason=f"PUBLISHING_STATE_ENABLED_{reason}"
                )
                
            elif not should_publish and symbol_was_active:
                # Symbol should not be active but is - remove it
                self._active_symbols.discard(symbol)
                self.logger.info(f"ASS_SYMBOL_REMOVED: Removed {symbol} from active symbols (reason: {reason})")
                self._publish_active_symbol_set_changed(
                    change_type="SYMBOL_REMOVED", 
                    changed_symbol=symbol,
                    reason=f"PUBLISHING_STATE_DISABLED_{reason}"
                )
                
            else:
                # No change needed
                self.logger.debug(f"ASS_SYMBOL_NO_CHANGE: {symbol} publishing state matches active state - "
                                f"ShouldPublish: {should_publish}, IsActive: {symbol_was_active}")
                
        except Exception as e:
            self.logger.error(f"ASS: Error handling SymbolPublishingStateChangedEvent: {e}", exc_info=True)

    def stop(self):
        """Unsubscribes from the event bus."""
        try:
            if hasattr(self, '_position_update_handler_ref'):
                self.event_bus.unsubscribe(PositionUpdateEvent, self._position_update_handler_ref)
            if hasattr(self, '_open_order_submitted_handler_ref'):
                self.event_bus.unsubscribe(OpenOrderSubmittedEvent, self._open_order_submitted_handler_ref)
            if hasattr(self, '_order_status_update_handler_ref'):
                self.event_bus.unsubscribe(OrderStatusUpdateEvent, self._order_status_update_handler_ref)
            if hasattr(self, '_symbol_publishing_state_changed_handler_ref'):
                self.event_bus.unsubscribe(SymbolPublishingStateChangedEvent, self._symbol_publishing_state_changed_handler_ref)
            self.logger.info("ActiveSymbolsService: Unsubscribed from all events.")
        except Exception as e:
            self.logger.error(f"ActiveSymbolsService: Error during unsubscription: {e}", exc_info=True)
