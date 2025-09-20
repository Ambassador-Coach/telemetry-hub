# modules/trade_management/trade_executor.py
import logging
import time
import threading
import queue
from typing import Dict, Any, Optional, Tuple
from time import perf_counter_ns

# Core interfaces
from interfaces.core.services import IEventBus
from interfaces.broker.services import IBrokerService
from interfaces.logging.services import ICorrelationLogger
from core.events import OpenOrderSubmittedEvent, OpenOrderSubmittedEventData, BrokerOrderSubmittedEvent, BrokerOrderSubmittedEventData
from utils.thread_safe_uuid import get_thread_safe_uuid

# Interfaces and Data Classes
from interfaces.trading.services import (
    ITradeExecutor,
    IPositionManager,
    IOrderStrategy,
    OrderParameters,
)
from data_models import TradeActionType
# Dependencies
from interfaces.order_management.services import IOrderRepository
from data_models.order_data_types import Order
from data_models import OrderLSAgentStatus
from interfaces.risk.services import IRiskManagementService # Assuming correct path
from interfaces.risk.services import RiskLevel # Import RiskLevel enum
from interfaces.data.services import IPriceProvider
from utils.global_config import GlobalConfig as IConfigService # Import for config service

# Utilities
from utils.performance_tracker import create_timestamp_dict, add_timestamp, is_performance_tracking_enabled # Assuming correct path

logger = logging.getLogger(__name__)

class TradeExecutor(ITradeExecutor):
    """
    Handles the execution of a trade order: final checks, repository interaction,
    broker placement, and fingerprint management.
    """

    def __init__(self,
                 broker: IBrokerService,
                 order_repository: IOrderRepository,
                 config_service: IConfigService,
                 event_bus: Optional[IEventBus] = None,
                 position_manager: Optional[IPositionManager] = None,
                 price_provider: Optional[IPriceProvider] = None,
                 order_strategy: Optional[IOrderStrategy] = None,
                 risk_service: Optional[IRiskManagementService] = None,
                 trade_manager_service=None,  # Kept for is_trading_enabled() and other methods
                 gui_logger_func=None,  # Callback function to log messages to GUI
                 pipeline_validator: Optional[Any] = None,  # Pipeline validator for order execution tracking
                 benchmarker: Optional[Any] = None,  # Performance benchmarker for metrics
                 correlation_logger: ICorrelationLogger = None,  # For IntelliSense logging
                 ):  # ApplicationCore dependency removed
        """
        Initializes the TradeExecutor.

        Args:
            broker: Service to interact with the broker API.
            order_repository: Service to manage local order copies.
            config_service: Service to access global configuration.
            position_manager: Optional service to manage internal position state (for checks/fingerprints).
            price_provider: Optional service to get price information.
            order_strategy: Optional service to calculate order parameters.
            risk_service: Optional service for pre-trade risk checks.
            trade_manager_service: Optional reference to TradeManagerService for checking trading_enabled status.
            gui_logger_func: Optional callback function to log messages to the GUI.
        """
        self._broker = broker
        self._order_repository = order_repository
        self._config = config_service  # Store config_service directly
        self._correlation_logger = correlation_logger  # Store correlation logger
        self._event_bus = event_bus
        self._pipeline_validator = pipeline_validator  # Store pipeline validator for order execution tracking
        self._benchmarker = benchmarker  # Store benchmarker for performance metrics
        self._position_manager = position_manager
        self._price_provider = price_provider
        self._order_strategy = order_strategy
        self._risk_service = risk_service
        self._trade_manager_service = trade_manager_service  # Keep for is_trading_enabled() and other methods
        self._gui_logger_func = gui_logger_func
        # Make the module-level logger available as an instance attribute
        self.logger = logger

        if not self._event_bus:
            self.logger.warning("TradeExecutor initialized WITHOUT EventBus. OpenOrderSubmittedEvent will not be published.")
            
        # Initialize async IPC publishing infrastructure
        self._ipc_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        # Thread creation deferred to start() method

        logger.info("TradeExecutor Initialized for direct execution calls. Thread creation deferred to start() method.")

    # --- Helper Methods ---
    def _get_tick_size(self, symbol: str, current_price: Optional[float]) -> float:
        """
        Determines the tick size for a given symbol and price.

        Implements standard US market tick size rules:
        - Price < $1.00: $0.0001 tick size (sub-penny pricing)
        - Price >= $1.00: $0.01 tick size (penny pricing)

        Args:
            symbol: Trading symbol (for future symbol-specific rules)
            current_price: Current price of the symbol

        Returns:
            Appropriate tick size for the price level
        """
        if current_price is None or current_price <= 0:
            # Log price validation issue for tick size calculation
            self.logger.debug(f"[{symbol}] Invalid price for tick size calculation: {current_price}. Using default 0.01")
            
            # Publish price validation warning for tick size calculation (non-critical)
            tick_price_warning_context = {
                "validation_warning_type": "TICK_SIZE_INVALID_PRICE_INPUT",
                "invalid_price_value": current_price,
                "symbol": symbol,
                "default_tick_size_used": 0.01,
                "warning_timestamp": time.time(),
                "validation_reason": "NONE_OR_NEGATIVE_PRICE_FOR_TICK_CALCULATION"
            }
            # Note: This is a debug-level event, only publish if correlation logger available
            if self._correlation_logger:
                try:
                    # Use a lightweight decision event for this non-critical validation
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionDecision",
                        payload={
                            "decision_type": "PRICE_VALIDATION_WARNING_TICK_SIZE",
                            "symbol": "N/A",
                            "local_order_id": "N/A",
                            **tick_price_warning_context
                        }
                    )
                except Exception:
                    pass  # Don't fail tick size calculation due to logging issues
            
            return 0.01  # Default tick size if price unknown

        # Standard US equity tick size rules
        if current_price < 1.00:
            return 0.0001  # Sub-penny pricing for stocks under $1
        else:
            return 0.01    # Penny pricing for stocks $1 and above

    def _is_regular_trading_hours(self) -> bool:
        """
        Determines if the current time is within regular trading hours.

        US market regular trading hours: 9:30 AM - 4:00 PM ET, Monday-Friday

        Returns:
            True if current time is within regular trading hours, False otherwise
        """
        try:
            import datetime
            import pytz

            # Define market open and close times in ET
            market_open_time = datetime.time(9, 30, 0)  # 9:30 AM ET
            market_close_time = datetime.time(16, 0, 0)  # 4:00 PM ET
            eastern = pytz.timezone('US/Eastern')

            # Get current time in ET
            now_utc = datetime.datetime.now(pytz.utc)
            now_et = now_utc.astimezone(eastern)

            # Check if it's a weekday (Monday=0, Sunday=6)
            if now_et.weekday() >= 5:  # Saturday or Sunday
                return False

            # Check if current time is within market hours
            current_time = now_et.time()
            is_rth = market_open_time <= current_time < market_close_time

            logger.debug(f"RTH Check: Current ET time: {current_time}, "
                        f"Weekday: {now_et.weekday()}, RTH: {is_rth}")

            return is_rth

        except Exception as e:
            logger.error(f"Error checking regular trading hours: {e}", exc_info=True)
            # Conservative fallback - assume it's NOT regular hours if we can't determine
            return False

    def _make_fingerprint(self, event_type: str, shares_float: float, cost_basis: float, realized_pnl: float, snapshot_version: Optional[str]) -> tuple:
        """Creates a tuple fingerprint to detect duplicate events. Rounds values."""
        # (Same implementation as previously in TradeManagerService)
        try:
            shares_int = int(round(shares_float))
            cost_rounded = round(float(cost_basis), 4) if isinstance(cost_basis, (int, float)) else 0.0
            pnl_rounded = round(float(realized_pnl), 2) if isinstance(realized_pnl, (int, float)) else 0.0
        except (ValueError, TypeError) as e:
            logger.error(f"Fingerprint creation error: Invalid numeric value - {e}. Shares: {shares_float}, Cost: {cost_basis}, PnL: {realized_pnl}")
            shares_int = 0; cost_rounded = 0.0; pnl_rounded = 0.0
        fp = (event_type.upper(), shares_int, cost_rounded, pnl_rounded, snapshot_version)
        logger.debug(f"Executor created fingerprint: {fp}")
        return fp

    # --- REMOVED: EventBus Integration ---
    # TradeExecutor is now called directly by TradeManagerService via execute_trade()
    # No longer subscribes to ValidatedOrderRequestEvent

    def stop(self):
        """Stop the TradeExecutor and clean up resources."""
        self.logger.info("TradeExecutor stopping...")
        # No background threads to stop in this basic implementation
        self.logger.info("TradeExecutor stopped.")

    # --- Main Execution Logic ---
    def execute_trade(self,
                      order_params: OrderParameters,
                      # Extra context needed for order repo / fingerprinting
                      time_stamps: Optional[Dict[str, Any]] = None,
                      snapshot_version: Optional[str] = None,
                      ocr_confidence: Optional[float] = None,
                      ocr_snapshot_cost_basis: Optional[float] = None, # Add parameter for OCR snapshot cost basis
                      extra_fields: Optional[Dict[str, Any]] = None,
                      is_manual_trigger: bool = False, # Flag for manual orders bypassing some checks
                      perf_timestamps: Optional[Dict[str, float]] = None,
                      aggressive_chase: bool = False # This flag is key
                     ) -> Optional[int]: # Returns local_id if successful
        """
        Executes a trade based on calculated parameters.

        Flow:
        1. Final Risk Assessment (if risk service available).
        2. Trading Status / LULD Check (via PositionManager).
        3. Create Local Order Record (via OrderRepository).
        4. Fingerprint Check (using PositionManager state).
        5. Place Order with Broker (via BrokerService).
        6. Update Fingerprint (via PositionManager).

        Args:
             order_params: The calculated parameters for the order.
             time_stamps: Timestamps from the original event trigger.
             snapshot_version: Identifier for the snapshot triggering this.
             ocr_confidence: Confidence score from OCR.
             extra_fields: Additional data from snapshot/trigger.
             is_manual_trigger: If True, might bypass some checks (like fingerprint).
             perf_timestamps: Performance tracking dict.
             aggressive_chase: If True, indicates this is an aggressive liquidation that should
                              chase the market if necessary (e.g., for critical risk events or liquidations).

        Returns:
             The local_id generated by the OrderRepository if submission initiated,
             None otherwise.
        """
        # T16: Trade execution pipeline start timing
        T16_ExecutionStart_ns = perf_counter_ns()
        
        symbol = order_params.symbol
        # The event_type from order_params should now be one of the specific, valid enum string values
        # from OCRScalpingSignalOrchestratorService (e.g., "OCR_SCALPING_OPEN_LONG")
        event_type_str_for_repo = order_params.event_type or \
                                  ("MANUAL_" + order_params.side.upper() if is_manual_trigger and order_params.side else "UNKNOWN")
        action_type = order_params.action_type # e.g., TradeActionType.OPEN_LONG, TradeActionType.CLOSE_POSITION

        # Extract correlation and causation metadata from order parameters for end-to-end tracking
        correlation_id = getattr(order_params, 'correlation_id', None)
        validation_id = extra_fields.get('validation_id') if extra_fields else None
        source_event_id = getattr(order_params, 'source_event_id', None)

        logger.info(f"[{symbol}] Attempting execution for {event_type_str_for_repo} order: {order_params.quantity} @ {order_params.limit_price}")

        # Validate order price parameters early in execution
        if order_params.order_type == "LMT" and order_params.limit_price is not None:
            if order_params.limit_price <= 0:
                self.logger.error(f"[{symbol}] Invalid limit price: {order_params.limit_price}. Must be positive.")
                price_validation_context = {
                    "validation_failure_type": "INVALID_LIMIT_PRICE_NON_POSITIVE",
                    "invalid_limit_price": order_params.limit_price,
                    "symbol": symbol,
                    "order_side": order_params.side,
                    "order_quantity": order_params.quantity,
                    "order_type": order_params.order_type,
                    "failure_timestamp": time.time(),
                    "validation_rule": "LIMIT_PRICE_MUST_BE_POSITIVE",
                    "failure_reason": "ORDER_PARAMETER_VALIDATION_FAILED"
                }
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionRejection",
                        payload={
                            "rejection_type": "INVALID_ORDER_PRICE_PARAMETERS",
                            "symbol": order_params.symbol if order_params else "N/A",
                            **price_validation_context
                        }
                    )
                return None
            elif order_params.limit_price > 10000:  # Reasonable upper bound check
                self.logger.warning(f"[{symbol}] Unusually high limit price: {order_params.limit_price}. Proceeding with caution.")
                high_price_context = {
                    "validation_warning_type": "UNUSUALLY_HIGH_LIMIT_PRICE",
                    "high_limit_price": order_params.limit_price,
                    "symbol": symbol,
                    "order_side": order_params.side,
                    "order_quantity": order_params.quantity,
                    "warning_timestamp": time.time(),
                    "price_threshold": 10000,
                    "decision": "PROCEED_WITH_CAUTION"
                }
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionDecision",
                        payload={
                            "decision_type": "HIGH_PRICE_WARNING_PROCEED",
                            "symbol": order_params.symbol if order_params else "N/A",
                            "local_order_id": "N/A",
                            **high_price_context
                        }
                    )

        # Only create or update performance timestamps if tracking is enabled
        if is_performance_tracking_enabled():
            perf_timestamps = perf_timestamps or create_timestamp_dict()
            if perf_timestamps is not None:
                add_timestamp(perf_timestamps, f'tm_executor.{symbol}.execute_start')

        # Determine if this is an OCR-driven EXIT signal that needs high priority
        is_ocr_driven_exit_signal = False
        if not is_manual_trigger and action_type is not None and action_type in (
            TradeActionType.CLOSE_POSITION,
            TradeActionType.REDUCE_PARTIAL,
            TradeActionType.REDUCE_FULL
            # Add any other system-generated exit types here, e.g., TradeActionType.SYSTEM_LIQUIDATE_RISK
        ):
            is_ocr_driven_exit_signal = True

        action_type_name_for_log = action_type.name if action_type is not None else "UNKNOWN_ACTION"

        # Refined trading enabled check:
        if self._trade_manager_service and not self._trade_manager_service.is_trading_enabled():
            # Allow exits even if general trading is disabled, but block new entries/adds
            if action_type is not None and action_type in (TradeActionType.OPEN_LONG, TradeActionType.ADD_LONG) and not is_manual_trigger:
                logger.warning(f"[{symbol}] Skipping {event_type_str_for_repo} (action: {action_type_name_for_log}) order: Trading is disabled.")
                # Publish execution rejection for IntelliSense visibility
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionRejection",
                        payload={
                            "rejection_type": "TRADING_DISABLED",
                            "symbol": order_params.symbol if order_params else "N/A",
                            **(extra_fields if extra_fields else {})
                        }
                    )
                if is_performance_tracking_enabled() and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, f'tm_executor.{symbol}.skipped_trading_disabled')
                return None
            else:
                logger.warning(f"[{symbol}] Proceeding with {event_type_str_for_repo} (action: {action_type_name_for_log}) despite trading being generally disabled because it's an exit or manual.")

        # 1. Standard Pre-Trade Risk Assessment (e.g., spread, account limits)
        # Bypassed if manual, OR if it's an OCR-driven EXIT signal.
        should_bypass_standard_risk_checks = is_manual_trigger or is_ocr_driven_exit_signal

        if self._risk_service and not should_bypass_standard_risk_checks:
            action_type_name = action_type.name if action_type is not None else "UNKNOWN"
            self.logger.info(f"[{symbol}] Running standard pre-trade risk checks for {event_type_str_for_repo} (action: {action_type_name}).")
            try:
                assessment = self._risk_service.assess_order_risk(order_params)
                if not self._risk_service.check_pre_trade_risk(
                    symbol=symbol,
                    side=order_params.side,
                    quantity=order_params.quantity,
                    limit_price=order_params.limit_price,
                    order_action_type=action_type,
                    gui_logger_func=self._gui_logger_func
                ):
                    logger.warning(f"[{symbol}] Pre-trade risk check failed for {action_type_name} action.")
                    # Publish execution rejection for IntelliSense visibility
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="TradeExecutor",
                            event_name="ExecutionRejection",
                            payload={
                                "rejection_type": "PRE_TRADE_RISK_FAILED",
                                "symbol": order_params.symbol if order_params else "N/A",
                                **(extra_fields if extra_fields else {})
                            }
                        )
                    if is_performance_tracking_enabled() and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, f'tm_executor.{symbol}.failed_pre_trade_risk')
                    return None

                if assessment and assessment.level in (RiskLevel.CRITICAL, RiskLevel.HALTED):
                    action_type_name = action_type.name if action_type is not None else "UNKNOWN"
                    logger.error(f"[{symbol}] Trade execution HALTED by RiskService for {action_type_name}: {assessment.reason} (Level: {assessment.level.name})")
                    # Send warning to GUI message box if gui_logger_func is available
                    if self._gui_logger_func:
                        self._gui_logger_func(f"RISK WARNING: {symbol} trade stopped - {assessment.reason} (Level: {assessment.level.name})", "error")
                    if is_performance_tracking_enabled() and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, f'tm_executor.{symbol}.halted_risk')
                    
                    # CRITICAL: Publish critical risk halt decision event
                    risk_halt_context = {
                        "risk_level": assessment.level.name,
                        "risk_reason": assessment.reason,
                        "action_type": action_type_name,
                        "halt_timestamp": time.time(),
                        "risk_score": getattr(assessment, 'score', None),
                        "risk_details": getattr(assessment, 'details', {}),
                        "symbol": symbol,
                        "order_side": order_params.side if order_params else "UNKNOWN",
                        "order_quantity": order_params.quantity if order_params else 0.0,
                        "limit_price": order_params.limit_price if order_params else None,
                        "halt_category": "CRITICAL_RISK_MANAGEMENT"
                    }
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="TradeExecutor",
                            event_name="ExecutionRejection",
                            payload={
                                "rejection_type": "CRITICAL_RISK_HALT",
                                "symbol": order_params.symbol if order_params else "N/A",
                                **risk_halt_context
                            }
                        )
                    
                    return None
                elif assessment and assessment.level == RiskLevel.HIGH:
                     warning_msg = f"[{symbol}] HIGH risk detected by RiskService: {assessment.reason}. Proceeding with caution."
                     logger.warning(warning_msg)
                     # Send warning to GUI message box if gui_logger_func is available
                     if self._gui_logger_func:
                         self._gui_logger_func(f"RISK WARNING: {symbol} HIGH risk - {assessment.reason}", "warning")
                     
                     # Publish high risk warning decision event (for IntelliSense tracking)
                     high_risk_context = {
                         "risk_level": assessment.level.name,
                         "risk_reason": assessment.reason,
                         "action_type": action_type.name if action_type is not None else "UNKNOWN",
                         "warning_timestamp": time.time(),
                         "risk_score": getattr(assessment, 'score', None),
                         "risk_details": getattr(assessment, 'details', {}),
                         "symbol": symbol,
                         "order_side": order_params.side if order_params else "UNKNOWN",
                         "order_quantity": order_params.quantity if order_params else 0.0,
                         "limit_price": order_params.limit_price if order_params else None,
                         "decision": "PROCEED_WITH_CAUTION",
                         "risk_category": "HIGH_RISK_WARNING"
                     }
                     if self._correlation_logger:
                         self._correlation_logger.log_event(
                             source_component="TradeExecutor",
                             event_name="ExecutionDecision",
                             payload={
                                 "decision_type": "HIGH_RISK_WARNING_PROCEED",
                                 "symbol": order_params.symbol if order_params else "N/A",
                                 "local_order_id": "N/A",
                                 **high_risk_context
                             }
                         )
            except Exception as e_risk:
                action_type_name = action_type.name if action_type is not None else "UNKNOWN"
                logger.error(f"[{symbol}] Error during risk assessment for {action_type_name}: {e_risk}", exc_info=True)
                if is_performance_tracking_enabled() and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_risk')
                
                # Publish risk system error event
                risk_error_context = {
                    "error_type": "RISK_ASSESSMENT_EXCEPTION",
                    "error_message": str(e_risk),
                    "action_type": action_type_name,
                    "symbol": symbol,
                    "order_side": order_params.side if order_params else "UNKNOWN",
                    "order_quantity": order_params.quantity if order_params else 0.0,
                    "error_timestamp": time.time(),
                    "exception_type": type(e_risk).__name__,
                    "halt_reason": "RISK_SERVICE_ERROR"
                }
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionRejection",
                        payload={
                            "rejection_type": "RISK_SYSTEM_ERROR",
                            "symbol": order_params.symbol if order_params else "N/A",
                            **risk_error_context
                        }
                    )
                
                return None
        elif self._risk_service and should_bypass_standard_risk_checks:
            action_type_name = action_type.name if action_type is not None else "UNKNOWN"
            self.logger.warning(f"[{symbol}] Standard pre-trade risk checks (e.g., spread) BYPASSED for {event_type_str_for_repo} (action: {action_type_name}) "
                             f"due to manual_trigger ({is_manual_trigger}) or ocr_driven_exit ({is_ocr_driven_exit_signal}).")
            
            # Publish risk bypass decision event (critical for audit trail)
            bypass_context = {
                "bypass_reason": "MANUAL_TRIGGER" if is_manual_trigger else "OCR_DRIVEN_EXIT",
                "action_type": action_type_name,
                "event_type": event_type_str_for_repo,
                "symbol": symbol,
                "order_side": order_params.side if order_params else "UNKNOWN",
                "order_quantity": order_params.quantity if order_params else 0.0,
                "bypass_timestamp": time.time(),
                "is_manual_trigger": is_manual_trigger,
                "is_ocr_driven_exit": is_ocr_driven_exit_signal,
                "decision": "RISK_CHECKS_BYPASSED",
                "risk_category": "BYPASS_AUTHORIZATION"
            }
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "RISK_CHECKS_BYPASSED",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": "N/A",
                        **bypass_context
                    }
                )
        if is_performance_tracking_enabled() and perf_timestamps is not None:
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.risk_checked')

        # 2. Trading Status / LULD Check (via PositionManager)
        # Bypassed if manual, OR if it's an OCR-driven EXIT signal.
        should_bypass_luld_halt_checks = is_manual_trigger or is_ocr_driven_exit_signal
        action_type_name_for_luld_log = action_type.name if action_type is not None else "UNKNOWN_ACTION" # Used for logging if action_type is None

        if not should_bypass_luld_halt_checks:
            action_type_name = action_type.name if action_type is not None else "UNKNOWN"
            self.logger.info(f"[{symbol}] Running LULD/Halt status checks for {event_type_str_for_repo} (action: {action_type_name}).")
            try:
                pm_status = self._position_manager.get_trading_status(symbol)
                current_trading_status = pm_status.get("status", "Unknown")
                luld_h = pm_status.get("luld_high")
                luld_l = pm_status.get("luld_low")

                if current_trading_status in ("Halted", "Halted (LULD)", "Paused (LULD)"):
                     warning_msg = f"[{symbol}] Skipping {event_type_str_for_repo} order: Trading status is '{current_trading_status}'."
                     logger.warning(warning_msg)
                     # Send warning to GUI message box if gui_logger_func is available
                     if self._gui_logger_func:
                         self._gui_logger_func(f"RISK WARNING: {symbol} trade stopped - Trading status is '{current_trading_status}'", "warning")
                     if is_performance_tracking_enabled() and perf_timestamps is not None:
                         add_timestamp(perf_timestamps, f'tm_executor.{symbol}.halted_trading_status')
                     
                     # Publish LULD/halt rejection event
                     if self._correlation_logger:
                         self._correlation_logger.log_event(
                             source_component="TradeExecutor",
                             event_name="ExecutionRejection",
                             payload={
                                 "rejection_type": f"TRADING_HALTED_{current_trading_status.replace(' ', '_').replace('(', '').replace(')', '').upper()}",
                                 "symbol": order_params.symbol if order_params else "N/A",
                                 "trading_status": current_trading_status,
                                 "pm_status": pm_status,
                                 "bypass_flags": {
                                     "manual_trigger": is_manual_trigger,
                                     "ocr_driven_exit": is_ocr_driven_exit_signal
                                 },
                                 "rejection_category": "LULD_HALT"
                             }
                         )
                     
                     return None

                # LULD Band check (only if bands are valid and we have a limit price)
                if order_params.order_type == "LMT" and order_params.limit_price is not None:
                    limit_price = order_params.limit_price
                    if luld_h is not None and luld_l is not None and luld_h > luld_l:
                        if order_params.side == "BUY" and limit_price > luld_h:
                            warning_msg = f"[{symbol}] Skipping BUY LMT: Price {limit_price:.4f} is ABOVE LULD High {luld_h:.4f}."
                            logger.warning(warning_msg)
                            # Send warning to GUI message box if gui_logger_func is available
                            if self._gui_logger_func:
                                self._gui_logger_func(f"RISK WARNING: {symbol} BUY trade stopped - Price {limit_price:.4f} is ABOVE LULD High {luld_h:.4f}", "warning")
                            if is_performance_tracking_enabled() and perf_timestamps is not None:
                                add_timestamp(perf_timestamps, f'tm_executor.{symbol}.halted_luld_high')
                            
                            # Publish LULD band violation rejection event
                            if self._correlation_logger:
                                self._correlation_logger.log_event(
                                    source_component="TradeExecutor",
                                    event_name="ExecutionRejection",
                                    payload={
                                        "rejection_type": "LULD_BAND_VIOLATION_HIGH",
                                        "symbol": order_params.symbol if order_params else "N/A",
                                        "violation_type": "BUY_ABOVE_LULD_HIGH",
                                        "limit_price": limit_price,
                                        "luld_high": luld_h,
                                        "luld_low": luld_l,
                                        "price_violation": limit_price - luld_h,
                                        "trading_status": current_trading_status,
                                        "rejection_category": "LULD_BAND"
                                    }
                                )
                            
                            return None
                        if order_params.side == "SELL" and limit_price < luld_l:
                             warning_msg = f"[{symbol}] Skipping SELL LMT: Price {limit_price:.4f} is BELOW LULD Low {luld_l:.4f}."
                             logger.warning(warning_msg)
                             # Send warning to GUI message box if gui_logger_func is available
                             if self._gui_logger_func:
                                 self._gui_logger_func(f"RISK WARNING: {symbol} SELL trade stopped - Price {limit_price:.4f} is BELOW LULD Low {luld_l:.4f}", "warning")
                             if is_performance_tracking_enabled() and perf_timestamps is not None:
                                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.halted_luld_low')
                             
                             # Publish LULD band violation rejection event
                             if self._correlation_logger:
                                 self._correlation_logger.log_event(
                                     source_component="TradeExecutor",
                                     event_name="ExecutionRejection",
                                     payload={
                                         "rejection_type": "LULD_BAND_VIOLATION_LOW",
                                         "symbol": order_params.symbol if order_params else "N/A",
                                         "violation_type": "SELL_BELOW_LULD_LOW",
                                         "limit_price": limit_price,
                                         "luld_high": luld_h,
                                         "luld_low": luld_l,
                                         "price_violation": luld_l - limit_price,
                                         "trading_status": current_trading_status,
                                         "rejection_category": "LULD_BAND"
                                     }
                                 )
                             
                             return None
            except Exception as e_status:
                 logger.error(f"[{symbol}] Error checking trading status: {e_status}", exc_info=True)
                 if is_performance_tracking_enabled() and perf_timestamps is not None:
                     add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_trading_status')
                 
                 # Publish trading status check error event
                 if self._correlation_logger:
                     self._correlation_logger.log_event(
                         source_component="TradeExecutor",
                         event_name="ExecutionRejection",
                         payload={
                             "rejection_type": "TRADING_STATUS_CHECK_ERROR",
                             "symbol": order_params.symbol if order_params else "N/A",
                             "error_type": type(e_status).__name__,
                             "error_message": str(e_status),
                             "rejection_category": "SYSTEM_ERROR",
                             "fail_closed": True
                         }
                     )
                 return None # Fail closed
        elif should_bypass_luld_halt_checks:
            logger.warning(f"[{symbol}] LULD/Halt status checks BYPASSED for {event_type_str_for_repo} (action: {action_type_name_for_luld_log}) "
                             f"due to manual_trigger ({is_manual_trigger}) or ocr_driven_exit ({is_ocr_driven_exit_signal}).")
        if is_performance_tracking_enabled() and perf_timestamps is not None:
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.status_checked')

        # --- AGGRESSIVE CHASE LOGIC ENTRY POINT ---
        # aggressive_chase might be set directly in order_params by OrderStrategy for liquidations
        # or passed as an argument if TMS makes that decision
        is_chase_scenario = aggressive_chase or \
                            (order_params.event_type and "CHASE" in order_params.event_type.upper()) or \
                            ((extra_fields or {}).get("aggressive_chase_parameters") is not None)

        if is_chase_scenario and order_params.side.upper() == "SELL": # Currently only for SELLING longs
            chase_config_from_extra = (extra_fields or {}).get("aggressive_chase_parameters", {})
            logger.info(f"[{symbol}] AGGRESSIVE CHASE EXIT detected for sell order. Params: {chase_config_from_extra}")
            return self._execute_aggressive_sell_chase(
                initial_order_params=order_params,
                chase_config=chase_config_from_extra, # Pass specific chase params if available
                time_stamps=time_stamps,
                snapshot_version=snapshot_version,
                ocr_confidence=ocr_confidence,
                ocr_snapshot_cost_basis=ocr_snapshot_cost_basis,
                original_extra_fields=extra_fields,
                perf_timestamps=perf_timestamps if is_performance_tracking_enabled() else None
            )
        # --- END AGGRESSIVE CHASE LOGIC ---

        # 3. Create Local Order Record (for non-chase orders)
        local_id: Optional[int] = None
        try:
             request_time = time.time()
             # Extract necessary components from time_stamps if present
             # Need robust parsing for ocr_raw_timestamp
             ocr_raw_timestamp_val: Optional[float] = None
             if time_stamps:
                 ocr_raw_time_str = time_stamps.get("ocr_raw_time")
                 if ocr_raw_time_str:
                      try:
                           if isinstance(ocr_raw_time_str, (float, int)):
                               ocr_raw_timestamp_val = float(ocr_raw_time_str)
                           elif isinstance(ocr_raw_time_str, str):
                              from datetime import datetime # Local import ok?
                              try: ocr_raw_timestamp_val = datetime.strptime(ocr_raw_time_str, "%Y-%m-%d %H:%M:%S.%f").timestamp()
                              except ValueError:
                                   try: ocr_raw_timestamp_val = datetime.strptime(ocr_raw_time_str, "%Y-%m-%d %H:%M:%S").timestamp()
                                   except ValueError: ocr_raw_timestamp_val = None
                      except Exception as e_ts:
                           logger.warning(f"Could not parse ocr_raw_time '{ocr_raw_time_str}': {e_ts}")

             timestamp_ocr_processed = time_stamps.get("timestamp_ocr_processed") if time_stamps else None # Get the correct key if it exists

             logger.debug(f"[{symbol}] Calling OrderRepository.create_copy_order with event_type='{event_type_str_for_repo}'...")
             local_id = self._order_repository.create_copy_order(
                 symbol=symbol,
                 side=order_params.side,
                 event_type=event_type_str_for_repo,  # <<< PASS THE (NOW VALID) SPECIFIC EVENT TYPE STRING
                 requested_shares=order_params.quantity,
                 req_time=request_time,
                 parent_trade_id=order_params.parent_trade_id,
                 requested_lmt_price=order_params.limit_price,
                 ocr_time=ocr_raw_timestamp_val, # Use parsed value
                 comment=f"{event_type_str_for_repo} triggered", # Generic comment, maybe enhance
                 timestamp_ocr_processed=timestamp_ocr_processed,
                 master_correlation_id=order_params.correlation_id, # <<<< PASS IT HERE >>>>
                 ocr_confidence=ocr_confidence,
                 perf_timestamps=perf_timestamps,
                 action_type=action_type.name if action_type else None, # Pass as string
                 order_type_str=order_params.order_type,
                 **(extra_fields or {})
             )
             if local_id is None or local_id <= 0:
                  logger.error(f"[{symbol}] OrderRepository failed to create valid local order record (returned {local_id}). Aborting execution.")
                  if is_performance_tracking_enabled() and perf_timestamps is not None:
                      add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_repo_create_none')
                  return None
             logger.info(f"[{symbol}] OrderRepository created local_id: {local_id} for {event_type_str_for_repo}")
             order_params.local_order_id = local_id # Store for broker call

        except Exception as e_repo:
             logger.error(f"[{symbol}] Error creating local order record: {e_repo}", exc_info=True)
             if is_performance_tracking_enabled() and perf_timestamps is not None:
                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_repo_create_exception')
             return None
        if is_performance_tracking_enabled() and perf_timestamps is not None:
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.repo_created')

        # 4. Fingerprint Check
        fp: Optional[Tuple] = None # Initialize fp
        try:
            pos_data = self._position_manager.get_position(symbol)
            pm_realized_pnl = pos_data.get("realized_pnl", 0.0) if pos_data else 0.0

            if event_type_str_for_repo.upper() == "OPEN":
                cost_basis_for_fp = 0.0
            # Use ocr_snapshot_cost_basis if provided AND it's not a manual trigger
            elif ocr_snapshot_cost_basis is not None and not is_manual_trigger:
                cost_basis_for_fp = ocr_snapshot_cost_basis
            else: # Fallback for manual orders or if ocr_snapshot_cost_basis is not relevant/provided
                cost_basis_for_fp = order_params.limit_price or 0.0

            fp = self._make_fingerprint(
                event_type=event_type_str_for_repo,
                shares_float=order_params.quantity,
                cost_basis=cost_basis_for_fp, # Uses the logic above
                realized_pnl=pm_realized_pnl,
                snapshot_version=snapshot_version
            )
            last_fp = pos_data.get("last_broker_fingerprint") if pos_data else None

            logger.debug(f"[{symbol}] Fingerprint Check: New='{fp}', Last='{last_fp}', Manual='{is_manual_trigger}'")
            if not is_manual_trigger and fp == last_fp:
                logger.warning(f"[{symbol}] Duplicate non-manual {event_type_str_for_repo} event detected by fingerprint. Skipping broker send.")
                if is_performance_tracking_enabled() and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, f'tm_executor.{symbol}.skipped_fingerprint')
                
                # Publish fingerprint duplicate detection event
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionRejection",
                        payload={
                            "rejection_type": "FINGERPRINT_DUPLICATE_DETECTED",
                            "symbol": order_params.symbol if order_params else "N/A",
                            "duplicate_detection_level": "EXECUTION_FINGERPRINT",
                            "fingerprint_new": str(fp),
                            "fingerprint_last": str(last_fp),
                            "event_type": event_type_str_for_repo,
                            "fingerprint_components": {
                                "event_type": fp[0] if fp and len(fp) > 0 else None,
                                "shares_int": fp[1] if fp and len(fp) > 1 else None,
                                "cost_rounded": fp[2] if fp and len(fp) > 2 else None,
                                "pnl_rounded": fp[3] if fp and len(fp) > 3 else None,
                                "snapshot_version": fp[4] if fp and len(fp) > 4 else None
                            },
                            "manual_trigger": is_manual_trigger,
                            "rejection_category": "DUPLICATE_FINGERPRINT"
                        }
                    )
                # TODO: Consider cancelling local order record?
                return None # Return None to indicate no broker send occurred
        except Exception as e_fp_check:
             logger.error(f"[{symbol}] Error during fingerprint check: {e_fp_check}", exc_info=True)
             if is_performance_tracking_enabled() and perf_timestamps is not None:
                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_fingerprint_check')
             
             # Publish fingerprint check error event
             if self._correlation_logger:
                 self._correlation_logger.log_event(
                     source_component="TradeExecutor",
                     event_name="ExecutionRejection",
                     payload={
                         "rejection_type": "FINGERPRINT_CHECK_ERROR",
                         "symbol": order_params.symbol if order_params else "N/A",
                         "error_stage": "FINGERPRINT_VALIDATION",
                         "error_type": type(e_fp_check).__name__,
                         "error_message": str(e_fp_check),
                         "event_type": event_type_str_for_repo,
                         "manual_trigger": is_manual_trigger,
                         "rejection_category": "SYSTEM_ERROR"
                     }
                 )
             
             # TODO: Cancel local order if fingerprint check fails?
             return None # Fail closed
        if is_performance_tracking_enabled() and perf_timestamps is not None:
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.fingerprint_checked')

        # 5. Place Order with Broker
        try:
             logger.info(f"[{symbol}] Placing order with Broker: {order_params.side} {order_params.quantity} @ {order_params.order_type} {order_params.limit_price or ''} (local_id={local_id})")
             
             # Prepare broker payload with correlation ID for end-to-end tracking
             broker_payload = {
                 'symbol': symbol,
                 'side': order_params.side,
                 'quantity': int(round(order_params.quantity)), # Broker usually wants integer shares
                 'order_type': order_params.order_type,
                 'limit_price': order_params.limit_price,
                 'stop_price': order_params.stop_price, # Pass through if set
                 'local_order_id': local_id, # Link to local record
                 'correlation_id': getattr(order_params, 'correlation_id', None), # Pass master correlation ID (Break #3 fix)
                 'time_in_force': order_params.time_in_force,
                 'outside_rth': True # Example: Allow ext hours
             }
             
             self._broker.submit_order(broker_payload)
             
             # T17: Broker submission completion timing
             T17_BrokerSubmission_ns = perf_counter_ns()
             execution_duration_ns = T17_BrokerSubmission_ns - T16_ExecutionStart_ns
             
             logger.info(f"[{symbol}] Broker order submission initiated for local_id={local_id}.")
             
             # NEW: Publish execution decision to Redis via bulletproof IPC for IntelliSense visibility
             if self._correlation_logger:
                 self._correlation_logger.log_event(
                     source_component="TradeExecutor",
                     event_name="ExecutionDecision",
                     payload={
                         "decision_type": "ORDER_SENT_TO_BROKER",
                         "symbol": order_params.symbol if order_params else "N/A",
                         "local_order_id": local_id,
                         **(extra_fields if extra_fields else {})
                     }
                 )
             
             # Publish BrokerOrderSubmittedEvent for comprehensive T16-T17 pipeline instrumentation
             if self._event_bus:
                 try:
                     # Get current market price for context
                     current_price = None
                     if self._price_provider:
                         try:
                             # Extract correlation_id from order_params for price fetching traceability
                             correlation_id = getattr(order_params, 'correlation_id', None)
                             current_price = self._price_provider.get_latest_price(symbol)
                             # Note: get_latest_price is a legacy method without correlation_id support
                             # Consider using get_price_data for enhanced traceability in future updates
                         except Exception:
                             pass  # Non-critical failure
                     
                     # Get current position for context
                     current_position = None
                     if self._position_manager:
                         try:
                             pos_data = self._position_manager.get_position(symbol)
                             current_position = pos_data.get("quantity", 0.0) if pos_data else 0.0
                         except Exception:
                             pass  # Non-critical failure
                     
                     broker_event_data = BrokerOrderSubmittedEventData(
                         symbol=symbol,
                         local_order_id=str(local_id),
                         order_side=order_params.side,
                         order_quantity=order_params.quantity,
                         order_type=order_params.order_type,
                         limit_price=order_params.limit_price,
                         stop_price=order_params.stop_price,
                         time_in_force=order_params.time_in_force,
                         
                         # Broker submission details
                         broker_payload=broker_payload.copy(),  # Copy to avoid reference issues
                         submission_timestamp=time.time(),
                         submission_successful=True,
                         
                         # T16-T17 milestone timing
                         T16_ExecutionStart_ns=T16_ExecutionStart_ns,
                         T17_BrokerSubmission_ns=T17_BrokerSubmission_ns,
                         execution_duration_ns=execution_duration_ns,
                         
                         # Causation and correlation tracking
                         source_event_id=source_event_id,
                         correlation_id=getattr(order_params, 'correlation_id', None),
                         validation_id=validation_id,
                         
                         # Market context
                         current_price=current_price,
                         current_position=current_position,
                         
                         # Order context
                         action_type=action_type.name if hasattr(action_type, 'name') else str(action_type),
                         is_manual_trigger=is_manual_trigger,
                         aggressive_chase=aggressive_chase,
                         
                         # Extended trading context
                         extended_hours_enabled=True,  # Based on broker_payload['outside_rth']
                         fingerprint_data=fp if 'fp' in locals() else None
                     )
                     
                     broker_event = BrokerOrderSubmittedEvent(data=broker_event_data)
                     self._event_bus.publish(broker_event)
                     logger.info(f"[{symbol}] Published BrokerOrderSubmittedEvent for local_id={local_id}, T16-T17 duration: {execution_duration_ns/1_000_000:.2f}ms")
                     
                 except Exception as e_broker_event:
                     logger.error(f"[{symbol}] Failed to publish BrokerOrderSubmittedEvent for local_id={local_id}: {e_broker_event}", exc_info=True)
             else:
                 logger.warning(f"[{symbol}] EventBus not available, cannot publish BrokerOrderSubmittedEvent for local_id={local_id}")
             
             # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T16-T17 milestone telemetry
             if self._correlation_logger:
                 try:
                     telemetry_payload = {
                         # Milestone timestamps (nanoseconds)
                         "T16_ExecutionStart_ns": T16_ExecutionStart_ns,
                         "T17_BrokerSubmission_ns": T17_BrokerSubmission_ns,
                         
                         # Calculated latencies
                         "T16_to_T17_execution_ms": execution_duration_ns / 1_000_000,
                         
                         # Order context
                         "symbol": symbol,
                         "local_order_id": str(local_id),
                         "action_type": order_params.action_type.value if hasattr(order_params.action_type, 'value') else str(order_params.action_type),
                         "quantity": order_params.quantity,
                         "side": order_params.side.value if hasattr(order_params.side, 'value') else str(order_params.side),
                         "order_type": order_params.order_type.value if hasattr(order_params.order_type, 'value') else str(order_params.order_type),
                         "limit_price": order_params.limit_price,
                         "stop_price": order_params.stop_price,
                         
                         # Broker response
                         "broker_success": bool(result),
                         "broker_order_id": result.get('broker_order_id') if result else None,
                         
                         # Position context
                         "current_position": current_position,
                         "target_position": target_position,
                         
                         # Causation chain
                         "request_id": order_params.request_id,
                         "validation_id": validation_id,
                         "correlation_id": master_correlation_id,
                         "broker_event_id": broker_event.event_id if 'broker_event' in locals() else None
                     }
                     
                     # Fire and forget - no blocking!
                     self._correlation_logger.log_event(
                         source_component="TradeExecutor",
                         event_name="BrokerSubmissionMilestones",
                         payload=telemetry_payload
                     )
                 except Exception as telemetry_error:
                     # Never let telemetry errors affect trading
                     logger.debug(f"Telemetry enqueue error (non-critical): {telemetry_error}")
             
             if is_performance_tracking_enabled() and perf_timestamps is not None:
                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.broker_sent')

             # Map local_order_id to validation_id for pipeline tracking
             if validation_id and hasattr(self._broker, 'map_local_order_id_to_validation_id'):
                 self._broker.map_local_order_id_to_validation_id(str(local_id), validation_id)
                 logger.debug(f"[{symbol}] Mapped local_order_id {local_id} to validation_id {validation_id} for pipeline tracking")

             # --- AFTER SUCCESSFUL BROKER SUBMISSION (for OPEN orders) ---
             if local_id and order_params.action_type == TradeActionType.OPEN_LONG:
                 if self._event_bus:
                     try:
                         open_event_data = OpenOrderSubmittedEventData(
                             symbol=order_params.symbol,
                             local_order_id=str(local_id) # Ensure it's a string
                         )
                         event_to_publish = OpenOrderSubmittedEvent(data=open_event_data)
                         self._event_bus.publish(event_to_publish)
                         self.logger.info(f"EXECUTOR: Published OpenOrderSubmittedEvent for {order_params.symbol}, Local Order ID: {local_id}")
                     except Exception as e_event:
                         self.logger.error(f"EXECUTOR: Failed to publish OpenOrderSubmittedEvent for {order_params.symbol}, Order ID {local_id}: {e_event}", exc_info=True)
                 else:
                     self.logger.warning(f"EXECUTOR: EventBus not available, cannot publish OpenOrderSubmittedEvent for {order_params.symbol}, Order ID {local_id}.")

             # 6. Update Fingerprint (ONLY after successful submission attempt)
             # Ensure fp is not None (it should be set in step 4)
             if fp is not None:
                 try:
                     self._position_manager.update_fingerprint(symbol, fp)
                     logger.debug(f"[{symbol}] Updated fingerprint via PositionManager to {fp}")
                     if is_performance_tracking_enabled() and perf_timestamps is not None:
                         add_timestamp(perf_timestamps, f'tm_executor.{symbol}.fingerprint_updated')
                 except Exception as e_fp_update:
                      logger.error(f"[{symbol}] Failed to update fingerprint after broker send: {e_fp_update}", exc_info=True)
                      if is_performance_tracking_enabled() and perf_timestamps is not None:
                          add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_fingerprint_update')
                      # Continue, as order was already sent
             else:
                  logger.error(f"[{symbol}] Cannot update fingerprint as it was not generated correctly.")

             if is_performance_tracking_enabled() and perf_timestamps is not None:
                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.execute_success')
             return local_id # Return local_id on successful submission attempt

        except Exception as e_broker:
             # T17: Broker submission failure timing
             T17_BrokerSubmission_ns = perf_counter_ns()
             execution_duration_ns = T17_BrokerSubmission_ns - T16_ExecutionStart_ns
             
             logger.error(f"[{symbol}] EXCEPTION during broker.place_order call for local_id={local_id}: {e_broker}", exc_info=True)
             if is_performance_tracking_enabled() and perf_timestamps is not None:
                 add_timestamp(perf_timestamps, f'tm_executor.{symbol}.error_broker_send')
             
             # Publish BrokerOrderSubmittedEvent for failed submission
             if self._event_bus:
                 try:
                     broker_event_data = BrokerOrderSubmittedEventData(
                         symbol=symbol,
                         local_order_id=str(local_id) if local_id else None,
                         order_side=order_params.side,
                         order_quantity=order_params.quantity,
                         order_type=order_params.order_type,
                         limit_price=order_params.limit_price,
                         stop_price=order_params.stop_price,
                         time_in_force=order_params.time_in_force,
                         
                         # Broker submission details - failed case
                         broker_payload=broker_payload.copy() if 'broker_payload' in locals() else None,
                         submission_timestamp=time.time(),
                         submission_successful=False,
                         
                         # T16-T17 milestone timing
                         T16_ExecutionStart_ns=T16_ExecutionStart_ns,
                         T17_BrokerSubmission_ns=T17_BrokerSubmission_ns,
                         execution_duration_ns=execution_duration_ns,
                         
                         # Causation and correlation tracking
                         source_event_id=source_event_id,
                         correlation_id=getattr(order_params, 'correlation_id', None),
                         validation_id=validation_id,
                         
                         # Order context
                         action_type=action_type.name if hasattr(action_type, 'name') else str(action_type),
                         is_manual_trigger=is_manual_trigger,
                         aggressive_chase=aggressive_chase,
                         
                         # Error context
                         error_message=str(e_broker),
                         error_type=type(e_broker).__name__
                     )
                     
                     broker_event = BrokerOrderSubmittedEvent(data=broker_event_data)
                     self._event_bus.publish(broker_event)
                     logger.info(f"[{symbol}] Published BrokerOrderSubmittedEvent (FAILED) for local_id={local_id}, T16-T17 duration: {execution_duration_ns/1_000_000:.2f}ms")
                     
                 except Exception as e_broker_event:
                     logger.error(f"[{symbol}] Failed to publish BrokerOrderSubmittedEvent (FAILED) for local_id={local_id}: {e_broker_event}", exc_info=True)
             
             # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T16-T17 failure telemetry
             if self._correlation_logger:
                 try:
                     error_telemetry_payload = {
                         # Milestone timestamps (nanoseconds)
                         "T16_ExecutionStart_ns": T16_ExecutionStart_ns,
                         "T17_BrokerSubmission_ns": T17_BrokerSubmission_ns,
                         
                         # Calculated latencies
                         "T16_to_T17_execution_ms": execution_duration_ns / 1_000_000,
                         
                         # Order context
                         "symbol": symbol,
                         "local_order_id": str(local_id) if local_id else None,
                         "action_type": order_params.action_type.value if hasattr(order_params.action_type, 'value') else str(order_params.action_type),
                         "quantity": order_params.quantity,
                         "side": order_params.side.value if hasattr(order_params.side, 'value') else str(order_params.side),
                         
                         # Error context
                         "broker_success": False,
                         "error_type": type(e_broker).__name__,
                         "error_message": str(e_broker),
                         
                         # Causation chain
                         "request_id": order_params.request_id,
                         "validation_id": validation_id,
                         "correlation_id": master_correlation_id
                     }
                     
                     # Fire and forget - no blocking!
                     self._correlation_logger.log_event(
                         source_component="TradeExecutor",
                         event_name="BrokerSubmissionError",
                         payload=error_telemetry_payload
                     )
                 except Exception as telemetry_error:
                     # Never let telemetry errors affect trading
                     logger.debug(f"Telemetry enqueue error (non-critical): {telemetry_error}")
             
             # Publish order send failure event
             send_failure_context = {
                 "send_failure_type": "BROKER_EXCEPTION",
                 "local_order_id": local_id,
                 "symbol": symbol,
                 "order_side": order_params.side if order_params else "UNKNOWN",
                 "order_quantity": order_params.quantity if order_params else 0.0,
                 "order_type": order_params.order_type if order_params else "UNKNOWN",
                 "limit_price": order_params.limit_price if order_params else None,
                 "broker_exception_type": type(e_broker).__name__,
                 "broker_error_message": str(e_broker),
                 "failure_timestamp": time.time(),
                 "send_stage": "BROKER_PLACE_ORDER_CALL",
                 "will_attempt_cancellation": True
             }
             if self._correlation_logger:
                 self._correlation_logger.log_event(
                     source_component="TradeExecutor",
                     event_name="ExecutionRejection",
                     payload={
                         "rejection_type": "ORDER_SEND_FAILED",
                         "symbol": order_params.symbol if order_params else "N/A",
                     }
                 )
             # Attempt to mark the local order as CancelRequested since broker send failed
             try:
                  logger.warning(f"[{symbol}] Attempting to mark local order {local_id} as CancelRequested due to broker send failure.")
                  self._order_repository.request_cancellation(local_id, f"Broker send failed: {e_broker}")
                  
                  # Publish order cancellation decision event
                  cancellation_context = {
                      "cancellation_trigger": "BROKER_SEND_FAILURE",
                      "local_order_id": local_id,
                      "symbol": symbol,
                      "order_side": order_params.side if order_params else "UNKNOWN",
                      "order_quantity": order_params.quantity if order_params else 0.0,
                      "broker_error_message": str(e_broker),
                      "cancellation_reason": f"Broker send failed: {e_broker}",
                      "cancellation_timestamp": time.time(),
                      "cancellation_stage": "POST_BROKER_SEND_FAILURE"
                  }
                  if self._correlation_logger:
                      self._correlation_logger.log_event(
                          source_component="TradeExecutor",
                          event_name="ExecutionDecision",
                          payload={
                              "decision_type": "ORDER_CANCELLATION_REQUESTED_BROKER_FAILURE",
                              "symbol": order_params.symbol if order_params else "N/A",
                              "local_order_id": local_id if local_id else "N/A",
                              **(cancellation_context
                   if cancellation_context
                   else {})
                          }
                      )
             except Exception as e_cancel:
                   logger.error(f"[{symbol}] Failed to mark local order {local_id} as CancelRequested after broker error: {e_cancel}", exc_info=True)
                   
                   # Publish cancellation failure event
                   cancellation_failure_context = {
                       "cancellation_failure_type": "CANCELLATION_REQUEST_FAILED",
                       "local_order_id": local_id,
                       "symbol": symbol,
                       "original_broker_error": str(e_broker),
                       "cancellation_error": str(e_cancel),
                       "failure_timestamp": time.time(),
                       "cancellation_stage": "POST_BROKER_SEND_FAILURE"
                   }
                   if self._correlation_logger:
                       self._correlation_logger.log_event(
                           source_component="TradeExecutor",
                           event_name="ExecutionRejection",
                           payload={
                               "rejection_type": "ORDER_CANCELLATION_FAILED",
                               "symbol": order_params.symbol if order_params else "N/A",
                           }
                       )

    # --- Helper methods for Aggressive Chase Logic ---
    def _attempt_cancellation_in_chase(self, symbol: str, local_order_id_to_cancel: int, reason: str):
        """Helper to attempt cancellation of an order during a chase."""
        if not local_order_id_to_cancel:
            return

        logger.info(f"[{symbol}] Chase: Attempting to cancel order {local_order_id_to_cancel}. Reason: {reason}")
        order_to_cancel = self._order_repository.get_order_by_local_id(local_order_id_to_cancel)

        if not order_to_cancel:
            logger.warning(f"[{symbol}] Chase: Cannot cancel order {local_order_id_to_cancel}, details not found in OrderRepository.")
            
            # Publish cancellation failure event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionRejection",
                    payload={
                        "rejection_type": "ORDER_CANCELLATION_FAILED",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "cancellation_failure_type": "ORDER_NOT_FOUND_FOR_CANCELLATION",
                        "local_order_id": local_order_id_to_cancel,
                        "symbol": symbol,
                        "cancellation_reason": reason,
                        "failure_timestamp": time.time(),
                        "cancellation_stage": "CHASE_CANCELLATION",
                        "failure_reason": "ORDER_NOT_FOUND_IN_REPOSITORY"
                    }
                )
            return

        broker_id_to_cancel = order_to_cancel.ls_order_id # Use attribute access
        # Fallback to ephemeral_corr_id IF your broker supports cancelling by it AND if ls_order_id is not yet available
        if not broker_id_to_cancel and hasattr(order_to_cancel, 'ephemeral_corr_id') and order_to_cancel.ephemeral_corr_id:
             # AGENT: Check if your broker can cancel via ephemeral_corr_id.
             # If so, uncomment and use. Otherwise, this might not be effective.
             # broker_id_to_cancel = order_to_cancel.ephemeral_corr_id # Use attribute access
             # logger.info(f"[{symbol}] Chase: Using ephemeral_corr_id {broker_id_to_cancel} for cancellation attempt.")
             pass # For now, only use final ls_order_id unless confirmed otherwise for cancellation by ephemeral ID

        if broker_id_to_cancel:
            try:
                self._broker.cancel_order(broker_id_to_cancel)
                logger.info(f"[{symbol}] Chase: Cancel request sent to broker for order {local_order_id_to_cancel} (BrokerID/Link: {broker_id_to_cancel}).")
                
                # Publish successful broker cancellation request
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionDecision",
                        payload={
                            "decision_type": "ORDER_CANCELLATION_SENT_TO_BROKER",
                            "symbol": order_params.symbol if order_params else "N/A",
                            "local_order_id": # No order params available
                    local_order_id_to_cancel if # No order params available
                    local_order_id_to_cancel else "N/A",
                            "cancellation_trigger": "CHASE_STRATEGY",
                            "local_order_id": local_order_id_to_cancel,
                            "broker_order_id": broker_id_to_cancel,
                            "symbol": symbol,
                            "cancellation_reason": reason,
                            "cancellation_timestamp": time.time(),
                            "cancellation_stage": "BROKER_CANCEL_REQUEST_SENT",
                            "order_side": order_to_cancel.side.value if hasattr(order_to_cancel.side, 'value') else str(order_to_cancel.side),
                            "order_quantity": order_to_cancel.requested_shares
                        }
                    )
            except Exception as e_cancel_broker:
                logger.error(f"[{symbol}] Chase: Broker error cancelling order {local_order_id_to_cancel} (BrokerID/Link: {broker_id_to_cancel}): {e_cancel_broker}")
                
                # Publish broker cancellation failure
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeExecutor",
                        event_name="ExecutionRejection",
                        payload={
                            "rejection_type": "ORDER_CANCELLATION_FAILED",
                            "symbol": order_params.symbol if order_params else "N/A",
                            "cancellation_failure_type": "BROKER_CANCEL_REQUEST_FAILED",
                            "local_order_id": local_order_id_to_cancel,
                            "broker_order_id": broker_id_to_cancel,
                            "symbol": symbol,
                            "cancellation_reason": reason,
                            "broker_error": str(e_cancel_broker),
                            "failure_timestamp": time.time(),
                            "cancellation_stage": "BROKER_CANCEL_REQUEST",
                            "order_side": order_to_cancel.side.value if hasattr(order_to_cancel.side, 'value') else str(order_to_cancel.side),
                            "order_quantity": order_to_cancel.requested_shares
                        }
                    )
        else:
            logger.warning(f"[{symbol}] Chase: No broker_id or usable ephemeral_id found for order {local_order_id_to_cancel} to send cancel to broker. Marking as CancelRequested locally.")
            
            # Publish local-only cancellation decision
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "ORDER_CANCELLATION_LOCAL_ONLY",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": # No order params available
                local_order_id_to_cancel if # No order params available
                local_order_id_to_cancel else "N/A",
                        "cancellation_trigger": "CHASE_STRATEGY",
                        "local_order_id": local_order_id_to_cancel,
                        "symbol": symbol,
                        "cancellation_reason": reason,
                        "cancellation_timestamp": time.time(),
                        "cancellation_stage": "LOCAL_CANCELLATION_ONLY",
                        "broker_id_status": "NOT_AVAILABLE",
                        "order_side": order_to_cancel.side.value if hasattr(order_to_cancel.side, 'value') else str(order_to_cancel.side),
                        "order_quantity": order_to_cancel.requested_shares
                    }
                )
        try:
            self._order_repository.request_cancellation(local_order_id_to_cancel, reason)
            
            # Publish repository cancellation request success
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "ORDER_CANCELLATION_MARKED_IN_REPOSITORY",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": # No order params available
                local_order_id_to_cancel if # No order params available
                local_order_id_to_cancel else "N/A",
                        "cancellation_trigger": "CHASE_STRATEGY",
                        "local_order_id": local_order_id_to_cancel,
                        "symbol": symbol,
                        "cancellation_reason": reason,
                        "cancellation_timestamp": time.time(),
                        "cancellation_stage": "REPOSITORY_CANCELLATION_MARKED",
                        "order_side": order_to_cancel.side.value if hasattr(order_to_cancel.side, 'value') else str(order_to_cancel.side),
                        "order_quantity": order_to_cancel.requested_shares
                    }
                )
        except Exception as e_repo_cancel:
            logger.error(f"[{symbol}] Chase: Error marking order {local_order_id_to_cancel} as CancelRequested in OR: {e_repo_cancel}")
            
            # Publish repository cancellation failure
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionRejection",
                    payload={
                        "rejection_type": "ORDER_CANCELLATION_FAILED",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "cancellation_failure_type": "REPOSITORY_CANCELLATION_FAILED",
                        "local_order_id": local_order_id_to_cancel,
                        "symbol": symbol,
                        "cancellation_reason": reason,
                        "repository_error": str(e_repo_cancel),
                        "failure_timestamp": time.time(),
                        "cancellation_stage": "REPOSITORY_CANCELLATION_REQUEST",
                        "order_side": order_to_cancel.side.value if hasattr(order_to_cancel.side, 'value') else str(order_to_cancel.side),
                        "order_quantity": order_to_cancel.requested_shares
                    }
                )
        # No time.sleep here; the main loop's sleep or subsequent logic will handle pauses.

    # --- Aggressive Chase Logic ---
    def _execute_aggressive_sell_chase(self,
                                   initial_order_params: OrderParameters,
                                   chase_config: Dict[str, Any],
                                   time_stamps: Optional[Dict[str, Any]],
                                   snapshot_version: Optional[str],
                                   ocr_confidence: Optional[float],
                                   ocr_snapshot_cost_basis: Optional[float],
                                   original_extra_fields: Optional[Dict[str, Any]],
                                   perf_timestamps: Dict[str, float]
                                  ) -> Optional[int]:
        symbol = initial_order_params.symbol.upper()
        parent_trade_id = initial_order_params.parent_trade_id

        if parent_trade_id is None:
            self.logger.error(f"[{symbol}] Chase: CRITICAL - parent_trade_id is None. Aborting.")
            return None

        # --- Fetch Chase Parameters ---
        max_duration_sec = float(chase_config.get("max_chase_duration_sec", getattr(self._config, 'meltdown_chase_max_duration_sec', 30.0)))
        check_interval_sec = float(chase_config.get("peg_update_interval_sec", getattr(self._config, 'meltdown_chase_peg_update_interval_sec', 0.25)))
        peg_aggression_ticks = int(chase_config.get("peg_aggression_ticks", getattr(self._config, 'meltdown_chase_peg_aggression_ticks', 1)))
        max_slippage_pct_cfg = float(chase_config.get("max_chase_slippage_percent", getattr(self._config, 'meltdown_chase_max_slippage_percent', 2.0)))
        max_chase_slippage_percent = max_slippage_pct_cfg / 100.0
        final_mkt_on_timeout = bool(chase_config.get("chase_final_mkt_on_timeout", getattr(self._config, 'meltdown_chase_final_market_order', True)))
        max_pending_link_sec = float(chase_config.get("max_pending_link_time_sec", getattr(self._config, 'CHASE_MAX_PENDING_LINK_TIME_SEC', 3.0)))
        max_ack_wait_sec = float(chase_config.get("max_acknowledged_wait_time_sec", getattr(self._config, 'CHASE_MAX_ACKNOWLEDGED_WAIT_TIME_SEC', 5.0)))
        # How long an active LMT order in the chase can sit without fills before we re-evaluate/re-peg
        lmt_active_check_duration_sec = float(chase_config.get("chase_lmt_order_active_check_duration_sec", getattr(self._config, 'meltdown_chase_peg_update_interval_sec', 0.25)))


        self.logger.warning(
            f"[{symbol}] ENTERING SMART PEG LMT SELL CHASE for TradeID {parent_trade_id}. Initial Target Qty: {initial_order_params.quantity:.0f}. "
            f"MaxDuration={max_duration_sec}s, CheckInterval={check_interval_sec}s, LMTActiveCheck={lmt_active_check_duration_sec}s"
        )
        add_timestamp(perf_timestamps, f'tm_executor.{symbol}.smart_lmt_chase_start')
        chase_start_time = time.time()
        
        original_total_shares_to_liquidate = abs(initial_order_params.quantity)

        # Calculate minimum acceptable limit price based on slippage tolerance
        initial_ref_price_for_slippage_calc = initial_order_params.limit_price or 0.0
        if initial_ref_price_for_slippage_calc <= 0.0:
            # Try to get current market price as reference if no limit price provided
            current_bid = self._price_provider.get_bid_price(symbol, allow_api_fallback=True) if self._price_provider else None
            initial_ref_price_for_slippage_calc = current_bid or 1.0  # Fallback to $1.00 if no price available

        # Calculate minimum acceptable limit price (prevent excessive slippage)
        min_acceptable_limit_price = initial_ref_price_for_slippage_calc * (1.0 - max_chase_slippage_percent)
        min_acceptable_limit_price = max(min_acceptable_limit_price, 0.01)  # Never go below $0.01

        self.logger.info(f"[{symbol}] Chase slippage control: RefPrice={initial_ref_price_for_slippage_calc:.4f}, "
                        f"MaxSlippage={max_chase_slippage_percent:.1%}, MinAcceptable={min_acceptable_limit_price:.4f}")

        active_local_order_id: Optional[int] = None
        last_placed_order_timestamp: float = 0.0
        
        # --- Initial Order Placement (Aggressive LMT, TIF determined by _get_appropriate_tif_for_chase) ---
        current_chase_order_params = initial_order_params.copy()
        current_chase_order_params.quantity = original_total_shares_to_liquidate # Ensure full initial quantity
        current_chase_order_params.order_type = "LMT" # Force initial chase to be LMT
        current_chase_order_params.time_in_force = self._get_appropriate_tif_for_chase() # Get appropriate TIF

        # Calculate fresh initial limit price
        initial_limit_price = None
        current_best_bid = self._price_provider.get_bid_price(symbol, allow_api_fallback=True) if self._price_provider else None

        if current_best_bid and current_best_bid > 0.01:
            tick_size = self._get_tick_size(symbol, current_best_bid)
            calculated_price = round(max(0.01, current_best_bid - (peg_aggression_ticks * tick_size)), 2) # Ensure 2 decimal places for price
            initial_limit_price = max(calculated_price, min_acceptable_limit_price) # Respect overall slippage
            current_chase_order_params.limit_price = initial_limit_price
            self.logger.info(f"[{symbol}] Chase (TradeID {parent_trade_id}): Calculated FRESH initial LMT price: {initial_limit_price:.4f} (Bid: {current_best_bid:.4f}, PegTicks: {peg_aggression_ticks}, MinAcceptable: {min_acceptable_limit_price:.4f})")
        else:
            self.logger.error(f"[{symbol}] Chase (TradeID {parent_trade_id}): FAILED to get valid current best bid ({current_best_bid}) for initial LMT price. Aborting chase.")
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.smart_lmt_chase_err_initial_price_fetch')
            
            # Publish price validation failure event for initial chase setup
            price_failure_context = {
                "validation_failure_type": "CHASE_INITIAL_PRICE_FETCH_FAILED",
                "failed_price_type": "CURRENT_BEST_BID",
                "received_price_value": current_best_bid,
                "symbol": symbol,
                "parent_trade_id": parent_trade_id,
                "order_side": initial_order_params.side if initial_order_params else "UNKNOWN",
                "order_quantity": initial_order_params.quantity if initial_order_params else 0.0,
                "failure_timestamp": time.time(),
                "price_provider_available": self._price_provider is not None,
                "min_acceptable_price": min_acceptable_limit_price,
                "reference_price": initial_ref_price_for_slippage_calc,
                "failure_reason": "INVALID_BID_PRICE_FOR_CHASE_INITIALIZATION",
                "abort_reason": "CANNOT_SET_INITIAL_LIMIT_PRICE"
            }
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionRejection",
                    payload={
                        "rejection_type": "PRICE_VALIDATION_FAILURE",
                        "symbol": order_params.symbol if order_params else "N/A",
                    }
                )
            return None # Abort chase if we can't set a fresh initial LMT price

        current_chase_order_params.event_type = f"CHASE_SELL_INIT_LMT" # More specific event type

        self.logger.info(f"[{symbol}] Chase (TradeID {parent_trade_id}): Placing FRESH initial aggressive LMT order: Qty={current_chase_order_params.quantity:.0f}, Type={current_chase_order_params.order_type}, LmtPx={current_chase_order_params.limit_price:.4f}, TIF={current_chase_order_params.time_in_force}")

        active_local_order_id = self._place_one_chase_order(current_chase_order_params,
                                                            time_stamps, snapshot_version, ocr_confidence,
                                                            ocr_snapshot_cost_basis, original_extra_fields,
                                                            perf_timestamps, "InitialChaseLMT")
        if active_local_order_id:
            last_placed_order_timestamp = time.time()
        else:
            self.logger.error(f"[{symbol}] Chase (TradeID {parent_trade_id}): FAILED to place initial fresh LMT chase order. Aborting chase.")
            add_timestamp(perf_timestamps, f'tm_executor.{symbol}.smart_lmt_chase_err_initial_place')
            return None

        # --- Iterative Smart PEG LMT Chase Loop ---
        while time.time() - chase_start_time < max_duration_sec:
            iteration_start_time = time.time()

            cumulative_filled_for_this_trade = self._order_repository.get_cumulative_filled_for_trade(parent_trade_id)
            shares_still_to_liquidate = original_total_shares_to_liquidate - cumulative_filled_for_this_trade
            
            self.logger.info(f"[{symbol}] Chase Loop (TradeID {parent_trade_id}): TargetTotal={original_total_shares_to_liquidate:.0f}, CumulativeFilled={cumulative_filled_for_this_trade:.0f}, StillToLiquidate={shares_still_to_liquidate:.0f}")

            if shares_still_to_liquidate <= 1e-9:
                self.logger.info(f"[{symbol}] Chase SUCCESS (TradeID {parent_trade_id}): All targeted shares filled.")
                if active_local_order_id:
                     self._attempt_cancellation_in_chase(symbol, active_local_order_id, "Chase completed, cancelling residual")
                add_timestamp(perf_timestamps, f'tm_executor.{symbol}.smart_lmt_chase_success')
                return active_local_order_id 

            needs_new_peg_order = False
            current_active_order_details: Optional[Order] = None

            if active_local_order_id:
                current_active_order_details = self._order_repository.get_order_by_local_id(active_local_order_id)
                if not current_active_order_details:
                    self.logger.warning(f"[{symbol}] Chase (TradeID {parent_trade_id}): Active order {active_local_order_id} details not found. Assuming done/gone.")
                    active_local_order_id = None
                    needs_new_peg_order = True
                else:
                    status = current_active_order_details.ls_status
                    order_age = time.time() - last_placed_order_timestamp
                    self.logger.debug(f"[{symbol}] Chase (TradeID {parent_trade_id}): Active order {active_local_order_id} Status={status.name}, Age={order_age:.1f}s, OrderLeftover={current_active_order_details.leftover_shares:.0f}")

                    is_terminal = status in (OrderLSAgentStatus.FILLED, OrderLSAgentStatus.CANCELLED, OrderLSAgentStatus.REJECTED)
                    is_stuck_pending = status == OrderLSAgentStatus.PENDING_SUBMISSION and order_age > max_pending_link_sec
                    is_stuck_acknowledged = status == OrderLSAgentStatus.ACKNOWLEDGED_BY_BROKER and order_age > max_ack_wait_sec
                    
                    # For a non-IOC LMT, check if it's been working too long without a fill or if market moved
                    is_lmt_stale_or_market_moved = False
                    if status in (OrderLSAgentStatus.WORKING, OrderLSAgentStatus.PARTIALLY_FILLED, OrderLSAgentStatus.SUBMITTED_TO_BROKER):
                        if order_age > lmt_active_check_duration_sec: # Waited long enough at this price
                            is_lmt_stale_or_market_moved = True
                            self.logger.info(f"[{symbol}] Chase: LMT {active_local_order_id} active for {order_age:.1f}s (max check {lmt_active_check_duration_sec}s). Checking market for re-peg.")
                        
                        # Check if market moved away even if not stale by time
                        current_best_bid = self._price_provider.get_bid_price(symbol, allow_api_fallback=True)
                        if current_best_bid and current_best_bid > 0.01:
                            tick_size = self._get_tick_size(symbol, current_best_bid)
                            ideal_peg_price = round(max(0.01, current_best_bid - (peg_aggression_ticks * tick_size)), 2)
                            ideal_peg_price = max(ideal_peg_price, min_acceptable_limit_price)
                            current_order_lmt_px = current_active_order_details.requested_lmt_price
                            if current_order_lmt_px is None or abs(ideal_peg_price - current_order_lmt_px) >= tick_size:
                                is_lmt_stale_or_market_moved = True # Market moved, our price is no longer aggressive
                                self.logger.info(f"[{symbol}] Chase: Market moved. Active LMT {active_local_order_id} ({current_order_lmt_px:.4f}) needs re-pegging to {ideal_peg_price:.4f}.")
                                
                                # Publish market movement detection event
                                if self._correlation_logger:
                                    self._correlation_logger.log_event(
                                        source_component="TradeExecutor",
                                        event_name="ExecutionDecision",
                                        payload={
                                            "decision_type": "CHASE_MARKET_MOVEMENT_DETECTED",
                                            "symbol": order_params.symbol if order_params else "N/A",
                                            "local_order_id": active_local_order_id if active_local_order_id else "N/A",
                                            "parent_trade_id": parent_trade_id,
                                            "current_order_price": current_order_lmt_px,
                                            "ideal_peg_price": ideal_peg_price,
                                            "price_difference": abs(ideal_peg_price - (current_order_lmt_px or 0)),
                                            "tick_size": tick_size,
                                            "current_best_bid": current_best_bid,
                                            "peg_aggression_ticks": peg_aggression_ticks,
                                            "decision": "REQUIRES_REPRICING",
                                            "chase_stage": "PRICE_STALE_DETECTION"
                                        }
                                    )

                    if is_terminal or is_stuck_pending or is_stuck_acknowledged or is_lmt_stale_or_market_moved:
                        log_reason = "terminal" if is_terminal else ("stuck pending" if is_stuck_pending else ("stuck ack" if is_stuck_acknowledged else "LMT stale/market moved"))
                        self.logger.info(f"[{symbol}] Chase (TradeID {parent_trade_id}): Active order {active_local_order_id} ({status.name}) is {log_reason}. Will cancel (if not terminal) and place new if shares remain.")
                        
                        # Publish order replacement decision
                        if self._correlation_logger:
                            self._correlation_logger.log_event(
                                source_component="TradeExecutor",
                                event_name="ExecutionDecision",
                                payload={
                                    "decision_type": "CHASE_ORDER_REPLACEMENT_REQUIRED",
                                    "symbol": order_params.symbol if order_params else "N/A",
                                    "local_order_id": active_local_order_id if active_local_order_id else "N/A",
                                    "parent_trade_id": parent_trade_id,
                                    "replacement_reason": log_reason,
                                    "order_status": status.name if status else "UNKNOWN",
                                    "is_terminal": is_terminal,
                                    "is_stuck_pending": is_stuck_pending,
                                    "is_stuck_acknowledged": is_stuck_acknowledged,
                                    "is_market_moved": is_lmt_stale_or_market_moved,
                                    "will_cancel": not is_terminal,
                                    "chase_stage": "ORDER_REPLACEMENT"
                                }
                            )
                        if not is_terminal:
                            self._attempt_cancellation_in_chase(symbol, active_local_order_id, f"Chase {log_reason}")
                        active_local_order_id = None
                        needs_new_peg_order = True
            else: 
                needs_new_peg_order = True
            
            if needs_new_peg_order and shares_still_to_liquidate > 1e-9:
                current_best_bid = self._price_provider.get_bid_price(symbol, allow_api_fallback=True)
                if not current_best_bid or current_best_bid <= 0.01:
                    self.logger.warning(f"[{symbol}] Chase (TradeID {parent_trade_id}): Cannot get live bid for new LMT. Holding for next check_interval.")
                    
                    # Publish price validation failure event for chase re-pegging
                    chase_price_failure_context = {
                        "validation_failure_type": "CHASE_REPEG_PRICE_FETCH_FAILED",
                        "failed_price_type": "CURRENT_BEST_BID",
                        "received_price_value": current_best_bid,
                        "symbol": symbol,
                        "parent_trade_id": parent_trade_id,
                        "shares_remaining": shares_still_to_liquidate,
                        "failure_timestamp": time.time(),
                        "price_provider_available": self._price_provider is not None,
                        "min_acceptable_price": min_acceptable_limit_price,
                        "failure_reason": "INVALID_BID_PRICE_FOR_CHASE_REPEG",
                        "action_taken": "HOLDING_FOR_NEXT_CHECK_INTERVAL",
                        "chase_stage": "RE_PEGGING"
                    }
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="TradeExecutor",
                            event_name="ExecutionDecision",
                            payload={
                                "decision_type": "PRICE_VALIDATION_FAILURE_HOLD",
                                "symbol": order_params.symbol if order_params else "N/A",
                                "local_order_id": "N/A",
                                **chase_price_failure_context
                            }
                        )
                    tick_size = self._get_tick_size(symbol, current_best_bid)
                    new_limit_price = round(max(0.01, current_best_bid - (peg_aggression_ticks * tick_size)), 2)
                    new_limit_price = max(new_limit_price, min_acceptable_limit_price)

                    # Publish chase order repricing decision
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="TradeExecutor",
                            event_name="ExecutionDecision",
                            payload={
                                "decision_type": "CHASE_ORDER_REPRICING",
                                "symbol": order_params.symbol if order_params else "N/A",
                                "local_order_id": active_local_order_id if active_local_order_id else "N/A",
                                "parent_trade_id": parent_trade_id,
                                "current_best_bid": current_best_bid,
                                "tick_size": tick_size,
                                "peg_aggression_ticks": peg_aggression_ticks,
                                "calculated_price_before_limits": current_best_bid - (peg_aggression_ticks * tick_size),
                                "new_limit_price": new_limit_price,
                                "min_acceptable_limit_price": min_acceptable_limit_price,
                                "shares_remaining": shares_still_to_liquidate,
                                "price_adjustment_reason": "MARKET_MOVEMENT_REPEG",
                                "chase_stage": "DYNAMIC_REPRICING"
                            }
                        )
                    # Determine appropriate TIF for premarket/extended hours
                    appropriate_tif = self._get_appropriate_tif_for_chase()

                    self.logger.info(f"[{symbol}] Chase (TradeID {parent_trade_id}): Placing new pegged LMT for {shares_still_to_liquidate:.0f} shares @ {new_limit_price:.4f} (TIF={appropriate_tif}).")

                    chase_peg_order_params = OrderParameters(
                        symbol=symbol, side="SELL", quantity=shares_still_to_liquidate,
                        order_type="LMT", limit_price=new_limit_price, time_in_force=appropriate_tif,
                        parent_trade_id=parent_trade_id,
                        event_type="CHASE_SELL_PEG_LMT", # Renamed
                        action_type=TradeActionType.CLOSE_POSITION,
                        correlation_id=initial_order_params.correlation_id # <<<< PROPAGATE CORRELATION ID >>>>
                    )
                    
                    active_local_order_id = self._place_one_chase_order(chase_peg_order_params, 
                                                                        time_stamps, snapshot_version, ocr_confidence,
                                                                        ocr_snapshot_cost_basis, original_extra_fields,
                                                                        perf_timestamps, "PegReattemptLMT")
                    if active_local_order_id:
                        last_placed_order_timestamp = time.time()
                    else: 
                        self.logger.error(f"[{symbol}] Chase (TradeID {parent_trade_id}): Failed to place new pegged LMT. Will retry if duration allows.")
            
            # Wait for the check interval before the next iteration
            # This allows time for fills to come in and for market to move
            time.sleep(check_interval_sec)

        # --- Loop Timed Out or Completed ---
        # (Final MKT order logic as previously specified, using shares_still_to_liquidate,
        #  and ensuring it calls _place_one_chase_order with MKT parameters)
        # ...
        add_timestamp(perf_timestamps, f'tm_executor.{symbol}.smart_lmt_chase_loop_end')
        final_cumulative_filled = self._order_repository.get_cumulative_filled_for_trade(parent_trade_id)
        final_shares_left_for_trade = original_total_shares_to_liquidate - final_cumulative_filled

        if final_shares_left_for_trade > 1e-9:
            self.logger.warning(f"[{symbol}] SMART PEG CHASE for TradeID {parent_trade_id} (orig qty {original_total_shares_to_liquidate:.0f}) TIMED OUT after {max_duration_sec:.1f}s. "
                              f"Shares left for this trade: {final_shares_left_for_trade:.0f}. Last active order: {active_local_order_id}.")
            if active_local_order_id: 
                self.logger.info(f"[{symbol}] Chase Timeout (TradeID {parent_trade_id}): Attempting to cancel last active LMT order {active_local_order_id}.")
                self._attempt_cancellation_in_chase(symbol, active_local_order_id, "Chase Timeout, before final MKT consideration")
                time.sleep(0.2) 

            if final_mkt_on_timeout and self._is_regular_trading_hours():
                self.logger.warning(f"[{symbol}] Chase Timeout (TradeID {parent_trade_id}): Attempting FINAL MARKET order for remaining {final_shares_left_for_trade:.0f} shares.")
                final_mkt_params = OrderParameters(
                    symbol=symbol, side="SELL", quantity=final_shares_left_for_trade,
                    order_type="MKT", limit_price=None, time_in_force="DAY",
                    parent_trade_id=parent_trade_id,
                    event_type="CHASE_SELL_FINAL_MKT", action_type=TradeActionType.CLOSE_POSITION,
                    correlation_id=initial_order_params.correlation_id # <<<< PROPAGATE CORRELATION ID >>>>
                )
                active_local_order_id = self._place_one_chase_order(final_mkt_params, time_stamps, snapshot_version, ocr_confidence, ocr_snapshot_cost_basis, original_extra_fields, perf_timestamps, "FinalMKT")
            else:
                self.logger.warning(f"[{symbol}] Chase Timeout (TradeID {parent_trade_id}): Final MKT not attempted (config_disabled: {not final_mkt_on_timeout} OR not_RTH: {not self._is_regular_trading_hours()}). Shares remaining for trade: {final_shares_left_for_trade:.0f}")
        else:
            self.logger.info(f"[{symbol}] Chase for TradeID {parent_trade_id} ended. All targeted shares assumed filled based on OR fills.")

        self.logger.warning(f"[{symbol}] SMART PEG LMT CHASE ENDED for TradeID {parent_trade_id}. Last active order_id: {active_local_order_id}. OrigTarget: {original_total_shares_to_liquidate:.0f}, TradeSharesLeft: {final_shares_left_for_trade:.0f}.")
        return active_local_order_id

    def _place_one_chase_order(self,
                             order_params_to_execute: OrderParameters,
                             time_stamps: Optional[Dict[str, Any]],
                             snapshot_version: Optional[str],
                             ocr_confidence: Optional[float],
                             ocr_snapshot_cost_basis: Optional[float],
                             original_extra_fields: Optional[Dict[str, Any]],
                             perf_timestamps: Dict[str, float],
                             chase_stage_name: str
                            ) -> Optional[int]:
        """Helper to create record and place one order during a chase. Returns local_id if broker send attempted."""
        symbol = order_params_to_execute.symbol
        parent_trade_id = order_params_to_execute.parent_trade_id # Should always be set by caller

        local_id = self._order_repository.create_copy_order(
            symbol=symbol, side=order_params_to_execute.side, event_type=order_params_to_execute.event_type,
            requested_shares=order_params_to_execute.quantity, req_time=time.time(),
            parent_trade_id=parent_trade_id, # Essential
            requested_lmt_price=order_params_to_execute.limit_price,
            order_type_str=order_params_to_execute.order_type,
            comment=f"{chase_stage_name} for TradeID {parent_trade_id}. Type: {order_params_to_execute.order_type}, TIF: {order_params_to_execute.time_in_force}",
            master_correlation_id=order_params_to_execute.correlation_id, # <<<< PASS IT HERE >>>>
            action_type=order_params_to_execute.action_type.name if order_params_to_execute.action_type else None,
            ocr_time=time_stamps.get("ocr_raw_time") if time_stamps else None,
            timestamp_ocr_processed=time_stamps.get("timestamp_ocr_processed") if time_stamps else None,
            ocr_confidence=ocr_confidence,
            perf_timestamps=perf_timestamps,
            **(original_extra_fields or {}) # Carry over original meta if relevant
        )

        if not local_id:
            self.logger.error(f"[{symbol}] Chase (TradeID {parent_trade_id}): Failed to create OR record for {chase_stage_name}.")
            # Publish chase order creation failure
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "CHASE_ORDER_CREATION_FAILED",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": None if None else "N/A",
                        "chase_stage": chase_stage_name,
                        "parent_trade_id": parent_trade_id,
                        "failure_reason": "ORDER_REPOSITORY_CREATION_FAILED"
                    }
                )
        
        # Publish chase order creation success
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="TradeExecutor",
                event_name="ExecutionDecision",
                payload={
                    "decision_type": "CHASE_ORDER_CREATED",
                    "symbol": order_params.symbol if order_params else "N/A",
                    "local_order_id": local_id if local_id else "N/A",
                    "chase_stage": chase_stage_name,
                    "parent_trade_id": parent_trade_id,
                    "order_type": order_params_to_execute.order_type,
                    "time_in_force": order_params_to_execute.time_in_force,
                    "limit_price": order_params_to_execute.limit_price,
                    "quantity": order_params_to_execute.quantity,
                    "outside_rth": True
                }
            )
        try:
            self.logger.info(f"[{symbol}] Chase (TradeID {parent_trade_id}): Placing {chase_stage_name} order local_id={local_id}, Qty={order_params_to_execute.quantity:.0f}, Type={order_params_to_execute.order_type}, LmtPx={order_params_to_execute.limit_price}, TIF={order_params_to_execute.time_in_force}")
            self._broker.submit_order({
                'symbol': symbol, 
                'side': order_params_to_execute.side, 
                'quantity': int(round(order_params_to_execute.quantity)),
                'order_type': order_params_to_execute.order_type, 
                'limit_price': order_params_to_execute.limit_price,
                'local_order_id': local_id, 
                'correlation_id': order_params_to_execute.correlation_id, # Pass master correlation ID (Break #3 fix)
                'time_in_force': order_params_to_execute.time_in_force,
                'outside_rth': True  # CRITICAL: Enable premarket/extended hours trading
            })
            
            # Publish successful chase order placement
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "CHASE_ORDER_SENT_TO_BROKER",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": local_id if local_id else "N/A",
                        "chase_stage": chase_stage_name,
                        "parent_trade_id": parent_trade_id,
                        "broker_submission_successful": True,
                        "extended_hours_enabled": True
                    }
                )
            return local_id
        except Exception as e_broker:
            self.logger.error(f"[{symbol}] Chase (TradeID {parent_trade_id}): FAILED to place {chase_stage_name} (local_id={local_id}): {e_broker}", exc_info=True)
            self._order_repository.update_order_status_locally(local_id, "SendFailed", reason=f"{chase_stage_name}BrokerFail: {e_broker}")
            
            # Publish chase order broker failure
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name="ExecutionDecision",
                    payload={
                        "decision_type": "CHASE_ORDER_BROKER_FAILED",
                        "symbol": order_params.symbol if order_params else "N/A",
                        "local_order_id": local_id if local_id else "N/A",
                        "chase_stage": chase_stage_name,
                        "parent_trade_id": parent_trade_id,
                        "broker_error": str(e_broker),
                        "exception_type": type(e_broker).__name__
                    }
                )
            return None

    def _get_appropriate_tif_for_chase(self) -> str:
        """Determine appropriate Time-In-Force for chase orders.

        Lightspeed uses DAY orders for both regular hours and premarket/extended hours.
        The C++ bridge hardcodes L_TIF::DAY for all orders regardless of session.

        Returns:
            "DAY" - Works for both regular hours and premarket in Lightspeed
        """
        return "DAY"
    
    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="TradeExecutor-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
        logger.info("Started async IPC publishing worker thread for TradeExecutor")
    
    def _ipc_worker_loop(self):
        """Worker loop that processes IPC messages asynchronously."""
        while not self._ipc_shutdown_event.is_set():
            try:
                # Get message from queue with timeout
                try:
                    target_stream, redis_message = self._ipc_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Attempt to send via IPC
                try:
                    # Legacy IPC code - now using telemetry service
                    logger.debug(f"Legacy IPC queue processing - message queued but telemetry service should be used instead")
                        
                except Exception as e:
                    logger.error(f"Async IPC: Error publishing execution decision: {e}")
                    
            except Exception as e:
                logger.error(f"Error in TradeExecutor IPC worker loop: {e}", exc_info=True)
                
        logger.info("TradeExecutor IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str):
        """Queue an IPC message for async publishing via telemetry service."""
        try:
            # Parse the message to extract event type
            import json
            msg_data = json.loads(redis_message)
            event_type = msg_data.get('metadata', {}).get('eventType', 'UNKNOWN')
            payload_data = msg_data.get('payload', {})
            
            # Use telemetry service for clean, fire-and-forget publishing
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="TradeExecutor",
                    event_name=event_type,
                    payload=payload_data
                )
        except Exception as e:
            logger.error(f"Error queuing to telemetry service: {e}")
            # Fallback to local queue if available
            try:
                self._ipc_queue.put_nowait((target_stream, redis_message))
            except queue.Full:
                logger.warning(f"Local IPC queue also full, dropping execution decision for stream '{target_stream}'")

    """
    # DELETED: _publish_execution_decision_to_redis method
    # This method was replaced with direct correlation logger calls
    """

    """
    # DELETED: _publish_execution_rejection_to_redis method
    # This method was replaced with direct correlation logger calls
    """

    # --- Service Lifecycle and Health Monitoring ---
    def start(self):
        """Start the TradeExecutor service."""
        self.logger.info("Starting TradeExecutor...")
        
        # Start IPC worker thread (correlation logger handles telemetry)
        self._start_ipc_worker()
        self.logger.info("TradeExecutor using local IPC worker")
        
        # Initialize health monitoring tracking
        self._is_running = True
        self._last_health_check_time = time.time()
        self._execution_count = 0
        self._execution_failures = 0
        self._last_execution_time = 0.0
        
        # INSTRUMENTATION: Publish service health status - STARTED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="TradeExecutor",
                event_name="ServiceHealthStatus",
                payload={
                    "health_status": "STARTED",
                    "status_reason": "TradeExecutor started successfully",
                    "service_name": "TradeExecutor",
                    "broker_available": self._broker is not None,
                    "order_repository_available": self._order_repository is not None,
                    "position_manager_available": self._position_manager is not None,
                    "price_provider_available": self._price_provider is not None,
                    "risk_service_available": self._risk_service is not None,
                    "trade_manager_service_available": self._trade_manager_service is not None,
                    "event_bus_available": self._event_bus is not None,
                    "correlation_logger_available": self._correlation_logger is not None,
                    "execution_count": 0,
                    "health_status": "HEALTHY",
                    "startup_timestamp": time.time()
                }
            )
        
        self.logger.info("TradeExecutor started successfully")
    
    def stop(self):
        """Stop the TradeExecutor service."""
        self.logger.info("Stopping TradeExecutor...")
        
        self._is_running = False
        
        # Get final execution metrics
        execution_count = getattr(self, '_execution_count', 0)
        execution_failures = getattr(self, '_execution_failures', 0)
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="TradeExecutor",
                event_name="ServiceHealthStatus",
                payload={
                    "health_status": "STOPPED",
                    "status_reason": "TradeExecutor stopped and cleaned up resources",
                    "service_name": "TradeExecutor",
                    "total_executions": execution_count,
                    "total_failures": execution_failures,
                    "success_rate": ((execution_count - execution_failures) / execution_count * 100) if execution_count > 0 else 0,
                    "health_status": "STOPPED",
                    "shutdown_timestamp": time.time()
                }
            )
        
        self.logger.info("TradeExecutor stopped successfully")

    """
    # DELETED: _publish_service_health_status method
    # This method was replaced with direct correlation logger calls
    """
    
    def _DELETED_publish_service_health_status(self, health_status: str, status_reason: str, 
                                     context: Dict[str, Any], correlation_id: str = None) -> None:
        """DELETED: This method was replaced with direct correlation logger calls"""
        pass

    """
    # DELETED: _publish_periodic_health_status method
    # This method was replaced with direct correlation logger calls
    """
    
    def _DELETED_publish_periodic_health_status(self) -> None:
        """DELETED: This method was replaced with direct correlation logger calls"""
        pass

    def check_and_publish_health_status(self) -> None:
        """
        Public method to trigger health status check and publishing.
        Can be called periodically by external services or after executions.
        """
        current_time = time.time()
        if not hasattr(self, '_last_health_check_time') or \
           (current_time - getattr(self, '_last_health_check_time', 0) > 30.0):  # Check every 30 seconds
            # DELETED: call to _publish_periodic_health_status()
            # Health status publishing now handled by correlation logger
            self._last_health_check_time = current_time
            
    def _track_execution_metrics(self, success: bool) -> None:
        """
        Track execution metrics for health monitoring.
        
        Args:
            success: Whether the execution was successful
        """
        if not hasattr(self, '_execution_count'):
            self._execution_count = 0
        if not hasattr(self, '_execution_failures'):
            self._execution_failures = 0
            
        self._execution_count += 1
        if not success:
            self._execution_failures += 1
        self._last_execution_time = time.time()
    
    def shutdown(self) -> None:
        """
        Shutdown the TradeExecutor and its async IPC worker thread.
        """
        logger.info("TradeExecutor shutting down...")
        
        # Stop IPC worker thread
        self._ipc_shutdown_event.set()
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            self._ipc_worker_thread.join(timeout=2.0)
            if self._ipc_worker_thread.is_alive():
                logger.warning("TradeExecutor IPC worker thread did not stop gracefully")
        
        logger.info("TradeExecutor shutdown complete")