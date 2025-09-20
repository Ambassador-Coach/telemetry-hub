# modules/trade_management/trade_lifecycle_manager_service.py
"""
TradeLifecycleManagerService module.

This module provides the TradeLifecycleManagerService class that implements ITradeLifecycleManager.
It manages the overall state (lifecycle) of individual trades, from creation through finalization.
Ported from legacy TradeLifecycleManager with enhanced dependency injection.
"""

import logging
import threading
import copy
import time
import uuid  # ✅ SME REQUIREMENT: For fallback correlation_id generation
import queue
from typing import Dict, Any, List, Optional, Callable, Tuple

# Interfaces and Data Classes
from interfaces.trading.services import (
    ITradeLifecycleManager, 
    IPositionManager,
    LifecycleState,
    TradeOpenedCallback,
    TradeClosedCallback
)
# Dependencies
from interfaces.order_management.services import IOrderRepository
from data_models.order_data_types import Order, FillRecord
from data_models import OrderLSAgentStatus
from interfaces.core.services import IEventBus
from interfaces.core.services import IConfigService
from interfaces.logging.services import ICorrelationLogger

logger = logging.getLogger(__name__)


class TradeLifecycleManagerService(ITradeLifecycleManager):
    """
    Service that manages the overall state (lifecycle) of trades and the global event ledger.
    
    Manages trades from creation (OPEN_PENDING) through active management (OPEN_ACTIVE, CLOSE_PENDING)
    to finalization (CLOSED, ERROR). Maintains a dictionary of all trades and a global event ledger.
    """

    def __init__(self,
                 order_repository: IOrderRepository,
                 position_manager: IPositionManager,
                 event_bus: Optional[IEventBus] = None,
                 config_service: Optional[IConfigService] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initialize the TradeLifecycleManagerService.

        Args:
            order_repository: Service to query order details
            position_manager: Position management service for trade state updates
            event_bus: Optional event bus for future enhancements
            config_service: Configuration service for stream names and settings
            correlation_logger: Correlation logger for publishing lifecycle events
        """
        self._order_repository = order_repository
        self._position_manager = position_manager
        self._event_bus = event_bus
        self._config_service = config_service
        self._correlation_logger = correlation_logger
        # ApplicationCore dependency removed for clean architecture
        self._trades_lock = threading.RLock()
        self._all_trades: Dict[int, Dict[str, Any]] = {}
        self._ledger: List[Dict[str, Any]] = []
        self._last_trade_id: int = 0
        self._trade_opened_callbacks: List[TradeOpenedCallback] = []
        self._trade_closed_callbacks: List[TradeClosedCallback] = []
        
        # Initialize async IPC publishing infrastructure
        self._ipc_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        self._ipc_failures = 0
        self._ipc_circuit_open = False
        self._ipc_circuit_threshold = 10  # Number of failures before opening circuit
        # Thread creation deferred to start() method

        # Check for CorrelationLogger availability for Redis publishing
        if not self._correlation_logger:
            logger.warning("TradeLifecycleManagerService initialized WITHOUT CorrelationLogger. Redis publishing will be disabled.")
        else:
            logger.info("TradeLifecycleManagerService initialized WITH CorrelationLogger for Redis publishing.")
            
        # Clean architecture - no ApplicationCore reference needed
        logger.debug("TradeLifecycleManagerService initialized with clean architecture (no ApplicationCore dependency).")

        logger.info("TradeLifecycleManagerService initialized. Thread creation deferred to start() method.")
    
    def start(self):
        """Start the TradeLifecycleManagerService and its worker threads."""
        logger.info("Starting TradeLifecycleManagerService...")
        
        # Start IPC worker thread
        self._start_ipc_worker()
        
        logger.info("TradeLifecycleManagerService started successfully.")
    
    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="TradeLifecycleManager-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
        logger.info("Started async IPC publishing worker thread")
    
    def _ipc_worker_loop(self):
        """Worker loop that processes IPC messages asynchronously."""
        while not self._ipc_shutdown_event.is_set():
            try:
                # Get message from queue with timeout
                try:
                    target_stream, redis_message = self._ipc_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Skip if circuit breaker is open
                if self._ipc_circuit_open:
                    if self._ipc_failures < self._ipc_circuit_threshold * 2:  # Try to recover
                        logger.debug(f"IPC circuit breaker open, discarding message for stream {target_stream}")
                        continue
                    else:
                        # Try to reset circuit breaker
                        self._ipc_circuit_open = False
                        self._ipc_failures = 0
                        logger.info("Attempting to reset IPC circuit breaker")
                
                # Legacy IPC code - now using telemetry service
                try:
                    # Legacy: This used to send via IPC, but now all publishing should use telemetry service
                    logger.debug(f"Legacy IPC queue processing - message queued but telemetry service should be used instead")
                    # Reset failure count 
                    if self._ipc_failures > 0:
                        self._ipc_failures = 0
                        
                except Exception as e:
                    self._ipc_failures += 1
                    logger.error(f"Async IPC: Error processing legacy queue: {e}")
                    if self._ipc_failures >= self._ipc_circuit_threshold:
                        self._ipc_circuit_open = True
                        logger.error(f"IPC circuit breaker opened after {self._ipc_failures} failures")
                    
            except Exception as e:
                logger.error(f"Error in IPC worker loop: {e}", exc_info=True)
                
        logger.info("IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str):
        """Queue an IPC message for async publishing."""
        try:
            self._ipc_queue.put_nowait((target_stream, redis_message))
        except queue.Full:
            logger.warning(f"IPC queue full, dropping message for stream '{target_stream}'")

    def _publish_trade_lifecycle_event_to_redis(self, event_type: str, trade_data: Dict[str, Any], additional_data: Optional[Dict[str, Any]] = None):
        """Publish trade lifecycle event to Redis stream via CorrelationLogger."""
        """
        # OLD CODE - COMMENTED OUT FOR REFERENCE
        if not self._telemetry_service:
            logger.warning("Telemetry service not available to publish trade lifecycle event")
            return
        """
        
        if not self._correlation_logger:
            logger.warning("Correlation logger not available to publish trade lifecycle event")
            return

        try:
            # Create payload with trade lifecycle information
            payload = {
                "event_type": event_type,
                "trade_id": trade_data.get("trade_id"),
                "symbol": trade_data.get("symbol"),
                "lifecycle_state": trade_data.get("lifecycle_state").name if hasattr(trade_data.get("lifecycle_state"), 'name') else str(trade_data.get("lifecycle_state")),
                "correlation_id": trade_data.get("correlation_id"),
                "origin": trade_data.get("origin"),
                "comment": trade_data.get("comment"),
                "shares": trade_data.get("shares"),
                "avg_price": trade_data.get("avg_price"),
                "local_orders": trade_data.get("local_orders", []),
                "timestamp": time.time()
            }

            # Add any additional data
            if additional_data:
                payload.update(additional_data)

            # Get target stream from config service
            if self._config_service:
                target_stream = getattr(self._config_service.get_config(), 'redis_stream_trade_lifecycle_events', 'testrade:trade-lifecycle-events')
            else:
                target_stream = 'testrade:trade-lifecycle-events'  # Default fallback
            
            # Add origin timestamp to payload
            payload["origin_timestamp_s"] = time.time()
            
            # Use CorrelationLogger for clean async publishing
            """
            # OLD CODE - COMMENTED OUT FOR REFERENCE
            success = self._telemetry_service.enqueue(
                source_component="TradeLifecycleManagerService",
                event_type="TESTRADE_TRADE_LIFECYCLE_EVENT",
                payload=payload,
                stream_override=target_stream,
                origin_timestamp_s=time.time()
            )
            """
            
            self._correlation_logger.log_event(
                source_component="TradeLifecycleManagerService",
                event_name="TradeLifecycleEvent",
                payload=payload,
                stream_override=target_stream
            )

            logger.debug(f"Trade lifecycle event logged to stream '{target_stream}': {event_type} for trade {trade_data.get('trade_id')}")

        except Exception as e:
            logger.error(f"TradeLifecycleManagerService: Error publishing lifecycle event to Redis: {e}", exc_info=True)

    def create_new_trade(self, symbol: str,
                         origin: Optional[str] = "MANUAL_CREATE",
                         correlation_id: Optional[str] = None,
                         initial_comment: Optional[str] = None
                         ) -> int:
        """
        Creates a new trade record. Stores the provided correlation_id (IntelliSense UUID).

        Args:
            symbol: The stock symbol for the trade
            origin: Optional origin description for tracking
            correlation_id: Optional correlation ID (IntelliSense UUID) for end-to-end nanosecond tracking.
                           This links the trade to the original OCR signal or other triggering event.
                           If None, the trade will not have IntelliSense correlation tracking.
            initial_comment: Optional initial comment for the trade.

        Returns:
            The trade ID (integer t_id) - LCM's internal key for this trade
        """
        with self._trades_lock:
            self._last_trade_id += 1
            t_id = self._last_trade_id  # Internal integer ID for the trade lifecycle entry

            # Prepare comment with correlation ID for easy logging/debug
            final_comment = initial_comment if initial_comment else f"Trade created via {origin}"
            if correlation_id:
                final_comment += f" (CorrID: {correlation_id[:8]}...)"

            # ✅ CRITICAL FIX: Dynamic parent flag based on origin
            is_parent = origin and ("OCR" in origin.upper() or "SYNC" in origin.upper())

            self._all_trades[t_id] = {
                "trade_id": t_id,                           # LCM's internal integer ID
                "correlation_id": correlation_id,           # <<< STORED INTELLISENSE UUID
                "symbol": symbol.upper(),
                "lifecycle_state": LifecycleState.OPEN_PENDING,
                "shares": 0.0,
                "avg_price": 0.0,
                "realized_pnl": 0.0,
                "start_time": time.time(),
                "end_time": None,
                "local_orders": [],  # List of OrderRepository local_ids
                "fills": [],
                "origin": origin,
                "comment": final_comment,
                "is_parent": is_parent  # ✅ DYNAMIC PARENT FLAG
            }

            # ✅ CORRELATION LOGGING: Track correlation ID flow
            try:
                from modules.shared.logging.correlation_logger import log_trade_lifecycle_correlation
                log_trade_lifecycle_correlation(
                    trade_id=str(t_id),
                    correlation_id=correlation_id,
                    symbol=symbol,
                    origin=origin
                )
            except ImportError:
                pass  # Correlation logging is optional

            logger.info(f"LCM: Created new trade {t_id} for {symbol.upper()} (origin: {origin}, correlation_id: {correlation_id})")

            # Publish trade creation event to Redis
            self._publish_trade_lifecycle_event_to_redis("TRADE_CREATED", self._all_trades[t_id])

            return t_id  # Return the internal integer t_id

    def bootstrap_id_counter(self) -> None:
        """
        Initializes the trade ID counter based on the highest trade_id
        currently loaded in the manager. MUST be called after any sync or load.
        """
        with self._trades_lock:
            if not self._all_trades:
                self._last_trade_id = 0
                logger.info("TLM_ID_BOOTSTRAP: No trades found. Counter starts at 0 (next will be 1).")
                return

            max_id = max(self._all_trades.keys()) if self._all_trades else 0
            
            # The next ID is simply the highest existing ID. create_new_trade increments it *before* use.
            self._last_trade_id = max_id
            logger.info(
                f"TLM_ID_BOOTSTRAP: Counter initialized from repository state. "
                f"Highest found ID was {max_id}. Next new trade ID will be {max_id + 1}."
            )

    def find_active_trade_for_symbol(self, symbol: str) -> Optional[int]:
        """
        Finds the trade_id for the currently active trade for a symbol.

        Args:
            symbol: The stock symbol to search for

        Returns:
            The trade ID if found, None otherwise
        """
        symbol_upper = symbol.upper()
        with self._trades_lock:
            for trade_id, trade_data in self._all_trades.items():
                if (trade_data.get("symbol") == symbol_upper and 
                    trade_data.get("lifecycle_state") not in [LifecycleState.CLOSED, LifecycleState.ERROR]):
                    return trade_id
            return None

    def update_on_order_submission(self, trade_id: int, local_id: int) -> None:
        """
        Associates a local_id with a trade_id when an order is submitted.

        Args:
            trade_id: The trade ID
            local_id: The local order ID to associate
        """
        with self._trades_lock:
            if trade_id in self._all_trades:
                self._all_trades[trade_id]["local_orders"].append(local_id)
                logger.debug(f"Associated local_id {local_id} with trade {trade_id}")
            else:
                logger.error(f"Cannot update trade {trade_id} - trade not found")

    def mark_trade_failed_to_open(self, trade_id: int, reason: str) -> None:
        """
        Marks a trade as failed to open if execution fails before any broker response.

        Args:
            trade_id: The trade ID to mark as failed
            reason: The reason for the failure
        """
        with self._trades_lock:
            if trade_id in self._all_trades:
                trade = self._all_trades[trade_id]
                if trade.get("lifecycle_state") == LifecycleState.OPEN_PENDING:
                    trade["lifecycle_state"] = LifecycleState.CLOSED
                    trade["end_time"] = time.time()
                    trade["comment"] = f"Failed to open: {reason}"
                    logger.warning(f"Trade {trade_id} marked as failed to open: {reason}")
                    
                    # Invoke closed callbacks
                    for callback in self._trade_closed_callbacks:
                        try:
                            callback(trade_id, trade["symbol"], "FAILED_OPEN", reason)
                        except Exception as e:
                            logger.error(f"Error in trade closed callback: {e}", exc_info=True)
                else:
                    logger.warning(f"Cannot mark trade {trade_id} as failed - not in OPEN_PENDING state")
            else:
                logger.error(f"Cannot mark trade {trade_id} as failed - trade not found")

    def update_trade_state(self,
                          trade_id: int,
                          new_state: Optional[LifecycleState] = None,
                          comment: Optional[str] = None,
                          add_fill: Optional[Dict[str, Any]] = None) -> None:
        """
        Updates specific attributes of a trade record.

        Args:
            trade_id: The trade ID to update
            new_state: Optional new lifecycle state
            comment: Optional comment to add
            add_fill: Optional fill record to append
        """
        with self._trades_lock:
            if trade_id not in self._all_trades:
                logger.error(f"Cannot update trade {trade_id} - trade not found")
                return

            trade = self._all_trades[trade_id]
            
            if new_state is not None:
                old_state = trade.get("lifecycle_state")
                trade["lifecycle_state"] = new_state
                logger.debug(f"Trade {trade_id} state updated: {old_state} -> {new_state}")
                
            if comment is not None:
                trade["comment"] = comment
                
            if add_fill is not None:
                trade["fills"].append(add_fill)
                self._recalculate_trade_metrics(trade_id)
                logger.debug(f"Fill added to trade {trade_id}: {add_fill}")

    def _recalculate_trade_metrics(self, trade_id: int) -> None:
        """
        Recalculates trade metrics (shares, avg_price, realized_pnl) based on fills.

        Args:
            trade_id: The trade ID to recalculate
        """
        if trade_id not in self._all_trades:
            return

        trade = self._all_trades[trade_id]
        fills = trade.get("fills", [])
        
        if not fills:
            return

        total_shares = 0.0
        total_cost = 0.0
        realized_pnl = 0.0

        for fill in fills:
            shares = fill.get("shares", 0.0)
            price = fill.get("price", 0.0)
            side = fill.get("side", "BUY")
            
            if side.upper() == "BUY":
                total_shares += shares
                total_cost += shares * price
            elif side.upper() == "SELL":
                # For sells, calculate realized P&L
                if total_shares > 0:
                    avg_cost = total_cost / total_shares if total_shares > 0 else 0.0
                    realized_pnl += shares * (price - avg_cost)
                    total_shares -= shares
                    total_cost -= shares * avg_cost

        trade["shares"] = total_shares
        trade["avg_price"] = total_cost / total_shares if total_shares > 0 else 0.0
        trade["realized_pnl"] = realized_pnl

        logger.debug(f"Trade {trade_id} metrics recalculated: shares={total_shares:.2f}, "
                    f"avg_price={trade['avg_price']:.4f}, realized_pnl={realized_pnl:.2f}")

    def get_trade_details(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """
        Gets details for a specific trade.

        Args:
            trade_id: The trade ID to retrieve

        Returns:
            Trade details dictionary or None if not found
        """
        with self._trades_lock:
            if trade_id in self._all_trades:
                return copy.deepcopy(self._all_trades[trade_id])
            return None

    def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """
        ✅ SME REQUIREMENT: Alias for get_trade_details for consistency with SME analysis.
        Gets details for a specific trade by its LCM integer t_id.

        Args:
            trade_id: The LCM integer t_id to retrieve

        Returns:
            Trade details dictionary or None if not found
        """
        return self.get_trade_details(trade_id)

    def handle_order_finalized(self,
                               local_id: int,
                               symbol: str,
                               event_type: str,
                               final_status: str,
                               parent_trade_id: Optional[int]) -> None:
        """
        Processes order finalization to update trade lifecycle state.
        Modified to use injected position_manager instead of parameter.

        Args:
            local_id: The local order ID
            symbol: The stock symbol
            event_type: The order event type (OPEN, ADD, REDUCE, CLOSE)
            final_status: The final order status (Filled, Rejected, Cancelled)
            parent_trade_id: The parent trade ID
        """
        if parent_trade_id is None or parent_trade_id <= 0:
            logger.error(f"LifecycleManager received handle_order_finalized for local_id={local_id} "
                        f"with invalid parent_trade_id={parent_trade_id}. Skipping.")
            return

        symbol_upper = symbol.upper()
        event_type_upper = event_type.upper()

        logger.info(f"LifecycleManager handling order finalized: local_id={local_id}, "
                   f"symbol={symbol_upper}, event_type={event_type_upper}, "
                   f"final_status={final_status}, parent_trade_id={parent_trade_id}")

        with self._trades_lock:
            if parent_trade_id not in self._all_trades:
                logger.error(f"LifecycleManager: Trade {parent_trade_id} not found for order {local_id}")
                return

            trade = self._all_trades[parent_trade_id]
            current_state = trade.get("lifecycle_state")

            # Handle failed OPEN orders
            if event_type_upper == "OPEN" and final_status in ("Rejected", "Cancelled"):
                logger.warning(f"LifecycleManager: OPEN order {local_id} failed with status {final_status}")

                # Use injected position_manager
                max_retries_reached = self._position_manager.handle_failed_open(
                    symbol_upper, f"OPEN order {local_id} {final_status}"
                )

                if max_retries_reached:
                    trade["lifecycle_state"] = LifecycleState.ERROR
                    trade["end_time"] = time.time()
                    trade["comment"] = f"OPEN failed after max retries: {final_status}"
                    logger.error(f"Trade {parent_trade_id} marked as ERROR after max OPEN retries")
                else:
                    trade["comment"] = f"OPEN order {final_status}, retries available"
                    logger.info(f"Trade {parent_trade_id} OPEN failed but retries available")
                return

            # Handle successful fills
            if final_status == "Filled":
                if event_type_upper == "OPEN":
                    trade["lifecycle_state"] = LifecycleState.OPEN_ACTIVE
                    logger.info(f"Trade {parent_trade_id} transitioned to OPEN_ACTIVE")

                    # Publish trade state change to Redis
                    self._publish_trade_lifecycle_event_to_redis("TRADE_OPENED", trade, {
                        "local_id": local_id,
                        "event_type": event_type_upper,
                        "final_status": final_status
                    })

                    # Invoke opened callbacks
                    for callback in self._trade_opened_callbacks:
                        try:
                            callback(parent_trade_id, symbol_upper, "OPENED")
                        except Exception as e:
                            logger.error(f"Error in trade opened callback: {e}", exc_info=True)

                elif event_type_upper in ["CLOSE", "REDUCE"]:
                    # Check if trade should be closed
                    self._check_and_close_trade(parent_trade_id, symbol_upper)

            # Log the event
            self.log_event({
                "event": f"ORDER_{final_status.upper()}",
                "symbol": symbol_upper,
                "event_type": event_type_upper,
                "local_id": local_id,
                "trade_id": parent_trade_id,
                "final_status": final_status
            })

    def _check_and_close_trade(self, trade_id: int, symbol: str) -> None:
        """
        Check if a trade should be closed based on position and order state.

        Args:
            trade_id: The trade ID to check
            symbol: The stock symbol
        """
        try:
            # Get current position data
            current_position_data = self._position_manager.get_position(symbol)
            current_shares = current_position_data.get('shares', 0.0) if current_position_data else 0.0

            # Get leftover shares from order repository
            leftover_shares = self._order_repository.compute_leftover_shares_for_trade(trade_id)

            logger.debug(f"Trade {trade_id} close check: position_shares={current_shares:.2f}, "
                        f"leftover_shares={leftover_shares:.2f}")

            # Trade is closed if both position and pending orders show no shares
            if abs(current_shares) < 1e-9 and abs(leftover_shares) < 1e-9:
                trade = self._all_trades[trade_id]
                trade["lifecycle_state"] = LifecycleState.CLOSED
                trade["end_time"] = time.time()
                trade["comment"] = "Trade closed - no remaining shares or pending orders"

                logger.info(f"Trade {trade_id} marked as CLOSED")

                # Publish trade closure event to Redis
                self._publish_trade_lifecycle_event_to_redis("TRADE_CLOSED", trade, {
                    "closure_reason": "Normal closure",
                    "current_shares": current_shares,
                    "leftover_shares": leftover_shares
                })

                # Update position manager
                try:
                    self._position_manager.set_position_closed(symbol, reason="trade_closed_by_lcm")
                    logger.debug(f"PositionManager updated for closed trade {trade_id}")
                except Exception as e:
                    logger.error(f"Error updating PositionManager for closed trade {trade_id}: {e}")

                # Invoke closed callbacks
                for callback in self._trade_closed_callbacks:
                    try:
                        callback(trade_id, symbol, "CLOSED", "Normal closure")
                    except Exception as e:
                        logger.error(f"Error in trade closed callback: {e}", exc_info=True)
            else:
                logger.debug(f"Trade {trade_id} remains active: shares or orders pending")

        except Exception as e:
            logger.error(f"Error checking trade closure for {trade_id}: {e}", exc_info=True)

    def get_ledger_snapshot(self) -> List[Dict[str, Any]]:
        """Gets a snapshot of the event ledger."""
        with self._trades_lock:
            return copy.deepcopy(self._ledger)

    def log_event(self, evt: dict) -> int:
        """Adds an event to the internal ledger."""
        if "timestamp" not in evt:
            evt["timestamp"] = time.time()
        with self._trades_lock:
            self._ledger.append(evt)
            ledger_index = len(self._ledger) - 1
            logger.debug(f"LifecycleManager logged event (idx={ledger_index}): "
                        f"{evt.get('event')} for {evt.get('symbol')}")
            return ledger_index

    def reset_all_trades(self) -> None:
        """Clears all trades, ledger, and callback lists."""
        with self._trades_lock:
            self._all_trades.clear()
            self._ledger.clear()
            self._last_trade_id = 0
            self._trade_opened_callbacks.clear()
            self._trade_closed_callbacks.clear()
            logger.info("All trades and ledger reset")

    def load_trade_from_dict(self, trade_dict: Dict[str, Any]) -> int:
        """Loads a trade into _all_trades from a dictionary."""
        with self._trades_lock:
            trade_id = trade_dict.get("trade_id")
            if trade_id is None:
                logger.error("Cannot load trade - no trade_id in dictionary")
                return -1

            self._all_trades[trade_id] = copy.deepcopy(trade_dict)
            if trade_id > self._last_trade_id:
                self._last_trade_id = trade_id

            logger.debug(f"Loaded trade {trade_id} from dictionary")
            return trade_id

    def create_or_sync_trade(self, symbol: str, shares: float, avg_price: float,
                           source: Optional[str] = "UNKNOWN_SYNC") -> Optional[int]:
        """
        Finds an active trade or creates/updates one to match a synced position.
        This will now generate a master_correlation_id for newly discovered trades.
        """
        symbol = symbol.upper()
        with self._trades_lock:
            # Check if an active trade already exists
            existing_trade_id = self.find_active_trade_for_symbol(symbol)

            if existing_trade_id:
                # Update existing trade
                trade = self._all_trades[existing_trade_id]
                trade["shares"] = shares
                trade["avg_price"] = avg_price
                trade["comment"] = f"Synced from {source}"
                logger.info(f"Updated existing trade {existing_trade_id} for {symbol}: "
                           f"shares={shares:.2f}, avg_price={avg_price:.4f}")
                return existing_trade_id
            else:
                # This is a position discovered at the broker that we didn't know about.
                # It needs its own master correlation ID to start its tracking lifecycle.
                new_master_corr_id = str(uuid.uuid4())
                
                trade_id = self.create_new_trade(
                    symbol,
                    origin=f"SYNC_{source}",
                    correlation_id=new_master_corr_id,  # Pass the newly generated UUID
                    initial_comment=f"Auto-created by sync from {source}"
                )
                trade = self._all_trades[trade_id]
                trade["shares"] = shares
                trade["avg_price"] = avg_price
                trade["lifecycle_state"] = LifecycleState.OPEN_ACTIVE if abs(shares) > 1e-9 else LifecycleState.CLOSED
                logger.info(f"Created new trade {trade_id} for synced position {symbol}: "
                           f"master_correlation_id={new_master_corr_id}")
                return trade_id

    def register_trade_opened_callback(self, callback: TradeOpenedCallback) -> None:
        """Registers a callback for trade opened events."""
        self._trade_opened_callbacks.append(callback)
        logger.debug("Trade opened callback registered")

    def register_trade_closed_callback(self, callback: TradeClosedCallback) -> None:
        """Registers a callback for trade closed events."""
        self._trade_closed_callbacks.append(callback)
        logger.debug("Trade closed callback registered")

    def unregister_trade_opened_callback(self, callback: TradeOpenedCallback) -> None:
        """Unregisters a callback for trade opened events."""
        try:
            self._trade_opened_callbacks.remove(callback)
            logger.debug("Trade opened callback unregistered")
        except ValueError:
            logger.warning("Attempted to unregister trade opened callback that was not registered")

    def unregister_trade_closed_callback(self, callback: TradeClosedCallback) -> None:
        """Unregisters a callback for trade closed events."""
        try:
            self._trade_closed_callbacks.remove(callback)
            logger.debug("Trade closed callback unregistered")
        except ValueError:
            logger.warning("Attempted to unregister trade closed callback that was not registered")

    def get_all_trades_snapshot(self) -> Dict[int, Dict[str, Any]]:
        """Gets a snapshot of all trade records."""
        with self._trades_lock:
            return copy.deepcopy(self._all_trades)

    def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """Gets a copy of a trade's data by its LCM integer ID."""
        with self._trades_lock:
            trade_data = self._all_trades.get(trade_id)
            return copy.deepcopy(trade_data) if trade_data else None
    
    def shutdown(self):
        """Shutdown the TradeLifecycleManagerService and clean up resources."""
        logger.info("TradeLifecycleManagerService shutdown initiated")
        
        # Stop the async IPC worker thread
        if hasattr(self, '_ipc_shutdown_event'):
            self._ipc_shutdown_event.set()
            if hasattr(self, '_ipc_worker_thread') and self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
                logger.info("Waiting for IPC worker thread to shutdown...")
                self._ipc_worker_thread.join(timeout=5.0)
                if self._ipc_worker_thread.is_alive():
                    logger.warning("IPC worker thread did not shutdown gracefully within timeout")
                else:
                    logger.info("IPC worker thread shutdown complete")
        
        # Clean up any remaining queued messages
        if hasattr(self, '_ipc_queue'):
            queue_size = self._ipc_queue.qsize()
            if queue_size > 0:
                logger.warning(f"Discarding {queue_size} queued IPC messages during shutdown")
                # Clear the queue
                while not self._ipc_queue.empty():
                    try:
                        self._ipc_queue.get_nowait()
                    except queue.Empty:
                        break
        
        logger.info("TradeLifecycleManagerService shutdown complete")
