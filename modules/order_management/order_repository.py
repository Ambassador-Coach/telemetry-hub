# modules/order_management/order_repository.py

import threading
import logging
import time
import copy
import csv
import os
import json
import random
import uuid
import queue
import pytz
from datetime import datetime
from collections import UserDict # For TrackedDict if used
from typing import Dict, Any, List, Optional, Callable, Set, Tuple

# Import performance tracking functions
try:
    # Adjust path if needed
    from utils.performance_tracker import create_timestamp_dict, add_timestamp, calculate_durations, add_to_stats, is_performance_tracking_enabled
except ImportError:
    # Define dummy functions if import fails that don't allocate memory when tracking is disabled
    def is_performance_tracking_enabled(): return False
    def create_timestamp_dict(): return None
    def add_timestamp(*args, **kwargs): return args[0] if args else None
    def calculate_durations(*args, **kwargs): return {}
    def add_to_stats(*args, **kwargs): pass
    print("WARNING: Could not import performance_tracker in OrderRepository.")

# Import core interfaces
from interfaces.core.services import IEventBus
from interfaces.broker.services import IBrokerService
from interfaces.core.services import IConfigService
from interfaces.logging.services import ICorrelationLogger

# Import the interface this class implements
from interfaces.order_management.services import IOrderRepository

# Import the position manager interface
from interfaces.trading.services import IPositionManager, ITradeLifecycleManager

# Import order data types
from data_models.order_data_types import (
    Order, FillRecord, StatusHistoryEntry,
    OrderEventType, TradeActionType, OrderOrigin
)
# Import core enums to maintain type consistency
from data_models import OrderSide, OrderLSAgentStatus, OrderStatus

# Import event classes for EventBus integration
from core.events import (
    BrokerConnectionEvent, BrokerConnectionEventData,
    OrderStatusUpdateEvent, OrderStatusUpdateData,
    OrderFilledEvent, OrderFilledEventData,
    OrderRejectedEvent, OrderRejectedData,
    BrokerErrorEvent, BrokerErrorData,
    BrokerRawMessageRelayEvent, BrokerRawMessageRelayEventData
)

# --- BEGIN MODIFICATION 2.1: Add CoreLogicManager import for type hinting ---
# This is conceptual as the actual import path might differ or be a forward reference
# from modules.core_logic.core_logic_manager import CoreLogicManager # Uncomment if available
# --- END MODIFICATION 2.1 ---

# Import TrackedDict or use standard dict
try:
    # If TrackedDict is in utils or elsewhere
    from utils.tracked_dict import TrackedDict # Adjust path as needed
    USE_TRACKED_DICT = True
except ImportError:
    TrackedDict = dict # Fallback to standard dict
    USE_TRACKED_DICT = False


# Setup logger for this module
logger = logging.getLogger(__name__)

# Optional: Define specific exception classes if needed
class OrderRepositoryError(Exception):
    pass

class OrderNotFoundError(OrderRepositoryError):
    pass


class OrderRepository(IOrderRepository):
    """
    Manages the state of local copy orders, linking to broker orders,
    processing status updates and fills, and providing access to order data.
    This class encapsulates the state previously managed by copy_order_manager globals.

    Now includes EventBus integration for robust event-driven architecture.
    """

    def __init__(self,
                 event_bus: IEventBus,
                 broker_service: IBrokerService,
                 config_service: IConfigService,
                 correlation_logger: ICorrelationLogger,
                 position_manager: Optional[IPositionManager] = None,
                 trade_lifecycle_manager: Optional[ITradeLifecycleManager] = None,
                 ipc_client: Optional[Any] = None):
        """
        Initializes the order repository state with EventBus integration.

        Args:
            event_bus: IEventBus instance for publishing and subscribing to events
            broker_service: IBroker instance for direct broker communication
            position_manager: Optional IPositionManager instance for position state management
                             (can be set later via set_position_manager method)
        """
        # Initialize logger for this instance
        self.logger = logging.getLogger(__name__)

        logger.info("Initializing OrderRepository with EventBus integration...")

        # --- Store Dependencies ---
        self._event_bus = event_bus  # FIX: Store as _event_bus to match usage in start() method
        self._broker = broker_service
        self._config_service = config_service
        self._correlation_logger = correlation_logger
        self._position_manager = position_manager
        self._trade_lifecycle_manager = trade_lifecycle_manager
        self._ipc_client = ipc_client  # Store IPC client for Redis publishing (legacy)

        if self._event_bus:
            logger.info("EventBus provided to OrderRepository")
        else:
            logger.error("No EventBus provided to OrderRepository - event integration will not work")
            raise ValueError("EventBus is required for OrderRepository")

        if self._broker:
            logger.info("Broker service provided to OrderRepository")
        else:
            logger.debug("No broker service provided to OrderRepository - cancellation functionality will be limited (will be injected via setter)")

        if self._position_manager:
            logger.info("Position manager provided to OrderRepository")
        else:
            logger.info("Position manager not provided to OrderRepository - will be set later")
            
        if self._ipc_client:
            logger.info("IPC client provided to OrderRepository for order decision publishing")
        else:
            logger.info("IPC client not provided to OrderRepository - order decision events will not be published to Redis")

        # --- Core State Variables (Previously Globals) ---
        self._lock = threading.RLock() # Use RLock for potential nested method calls
        self._calculated_pos_lock = threading.RLock() # Separate lock for calculated position operations

        # Use TrackedDict if available for debugging writes, otherwise use standard dict
        DictType = TrackedDict if USE_TRACKED_DICT else dict
        self._copy_orders: Dict[int, Order] = DictType()
        if USE_TRACKED_DICT:
            logger.critical("!!! OrderRepository USING TrackedDict for self._copy_orders !!!")

        # DELETED: ID mapping moved to OrderLinkingService
        self._pending_lmt_orders: Dict[int, Dict[str, Any]] = {} # Added from old copy_order_manager

        self._pending_ocr_timestamps: Dict[int, float] = {}
        self._next_local_id: int = 1  # Start all IDs from 1
        self._id_lock = threading.Lock()

        # Cached Account/Position Data (Received from Broker)
        self._account_data: Dict[str, Any] = {
            "open_pl": 0.0,
            "net_pl": 0.0, # This is total P&L
            "equity": 0.0,
            "buying_power": 0.0,
            "cash": 0.0,  # Initialize cash to 0.0
            "last_open_pl": None,
            "last_net_pl": None,
            "last_update_time": 0.0,
            "num_trades": 0,
            "win_rate": 0.0,
            "trade_history": [],
            "data_source": "repository_init_with_cash" # Indicate cash is initialized
        }
        self._symbol_stats: Dict[str, Dict[str, Any]] = {} # Keep if used

        # --- Position Caches ---
        self._calculated_net_position: Dict[str, float] = {}
        logger.info("Initialized _calculated_net_position cache (fill-driven).")

        # --- Position Sync Flag ---
        self._initial_position_sync_done: bool = False
        logger.info("Initial position sync flag set to False.")

        # --- Callbacks and Injected Dependencies ---
        self._trade_finalized_callback: Optional[Callable[[int, str, str, str, Optional[int]], None]] = None

        # --- Stale Order Cancellation State ---
        # REMOVED: Moved to StaleOrderService

        # --- Account Data Debug Counter ---
        self._account_data_debug_counter: int = 0

        # --- BEGIN MODIFICATION 2.2: Initialize _core_logic_manager_ref ---
        self._core_logic_manager_ref: Optional['CoreLogicManager'] = None # Use forward reference if CoreLogicManager not imported
        # --- END MODIFICATION 2.2 ---
        
        # --- Initialize async IPC publishing infrastructure ---
        self._ipc_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        self._ipc_failures = 0
        self._ipc_circuit_open = False
        self._ipc_circuit_threshold = 10  # Number of failures before opening circuit
        self._ipc_worker_thread = None  # Initialize to None, will be started in start() method

        # --- Subscribe to EventBus events ---
        # DEPRECATED: Event subscriptions moved to OrderEventProcessor
        # self._subscribe_to_events()

        logger.info("OrderRepository initialized with EventBus integration. Thread creation deferred to start() method.")

    
    def sync_with_broker_orders(self, broker_orders: List[Dict[str, Any]]) -> None:
        """
        Syncs broker orders into the repository during initialization.
        Creates Order objects for broker-originated orders that don't exist locally.
        
        Args:
            broker_orders: List of broker order dictionaries from get_open_orders()
        """
        with self._lock:
            logger.info(f"OR_SYNC_BROKER_ORDERS: Processing {len(broker_orders)} broker orders")
            
            for broker_order in broker_orders:
                try:
                    broker_order_id = broker_order.get('order_id')
                    if not broker_order_id:
                        logger.warning("OR_SYNC_BROKER_ORDERS: Skipping order without broker ID")
                        continue
                    
                    # Check if we already have this order
                    existing_order = self._find_order_by_broker_id(broker_order_id)
                    if existing_order:
                        logger.debug(f"OR_SYNC_BROKER_ORDERS: Order {broker_order_id} already exists as local_id {existing_order.local_id}")
                        continue
                    
                    # Create new Order object for broker-originated order
                    local_id = self._get_next_local_id()
                    
                    # Extract order details
                    symbol = broker_order.get('symbol', 'UNKNOWN')
                    side_str = broker_order.get('side', 'BUY').upper()
                    side = OrderSide.BUY if side_str == 'BUY' else OrderSide.SELL
                    quantity = float(broker_order.get('quantity', 0))
                    filled_qty = float(broker_order.get('filled_quantity', 0))
                    limit_price = broker_order.get('limit_price')
                    order_type = broker_order.get('order_type', 'LMT')
                    status_str = broker_order.get('status', 'UNKNOWN')
                    
                    # Map broker status to our status enum
                    status = self._map_broker_status_to_agent_status(status_str)
                    
                    # Create Order with broker origin
                    order = Order(
                        local_id=local_id,
                        symbol=symbol,
                        side=side,
                        event_type=OrderEventType.MANUAL_BROKER_ORDER,
                        requested_shares=quantity,
                        timestamp_requested=time.time(),
                        ls_order_id=broker_order_id,
                        origin=OrderOrigin.BROKER,
                        requested_lmt_price=limit_price,
                        order_type=order_type,
                        ls_status=status,
                        filled_quantity=filled_qty,
                        leftover_shares=quantity - filled_qty,
                        comment=f"Synced from broker during initialization"
                    )
                    
                    self._copy_orders[local_id] = order
                    logger.info(f"OR_SYNC_BROKER_ORDERS: Created order {local_id} for broker order {broker_order_id} ({symbol} {side_str} {quantity})")
                    
                except Exception as e:
                    logger.error(f"OR_SYNC_BROKER_ORDERS: Error processing broker order: {e}", exc_info=True)
            
            logger.info(f"OR_SYNC_BROKER_ORDERS: Sync complete. Repository now has {len(self._copy_orders)} orders")
    
    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="OrderRepository-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
        logger.info("Started async IPC publishing worker thread")
    
    def _ipc_worker_loop(self):
        """
        LATE WORKER ACTIVATION: Worker loop that waits for application_ready_event 
        before processing IPC messages to avoid interfering with broker handshake.
        """
        # Wait for the global "Go" signal before starting heavy IPC processing
        if hasattr(self, '_app_ready_event') and self._app_ready_event:
            logger.info("🚦 OrderRepository IPC Worker: Waiting for application_ready_event...")
            self._app_ready_event.wait()
            logger.info("✅ OrderRepository IPC Worker: Application ready signal received!")
        
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
                    logger.error(f"Async IPC: Error publishing: {e}")
                    if self._ipc_failures >= self._ipc_circuit_threshold:
                        self._ipc_circuit_open = True
                        logger.error(f"IPC circuit breaker opened after {self._ipc_failures} failures")
                    
            except Exception as e:
                logger.error(f"Error in IPC worker loop: {e}", exc_info=True)
                
        logger.info("IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str):
        """Queue an IPC message for async publishing via correlation logger."""
        # Use correlation logger:
        try:
            # Parse the message to extract event type
            import json
            msg_data = json.loads(redis_message)
            event_type = msg_data.get('metadata', {}).get('eventType', 'UNKNOWN')
            payload_data = msg_data.get('payload', {})
            
            # Use correlation logger
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name=event_type.replace('_', ''),  # Remove underscores for CamelCase
                    payload={
                        "target_stream": target_stream,
                        "causation_id": payload_data.get("__correlation_id") or payload_data.get("correlation_id") or payload_data.get("context", {}).get("local_order_id"),
                        **payload_data
                    },
                    stream_override=target_stream
                )
        except Exception as e:
            logger.error(f"Error logging with correlation logger: {e}")
            # Fallback to local queue if available
            try:
                self._ipc_queue.put_nowait((target_stream, redis_message))
            except queue.Full:
                logger.warning(f"Local IPC queue also full, dropping message for stream '{target_stream}'")
        else:
            # No central queue, use local queue
            try:
                self._ipc_queue.put_nowait((target_stream, redis_message))
            except queue.Full:
                logger.warning(f"IPC queue full, dropping message for stream '{target_stream}'")

    def _get_next_local_id(self) -> int:
        """
        Thread-safely generates the next sequential local order ID.
        This is the single source of truth for all new local IDs.
        """
        with self._id_lock:
            new_id = self._next_local_id
            self._next_local_id += 1
            return new_id

    def bootstrap_id_counter(self) -> None:
        """
        Initializes order ID counter based on highest local_id currently loaded.
        Should be called after all initialization syncs and order loads.
        """
        with self._lock:
            if not self._copy_orders:
                self._next_local_id = 1
                logger.info("OR_ID_BOOTSTRAP: No orders found. Counter starts at 1.")
                return
            
            max_id = max(self._copy_orders.keys()) if self._copy_orders else 0
            self._next_local_id = max_id + 1
            logger.info(f"OR_ID_BOOTSTRAP: Counter initialized. Next new order ID will be {self._next_local_id}.")

    def perform_initial_broker_order_sync(self) -> None:
        """
        Performs initial synchronization with broker orders at startup.
        
        This method:
        1. Retrieves all open orders from the broker service
        2. Syncs them into the local order repository
        3. Ensures OrderRepository has complete view of broker state
        
        Should be called after broker connection is established during startup.
        """
        try:
            if not self._broker:
                logger.warning("OR_INITIAL_SYNC: No broker service available for initial order sync")
                return
                
            logger.info("OR_INITIAL_SYNC: Starting initial broker order synchronization...")
            
            # Get all orders from broker
            if hasattr(self._broker, 'get_all_orders'):
                broker_orders = self._broker.get_all_orders(timeout=10.0)
                logger.info(f"OR_INITIAL_SYNC: Retrieved {len(broker_orders)} orders from broker")
                
                # Sync the orders into our repository
                if broker_orders:
                    self.sync_with_broker_orders(broker_orders)
                    logger.info(f"OR_INITIAL_SYNC: Synchronized {len(broker_orders)} broker orders into repository")
                else:
                    logger.info("OR_INITIAL_SYNC: No orders to sync from broker")
            else:
                logger.warning("OR_INITIAL_SYNC: Broker service does not support get_all_orders method")
                
        except Exception as e:
            logger.error(f"OR_INITIAL_SYNC: Error during initial broker order sync: {e}", exc_info=True)

    # --- EventBus Event Handler Methods ---
    # NOTE: All event handlers have been moved to OrderEventProcessor




    def _mark_orders_connection_lost(self):
        """Mark all open/pending orders with connection_lost status when broker disconnects."""
        try:
            with self._lock:
                connection_lost_count = 0
                for order_key, order in self._copy_orders.items():
                    # Check if order is in a final state using OrderLSAgentStatus enum
                    final_statuses = {OrderLSAgentStatus.FILLED, OrderLSAgentStatus.CANCELLED, OrderLSAgentStatus.REJECTED}
                    if order.ls_status not in final_statuses:
                        # Update order with connection lost status using immutable pattern
                        meta_updates = dict(order.meta) if order.meta else {}
                        meta_updates['connection_lost'] = True
                        meta_updates['connection_lost_timestamp'] = time.time()

                        updated_order = order.with_update(meta=meta_updates)
                        self._copy_orders[order_key] = updated_order
                        connection_lost_count += 1

                if connection_lost_count > 0:
                    logger.warning(f"Marked {connection_lost_count} orders as connection_lost due to broker disconnect")

        except Exception as e:
            logger.error(f"Error marking orders as connection_lost: {e}", exc_info=True)

    # --- Order Management Methods (Core Functionality) ---

    def create_order_request(self,
                           symbol: str,
                           side: str,
                           quantity: float,
                           order_type: str,
                           limit_price: Optional[float] = None,
                           stop_price: Optional[float] = None,
                           time_in_force: str = "DAY",
                           client_order_id: Optional[str] = None) -> int:
        """
        Create a new order request before sending to broker.

        Args:
            symbol: Stock symbol
            side: Order side ("BUY" or "SELL")
            quantity: Number of shares
            order_type: Order type ("MARKET", "LIMIT", "STOP", etc.)
            limit_price: Limit price for limit orders
            stop_price: Stop price for stop orders
            time_in_force: Time in force ("DAY", "GTC", etc.)
            client_order_id: Optional client-specified order ID

        Returns:
            The local order ID (int) for tracking
        """
        try:
            # Convert side string to OrderSide enum
            side_enum = OrderSide.UNKNOWN
            if side.upper() == "BUY":
                side_enum = OrderSide.BUY
            elif side.upper() == "SELL":
                side_enum = OrderSide.SELL

            # For now, use a generic event type - this could be enhanced to accept event_type parameter
            event_type_enum = OrderEventType.MANUAL  # Default for manual order requests

            req_time = time.time()

            with self._lock:
                # Generate new local ID
                new_local_id = self._get_next_local_id()

            # Initial status history entry
            initial_status_entry = StatusHistoryEntry(
                timestamp=req_time,
                status=OrderLSAgentStatus.PENDING_SUBMISSION,  # The broker-specific status
                generic_status=OrderStatus.PENDING_SUBMISSION  # The required generic status
            )

            # --- NEW: For create_order_request, assume manual trading ---
            client_id = "MANUAL_TRADER"
            tags = {
                "create_timestamp": time.time(),
                "create_source": "create_order_request",
                "session_id": str(uuid.uuid4())[:8]
            }

            # Create the immutable Order object with enums
            order_to_store = Order(
                local_id=new_local_id,
                version=0,
                symbol=symbol.upper(),
                side=side_enum,  # Using OrderSide enum
                event_type=event_type_enum,  # Using OrderEventType enum
                requested_shares=float(quantity),
                requested_lmt_price=float(limit_price) if limit_price is not None else None,
                order_type=order_type.upper(),
                requested_cost=0.0,  # Will be calculated if needed
                real_time_price_at_request=None,  # Could be populated from price provider
                trader_price_at_request=None,
                timestamp_requested=req_time,
                ls_status=OrderLSAgentStatus.PENDING_SUBMISSION,  # Using OrderLSAgentStatus enum
                fills=tuple(),
                filled_quantity=0.0,
                leftover_shares=float(quantity),
                status_history=(initial_status_entry,),
                perf_timestamps={},
                meta={"time_in_force": time_in_force.upper(), "client_order_id": client_order_id},
                
                # --- NEW: Professional order tracking ---
                client_id=client_id,
                tags=tags,
                origin=OrderOrigin.PYTHON  # Python-initiated order
            )

            with self._lock:
                self._copy_orders[new_local_id] = order_to_store

            # Log ALL IDs for complete tracing
            correlation_id = client_order_id or f"ORDER_{new_local_id}"
            self.logger.info(f"[ORDER_REPO] Creating order with ALL IDs - "
                           f"CorrID: {correlation_id}, "
                           f"LocalID: {new_local_id}, "
                           f"ClientOrderID: {client_order_id}, "
                           f"Symbol: {symbol}, "
                           f"Side: {side}, "
                           f"Qty: {quantity}, "
                           f"Type: {order_type}")

            # Add CRITICAL logging for OPEN orders
            if client_order_id and ("OPEN" in client_order_id.upper() or "OCR_SCALPING_OPEN" in client_order_id.upper()):
                self.logger.critical(f"ORDER_REPO_LIFECYCLE: OPEN Order Record Created. LocalOrderID={order_to_store.local_id}, "
                                   f"Symbol={order_to_store.symbol}, Side={order_to_store.side.value}, Qty={order_to_store.requested_shares}, "
                                   f"Type={order_to_store.order_type}, Status={order_to_store.ls_status.value}")

            logger.info(f"Created order request {new_local_id}: {symbol} {side_enum.value} {quantity} @ {limit_price or 'MARKET'} [Origin: {OrderOrigin.PYTHON.value}]")
            
            # Publish order creation decision event
            creation_context = {
                "local_order_id": new_local_id,
                "symbol": symbol.upper(),
                "side": side_enum.value,
                "quantity": float(quantity),
                "order_type": order_type.upper(),
                "limit_price": float(limit_price) if limit_price is not None else None,
                "stop_price": float(stop_price) if stop_price is not None else None,
                "time_in_force": time_in_force.upper(),
                "client_order_id": client_order_id,
                "order_origin": OrderOrigin.PYTHON.value,
                "client_id": client_id,
                "status": OrderLSAgentStatus.PENDING_SUBMISSION.value,
                "creation_timestamp": req_time,
                "creation_source": "create_order_request",
                "session_id": tags.get("session_id") if tags else None,
                "order_creation_method": "MANUAL_REQUEST"
            }
            # Order decision publishing removed - handled by telemetry service
            
            return new_local_id

        except Exception as e:
            logger.error(f"Error creating order request: {e}", exc_info=True)
            
            # Publish order creation failure event
            failure_context = {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "time_in_force": time_in_force,
                "client_order_id": client_order_id,
                "error_type": type(e).__name__,
                "error_message": str(e),
                "failure_timestamp": time.time(),
                "creation_source": "create_order_request",
                "failure_stage": "ORDER_OBJECT_CREATION"
            }
            # Order decision publishing removed - handled by telemetry service
            
            raise

    # --- Order Retrieval Methods ---

    def get_order_by_local_id(self, local_id: int) -> Optional["Order"]:
        """
        Get order by local order ID.

        Args:
            local_id: The local order ID (int)

        Returns:
            Order object or None if not found
        """
        with self._lock:
            return self._copy_orders.get(local_id)




    def get_account_summary_for_gui_bootstrap(self) -> Dict[str, Any]:
        """
        🚨 NEW METHOD: Get account summary data for GUI Bootstrap API.
        Returns account information from broker or cached state.

        Returns:
            Dictionary with account summary data for Bootstrap API
        """
        try:
            logger.info("OrderRepository: Preparing account summary for GUI Bootstrap API")

            # Initialize default account data
            account_data = {
                "account_value": 0.0,
                "buying_power": 0.0,
                "day_pnl": 0.0,
                "total_pnl": 0.0,
                "timestamp": time.time(),
                "broker_connected": False,
                "is_bootstrap": True
            }

            # Try to get live data from broker if available
            if hasattr(self, '_broker') and self._broker:
                try:
                    # Check if broker is connected
                    if hasattr(self._broker, 'is_connected') and self._broker.is_connected():
                        account_data["broker_connected"] = True

                        # Try to get account values from broker
                        if hasattr(self._broker, 'account_value'):
                            account_data["account_value"] = getattr(self._broker, 'account_value', 0.0)
                        if hasattr(self._broker, 'buying_power'):
                            account_data["buying_power"] = getattr(self._broker, 'buying_power', 0.0)
                        if hasattr(self._broker, 'day_pnl'):
                            account_data["day_pnl"] = getattr(self._broker, 'day_pnl', 0.0)
                        if hasattr(self._broker, 'total_pnl'):
                            account_data["total_pnl"] = getattr(self._broker, 'total_pnl', 0.0)

                        logger.info("OrderRepository: Retrieved live account data from broker")
                    else:
                        logger.warning("OrderRepository: Broker not connected - using default account data")

                except Exception as e:
                    logger.error(f"OrderRepository: Error accessing broker for account data: {e}")
                    # Continue with default values

            else:
                logger.warning("OrderRepository: BrokerService not available - using default account data")

            # Check for cached account summary if broker data not available
            if not account_data["broker_connected"]:
                cached_summary = getattr(self, '_account_summary_cache', None)
                if cached_summary and isinstance(cached_summary, dict):
                    logger.info("OrderRepository: Using cached account summary for bootstrap")
                    account_data.update({
                        "account_value": cached_summary.get('account_value', 0.0),
                        "buying_power": cached_summary.get('buying_power', 0.0),
                        "day_pnl": cached_summary.get('day_pnl', 0.0),
                        "cash_balance": cached_summary.get('cash_balance', 0.0),
                        "buying_power_used": cached_summary.get('buying_power_used', 0.0),
                        "margin_used": cached_summary.get('margin_used', 0.0),
                        "maintenance_margin": cached_summary.get('maintenance_margin', 0.0),
                        "data_source": "cached"
                    })

            # Calculate additional metrics from order data if needed
            try:
                with self._lock:
                    # Calculate day P&L from filled orders if broker data not available
                    if account_data["day_pnl"] == 0.0:
                        day_pnl = 0.0
                        for order in self._copy_orders.values():
                            if order.ls_status == OrderLSAgentStatus.FILLED:
                                # Simple P&L calculation (this could be enhanced)
                                for fill in order.fills:
                                    if hasattr(fill, 'realized_pnl'):
                                        day_pnl += getattr(fill, 'realized_pnl', 0.0)

                        if day_pnl != 0.0:
                            account_data["day_pnl"] = day_pnl
                            logger.info(f"OrderRepository: Calculated day P&L from orders: {day_pnl}")

            except Exception as e:
                logger.error(f"OrderRepository: Error calculating P&L from orders: {e}")

            logger.info(f"OrderRepository: Account summary prepared - connected: {account_data['broker_connected']}, "
                       f"account_value: {account_data['account_value']}, day_pnl: {account_data['day_pnl']}")

            return account_data

        except Exception as e:
            logger.error(f"Error in get_account_summary_for_gui_bootstrap: {e}", exc_info=True)
            # Return minimal error structure, no mock data
            return {
                "account_value": 0.0,
                "buying_power": 0.0,
                "day_pnl": 0.0,
                "total_pnl": 0.0,
                "timestamp": time.time(),
                "broker_connected": False,
                "is_bootstrap": True,
                "error": str(e)
            }


    # --- Dependency Injection Methods ---

    def set_position_manager(self, position_manager: IPositionManager):
        """Set the position manager for direct interaction if needed."""
        self._position_manager = position_manager
        logger.info("PositionManager set on OrderRepository")

    # --- Method Implementations Go Here ---

    # @log_function_call('order_repository') # Uncomment if decorator exists and is desired
    def create_copy_order(
        self,
        symbol: str,
        side: str,             # Will be converted to OrderSide enum
        event_type: str,       # Will be converted to OrderEventType enum
        requested_shares: float,
        req_time: float,       # This will be timestamp_requested
        parent_trade_id: Optional[int],
        requested_lmt_price: Optional[float],
        ocr_time: Optional[float] = None,
        comment: str = "",
        requested_cost: float = 0.0,
        timestamp_ocr_processed: Optional[float] = None,
        real_time_price: Optional[float] = None,    # Price at time of OCR/Signal
        trader_price: Optional[float] = None,       # Trader's perceived price
        ocr_confidence: Optional[float] = None,
        perf_timestamps: Optional[Dict[str, float]] = None,
        master_correlation_id: Optional[str] = None, # <<<< NEW PARAMETER >>>>
        action_type: Optional[str] = None,      # String, will be converted to TradeActionType enum
        order_type_str: str = "LMT",          # String for order_type, e.g., "LMT", "MKT"
        **kwargs # For any other fields to go into meta
    ) -> Optional[int]: # Returns local_id
        # Assuming self.logger is available
        logger.debug(f"ENTER create_copy_order for {symbol} {side} {event_type}")
        local_id_to_return = None
        new_local_id = None

        try:
            # --- Parameter Validation & Enum Conversion ---
            if not symbol or not side or not event_type:
                logger.error(f"Validation failed: symbol, side, or event_type missing.")
                
                # Publish order creation failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderCreationFailed",
                        payload={
                            "decision_type": "ORDER_CREATION_FAILED",
                            "decision_reason": "Missing required parameters",
                            "context": {
                                "failure_stage": "PARAMETER_VALIDATION",
                                "symbol": symbol or "MISSING",
                                "side": side or "MISSING", 
                                "event_type": event_type or "MISSING",
                                "requested_shares": requested_shares,
                                "validation_error": "symbol, side, or event_type missing"
                            },
                            "causation_id": master_correlation_id,
                            "__correlation_id": master_correlation_id
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                
                return None
            if not isinstance(requested_shares, (int, float)) or requested_shares <= 1e-9:
                logger.error(f"Validation failed: requested_shares ({requested_shares}) must be a positive number.")
                
                # Publish order creation failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderCreationFailed",
                        payload={
                            "decision_type": "ORDER_CREATION_FAILED",
                            "decision_reason": "Invalid requested_shares",
                            "context": {
                                "failure_stage": "PARAMETER_VALIDATION",
                                "symbol": symbol,
                                "side": side,
                                "event_type": event_type,
                                "requested_shares": requested_shares,
                                "validation_error": f"requested_shares ({requested_shares}) must be a positive number"
                            },
                            "causation_id": master_correlation_id,
                            "__correlation_id": master_correlation_id
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                
                return None

            try:
                order_side_enum = OrderSide(side.upper())
            except ValueError:
                logger.error(f"Invalid order side: '{side}'. Valid values are: {[s.value for s in OrderSide]}. Using OrderSide.UNKNOWN.")
                order_side_enum = OrderSide.UNKNOWN

            try:
                order_event_type_enum = OrderEventType(event_type.upper())
            except ValueError:
                logger.error(f"Invalid order event_type: '{event_type}'. Valid values are: {[et.value for et in OrderEventType]}. Using OrderEventType.UNKNOWN.")
                order_event_type_enum = OrderEventType.UNKNOWN

            trade_action_type_enum: Optional[TradeActionType] = None
            if action_type:
                try:
                    # Assuming TradeActionType values are uppercase strings matching enum member names
                    trade_action_type_enum = TradeActionType[action_type.upper()]
                except KeyError: # Use KeyError for missing enum member by name
                    logger.error(f"Invalid trade action_type string: '{action_type}'. Valid names are: {[tat.name for tat in TradeActionType]}. Setting to None.")
                except ValueError: # Catch if action_type was not a string to begin with for .upper()
                     logger.error(f"Invalid type for trade action_type: '{action_type}'. Setting to None.")


            current_order_type_str_processed = order_type_str.upper()
            if current_order_type_str_processed not in ["LMT", "MKT"]: # Add other valid types if you have them (e.g. "STP", "STP_LMT")
                logger.warning(f"Invalid order_type_str '{order_type_str}', defaulting to LMT.")
                current_order_type_str_processed = "LMT"

            if current_order_type_str_processed == "LMT" and (requested_lmt_price is None or not isinstance(requested_lmt_price, (int,float)) or requested_lmt_price <= 0):
                logger.error(f"LMT order for {symbol} requires a positive limit price, got {requested_lmt_price}.")
                
                # Publish order creation failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderCreationFailed",
                        payload={
                            "decision_type": "ORDER_CREATION_FAILED",
                            "decision_reason": "Invalid limit price for LMT order",
                            "context": {
                                "failure_stage": "PRICE_VALIDATION",
                                "symbol": symbol,
                                "side": side,
                                "event_type": event_type,
                                "requested_shares": requested_shares,
                                "order_type": current_order_type_str_processed,
                                "requested_lmt_price": requested_lmt_price,
                                "validation_error": f"LMT order requires positive limit price, got {requested_lmt_price}"
                            },
                            "causation_id": master_correlation_id,
                            "__correlation_id": master_correlation_id
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                
                return None

            if parent_trade_id is not None and not isinstance(parent_trade_id, int): # Allow parent_trade_id <= 0 if that's a valid sentinel
                logger.error(f"Invalid parent_trade_id type: {parent_trade_id} (must be int or None).")
                
                # Publish order creation failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderCreationFailed",
                        payload={
                            "decision_type": "ORDER_CREATION_FAILED",
                            "decision_reason": "Invalid parent_trade_id type",
                            "context": {
                                "failure_stage": "PARAMETER_VALIDATION",
                                "symbol": symbol,
                                "side": side,
                                "event_type": event_type,
                                "requested_shares": requested_shares,
                                "parent_trade_id": parent_trade_id,
                                "parent_trade_id_type": type(parent_trade_id).__name__,
                                "validation_error": f"parent_trade_id must be int or None, got {type(parent_trade_id).__name__}"
                            },
                            "causation_id": master_correlation_id,
                            "__correlation_id": master_correlation_id
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                
                return None


            with self._lock:
                new_local_id = self._get_next_local_id()

                # Store pending OCR timestamp (if any) under lock
                # This is for link_ls_order to pick up later.
                if ocr_time is not None:
                     self._pending_ocr_timestamps[new_local_id] = ocr_time

            # Initial status history entry
            initial_status_entry = StatusHistoryEntry(
                timestamp=req_time, # Time of request/creation
                status=OrderLSAgentStatus.PENDING_SUBMISSION,  # The broker-specific status
                generic_status=OrderStatus.PENDING_SUBMISSION  # The required generic status
            )

            # --- NEW: Derive client_id from event_type ---
            client_id = "TESTRADE"  # Default
            if event_type and isinstance(event_type, str):
                event_type_upper = event_type.upper()
                if "OCR" in event_type_upper:
                    client_id = "OCR_ALGO"
                elif "MANUAL" in event_type_upper:
                    client_id = "MANUAL_TRADER"
                elif "FORCE" in event_type_upper or "LIQUIDATE" in event_type_upper:
                    client_id = "RISK_MANAGER"
                elif "REPRICE" in event_type_upper:
                    client_id = "PRICE_ADJUSTER"
            
            # --- NEW: Build tags with useful metadata ---
            tags = {
                "create_timestamp": time.time(),
                "create_source": "create_copy_order",
                "session_id": str(uuid.uuid4())[:8]  # Short session identifier
            }
            
            # Add the master trader's info if this is an OCR order
            if client_id == "OCR_ALGO" and ocr_confidence is not None:
                tags["ocr_confidence"] = float(ocr_confidence)
                tags["master_trader"] = "PRIMARY"  # Tag the master's trades

            # Create the immutable Order object
            # All other fields not explicitly listed here but part of Order dataclass
            # will get their default values from the dataclass definition.
            order_to_store = Order(
                local_id=new_local_id,
                version=0, # Initial version is 0
                symbol=symbol.upper(),
                side=order_side_enum,
                event_type=order_event_type_enum,
                action_type=trade_action_type_enum, # From kwargs or None
                master_correlation_id=master_correlation_id, # <<<< ASSIGN NEW FIELD >>>>
                requested_shares=float(requested_shares), # Ensure float
                requested_lmt_price=float(requested_lmt_price) if requested_lmt_price is not None else None,
                order_type=current_order_type_str_processed, # "LMT" or "MKT"

                requested_cost=float(requested_cost),
                real_time_price_at_request=real_time_price,
                trader_price_at_request=trader_price,

                timestamp_requested=float(req_time),
                timestamp_ocr=float(ocr_time) if ocr_time is not None else None, # Storing ocr_time directly now
                timestamp_ocr_processed=float(timestamp_ocr_processed) if timestamp_ocr_processed is not None else None,

                ls_status=OrderLSAgentStatus.PENDING_SUBMISSION, # Default initial status
                fills=tuple(), # Empty tuple
                filled_quantity=0.0,
                leftover_shares=float(requested_shares), # Initially, leftover is full amount

                parent_trade_id=parent_trade_id,
                comment=comment,
                ocr_confidence=float(ocr_confidence) if ocr_confidence is not None else None,

                status_history=(initial_status_entry,), # Initial status in history
                perf_timestamps=perf_timestamps if perf_timestamps is not None else {},
                meta=kwargs, # Store any remaining **kwargs here
                
                # --- NEW: Professional order tracking ---
                client_id=client_id,
                tags=tags,
                origin=OrderOrigin.PYTHON  # Python-initiated order by default
            )

            with self._lock:
                self._copy_orders[new_local_id] = order_to_store

            local_id_to_return = new_local_id
            logger.info(f"Created Order object for local_id={new_local_id} with MasterCorrID: {master_correlation_id}: {order_to_store.symbol} {order_to_store.side.value} {order_to_store.event_type.value} [Origin: {OrderOrigin.PYTHON.value}]")
            
            # Publish successful order creation event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderCreatedSuccessfully",
                    payload={
                        "decision_type": "ORDER_CREATED_SUCCESSFULLY",
                        "decision_reason": "Order object created and stored in repository",
                        "context": {
                            "creation_stage": "ORDER_OBJECT_STORED",
                            "symbol": symbol,
                            "side": order_to_store.side.value,
                            "event_type": order_to_store.event_type.value,
                            "requested_shares": requested_shares,
                            "local_id": new_local_id,
                            "order_type": current_order_type_str_processed,
                            "requested_lmt_price": requested_lmt_price,
                            "client_id": client_id,
                            "origin": OrderOrigin.PYTHON.value
                        },
                        "causation_id": str(new_local_id),
                        "__correlation_id": master_correlation_id
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )

        except ValueError as ve: # Catch specific Enum conversion errors during instantiation
            logger.error(f"ValueError during create_copy_order for {symbol} {side} {event_type}: {ve}", exc_info=True)
            
            # Publish order creation failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderCreationFailed",
                    payload={
                        "decision_type": "ORDER_CREATION_FAILED",
                        "decision_reason": "ValueError during order object creation",
                        "context": {
                            "failure_stage": "ORDER_OBJECT_CREATION",
                            "symbol": symbol,
                            "side": side,
                            "event_type": event_type,
                            "requested_shares": requested_shares,
                            "local_id": new_local_id,
                            "error_type": "ValueError",
                            "error_message": str(ve)
                        },
                        "causation_id": master_correlation_id
                    },
                    correlation_id=master_correlation_id,
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            # If local_id was generated but object creation failed, cleanup pending OCR timestamp
            if new_local_id is not None and ocr_time is not None: # Use new_local_id if assigned
                with self._lock:
                    self._pending_ocr_timestamps.pop(new_local_id, None)
            return None
        except Exception as e:
            logger.exception(f"CRITICAL ERROR in create_copy_order for {symbol} {side} {event_type}: {e}")
            
            # Publish order creation failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderCreationFailed",
                    payload={
                        "decision_type": "ORDER_CREATION_FAILED",
                        "decision_reason": "Critical error during order creation",
                        "context": {
                            "failure_stage": "ORDER_OBJECT_CREATION",
                            "symbol": symbol,
                            "side": side,
                            "event_type": event_type,
                            "requested_shares": requested_shares,
                            "local_id": local_id_to_return or new_local_id,
                            "error_type": type(e).__name__,
                            "error_message": str(e)
                        },
                        "causation_id": master_correlation_id,
                        "__correlation_id": master_correlation_id
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            # Cleanup if error occurred after ID generation
            if local_id_to_return is not None and ocr_time is not None: # Use local_id_to_return for consistency
                with self._lock:
                    self._pending_ocr_timestamps.pop(local_id_to_return, None)
            return None # Ensure None is returned on error

        return local_id_to_return
    
    def create_broker_placeholder_order(self, broker_order_id: str, symbol: str, 
                                      side: OrderSide, quantity: float,
                                      price: Optional[float] = None,
                                      source: str = "broker") -> int:
        """
        Create a placeholder order for a broker-originated order.
        This is used when we receive order updates from the broker for orders we didn't create.
        
        Args:
            broker_order_id: The broker's order ID
            symbol: Stock symbol
            side: Order side (BUY/SELL)
            quantity: Order quantity
            price: Order price (if known)
            source: Source description
            
        Returns:
            The local order ID
        """
        new_local_id = self._get_next_local_id()
        
        # Create tags for tracking
        tags = {
            "create_timestamp": time.time(),
            "create_source": f"broker_placeholder_{source}",
            "broker_order_id": broker_order_id
        }
        
        # Create the placeholder order
        order_to_store = Order(
            local_id=new_local_id,
            version=0,
            symbol=symbol.upper(),
            side=side,
            event_type=OrderEventType.MANUAL_BROKER_ORDER,
            requested_shares=float(quantity),
            requested_lmt_price=float(price) if price is not None else None,
            order_type="LMT" if price is not None else "MKT",
            requested_cost=0.0,
            timestamp_requested=time.time(),
            ls_status=OrderLSAgentStatus.PENDING,
            fills=tuple(),
            filled_quantity=0.0,
            leftover_shares=float(quantity),
            status_history=tuple(),
            perf_timestamps={},
            meta={"broker_order_id": broker_order_id},
            
            # Professional order tracking
            client_id="BROKER",
            tags=tags,
            origin=OrderOrigin.BROKER  # Broker-originated order
        )
        
        with self._lock:
            self._copy_orders[new_local_id] = order_to_store
            # Link the broker order ID immediately
            if broker_order_id.isdigit():
                broker_id_int = int(broker_order_id)
                self._ls_to_local[broker_id_int] = new_local_id
                self._local_to_ls[new_local_id] = broker_id_int
        
        self.logger.info(f"Created BROKER placeholder order {new_local_id} for broker_order_id={broker_order_id}")
        return new_local_id
    
    def create_sync_order(self, symbol: str, side: OrderSide, quantity: float,
                         avg_price: float, source: str = "position_sync",
                         master_correlation_id: Optional[str] = None) -> int:
        """
        Create an order record during position synchronization.
        
        Args:
            symbol: Stock symbol
            side: Order side (BUY/SELL)
            quantity: Position quantity
            avg_price: Average price
            source: Sync source description
            master_correlation_id: Optional correlation ID from sync
            
        Returns:
            The local order ID
        """
        new_local_id = self._get_next_local_id()
        
        # Create tags for tracking
        tags = {
            "create_timestamp": time.time(),
            "create_source": f"sync_{source}",
            "sync_avg_price": avg_price
        }
        
        # Create the sync order
        order_to_store = Order(
            local_id=new_local_id,
            version=0,
            symbol=symbol.upper(),
            side=side,
            event_type=OrderEventType.MANUAL,  # Generic manual type for sync
            requested_shares=float(quantity),
            requested_lmt_price=float(avg_price),
            order_type="LMT",
            requested_cost=float(quantity) * float(avg_price),
            timestamp_requested=time.time(),
            ls_status=OrderLSAgentStatus.FILLED,  # Sync orders are already filled
            fills=tuple([FillRecord(
                shares_filled=float(quantity),
                fill_price=float(avg_price),
                fill_time=time.time(),
                fill_time_est_str=datetime.now(pytz.timezone('US/Eastern')).strftime("%H:%M:%S.%f")[:-3]
            )]),
            filled_quantity=float(quantity),
            leftover_shares=0.0,
            master_correlation_id=master_correlation_id,
            status_history=tuple(),
            perf_timestamps={},
            meta={"sync_source": source},
            
            # Professional order tracking
            client_id="SYNC",
            tags=tags,
            origin=OrderOrigin.SYNC  # Sync-originated order
        )
        
        with self._lock:
            self._copy_orders[new_local_id] = order_to_store
        
        self.logger.info(f"Created SYNC order {new_local_id} for {symbol} during {source}")
        return new_local_id

    def _infer_order_origin(self, order: Order) -> OrderOrigin:
        """
        Infer the origin of an order for backward compatibility.
        This is used for orders created before the origin field was added.
        
        Args:
            order: The order to check
            
        Returns:
            The inferred OrderOrigin
        """
        # Check if order already has origin field
        if hasattr(order, 'origin') and isinstance(order.origin, OrderOrigin):
            return order.origin
            
        # Check event type for broker orders
        if order.event_type == OrderEventType.MANUAL_BROKER_ORDER:
            return OrderOrigin.BROKER
            
        # Check tags for sync source
        if "sync" in order.tags.get("create_source", "").lower():
            return OrderOrigin.SYNC
            
        # Check meta for sync source
        if "sync_source" in order.meta:
            return OrderOrigin.SYNC
            
        # Default to Python origin
        return OrderOrigin.PYTHON
    
    def get_next_order_id(self) -> int:
        """
        Public method to get the next order ID.
        This is primarily for sync operations that need to know the ID before creating the order.
        
        Returns:
            The next available local order ID
        """
        return self._get_next_local_id()
    
    def validate_order_invariants(self, order: Order) -> bool:
        """
        Validate that order state is consistent with its origin.
        
        Args:
            order: The order to validate
            
        Returns:
            True if valid, False otherwise
        """
        origin = self._infer_order_origin(order)
        
        # Broker orders must have broker_order_id
        if origin == OrderOrigin.BROKER:
            if not order.ls_order_id and "broker_order_id" not in order.meta:
                self.logger.error(f"Order {order.local_id} has BROKER origin but no broker_order_id")
                return False
                
        # Sync orders should be in FILLED state
        if origin == OrderOrigin.SYNC:
            if order.ls_status != OrderLSAgentStatus.FILLED:
                self.logger.warning(f"Order {order.local_id} has SYNC origin but status is {order.ls_status.value}")
                
        # Python orders should have matching local_id and ephemeral_corr_id (if set)
        if origin == OrderOrigin.PYTHON:
            if order.ephemeral_corr_id and order.ephemeral_corr_id != order.local_id:
                self.logger.warning(f"Order {order.local_id} has PYTHON origin but ephemeral_corr_id={order.ephemeral_corr_id}")
                
        return True

    # Add other placeholder methods or start implementing them...
    # Remember to acquire self._lock appropriately in methods modifying shared state.

    # --- BEGIN MODIFICATION 2.4: Modify process_broker_message signature ---
    def process_broker_message(
        self,
        msg: Dict[str, Any],
        perf_timestamps: Optional[Dict[str, float]] = None
        # Remove: core_logic_manager: Optional['CoreLogicManager'] = None
    ) -> None:
    # --- END MODIFICATION 2.4 ---
        """
        Processes a message from the broker, updating order state as needed.
        This is the main entry point for broker messages and dispatches to specific handlers.

        Args:
            msg: The message from the broker
            perf_timestamps: Optional performance tracking timestamps
        """
        # Create or update performance timestamps only if tracking is enabled
        try:
            if is_performance_tracking_enabled():
                if perf_timestamps is None:
                    perf_timestamps = create_timestamp_dict()
                add_timestamp(perf_timestamps, 'order_repo.process_broker_message_start')
        except Exception as e:
            logger.warning(f"Error initializing performance tracking in process_broker_message: {e}")
            perf_timestamps = None

        logger.debug(f"ENTER process_broker_message msg={msg}")

        event_type = msg.get("event", "") # Get event, default to empty string
        status = msg.get("status", "") # Get status as well

        # Flag to track if we need to update the GUI after processing
        need_gui_update = False
        update_type = None

        try:
            # Also check status if event_type is empty
            if event_type == "order_status" or (not event_type and status and msg.get('ls_order_id') is not None):
                # If event is 'order_status' OR event is missing but status and ls_order_id exist
                logger.debug(f"Routing message with status '{status}' to order status handler.")
                self._handle_order_status(msg, perf_timestamps)
                # Order status updates are handled by _update_status method which schedules its own GUI updates
            elif event_type == "positions_update":
                self._handle_position_update(msg, perf_timestamps)
                # Set flag to update GUI after processing
                need_gui_update = True
                update_type = 'refresh_positions'
            elif event_type == "account_update":
                self._handle_account_update(msg)
                # Set flag to update GUI after processing
                need_gui_update = True
                update_type = 'refresh_account_info'
            elif event_type == "get_all_positions_response":
                # This response is primarily for the requester waiting on the Future.
                # We might also trigger our internal position update from it.
                logger.debug(f"Processing 'get_all_positions_response': {msg.get('req_id')}")
                self._handle_get_all_positions_response(msg, perf_timestamps) # Call helper for internal state

                # Fulfill the pending request Future
                rid = msg.get("req_id")
                if rid is not None:
                    # Need access to pop_pending_request
                    # Assuming it's imported correctly at the top
                    from utils.position_request_manager import pop_pending_request
                    fut = pop_pending_request(rid)
                    if fut:
                        fut.set_result(msg) # Set the raw message as the result
                        logger.debug(f"Fulfilled Future for get_all_positions req_id={rid}")
                    else:
                        logger.warning(f"Received get_all_positions_response for req_id={rid}, but no pending Future found.")

                # Set flag to update GUI after processing
                need_gui_update = True
                update_type = 'refresh_positions'
            # Handle other specific events if needed
            elif event_type == "ping_response":
                logger.debug("Ignoring ping_response in OrderRepository")
            elif event_type == "trading_status":
                logger.debug("Ignoring trading_status in OrderRepository (handled by BrokerBridge/TMS)")
            elif not event_type and not status: # Both are missing/empty
                logger.warning(f"OrderRepository received message with no event or status: {msg}")
            else: # Has an event type we don't specifically handle
                logger.warning(f"OrderRepository received unhandled event type: '{event_type}' msg: {msg}")

            # Schedule GUI update outside of any specific handler logic
            # This is now outside of any locks and after all processing is complete
            if need_gui_update and self._core_logic_manager_ref and update_type:
                try:
                    self._core_logic_manager_ref.schedule_gui_update(update_type)
                    logger.info(f"[OR on CLM Thread] Scheduled GUI update ({update_type}) after {event_type}.")
                except Exception as e:
                    logger.warning(f"[OR on CLM Thread] Error scheduling GUI update: {e}")
            elif need_gui_update and not self._core_logic_manager_ref:
                logger.debug(f"[OR on CLM Thread] CoreLogicManager ref not set. Cannot schedule GUI update after {event_type}.")

        except Exception as e:
            logger.exception(f"Error processing broker message (event: {event_type}): {e}")
        finally:
            try:
                # Only track performance if enabled and perf_timestamps exists
                if is_performance_tracking_enabled() and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'order_repo.process_broker_message_end')

                    # Log performance metrics occasionally
                    if random.random() < 0.1:  # 10% of the time
                        durations = calculate_durations(perf_timestamps)
                        add_to_stats(perf_timestamps)
                        logger.debug(f"Process Broker Message Perf: " +
                                   f"total={durations.get('total', 0)*1000:.2f}ms, " +
                                   f"processing={durations.get('order_repo.process_broker_message_start_to_order_repo.process_broker_message_end', 0)*1000:.2f}ms")
            except Exception as perf_e:
                logger.warning(f"Error calculating performance metrics in process_broker_message: {perf_e}")

        logger.debug("EXIT process_broker_message")



    # ... Add placeholders or implementations for ALL methods defined in IOrderRepository ...
    # Make sure to include the method signatures exactly as in the interface.

    def get_order_by_local_id(self, local_id: int) -> Optional["Order"]:
        """
        Gets an order by its local ID.

        Args:
            local_id: The local order ID

        Returns:
            The Order object, or None if not found
        """
        with self._lock:
            return self._copy_orders.get(local_id)

    def get_order_by_ls_id(self, ls_order_id: int) -> Optional["Order"]:
        """
        Gets an order by its Lightspeed ID.

        Args:
            ls_order_id: The Lightspeed order ID

        Returns:
            The Order object, or None if not found
        """
        with self._lock:
            local_id = self._ls_to_local.get(ls_order_id)
            if local_id:
                return self._copy_orders.get(local_id)
        return None

    def get_orders_for_trade(self, trade_id: int) -> List[Dict[str, Any]]:
        """DEPRECATED: Moved to OrderQueryService.
        
        Returns a list of all copy order dictionaries associated with a specific parent trade ID.
        Uses copy-on-read pattern to minimize lock duration.

        Args:
            trade_id: The parent trade ID to find orders for

        Returns:
            A list of deep copies of orders associated with the trade ID
        """
        logger.debug(f"ENTER get_orders_for_trade(trade_id={trade_id})")

        if trade_id is None or trade_id <= 0:
            logger.warning(f"get_orders_for_trade called with invalid trade_id: {trade_id}")
            return []

        # First, collect references to orders that match the trade_id
        matching_orders_refs = []

        # Acquire lock only to collect references
        with self._lock:
            for local_id, order_data in self._copy_orders.items():
                # FIXED: order_data is an Order object, not a dict - access attribute directly
                if order_data.parent_trade_id == trade_id:
                    # Store a tuple of (local_id, reference to order_data)
                    matching_orders_refs.append((local_id, order_data))

        # Process the references outside the lock
        orders_for_trade = []
        for local_id, order_data_ref in matching_orders_refs:
            try:
                # Convert Order object to dictionary as expected by interface
                order_dict = self._order_to_dict(order_data_ref)
                order_dict['local_id'] = local_id  # Ensure local_id is present
                orders_for_trade.append(order_dict)
            except Exception as e_copy:
                # Log if conversion fails for some reason
                logger.error(f"Error converting order {local_id} to dict for trade {trade_id}: {e_copy}")

        logger.debug(f"Found {len(orders_for_trade)} orders for trade_id={trade_id}.")

        # Sort by request time outside the lock, newest first
        # Handle potential None values in timestamp gracefully
        try:
            orders_for_trade.sort(key=lambda x: x.get("timestamp_requested", 0) or 0, reverse=True)
        except Exception as e_sort:
            logger.error(f"Error sorting orders for trade {trade_id}: {e_sort}")

        logger.debug(f"RETURN get_orders_for_trade({trade_id}) - returning {len(orders_for_trade)} orders.")
        return orders_for_trade


    def get_all_orders_snapshot(self) -> Dict[int, "Order"]:
        """
        Gets a snapshot of all orders using copy-on-read pattern.
        Returns a shallow copy of the internal dictionary; the Order objects
        themselves are immutable and are shared.

        Returns:
            A dictionary mapping local order IDs to Order objects
        """
        with self._lock:
            return dict(self._copy_orders)

    def get_leftover_shares(self, local_id: int) -> float:
        """DEPRECATED: Moved to OrderQueryService.
        
        Gets the number of unfilled shares for an order.

        Args:
            local_id: The local order ID

        Returns:
            The number of unfilled shares
        """
        with self._lock:
            order = self._copy_orders.get(local_id)
            if not order:
                logger.warning(f"get_leftover_shares: Order with local_id={local_id} not found")
                return 0.0

            # If the order is in a final state, there are no leftover shares
            final_statuses = {OrderLSAgentStatus.FILLED, OrderLSAgentStatus.CANCELLED, OrderLSAgentStatus.REJECTED}
            if order.ls_status in final_statuses:
                return 0.0

            # Return the leftover shares from the order data
            leftover = float(order.leftover_shares)
            logger.debug(f"get_leftover_shares: Order {local_id} has {leftover} leftover shares")
            return leftover




    def get_symbol_total_fills(self, symbol: str) -> float:
        """DEPRECATED: Moved to OrderQueryService.
        
        Gets the net filled position for a symbol based on the processed fills cache.

        Args:
            symbol: The stock symbol

        Returns:
            The total net filled shares (positive for long, negative for short) based on fills.
        """
        symbol_upper = symbol.upper()
        thread_name = threading.current_thread().name

        # Log before acquiring lock - outside the critical section
        logger.debug(f"[{thread_name}] Attempting lock for GET_FILLS: symbol={symbol_upper}")

        # --- CRITICAL SECTION: Keep as short as possible ---
        # Minimize time inside the lock - just get the value and release
        with self._lock:
            cached_value = self._calculated_net_position.get(symbol_upper, 0.0)
        # --- End of critical section ---

        # Log after releasing lock - outside the critical section
        logger.debug(f"[{thread_name}] [GET_FILLS] Cache read for {symbol_upper}: Retrieved {cached_value:.2f}")

        return cached_value

    def get_fill_calculated_position(self, symbol: str) -> float:
        """DEPRECATED: Moved to OrderQueryService.
        
        Returns the net position for a symbol calculated purely from processed fills
        within the OrderRepository. Positive for long, negative for short.
        """
        symbol_upper = symbol.upper()
        with self._lock: # Protects read of _calculated_net_position
            # Ensure a default of 0.0 is returned if the symbol is not in the cache
            position = self._calculated_net_position.get(symbol_upper, 0.0)
            logger.debug(f"[OrderRepo] get_fill_calculated_position for {symbol_upper}: returning {position:.2f}")
            return position

    def force_set_calculated_position(self, symbol: str, shares: float) -> None:
        """
        Forcibly sets the calculated position for a symbol to match the broker's position.
        This is used during reconciliation to ensure OrderRepository._calculated_net_position
        reflects the true broker position after a sync.

        Args:
            symbol: The stock symbol
            shares: The number of shares (positive for long, negative for short, 0 for no position)
        """
        symbol_upper = symbol.upper()
        logger.info(f"[OrderRepo] force_set_calculated_position called for {symbol_upper} with shares={shares:.2f}")

        try:
            with self._lock:  # Protects write to _calculated_net_position
                old_value = self._calculated_net_position.get(symbol_upper, 0.0)
                self._calculated_net_position[symbol_upper] = shares
                logger.info(f"[OrderRepo] Forced calculated position for {symbol_upper}: {old_value:.2f} -> {shares:.2f}")

                # If shares is 0 or very close to 0, consider removing the entry entirely
                if abs(shares) < 1e-9:
                    if symbol_upper in self._calculated_net_position:
                        del self._calculated_net_position[symbol_upper]
                        logger.info(f"[OrderRepo] Removed {symbol_upper} from calculated positions (zero shares)")

                # Log the current state of all calculated positions for debugging
                positions_str = ", ".join([f"{s}: {p:.2f}" for s, p in self._calculated_net_position.items()])
                logger.info(f"[OrderRepo] Current calculated positions after update: {positions_str or 'None'}")
        except Exception as e:
            logger.error(f"[OrderRepo] Error in force_set_calculated_position for {symbol_upper}: {e}", exc_info=True)

    def request_cancellation(self, local_id: int, reason: str = "Cancel Requested") -> Optional[int]:
        """
        Marks a local order as 'CancelRequested' if it's in an active state.
        Returns the associated ls_order_id if found and cancellation is requested,
        otherwise returns None. The actual broker cancellation must be sent separately.
        """
        logger.debug(f"Requesting cancellation for local_id={local_id}, reason='{reason}'")
        ls_order_id_to_cancel: Optional[int] = None
        order_updated = False

        with self._lock:
            current_order = self._copy_orders.get(local_id)

            if not current_order:
                logger.warning(f"request_cancellation: local_id={local_id} not found.")
                return None

            current_status = current_order.ls_status.value if hasattr(current_order, 'ls_status') else "Unknown"
            # Check if order is already in a final state
            if current_status in ("Filled", "Cancelled", "Rejected"):
                logger.debug(f"request_cancellation: Order {local_id} already in final state '{current_status}'. No action taken.")
                return None # Don't need to cancel

            # If not final, mark as CancelRequested and get the LS ID
            # Prepare changes for the new Order instance
            changes = {
                "ls_status": OrderLSAgentStatus.CANCEL_REQUESTED,
                "cancel_reason": reason,
                "cancel_request_time": time.time() # Add timestamp for request
            }

            # Create the new immutable order with the changes
            updated_order = current_order.with_update(**changes)

            # Store the updated order
            self._copy_orders[local_id] = updated_order

            ls_order_id_to_cancel = updated_order.ls_order_id
            order_updated = True

        if order_updated:
             logger.info(f"Marked local_id={local_id} as 'CancelRequested'. Associated ls_order_id={ls_order_id_to_cancel}.")
             # Consider logging an event here if you have an internal event system

        # Return the ls_order_id so the caller can send the actual cancel command
        return ls_order_id_to_cancel

    def update_order_status_locally(self, local_id: int, new_status: str, reason: Optional[str] = None) -> bool:
        """
        Updates the status of a local order copy directly, e.g., if a broker send fails.
        Does NOT invoke trade_finalized_callback as this is not a broker-confirmed status.

        Args:
            local_id: The local order ID to update
            new_status: The new status to set
            reason: Optional reason for the status update

        Returns:
            bool: True if the order was found and updated, False otherwise
        """
        with self._lock:
            current_order = self._copy_orders.get(local_id)
            if not current_order:
                logger.warning(f"update_order_status_locally: local_id={local_id} not found.")
                return False

            old_status = current_order.ls_status.value if hasattr(current_order, 'ls_status') else "Unknown"

            # Prepare changes for the new Order instance
            changes = {
                "ls_status": OrderLSAgentStatus(new_status)
            }

            if reason:
                # Update comment
                new_comment = f"{current_order.comment or ''} | Status Update: {reason}".strip()
                changes["comment"] = new_comment

                # Set rejection_reason if needed
                if new_status == "Rejected" or "Failed" in new_status: # Be more specific if needed
                    changes["rejection_reason"] = reason

            # Create the new immutable order with the changes
            updated_order = current_order.with_update(**changes)

            # Store the updated order
            self._copy_orders[local_id] = updated_order

            logger.info(f"[{current_order.symbol}] Order {local_id} status updated locally: {old_status} -> {new_status}. Reason: {reason}")
            return True

    def cancel_order(self, local_id: int, reason: str = "Cancel Requested") -> bool:
        """
        Cancels an order by its local ID. This method:
        1. Marks the order as 'CancelRequested' in the repository
        2. Gets the associated Lightspeed order ID
        3. Sends the cancellation request to the broker service

        Args:
            local_id: The local order ID to cancel
            reason: The reason for cancellation

        Returns:
            bool: True if cancellation request was sent successfully, False otherwise
        """
        logger.info(f"Cancel order request for local_id={local_id}, reason='{reason}'")

        # First mark the order as CancelRequested and get the LS order ID
        ls_order_id = self.request_cancellation(local_id, reason)

        if not ls_order_id:
            logger.warning(f"Cannot cancel order {local_id}: No associated Lightspeed order ID found or order already in final state")
            return False

        # Check if we have a broker service
        if not self._broker:
            logger.error(f"Cannot cancel order {local_id}: No broker service available")
            return False

        # Send the cancellation request to the broker
        try:
            logger.info(f"Sending cancellation request to broker for order {local_id} (LS ID: {ls_order_id})")
            success = self._broker.cancel_order(ls_order_id)

            if success:
                logger.info(f"Successfully sent cancel request for order {local_id} (LS ID: {ls_order_id})")
                return True
            else:
                logger.warning(f"Broker service reported failure sending cancel request for order {local_id} (LS ID: {ls_order_id})")
                return False

        except Exception as e:
            logger.error(f"Error sending cancellation request to broker for order {local_id} (LS ID: {ls_order_id}): {e}", exc_info=True)
            return False

    # --- Account / Position Data Methods ---
    def get_account_data(self) -> Dict[str, Any]:
        """
        Gets the current account data, including calculated metrics.

        Returns:
            A dictionary containing account data and calculated metrics
        """
        # --- BEGIN MODIFICATION: Change INFO to DEBUG for high-frequency logs ---
        logger.debug("!!! OrderRepository.get_account_data CALLED - CHECKING EQUITY !!!")
        with self._lock:
            # Start with a copy of the broker data
            combined_data = copy.deepcopy(self._account_data)
            logger.debug(f"OR.get_account_data: Initial combined_data (from self._account_data, first 250 chars): {str(combined_data)[:250]}")

            # If equity is zero (or not set by broker), try to calculate it
            # from cash and market value of positions.
            # Ensure 'equity' key exists before checking its value.
            original_equity = combined_data.get("equity") # Get original equity, could be None
            cash_available = "cash" in combined_data
            cash_value_from_combined_data = combined_data.get("cash", 0.0) if cash_available else "N/A (cash key missing)"

            logger.debug(f"OR.get_account_data: Equity check: Original Equity={original_equity}, Cash Available={cash_available}, Cash Value='{cash_value_from_combined_data}'")

            # Condition for attempting recalculation:
            # 1. Equity is present and is 0.0
            # OR
            # 2. Equity key is missing (original_equity is None)
            # AND
            # 3. Cash key is present in combined_data
            attempt_recalculation = False
            if original_equity == 0.0 and cash_available:
                attempt_recalculation = True
                logger.debug("OR.get_account_data: Condition met for equity recalc: Equity is 0.0 and cash is available.")
            elif original_equity is None and cash_available:
                attempt_recalculation = True
                logger.debug("OR.get_account_data: Condition met for equity recalc: Equity key was missing and cash is available.")
            else:
                logger.debug(f"OR.get_account_data: Condition NOT met for equity recalc. Original Equity: {original_equity}, Cash Available: {cash_available}")


            if attempt_recalculation:
                logger.debug("Original equity is zero or missing, and cash is available. Attempting to calculate equity from cash and broker positions.")

                # Ensure cash_value is float for calculation if it was available
                current_cash_for_calc = 0.0
                if cash_available:
                    try:
                        current_cash_for_calc = float(combined_data.get("cash", 0.0))
                    except (ValueError, TypeError):
                        logger.warning(f"Could not convert cash value '{combined_data.get('cash')}' to float for equity calculation. Using 0.0 for cash.")
                        current_cash_for_calc = 0.0

                calculated_equity = current_cash_for_calc
                market_value_sum = 0.0
                num_positions_for_mv = 0

                logger.debug(f"OR.get_account_data: Starting equity calculation with Cash = {calculated_equity:.2f}")
                logger.debug(f"OR.get_account_data: Current self._broker_positions: {self._broker_positions}")

                # Accessing self._broker_positions directly within the lock is safe here.
                for symbol, pos_data in self._broker_positions.items():
                    mv = pos_data.get("market_value") # Get market_value, could be None
                    logger.debug(f"OR.get_account_data: Processing position {symbol} for MV: data={pos_data}, mv_value={mv}")
                    if mv is not None:
                        try:
                            market_value_sum += float(mv)
                            num_positions_for_mv +=1
                            logger.debug(f"Added {float(mv):.2f} from {symbol} to market_value_sum. Current sum: {market_value_sum:.2f}")
                        except (ValueError, TypeError):
                            logger.warning(f"Could not convert market_value '{mv}' for symbol {symbol} to float. Skipping.")
                    else:
                        logger.debug(f"OR.get_account_data: Symbol {symbol} has no 'market_value' or it's None.")

                calculated_equity += market_value_sum
                # --- BEGIN MODIFICATION ---
                logger.debug( # CHANGE THIS LINE TO DEBUG
                    f"Equity calculation: Cash ({current_cash_for_calc:.2f}) + "
                    f"Market Value Sum ({market_value_sum:.2f} from {num_positions_for_mv} positions) = Calculated Equity ({calculated_equity:.2f})"
                )
                # --- END MODIFICATION ---

                should_overwrite_equity = False
                if original_equity == 0.0 and calculated_equity != 0.0:
                    should_overwrite_equity = True
                    logger.debug("OR.get_account_data: Overwrite: Original equity was 0.0, calculated is non-zero.")
                elif original_equity is None and calculated_equity != 0.0: # If original was missing, and we calculated non-zero
                    should_overwrite_equity = True
                    logger.debug("OR.get_account_data: Overwrite: Original equity was missing, calculated is non-zero.")

                if should_overwrite_equity:
                    combined_data["equity"] = calculated_equity
                    combined_data["data_source"] = "repository_calculated_equity"
                    logger.info(f"Overwrote equity. New equity: {calculated_equity:.2f}, data_source: {combined_data['data_source']}") # Keep INFO for actual change
                elif calculated_equity == 0.0 and (num_positions_for_mv > 0 or current_cash_for_calc != 0.0):
                    logger.debug(f"Calculated equity is zero ({calculated_equity:.2f}), and original equity was {original_equity}. Not overwriting equity value.")
                    if original_equity is None:
                        combined_data["equity"] = 0.0
                        combined_data["data_source"] = "repository_calculated_equity_is_zero"
                        logger.info(f"Original equity was missing. Set equity to calculated 0.0. Data source: {combined_data['data_source']}") # Keep INFO
                else:
                    logger.debug(f"Calculated equity is zero ({calculated_equity:.2f}) and no basis for calculation (no cash or MV). Original equity: {original_equity}. Not overwriting equity value.")
            else:
                logger.debug("OR.get_account_data: Equity recalculation not attempted based on initial checks.")

            # Count the number of trades
            processed_trade_ids = set()

            # Add timestamp for this calculation
            combined_data['calculation_time'] = time.time()

            # Calculate remaining buying power (buying_power - bp_in_use)
            buying_power = combined_data.get("buying_power", 0.0)
            bp_in_use = combined_data.get("bp_in_use", 0.0)
            remaining_bp = buying_power - bp_in_use
            combined_data["remaining_buying_power"] = remaining_bp
            logger.debug(f"OR.get_account_data: Calculated remaining_buying_power = {remaining_bp:.2f} (buying_power={buying_power:.2f} - bp_in_use={bp_in_use:.2f})")

            logger.debug(f"OR.get_account_data: Final combined_data before returning (first 250 chars): {str(combined_data)[:250]}")
        # --- END MODIFICATION ---

        # Calculate win rate outside the lock (it acquires its own lock)
        combined_data['win_rate'] = self._calculate_win_rate_internal()

        # Get trade history outside the lock (it acquires its own lock)
        combined_data['trade_history'] = self._get_trade_history_internal()

        return combined_data

    def get_current_account_summary_data_for_stream(self) -> Dict[str, Any]:
        """
        Fetches and formats the current account summary from internal state.
        The data structure MUST match what gui_backend.py's
        handle_enhanced_account_summary_message or handle_account_summary_message
        expects for the "account_summary_update" WebSocket message.
        """
        with self._lock: # Protect access to self._account_data
            # Create a deep copy to ensure no modification of the internal state outside this class
            # and to avoid issues if _account_data is modified by another thread during processing.
            current_summary_internal = copy.deepcopy(self._account_data)

        # Transform/ensure fields match what app.js's updateAccountSummaryUI expects
        # (via gui_backend.py's 'account_summary_update' WebSocket message).
        # Your gui_backend.py's handle_enhanced_account_summary_message looks for specific fields.
        # Your self._account_data is populated by self._handle_account_update from broker messages.
        # Let's map fields from self._account_data to what the gui_backend stream handler for enhanced summary expects.

        # Fields expected by gui_backend.py's handle_enhanced_account_summary_message:
        # "account_value", "buying_power", "buying_power_used", "day_pnl",
        # "cash_balance", "margin_used", "maintenance_margin", "timestamp"

        # Mapping from self._account_data keys (populated by C++ bridge messages) to desired output keys:
        formatted_summary = {
            "account_value": current_summary_internal.get("equity", 0.0),
            "buying_power": current_summary_internal.get("buying_power", 0.0),
            "buying_power_used": current_summary_internal.get("bp_in_use", 0.0), # From C++
            "day_pnl": current_summary_internal.get("open_pl", 0.0) + current_summary_internal.get("realized_pl", current_summary_internal.get("net_pl", 0.0) - current_summary_internal.get("open_pl", 0.0)),
            "cash_balance": current_summary_internal.get("cash", current_summary_internal.get("running_balance", 0.0)), # Prioritize 'cash', fallback to 'running_balance'
            "margin_used": current_summary_internal.get("margin_used", 0.0), # If C++ sends this
            "maintenance_margin": current_summary_internal.get("maintenance_margin", 0.0), # If C++ sends this
            "timestamp": current_summary_internal.get("last_update_time", current_summary_internal.get("timestamp", time.time())) # Prefer last_update_time
        }
        self.logger.debug(f"OrderRepository: Formatted account summary for stream: {formatted_summary}")
        return formatted_summary


    def get_all_open_orders_data_for_stream(self) -> List[Dict[str, Any]]:
        """
        Fetches all current open/working orders and formats them for stream publishing.
        The data structure MUST match what gui_backend.py's handler for 'orders_update'
        (plural, for a list of orders) or 'order_status_update' (singular) WebSocket messages expects.
        """
        open_orders_list_internal = self.get_open_orders() # This returns List[Order] (your dataclass)

        formatted_for_stream = []
        for order_obj in open_orders_list_internal:
            # Transform order_obj (your internal Order dataclass) to the dict structure
            # that gui_backend.py's handle_all_orders_command_response creates for the "orders_update" WebSocket message

            # Extract side and status, handling potential Enum or string values
            side_val = order_obj.side.value if hasattr(order_obj.side, 'value') else str(order_obj.side).upper()
            status_val = order_obj.ls_status.value if hasattr(order_obj.ls_status, 'value') else str(order_obj.ls_status).upper()

            # ✅ SME REQUIREMENT: Construct GUI Display Parent ID
            gui_display_parent_trade_id = None
            parent_lcm_tid = getattr(order_obj, 'parent_trade_id', None)  # This is the integer LCM t_id
            parent_master_correlation_id = None  # IntelliSense UUID of the PARENT trade

            if parent_lcm_tid and self._trade_lifecycle_manager:
                parent_trade_lcm_data = self._trade_lifecycle_manager.get_trade_by_id(parent_lcm_tid)
                if parent_trade_lcm_data:
                    parent_master_correlation_id = parent_trade_lcm_data.get("correlation_id")
                    # ✅ SME: Use IntelliSense UUID as primary GUI identifier
                    if parent_master_correlation_id:
                        gui_display_parent_trade_id = parent_master_correlation_id
                    else:  # Fallback to SYMBOL-LCM_TID for parent display
                        gui_display_parent_trade_id = f"{order_obj.symbol}-{parent_lcm_tid}"
                else:
                    self.logger.warning(f"OrderRepo: Could not find LCM trade data for parent_lcm_tid {parent_lcm_tid} of order {order_obj.local_id}")
                    gui_display_parent_trade_id = f"{order_obj.symbol}-{parent_lcm_tid}-LcmDataNotFound"  # Fallback
            elif parent_lcm_tid:
                self.logger.warning(f"OrderRepo: TradeLifecycleManager not available for order {order_obj.local_id}. Cannot fetch parent's master_correlation_id.")
                gui_display_parent_trade_id = f"{order_obj.symbol}-{parent_lcm_tid}"  # Fallback

            formatted_order = {
                "order_id": str(order_obj.local_id), # GUI likely uses local_id as primary display ID
                "local_order_id": str(order_obj.local_id),
                "broker_order_id": str(order_obj.ls_order_id) if order_obj.ls_order_id is not None else None,
                "symbol": order_obj.symbol,
                "side": side_val,
                "quantity": order_obj.requested_shares, # Original requested quantity
                "price": order_obj.requested_lmt_price if hasattr(order_obj, 'requested_lmt_price') and order_obj.requested_lmt_price else 0.0,
                "status": status_val,
                "order_type": getattr(order_obj, 'order_type', 'LMT'),
                "timestamp": getattr(order_obj, 'timestamp_requested', time.time()) * 1000, # Convert to ms for JS
                # Fields expected by gui_backend.py's handle_order_status_message's payload for "order_status_update":
                "filled_quantity": order_obj.filled_quantity,
                "remaining_quantity": order_obj.leftover_shares,
                "message": order_obj.comment or "",

                # ✅ SME REQUIREMENT: Dual ID system for GUI hierarchy
                "parent_trade_id": gui_display_parent_trade_id,       # <<< GUI uses this to link to parent card (IntelliSense UUID)
                "lcm_parent_trade_id": parent_lcm_tid,                # Raw LCM integer t_id of parent
                "order_master_correlation_id": getattr(order_obj, 'master_correlation_id', None),  # IntelliSense UUID of this order's flow
                "event_type": order_obj.event_type.value if hasattr(order_obj, 'event_type') and order_obj.event_type and hasattr(order_obj.event_type, 'value') else None
            }
            formatted_for_stream.append(formatted_order)
        self.logger.debug(f"OrderRepository: Formatted {len(formatted_for_stream)} open orders for stream.")
        return formatted_for_stream

    def publish_all_open_orders_to_stream(self, correlation_id: str):
        """
        Publishes all current open orders to Redis stream for GUI bootstrap using EventBus pattern.
        """
        self.logger.info(f"OrderRepository: Publishing all open orders for bootstrap (CorrID: {correlation_id}).")
        all_open_orders_data_list = self.get_all_open_orders_data_for_stream()

        # The payload structure for an "orders_update" WebSocket message (plural for list)
        # using EventBus pattern instead of command response pattern
        payload_for_stream = {
            "orders": all_open_orders_data_list,
            "total_orders": len(all_open_orders_data_list),
            "is_bootstrap": True,
            "bootstrap_correlation_id": correlation_id,
            "event_source": "OrderRepository_Bootstrap"
        }

        # Use EventBus pattern - publish to a dedicated stream for order batch updates
        # This will be consumed by gui_backend.py's order batch update handler
        # Use CorrelationLogger to publish order batch updates
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderRepository",
                event_name="OrderBatchUpdate", 
                payload={
                    "orders": all_open_orders_data_list,
                    "total_orders": len(all_open_orders_data_list),
                    "is_bootstrap": True,
                    "bootstrap_correlation_id": correlation_id,
                    "event_source": "OrderRepository_Bootstrap",
                    "target_stream": self._config_service.get_redis_stream_name("order_batch_updates"),
                    "causation_id": correlation_id
                },
                stream_override=self._config_service.get_redis_stream_name("order_batch_updates")
            )

        # CorrelationLogger handles the publishing
        self.logger.info(f"OrderRepository: Logged {len(all_open_orders_data_list)} open orders for bootstrap (CorrID: {correlation_id}).")

    def get_broker_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Gets a broker position for a symbol using copy-on-read pattern.

        Args:
            symbol: The stock symbol

        Returns:
            A deep copy of the broker position, or None if not found
        """
        # Get a reference to the position while holding the lock
        pos_ref = None
        with self._lock:
            symbol_upper = symbol.upper()
            if symbol_upper in self._broker_positions:
                pos_ref = self._broker_positions[symbol_upper]

        # Make the deep copy outside the lock
        if pos_ref is not None:
            return copy.deepcopy(pos_ref)

        return None

    def get_all_broker_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Gets all broker positions using copy-on-read pattern.

        Returns:
            A deep copy of all broker positions
        """
        # Get a reference to the positions dictionary while holding the lock
        positions_ref = None
        with self._lock:
            positions_ref = self._broker_positions

        # Make the deep copy outside the lock
        if positions_ref is not None:
            return copy.deepcopy(positions_ref)

        return {}

    def get_symbol_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns a copy of the symbol stats dictionary using copy-on-read pattern.

        Returns:
            Dict[str, Dict[str, Any]]: A dictionary of symbol stats
        """
        # Get a reference to the symbol stats dictionary while holding the lock
        stats_ref = None
        with self._lock:
            stats_ref = self._symbol_stats

        # Make the deep copy outside the lock
        if stats_ref is not None:
            return copy.deepcopy(stats_ref)

        return {}

    # --- Callbacks / Injection ---
    def set_trade_finalized_callback(self, callback_func: Callable[[int, str, str, str, Optional[int]], None]) -> None:
         with self._lock: # Lock needed? Callback likely set only once at init. Let's lock anyway.
             self._trade_finalized_callback = callback_func
             logger.info("Trade finalized callback set in OrderRepository.")

    def set_position_manager(self, position_manager: IPositionManager) -> None:
        """
        Sets the position manager instance.
        This method provides setter injection for the position manager dependency.

        Args:
            position_manager: The position manager instance
        """
        with self._lock:
            self._position_manager = position_manager
            logger.info("PositionManager instance set in OrderRepository via setter injection.")

    def set_broker_service(self, broker_service: IBrokerService) -> None:
        """
        Injects the broker service post-initialization to break a circular dependency.

        Args:
            broker_service: The broker service instance
        """
        with self._lock:
            self._broker = broker_service
            logger.info("BrokerService dependency injected into OrderRepository successfully.")

    # --- BEGIN MODIFICATION 2.3: Add set_core_logic_manager method ---
    def set_core_logic_manager(self, clm_ref: 'CoreLogicManager') -> None: # Type hint
        """
        Sets the reference to the CoreLogicManager instance.
        This allows the service to schedule GUI updates.

        Args:
            clm_ref: The CoreLogicManager instance
        """
        with self._lock:
            self._core_logic_manager_ref = clm_ref
            logger.info(f"OrderRepository: CoreLogicManager reference set (ID: {id(clm_ref) if clm_ref else 'None'}).")
    # --- END MODIFICATION 2.3 ---

    # --- Export ---
    def export_orders_to_csv(self, filepath: str) -> None:
        """
        Exports all orders to a CSV file.

        Args:
            filepath: The path to the CSV file
        """
        import json
        logger.info(f"Exporting orders to {filepath}")

        # 1) Define the core fields in the order we want them to appear
        core_fields = [
            "local_id",
            "parent_trade_id",
            "symbol",
            "side",
            "event_type",
            "action_type",
            "requested_shares",
            "requested_cost",
            "timestamp_ocr",
            "timestamp_ocr_processed",
            "requested_lmt_price",
            "real_time_price",
            "trader_price",
            "ls_order_id",
            "ls_status",
            "leftover_shares",
            "final_fill_time",
            "comment",
            "fills",
            "version",
            "order_type",
            "filled_quantity",
            "rejection_reason",
            "cancel_reason",
            "ocr_confidence",
        ]

        # Make a copy of the orders to process outside the lock
        orders_copy = {}
        with self._lock:
            logger.debug(f"Exporting {len(self._copy_orders)} orders from repository")
            orders_copy = copy.deepcopy(self._copy_orders)

        # Log some debug info
        logger.debug(f"Exporting {len(orders_copy)} orders from snapshot")
        if orders_copy:
            first_few_keys = list(orders_copy.keys())[:5]
            logger.debug(f"First few keys in snapshot: {first_few_keys}")

        # Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Write the CSV file
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=core_fields)
            writer.writeheader()
            logger.debug("Starting to write rows to CSV...")

            for lid, order_obj in orders_copy.items():
                row = {"local_id": lid}

                # Process each field from the Order object
                for col in core_fields:
                    if col == "local_id":
                        continue  # Already handled
                    elif col == "fills":
                        # Convert FillRecord tuples to a JSON-serializable list of dictionaries
                        if hasattr(order_obj, 'fills') and order_obj.fills:
                            fill_dicts = []
                            for fill_record in order_obj.fills:
                                fill_dict = {}
                                # Extract attributes from FillRecord
                                if hasattr(fill_record, 'shares_filled'):
                                    fill_dict['shares_filled'] = fill_record.shares_filled
                                if hasattr(fill_record, 'fill_price'):
                                    fill_dict['fill_price'] = fill_record.fill_price
                                if hasattr(fill_record, 'fill_time'):
                                    fill_dict['fill_time'] = fill_record.fill_time
                                if hasattr(fill_record, 'commission'):
                                    fill_dict['commission'] = fill_record.commission
                                if hasattr(fill_record, 'side') and fill_record.side:
                                    # Handle enum values
                                    if hasattr(fill_record.side, 'value'):
                                        fill_dict['side'] = fill_record.side.value
                                    else:
                                        fill_dict['side'] = str(fill_record.side)
                                if hasattr(fill_record, 'liquidity'):
                                    fill_dict['liquidity'] = fill_record.liquidity
                                if hasattr(fill_record, 'fill_id_broker'):
                                    fill_dict['fill_id_broker'] = fill_record.fill_id_broker
                                if hasattr(fill_record, 'fill_time_est_str'):
                                    fill_dict['fill_time_est_str'] = fill_record.fill_time_est_str
                                if hasattr(fill_record, 'meta'):
                                    fill_dict['meta'] = fill_record.meta
                                fill_dicts.append(fill_dict)
                            row["fills"] = json.dumps(fill_dicts)
                        else:
                            row["fills"] = "[]"
                    elif col == "side" or col == "event_type" or col == "action_type" or col == "ls_status":
                        # Handle enum values
                        if hasattr(order_obj, col):
                            attr_value = getattr(order_obj, col)
                            if attr_value is not None and hasattr(attr_value, 'value'):
                                row[col] = attr_value.value
                            else:
                                row[col] = str(attr_value) if attr_value is not None else ""
                        else:
                            row[col] = ""
                    else:
                        # Handle regular attributes
                        if hasattr(order_obj, col):
                            attr_value = getattr(order_obj, col)
                            row[col] = attr_value if attr_value is not None else ""
                        else:
                            row[col] = ""

                writer.writerow(row)

        logger.info(f"Export complete: {len(orders_copy)} orders exported to {filepath}")

    def export_account_summary_to_csv(self, filepath: str) -> None:
        """
        Exports account summary data to a CSV file.

        Args:
            filepath: The path to the CSV file
        """
        logger.info(f"Exporting account data to {filepath}")

        # Make a copy of the account data to process outside the lock
        account_data_copy = {}
        with self._lock:
            account_data_copy = copy.deepcopy(self._account_data)

        # Create the directory if it doesn't exist
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        # Write the CSV file
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)

            # Write header row with all fields
            header = list(account_data_copy.keys())
            w.writerow(header)

            # Write values row
            values = [account_data_copy.get(field, "") for field in header]
            w.writerow(values)

        logger.info(f"Account data export complete: {filepath}")

    # --- Repricing ---
    def reprice_buy_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        """
        Reprices a buy order.

        Args:
            symbol: The stock symbol
            original_local_id: The local ID of the original order
            new_limit_price: The new limit price

        Returns:
            The local ID of the new order, or None if repricing failed
        """
        logger.info(f"Reprice BUY request: symbol={symbol}, original_local_id={original_local_id}, new_price={new_limit_price:.4f}")
        parent_trade_id: Optional[int] = None
        original_order_data: Optional[Dict[str, Any]] = None

        # 1. Find parent trade ID from the original order
        with self._lock:
            original_order = self._copy_orders.get(original_local_id)
            if original_order:
                parent_trade_id = original_order.parent_trade_id
                # Copy relevant metadata if needed
                original_req_shares = original_order.requested_shares
                original_event_type = original_order.event_type.value if original_order.event_type else "UNKNOWN"
                original_order_data = {
                    "timestamp_ocr": getattr(original_order, "timestamp_ocr", None),
                    "timestamp_ocr_processed": getattr(original_order, "timestamp_ocr_processed", None),
                    "ocr_confidence": getattr(original_order, "ocr_confidence", None),
                    "original_price": getattr(original_order, "requested_lmt_price", None),
                    "status": getattr(original_order, "status", None)
                }
            else:
                logger.error(f"Reprice BUY failed: Cannot find original order data for local_id={original_local_id}")
                # Publish order modification failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderRepriceFailed",
                        payload={
                            "decision_type": "ORDER_REPRICE_FAILED",
                            "decision_reason": "Original order not found in repository",
                            "context": {
                                "symbol": symbol,
                                "original_local_id": original_local_id,
                                "new_limit_price": new_limit_price,
                                "side": "BUY",
                                "failure_reason": "ORIGINAL_ORDER_NOT_FOUND",
                                "reprice_timestamp": time.time()
                            },
                            "causation_id": str(original_local_id)
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                return None

        if parent_trade_id is None:
            logger.error(f"Reprice BUY failed: Original order {original_local_id} has no parent_trade_id.")
            # Publish order modification failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceFailed",
                    payload={
                        "decision_type": "ORDER_REPRICE_FAILED",
                        "decision_reason": "Original order missing parent trade ID",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_limit_price": new_limit_price,
                            "side": "BUY",
                            "failure_reason": "MISSING_PARENT_TRADE_ID",
                            "original_order_data": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(original_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            return None

        # 2. Create a NEW copy order for the reprice
        try:
            # Use a distinct event_type if desired, e.g., "REPRICE_BUY"
            # Use original requested shares
            new_local_id = self.create_copy_order(
                symbol=symbol,
                side="BUY",
                event_type=f"REPRICE_{original_event_type}", # e.g., REPRICE_OPEN, REPRICE_ADD
                requested_shares=original_req_shares,
                req_time=time.time(),
                parent_trade_id=parent_trade_id, # Link to the SAME parent trade
                requested_lmt_price=new_limit_price,
                comment=f"Reprice of local_id={original_local_id}",
                # Copy other relevant fields if necessary (ocr_time, confidence, etc.)
                ocr_time=original_order_data.get("timestamp_ocr"),
                timestamp_ocr_processed=original_order_data.get("timestamp_ocr_processed"),
                ocr_confidence=original_order_data.get("ocr_confidence")
            )
            logger.info(f"Reprice BUY successful: Created new order local_id={new_local_id} for original {original_local_id} (parent_trade={parent_trade_id})")
            
            # Publish successful order repricing event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceSuccess",
                    payload={
                        "decision_type": "ORDER_REPRICE_SUCCESS",
                        "decision_reason": "Successfully created repriced buy order",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_local_id": new_local_id,
                            "original_price": original_order_data.get("original_price"),
                            "new_price": new_limit_price,
                            "price_change": new_limit_price - (original_order_data.get("original_price") or 0),
                            "side": "BUY",
                            "event_type": f"REPRICE_{original_event_type}",
                            "requested_shares": original_req_shares,
                            "parent_trade_id": parent_trade_id,
                            "original_order_metadata": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(new_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            return new_local_id
        except Exception as e:
            logger.error(f"Reprice BUY failed: Error creating new copy order for original {original_local_id}: {e}", exc_info=True)
            
            # Publish order repricing failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceFailed",
                    payload={
                        "decision_type": "ORDER_REPRICE_FAILED",
                        "decision_reason": f"Exception during repriced order creation: {str(e)}",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_limit_price": new_limit_price,
                            "side": "BUY",
                            "failure_reason": "CREATION_EXCEPTION",
                            "exception_type": type(e).__name__,
                            "exception_message": str(e),
                            "parent_trade_id": parent_trade_id,
                            "original_order_data": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(original_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            return None

    def reprice_sell_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        """
        Reprices a sell order.

        Args:
            symbol: The stock symbol
            original_local_id: The local ID of the original order
            new_limit_price: The new limit price

        Returns:
            The local ID of the new order, or None if repricing failed
        """
        logger.info(f"Reprice SELL request: symbol={symbol}, original_local_id={original_local_id}, new_price={new_limit_price:.4f}")
        parent_trade_id: Optional[int] = None
        original_order_data: Optional[Dict[str, Any]] = None

        # 1. Find parent trade ID from the original order
        with self._lock:
            original_order = self._copy_orders.get(original_local_id)
            if original_order:
                parent_trade_id = original_order.parent_trade_id
                original_req_shares = original_order.requested_shares
                original_event_type = original_order.event_type.value if original_order.event_type else "UNKNOWN"
                original_order_data = {
                    "timestamp_ocr": getattr(original_order, "timestamp_ocr", None),
                    "timestamp_ocr_processed": getattr(original_order, "timestamp_ocr_processed", None),
                    "ocr_confidence": getattr(original_order, "ocr_confidence", None),
                    "original_price": getattr(original_order, "requested_lmt_price", None),
                    "status": getattr(original_order, "status", None)
                }
            else:
                logger.error(f"Reprice SELL failed: Cannot find original order data for local_id={original_local_id}")
                # Publish order modification failure event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OrderRepository",
                        event_name="OrderRepriceFailed",
                        payload={
                            "decision_type": "ORDER_REPRICE_FAILED",
                            "decision_reason": "Original order not found in repository",
                            "context": {
                                "symbol": symbol,
                                "original_local_id": original_local_id,
                                "new_limit_price": new_limit_price,
                                "side": "SELL",
                                "failure_reason": "ORIGINAL_ORDER_NOT_FOUND",
                                "reprice_timestamp": time.time()
                            },
                            "causation_id": str(original_local_id)
                        },
                        stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                    )
                return None

        if parent_trade_id is None:
            logger.error(f"Reprice SELL failed: Original order {original_local_id} has no parent_trade_id.")
            # Publish order modification failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceFailed",
                    payload={
                        "decision_type": "ORDER_REPRICE_FAILED",
                        "decision_reason": "Original order missing parent trade ID",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_limit_price": new_limit_price,
                            "side": "SELL",
                            "failure_reason": "MISSING_PARENT_TRADE_ID",
                            "original_order_data": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(original_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            return None

        # 2. Create a NEW copy order for the reprice
        try:
            # Use a distinct event_type if desired, e.g., "REPRICE_SELL"
            new_local_id = self.create_copy_order(
                symbol=symbol,
                side="SELL",
                event_type=f"REPRICE_{original_event_type}", # e.g., REPRICE_CLOSE, REPRICE_REDUCE
                requested_shares=original_req_shares,
                req_time=time.time(),
                parent_trade_id=parent_trade_id, # Link to the SAME parent trade
                requested_lmt_price=new_limit_price,
                comment=f"Reprice of local_id={original_local_id}",
                # Copy other relevant fields if necessary
                ocr_time=original_order_data.get("timestamp_ocr"),
                timestamp_ocr_processed=original_order_data.get("timestamp_ocr_processed"),
                ocr_confidence=original_order_data.get("ocr_confidence")
            )
            logger.info(f"Reprice SELL successful: Created new order local_id={new_local_id} for original {original_local_id} (parent_trade={parent_trade_id})")
            
            # Publish successful order repricing event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceSuccess",
                    payload={
                        "decision_type": "ORDER_REPRICE_SUCCESS",
                        "decision_reason": "Successfully created repriced sell order",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_local_id": new_local_id,
                            "original_price": original_order_data.get("original_price"),
                            "new_price": new_limit_price,
                            "price_change": new_limit_price - (original_order_data.get("original_price") or 0),
                            "side": "SELL",
                            "event_type": f"REPRICE_{original_event_type}",
                            "requested_shares": original_req_shares,
                            "parent_trade_id": parent_trade_id,
                            "original_order_metadata": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(new_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            return new_local_id
        except Exception as e:
            logger.error(f"Reprice SELL failed: Error creating new copy order for original {original_local_id}: {e}", exc_info=True)
            
            # Publish order repricing failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OrderRepository",
                    event_name="OrderRepriceFailed",
                    payload={
                        "decision_type": "ORDER_REPRICE_FAILED",
                        "decision_reason": f"Exception during repriced order creation: {str(e)}",
                        "context": {
                            "symbol": symbol,
                            "original_local_id": original_local_id,
                            "new_limit_price": new_limit_price,
                            "side": "SELL",
                            "failure_reason": "CREATION_EXCEPTION",
                            "exception_type": type(e).__name__,
                            "exception_message": str(e),
                            "parent_trade_id": parent_trade_id,
                            "original_order_data": original_order_data,
                            "reprice_timestamp": time.time()
                        },
                        "causation_id": str(original_local_id)
                    },
                    stream_override=self._config_service.get_redis_stream_name('order_decisions', 'testrade:order-decisions')
                )
            
            return None

    # --- Stale Order Management ---
    # REMOVED: All stale order management methods have been moved to StaleOrderService

    # --- Private Helper Methods ---
    def _handle_order_status(self, msg: Dict[str, Any], perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        """
        Handles 'order_status' events from the broker.

        Args:
            msg: The order status message
            perf_timestamps: Optional performance tracking timestamps
        """
        ls_order_id = msg.get("ls_order_id")
        local_id = msg.get("local_copy_id", -1)
        status = msg.get("status", "")
        ephemeral_corr = msg.get("ephemeral_corr", None)  # Get ephemeral correlation ID if present
        symbol = msg.get("symbol", "UNKNOWN")

        # Early filter for "SentToLightspeed" status for non-system orders
        # If we can't link it to a system order, we'll ignore it
        if status == "SentToLightspeed" and local_id < 0 and ls_order_id is None and ephemeral_corr is None:
            logger.debug(f"Ignoring 'SentToLightspeed' status for non-system order: {msg}")
            return

        with self._lock:
            # Step 1: Try to find our_internal_local_id using multiple methods
            our_internal_local_id = -1

            # First try: Use ls_order_id if present
            if ls_order_id is not None and our_internal_local_id < 0:
                our_internal_local_id = self._ls_to_local.get(ls_order_id, -1)
                if our_internal_local_id >= 0:
                    logger.debug(f"[{symbol}] Found local_id={our_internal_local_id} using ls_order_id={ls_order_id}")

            # Second try: Use ephemeral_corr if present
            if ephemeral_corr is not None and our_internal_local_id < 0:
                # First check _correlation_to_local dictionary
                our_internal_local_id = self._correlation_to_local.get(ephemeral_corr, -1)
                if our_internal_local_id >= 0:
                    logger.debug(f"[{symbol}] Found local_id={our_internal_local_id} using _correlation_to_local for ephemeral_corr={ephemeral_corr}")
                else:
                    # If not found, search through _ephemeral_ls_ids
                    for lid, eph_id in self._ephemeral_ls_ids.items():
                        if eph_id == ephemeral_corr:
                            our_internal_local_id = lid
                            logger.debug(f"[{symbol}] Found local_id={our_internal_local_id} using _ephemeral_ls_ids for ephemeral_corr={ephemeral_corr}")
                            break

            # Third try: Use local_copy_id if provided by C++ extension
            if local_id >= 0 and our_internal_local_id < 0:
                # Verify that this local_id exists in our system
                if local_id in self._copy_orders:
                    our_internal_local_id = local_id
                    logger.debug(f"[{symbol}] Using provided local_id={our_internal_local_id}")

            # If we still couldn't find a valid local_id, log and return
            if our_internal_local_id < 0:
                if status == "SentToLightspeed":
                    logger.debug(f"[{symbol}] Could not find local_id for 'SentToLightspeed' status message: {msg}")
                else:
                    logger.warning(f"[{symbol}] Could not find local_id for order status message: {msg}")

                # Use the original local_id for further processing (which might be -1)
                our_internal_local_id = local_id

            # 0) Manual LS order (id ≥ 100 & unknown) → delegate
            if our_internal_local_id >= 100 and our_internal_local_id not in self._copy_orders:
                logger.info("[_handle_order_status] unlinked manual order → delegating")
                # Release lock before calling _handle_unlinked_order
                # which will acquire its own lock
                pass  # We'll handle this outside the lock
            else:
                # 1) Bail on negative / missing local_id
                if our_internal_local_id < 0:
                    logger.warning(f"[{symbol}] Order status with invalid local_id → ignored. Message: {msg}")
                    return

                # 2) Auto‑create placeholder if unseen
                if our_internal_local_id not in self._copy_orders:
                    # --- START of Refactored Code ---
                    self.logger.warning(
                        f"REACTIVE_SYNC_DISABLED: Received status update for an unknown order "
                        f"(local_id={our_internal_local_id}, broker_id={ls_order_id}). "
                        f"This indicates a sync gap or a delayed message. The event will be ignored."
                    )
                    return # Exit the handler immediately. Do not process further.
                    # --- END of Refactored Code ---

                # Handle broker acknowledgement status "SentToLightspeed"
                if status == "SentToLightspeed" and our_internal_local_id in self._copy_orders:
                    current_order = self._copy_orders[our_internal_local_id]
                    old_status = current_order.ls_status

                    # Only update if the order is in an initial state
                    if old_status in [OrderLSAgentStatus.PENDING_SUBMISSION, OrderLSAgentStatus.UNKNOWN]:
                        # Prepare meta updates for ephemeral correlation ID
                        meta_updates = dict(current_order.meta) if current_order.meta else {}
                        meta_updates["timestamp_broker_ack"] = time.time()

                        if ephemeral_corr is not None:
                            meta_updates["ephemeral_corr_id"] = ephemeral_corr
                            # Handle linked_ephemeral_ids as a list in meta (since sets aren't JSON serializable)
                            existing_linked = meta_updates.get("linked_ephemeral_ids", [])
                            if ephemeral_corr not in existing_linked:
                                existing_linked.append(ephemeral_corr)
                            meta_updates["linked_ephemeral_ids"] = existing_linked

                        # Create updated order with new status and meta
                        updated_order = current_order.with_update(
                            ls_status=OrderLSAgentStatus.SUBMITTED_TO_BROKER,  # "AcknowledgedByBroker" equivalent
                            meta=meta_updates
                        )

                        self._copy_orders[our_internal_local_id] = updated_order
                        logger.info(f"[{symbol}] Order {our_internal_local_id} status: {old_status.value} -> {updated_order.ls_status.value} via ephemeral_corr {ephemeral_corr}")

                # 3) Check if we need to link LS order IDs (ephemeral vs final)
                existing_oid = self._copy_orders[our_internal_local_id].ls_order_id if our_internal_local_id in self._copy_orders else None
                need_to_link = ls_order_id and ls_order_id != existing_oid
                is_submitted = status == "Submitted"

        # Handle the unlinked order case outside the lock
        if our_internal_local_id >= 100 and our_internal_local_id not in self._copy_orders:
            self._handle_unlinked_order(msg)
            return

        # Link LS order IDs outside the lock to avoid nested locks
        if need_to_link:
            if is_submitted:
                self.link_ls_order(our_internal_local_id, ls_order_id, is_ephemeral=True, perf_timestamps=perf_timestamps)
            else:
                self.link_ls_order(our_internal_local_id, ls_order_id, is_ephemeral=False, perf_timestamps=perf_timestamps)

        # 4) Handle any fills
        fill_sh = float(msg.get("fill_shares", 0.0))
        fill_px = float(msg.get("fill_price", 0.0))
        fill_ms = msg.get("fill_time_ms")  # ms since epoch or None
        fill_sec = (float(fill_ms) / 1000.0) if fill_ms is not None else time.time()

        if fill_sh > 0.0 and ls_order_id is not None:
            extras_for_fill = {
                k: v for k, v in msg.items()
                if k not in ("event", "ls_order_id", "local_copy_id", "status", "fill_shares", "fill_price")
            }
            # Add our_internal_local_id to extras for reference
            if our_internal_local_id >= 0:
                extras_for_fill["our_internal_local_id"] = our_internal_local_id

            partial_flag = "FullFill" if status in ("Filled", "FullFill") else "PartialFill"

            self._handle_fill(
                ls_order_id=ls_order_id,
                shares_filled=fill_sh,
                fill_price=fill_px,
                fill_time=fill_sec,
                partial_status=partial_flag,
                perf_timestamps=perf_timestamps,
                **extras_for_fill
            )

        # 5) Update non‑fill status fields
        if ls_order_id is not None and status:
            extras_for_status = {
                k: v for k, v in msg.items()
                if k not in ("event", "fill_shares", "fill_price", "ls_order_id", "status")
            }
            # Add our_internal_local_id to extras for reference
            if our_internal_local_id >= 0:
                extras_for_status["our_internal_local_id"] = our_internal_local_id

            # rename ambiguous event_type
            if extras_for_status.get("event_type", "").lower() == "order_status":
                extras_for_status["broker_event_type"] = extras_for_status.pop("event_type")
            # drop non‑positive limit prices
            try:
                if float(extras_for_status.get("requested_lmt_price", 1)) <= 0:
                    extras_for_status.pop("requested_lmt_price", None)
            except (TypeError, ValueError):
                extras_for_status.pop("requested_lmt_price", None)

            self._update_status(ls_order_id, status, **extras_for_status)

# @log_function_call('order_repository') # Uncomment if decorator exists and is desired
    def _update_status(self, ls_order_id: int, status: str, **kwargs) -> None:
        """
        Updates the status of an order using immutable Order objects.
        Refactored to release lock before external calls (callback, GUI updates).

        Args:
            ls_order_id: The Lightspeed order ID (if available, can be None if using our_internal_local_id)
            status: The new status string from the broker or system.
            **kwargs: Additional fields to update or use.
                      Expected: 'our_internal_local_id' (Optional[int]), 'reason' (Optional[str]),
                                'symbol' (Optional[str] for logging if local_id not found early).
        """
        # Extract our_internal_local_id if provided in kwargs (used for direct local_id updates)
        our_internal_local_id = kwargs.pop("our_internal_local_id", None)
        symbol_from_kwargs = kwargs.get("symbol", "UNKNOWN_SYM_IN_UPDATE_STATUS")

        logger.debug(f"ENTER _update_status: ls_order_id={ls_order_id}, status_str='{status}', "
                          f"our_internal_local_id_kwarg={our_internal_local_id}, extras={kwargs}")

        # Variables for data to be used AFTER the lock is released
        updated_order_for_ext_calls: Optional[Order] = None # The new immutable order object
        trade_finalized_callback_ref: Optional[Callable] = None

        # Flags for actions after lock release
        invoke_trade_finalized_callback: bool = False
        schedule_gui_update_flag: bool = False

        try:
            current_time = time.time()

            # Convert input status string to OrderLSAgentStatus Enum
            # First try direct enum value lookup
            try:
                new_ls_status_enum = OrderLSAgentStatus(status)
            except ValueError:
                # If direct lookup fails, try mapping common status strings
                # Comprehensive mapping based on Lightspeed broker documentation and legacy code
                status_mapping = {
                    # Basic status mappings
                    "SentToLightspeed": OrderLSAgentStatus.SENT_TO_LIGHTSPEED,
                    "Submitted": OrderLSAgentStatus.SUBMITTED_TO_BROKER,
                    "Accepted": OrderLSAgentStatus.WORKING,
                    "Working": OrderLSAgentStatus.WORKING,
                    "Filled": OrderLSAgentStatus.FILLED,
                    "Cancelled": OrderLSAgentStatus.CANCELLED,
                    "Canceled": OrderLSAgentStatus.CANCELLED,  # Handle both spellings
                    "Rejected": OrderLSAgentStatus.REJECTED,

                    # Additional Lightspeed-specific status mappings
                    "PENDING": OrderLSAgentStatus.PENDING_SUBMISSION,
                    "NEW": OrderLSAgentStatus.SUBMITTED_TO_BROKER,
                    "SENTTOLIGHTSPEED": OrderLSAgentStatus.SUBMITTED_TO_BROKER,
                    "ACCEPTED": OrderLSAgentStatus.WORKING,
                    "WORKING": OrderLSAgentStatus.WORKING,
                    "PARTIALLY FILLED": OrderLSAgentStatus.PARTIALLY_FILLED,
                    "PARTIALLY_FILLED": OrderLSAgentStatus.PARTIALLY_FILLED,
                    "FILLED": OrderLSAgentStatus.FILLED,
                    "CANCEL REQUESTED": OrderLSAgentStatus.PENDING_CANCEL,
                    "PENDING CANCEL": OrderLSAgentStatus.PENDING_CANCEL,
                    "PENDING_CANCEL": OrderLSAgentStatus.PENDING_CANCEL,
                    "CANCELLED": OrderLSAgentStatus.CANCELLED,
                    "CANCELED": OrderLSAgentStatus.CANCELLED,
                    "REJECTED": OrderLSAgentStatus.REJECTED,

                    # Legacy status mappings for backward compatibility
                    "Pending": OrderLSAgentStatus.PENDING_SUBMISSION,
                    "SendAttempted": OrderLSAgentStatus.PENDING_SUBMISSION,
                    "AcknowledgedByBroker": OrderLSAgentStatus.SUBMITTED_TO_BROKER,
                    "PartiallyFilled": OrderLSAgentStatus.PARTIALLY_FILLED,
                    "CancelRequested": OrderLSAgentStatus.PENDING_CANCEL,
                    "Unknown": OrderLSAgentStatus.UNKNOWN,
                }
                new_ls_status_enum = status_mapping.get(status, OrderLSAgentStatus.UNKNOWN)
                if new_ls_status_enum == OrderLSAgentStatus.UNKNOWN:
                    logger.error(f"Invalid new status string: '{status}' for ls_order_id={ls_order_id}/local_id={our_internal_local_id}. Using UNKNOWN.")
                else:
                    logger.debug(f"Mapped status string '{status}' to enum {new_ls_status_enum.value}")

            # --- Short critical section to fetch current order and prepare updates ---
            current_order: Optional[Order] = None
            local_id_found: Optional[int] = None

            with self._lock:
                if our_internal_local_id is not None:
                    if our_internal_local_id in self._copy_orders:
                        current_order = self._copy_orders[our_internal_local_id]
                        local_id_found = our_internal_local_id
                        symbol_from_kwargs = current_order.symbol # Update for better logging
                        logger.debug(f"[{symbol_from_kwargs}] Using provided our_internal_local_id={local_id_found} in _update_status")
                elif ls_order_id is not None: # ls_order_id can be None if we are only given our_internal_local_id
                    local_id_from_map = self._ls_to_local.get(ls_order_id)
                    if local_id_from_map and local_id_from_map in self._copy_orders:
                        current_order = self._copy_orders[local_id_from_map]
                        local_id_found = local_id_from_map
                        symbol_from_kwargs = current_order.symbol # Update for better logging

                if not current_order or local_id_found is None:
                    # Provide detailed debugging information
                    available_local_ids = list(self._copy_orders.keys())
                    available_ls_ids = list(self._ls_to_local.keys())
                    available_correlations = list(self._correlation_to_local.keys())

                    logger.warning(
                        f"[{symbol_from_kwargs}] No local copy order found for ls_orderId={ls_order_id} / our_internal_local_id={our_internal_local_id}. "
                        f"Ignoring status update to '{status}'. "
                        f"Available local_ids: {available_local_ids[:5]}{'...' if len(available_local_ids) > 5 else ''}, "
                        f"Available ls_ids: {available_ls_ids[:5]}{'...' if len(available_ls_ids) > 5 else ''}, "
                        f"Available correlations: {available_correlations[:5]}{'...' if len(available_correlations) > 5 else ''}"
                    )
                    return

                # Prepare changes for the new Order instance
                changes_for_new_order: Dict[str, Any] = {}

                # Initialize meta_updates
                meta_updates = {}

                # Get the callback reference while under lock
                trade_finalized_callback_ref = self._trade_finalized_callback

                # Status history entry
                reason_for_status = kwargs.get("reason", "") # Get reason from kwargs
                # TODO: Consider if broker_timestamp for status should come from kwargs
                
                # Translate broker status to generic status using same logic as Order class
                broker_to_generic_mapping = {
                    OrderLSAgentStatus.PENDING_SUBMISSION: OrderStatus.PENDING_SUBMISSION,
                    OrderLSAgentStatus.SEND_ATTEMPTED_TO_BRIDGE: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.SEND_FAILED_TO_BRIDGE: OrderStatus.ERROR,
                    OrderLSAgentStatus.ACKNOWLEDGED_BY_BROKER: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.SUBMITTED_TO_BROKER: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.WORKING: OrderStatus.WORKING,
                    OrderLSAgentStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
                    OrderLSAgentStatus.FILLED: OrderStatus.FILLED,
                    OrderLSAgentStatus.CANCEL_REQUESTED_TO_BRIDGE: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.CANCELLED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.REJECTED: OrderStatus.REJECTED,
                    OrderLSAgentStatus.CANCEL_REJECTED: OrderStatus.WORKING,
                    OrderLSAgentStatus.LS_SUBMITTED: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE: OrderStatus.WORKING,
                    OrderLSAgentStatus.LS_REJECTION_FROM_BROKER: OrderStatus.REJECTED,
                    OrderLSAgentStatus.LS_KILLED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.EXPIRED: OrderStatus.EXPIRED,
                    OrderLSAgentStatus.DONE_FOR_DAY: OrderStatus.EXPIRED,
                    OrderLSAgentStatus.STOPPED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.PENDING: OrderStatus.PENDING_SUBMISSION,
                    OrderLSAgentStatus.SEND_FAILED: OrderStatus.ERROR,
                    OrderLSAgentStatus.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.SENT_TO_LIGHTSPEED: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.FULL_FILL: OrderStatus.FILLED,
                    OrderLSAgentStatus.UNKNOWN: OrderStatus.UNKNOWN,
                }
                generic_status = broker_to_generic_mapping.get(new_ls_status_enum, OrderStatus.UNKNOWN)
                
                new_status_history_entry = StatusHistoryEntry(
                    timestamp=current_time,
                    status=new_ls_status_enum,  # Broker-specific status
                    generic_status=generic_status,  # Derived generic status
                    reason=reason_for_status if reason_for_status else None
                )
                changes_for_new_order["status_history"] = current_order.status_history + (new_status_history_entry,)

                # Logic for specific statuses
                old_ls_status_enum = current_order.ls_status

                # Default to new status enum
                changes_for_new_order["ls_status"] = new_ls_status_enum

                if new_ls_status_enum == OrderLSAgentStatus.REJECTED:
                    if old_ls_status_enum == OrderLSAgentStatus.CANCEL_REQUESTED:
                        changes_for_new_order["ls_status"] = OrderLSAgentStatus.WORKING # Cancel rejected, still live
                        logger.warning(f"[{current_order.symbol}] Order {current_order.local_id} CANCEL_REJECTED. Status remains/reverts to WORKING.")
                    else: # Normal rejection
                        # Note: Order class doesn't have final_fill_time field, so we'll store it in meta
                        meta_updates = meta_updates or {}
                        meta_updates["final_fill_time"] = current_time
                        changes_for_new_order["leftover_shares"] = 0.0
                        changes_for_new_order["rejection_reason"] = reason_for_status or kwargs.get("rejectionText", "Unknown broker rejection")
                        invoke_trade_finalized_callback = True

                elif new_ls_status_enum == OrderLSAgentStatus.FILLED:
                    # Note: Order class doesn't have final_fill_time field, so we'll store it in meta
                    meta_updates = meta_updates or {}
                    meta_updates["final_fill_time"] = current_time
                    changes_for_new_order["leftover_shares"] = 0.0
                    invoke_trade_finalized_callback = True

                elif new_ls_status_enum == OrderLSAgentStatus.CANCELLED:
                    # Note: Order class doesn't have final_fill_time field, so we'll store it in meta
                    meta_updates = meta_updates or {}
                    meta_updates["final_fill_time"] = current_time
                    changes_for_new_order["leftover_shares"] = 0.0
                    if reason_for_status: # Store cancel reason if provided
                        changes_for_new_order["cancel_reason"] = reason_for_status
                    invoke_trade_finalized_callback = True

                # Initialize meta_updates if it doesn't exist yet
                meta_updates = {}

                # Add any other fields from kwargs to 'changes_for_new_order'
                # These will override fields if they match Order dataclass fields,
                # or be ignored by `with_update` if they don't.
                # For 'meta', we need to merge.
                kwargs_meta_updates = {k: v for k, v in kwargs.items() if k not in changes_for_new_order and k not in Order.__annotations__}
                first_class_kwarg_updates = {k:v for k,v in kwargs.items() if k in Order.__annotations__}
                changes_for_new_order.update(first_class_kwarg_updates)

                # Merge all meta updates
                meta_updates.update(kwargs_meta_updates)

                if meta_updates:
                    current_meta = dict(current_order.meta) if hasattr(current_order, 'meta') else {}
                    current_meta.update(meta_updates)
                    changes_for_new_order["meta"] = current_meta

                # Create the new immutable order
                updated_order_for_ext_calls = current_order.with_update(**changes_for_new_order)

                # Store the new order object
                self._copy_orders[local_id_found] = updated_order_for_ext_calls

                # If status became final, clean up ephemeral correlation ID mapping
                if invoke_trade_finalized_callback: # invoke_trade_finalized_callback implies a final state
                    ephemeral_ids_to_remove = [
                        eph_id for eph_id, mapped_local_id in self._correlation_to_local.items()
                        if mapped_local_id == local_id_found
                    ]
                    for eph_id in ephemeral_ids_to_remove:
                        self._correlation_to_local.pop(eph_id, None)
                        logger.debug(f"Cleaned up ephemeral_corr mapping for {updated_order_for_ext_calls.ls_status.value} order {local_id_found} (eph_id: {eph_id})")

                logger.info(f"Refactored _update_status: Order {local_id_found} status {old_ls_status_enum.value} -> {updated_order_for_ext_calls.ls_status.value}. Lock released.")
            # --- Lock released ---

            if not updated_order_for_ext_calls: # Should not happen if current_order was found
                return

            # Log details (outside lock, using data from immutable updated_order_for_ext_calls)
            log_prefix = f"[_update_status POST-LOCK] local_id={updated_order_for_ext_calls.local_id} ({updated_order_for_ext_calls.symbol}): "
            if updated_order_for_ext_calls.ls_status == OrderLSAgentStatus.REJECTED and old_ls_status_enum != OrderLSAgentStatus.CANCEL_REQUESTED:
                logger.warning(f"{log_prefix}Order REJECTED. Leftover={updated_order_for_ext_calls.leftover_shares:.2f}. Reason: '{updated_order_for_ext_calls.rejection_reason}'.")
            elif updated_order_for_ext_calls.ls_status == OrderLSAgentStatus.FILLED:
                logger.info(f"{log_prefix}Order FULLY FILLED. Leftover={updated_order_for_ext_calls.leftover_shares:.2f}.")
            elif updated_order_for_ext_calls.ls_status == OrderLSAgentStatus.CANCELLED:
                logger.info(f"{log_prefix}Order CANCELLED. Leftover={updated_order_for_ext_calls.leftover_shares:.2f}. Reason: '{updated_order_for_ext_calls.cancel_reason}'.")
            else: # Non-final or other status update
                logger.info(f"{log_prefix}Order status updated to '{updated_order_for_ext_calls.ls_status.value}'. Original broker status string was '{status}'. Leftover={updated_order_for_ext_calls.leftover_shares:.2f}. Extras from kwargs: {kwargs}")


            # --- Schedule GUI update OUTSIDE the lock if needed ---
            # This is based on invoke_trade_finalized_callback flag, which implies a final status
            if invoke_trade_finalized_callback and self._core_logic_manager_ref:
                schedule_gui_update_flag = True # Set flag to true as conditions met

            if schedule_gui_update_flag: # Check the flag
                try:
                    gui_data = {
                        "local_id": updated_order_for_ext_calls.local_id,
                        "symbol": updated_order_for_ext_calls.symbol,
                        "status": updated_order_for_ext_calls.ls_status.value, # Send string value to GUI
                        "leftover": updated_order_for_ext_calls.leftover_shares
                    }
                    self._core_logic_manager_ref.schedule_gui_update(
                        update_type="order_status_updated",
                        payload=gui_data
                    )
                    logger.debug(f"{log_prefix}Scheduled GUI update for 'order_status_updated'.")
                except Exception as e_gui:
                    logger.error(f"{log_prefix}Error scheduling GUI update: {e_gui}")
            elif invoke_trade_finalized_callback: # Log if GUI update was intended but CLM ref missing
                 logger.debug(f"{log_prefix}CoreLogicManager ref not set. Cannot schedule GUI update for final status.")

            # --- Call the trade finalized callback OUTSIDE the lock if needed ---
            if invoke_trade_finalized_callback and trade_finalized_callback_ref:
                if not (updated_order_for_ext_calls.symbol and updated_order_for_ext_calls.event_type and updated_order_for_ext_calls.ls_status):
                    logger.error(f"{log_prefix}Cannot invoke trade finalized callback due to missing data in Order object: "
                                 f"Sym='{updated_order_for_ext_calls.symbol}', EvType='{updated_order_for_ext_calls.event_type}', FinalStat='{updated_order_for_ext_calls.ls_status}'.")
                else:
                    try:
                        logger.info(f"{log_prefix}Invoking trade finalized callback. Status='{updated_order_for_ext_calls.ls_status.value}', ParentTradeID={updated_order_for_ext_calls.parent_trade_id}.")
                        trade_finalized_callback_ref(
                            updated_order_for_ext_calls.local_id,
                            updated_order_for_ext_calls.symbol,
                            updated_order_for_ext_calls.event_type.value, # Send string value
                            updated_order_for_ext_calls.ls_status.value,  # Send string value
                            updated_order_for_ext_calls.parent_trade_id
                        )
                        logger.debug(f"{log_prefix}Trade finalized callback completed successfully.")
                    except Exception as e_cb:
                        logger.error(f"{log_prefix}Error executing trade finalized callback: {e_cb}", exc_info=True)
            elif invoke_trade_finalized_callback:
                logger.debug(f"{log_prefix}Trade finalized callback not set. Skipping callback for final status.")

            # --- Publish order update to Redis for GUI (after all callbacks) ---
            if updated_order_for_ext_calls:
                # Order update publishing removed - handled by telemetry service
                pass

        except Exception as e:
            logger.exception(f"CRITICAL ERROR within _update_status for ls_order_id={ls_order_id}/local_id={our_internal_local_id}: {e}")

# @log_function_call('order_repository') # Uncomment if decorator exists
    def _handle_fill(
        self,
        ls_order_id: Optional[int], # Made Optional: can be None if our_internal_local_id is primary
        shares_filled: float,
        fill_price: float,
        fill_time: float,
        commission: float = 0.0,
        partial_status: str = "PartialFill", # Status of THIS fill
        perf_timestamps: Optional[Dict[str, float]] = None,
        **kwargs
    ) -> None:
        our_internal_local_id = kwargs.pop("our_internal_local_id", None)
        # symbol_from_kwargs is for logging if order not found.
        # If order is found, its own symbol will be used.
        symbol_from_kwargs = kwargs.get("symbol", "UNKNOWN_SYM_AT_FILL_ENTRY")

        fill_time_est_str = kwargs.pop("fill_time_est_str", kwargs.pop("fill_time_est", None))
        liquidity_from_kwargs = kwargs.pop("liquidity", None)
        fill_id_broker_from_kwargs = kwargs.pop("fill_id_broker", None)

        # Make a copy of kwargs for FillRecord.meta to avoid modifying the original dict further
        meta_for_fill_record = dict(kwargs)

        logger.debug(
            f"ENTER _handle_fill: ls_order_id={ls_order_id}, shares={shares_filled}, price={fill_price:.4f}, time={fill_time}, "
            f"our_internal_local_id_kwarg={our_internal_local_id}, partial_status_str='{partial_status}'"
        )

        active_perf_timestamps = perf_timestamps
        if is_performance_tracking_enabled():
            if active_perf_timestamps is None: active_perf_timestamps = create_timestamp_dict()
            if active_perf_timestamps: add_timestamp(active_perf_timestamps, 'order_repo.handle_fill_start')

        updated_order_for_ext_calls: Optional[Order] = None
        call_position_manager_flag: bool = False

        # Data for PositionManager.process_fill - will be populated from the updated_order
        # These are distinct from pm_symbol/pm_side used *inside* the lock for internal cache updates
        ext_call_symbol: Optional[str] = None
        ext_call_side_str: Optional[str] = None
        ext_call_local_id: Optional[int] = None

        try:
            current_order: Optional[Order] = None
            local_id_found: Optional[int] = None

            # --- CRITICAL SECTION START ---
            with self._lock:
                # 1. Find the order
                if our_internal_local_id is not None:
                    if our_internal_local_id in self._copy_orders:
                        current_order = self._copy_orders[our_internal_local_id]
                        local_id_found = our_internal_local_id
                elif ls_order_id is not None:
                    local_id_from_map = self._ls_to_local.get(ls_order_id)
                    if local_id_from_map and local_id_from_map in self._copy_orders:
                        current_order = self._copy_orders[local_id_from_map]
                        local_id_found = local_id_from_map

                if not current_order or local_id_found is None:
                    log_sym = symbol_from_kwargs if not current_order else current_order.symbol
                    self.logger.warning(
                        f"[{log_sym}] No local order found for ls_orderId={ls_order_id} / "
                        f"our_internal_local_id={our_internal_local_id}. Ignoring fill."
                    )
                    if is_performance_tracking_enabled() and active_perf_timestamps:
                        add_timestamp(active_perf_timestamps, 'order_repo.handle_fill_end_no_local_id')
                    return

                # At this point, current_order and local_id_found are valid
                # Use these for all subsequent operations within the lock
                order_symbol_for_internal_caches = current_order.symbol
                order_side_for_internal_caches = current_order.side # This is OrderSide Enum

                logger.info(f"[{current_order.symbol}] LOCK ACQUIRED for FILL processing: local_id={local_id_found}")

                changes: Dict[str, Any] = {}

                # 2. Create FillRecord
                new_fill_record = FillRecord(
                    shares_filled=float(shares_filled), fill_price=float(fill_price), fill_time=float(fill_time),
                    commission=float(commission), side=order_side_for_internal_caches,
                    liquidity=liquidity_from_kwargs, fill_id_broker=fill_id_broker_from_kwargs,
                    fill_time_est_str=fill_time_est_str, meta=meta_for_fill_record
                )
                changes["fills"] = current_order.fills + (new_fill_record,)

                # 3. Update quantities
                new_filled_qty = current_order.filled_quantity + shares_filled
                new_leftover_shares = max(0.0, current_order.requested_shares - new_filled_qty)
                changes["filled_quantity"] = new_filled_qty
                changes["leftover_shares"] = new_leftover_shares

                # 4. Update status & related fields
                meta_updates = dict(current_order.meta) if hasattr(current_order, 'meta') else {}

                if new_leftover_shares <= 1e-9:
                    new_status_enum = OrderLSAgentStatus.FILLED
                    meta_updates["final_fill_time"] = fill_time # Time of this fill
                else:
                    new_status_enum = OrderLSAgentStatus.PARTIALLY_FILLED

                changes["meta"] = meta_updates
                changes["ls_status"] = new_status_enum

                # Use same translation mapping as above for consistency
                broker_to_generic_mapping = {
                    OrderLSAgentStatus.PENDING_SUBMISSION: OrderStatus.PENDING_SUBMISSION,
                    OrderLSAgentStatus.SEND_ATTEMPTED_TO_BRIDGE: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.SEND_FAILED_TO_BRIDGE: OrderStatus.ERROR,
                    OrderLSAgentStatus.ACKNOWLEDGED_BY_BROKER: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.SUBMITTED_TO_BROKER: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.WORKING: OrderStatus.WORKING,
                    OrderLSAgentStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
                    OrderLSAgentStatus.FILLED: OrderStatus.FILLED,
                    OrderLSAgentStatus.CANCEL_REQUESTED_TO_BRIDGE: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.CANCELLED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.REJECTED: OrderStatus.REJECTED,
                    OrderLSAgentStatus.CANCEL_REJECTED: OrderStatus.WORKING,
                    OrderLSAgentStatus.LS_SUBMITTED: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE: OrderStatus.WORKING,
                    OrderLSAgentStatus.LS_REJECTION_FROM_BROKER: OrderStatus.REJECTED,
                    OrderLSAgentStatus.LS_KILLED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.EXPIRED: OrderStatus.EXPIRED,
                    OrderLSAgentStatus.DONE_FOR_DAY: OrderStatus.EXPIRED,
                    OrderLSAgentStatus.STOPPED: OrderStatus.CANCELLED,
                    OrderLSAgentStatus.PENDING: OrderStatus.PENDING_SUBMISSION,
                    OrderLSAgentStatus.SEND_FAILED: OrderStatus.ERROR,
                    OrderLSAgentStatus.CANCEL_REQUESTED: OrderStatus.PENDING_CANCEL,
                    OrderLSAgentStatus.SENT_TO_LIGHTSPEED: OrderStatus.SUBMITTED,
                    OrderLSAgentStatus.FULL_FILL: OrderStatus.FILLED,
                    OrderLSAgentStatus.UNKNOWN: OrderStatus.UNKNOWN,
                }
                generic_status_for_fill = broker_to_generic_mapping.get(new_status_enum, OrderStatus.UNKNOWN)
                
                new_status_history_entry = StatusHistoryEntry(
                    timestamp=fill_time, 
                    status=new_status_enum,  # Broker-specific status
                    generic_status=generic_status_for_fill,  # Derived generic status
                    reason=f"Fill: {shares_filled}@{fill_price}"
                )
                changes["status_history"] = current_order.status_history + (new_status_history_entry,)

                # 5. Create new immutable Order
                updated_order_obj = current_order.with_update(**changes)
                self._copy_orders[local_id_found] = updated_order_obj

                # For calls after lock, use data from the new immutable object
                updated_order_for_ext_calls = updated_order_obj
                ext_call_symbol = updated_order_obj.symbol

                # Handle side which could be either an enum or a string
                if hasattr(updated_order_obj.side, 'value'):
                    ext_call_side_str = updated_order_obj.side.value  # Enum case
                else:
                    ext_call_side_str = str(updated_order_obj.side)  # String case

                ext_call_local_id = updated_order_obj.local_id
                call_position_manager_flag = True
                schedule_gui_update_flag = True


                # 6. Update internal caches (_calculated_net_position, _symbol_stats)
                # Use order_symbol_for_internal_caches and order_side_for_internal_caches
                current_net_pos = self._calculated_net_position.get(order_symbol_for_internal_caches, 0.0)
                new_net_pos = current_net_pos

                # Check if order_side_for_internal_caches is an OrderSide Enum or a string
                if isinstance(order_side_for_internal_caches, OrderSide):
                    # It's an OrderSide Enum
                    if order_side_for_internal_caches == OrderSide.BUY:
                        new_net_pos += shares_filled
                    elif order_side_for_internal_caches == OrderSide.SELL:
                        new_net_pos -= shares_filled
                    else:
                        logger.error(f"Unknown side '{order_side_for_internal_caches.value}' for fill cache update. Symbol {order_symbol_for_internal_caches}")
                else:
                    # It's a string
                    side_str = str(order_side_for_internal_caches).upper()
                    if side_str == "BUY":
                        new_net_pos += shares_filled
                    elif side_str == "SELL":
                        new_net_pos -= shares_filled
                    else:
                        logger.error(f"Unknown side '{side_str}' for fill cache update. Symbol {order_symbol_for_internal_caches}")

                if abs(new_net_pos) < 1e-9: new_net_pos = 0.0
                self._calculated_net_position[order_symbol_for_internal_caches] = new_net_pos

                # Get side string for logging
                side_str_for_log = None
                if isinstance(order_side_for_internal_caches, OrderSide):
                    side_str_for_log = order_side_for_internal_caches.value
                else:
                    side_str_for_log = str(order_side_for_internal_caches).upper()

                logger.info(
                    f"[{order_symbol_for_internal_caches}] Updated _calculated_net_position: {current_net_pos:.2f} -> {new_net_pos:.2f} "
                    f"(Fill: {side_str_for_log} {shares_filled})"
                )

                self._init_symbol_stats(order_symbol_for_internal_caches)
                st = self._symbol_stats[order_symbol_for_internal_caches]
                st["total_shares_traded"] += shares_filled

                # Check if order_side_for_internal_caches is an OrderSide Enum or a string
                if isinstance(order_side_for_internal_caches, OrderSide):
                    # It's an OrderSide Enum
                    if order_side_for_internal_caches == OrderSide.BUY:
                        st["position"] += shares_filled
                    elif order_side_for_internal_caches == OrderSide.SELL:
                        st["position"] -= shares_filled
                else:
                    # It's a string
                    side_str = str(order_side_for_internal_caches).upper()
                    if side_str == "BUY":
                        st["position"] += shares_filled
                    elif side_str == "SELL":
                        st["position"] -= shares_filled

                logger.debug(f"Updated _symbol_stats for {order_symbol_for_internal_caches}: position={st['position']:.2f}, traded={st['total_shares_traded']:.2f}")

                logger.info(f"Refactored _handle_fill: Order {local_id_found} processed fill. Lock to be released.")
            # --- CRITICAL SECTION END ---

            if not updated_order_for_ext_calls: return # Should have been caught earlier

            # --- External Calls (Post-Lock) ---
            if call_position_manager_flag and self._position_manager and ext_call_local_id is not None and ext_call_symbol is not None and ext_call_side_str is not None:
                logger.debug(
                    f"Calling PositionManager.process_fill for local_id={ext_call_local_id} "
                    f"(symbol={ext_call_symbol}, side={ext_call_side_str}, shares={shares_filled}, price={fill_price}, time={fill_time})"
                )
                try:
                    if is_performance_tracking_enabled() and active_perf_timestamps: add_timestamp(active_perf_timestamps, 'order_repo.call_position_manager_start')
                    # Get master_correlation_id from the updated order
                    master_corr_id_for_fill = updated_order_for_ext_calls.master_correlation_id
                    self._position_manager.process_fill(
                        symbol=str(ext_call_symbol), side=str(ext_call_side_str),
                        shares_filled=float(shares_filled), fill_price=float(fill_price),
                        local_id=int(ext_call_local_id), fill_time=float(fill_time),
                        master_correlation_id=master_corr_id_for_fill  # Pass the master correlation ID
                    )
                    if is_performance_tracking_enabled() and active_perf_timestamps: add_timestamp(active_perf_timestamps, 'order_repo.call_position_manager_end')
                except Exception as e_pm:
                    logger.error(f"Error calling PositionManager.process_fill for local_id={ext_call_local_id}: {e_pm}", exc_info=True)
            # ... (rest of GUI update and logging as before, using updated_order_for_ext_calls) ...
            if schedule_gui_update_flag and self._core_logic_manager_ref:
                try:
                    # Handle side which could be either an enum or a string
                    if hasattr(updated_order_for_ext_calls.side, 'value'):
                        side_str = updated_order_for_ext_calls.side.value  # Enum case
                    else:
                        side_str = str(updated_order_for_ext_calls.side)  # String case

                    # Handle status which could be either an enum or a string
                    if hasattr(updated_order_for_ext_calls.ls_status, 'value'):
                        status_str = updated_order_for_ext_calls.ls_status.value  # Enum case
                    else:
                        status_str = str(updated_order_for_ext_calls.ls_status)  # String case

                    gui_data = {
                        "local_id": updated_order_for_ext_calls.local_id,
                        "symbol": updated_order_for_ext_calls.symbol,
                        "side": side_str,
                        "shares_filled": shares_filled,
                        "fill_price": fill_price,
                        "status": status_str
                    }
                    self._core_logic_manager_ref.schedule_gui_update(update_type="fill_processed", data=gui_data)
                except Exception as e_gui: logger.error(f"Error scheduling GUI update after fill: {e_gui}")
            elif schedule_gui_update_flag: logger.debug(f"CoreLogicManager ref not set. Cannot schedule GUI update.")

            if updated_order_for_ext_calls.leftover_shares <= 1e-9: logger.info(f"Local copy order {updated_order_for_ext_calls.local_id} fully filled.")
            logger.debug(
                f"Local copy order {updated_order_for_ext_calls.local_id} => recorded fill: {shares_filled}@{fill_price}, "
                f"new leftover={updated_order_for_ext_calls.leftover_shares:.2f}, new status={updated_order_for_ext_calls.ls_status.value}"
            )

            # --- Publish order update to Redis for GUI (after all processing) ---
            if updated_order_for_ext_calls:
                # Order update publishing removed - handled by telemetry service
                pass

        except Exception as e:
            logger.exception(f"CRITICAL ERROR within _handle_fill for ls_order_id={ls_order_id}/local_id={our_internal_local_id}: {e}")
        finally:
            if is_performance_tracking_enabled() and active_perf_timestamps:
                add_timestamp(active_perf_timestamps, 'order_repo.handle_fill_end')
                # ... (performance summary logging) ...

    def _init_symbol_stats(self, sym: str) -> None:
        """
        Ensures that the given symbol has an entry in self._symbol_stats.
        If not, create a default blank stats dict.

        Args:
            sym: The symbol
        """
        if sym not in self._symbol_stats:
            self._symbol_stats[sym] = {
                "symbol": sym,
                "position": 0.0,
                "my_cost_basis": 0.0,
                "open_pnl": 0.0,
                "closed_pnl": 0.0,
                "total_pnl": 0.0,
                "pos_from_cost_basis": 0.0,
                "total_shares_traded": 0.0
            }
            logger.debug(f"[DEBUG] _init_symbol_stats: Created new stats entry for symbol={sym}")

    def _handle_unlinked_order(self, msg: Dict[str, Any]) -> None:
        """
        Handles order status messages for orders that were not created through our system.
        These are typically manual orders entered directly in Lightspeed.

        Args:
            msg: The order status message
        """
        ls_order_id = msg.get("ls_order_id")
        local_id = msg.get("local_copy_id", -1)
        status = msg.get("status", "")
        symbol = msg.get("symbol", "UNKNOWN")

        logger.info(f"Processing unlinked order: ls_order_id={ls_order_id}, local_id={local_id}, status={status}, symbol={symbol}")

        # Create a placeholder order record if we don't have one yet
        with self._lock:
            if local_id not in self._copy_orders:
                now_ts = time.time()
                self._copy_orders[local_id] = {
                    "ls_order_id": ls_order_id,
                    "ls_status": status,
                    "symbol": symbol,
                    "side": msg.get("side", "UNKNOWN"),
                    "event_type": "MANUAL",
                    "fills": [],
                    "leftover_shares": float(msg.get("shares", 0.0)),
                    "requested_shares": float(msg.get("shares", 0.0)),
                    "requested_lmt_price": float(msg.get("limit_price", 0.0)),
                    "timestamp_sent": now_ts,
                    "timestamp_ocr": now_ts,  # prevents GUI crash
                    "timestamp_requested": now_ts,
                    "parent_trade_id": -1,  # No parent trade for manual orders
                    "comment": "Manual order from Lightspeed",
                }

                # Link the order
                self._ls_to_local[ls_order_id] = local_id
                self._local_to_ls[local_id] = ls_order_id

                logger.info(f"Created placeholder for manual order: local_id={local_id}, ls_order_id={ls_order_id}, symbol={symbol}")

        # Process any fills
        fill_sh = float(msg.get("fill_shares", 0.0))
        fill_px = float(msg.get("fill_price", 0.0))
        fill_ms = msg.get("fill_time_ms")  # ms since epoch or None
        fill_sec = (float(fill_ms) / 1000.0) if fill_ms is not None else time.time()

        if fill_sh > 0.0 and ls_order_id is not None:
            extras_for_fill = {
                k: v for k, v in msg.items()
                if k not in ("event", "ls_order_id", "local_copy_id", "status", "fill_shares", "fill_price")
            }
            partial_flag = "FullFill" if status in ("Filled", "FullFill") else "PartialFill"

            self._handle_fill(
                ls_order_id=ls_order_id,
                shares_filled=fill_sh,
                fill_price=fill_px,
                fill_time=fill_sec,
                partial_status=partial_flag,
                **extras_for_fill
            )

        # Update status
        if ls_order_id is not None and status:
            extras_for_status = {
                k: v for k, v in msg.items()
                if k not in ("event", "fill_shares", "fill_price", "ls_order_id", "status")
            }
            self._update_status(ls_order_id, status, **extras_for_status)

    def _handle_account_update(self, msg: Dict[str, Any]) -> None:
        """
        Handles account update messages from the broker.
        Updates the internal account data cache.

        Args:
            msg: The account update message
        """
        logger.debug(f"Received broker account_update message: {msg}")
        account_data_updates = {}

        # Common fields
        for field in ["open_pl", "net_pl", "equity", "buying_power", "margin_used", "long_value", "short_value", "net_dollar_value"]:
            if field in msg:
                try:
                    account_data_updates[field] = float(msg[field])
                except (ValueError, TypeError):
                    logger.warning(f"Failed to convert {field}='{msg[field]}' to float. Defaulting to 0.0.")
                    account_data_updates[field] = 0.0

        # --- MODIFIED CASH HANDLING ---
        if "running_balance" in msg: # Prioritize running_balance from C++ extension
            try:
                account_data_updates["cash"] = float(msg["running_balance"])
                logger.info(f"Account update: Using 'running_balance' ({msg['running_balance']}) as 'cash'.")
            except (ValueError, TypeError):
                logger.warning(f"Failed to convert running_balance='{msg['running_balance']}' to float. 'cash' may be incorrect.")
                # Fall through to check 'cash' field or buying_power if running_balance is invalid

        if "cash" not in account_data_updates: # If running_balance wasn't used or present
            if "cash" in msg: # Check for an explicit "cash" field in the message
                try:
                    account_data_updates["cash"] = float(msg["cash"])
                    logger.info(f"Account update: Using explicit 'cash' field ({msg['cash']}).")
                except (ValueError, TypeError):
                    logger.warning(f"Failed to convert explicit 'cash' field='{msg['cash']}' to float.")
                    # Fall through to buying_power if explicit 'cash' is invalid

            if "cash" not in account_data_updates and "buying_power" in account_data_updates: # Fallback to buying_power
                inferred_cash_from_bp = account_data_updates.get("buying_power", 0.0) # Use .get for safety
                logger.info(f"Account update: 'cash' and 'running_balance' not usable/present. Using 'buying_power' ({inferred_cash_from_bp}) as 'cash'.")
                account_data_updates["cash"] = inferred_cash_from_bp
            elif "cash" not in account_data_updates: # If none of the above worked
                 logger.warning(f"Account update: 'running_balance', 'cash', and 'buying_power' not usable/present. 'cash' will be missing or default.")
        # --- END MODIFIED CASH HANDLING ---

        # --- DAY P&L MAPPING (REALIZED P&L) ---
        if "closed_pl" in msg: # Map closed_pl from C++ extension to realized_pl for GUI
            try:
                account_data_updates["realized_pl"] = float(msg["closed_pl"])
                logger.info(f"Account update: Mapped 'closed_pl' ({msg['closed_pl']}) to 'realized_pl' for GUI display.")
            except (ValueError, TypeError):
                logger.warning(f"Failed to convert closed_pl='{msg['closed_pl']}' to float. 'realized_pl' may be incorrect.")
        # --- END DAY P&L MAPPING ---

        # Additional fields
        for field in ["account_id", "account_name", "account_type", "timestamp"]: # Removed 'base_bp', 'bp_in_use' as they are numeric
            if field in msg:
                account_data_updates[field] = msg[field]

        # Numeric fields like base_bp, bp_in_use if they are part of msg and not handled above
        for field in ["base_bp", "bp_in_use"]: # Add any other numeric fields directly from msg
            if field in msg:
                try:
                    account_data_updates[field] = float(msg[field])
                except (ValueError, TypeError):
                    logger.warning(f"Failed to convert {field}='{msg[field]}' to float. Skipping.")

        if "timestamp" not in account_data_updates:
            account_data_updates["timestamp"] = time.time()

        # Update the account data
        with self._lock:
            # Store previous values for change detection
            prev_open_pl = self._account_data.get("open_pl")
            prev_net_pl = self._account_data.get("net_pl")
            prev_equity = self._account_data.get("equity") # For logging change

            # Update the account data
            self._account_data.update(account_data_updates)

            # Store previous values for UI display
            if prev_open_pl is not None: self._account_data["last_open_pl"] = prev_open_pl
            if prev_net_pl is not None: self._account_data["last_net_pl"] = prev_net_pl

            # Update last update time
            self._account_data["last_update_time"] = time.time()

            # Set data_source more thoughtfully
            if self._account_data.get("data_source") != "repository_calculated_equity":
                if "running_balance" in msg or "cash" in msg: # If direct cash info was present
                    self._account_data["data_source"] = "broker_update_processed_with_cash_info"
                else: # Cash was inferred or missing
                    self._account_data["data_source"] = "broker_update_processed_cash_inferred_or_missing"

            # Debug counter for tracking update frequency
            self._account_data_debug_counter += 1
            if self._account_data_debug_counter % 1 == 0:  # Log every update for now
                current_equity = self._account_data.get('equity', 0.0)
                current_cash = self._account_data.get('cash', 'N/A') # Get current cash after update
                logger.info(
                    f"Account data updated (update #{self._account_data_debug_counter}): "
                    f"Prev Equity={prev_equity:.2f} -> New Equity={current_equity:.2f}, "
                    f"Cash={current_cash}, "
                    f"OpenPL={self._account_data.get('open_pl', 0.0):.2f}, "
                    f"NetPL={self._account_data.get('net_pl', 0.0):.2f}, "
                    f"BP={self._account_data.get('buying_power', 0.0):.2f}. "
                    f"Source: {self._account_data.get('data_source')}"
                )


    def _calculate_win_rate_internal(self) -> float:
        """Calculates the win rate based on finalized orders."""
        with self._lock:
            # Count total trades and winning trades
            total_trades = 0
            winning_trades = 0

            # Track processed trade IDs to avoid counting the same trade multiple times
            processed_trade_ids = set()

            # Iterate through all orders
            for _, order in self._copy_orders.items():
                # Skip orders that aren't final
                if order.get("ls_status") not in ("Filled", "Cancelled", "Rejected"):
                    continue

                # Get the parent trade ID
                trade_id = order.get("parent_trade_id")
                if trade_id is None or trade_id <= 0:
                    continue  # Skip orders without a valid parent trade

                # Skip trades we've already processed
                if trade_id in processed_trade_ids:
                    continue

                # Mark this trade as processed
                processed_trade_ids.add(trade_id)

                # Count this as a trade
                total_trades += 1

                # Calculate P&L for this trade
                trade_pnl = 0.0

                # Get all orders for this trade
                trade_orders = [o for _, o in self._copy_orders.items()
                               if o.get("parent_trade_id") == trade_id]

                # Calculate P&L from fills
                for order in trade_orders:
                    side = order.get("side", "").upper()
                    for fill in order.get("fills", []):
                        shares = fill.get("shares_filled", 0.0)
                        price = fill.get("fill_price", 0.0)

                        if side == "BUY":
                            trade_pnl -= shares * price  # Cost
                        elif side == "SELL":
                            trade_pnl += shares * price  # Revenue

                # Count as a winning trade if P&L is positive
                if trade_pnl > 0:
                    winning_trades += 1

            # Calculate win rate
            return winning_trades / total_trades if total_trades > 0 else 0.0

    def _get_trade_history_internal(self) -> List[Dict[str, Any]]:
        """Generates a summarized list of closed trades."""
        with self._lock:
            # Track processed trade IDs to avoid duplicates
            processed_trade_ids = set()
            trade_history = []

            # Iterate through all orders
            for _, order_obj in self._copy_orders.items():
                # Skip orders that aren't final
                if not hasattr(order_obj, 'ls_status') or order_obj.ls_status not in (OrderLSAgentStatus.FILLED, OrderLSAgentStatus.CANCELLED, OrderLSAgentStatus.REJECTED):
                    continue

                # Get the parent trade ID
                trade_id = order_obj.parent_trade_id if hasattr(order_obj, 'parent_trade_id') else None
                if trade_id is None or trade_id <= 0:
                    continue  # Skip orders without a valid parent trade

                # Skip trades we've already processed
                if trade_id in processed_trade_ids:
                    continue

                # Mark this trade as processed
                processed_trade_ids.add(trade_id)

                # Get all orders for this trade
                trade_orders = [o for _, o in self._copy_orders.items()
                               if hasattr(o, 'parent_trade_id') and o.parent_trade_id == trade_id]

                # Skip if no orders found (shouldn't happen)
                if not trade_orders:
                    continue

                # Calculate trade details
                symbol = trade_orders[0].symbol if hasattr(trade_orders[0], 'symbol') else "UNKNOWN"

                # Get entry time (earliest order timestamp)
                entry_times = []
                for o in trade_orders:
                    if hasattr(o, 'timestamp_requested') and o.timestamp_requested is not None:
                        entry_times.append(o.timestamp_requested)
                entry_time = min(entry_times) if entry_times else float('inf')

                # Get exit time (latest final fill time)
                exit_times = []
                for o in trade_orders:
                    if hasattr(o, 'final_fill_time') and o.final_fill_time is not None:
                        exit_times.append(o.final_fill_time)
                exit_time = max(exit_times) if exit_times else 0.0

                # Calculate P&L and shares
                trade_pnl = 0.0
                total_shares = 0.0

                for order_obj in trade_orders:
                    # Get the order side
                    if hasattr(order_obj, 'side'):
                        # Handle both string and enum values
                        if hasattr(order_obj.side, 'value'):
                            side = order_obj.side.value.upper()  # OrderSide enum
                        else:
                            side = str(order_obj.side).upper()  # String
                    else:
                        side = ""

                    # Process fills
                    fills = order_obj.fills if hasattr(order_obj, 'fills') else []
                    for fill_record in fills:
                        shares = fill_record.shares_filled if hasattr(fill_record, 'shares_filled') else 0.0
                        price = fill_record.fill_price if hasattr(fill_record, 'fill_price') else 0.0

                        if side == "BUY":
                            trade_pnl -= shares * price  # Cost
                            total_shares += shares
                        elif side == "SELL":
                            trade_pnl += shares * price  # Revenue
                            total_shares -= shares

                # Calculate duration with improved validation
                duration = 0.0  # Default
                if isinstance(entry_time, (int, float)) and isinstance(exit_time, (int, float)) and entry_time < float('inf') and exit_time > 0:
                    duration = exit_time - entry_time

                # Create trade history entry - use string values for dictionary keys
                # This is important because update_account_info_original expects this format
                trade_entry = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "duration": duration,
                    "pnl": trade_pnl,
                    "shares": total_shares,
                    "status": "CLOSED" if abs(total_shares) < 0.01 else "OPEN"
                }

                trade_history.append(trade_entry)

            # Sort by exit time, most recent first
            # Use 'or 0.0' to handle None values safely
            trade_history.sort(key=lambda x: x.get("exit_time") or 0.0, reverse=True)

            return trade_history

    # REMOVED: cancel_stale_orders_internal() - moved to StaleOrderService
    # REMOVED: All telemetry publishing methods - handled by telemetry service

    
    # --- Service Lifecycle and Health Monitoring ---
    def start(self):
        """Start the OrderRepository service."""
        logger.info("Starting OrderRepository...")
        
        # Start the IPC worker thread only if correlation logger not available
        # Check correlation logger availability:
        if not self._correlation_logger:
            self._start_ipc_worker()
            logger.info("OrderRepository using local IPC worker (correlation logger not available)")
        else:
            logger.info("OrderRepository using correlation logger for event logging")
        
        # Initialize health monitoring tracking
        self._is_running = True
        self._last_health_check_time = time.time()
        self._order_operations_count = 0
        self._order_failures = 0
        self._last_order_operation_time = 0.0
        
        # INSTRUMENTATION: Publish service health status - STARTED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderRepository",
                event_name="ServiceHealthStatusStarted",
                payload={
                    "status": "STARTED",
                    "message": "OrderRepository started successfully",
                    "context": {
                        "service_name": "OrderRepository",
                        "broker_service_available": self._broker is not None,
                        "position_manager_available": self._position_manager is not None,
                        "event_bus_available": self._event_bus is not None,
                        "trade_lifecycle_manager_available": self._trade_lifecycle_manager is not None,
                        "ipc_client_available": self._ipc_client is not None,
                        "orders_count": len(getattr(self, '_copy_orders', {})),
                        "filled_orders_count": len([o for o in getattr(self, '_copy_orders', {}).values() if getattr(o, 'ls_status', '') == 'Filled']),
                        "ipc_worker_thread_enabled": hasattr(self, '_ipc_worker_thread'),
                        "health_status": "HEALTHY",
                        "startup_timestamp": time.time()
                    }
                },
                stream_override=self._config_service.get_redis_stream_name('health_status', 'testrade:health-status')
            )
        
        logger.info("OrderRepository started successfully")
    
    def stop(self):
        """Stop the OrderRepository service."""
        self.shutdown()
    
    def shutdown(self):
        """Shutdown the OrderRepository and clean up resources."""
        logger.info("OrderRepository shutdown initiated")
        
        # REMOVED: Stale order cancellation thread handling moved to StaleOrderService
        
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
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderRepository",
                event_name="ServiceHealthStatusStopped",
                payload={
                    "status": "STOPPED",
                    "message": "OrderRepository stopped and cleaned up resources",
                    "context": {
                        "service_name": "OrderRepository",
                        "orders_count": len(getattr(self, '_copy_orders', {})),
                        "filled_orders_count": len([o for o in getattr(self, '_copy_orders', {}).values() if getattr(o, 'ls_status', '') == 'Filled']),
                        "discarded_ipc_messages": queue_size if hasattr(self, '_ipc_queue') else 0,
                        "stale_cancel_thread_shutdown": True,
                        "ipc_worker_thread_shutdown": True,
                        "health_status": "STOPPED",
                        "shutdown_timestamp": time.time()
                    }
                },
                stream_override=self._config_service.get_redis_stream_name('health_status', 'testrade:health-status')
            )
        
        logger.info("OrderRepository shutdown complete")



    def check_and_publish_health_status(self) -> None:
        """
        Public method to trigger health status check and publishing.
        Can be called periodically by external services or after order operations.
        """
        current_time = time.time()
        if not hasattr(self, '_last_health_check_time') or \
           (current_time - getattr(self, '_last_health_check_time', 0) > 30.0):  # Check every 30 seconds
            # Health status publishing removed - handled by health monitor service
            self._last_health_check_time = current_time
            
    def _track_order_operation_metrics(self, success: bool) -> None:
        """
        Track order operation metrics for health monitoring.
        
        Args:
            success: Whether the order operation was successful
        """
        if not hasattr(self, '_order_operations_count'):
            self._order_operations_count = 0
        if not hasattr(self, '_order_failures'):
            self._order_failures = 0
            
        self._order_operations_count += 1
        if not success:
            self._order_failures += 1
        self._last_order_operation_time = time.time()
    
    # ===== Methods moved to new services - kept for backward compatibility =====
    
    def get_all_orders(self) -> List["Order"]:
        """
        MOVED TO: OrderStateService and OrderQueryService
        Gets all orders from the internal state.
        """
        with self._lock:
            return list(self._copy_orders.values())
    
    def compute_leftover_shares_for_trade(self, trade_id: int) -> float:
        """
        MOVED TO: OrderQueryService
        Computes the total unfilled shares for a trade.
        """
        total_leftover = 0.0
        with self._lock:
            for order in self._copy_orders.values():
                if order.parent_trade_id == trade_id:
                    total_leftover += order.leftover_shares
        return total_leftover
    
    def get_cumulative_filled_for_trade(self, parent_trade_id: int) -> float:
        """
        MOVED TO: OrderQueryService
        Computes the total filled shares for all orders associated with a trade.
        """
        total_filled = 0.0
        with self._lock:
            for order in self._copy_orders.values():
                if order.parent_trade_id == parent_trade_id:
                    total_filled += order.filled_quantity
        return total_filled
    
    def get_order_by_broker_id(self, broker_order_id: str) -> Optional["Order"]:
        """
        MOVED TO: OrderLinkingService
        Gets an order by its broker ID.
        """
        try:
            broker_id_int = int(broker_order_id)
            with self._lock:
                local_id = self._ls_to_local.get(broker_id_int)
                if local_id:
                    return self._copy_orders.get(local_id)
        except ValueError:
            logger.warning(f"Invalid broker_order_id format: {broker_order_id}")
        return None
    
    def get_order_by_ephemeral_corr(self, ephemeral_corr: str) -> Optional["Order"]:
        """
        MOVED TO: OrderLinkingService
        Gets an order by its ephemeral correlation ID.
        """
        try:
            ephemeral_id = int(ephemeral_corr)
            with self._lock:
                local_id = self._correlation_to_local.get(ephemeral_id)
                if local_id:
                    return self._copy_orders.get(local_id)
        except ValueError:
            logger.warning(f"Invalid ephemeral_corr format: {ephemeral_corr}")
        return None
    
    def is_order_final(self, local_id: int) -> bool:
        """
        MOVED TO: OrderQueryService
        Checks if an order is in a final state.
        """
        with self._lock:
            order = self._copy_orders.get(local_id)
            if order:
                return order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
        return False
    
    def link_ls_order(self,
                      local_id: int,
                      ls_order_id: int,
                      is_ephemeral: bool = False,
                      perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        """
        MOVED TO: OrderLinkingService
        Links a local order to a broker order ID.
        """
        with self._lock:
            if is_ephemeral:
                self._ephemeral_ls_ids.add(ls_order_id)
                self._correlation_to_local[ls_order_id] = local_id
            else:
                self._ls_to_local[ls_order_id] = local_id
                self._local_to_ls[local_id] = ls_order_id
                
            # Update the order object
            order = self._copy_orders.get(local_id)
            if order and order.ls_order_id != ls_order_id:
                updated_order = order._replace(ls_order_id=ls_order_id)
                self._copy_orders[local_id] = updated_order
    
    def start_auto_cancel_stale_pending(self, interval: float = 5.0, max_age: float = 11.0) -> None:
        """
        MOVED TO: StaleOrderService
        This method is kept for backward compatibility.
        """
        logger.warning("DEPRECATED: start_auto_cancel_stale_pending() has been moved to StaleOrderService")
        # No-op implementation for backward compatibility
    
    def stop_auto_cancel_stale_pending(self) -> None:
        """
        MOVED TO: StaleOrderService
        This method is kept for backward compatibility.
        """
        logger.warning("DEPRECATED: stop_auto_cancel_stale_pending() has been moved to StaleOrderService")
        # No-op implementation for backward compatibility
