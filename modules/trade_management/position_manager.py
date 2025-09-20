# modules/trade_management/position_manager.py
import logging
import threading
import copy
import time
import uuid
import queue
import json
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# Import core interfaces
from interfaces.core.services import IEventBus
from interfaces.logging.services import ICorrelationLogger
from interfaces.order_management.services import IOrderRepository
from interfaces.core.services import IConfigService
from interfaces.data.services import IPriceProvider
from interfaces.core.services import IBulletproofBabysitterIPCClient
from interfaces.trading.services import IPositionManager, ITradeLifecycleManager

# Import event classes and enums
from core.events import OrderFilledEvent, PositionUpdateEvent, PositionUpdateEventData, BrokerPositionQuantityOnlyPushEvent, BrokerFullPositionPushEvent
from data_models import OrderSide

logger = logging.getLogger(__name__)

class PositionManager(IPositionManager):
    def __init__(self, event_bus: IEventBus,
                 correlation_logger: Optional[ICorrelationLogger] = None,
                 trade_lifecycle_manager: Optional[ITradeLifecycleManager] = None,
                 order_repository: Optional[IOrderRepository] = None,
                 config_service: Optional[IConfigService] = None,
                 price_provider: Optional[IPriceProvider] = None,
                 ipc_client: Optional[IBulletproofBabysitterIPCClient] = None,
                 broker_service: Optional[Any] = None):
        """
        Initialize the PositionManager with EventBus integration.

        Args:
            event_bus: IEventBus instance for publishing and subscribing to events
            correlation_logger: Optional correlation logger for event tracking
            trade_lifecycle_manager: Optional TradeLifecycleManager for parent trade ID resolution
            order_repository: Optional OrderRepository for direct order access (preferred DI approach)
            config_service: Optional ConfigService for configuration access
            price_provider: Optional IPriceProvider for price access
            ipc_client: Optional IPC client for legacy support
            broker_service: Optional IBrokerService for initial position loading during startup
        """
        # Initialize logger for this instance
        self.logger = logging.getLogger(__name__)

        # Store dependencies
        self.event_bus = event_bus
        self._correlation_logger = correlation_logger
        self.trade_lifecycle_manager = trade_lifecycle_manager
        self._order_repository = order_repository
        self._config_service = config_service
        self._price_provider = price_provider
        self._ipc_client = ipc_client
        self._broker_service = broker_service

        # Check for CorrelationLogger availability
        if not self._correlation_logger:
            self.logger.warning("PositionManager initialized WITHOUT CorrelationLogger. Event logging will be disabled.")
        else:
            self.logger.info("PositionManager initialized WITH CorrelationLogger for event logging.")

        if not self.trade_lifecycle_manager:
            self.logger.warning("PositionManager initialized WITHOUT TradeLifecycleManager. Parent trade ID resolution for bootstrap will be limited/fallback.")

        # Initialize in-memory storage for positions
        self._positions_lock = threading.RLock()
        # Key: Symbol (UPPERCASE), Value: Dict containing position state
        self._positions: Dict[str, Dict[str, Any]] = {}

        # Subscribe to EventBus events
        self._subscribe_to_events()
        
        # Initialize async IPC publishing infrastructure
        self._ipc_queue: queue.Queue[Tuple[str, str, str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        self._ipc_failures = 0
        self._ipc_circuit_open = False
        self._ipc_circuit_threshold = 10  # Number of failures before opening circuit
        # Thread creation deferred to start() method
        
        # Position validation metrics
        self._validation_errors_count = 0
        self._last_validation_error_time = 0.0

        self.logger.info("PositionManager initialized with EventBus integration and async IPC publishing.")


    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="PositionManager-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
    
    def _ipc_worker_loop(self):
        """Worker loop that processes IPC messages asynchronously."""
        # --- FINAL WORKER THREAD HARDENING ---
        # Add a brief startup delay to ensure all other core services
        # have completed their own startup routines before this worker
        # begins intensive IPC operations.
        # This is a non-blocking delay within the worker thread itself.
        default_delay = 5.0
        startup_delay = default_delay
        
        # Try to get config value if available
        if self._config_service:
            startup_delay = float(getattr(self._config_service, 'PM_WORKER_STARTUP_DELAY_SEC', default_delay))
        
        self.logger.info(f"PositionManager IPC worker starting with {startup_delay}s startup delay to ensure system readiness")
        time.sleep(startup_delay)
        # --- END OF HARDENING ---
        
        while not self._ipc_shutdown_event.is_set():
            try:
                # INSTRUMENTATION: Periodic health status monitoring
                current_time = time.time()
                if not hasattr(self, '_last_health_status_time_pm') or \
                   (current_time - getattr(self, '_last_health_status_time_pm', 0) > 30.0):  # Health check every 30 seconds
                    try:
                        # Publish periodic health status
                        if self._correlation_logger:
                            # Determine current health status based on operational metrics
                            ipc_queue_size = self._ipc_queue.qsize() if hasattr(self, '_ipc_queue') else 0
                            queue_capacity_pct = (ipc_queue_size / self._ipc_queue.maxsize) * 100 if self._ipc_queue.maxsize > 0 else 0
                            
                            # Check various health indicators
                            health_status = "HEALTHY"
                            status_issues = []
                            
                            # IPC queue capacity check
                            if queue_capacity_pct > 90:
                                health_status = "CRITICAL"
                                status_issues.append(f"IPC queue at {queue_capacity_pct:.1f}% capacity")
                            elif queue_capacity_pct > 80:
                                health_status = "DEGRADED"
                                status_issues.append(f"IPC queue at {queue_capacity_pct:.1f}% capacity")
                            
                            # Circuit breaker check
                            if self._ipc_circuit_open:
                                health_status = "DEGRADED"
                                status_issues.append("IPC circuit breaker open")
                            
                            # IPC worker thread health check
                            if not self._ipc_worker_thread or not self._ipc_worker_thread.is_alive():
                                health_status = "DEGRADED"
                                status_issues.append("IPC worker thread not running")
                            
                            # High failure rate check
                            if self._ipc_failures > self._ipc_circuit_threshold / 2:
                                if health_status != "CRITICAL":
                                    health_status = "DEGRADED"
                                status_issues.append(f"High IPC failure count: {self._ipc_failures}")
                            
                            status_reason = f"Service operational - {', '.join(status_issues) if status_issues else 'All systems healthy'}"
                            
                            # Build context with performance metrics
                            context = {
                                "service_name": "PositionManager",
                                "health_status": health_status,
                                "status_reason": status_reason,
                                "ipc_queue_size": ipc_queue_size,
                                "ipc_queue_capacity_pct": round(queue_capacity_pct, 2),
                                "ipc_queue_maxsize": self._ipc_queue.maxsize,
                                "ipc_failures": self._ipc_failures,
                                "ipc_circuit_open": self._ipc_circuit_open,
                                "ipc_worker_alive": self._ipc_worker_thread and self._ipc_worker_thread.is_alive(),
                                "positions_count": len(self._positions),
                                "trade_lifecycle_manager_available": self.trade_lifecycle_manager is not None,
                                "validation_errors_count": getattr(self, '_validation_errors_count', 0),
                                "last_validation_error_time": getattr(self, '_last_validation_error_time', 0.0),
                                "health_check_timestamp": time.time()
                            }
                            
                            self._correlation_logger.log_event(
                                source_component="PositionManager",
                                event_name="TestradeServiceHealthStatus",
                                payload=context
                            )
                        setattr(self, '_last_health_status_time_pm', current_time)
                    except Exception as e:
                        self.logger.warning(f"Failed to publish periodic health status: {e}")
                
                # Get message from queue with timeout
                try:
                    target_stream, redis_message, symbol, update_source = self._ipc_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Handle special health status messages
                if target_stream == "__HEALTH_STATUS__":
                    try:
                        health_data = json.loads(redis_message)
                        if self._correlation_logger:
                            health_payload = {
                                "health_status": health_data.get("health_status", "UNKNOWN"),
                                "status_reason": health_data.get("status_reason", "Unknown reason"),
                                "context": health_data.get("context", {}),
                                "component": "PositionManager",
                                "health_timestamp": time.time()
                            }
                            self._correlation_logger.log_event(
                                source_component="PositionManager",
                                event_name="TestradeServiceHealthStatus",
                                payload=health_payload
                            )
                        continue
                    except Exception as e:
                        self.logger.error(f"Error processing queued health status: {e}")
                        continue
                
                # Skip if circuit breaker is open
                if self._ipc_circuit_open:
                    if self._ipc_failures < self._ipc_circuit_threshold * 2:  # Try to recover
                        self.logger.debug(f"IPC circuit breaker open, discarding message for {symbol}")
                        continue
                    else:
                        # Try to reset circuit breaker
                        self._ipc_circuit_open = False
                        self._ipc_failures = 0
                        self.logger.info("Attempting to reset IPC circuit breaker")
                
                # Attempt to send via IPC
                try:
                    # Legacy IPC replaced with telemetry service
                    if self._correlation_logger:
                        # Need to parse the redis_message to extract payload for telemetry service
                        import json
                        try:
                            msg_data = json.loads(redis_message)
                            payload = msg_data.get('payload', {})
                            if self._correlation_logger:
                                self._correlation_logger.log_event(
                                    source_component="PositionManager",
                                    event_name="PositionUpdate",
                                    payload=payload
                                )
                                success = True
                            else:
                                success = False
                        except json.JSONDecodeError:
                            logger.error(f"Failed to parse Redis message for telemetry service")
                            success = False
                        if success:
                            self._ipc_failures = 0  # Reset failure count on success
                            self.logger.debug(f"Async IPC: Published position update for {symbol}")
                        else:
                            self._ipc_failures += 1
                            self.logger.warning(f"Async IPC: Failed to publish position update for {symbol}")
                    else:
                        self.logger.debug("Async IPC: No IPC client available")
                        
                except Exception as e:
                    self._ipc_failures += 1
                    self.logger.error(f"Async IPC: Error publishing position update: {e}")
                
                # Open circuit breaker if too many failures
                if self._ipc_failures >= self._ipc_circuit_threshold:
                    self._ipc_circuit_open = True
                    self.logger.error(f"IPC circuit breaker opened after {self._ipc_failures} failures")
                    
            except Exception as e:
                self.logger.error(f"Error in IPC worker loop: {e}", exc_info=True)
                
        self.logger.info("IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str, symbol: str, update_source: str):
        """Queue an IPC message for async publishing via telemetry service."""
        try:
            # Parse the message to extract event type
            import json
            msg_data = json.loads(redis_message)
            event_type = msg_data.get('metadata', {}).get('eventType', 'UNKNOWN')
            payload_data = msg_data.get('payload', {})
            
            # Use correlation logger for clean, fire-and-forget publishing
            if self._correlation_logger:
                # Convert event_type to CamelCase
                event_name = event_type.replace('_', ' ').title().replace(' ', '')
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name=event_name,
                    payload=payload_data
                )
        except Exception as e:
            self.logger.error(f"Error queuing to telemetry service: {e}")
            # Fallback to local queue if available
            try:
                self._ipc_queue.put_nowait((target_stream, redis_message, symbol, update_source))
            except queue.Full:
                    self.logger.warning(f"Local IPC queue also full, dropping position update for {symbol}")
        else:
            # No central queue, use local queue
            try:
                self._ipc_queue.put_nowait((target_stream, redis_message, symbol, update_source))
            except queue.Full:
                self.logger.warning(f"IPC queue full, dropping position update for {symbol}")
    
    def _classify_retry_event_severity(self, event_type: str) -> str:
        """Classify retry event severity based on event type."""
        critical_events = {"MAX_RETRIES_REACHED", "RECOVERY_FAILED", "PERMANENT_FAILURE"}
        warning_events = {"RETRY_ATTEMPT", "COOLDOWN_INITIATED", "RECOVERY_INITIATED"}
        info_events = {"RETRY_SUCCESS", "RECOVERY_SUCCESS", "STATE_RESET"}
        
        if event_type in critical_events:
            return "CRITICAL"
        elif event_type in warning_events:
            return "WARNING"
        elif event_type in info_events:
            return "INFO"
        else:
            return "WARNING"
    
    def _subscribe_to_events(self):
        """Subscribe to relevant events from the EventBus."""
        # Create wrapper function to prevent garbage collection with WeakSet
        def order_fill_wrapper(event):
            return self.handle_order_fill(event)

        def broker_quantity_push_wrapper(event):
            return self._handle_broker_quantity_only_push(event)

        def broker_full_position_push_wrapper(event):
            return self._handle_broker_full_position_push(event)

        # Store wrapper as instance variable to prevent garbage collection
        self._order_fill_handler = order_fill_wrapper
        self._broker_quantity_push_handler = broker_quantity_push_wrapper
        self._broker_full_position_push_handler = broker_full_position_push_wrapper

        self.event_bus.subscribe(OrderFilledEvent, self._order_fill_handler)
        # --- AGENT_DEBUG: START ---
        self.logger.critical(f"AGENT_DEBUG_PM_SUBSCRIBE: Subscribed to OrderFilledEvent. Type Object ID: {id(OrderFilledEvent)}")
        # --- AGENT_DEBUG: END ---

        self.event_bus.subscribe(BrokerPositionQuantityOnlyPushEvent, self._broker_quantity_push_handler)
        # --- AGENT_DEBUG: START ---
        self.logger.critical(f"AGENT_DEBUG_PM_SUBSCRIBE: Subscribed to BrokerPositionQuantityOnlyPushEvent. Type Object ID: {id(BrokerPositionQuantityOnlyPushEvent)}")
        # --- AGENT_DEBUG: END ---

        self.event_bus.subscribe(BrokerFullPositionPushEvent, self._broker_full_position_push_handler)
        # --- AGENT_DEBUG: START ---
        self.logger.critical(f"AGENT_DEBUG_PM_SUBSCRIBE: Subscribed to BrokerFullPositionPushEvent. Type Object ID: {id(BrokerFullPositionPushEvent)}")
        # --- AGENT_DEBUG: END ---

        self.logger.critical("POSITION_MANAGER_INIT: PositionManager subscribed to OrderFilledEvent")
        self.logger.critical("POSITION_MANAGER_INIT: PositionManager subscribed to BrokerPositionQuantityOnlyPushEvent")
        self.logger.critical("POSITION_MANAGER_INIT: PositionManager subscribed to BrokerFullPositionPushEvent")
        self.logger.critical(f"POSITION_MANAGER_INIT: EventBus instance: {type(self.event_bus).__name__} at {id(self.event_bus)}")
        self.logger.critical(f"POSITION_MANAGER_INIT: PositionManager instance: {type(self).__name__} at {id(self)}")

    def handle_order_fill(self, event: OrderFilledEvent):
        """
        Handles OrderFilledEvent from the EventBus.
        Updates position based on fill data.
        This logic is a direct port and adaptation of the legacy PositionManager.process_fill.
        """
        # Extract correlation ID if available
        correlation_id = getattr(event.data, 'correlation_id', None) if hasattr(event, 'data') and event.data else None
        self.logger.info(f"[POSITION_MANAGER] Processing OrderFilledEvent with CorrID: {correlation_id}, EventID: {event.event_id}")
        # --- AGENT_DEBUG: START ---
        event_symbol_pm_fill = "NO_SYMBOL"
        if hasattr(event, 'data') and event.data is not None and hasattr(event.data, 'symbol') and event.data.symbol is not None:
            event_symbol_pm_fill = event.data.symbol
        elif hasattr(event, 'symbol') and event.symbol is not None:
            event_symbol_pm_fill = event.symbol

        self.logger.critical(
            f"AGENT_DEBUG_PM_HANDLER_ENTERED: Method=handle_order_fill, EventType={type(event).__name__}, "
            f"Symbol={event_symbol_pm_fill}, EventID={getattr(event, 'event_id', 'N/A')}, "
            f"EventTypeID={id(type(event))}"
        )
        # --- AGENT_DEBUG: END ---
        try:
            data = event.data
            
            # Capture correlation context
            master_correlation_id = getattr(event, 'correlation_id', None)
            # The ID of the current event is the cause of the next event
            causation_id_for_next_step = getattr(event, 'event_id', None)
            
            if not master_correlation_id:
                master_correlation_id = str(uuid.uuid4())
                self.logger.warning(f"Missing correlation_id on event {type(event).__name__}. Generated new one: {master_correlation_id}")
            
            symbol_upper = data.symbol.upper()
            # data.side is OrderSide enum; data.fill_quantity & data.fill_price are floats
            # data.local_order_id is Optional[str]; data.fill_timestamp is float

            # DEBUG: Intercept and log the full OrderFilledEvent payload
            self.logger.critical(f"[DEBUG] FILL PAYLOAD DEBUG: OrderFilledEvent")
            self.logger.critical(f"[DEBUG] FILL PAYLOAD: event.data = {data}")
            self.logger.critical(f"[DEBUG] FILL PAYLOAD: event.timestamp = {event.timestamp}")
            self.logger.critical(f"[DEBUG] FILL PAYLOAD: event.event_id = {getattr(event, 'event_id', 'N/A')}")
            self.logger.critical(f"[DEBUG] FILL PAYLOAD: event.correlation_id = {getattr(event, 'correlation_id', 'N/A')}")
            self.logger.critical(f"[DEBUG] FILL PAYLOAD: Available attributes: {dir(data)}")

            self.logger.critical(f"POSITION_MGR_LIFECYCLE: Received OrderFilledEvent. EventData={data}")

            with self._positions_lock:
                pos = self._get_position_no_lock(symbol_upper) # Gets or initializes position dict

                old_total_shares = float(pos.get("total_shares", 0.0))
                old_total_avg_price = float(pos.get("total_avg_price", 0.0))
                old_strategy = str(pos.get("strategy", "closed"))
                # overall_realized_pnl should already exist in pos from _get_position_no_lock
                pnl_contribution_this_fill = 0.0

                fill_qty = float(data.fill_quantity)
                fill_px = float(data.fill_price)
                side_enum_val = data.side # This is already an OrderSide enum from the event
                
                # INSTRUMENTATION: Validate calculation inputs for position calculation errors
                calculation_errors = []
                
                # Check for invalid fill quantity
                if fill_qty <= 0 or not isinstance(fill_qty, (int, float)) or fill_qty != fill_qty:  # NaN check
                    calculation_errors.append(f"Invalid fill quantity: {fill_qty}")
                
                # Check for invalid fill price  
                if fill_px <= 0 or not isinstance(fill_px, (int, float)) or fill_px != fill_px:  # NaN check
                    calculation_errors.append(f"Invalid fill price: {fill_px}")
                
                # Check for extremely large values that could cause overflow
                if abs(fill_qty) > 1e12 or abs(fill_px) > 1e12:
                    calculation_errors.append(f"Extremely large values: qty={fill_qty}, px={fill_px}")
                
                # If calculation validation errors found, publish and abort
                if calculation_errors:
                    error_details = "; ".join(calculation_errors)
                    self.logger.error(f"Position calculation validation failed for {symbol_upper}: {error_details}")
                    
                    # INSTRUMENTATION: Publish invalid calculation input error
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionCalculationError",
                            payload={
                                "error_type": "INVALID_CALCULATION_INPUTS",
                                "error_reason": f"Invalid calculation inputs detected - {error_details}",
                                "context": {
                                    "symbol": symbol_upper,
                                    "fill_quantity": fill_qty,
                                    "fill_price": fill_px,
                                    "validation_errors": calculation_errors,
                                    "local_order_id": data.local_order_id,
                                    "fill_timestamp": data.fill_timestamp,
                                    "old_total_shares": old_total_shares,
                                    "old_total_avg_price": old_total_avg_price,
                                    "calculation_aborted": True,
                                    "error_severity": "CRITICAL"
                                },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )
                    return

                if side_enum_val == OrderSide.BUY:
                    new_total_shares = old_total_shares + fill_qty
                    if abs(new_total_shares) > 1e-9:
                        # INSTRUMENTATION: Wrap average price calculation in error detection
                        try:
                            numerator = (old_total_shares * old_total_avg_price) + (fill_qty * fill_px)
                            new_total_avg_price = numerator / new_total_shares
                            
                            # Check for calculation errors
                            if (new_total_avg_price != new_total_avg_price or  # NaN check
                                abs(new_total_avg_price) > 1e12 or  # Overflow check
                                new_total_avg_price <= 0):  # Invalid price check
                                
                                if self._correlation_logger:
                                    self._correlation_logger.log_event(
                                        source_component="PositionManager",
                                        event_name="TestradePositionCalculationError",
                                        payload={
                                            "error_type": "AVERAGE_PRICE_CALCULATION_ERROR",
                                            "error_reason": f"Invalid average price calculated: {new_total_avg_price}",
                                            "context": {
                                        "symbol": symbol_upper,
                                        "calculation_type": "BUY_AVERAGE_PRICE",
                                        "old_total_shares": old_total_shares,
                                        "old_total_avg_price": old_total_avg_price,
                                        "fill_qty": fill_qty,
                                        "fill_px": fill_px,
                                        "new_total_shares": new_total_shares,
                                        "calculated_avg_price": new_total_avg_price,
                                        "numerator": numerator,
                                        "calculation_fallback_used": True,
                                        "error_severity": "HIGH"
                                    },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )
                                new_total_avg_price = fill_px  # Fallback to fill price
                        except (ZeroDivisionError, OverflowError, ArithmeticError) as calc_error:
                            if self._correlation_logger:
                                self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionCalculationError",
                            payload={
                                "error_type": "ARITHMETIC_ERROR_IN_CALCULATION",
                                "error_reason": f"Arithmetic error in BUY average price calculation: {type(calc_error).__name__}",
                                "context": {
                                    "symbol": symbol_upper,
                                    "calculation_type": "BUY_AVERAGE_PRICE",
                                    "old_total_shares": old_total_shares,
                                    "old_total_avg_price": old_total_avg_price,
                                    "fill_qty": fill_qty,
                                    "fill_px": fill_px,
                                    "new_total_shares": new_total_shares,
                                    "arithmetic_error": str(calc_error),
                                    "error_type": type(calc_error).__name__,
                                    "calculation_fallback_used": True,
                                    "error_severity": "HIGH"
                                },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )
                            new_total_avg_price = fill_px  # Fallback to fill price
                    else:
                        new_total_avg_price = 0.0 # Should not happen if buying to non-zero
                elif side_enum_val == OrderSide.SELL:
                    new_total_shares = old_total_shares - fill_qty
                    if abs(old_total_avg_price) > 1e-9: # If we had a cost basis
                        # INSTRUMENTATION: Wrap PnL calculation in error detection
                        try:
                            pnl_contribution_this_fill = (fill_px - old_total_avg_price) * fill_qty
                            current_realized_pnl = float(pos.get("overall_realized_pnl", 0.0))
                            new_realized_pnl = current_realized_pnl + pnl_contribution_this_fill
                            
                            # Check for PnL calculation errors
                            if (new_realized_pnl != new_realized_pnl or  # NaN check
                                abs(new_realized_pnl) > 1e15):  # Extremely large PnL check
                                
                                if self._correlation_logger:
                                    self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionCalculationError",
                            payload={
                                "error_type": "PNL_CALCULATION_OVERFLOW",
                                "error_reason": f"PnL calculation resulted in invalid value: {new_realized_pnl}",
                                "context": {
                                        "symbol": symbol_upper,
                                        "calculation_type": "SELL_PNL",
                                        "fill_px": fill_px,
                                        "old_total_avg_price": old_total_avg_price,
                                        "fill_qty": fill_qty,
                                        "pnl_contribution": pnl_contribution_this_fill,
                                        "current_realized_pnl": current_realized_pnl,
                                        "calculated_new_pnl": new_realized_pnl,
                                        "calculation_fallback_used": True,
                                        "error_severity": "MEDIUM"
                                    },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )
                                # Keep original PnL if calculation fails
                            else:
                                pos["overall_realized_pnl"] = new_realized_pnl
                        except (OverflowError, ArithmeticError) as calc_error:
                            if self._correlation_logger:
                                self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionCalculationError",
                            payload={
                                "error_type": "ARITHMETIC_ERROR_IN_PNL",
                                "error_reason": f"Arithmetic error in SELL PnL calculation: {type(calc_error).__name__}",
                                "context": {
                                    "symbol": symbol_upper,
                                    "calculation_type": "SELL_PNL",
                                    "fill_px": fill_px,
                                    "old_total_avg_price": old_total_avg_price,
                                    "fill_qty": fill_qty,
                                    "arithmetic_error": str(calc_error),
                                    "error_type": type(calc_error).__name__,
                                    "calculation_fallback_used": True,
                                    "error_severity": "MEDIUM"
                                },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )

                    new_total_avg_price = old_total_avg_price # Avg price doesn't change on sell...
                    if abs(new_total_shares) < 1e-9: # ...unless position becomes flat
                        new_total_avg_price = 0.0
                        # Optional: If strategy was 'short' and now flat, PnL calculation might differ for shorts.
                        # Since we are "long only" for now, this is fine.
                else:
                    self.logger.error(f"POSITION_MGR: Unknown side '{str(side_enum_val)}' in handle_order_fill for {symbol_upper}")
                    
                    # INSTRUMENTATION: Publish unknown side validation failure
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionValidationDecision",
                            payload={
                                "decision_type": "UNKNOWN_ORDER_SIDE",
                                "decision_reason": f"Unknown order side '{str(side_enum_val)}' in fill processing - position calculation aborted",
                                "context": {
                            "symbol": symbol_upper,
                            "invalid_side": str(side_enum_val),
                            "fill_quantity": fill_qty,
                            "fill_price": fill_px,
                            "local_order_id": data.local_order_id,
                            "fill_timestamp": data.fill_timestamp,
                            "old_total_shares": old_total_shares,
                            "old_total_avg_price": old_total_avg_price,
                            "validation_severity": "HIGH"
                        },
                                "validation_timestamp": time.time(),
                                "total_validation_errors": self._validation_errors_count,
                                "component": "PositionManager"
                            }
                        )
                    # Position validation metrics
                    self._validation_errors_count += 1
                    self._last_validation_error_time = time.time()
                    return

                pos["total_shares"] = new_total_shares
                pos["total_avg_price"] = new_total_avg_price
                pos["open"] = abs(new_total_shares) > 1e-9

                # INSTRUMENTATION: Log position state determination
                self.logger.critical(f"PM_POSITION_STATE: {symbol_upper} after fill processing. "
                                   f"total_shares={new_total_shares:.1f}, is_open={pos['open']}, "
                                   f"abs(shares)={abs(new_total_shares):.1f}, threshold=1e-9")

                new_strategy = "long" if new_total_shares > 1e-9 else \
                               ("short" if new_total_shares < -1e-9 else "closed") # Basic logic for long/short/closed
                pos["strategy"] = new_strategy

                # --- CAPTURE CORRELATION IDs FROM ORDER ---
                lcm_parent_trade_id_for_pos = pos.get("lcm_parent_trade_id")
                master_correlation_id_for_pos = pos.get("master_correlation_id")

                # Fetch the Order object to get its parent_trade_id (LCM t_id) and master_correlation_id (IntelliSense UUID)
                # Use injected OrderRepository instead of accessing through ApplicationCore
                if data.local_order_id and self._order_repository:
                    try:
                        order_obj = self._order_repository.get_order_by_local_id(int(data.local_order_id))
                        if order_obj:
                            if order_obj.parent_trade_id:  # This is LCM t_id
                                lcm_parent_trade_id_for_pos = order_obj.parent_trade_id
                                pos["lcm_parent_trade_id"] = lcm_parent_trade_id_for_pos
                            if order_obj.master_correlation_id:  # This is IntelliSense UUID
                                master_correlation_id_for_pos = order_obj.master_correlation_id
                                pos["master_correlation_id"] = master_correlation_id_for_pos
                            self.logger.info(f"PM Fill: For order {data.local_order_id}, associated LCM TID: {lcm_parent_trade_id_for_pos}, MasterCorrID: {master_correlation_id_for_pos}")
                        else:
                            self.logger.warning(f"PM Fill: Order {data.local_order_id} not found in OrderRepository")
                            
                            # INSTRUMENTATION: Publish order not found validation failure
                            if self._correlation_logger:
                                self._correlation_logger.log_event(
                                    source_component="PositionManager",
                                    event_name="TestradePositionValidationDecision",
                                    payload={
                                        "decision_type": "ORDER_NOT_FOUND_IN_REPOSITORY",
                                        "decision_reason": f"Order {data.local_order_id} not found in OrderRepository during fill processing - missing correlation data",
                                        "context": {
                                            "symbol": symbol_upper,
                                            "local_order_id": data.local_order_id,
                                            "fill_quantity": fill_qty,
                                            "fill_price": fill_px,
                                            "fill_timestamp": data.fill_timestamp,
                                            "current_lcm_trade_id": lcm_parent_trade_id_for_pos,
                                            "current_master_correlation_id": master_correlation_id_for_pos,
                                            "validation_severity": "MEDIUM"
                                        },
                                        "validation_timestamp": time.time(),
                                        "total_validation_errors": self._validation_errors_count,
                                        "component": "PositionManager"
                                    }
                                )
                                # Position validation metrics
                                self._validation_errors_count += 1
                                self._last_validation_error_time = time.time()
                    except Exception as e:
                        self.logger.error(f"PM Fill: Error fetching order {data.local_order_id}: {e}")
                        
                        # INSTRUMENTATION: Publish order fetch error validation failure
                        if self._correlation_logger:
                            self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionValidationDecision",
                            payload={
                                "decision_type": "ORDER_FETCH_ERROR",
                                "decision_reason": f"Error fetching order {data.local_order_id} from repository during fill processing",
                                "context": {
                                "symbol": symbol_upper,
                                "local_order_id": data.local_order_id,
                                "fill_quantity": fill_qty,
                                "fill_price": fill_px,
                                "fill_timestamp": data.fill_timestamp,
                                "error_message": str(e),
                                "error_type": type(e).__name__,
                                "validation_severity": "HIGH"
                            },
                                "validation_timestamp": time.time(),
                                "total_validation_errors": self._validation_errors_count,
                                "component": "PositionManager"
                            }
                        )
                    # Position validation metrics
                    self._validation_errors_count += 1
                    self._last_validation_error_time = time.time()
                else:
                    self.logger.debug(f"PM Fill: Cannot fetch order details - missing local_order_id or order_repository")

                # Record this fill in the ledger (ensure this structure matches what _get_position_no_lock initializes)
                if "session_system_fills_ledger" not in pos: pos["session_system_fills_ledger"] = [] # Defensive
                pos["session_system_fills_ledger"].append({
                    "timestamp": data.fill_timestamp,
                    "type": side_enum_val.value, # Store string value of enum
                    "shares": fill_qty,
                    "price": fill_px,
                    "pnl_contribution": pnl_contribution_this_fill,
                    "local_order_id": data.local_order_id, # From event
                    "source": "eventbus_fill" # Mark source
                })

                # Update enhanced metadata

                # CRITICAL FIX: Generate position UUID based on parent trade ID if available
                if "position_uuid" not in pos or pos["position_uuid"] is None:
                    if lcm_parent_trade_id_for_pos:
                        pos["position_uuid"] = f"{symbol_upper}-{lcm_parent_trade_id_for_pos}"
                    else:
                        pos["position_uuid"] = str(uuid.uuid4())

                # Track position opening
                if "position_opened_timestamp" not in pos or pos["position_opened_timestamp"] is None:
                    if abs(old_total_shares) < 1e-9 and abs(new_total_shares) > 1e-9:  # Position just opened
                        pos["position_opened_timestamp"] = data.fill_timestamp
                        pos["position_opened_timestamp_ns"] = int(data.fill_timestamp * 1_000_000_000)
                        # Store the correlation ID that opened this position
                        pos["opening_signal_correlation_id"] = getattr(event, 'correlation_id', None)
                        pos["opening_order_correlation_id"] = data.local_order_id

                pos["last_fill_timestamp"] = data.fill_timestamp
                pos["last_fill_timestamp_ns"] = int(data.fill_timestamp * 1_000_000_000)
                pos["total_fills_count"] = pos.get("total_fills_count", 0) + 1

                # Track most recent signal/order correlation IDs
                pos["last_signal_correlation_id"] = getattr(event, 'correlation_id', None)
                pos["last_order_correlation_id"] = data.local_order_id

                # Calculate latencies (if we have the data)
                if hasattr(event, 'timestamp') and hasattr(data, 'fill_timestamp'):
                    signal_to_fill_latency = (data.fill_timestamp - event.timestamp) * 1000  # Convert to ms
                    pos["signal_to_fill_latency_ms"] = signal_to_fill_latency

                # Track session buy/sell quantities and values
                if side_enum_val == OrderSide.BUY:
                    pos["session_buy_quantity"] = pos.get("session_buy_quantity", 0.0) + fill_qty
                    pos["session_buy_value"] = pos.get("session_buy_value", 0.0) + (fill_qty * fill_px)
                elif side_enum_val == OrderSide.SELL:
                    pos["session_sell_quantity"] = pos.get("session_sell_quantity", 0.0) + fill_qty
                    pos["session_sell_value"] = pos.get("session_sell_value", 0.0) + (fill_qty * fill_px)

                pos["last_update_source"] = "eventbus_fill"
                pos["last_update"] = data.fill_timestamp # Use fill_timestamp from event
                pos["last_update_timestamp"] = data.fill_timestamp # For PositionData event compatibility

                # Reset retry_count if position was just opened by this fill, or closed by this fill.
                if (abs(old_total_shares) < 1e-9 and pos["open"]) or \
                   (abs(old_total_shares) > 1e-9 and not pos["open"]):
                    pos["retry_count"] = 0
                    self.logger.info(f"POSITION_MGR: Retry count for {symbol_upper} reset due to fill resulting in open/close.")

                self.logger.critical(f"POSITION_MGR_LIFECYCLE: State AFTER processing fill for {symbol_upper}. "
                                   f"Shares={pos.get('total_shares', 0.0):.2f}, AvgPx={pos.get('total_avg_price', 0.0):.4f}, "
                                   f"Strategy='{pos.get('strategy')}', IsOpen={pos.get('open')}, "
                                   f"RealPnL={pos.get('overall_realized_pnl', 0.0):.2f}")

                # Create and publish PositionUpdateEvent
                position_data_payload = PositionUpdateEventData(
                    symbol=symbol_upper,
                    quantity=new_total_shares,
                    average_price=new_total_avg_price,
                    realized_pnl_session=pos.get("overall_realized_pnl", 0.0),
                    is_open=pos.get("open", False),
                    strategy=pos.get("strategy", "closed"),
                    last_update_timestamp=data.fill_timestamp,
                    update_source_trigger="FILL",
                    position_uuid=pos.get("position_uuid"),
                    causing_correlation_id=master_correlation_id_for_pos,
                    opening_signal_correlation_id=pos.get("opening_signal_correlation_id"),
                    opening_order_correlation_id=pos.get("opening_order_correlation_id"),
                    last_signal_correlation_id=pos.get("last_signal_correlation_id"),
                    last_order_correlation_id=pos.get("last_order_correlation_id"),
                    position_opened_timestamp=pos.get("position_opened_timestamp"),
                    last_fill_timestamp=pos.get("last_fill_timestamp"),
                    total_fills_count=pos.get("total_fills_count", 0),
                    session_buy_quantity=pos.get("session_buy_quantity", 0.0),
                    session_sell_quantity=pos.get("session_sell_quantity", 0.0),
                    session_buy_value=pos.get("session_buy_value", 0.0),
                    session_sell_value=pos.get("session_sell_value", 0.0),
                    position_opened_timestamp_ns=pos.get("position_opened_timestamp_ns"),
                    last_fill_timestamp_ns=pos.get("last_fill_timestamp_ns"),
                    signal_to_fill_latency_ms=pos.get("signal_to_fill_latency_ms"),
                    order_to_fill_latency_ms=pos.get("order_to_fill_latency_ms")
                )
                position_event = PositionUpdateEvent(
                    data=position_data_payload
                )

                # INSTRUMENTATION: Log detailed PositionUpdateEvent before publishing
                self.logger.critical(f"PM_EVENT_PUBLISH: Publishing PositionUpdateEvent for {symbol_upper}. "
                                   f"is_open={position_data_payload.is_open}, shares={position_data_payload.quantity:.1f}, "
                                   f"avg_px={position_data_payload.average_price:.4f}, strategy={position_data_payload.strategy}, "
                                   f"source={position_data_payload.update_source_trigger}, timestamp={position_data_payload.last_update_timestamp}")

                self.event_bus.publish(position_event)
                self.logger.critical(f"POSITION_MGR_LIFECYCLE: Published PositionUpdateEvent for {symbol_upper}. EventData={position_event.data}")

                # Also publish to Redis for GUI consumption
                # When publishing, use the stored/updated master_correlation_id_for_pos
                # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
                pos_after_fill = self._get_position_no_lock(symbol_upper)
                pos_snapshot = copy.deepcopy(pos_after_fill)  # Create deep copy while locked
                
            # Position update publishing now handled via telemetry service
            self._publish_position_update_to_redis(
                symbol_upper=symbol_upper,
                pos_state_dict=pos_snapshot,
                update_source="FILL",
                master_correlation_id=master_correlation_id_for_pos,
                causation_id=None
            )

        except Exception as e:
            self.logger.error(f"POSITION_MGR: Error handling order fill event: {e}", exc_info=True)

    def _handle_broker_quantity_only_push(self, event: BrokerPositionQuantityOnlyPushEvent):
        """Handle real-time quantity-only position updates from broker."""
        # --- AGENT_DEBUG: START ---
        event_symbol_pm_qty = "NO_SYMBOL"
        if hasattr(event, 'data') and event.data is not None and hasattr(event.data, 'symbol') and event.data.symbol is not None:
            event_symbol_pm_qty = event.data.symbol
        elif hasattr(event, 'symbol') and event.symbol is not None:
            event_symbol_pm_qty = event.symbol

        self.logger.critical(
            f"AGENT_DEBUG_PM_HANDLER_ENTERED: Method=_handle_broker_quantity_only_push, EventType={type(event).__name__}, "
            f"Symbol={event_symbol_pm_qty}, EventID={getattr(event, 'event_id', 'N/A')}, "
            f"EventTypeID={id(type(event))}"
        )
        # --- AGENT_DEBUG: END ---
        data = event.data
        
        # Capture correlation context
        master_correlation_id = getattr(event, 'correlation_id', None)
        # The ID of the current event is the cause of the next event
        causation_id_for_next_step = getattr(event, 'event_id', None)
        
        if not master_correlation_id:
            self.logger.error(f"CRITICAL: Missing correlation_id on event {type(event).__name__}. This breaks end-to-end tracing!")
            master_correlation_id = "MISSING_CORRELATION_ID"
        
        symbol_upper = data.symbol.upper()
        # Use event.timestamp (from BaseEvent) or data.broker_timestamp if more accurate
        update_ts = data.broker_timestamp if data.broker_timestamp is not None else event.timestamp

        # DEBUG: Intercept and log the full payload
        self.logger.critical(f"[DEBUG] BROKER PAYLOAD DEBUG: BrokerPositionQuantityOnlyPushEvent")
        self.logger.critical(f"[DEBUG] BROKER PAYLOAD: event.data = {data}")
        self.logger.critical(f"[DEBUG] BROKER PAYLOAD: event.timestamp = {event.timestamp}")
        self.logger.critical(f"[DEBUG] BROKER PAYLOAD: data.broker_timestamp = {data.broker_timestamp}")
        self.logger.critical(f"[DEBUG] BROKER PAYLOAD: Available attributes: {dir(data)}")

        self.logger.info(f"[{symbol_upper}] PM: Received BrokerPositionQuantityOnlyPushEvent: NewBrokerQty={data.quantity:.0f}")

        with self._positions_lock:
            pos = self._get_position_no_lock(symbol_upper)
            old_pm_shares = pos.get("total_shares", 0.0)

            # Only update if the broker's quantity is different from PM's current quantity
            if abs(old_pm_shares - data.quantity) > 1e-9:
                self.logger.warning(
                    f"[{symbol_upper}] PM: Quantity updated by broker push. "
                    f"Old PM Shares={old_pm_shares:.0f}, New Broker Qty={data.quantity:.0f}. "
                    f"PM AvgPx and RealPnL remain unchanged by this event."
                )
                pos["total_shares"] = data.quantity
                pos["open"] = abs(data.quantity) > 1e-9

                # INSTRUMENTATION: Log position state determination
                self.logger.critical(f"PM_POSITION_STATE: {symbol_upper} after broker quantity push. "
                                   f"total_shares={data.quantity:.1f}, is_open={pos['open']}, "
                                   f"abs(shares)={abs(data.quantity):.1f}, threshold=1e-9")

                new_strategy = "long" if data.quantity > 1e-9 else \
                               ("short" if data.quantity < -1e-9 else "closed")
                if pos.get("strategy") != new_strategy:
                     self.logger.info(f"[{symbol_upper}] PM: Strategy changed due to quantity push: {pos.get('strategy')} -> {new_strategy}")
                pos["strategy"] = new_strategy

                pos["last_update_source"] = "broker_realtime_qty_push"
                pos["last_update"] = update_ts
                pos["last_update_timestamp"] = update_ts  # For PositionData event

                # Publish PositionUpdateEvent to notify other consumers of PM's state change
                # PM's avg_price and realized_pnl are NOT changed by this specific event.
                # They are only changed by PM's own processing of OrderFilledEvents or a full sync.
                position_data_payload = PositionUpdateEventData(
                    symbol=symbol_upper,
                    quantity=pos["total_shares"],
                    average_price=pos.get("total_avg_price", 0.0),
                    realized_pnl_session=pos.get("overall_realized_pnl", 0.0),
                    is_open=pos.get("open", False),
                    strategy=pos.get("strategy", "closed"),
                    last_update_timestamp=update_ts,
                    update_source_trigger="BROKER_QUANTITY_PUSH",
                    position_uuid=pos.get("position_uuid"),
                    causing_correlation_id=master_correlation_id
                )
                if self.event_bus:
                     # INSTRUMENTATION: Log detailed PositionUpdateEvent before publishing from qty_push
                     self.logger.critical(f"PM_QTY_PUSH_EVENT_PUBLISH: Publishing PositionUpdateEvent for {symbol_upper} (from qty_push). "
                                        f"is_open={position_data_payload.is_open}, shares={position_data_payload.quantity:.1f}, "
                                        f"avg_px={position_data_payload.average_price:.4f}, strategy={position_data_payload.strategy}, "
                                        f"source={position_data_payload.update_source_trigger}, timestamp={position_data_payload.last_update_timestamp}")

                     self.event_bus.publish(PositionUpdateEvent(
                         data=position_data_payload
                     ))
                     self.logger.info(f"[{symbol_upper}] PM: Published PositionUpdateEvent after broker_realtime_qty_push.")
            else:
                self.logger.debug(f"[{symbol_upper}] PM: Broker quantity push ({data.quantity:.0f}) matches current PM shares ({old_pm_shares:.0f}). No state change from this event.")

    def _handle_broker_full_position_push(self, event: BrokerFullPositionPushEvent):
        """Handle full position updates from broker with PnL data."""
        # --- AGENT_DEBUG: START ---
        event_symbol_pm_full = "NO_SYMBOL"
        if hasattr(event, 'data') and event.data is not None and hasattr(event.data, 'symbol') and event.data.symbol is not None:
            event_symbol_pm_full = event.data.symbol
        elif hasattr(event, 'symbol') and event.symbol is not None:
            event_symbol_pm_full = event.symbol

        self.logger.critical(
            f"AGENT_DEBUG_PM_HANDLER_ENTERED: Method=_handle_broker_full_position_push, EventType={type(event).__name__}, "
            f"Symbol={event_symbol_pm_full}, EventID={getattr(event, 'event_id', 'N/A')}, "
            f"EventTypeID={id(type(event))}"
        )
        # --- AGENT_DEBUG: END ---
        data = event.data
        symbol_upper = data.symbol.upper()
        update_ts = data.broker_timestamp if data.broker_timestamp is not None else event.timestamp

        # DEBUG: Intercept and log the full payload
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD DEBUG: BrokerFullPositionPushEvent")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: event.data = {data}")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: quantity = {data.quantity}")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: average_price = {data.average_price}")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: realized_pnl_day = {data.realized_pnl_day}")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: unrealized_pnl_day = {data.unrealized_pnl_day}")
        self.logger.critical(f"[DEBUG] FULL BROKER PAYLOAD: total_pnl_day = {data.total_pnl_day}")

        self.logger.info(f"[{symbol_upper}] PM: Received BrokerFullPositionPushEvent: Qty={data.quantity:.0f}, AvgPx={data.average_price:.4f}, RPNL={data.realized_pnl_day}, UPNL={data.unrealized_pnl_day}")

        # CRITICAL FIX: Use sync_broker_position to ensure trade ID linkage
        # This ensures that broker position updates create proper trade IDs and correlation tracking
        self.sync_broker_position(
            symbol=symbol_upper,
            broker_total_shares=data.quantity,
            broker_total_avg_price=data.average_price,
            broker_realized_pnl=data.realized_pnl_day,
            source="broker_full_position_push"
        )

        self.logger.info(f"CRITICAL FIX: {symbol_upper} broker full position push routed through sync_broker_position for proper trade ID linkage")

    def process_fill(self, symbol: str, side: str, shares_filled: float,
                     fill_price: float, local_id: int, fill_time: float,
                     master_correlation_id: Optional[str] = None) -> None:
        
        # === START MODIFICATION (IntelliSense Instrumentation) ===
        
        # Ingest the Correlation ID
        log_corr_id = master_correlation_id or "uncorrelated_fill"
        self.logger.info(f"[ID_TRACE] PM processing fill. CorrID: {log_corr_id}, LocalID: {local_id}")

        # === END MODIFICATION ===

        symbol_upper = symbol.upper()
        side_upper = side.upper()
        self.logger.info(f"PM processing SYSTEM fill: {symbol_upper} {side_upper} {shares_filled}@{fill_price:.4f} (local_id={local_id})")

        # === START MODIFICATION (IntelliSense Instrumentation) ===
        # Initialize T22 timestamp variable outside lock for later use
        T22_PositionUpdateEnd_ns = None
        telemetry_payload = None
        # === END MODIFICATION ===
        
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol_upper)

            old_total_shares = pos["total_shares"]
            old_total_avg_price = pos["total_avg_price"]
            old_strategy = pos.get("strategy", "closed")
            pnl_contribution_this_fill = 0.0

            # 1. Update overall position based on this system fill
            if side_upper == "BUY":
                new_total_shares = old_total_shares + shares_filled
                if abs(new_total_shares) > 1e-9: # Avoid division by zero if shares become zero (e.g. closing a short)
                    new_total_avg_price = ((old_total_shares * old_total_avg_price) + (shares_filled * fill_price)) / new_total_shares
                else:
                    new_total_avg_price = 0.0
            elif side_upper == "SELL":
                new_total_shares = old_total_shares - shares_filled
                if abs(old_total_avg_price) > 1e-9: # If we had a cost basis
                    pnl_contribution_this_fill = (fill_price - old_total_avg_price) * shares_filled
                    pos["overall_realized_pnl"] += pnl_contribution_this_fill
                # Avg price doesn't change on a sell unless the position becomes flat or flips
                new_total_avg_price = old_total_avg_price
                if abs(new_total_shares) < 1e-9:
                    new_total_avg_price = 0.0 # Position is now flat
            else:
                self.logger.error(f"Unknown side '{side_upper}' in process_fill for {symbol_upper}")
                
                # INSTRUMENTATION: Publish unknown side calculation error
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionCalculationError",
                            payload={
                                "error_type": "UNKNOWN_SIDE_IN_CALCULATION",
                                "error_reason": f"Unknown side '{side_upper}' in position calculation - aborting fill processing",
                                "context": {
                        "symbol": symbol_upper,
                        "invalid_side": side_upper,
                        "shares_filled": shares_filled,
                        "fill_price": fill_price,
                        "local_order_id": local_id,
                        "fill_timestamp": fill_time,
                        "old_total_shares": old_total_shares,
                        "old_total_avg_price": old_total_avg_price,
                        "calculation_aborted": True,
                        "error_severity": "HIGH"
                    },
                                "calculation_timestamp": time.time(),
                                "component": "PositionManager"
                            }
                        )
                return # Or raise error

            # Capture old state for transition tracking
            old_state = {
                "total_shares": old_total_shares,
                "total_avg_price": old_total_avg_price,
                "strategy": old_strategy,
                "overall_realized_pnl": pos.get("overall_realized_pnl", 0.0)
            }

            pos["total_shares"] = new_total_shares
            pos["total_avg_price"] = new_total_avg_price

            pos["open"] = abs(pos["total_shares"]) > 1e-9
            new_strategy = "long" if pos["total_shares"] > 0 else ("short" if pos["total_shares"] < 0 else "closed")
            pos["strategy"] = new_strategy

            # Capture new state for transition tracking
            new_state = {
                "total_shares": new_total_shares,
                "total_avg_price": new_total_avg_price,
                "strategy": new_strategy,
                "overall_realized_pnl": pos.get("overall_realized_pnl", 0.0)
            }

            # Determine transition type based on state changes
            transition_type = "QUANTITY_CHANGE"
            if old_total_shares == 0 and new_total_shares != 0:
                transition_type = "POSITION_OPENED"
            elif old_total_shares != 0 and new_total_shares == 0:
                transition_type = "POSITION_CLOSED"
            elif (old_total_shares > 0 and new_total_shares < 0) or (old_total_shares < 0 and new_total_shares > 0):
                transition_type = "POSITION_FLIPPED"

            # 2. Record this system fill in its own ledger
            pos["session_system_fills_ledger"].append({
                "timestamp": fill_time,
                "type": side_upper,
                "shares": shares_filled,
                "price": fill_price,
                "pnl_contribution": pnl_contribution_this_fill,
                "local_order_id": local_id,
                "source": "process_fill"
            })

            # Detailed logging for position changes
            self.logger.critical(f"PM POSITION CHANGE in process_fill for {symbol_upper}: "
                          f"Shares: {old_total_shares:.2f} -> {new_total_shares:.2f}, "
                          f"AvgPrice: {old_total_avg_price:.4f} -> {new_total_avg_price:.4f}, "
                          f"Strategy: {old_strategy} -> {new_strategy}, "
                          f"Open: {pos['open']}, "
                          f"Fill: {side_upper} {shares_filled}@{fill_price:.4f}, "
                          f"PnL Contribution: {pnl_contribution_this_fill:.2f}, "
                          f"Local ID: {local_id}")

            pos["last_update_source"] = "system_fill"
            pos["last_update"] = fill_time

            # Reset retry_count if position was just opened or closed by this system fill
            if (abs(old_total_shares) < 1e-9 and pos["open"]) or \
               (abs(old_total_shares) > 1e-9 and not pos["open"]):
                pos["retry_count"] = 0

            self.logger.debug(f"PM state after SYSTEM fill {local_id}: TotalShares={pos['total_shares']:.2f}, AvgPx={pos['total_avg_price']:.4f}, OverallPnL={pos['overall_realized_pnl']:.2f}")

        # Publish state transition event (outside of lock)
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="TestradePositionStateTransition",
                payload={
                    "symbol": symbol_upper,
                    "old_state": old_state,
                    "new_state": new_state,
                    "transition_type": transition_type,
                    "trigger_event": "FILL_PROCESSED",
                    "transition_timestamp": time.time(),
                    "component": "PositionManager"
                }
            )

        with self._positions_lock:
            # Create and publish PositionUpdateEvent to EventBus for ActiveSymbolsService
            position_data_payload = PositionUpdateEventData(
                symbol=symbol_upper,
                quantity=new_total_shares,
                average_price=new_total_avg_price,
                realized_pnl_session=pos.get("overall_realized_pnl", 0.0),
                is_open=pos.get("open", False),
                strategy=pos.get("strategy", "closed"),
                last_update_timestamp=fill_time,
                update_source_trigger="SYSTEM_FILL",
                position_uuid=pos.get("position_uuid"),
                causing_correlation_id=master_correlation_id
            )
            position_event = PositionUpdateEvent(data=position_data_payload)

            # INSTRUMENTATION: Log detailed PositionUpdateEvent before publishing from process_fill
            self.logger.critical(f"PM_SYSTEM_FILL_EVENT_PUBLISH: Publishing PositionUpdateEvent for {symbol_upper} (from process_fill). "
                               f"is_open={position_data_payload.is_open}, shares={position_data_payload.quantity:.1f}, "
                               f"avg_px={position_data_payload.average_price:.4f}, strategy={position_data_payload.strategy}, "
                               f"source={position_data_payload.update_source_trigger}, timestamp={position_data_payload.last_update_timestamp}")

            if self.event_bus:
                self.event_bus.publish(position_event)
                self.logger.critical(f"POSITION_MGR_LIFECYCLE: Published PositionUpdateEvent for {symbol_upper} from process_fill. EventData={position_event.data}")
                
                # CRITICAL: Also publish SymbolPublishingStateChangedEvent for market data filtering
                from core.events import SymbolPublishingStateChangedEvent, SymbolPublishingStateChangedData
                publishing_state_data = SymbolPublishingStateChangedData(
                    symbol=symbol_upper,
                    should_publish=pos.get("open", False),  # Publish market data if position is open
                    reason="POSITION_OPENED_BY_FILL" if pos.get("open", False) else "POSITION_CLOSED_BY_FILL"
                )
                publishing_state_event = SymbolPublishingStateChangedEvent(data=publishing_state_data)
                self.event_bus.publish(publishing_state_event)
                self.logger.info(f"Published SymbolPublishingStateChangedEvent for {symbol_upper}: should_publish={publishing_state_data.should_publish}")
            else:
                self.logger.warning(f"EventBus not available, cannot publish PositionUpdateEvent for {symbol_upper} from process_fill")

            # Publish position update to Redis
            # Use the passed master_correlation_id as the causing_corr_id
            # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
            pos_after_fill = self._get_position_no_lock(symbol_upper)  # Get the updated state
            pos_snapshot = copy.deepcopy(pos_after_fill)  # Create deep copy while locked
            
            # === START MODIFICATION (IntelliSense Instrumentation) ===

            # MILESTONE T22: Position state has been fully updated after a fill.
            T22_PositionUpdateEnd_ns = time.time_ns()

            # Find T0 for full end-to-end latency calculation if possible
            t0_timestamp_ns = None
            if self._order_repository:
                order_obj = self._order_repository.get_order_by_local_id(local_id)
                if order_obj and hasattr(order_obj, 'meta') and order_obj.meta:
                    ocr_meta = order_obj.meta.get('ocr_snapshot_metadata', {})
                    t0_timestamp_ns = ocr_meta.get('T0_ImageIngress_ns')
            
            total_latency_ms = None
            if t0_timestamp_ns:
                total_latency_ms = (T22_PositionUpdateEnd_ns - t0_timestamp_ns) / 1_000_000

            self.logger.critical(
                f"[T22_PositionUpdate] *** POSITION FINALIZED *** "
                f"CorrID: {log_corr_id}, Symbol: {symbol_upper}, "
                f"{'Total_Latency_Since_T0: {:.2f}ms'.format(total_latency_ms) if total_latency_ms is not None else 'Total_Latency_Since_T0: T0_MISSING'}, "
                f"New_Shares: {pos['total_shares']:.0f}, New_AvgPx: {pos['total_avg_price']:.4f}"
            )
            
            # Add this timing information to the telemetry event
            telemetry_payload = {
                "milestone": "T22_PositionUpdate",
                "T22_PositionUpdateEnd_ns": T22_PositionUpdateEnd_ns,
                "total_latency_ms": total_latency_ms,
                "symbol": symbol_upper,
                "local_order_id": local_id,
                "new_total_shares": pos["total_shares"],
                "new_avg_price": pos["total_avg_price"],
                "side": side_upper,
                "shares_filled": shares_filled,
                "fill_price": fill_price,
                "realized_pnl_session": pos.get("overall_realized_pnl", 0.0),
                "position_open": pos.get("open", False),
                "strategy": pos.get("strategy", "closed")
            }
            
            # === END MODIFICATION ===
            
        # Position update publishing now handled via telemetry service
        self._publish_position_update_to_redis(
            symbol_upper=symbol_upper,
            pos_state_dict=pos_snapshot,
            update_source="SYSTEM_FILL",
            master_correlation_id=master_correlation_id,
            causation_id=None
        )
        
        # === START MODIFICATION (IntelliSense Instrumentation) ===
        # Publish telemetry event after lock is released using world-class telemetry
        if self._correlation_logger:
            # Fire-and-forget telemetry with proper nanosecond origin timestamp
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="PositionUpdateMilestone",
                payload=telemetry_payload
            )
        else:
            self.logger.warning("CorrelationLogger not available - T22 milestone not published")
        # === END MODIFICATION ===

    def debug_all_positions(self):
        """Debug method to log all current positions"""
        with self._positions_lock:
            self.logger.critical(f"[DEBUG] PM DEBUG: Total positions tracked: {len(self._positions)}")
            for symbol, pos in self._positions.items():
                fills_count = pos.get('total_fills_count', 0)
                opened_ts = pos.get('position_opened_timestamp', None)
                last_update_source = pos.get('last_update_source', 'unknown')
                session_fills = len(pos.get('session_system_fills_ledger', []))

                self.logger.critical(f"[DEBUG] PM DEBUG: {symbol} = {pos.get('total_shares', 0):.0f} shares @ {pos.get('total_avg_price', 0):.4f}")
                self.logger.critical(f"[DEBUG] PM DEBUG: {symbol} - Fills: {fills_count}, Session Fills: {session_fills}, Opened: {opened_ts}, Source: {last_update_source}")

                # Show recent fills
                recent_fills = pos.get('session_system_fills_ledger', [])[-3:]  # Last 3 fills
                for i, fill in enumerate(recent_fills):
                    self.logger.critical(f"[DEBUG] PM DEBUG: {symbol} - Recent Fill {i+1}: {fill.get('type')} {fill.get('shares')}@{fill.get('price')} from {fill.get('source')}")

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        symbol_upper = symbol.upper()
        self.logger.critical(f"POSITION_MANAGER_GET_POSITION: Called for symbol {symbol_upper}")
        with self._positions_lock:
            internal_pos = self._positions.get(symbol_upper)
            self.logger.critical(f"POSITION_MANAGER_GET_POSITION: Internal position for {symbol_upper}: {internal_pos}")
            if not internal_pos:
                self.logger.critical(f"POSITION_MANAGER_GET_POSITION: No position found for {symbol_upper}, returning None")
                return None

            # Create a snapshot that consumers expect
            snapshot = copy.deepcopy(internal_pos) # Start with a copy of all internal fields

            # Add/map to the expected "public" key names
            snapshot["shares"] = internal_pos.get("total_shares", 0.0)
            snapshot["avg_price"] = internal_pos.get("total_avg_price", 0.0)
            snapshot["quantity"] = internal_pos.get("total_shares", 0.0)  # For PositionData compatibility
            snapshot["average_price"] = internal_pos.get("total_avg_price", 0.0)  # For PositionData compatibility

            # Ensure last_update_timestamp is present for PositionData compatibility
            if "last_update_timestamp" not in snapshot:
                snapshot["last_update_timestamp"] = internal_pos.get("last_update", time.time())

            self.logger.critical(f"POSITION_MANAGER_GET_POSITION: Returning snapshot for {symbol_upper}: {snapshot}")
            return snapshot

    def get_all_positions_snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._positions_lock:
            snapshot_all = {}
            for symbol, internal_pos in self._positions.items():
                # Create a snapshot that consumers expect for each position
                snapshot_one = copy.deepcopy(internal_pos)
                snapshot_one["shares"] = internal_pos.get("total_shares", 0.0)
                snapshot_one["avg_price"] = internal_pos.get("total_avg_price", 0.0)
                snapshot_one["quantity"] = internal_pos.get("total_shares", 0.0)  # For PositionData compatibility
                snapshot_one["average_price"] = internal_pos.get("total_avg_price", 0.0)  # For PositionData compatibility

                # Ensure last_update_timestamp is present for PositionData compatibility
                if "last_update_timestamp" not in snapshot_one:
                    snapshot_one["last_update_timestamp"] = internal_pos.get("last_update", time.time())

                snapshot_all[symbol] = snapshot_one
            return snapshot_all

    def get_all_positions_for_gui_bootstrap(self, include_market_data: bool = True) -> Dict[str, Any]:
        """
        🚨 NEW METHOD: Get all positions formatted for GUI Bootstrap API.
        Returns positions as a properly structured dictionary for JSON serialization.

        Args:
            include_market_data: Whether to include market data in the response

        Returns:
            Dictionary with positions list and metadata for Bootstrap API
        """
        try:
            self.logger.info("PositionManager: Preparing positions data for GUI Bootstrap API")

            # Get current positions snapshot
            positions_snapshot = self.get_all_positions_snapshot()

            # Format positions for GUI consumption
            formatted_positions = []
            hierarchy_available = False

            for symbol, position_data in positions_snapshot.items():
                try:
                    # Extract position information
                    total_shares = position_data.get("total_shares", 0.0)
                    total_avg_price = position_data.get("total_avg_price", 0.0)
                    is_open = position_data.get("open", False)

                    # Trade lifecycle data
                    lcm_trade_id = position_data.get("lcm_parent_trade_id")
                    master_corr_id = position_data.get("master_correlation_id")

                    # Generate trade ID for GUI
                    if master_corr_id:
                        gui_trade_id = master_corr_id
                    elif lcm_trade_id:
                        gui_trade_id = f"{symbol}-{lcm_trade_id}"
                    else:
                        gui_trade_id = f"{symbol}-{uuid.uuid4()}"

                    # Check if we have hierarchy data
                    if lcm_trade_id or master_corr_id:
                        hierarchy_available = True

                    # Create position record for GUI
                    position_record = {
                        "symbol": symbol,
                        "quantity": total_shares,
                        "average_price": total_avg_price,
                        "is_open": is_open,
                        "realized_pnl": position_data.get("overall_realized_pnl", 0.0),
                        "unrealized_pnl": position_data.get("current_unrealized_pnl", 0.0),
                        "last_update_timestamp": position_data.get("last_update", time.time()),
                        "strategy": position_data.get("strategy", "UNKNOWN"),

                        # Trade hierarchy fields
                        "trade_id": gui_trade_id,
                        "parent_trade_id": None,  # This is a parent record
                        "is_parent": True,
                        "is_child": False,
                        "lcm_trade_id": lcm_trade_id,
                        "master_correlation_id": master_corr_id,

                        # Market data fields (will be populated if include_market_data is True)
                        "bid_price": None,
                        "ask_price": None,
                        "last_price": None,
                        "market_data_available": False,
                        "hierarchy_data_available": hierarchy_available,

                        # Additional GUI fields
                        "action": "BUY" if total_shares > 0 else ("SELL" if total_shares < 0 else "FLAT"),
                        "side": "BUY" if total_shares > 0 else ("SELL" if total_shares < 0 else "FLAT"),
                        "status": "OPEN" if is_open else "CLOSED",
                        "price": total_avg_price,
                        "timestamp": position_data.get("last_update", time.time())
                    }

                    # Add market data if requested and available
                    if include_market_data:
                        # TODO: Integrate with market data service when available
                        # For now, market data fields remain None
                        pass

                    formatted_positions.append(position_record)

                except Exception as e:
                    self.logger.error(f"Error formatting position for {symbol}: {e}", exc_info=True)
                    continue

            # Create the response structure
            response_data = {
                "positions": formatted_positions,
                "market_data_available": False,  # TODO: Determine from market data service
                "hierarchy_data_available": hierarchy_available,
                "is_bootstrap": True,
                "total_positions": len(formatted_positions)
            }

            self.logger.info(f"PositionManager: Prepared {len(formatted_positions)} positions for GUI Bootstrap API")
            return response_data

        except Exception as e:
            self.logger.error(f"Error in get_all_positions_for_gui_bootstrap: {e}", exc_info=True)
            # Return empty structure on error, no mock data
            return {
                "positions": [],
                "market_data_available": False,
                "hierarchy_data_available": False,
                "is_bootstrap": True,
                "total_positions": 0,
                "error": str(e)
            }

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all positions (alias for get_all_positions_snapshot for compatibility).

        Returns:
            A dictionary of all positions
        """
        return self.get_all_positions_snapshot()

    def get_position_data(self) -> Dict[str, Any]:
        """
        Get position data for IntelliSense OCR engine compatibility.

        This method provides compatibility with the IntelliSense OCR engine which expects
        get_position_data() method signature.

        Returns:
            Dict[symbol, position_data] containing all current positions
        """
        return self.get_all_positions_snapshot()

    def get_symbol_positions(self) -> Dict[str, Any]:
        """
        Get symbol positions for IntelliSense OCR engine compatibility.

        This method provides compatibility with the IntelliSense OCR engine which expects
        get_symbol_positions() method signature.

        Returns:
            Dict[symbol, position_data] containing all current positions
        """
        return self.get_all_positions_snapshot()

    def publish_all_positions_to_stream(self, correlation_id: str):
        """
        Fetches all current positions, formats them, and publishes them to the
        appropriate Redis stream for GUI bootstrap.
        The payload structure is designed to be compatible with what gui_backend.py
        expects for a "trades_update" WebSocket message.
        """
        self.logger.info(f"PositionManager: Publishing all positions for bootstrap (CorrID: {correlation_id}).")

        # get_all_positions_snapshot already returns a dict of {symbol: position_dict}
        # where position_dict contains fields like total_shares, total_avg_price, etc.
        # We need to convert this into a list of position objects for the "trades" key.
        all_positions_snapshot = self.get_all_positions_snapshot() # This uses a lock internally

        formatted_positions_for_stream = []
        for symbol, internal_pos_data in all_positions_snapshot.items():
            # Transform internal_pos_data to match the structure your gui_backend.py's
            # handle_enriched_position_updates_message or handle_position_updates_message
            # (which then sends a 'trades_update' WebSocket message) expects.

            # Fetch live market data for enrichment if possible
            live_bid = None
            live_ask = None
            live_last = None
            live_volume = None
            market_data_available_for_symbol = False

            price_provider = self._price_provider
            if price_provider:
                try:
                    live_bid = price_provider.get_bid_price(symbol)
                    live_ask = price_provider.get_ask_price(symbol)
                    live_last = price_provider.get_current_price(symbol)
                    if hasattr(price_provider, 'get_volume'):
                         live_volume = price_provider.get_volume(symbol)
                    if live_bid or live_ask or live_last:
                        market_data_available_for_symbol = True
                except Exception as e_price:
                    self.logger.debug(f"Could not fetch live market data for {symbol} during bootstrap: {e_price}")

            # SME REQUIREMENT: USE STORED IDs FOR STABLE PARENT TRADE IDENTIFICATION
            lcm_parent_trade_id = internal_pos_data.get("lcm_parent_trade_id")  # Integer ID from LCM
            master_corr_id = internal_pos_data.get("master_correlation_id")     # IntelliSense UUID

            # SME: IntelliSense UUID is now THE primary trade identifier for GUI grouping
            gui_display_trade_id = master_corr_id  # Use IntelliSense UUID as primary identifier
            lcm_internal_tid = lcm_parent_trade_id  # For reference/debug

            if not gui_display_trade_id:
                # If master_correlation_id is missing, fallback to SYMBOL-LCM_TID
                if lcm_internal_tid is not None:
                    gui_display_trade_id = f"{symbol}-{lcm_internal_tid}"
                    self.logger.warning(f"PM Bootstrap: Position {symbol} missing master_correlation_id. Using LCM-based GUI trade_id '{gui_display_trade_id}'.")
                else:  # Worst case, should not happen if PM state is populated correctly
                    gui_display_trade_id = internal_pos_data.get("position_uuid", f"{symbol}-FALLBACK_{int(time.time())}")
                    self.logger.error(f"PM Bootstrap: Position {symbol} missing BOTH master_correlation_id AND lcm_trade_id. Using fallback GUI trade_id '{gui_display_trade_id}'. THIS IS A DATA INTEGRITY ISSUE.")

            # 🚨 HIERARCHY FIX: Create both parent and child records for proper trade hierarchy

            # 1. Create parent position record (summary)
            parent_record = {
                "symbol": symbol,
                "is_open": internal_pos_data.get("open", False),
                "quantity": internal_pos_data.get("total_shares", 0.0),
                "average_price": internal_pos_data.get("total_avg_price", 0.0),
                "realized_pnl": internal_pos_data.get("overall_realized_pnl", 0.0),
                "unrealized_pnl": internal_pos_data.get("current_unrealized_pnl", 0.0),
                "last_update_timestamp": internal_pos_data.get("last_update", time.time()),
                "last_update_source": internal_pos_data.get("last_update_source", "bootstrap"),
                "strategy": internal_pos_data.get("strategy", "UNKNOWN"),
                "total_fills_count": internal_pos_data.get("total_fills_count", 0),
                "position_opened_timestamp": internal_pos_data.get("position_opened_timestamp"),

                # Enriched market data
                "bid_price": live_bid,
                "ask_price": live_ask,
                "last_price": live_last,
                "total_volume": live_volume,

                # Data availability flags
                "market_data_available": market_data_available_for_symbol,
                "hierarchy_data_available": True,

                # 🚨 HIERARCHY FIX: Parent record with actual trade ID
                "is_parent": True,
                "is_child": False,
                "parent_trade_id": None,  # Parent has no parent
                "trade_id": gui_display_trade_id,              # <<< STABLE INTELLISENSE UUID
                "lcm_trade_id": lcm_internal_tid,               # <<< LCM integer t_id for reference
                "master_correlation_id": master_corr_id,        # <<< INTELLISENSE UUID for tracing

                # Other fields your app.js might expect in the 'trades' array
                "action": "BUY" if internal_pos_data.get("total_shares", 0.0) > 0 else ("SELL" if internal_pos_data.get("total_shares", 0.0) < 0 else "FLAT"),
                "side": "BUY" if internal_pos_data.get("total_shares", 0.0) > 0 else ("SELL" if internal_pos_data.get("total_shares", 0.0) < 0 else "FLAT"),
                "status": "OPEN" if internal_pos_data.get("open", False) else "CLOSED",
                "price": internal_pos_data.get("total_avg_price", 0.0),
                "timestamp": internal_pos_data.get("last_update", time.time())
            }
            formatted_positions_for_stream.append(parent_record)

            # 2. 🚨 HIERARCHY FIX: Create child records from individual orders/fills
            # Use injected OrderRepository instead of accessing through ApplicationCore
            if lcm_internal_tid and self._order_repository:
                try:
                    # Get all orders for this trade
                    orders_for_trade = self._order_repository.get_orders_for_trade(lcm_internal_tid)
                    self.logger.info(f"🚨 HIERARCHY FIX: Found {len(orders_for_trade)} orders for trade {lcm_internal_tid} ({symbol})")

                    for order_data in orders_for_trade:
                        # Create child record for each order
                        child_record = {
                            "symbol": symbol,
                            "is_open": False,  # Individual orders are completed
                            "quantity": order_data.get("requested_shares", 0.0),
                            "average_price": order_data.get("avg_fill_price", 0.0) or order_data.get("requested_lmt_price", 0.0),
                            "realized_pnl": 0.0,  # Individual orders don't have PnL
                            "unrealized_pnl": 0.0,
                            "last_update_timestamp": order_data.get("timestamp_requested", time.time()),
                            "last_update_source": "order_fill",
                            "strategy": internal_pos_data.get("strategy", "UNKNOWN"),
                            "total_fills_count": len(order_data.get("fills", [])),
                            "position_opened_timestamp": order_data.get("timestamp_requested"),

                            # Market data (same as parent)
                            "bid_price": live_bid,
                            "ask_price": live_ask,
                            "last_price": live_last,
                            "total_volume": live_volume,
                            "market_data_available": market_data_available_for_symbol,
                            "hierarchy_data_available": True,

                            # 🚨 HIERARCHY FIX: Child record with parent linkage
                            "is_parent": False,
                            "is_child": True,
                            "parent_trade_id": gui_display_trade_id,  # Link to parent
                            "trade_id": f"{gui_display_trade_id}-{order_data.get('local_id', 'unknown')}",  # Unique child ID
                            "lcm_trade_id": lcm_internal_tid,
                            "master_correlation_id": order_data.get("master_correlation_id", master_corr_id),

                            # Order-specific fields
                            "action": order_data.get("side", "UNKNOWN"),
                            "side": order_data.get("side", "UNKNOWN"),
                            "status": order_data.get("ls_status", "UNKNOWN"),
                            "price": order_data.get("avg_fill_price", 0.0) or order_data.get("requested_lmt_price", 0.0),
                            "timestamp": order_data.get("timestamp_requested", time.time()),
                            "local_order_id": order_data.get("local_id"),
                            "broker_order_id": order_data.get("ls_order_id")
                        }
                        formatted_positions_for_stream.append(child_record)
                        self.logger.info(f"🚨 HIERARCHY FIX: Created child record for order {order_data.get('local_id')} -> parent {gui_display_trade_id}")

                except Exception as e:
                    self.logger.error(f"🚨 HIERARCHY FIX: Error creating child records for {symbol}: {e}", exc_info=True)

        if not formatted_positions_for_stream:
            self.logger.info("PositionManager: No current positions to publish for bootstrap after formatting.")

        # This is the outer payload structure for the "trades_update" WebSocket message
        # NOTE: For command response stream, we need "positions" key to match handle_all_positions_command_response
        payload_for_stream = {
            "positions": formatted_positions_for_stream,  # Changed from "trades" to "positions"
            "market_data_available": any(p.get("market_data_available") for p in formatted_positions_for_stream),
            "hierarchy_data_available": True,
            "is_bootstrap": True,
            "bootstrap_correlation_id": correlation_id
        }

        if not self._correlation_logger:
            self.logger.error("PositionManager: TelemetryService not available for bootstrap publishing.")
            return

        # For bootstrap, publish as command response to testrade:responses:to_gui stream
        # This will be handled by handle_phase2_response_message -> handle_all_positions_command_response
        command_response_data_payload = payload_for_stream  # This contains {"positions": [...], ...}

        inner_payload_for_redis_msg = {
            "original_command_id": correlation_id,  # Link to the REQUEST_INITIAL_STATE
            "command_type_echo": "GET_ALL_POSITIONS_BOOTSTRAP",  # Bootstrap command type
            "status": "success",
            "message": f"Bootstrap data for all positions ({len(formatted_positions_for_stream)} positions).",
            "response_timestamp": time.time(),
            "data": command_response_data_payload  # This becomes response_data in handle_phase2_response_message
        }

        # Get target stream from config
        target_stream_name = 'testrade:responses:to_gui'
        if self._config_service:
            target_stream_name = getattr(self._config_service, 'redis_stream_responses_to_gui', target_stream_name)

        # TANK Mode: Check if IPC data dumping is disabled
        if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
            self.logger.debug(f"TANK_MODE: Skipping position bootstrap - ENABLE_IPC_DATA_DUMP is False (CorrID: {correlation_id})")
            return

        # Use correlation logger for clean, fire-and-forget publish
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="TestradeGuiCommandResponse",
                payload=inner_payload_for_redis_msg
            )
        self.logger.info(f"PositionManager: Published {len(formatted_positions_for_stream)} positions for bootstrap (CorrID: {correlation_id}).")

    def publish_position_summary_to_stream(self, correlation_id: str):
        """
        Publishes the current overall position summary (P&L, counts) to Redis.
        """
        self.logger.info(f"PositionManager: Publishing position summary for bootstrap (CorrID: {correlation_id}).")

        # Calculate the summary based on current self._positions
        summary_data = self._calculate_current_overall_summary_for_stream()

        summary_data["is_bootstrap"] = True
        summary_data["bootstrap_correlation_id"] = correlation_id

        if not self._correlation_logger:
            self.logger.error("PositionManager: TelemetryService not available for summary publishing.")
            return

        # For bootstrap, publish as command response to testrade:responses:to_gui stream
        # This will be handled by handle_phase2_response_message -> handle_position_summary_command_response
        command_response_data_payload = {
            "summary": summary_data,  # Wrap summary data in "summary" key as expected by handler
            "market_data_available": True,
            "is_bootstrap": True,
            "bootstrap_correlation_id": correlation_id
        }

        inner_payload_for_redis_msg = {
            "original_command_id": correlation_id,  # Link to the REQUEST_INITIAL_STATE
            "command_type_echo": "GET_POSITION_SUMMARY_BOOTSTRAP",  # Bootstrap command type
            "status": "success",
            "message": "Bootstrap data for position summary.",
            "response_timestamp": time.time(),
            "data": command_response_data_payload  # This becomes response_data in handle_phase2_response_message
        }

        # Get target stream from config
        target_stream_name = 'testrade:responses:to_gui'
        if self._config_service:
            target_stream_name = getattr(self._config_service, 'redis_stream_responses_to_gui', target_stream_name)

        # TANK Mode: Check if IPC data dumping is disabled
        if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
            self.logger.debug(f"TANK_MODE: Skipping position summary - ENABLE_IPC_DATA_DUMP is False (CorrID: {correlation_id})")
            return

        # Use correlation logger for clean, fire-and-forget publish
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="TestradeGuiCommandResponse",
                payload=inner_payload_for_redis_msg
            )
        self.logger.info(f"PositionManager: Published position summary for bootstrap (CorrID: {correlation_id}).")

    def _calculate_current_overall_summary_for_stream(self) -> Dict[str, Any]:
        """
        Calculates the overall position summary.
        The returned dict structure should match what gui_backend.py's
        handle_enhanced_position_summary_message expects for its "summary" field,
        which is then broadcast as "position_summary_update" to app.js.
        """
        open_pnl_total = 0
        closed_pnl_session_total = 0
        open_positions_count = 0
        closed_positions_count = 0
        total_shares_traded = 0
        total_trades_count = 0
        winners = 0
        losers = 0
        total_volume = 0

        with self._positions_lock:
            for pos_data in self._positions.values():
                if pos_data.get("open", False):
                    open_positions_count += 1
                    # Accumulate unrealized P&L (needs live prices for accuracy)
                    open_pnl_total += pos_data.get("current_unrealized_pnl", 0.0)
                else:
                    # Count closed positions that had some activity
                    if pos_data.get("overall_realized_pnl", 0) != 0:
                        closed_positions_count += 1

                closed_pnl_session_total += pos_data.get("overall_realized_pnl", 0.0)

                # Track session trading activity
                total_shares_traded += pos_data.get("session_buy_quantity", 0) + pos_data.get("session_sell_quantity", 0)

                # Count winners/losers based on realized PnL
                realized_pnl = pos_data.get("overall_realized_pnl", 0.0)
                if realized_pnl > 0:
                    winners += 1
                elif realized_pnl < 0:
                    losers += 1

                # Add volume if available
                total_volume += pos_data.get("live_total_volume", 0)

            total_trades_count = len(self._positions)

        # Calculate win rate and averages
        total_closed_trades = winners + losers
        win_rate = (winners / total_closed_trades * 100) if total_closed_trades > 0 else 0.0

        # Calculate average win/loss (simplified)
        avg_win = 0.0
        avg_loss = 0.0
        if winners > 0 or losers > 0:
            with self._positions_lock:
                winning_pnls = [pos.get("overall_realized_pnl", 0.0) for pos in self._positions.values() if pos.get("overall_realized_pnl", 0.0) > 0]
                losing_pnls = [pos.get("overall_realized_pnl", 0.0) for pos in self._positions.values() if pos.get("overall_realized_pnl", 0.0) < 0]

                avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else 0.0
                avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0

        # Match the structure used in gui_backend.py's handle_enhanced_position_summary_message
        return {
            "totalSharesTraded": total_shares_traded,
            "openPositionsCount": open_positions_count,
            "closedPositionsCount": closed_positions_count,
            "totalTradesCount": total_trades_count,
            "openPnL": open_pnl_total,
            "closedPnL": closed_pnl_session_total,
            "totalDayPnL": open_pnl_total + closed_pnl_session_total,
            "winRate": win_rate,
            "avgWin": avg_win,
            "avgLoss": avg_loss,
            "totalVolume": total_volume,
            "winners": winners,
            "losers": losers,
            "timestamp": time.time()
        }

    def get_trading_status(self, symbol: str) -> Dict[str, Any]:
         with self._positions_lock:
              pos = self._get_position_no_lock(symbol.upper()) # Use helper
              return {
                   "status": pos.get("trading_status", "Unknown"),
                   "luld_high": pos.get("luld_high"),
                   "luld_low": pos.get("luld_low"),
                   "last_update": pos.get("last_update")
              }

    def update_trading_status(self, symbol: str, status_data: Dict[str, Any]) -> None:
        symbol = symbol.upper()
        self.logger.debug(f"PositionManager updating trading status for {symbol}: {status_data}")
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol) # Get or create

            # Status Interpretation Logic
            luld_char = status_data.get("luld_indicator")
            luld_status_str = "Unknown"

            # Map LULD indicator to status string
            if luld_char == "":
                luld_status_str = "Normal"
            elif luld_char == "L":
                luld_status_str = "LULD_LowerBand"
            elif luld_char == "U":
                luld_status_str = "LULD_UpperBand"
            elif luld_char == "P":
                luld_status_str = "Trading_Pause"
            else:
                luld_status_str = "Unknown"

            # Check for explicit status override
            explicit_status = status_data.get("status")
            final_status = luld_status_str

            if explicit_status and explicit_status != "Unknown":
                # Explicit status overrides LULD
                final_status = explicit_status
                self.logger.debug(f"Using explicit status '{explicit_status}' for {symbol} (overrides LULD '{luld_status_str}')")

            pos["trading_status"] = final_status
            pos["luld_high"] = status_data.get("luld_high")
            pos["luld_low"] = status_data.get("luld_low")
            pos["last_update"] = status_data.get("timestamp", time.time()) # Use provided timestamp if available
            self.logger.info(f"PositionManager stored trading status for {symbol}: Status='{final_status}'")


    def reset_position(self, symbol: str) -> None:
         symbol = symbol.upper()
         old_state = None
         
         with self._positions_lock:
              if symbol in self._positions:
                   # Capture old state before reset
                   old_pos = self._positions[symbol]
                   old_state = {
                       "total_shares": old_pos.get("total_shares", 0.0),
                       "total_avg_price": old_pos.get("total_avg_price", 0.0),
                       "strategy": old_pos.get("strategy", "closed"),
                       "overall_realized_pnl": old_pos.get("overall_realized_pnl", 0.0)
                   }
                   
                   self.logger.warning(f"Resetting internal position state for {symbol}")
                   del self._positions[symbol] # Or reset fields to zero/default
              else:
                   self.logger.debug(f"No internal position state found for {symbol} to reset.")
         
         # Publish state transition if there was a position to reset
         if old_state:
             new_state = {
                 "total_shares": 0.0,
                 "total_avg_price": 0.0,
                 "strategy": "closed",
                 "overall_realized_pnl": 0.0
             }
             
             self._publish_position_state_transition(
                 symbol=symbol,
                 old_state=old_state,
                 new_state=new_state,
                 transition_type="POSITION_RESET",
                 trigger_event="MANUAL_RESET",
                master_correlation_id=None,  # No correlation context for manual reset
                causation_id=None
             )

    def reset_all_positions(self) -> None:
         with self._positions_lock:
              self.logger.warning("Resetting all internal position states.")
              self._positions.clear()

    def update_fingerprint(self, symbol: str, fingerprint: tuple) -> None:
        """Updates the last broker fingerprint for a symbol."""
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            pos["last_broker_fingerprint"] = fingerprint
            self.logger.debug(f"PositionManager updated fingerprint for {symbol}: {fingerprint}")

    def handle_failed_open(self, symbol: str, max_retries: int, base_cooldown_sec: float) -> bool:
        """
        Handles a failed open by incrementing retry count and setting cooldown.

        Args:
            symbol: The stock symbol
            max_retries: Maximum number of retries allowed
            base_cooldown_sec: Base cooldown time in seconds

        Returns:
            True if max retries reached, False otherwise
        """
        symbol = symbol.upper()
        max_reached = False # Flag for return value
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            pos["retry_count"] = pos.get("retry_count", 0) + 1 # Increment first
            retry_count = pos["retry_count"]

            cooldown_sec = base_cooldown_sec * (2 ** (retry_count - 1))
            cooldown_until = time.time() + cooldown_sec
            pos["cooldown_until"] = cooldown_until

            # Capture retry info for event publishing
            retry_info = {
                "retry_count": retry_count,
                "max_retries": max_retries,
                "cooldown_seconds": cooldown_sec,
                "cooldown_until": cooldown_until,
                "base_cooldown_sec": base_cooldown_sec
            }

            if retry_count > max_retries:
                pos["strategy"] = "OpenFailed" # Set state
                pos["open"] = False # Ensure marked closed
                self.logger.error(f"Position {symbol} SET TO OpenFailed state after {retry_count} retries.") # Log state SET
                max_reached = True
                
                # Publish max retries reached event
                retry_info["final_state"] = "OpenFailed"
                event_type = "MAX_RETRIES_REACHED"
                
            else:
                pos["strategy"] = "OpenRetryCooldown" # Set state
                pos["open"] = False # Ensure marked closed
                self.logger.warning(f"Position {symbol} SET TO OpenRetryCooldown. Retry {retry_count}/{max_retries}, cooldown until {cooldown_until:.1f}") # Log state SET
                max_reached = False
                
                # Publish retry attempt event
                retry_info["final_state"] = "OpenRetryCooldown"
                event_type = "RETRY_ATTEMPT"

            # --- VERIFY ---
            final_set_strategy = pos.get("strategy") # Read back the value just set
            self.logger.info(f"VERIFY Post handle_failed_open state for {symbol}: strategy='{final_set_strategy}', retry_count={pos.get('retry_count')}, cooldown_until={pos.get('cooldown_until')}")
            # --- END VERIFY ---

        # Publish retry/recovery event (outside of lock)
        self._publish_position_retry_recovery_event(
            symbol=symbol,
            event_type=event_type,
            retry_info=retry_info,
            master_correlation_id=None,  # No correlation context for retry handling
            causation_id=None
        )

        return max_reached # Return the determined value

    def reset_retry_state(self, symbol: str) -> None:
        """Resets the retry count and cooldown for a symbol."""
        symbol = symbol.upper()
        old_retry_count = 0
        
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            old_retry_count = pos.get("retry_count", 0)
            old_cooldown_until = pos.get("cooldown_until", 0.0)
            pos["retry_count"] = 0
            pos["cooldown_until"] = 0.0
            self.logger.debug(f"Reset retry state for {symbol} (was: retry_count={old_retry_count})")
        
        # Publish retry reset event if there was a retry state to reset
        if old_retry_count > 0:
            retry_info = {
                "previous_retry_count": old_retry_count,
                "previous_cooldown_until": old_cooldown_until,
                "reset_reason": "manual_reset"
            }
            
            self._publish_position_retry_recovery_event(
                symbol=symbol,
                event_type="RETRY_STATE_RESET",
                retry_info=retry_info,
                master_correlation_id=None,  # No correlation context for manual reset
                causation_id=None
            )

    def clear_failed_state(self, symbol: str) -> None:
        """Clears any failed state for a symbol."""
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            pos["retry_count"] = 0
            pos["cooldown_until"] = 0.0
            self.logger.debug(f"Cleared failed state for {symbol}")

    def force_close_state(self, symbol: str, reason: str = "unknown") -> None:
        """
        Forces a position to closed state.

        Args:
            symbol: The stock symbol
            reason: The reason for forcing the position to closed state
        """
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)

            # Store old values for logging
            old_open = pos.get("open", False)
            old_strategy = pos.get("strategy", "closed")
            old_shares = pos.get("total_shares", 0.0)
            old_avg_price = pos.get("total_avg_price", 0.0)

            # ---- ADD LOGGING FOR OLD FINGERPRINT ----
            old_fingerprint = pos.get("last_broker_fingerprint", "Not Set")
            if old_fingerprint != "Not Set": # Only log if it was actually set
                 self.logger.info(f"PM [{symbol}]: Clearing last_broker_fingerprint in force_close_state. Old FP was: {old_fingerprint}. Reason: {reason}")
            # ---- END LOGGING ----

            # Update position state
            pos["open"] = False
            pos["strategy"] = "closed"
            pos["last_broker_fingerprint"] = None # Clear fingerprint

            # Keep shares/avg_price/realized_pnl for reference

            # Detailed logging for position changes
            self.logger.critical(f"PM POSITION CHANGE in force_close_state for {symbol}: "
                          f"Shares: {old_shares:.2f} (unchanged), "
                          f"AvgPrice: {old_avg_price:.4f} (unchanged), "
                          f"Strategy: {old_strategy} -> closed, "
                          f"Open: {old_open} -> False, "
                          f"Reason: {reason}, "
                          f"Timestamp: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

            self.logger.info(f"Forced position {symbol} to closed state and cleared fingerprint. Reason: {reason}")

            # Publish position update to Redis
            # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
            pos_after_force_close = self._get_position_no_lock(symbol.upper())
            pos_snapshot = copy.deepcopy(pos_after_force_close)  # Create deep copy while locked
            
        # Position update publishing now handled via telemetry service
        self._publish_position_update_to_redis(
            symbol_upper=symbol.upper(),
            pos_state_dict=pos_snapshot,
            update_source=f"FORCE_CLOSE:{reason}",
            master_correlation_id=None,
            causation_id=None
        )

    def mark_position_opening(self, symbol: str, potential_avg_price: float) -> None:
        """Marks a position as in the process of opening."""
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            pos["open"] = True
            pos["strategy"] = "opening"  # Special state to indicate in-progress open
            pos["potential_avg_price"] = potential_avg_price
            self.logger.debug(f"Marked position {symbol} as opening with potential avg price {potential_avg_price:.4f}")

    def get_retry_count(self, symbol: str) -> int:
        """Gets the current retry count for a failed open."""
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            return pos.get("retry_count", 0)

    def sync_broker_position(self, symbol: str, broker_total_shares: float, broker_total_avg_price: float,
                              broker_realized_pnl: Optional[float] = None,
                              source: str = "unknown_sync",
                              lcm_parent_trade_id_from_sync: Optional[int] = None,
                              master_correlation_id_from_sync: Optional[str] = None) -> None:
        symbol_upper = symbol.upper()
        lcm_trade_id_for_sync: Optional[int] = None
        master_correlation_id_for_sync: Optional[str] = None

        # Get/Create LCM trade and its associated correlation_id
        if self.trade_lifecycle_manager:
            lcm_trade_id_for_sync = self.trade_lifecycle_manager.create_or_sync_trade(
                symbol_upper, broker_total_shares, broker_total_avg_price, source
            )
            if lcm_trade_id_for_sync:
                trade_data_from_lcm = self.trade_lifecycle_manager.get_trade_by_id(lcm_trade_id_for_sync)
                if trade_data_from_lcm:
                    master_correlation_id_for_sync = trade_data_from_lcm.get("correlation_id")  # Get the stored IntelliSense UUID

        with self._positions_lock:
            pos = self._get_position_no_lock(symbol_upper)

            # Store old values for logging
            old_total_shares = pos["total_shares"]
            old_total_avg_price = pos["total_avg_price"]
            old_strategy = pos.get("strategy", "closed")
            old_open = pos.get("open", False)

            self.logger.warning(
                f"PM SYNC for {symbol_upper} (Source: {source}): BrokerTotalShares={broker_total_shares}, BrokerAvgPx={broker_total_avg_price}. "
                f"Current PM: TotalShares={old_total_shares:.2f}, TotalAvgPx={old_total_avg_price:.4f}"
            )

            pos["total_shares"] = broker_total_shares
            pos["total_avg_price"] = broker_total_avg_price
            if broker_realized_pnl is not None: # If broker provides daily PnL, use it
                pos["overall_realized_pnl"] = broker_realized_pnl

            pos["open"] = abs(pos["total_shares"]) > 1e-9

            # INSTRUMENTATION: Log position state determination
            self.logger.critical(f"PM_POSITION_STATE: {symbol_upper} after broker sync. "
                               f"total_shares={pos['total_shares']:.1f}, is_open={pos['open']}, "
                               f"abs(shares)={abs(pos['total_shares']):.1f}, threshold=1e-9")

            new_strategy = "long" if pos["total_shares"] > 0 else ("short" if pos["total_shares"] < 0 else "closed")
            pos["strategy"] = new_strategy

            # CRITICAL FIX: Store the IDs obtained from LCM or passed parameters
            if lcm_trade_id_for_sync:
                pos["lcm_parent_trade_id"] = lcm_trade_id_for_sync
                # CRITICAL FIX: Use parent trade ID as position UUID for stable GUI grouping
                pos["position_uuid"] = f"{symbol_upper}-{lcm_trade_id_for_sync}"
                self.logger.info(f"CRITICAL FIX: Linked {symbol_upper} position to trade {lcm_trade_id_for_sync}")
            elif lcm_parent_trade_id_from_sync:
                pos["lcm_parent_trade_id"] = lcm_parent_trade_id_from_sync
                # CRITICAL FIX: Use parent trade ID as position UUID for stable GUI grouping
                pos["position_uuid"] = f"{symbol_upper}-{lcm_parent_trade_id_from_sync}"
                self.logger.info(f"CRITICAL FIX: Linked {symbol_upper} position to trade {lcm_parent_trade_id_from_sync}")
            else:
                self.logger.warning(f"CRITICAL ISSUE: {symbol_upper} position has NO trade ID linkage! This will cause 0 shares display.")
                
                # INSTRUMENTATION: Publish missing trade ID linkage validation failure
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="TestradePositionValidationDecision",
                            payload={
                                "decision_type": "TRADE_ID_LINKAGE_MISSING",
                                "decision_reason": "Position has no trade ID linkage - will cause 0 shares display in GUI",
                                "context": {
                                    "symbol": symbol_upper,
                                    "broker_shares": broker_total_shares,
                                    "broker_avg_price": broker_total_avg_price,
                                    "broker_realized_pnl": broker_realized_pnl,
                                    "sync_source": source,
                                    "lcm_trade_id_for_sync": lcm_trade_id_for_sync,
                                    "lcm_parent_trade_id_from_sync": lcm_parent_trade_id_from_sync,
                                    "will_generate_fallback_uuid": True,
                                    "validation_severity": "CRITICAL"
                                },
                                "validation_timestamp": time.time(),
                                "total_validation_errors": self._validation_errors_count,
                                "component": "PositionManager"
                            }
                        )
                    # Position validation metrics
                    self._validation_errors_count += 1
                    self._last_validation_error_time = time.time()

            if master_correlation_id_for_sync:
                pos["master_correlation_id"] = master_correlation_id_for_sync
            elif master_correlation_id_from_sync:
                pos["master_correlation_id"] = master_correlation_id_from_sync

            # FALLBACK: If no parent trade ID available, generate UUID
            if pos["position_uuid"] is None:
                pos["position_uuid"] = str(uuid.uuid4())

            # CORRELATION LOGGING: Track correlation ID flow
            try:
                from modules.shared.logging.correlation_logger import log_position_correlation
                log_position_correlation(
                    position_uuid=pos["position_uuid"],
                    lcm_trade_id=pos.get("lcm_parent_trade_id"),
                    master_correlation_id=pos.get("master_correlation_id"),
                    symbol=symbol_upper
                )
            except ImportError:
                pass  # Correlation logging is optional

            pos["last_sync_details"] = {
                "source": source,
                "timestamp": time.time(),
                "broker_shares_at_sync": broker_total_shares,
                "broker_avg_price_at_sync": broker_total_avg_price,
                "broker_realized_pnl_at_sync": broker_realized_pnl
            }
            pos["last_update_source"] = source

            if source == "startup_sync":
                pos["startup_shares"] = broker_total_shares
                pos["startup_avg_price"] = broker_total_avg_price
                pos["session_system_fills_ledger"] = [] # Reset session-specific system fills
                # overall_realized_pnl is set from broker_realized_pnl above for startup

            pos["last_update"] = time.time()
            pos["retry_count"] = 0
            pos["last_broker_fingerprint"] = None

            # Detailed logging for position changes
            self.logger.critical(f"PM POSITION CHANGE in sync_broker_position for {symbol_upper}: "
                          f"Shares: {old_total_shares:.2f} -> {broker_total_shares:.2f}, "
                          f"AvgPrice: {old_total_avg_price:.4f} -> {broker_total_avg_price:.4f}, "
                          f"Strategy: {old_strategy} -> {new_strategy}, "
                          f"Open: {old_open} -> {pos['open']}, "
                          f"Source: {source}, "
                          f"Timestamp: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

            self.logger.info(f"PM SYNCED for {symbol_upper}: Shares={pos['total_shares']:.2f}, LCM_ID={pos.get('lcm_parent_trade_id')}, MasterCorrID={pos.get('master_correlation_id')}")

            # Publish position update to Redis
            # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
            pos_after_sync = self._get_position_no_lock(symbol_upper)
            pos_snapshot = copy.deepcopy(pos_after_sync)  # Create deep copy while locked
            stored_master_corr_id = master_correlation_id_for_sync or pos.get("master_correlation_id")
            
        # Position update publishing now handled via telemetry service
        self._publish_position_update_to_redis(
            symbol_upper=symbol_upper,
            pos_state_dict=pos_snapshot,
            update_source=source,
            master_correlation_id=stored_master_corr_id,
            causation_id=None
        )

        # Create and publish PositionUpdateEvent to EventBus for MarketDataFilterService
        position_data = PositionUpdateEventData(
            symbol=symbol_upper,
            quantity=broker_total_shares,
            average_price=broker_total_avg_price,
            realized_pnl_session=pos_snapshot.get("overall_realized_pnl", 0.0),
            is_open=pos_snapshot.get("open", False),
            strategy=pos_snapshot.get("strategy", "closed"),
            last_update_timestamp=time.time(),
            update_source_trigger=source,
            position_uuid=pos_snapshot.get("position_uuid"),
            causing_correlation_id=stored_master_corr_id
        )
        position_event = PositionUpdateEvent(data=position_data)
        if self.event_bus:
            # INSTRUMENTATION: Log detailed PositionUpdateEvent before publishing from sync
            self.logger.critical(f"PM_SYNC_EVENT_PUBLISH: Publishing PositionUpdateEvent for {symbol_upper} (from sync). "
                               f"is_open={position_data.is_open}, shares={position_data.quantity:.1f}, "
                               f"avg_px={position_data.average_price:.4f}, strategy={position_data.strategy}, "
                               f"source={position_data.update_source_trigger}, timestamp={position_data.last_update_timestamp}")

            self.event_bus.publish(position_event)
            self.logger.info(f"Published PositionUpdateEvent for sync_broker_position {symbol_upper}: {broker_total_shares:.2f} @ {broker_total_avg_price:.4f}")
            
            # CRITICAL: Also publish SymbolPublishingStateChangedEvent for market data filtering
            from core.events import SymbolPublishingStateChangedEvent, SymbolPublishingStateChangedData
            publishing_state_data = SymbolPublishingStateChangedData(
                symbol=symbol_upper,
                should_publish=pos_snapshot.get("open", False),  # Publish market data if position is open
                reason=f"POSITION_SYNC_{source.upper()}" if pos_snapshot.get("open", False) else "POSITION_CLOSED"
            )
            publishing_state_event = SymbolPublishingStateChangedEvent(data=publishing_state_data)
            self.event_bus.publish(publishing_state_event)
            self.logger.info(f"Published SymbolPublishingStateChangedEvent for {symbol_upper}: should_publish={publishing_state_data.should_publish}")
        else:
            self.logger.warning(f"EventBus not available, cannot publish PositionUpdateEvent for {symbol_upper}")

    def add_ledger_index(self, symbol: str, ledger_index: int) -> None:
        """Adds an index from the main event ledger to the position's history (optional)."""
        symbol = symbol.upper()
        with self._positions_lock:
            pos = self._get_position_no_lock(symbol)
            if "ledger_indices" not in pos:
                pos["ledger_indices"] = []
            pos["ledger_indices"].append(ledger_index)
            self.logger.debug(f"Added ledger index {ledger_index} to position {symbol}")

    def set_position_closed(self, symbol: str, reason: str = "set_position_closed") -> None:
        """
        Sets a position to closed state. Uses force_close_state for consistency.

        Args:
            symbol: The stock symbol
            reason: The reason for setting the position to closed state
        """
        # For consistency, we'll just call force_close_state
        self.force_close_state(symbol, reason=reason)

    def process_manual_adjustment(self, symbol: str, quantity_change: float, price_for_avg_cost: float) -> None:
        """
        Process a manual position adjustment.

        Args:
            symbol: The stock symbol
            quantity_change: The change in quantity (positive for increase, negative for decrease)
            price_for_avg_cost: The price to use for average cost calculation
        """
        symbol_upper = symbol.upper()
        self.logger.info(f"PM processing manual adjustment: {symbol_upper} quantity_change={quantity_change} @ {price_for_avg_cost:.4f}")

        with self._positions_lock:
            pos = self._get_position_no_lock(symbol_upper)

            old_total_shares = pos["total_shares"]
            old_total_avg_price = pos["total_avg_price"]
            old_strategy = pos.get("strategy", "closed")

            # Calculate new position
            new_total_shares = old_total_shares + quantity_change

            # Update average price based on the adjustment
            if quantity_change > 0:  # Adding shares
                if abs(new_total_shares) > 1e-9:
                    new_total_avg_price = ((old_total_shares * old_total_avg_price) + (quantity_change * price_for_avg_cost)) / new_total_shares
                else:
                    new_total_avg_price = 0.0
            else:  # Reducing shares
                # For reductions, average price typically doesn't change unless position flips or goes flat
                new_total_avg_price = old_total_avg_price
                if abs(new_total_shares) < 1e-9:
                    new_total_avg_price = 0.0  # Position is now flat

            pos["total_shares"] = new_total_shares
            pos["total_avg_price"] = new_total_avg_price

            pos["open"] = abs(pos["total_shares"]) > 1e-9
            new_strategy = "long" if pos["total_shares"] > 0 else ("short" if pos["total_shares"] < 0 else "closed")
            pos["strategy"] = new_strategy

            # Update timestamps
            adjustment_time = time.time()
            pos["last_update_source"] = "manual_adjustment"
            pos["last_update"] = adjustment_time
            pos["last_update_timestamp"] = adjustment_time

            self.logger.critical(f"PM POSITION CHANGE in process_manual_adjustment for {symbol_upper}: "
                          f"Shares: {old_total_shares:.2f} -> {new_total_shares:.2f}, "
                          f"AvgPrice: {old_total_avg_price:.4f} -> {new_total_avg_price:.4f}, "
                          f"Strategy: {old_strategy} -> {new_strategy}, "
                          f"Open: {pos['open']}, "
                          f"Adjustment: {quantity_change}@{price_for_avg_cost:.4f}")

            # Publish position update to Redis
            # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
            pos_after_manual_adj = self._get_position_no_lock(symbol_upper)
            pos_snapshot = copy.deepcopy(pos_after_manual_adj)  # Create deep copy while locked
            
        # Position update publishing now handled via telemetry service
        self._publish_position_update_to_redis(
            symbol_upper=symbol_upper,
            pos_state_dict=pos_snapshot,
            update_source="MANUAL_ADJUSTMENT",
            master_correlation_id=None,
            causation_id=None
        )

        # Create and publish PositionUpdateEvent
        position_data = PositionUpdateEventData(
            symbol=symbol_upper,
            quantity=pos_snapshot.get("total_shares", 0.0),
            average_price=pos_snapshot.get("total_avg_price", 0.0),
            realized_pnl_session=pos_snapshot.get("overall_realized_pnl", 0.0),
            is_open=pos_snapshot.get("open", False),
            strategy=pos_snapshot.get("strategy", "closed"),
            last_update_timestamp=pos_snapshot.get("last_update", time.time()),
            update_source_trigger="MANUAL_ADJUSTMENT",
            position_uuid=pos_snapshot.get("position_uuid")
        )
        position_event = PositionUpdateEvent(data=position_data)

        # INSTRUMENTATION: Log detailed PositionUpdateEvent before publishing
        self.logger.critical(f"PM_EVENT_PUBLISH: Publishing PositionUpdateEvent for {symbol_upper}. "
                           f"is_open={position_data.is_open}, shares={position_data.quantity:.1f}, "
                           f"avg_px={position_data.average_price:.4f}, strategy={position_data.strategy}, "
                           f"source={position_data.update_source_trigger}, timestamp={position_data.last_update_timestamp}")

        if self.event_bus:
            self.event_bus.publish(position_event)
            self.logger.info(f"Published PositionUpdateEvent for manual adjustment {symbol_upper}: {pos_snapshot.get('total_shares', 0.0):.2f} @ {pos_snapshot.get('total_avg_price', 0.0):.4f}")
        else:
            self.logger.warning(f"EventBus not available, cannot publish PositionUpdateEvent for {symbol_upper}")

    # --- Helper to get/create position dict internally ---
    def _get_position_no_lock(self, symbol: str) -> Dict[str, Any]:
        if symbol not in self._positions:
            self.logger.debug(f"PositionManager: Creating new state for {symbol}")
            self._positions[symbol] = {
                "symbol": symbol,
                "open": False,
                "strategy": "closed",

                "total_shares": 0.0,
                "total_avg_price": 0.0,
                "overall_realized_pnl": 0.0,

                "session_system_fills_ledger": [], # List of: {ts, type, shares, price, pnl_contrib, local_id}

                "startup_shares": 0.0,
                "startup_avg_price": 0.0,

                "last_sync_details": { # Stores info about the last direct broker sync
                    "source": None, # e.g., "startup_sync", "poll_reconcile"
                    "timestamp": 0.0,
                    "broker_shares_at_sync": 0.0,
                    "broker_avg_price_at_sync": 0.0
                },
                "last_update_source": None, # "system_fill", "startup_sync", "poll_reconcile"

                "last_update": 0.0,
                "last_update_timestamp": 0.0,  # For PositionData compatibility
                "trading_status": "Unknown",
                "luld_high": None,
                "luld_low": None,
                "last_broker_fingerprint": None,
                "retry_count": 0,
                "cooldown_until": 0.0,
                "potential_avg_price": 0.0, # Still useful for "opening" state

                # --- IntelliSense Correlation Tracking ---
                "lcm_parent_trade_id": None,    # LCM integer t_id from TradeLifecycleManagerService
                "master_correlation_id": None, # IntelliSense UUID for end-to-end nanosecond tracking
                "position_uuid": None # Will be set to parent_trade_id when available, fallback to UUID
                # "ledger_indices" can be removed if session_system_fills_ledger is primary
            }
        return self._positions[symbol]
    
    def start(self):
        """Start the PositionManager service."""
        self.logger.info("Starting PositionManager...")
        
        # Start IPC worker only if telemetry service not available
        if not self._correlation_logger:
            if not self._ipc_worker_thread or not self._ipc_worker_thread.is_alive():
                try:
                    self._start_ipc_worker()
                    self.logger.info("PositionManager using local IPC worker (telemetry service not available)")
                except Exception as e:
                    self.logger.error(f"Failed to start IPC worker thread: {e}")
                    # Continue anyway - PositionManager can work without IPC
        else:
            self.logger.info("PositionManager using telemetry service for IPC")
        
        # INSTRUMENTATION: Publish service health status - STARTED
        if self._correlation_logger:
            health_payload = {
                "health_status": "STARTED",
                "status_reason": "PositionManager successfully initialized and ready",
                "context": {
                    "service_name": "PositionManager",
                    "ipc_worker_started": self._ipc_worker_thread and self._ipc_worker_thread.is_alive() if not self._correlation_logger else False,
                    "correlation_logger_available": self._correlation_logger is not None,
                    "trade_lifecycle_manager_available": self.trade_lifecycle_manager is not None,
                    "positions_count": len(self._positions),
                    "health_status": "STARTED",
                    "startup_timestamp": time.time()
                },
                "component": "PositionManager",
                "health_timestamp": time.time()
            }
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="TestradeServiceHealthStatus",
                payload=health_payload
            )
        
        # Load initial positions from broker to populate PositionManager
        self._load_initial_broker_positions()
        
        self.logger.info("PositionManager started successfully")
    
    def _load_initial_broker_positions(self):
        """Load initial positions from broker and populate PositionManager on startup."""
        if not self._broker_service:
            self.logger.warning("PositionManager: No broker service available for initial position loading")
            return
            
        try:
            self.logger.critical("PM_STARTUP_BOOTSTRAP: Loading initial positions from broker...")
            
            # Get all positions from broker
            positions_data, success = self._broker_service.get_all_positions(timeout=10.0)
            
            if not success or not positions_data:
                self.logger.warning("PM_STARTUP_BOOTSTRAP: Failed to load positions from broker or no positions found")
                return
                
            self.logger.critical(f"PM_STARTUP_BOOTSTRAP: Loaded {len(positions_data)} positions from broker")
            
            # Process each position and publish PositionUpdateEvent
            for position_dict in positions_data:
                try:
                    symbol = position_dict.get('symbol', '').upper()
                    quantity = float(position_dict.get('quantity', 0.0))
                    avg_price = float(position_dict.get('average_price', 0.0))
                    
                    if not symbol or abs(quantity) < 1e-9:
                        continue  # Skip empty positions
                        
                    self.logger.critical(f"PM_STARTUP_BOOTSTRAP: Processing {symbol} - Qty: {quantity}, AvgPx: {avg_price}")
                    
                    # Create position in internal storage
                    with self._positions_lock:
                        self._positions[symbol] = {
                            'symbol': symbol,
                            'total_shares': quantity,
                            'total_avg_price': avg_price,
                            'open': abs(quantity) > 1e-9,
                            'strategy': 'broker_bootstrap',
                            'overall_realized_pnl': 0.0,
                            'last_update': time.time(),
                            'position_uuid': f"bootstrap_{symbol}_{int(time.time())}",
                            'opening_signal_correlation_id': f"broker_bootstrap_{symbol}",
                            'opening_order_correlation_id': f"broker_bootstrap_{symbol}"
                        }
                    
                    # Create and publish PositionUpdateEvent to trigger ActiveSymbolsService
                    from core.events import PositionUpdateEvent, PositionUpdateEventData
                    position_event_data = PositionUpdateEventData(
                        symbol=symbol,
                        quantity=quantity,
                        average_price=avg_price,
                        realized_pnl_session=0.0,
                        is_open=abs(quantity) > 1e-9,
                        strategy='broker_bootstrap',
                        last_update_timestamp=time.time(),
                        update_source_trigger="BROKER_STARTUP_BOOTSTRAP",
                        position_uuid=f"bootstrap_{symbol}_{int(time.time())}",
                        causing_correlation_id=f"broker_bootstrap_{symbol}",
                        opening_signal_correlation_id=f"broker_bootstrap_{symbol}",
                        opening_order_correlation_id=f"broker_bootstrap_{symbol}"
                    )
                    
                    position_event = PositionUpdateEvent(data=position_event_data)
                    
                    self.logger.critical(f"PM_STARTUP_EVENT: Publishing PositionUpdateEvent for {symbol} - qty={quantity}")
                    self.event_bus.publish(position_event)
                    
                    # Also log to correlation logger for Redis streams (same as other position update methods)
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="PositionManager",
                            event_name="PositionUpdate",
                            payload={
                                "symbol": symbol,
                                "quantity": quantity,
                                "average_price": avg_price,
                                "realized_pnl_session": 0.0,
                                "is_open": abs(quantity) > 1e-9,
                                "strategy": "broker_bootstrap",
                                "last_update_timestamp": time.time(),
                                "causing_correlation_id": f"broker_bootstrap_{symbol}",
                                "update_source_trigger": "BROKER_STARTUP_BOOTSTRAP",
                                "position_uuid": f"bootstrap_{symbol}_{int(time.time())}",
                                "opening_signal_correlation_id": f"broker_bootstrap_{symbol}",
                                "opening_order_correlation_id": f"broker_bootstrap_{symbol}"
                            }
                        )
                        self.logger.critical(f"PM_STARTUP_EVENT: Logged position to correlation logger for {symbol}")
                    
                except Exception as e:
                    self.logger.error(f"PM_STARTUP_BOOTSTRAP: Error processing position {position_dict}: {e}")
                    continue
                    
            self.logger.critical(f"PM_STARTUP_BOOTSTRAP: Completed - {len(self._positions)} positions loaded and events published")
            
        except Exception as e:
            self.logger.error(f"PM_STARTUP_BOOTSTRAP: Failed to load initial broker positions: {e}", exc_info=True)
    
    def stop(self):
        """Stop the PositionManager service."""
        self.shutdown()
    
    def shutdown(self):
        """Shutdown the PositionManager and cleanup resources."""
        self.logger.info("Shutting down PositionManager...")
        
        # Signal worker thread to stop
        self._ipc_shutdown_event.set()
        
        # Wait for worker thread to finish
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            self._ipc_worker_thread.join(timeout=5.0)
            if self._ipc_worker_thread.is_alive():
                self.logger.warning("IPC worker thread did not shutdown cleanly")
        
        # Log any remaining queued messages
        remaining = self._ipc_queue.qsize()
        if remaining > 0:
            self.logger.warning(f"Shutting down with {remaining} IPC messages still queued")
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        if self._correlation_logger:
            health_payload = {
                "health_status": "STOPPED",
                "status_reason": "PositionManager stopped and cleaned up resources",
                "context": {
                    "service_name": "PositionManager",
                    "remaining_ipc_queue_size": remaining,
                    "worker_thread_shutdown": True,
                    "positions_count": len(self._positions),
                    "ipc_failures": self._ipc_failures,
                    "circuit_breaker_open": self._ipc_circuit_open,
                    "health_status": "STOPPED",
                    "shutdown_timestamp": time.time()
                },
                "component": "PositionManager",
                "health_timestamp": time.time()
            }
            self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name="TestradeServiceHealthStatus",
                payload=health_payload
            )
        
        self.logger.info("PositionManager shutdown complete")

    # ========== RESTORED TELEMETRY METHODS USING CORRELATION LOGGER ==========
    
    def _publish_telemetry_event(self, event_type: str, payload: dict, stream_name: str = None, correlation_id: str = None):
        """Helper method to publish events via correlation logger."""
        if self._correlation_logger:
            # Add correlation_id to payload
            if correlation_id:
                payload["__correlation_id"] = correlation_id
            
            # Convert event type to CamelCase event name
            event_name = self._convert_event_type_to_name(event_type)
            
            return self._correlation_logger.log_event(
                source_component="PositionManager",
                event_name=event_name,
                payload=payload,
                stream_override=stream_name
            )
        else:
            self.logger.debug(f"Cannot publish telemetry event {event_type}: no correlation logger available")
            return False
    
    def _convert_event_type_to_name(self, event_type: str) -> str:
        """Convert SNAKE_CASE event types to CamelCase event names."""
        # Map specific event types to proper names
        event_type_map = {
            "TESTRADE_POSITION_UPDATE": "PositionUpdate",
            "TESTRADE_POSITION_VALIDATION_DECISION": "PositionValidationDecision",
            "TESTRADE_POSITION_CALCULATION_ERROR": "PositionCalculationError",
            "TESTRADE_POSITION_STATE_TRANSITION": "PositionStateTransition",
            "TESTRADE_POSITION_RETRY_RECOVERY": "PositionRetryRecovery",
            "TESTRADE_SERVICE_HEALTH_STATUS": "TestradeServiceHealthStatus"
        }
        
        if event_type in event_type_map:
            return event_type_map[event_type]
        
        # Fallback: convert snake_case to CamelCase
        parts = event_type.lower().split('_')
        return ''.join(word.capitalize() for word in parts)
    
    def _publish_position_validation_decision(self, decision_type: str, decision_reason: str, 
                                            context: Dict[str, Any], master_correlation_id: str = None, causation_id: str = None) -> None:
        """
        Publish position validation decision to Redis stream via correlation logger.
        
        Args:
            decision_type: Type of validation decision (e.g., "TRADE_ID_LINKAGE_MISSING", "BROKER_POSITION_MISMATCH")
            decision_reason: Human-readable reason for the decision
            context: Additional context data for the decision
            master_correlation_id: Optional master correlation ID for end-to-end tracking
            causation_id: Optional causation ID for tracking event causality
        """
        # Position validation metrics
        self._validation_errors_count += 1
        self._last_validation_error_time = time.time()
        
        try:
            # Create position validation decision payload
            payload = {
                "decision_type": decision_type,
                "decision_reason": decision_reason,
                "context": context,
                "validation_timestamp": time.time(),
                "component": "PositionManager"
            }
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
            if causation_id:
                payload["causation_id"] = causation_id
            
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping position validation decision - ENABLE_IPC_DATA_DUMP is False")
                return
                
            # Get target stream from config
            target_stream = 'testrade:position-validation'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_position_validation', target_stream)
            
            # Use correlation logger for clean, fire-and-forget validation decision publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="PositionValidationDecision",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Position validation decision enqueued for stream '{target_stream}': {decision_type}")
            else:
                self.logger.debug(f"Cannot publish position validation decision: no correlation logger available")
            
        except Exception as e:
            self.logger.error(f"Error publishing position validation decision: {e}", exc_info=True)
    
    def _publish_position_calculation_error(self, error_type: str, error_reason: str, 
                                          context: Dict[str, Any], master_correlation_id: str = None, causation_id: str = None) -> None:
        """
        Publish position calculation error to Redis stream via correlation logger.
        
        Args:
            error_type: Type of calculation error (e.g., "DIVISION_BY_ZERO", "INVALID_PRICE_DATA")
            error_reason: Human-readable reason for the error
            context: Additional context data for the error
            master_correlation_id: Optional master correlation ID for end-to-end tracking
            causation_id: Optional causation ID for tracking event causality
        """
        try:
            # Create position calculation error payload
            payload = {
                "error_type": error_type,
                "error_reason": error_reason,
                "context": context,
                "calculation_timestamp": time.time(),
                "component": "PositionManager"
            }
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
            if causation_id:
                payload["causation_id"] = causation_id
            
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping position calculation error - ENABLE_IPC_DATA_DUMP is False")
                return
                
            # Get target stream from config
            target_stream = 'testrade:position-errors'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_position_errors', target_stream)
            
            # Use correlation logger for clean, fire-and-forget error publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="PositionCalculationError",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Position calculation error enqueued for stream '{target_stream}': {error_type}")
            else:
                self.logger.debug(f"Cannot publish position calculation error: no correlation logger available")
                
        except Exception as e:
            self.logger.error(f"Error publishing position calculation error: {e}", exc_info=True)
    
    def _publish_position_state_transition(self, symbol: str, old_state: Dict[str, Any], new_state: Dict[str, Any], 
                                          transition_type: str, trigger_event: str, master_correlation_id: str = None, causation_id: str = None) -> None:
        """
        Publish position state transition events for monitoring and analysis.
        
        Args:
            symbol: Trading symbol
            old_state: Previous position state
            new_state: New position state after transition
            transition_type: Type of transition (e.g., "OPENED", "CLOSED", "QUANTITY_CHANGE", "STRATEGY_CHANGE")
            trigger_event: What triggered this transition (e.g., "FILL_PROCESSED", "BROKER_SYNC", "MANUAL_ADJUSTMENT")
            master_correlation_id: Optional master correlation ID for end-to-end tracking
            causation_id: Optional causation ID for tracking event causality
        """
        try:
            # Calculate meaningful state changes
            old_shares = old_state.get("total_shares", 0.0)
            new_shares = new_state.get("total_shares", 0.0)
            old_strategy = old_state.get("strategy", "closed")
            new_strategy = new_state.get("strategy", "closed")
            old_avg_price = old_state.get("total_avg_price", 0.0)
            new_avg_price = new_state.get("total_avg_price", 0.0)
            
            # Create state transition payload
            payload = {
                "symbol": symbol,
                "transition_type": transition_type,
                "trigger_event": trigger_event,
                "old_state": {
                    "shares": old_shares,
                    "strategy": old_strategy,
                    "avg_price": old_avg_price,
                    "open": old_state.get("open", False)
                },
                "new_state": {
                    "shares": new_shares,
                    "strategy": new_strategy,
                    "avg_price": new_avg_price,
                    "open": new_state.get("open", False)
                },
                "state_delta": {
                    "shares_change": new_shares - old_shares,
                    "strategy_changed": old_strategy != new_strategy,
                    "avg_price_change": new_avg_price - old_avg_price
                },
                "transition_timestamp": time.time(),
                "component": "PositionManager"
            }
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
            if causation_id:
                payload["causation_id"] = causation_id
            
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping position state transition - ENABLE_IPC_DATA_DUMP is False")
                return
                
            # Get target stream from config
            target_stream = 'testrade:position-transitions'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_position_transitions', target_stream)
            
            # Use correlation logger for clean, fire-and-forget transition publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="PositionStateTransition",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Position state transition enqueued for stream '{target_stream}': {symbol} {transition_type}")
            else:
                self.logger.debug(f"Cannot publish position state transition: no correlation logger available")
                
        except Exception as e:
            self.logger.error(f"Error publishing position state transition: {e}", exc_info=True)
    
    def _publish_position_retry_recovery_event(self, symbol: str, event_type: str, retry_info: Dict[str, Any], 
                                             master_correlation_id: str = None, causation_id: str = None) -> None:
        """
        Publish position retry/recovery events for monitoring trade execution resilience.
        
        Args:
            symbol: Trading symbol
            event_type: Type of retry/recovery event (e.g., "RETRY_ATTEMPT", "MAX_RETRIES_REACHED", "RETRY_SUCCESS", "RECOVERY_INITIATED")
            retry_info: Information about the retry/recovery attempt
            master_correlation_id: Optional master correlation ID for end-to-end tracking
            causation_id: Optional causation ID for tracking event causality
        """
        try:
            # Create retry/recovery event payload
            payload = {
                "symbol": symbol,
                "event_type": event_type,
                "retry_info": retry_info,
                "event_timestamp": time.time(),
                "component": "PositionManager",
                "severity": self._classify_retry_event_severity(event_type)
            }
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
            if causation_id:
                payload["causation_id"] = causation_id
            
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping position retry recovery event - ENABLE_IPC_DATA_DUMP is False")
                return
                
            # Get target stream from config
            target_stream = 'testrade:position-retries'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_position_retries', target_stream)
            
            # Use correlation logger for clean, fire-and-forget retry event publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="PositionRetryRecovery",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Position retry/recovery event enqueued for stream '{target_stream}': {symbol} {event_type}")
            else:
                self.logger.debug(f"Cannot publish position retry/recovery event: no correlation logger available")
                
        except Exception as e:
            self.logger.error(f"Error publishing position retry recovery event: {e}", exc_info=True)
    
    def _classify_retry_event_severity(self, event_type: str) -> str:
        """Classify the severity of a retry event."""
        if event_type == "MAX_RETRIES_REACHED":
            return "ERROR"
        elif event_type in ["RETRY_SUCCESS", "RECOVERY_INITIATED"]:
            return "INFO"
        else:
            return "WARNING"
    
    def _publish_position_update_to_redis(self,
                                      symbol_upper: str,
                                      pos_state_dict: Dict[str, Any],
                                      update_source: str,
                                      master_correlation_id: Optional[str] = None,
                                      causation_id: Optional[str] = None):
        """
        Serializes and enqueues a position update for Redis publishing via correlation logger.
        """
        try:
            import dataclasses
            # CRITICAL FIX: Create the specific payload object with explicit None handling
            payload_obj = PositionUpdateEventData(
                symbol=symbol_upper,
                quantity=pos_state_dict.get("total_shares", 0.0),
                average_price=pos_state_dict.get("total_avg_price", 0.0),
                realized_pnl_session=pos_state_dict.get("overall_realized_pnl", 0.0),  # FIX: Default to 0.0 instead of None
                is_open=pos_state_dict.get("open", False),
                strategy=pos_state_dict.get("strategy", "closed"),  # FIX: Default to "closed" instead of None
                last_update_timestamp=pos_state_dict.get("last_update", time.time()),
                causing_correlation_id=master_correlation_id,
                causation_id=causation_id,  # Now supported by dataclass
                update_source=update_source  # Now supported by dataclass
            )
            
            # Convert dataclass to dict
            payload = dataclasses.asdict(payload_obj)
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
                
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping position update - ENABLE_IPC_DATA_DUMP is False")
                return
                
            # Get target stream from config
            target_stream = 'testrade:position-updates'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_position_updates', target_stream)
            
            # Use correlation logger for clean, fire-and-forget position update publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="PositionUpdate",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Position update enqueued for stream '{target_stream}': {symbol_upper}")
            else:
                self.logger.debug(f"Cannot publish position update: no correlation logger available")
                
        except Exception as e:
            self.logger.error(f"Error publishing position update to Redis: {e}", exc_info=True)
    
    def _publish_service_health_status(self, health_status: str, status_reason: str, 
                                     context: Dict[str, Any], master_correlation_id: str = None, causation_id: str = None) -> None:
        """
        Publish service health status event to Redis stream via correlation logger.
        
        Args:
            health_status: Health status (e.g., "STARTED", "STOPPED", "HEALTHY", "DEGRADED", "FAILED")
            status_reason: Human-readable reason for the health status
            context: Additional context data for the health status
            master_correlation_id: Optional master correlation ID for end-to-end tracking
            causation_id: Optional causation ID for tracking event causality
        """
        try:
            # Create service health status payload
            payload = {
                "health_status": health_status,
                "status_reason": status_reason,
                "context": context,
                "status_timestamp": time.time(),
                "component": "PositionManager"
            }
            
            # Add correlation tracking
            if master_correlation_id:
                payload["__correlation_id"] = master_correlation_id
            if causation_id:
                payload["causation_id"] = causation_id
            
            # Check if IPC data dumping is disabled (TANK mode)
            if self._config_service and hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and not self._config_service.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping service health status - ENABLE_IPC_DATA_DUMP is False")
                return
            
            # Get target stream from config
            target_stream = 'testrade:system-health'
            if self._config_service:
                target_stream = getattr(self._config_service, 'redis_stream_system_health', target_stream)
            
            # Use correlation logger for clean, fire-and-forget health status publish
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PositionManager",
                    event_name="TestradeServiceHealthStatus",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Service health status enqueued for stream '{target_stream}': {health_status}")
            else:
                # Health status is non-critical instrumentation - log at debug level only
                self.logger.debug(f"Cannot publish service health status: no correlation logger available")
                
        except Exception as e:
            self.logger.error(f"Error publishing service health status: {e}", exc_info=True)
    
    def _publish_periodic_health_status(self) -> None:
        """
        Publish periodic health status based on current service state and performance metrics.
        """
        try:
            # Determine current health status based on operational metrics
            ipc_queue_size = self._ipc_queue.qsize() if hasattr(self, '_ipc_queue') else 0
            queue_capacity_pct = (ipc_queue_size / self._ipc_queue.maxsize) * 100 if self._ipc_queue.maxsize > 0 else 0
            
            # Check various health indicators
            health_status = "HEALTHY"
            status_issues = []
            
            # IPC queue capacity check
            if queue_capacity_pct > 90:
                health_status = "CRITICAL"
                status_issues.append(f"IPC queue at {queue_capacity_pct:.1f}% capacity")
            elif queue_capacity_pct > 80:
                health_status = "DEGRADED"
                status_issues.append(f"IPC queue at {queue_capacity_pct:.1f}% capacity")
            
            # Circuit breaker check
            if self._ipc_circuit_open:
                health_status = "DEGRADED"
                status_issues.append("IPC circuit breaker open")
            
            # IPC worker thread health check
            if not self._ipc_worker_thread or not self._ipc_worker_thread.is_alive():
                health_status = "DEGRADED"
                status_issues.append("IPC worker thread not running")
            
            # High failure rate check
            if self._ipc_failures > self._ipc_circuit_threshold / 2:
                if health_status != "CRITICAL":
                    health_status = "DEGRADED"
                status_issues.append(f"High IPC failure count: {self._ipc_failures}")
            
            # Correlation logger availability check
            if not self._correlation_logger:
                health_status = "DEGRADED"
                status_issues.append("Correlation logger not available")
            
            status_reason = f"Service operational - {', '.join(status_issues) if status_issues else 'All systems healthy'}"
            
            # Build context with performance metrics
            context = {
                "service_name": "PositionManager",
                "health_status": health_status,
                "ipc_queue_size": ipc_queue_size,
                "ipc_queue_capacity_pct": round(queue_capacity_pct, 2),
                "ipc_queue_maxsize": self._ipc_queue.maxsize,
                "ipc_failures": self._ipc_failures,
                "ipc_circuit_open": self._ipc_circuit_open,
                "ipc_worker_alive": self._ipc_worker_thread and self._ipc_worker_thread.is_alive(),
                "positions_count": len(self._positions),
                "correlation_logger_available": self._correlation_logger is not None,
                "trade_lifecycle_manager_available": self.trade_lifecycle_manager is not None,
                "validation_errors_count": getattr(self, '_validation_errors_count', 0),
                "last_validation_error_time": getattr(self, '_last_validation_error_time', 0.0),
                "health_check_timestamp": time.time()
            }
            
            # Only publish if status has changed or it's been a while (reduces noise)
            last_published_status = getattr(self, '_last_published_health_status', None)
            time_since_last_publish = time.time() - getattr(self, '_last_health_publish_time', 0)
            
            if (last_published_status != health_status or 
                time_since_last_publish > 300):  # Force publish every 5 minutes
                
                self._publish_service_health_status(health_status, status_reason, context, None, None)
                setattr(self, '_last_published_health_status', health_status)
                setattr(self, '_last_health_publish_time', time.time())
                
        except Exception as e:
            self.logger.error(f"Error in periodic health status publishing: {e}", exc_info=True)

