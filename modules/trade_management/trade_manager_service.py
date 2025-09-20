# --- services/trade_manager_service.py (Refactored Code - Part 3) ---

import threading
import time
import math
import re
import random
import logging
import copy
import queue
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable, Tuple, Set

# Dependency Interfaces
# import sys  # No longer needed if only used for sys.path
# import os   # No longer needed if only used for sys.path
# sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) # REMOVED: Problematic sys.path manipulation

# --- Interfaces ---
from interfaces.trading.services import (
    ITradeManagerService, # Import from interfaces.py
    IPositionManager, ISnapshotInterpreter, TradeSignal, TradeActionType,
    IOrderStrategy, OrderParameters, ITradeExecutor, ITradeLifecycleManager, LifecycleState, # Add Lifecycle Manager
    TradeOpenedCallback, TradeClosedCallback # Callback types for trade lifecycle events
)

# Event Bus and Events for ValidatedOrderRequestEvent handling
from interfaces.core.services import IEventBus, ILifecycleService
from core.events import ValidatedOrderRequestEvent, OrderRequestData

# Import order data types for proper handling of Order objects
from data_models.order_data_types import (
    Order, OrderLSAgentStatus, FillRecord, OrderSide, OrderEventType
)

# Concrete types for type hinting or dependencies
from interfaces.broker.services import IBrokerService
from utils.global_config import GlobalConfig as IConfigService # Assuming concrete class used
from interfaces.data.services import IPriceProvider
# Add import for OrderRepository interface
try:
    from interfaces.order_management.services import IOrderRepository
except ImportError:
    print("ERROR: Could not import IOrderRepository from modules.order_management.interfaces")
    class IOrderRepository: pass # Dummy
# Add import for Alpaca REST API client
try:
    import alpaca_trade_api as tradeapi # For type hinting
    from alpaca_trade_api.rest import APIError # <<< ADD THIS IMPORT
except ImportError:
    print("ERROR: Could not import alpaca_trade_api. Run: pip install alpaca-trade-api")
    tradeapi = None # Placeholder
    APIError = Exception # Define as base Exception if import fails
# Add import for Risk Management interfaces
from interfaces.risk.services import IRiskManagementService, RiskLevel # Import Enum

# Other necessary imports from the old TMM
from utils.performance_tracker import create_timestamp_dict, add_timestamp, calculate_durations, add_to_stats, is_performance_tracking_enabled
from utils.flags import set_kill_switch as set_global_kill_switch, get_kill_switch as get_global_kill_switch # Keep global for now? Or manage internally?
from utils.function_tracer import trace_function
from utils.decorators import log_function_call

# --- BEGIN MODIFICATION 1.1: Add CoreLogicManager import for type hinting ---
# This is conceptual as the actual import path might differ or be a forward reference
# from modules.core_logic.core_logic_manager import CoreLogicManager # Uncomment if available
# --- END MODIFICATION 1.1 ---

# NEW: Import ICorrelationLogger interface for IntelliSense
try:
    from interfaces.logging.services import ICorrelationLogger
except ImportError:
    # Graceful fallback if IntelliSense not available
    ICorrelationLogger = None

# Setup logger for this service
logger = logging.getLogger("trade_manager_service")

class TradeManagerService(ITradeManagerService, ILifecycleService):
    """
    Orchestrates trade management by coordinating specialized components.
    Implements ITradeManagerService interface.
    """
    MAX_OPEN_RETRIES = 1
    BASE_OPEN_RETRY_COOLDOWN_SEC = 0.3

    # Constants for caching removed - direct repository access is used instead

    @log_function_call('trade_manager')
    def __init__(self,
                 event_bus: IEventBus,
                 broker_service: IBrokerService,
                 price_provider: IPriceProvider,
                 config_service: IConfigService,
                 order_repository: IOrderRepository,
                 position_manager: IPositionManager,
                 order_strategy: IOrderStrategy,
                 trade_executor: ITradeExecutor,
                 lifecycle_manager: ITradeLifecycleManager,
                 snapshot_interpreter: Optional[ISnapshotInterpreter] = None,
                 rest_client: Optional['tradeapi.REST'] = None,
                 risk_service: Optional[IRiskManagementService] = None,
                 gui_logger_func: Optional[Callable[[str, str], None]] = None,
                 pipeline_validator: Optional[Any] = None,
                 benchmarker: Optional[Any] = None,
                 correlation_logger: 'Optional[ICorrelationLogger]' = None): # NEW PARAMETER
        # Use the module-level logger as an instance logger for consistent logging
        self.logger = logger
        self.logger.info("Initializing TradeManagerService (Order Orchestrator)...")
        self.event_bus = event_bus
        self._broker: IBrokerService = broker_service
        self._price_provider: IPriceProvider = price_provider
        self._config: IConfigService = config_service
        self._gui_logger_func = gui_logger_func
        self._order_repository: IOrderRepository = order_repository
        self._position_manager: IPositionManager = position_manager
        self.logger.info(f"TMS __init__: Using PositionManager instance ID: {id(self._position_manager)}") # <<< ADD LOG
        self._snapshot_interpreter: Optional[ISnapshotInterpreter] = snapshot_interpreter
        self._order_strategy: IOrderStrategy = order_strategy
        self._trade_executor: ITradeExecutor = trade_executor
        # Set the trade_manager_service reference in the executor for trading_enabled checks
        if hasattr(self._trade_executor, '_trade_manager_service'):
            self._trade_executor._trade_manager_service = self
        # Pass the GUI logger function to the executor if it has the attribute
        if hasattr(self._trade_executor, '_gui_logger_func'):
            self._trade_executor._gui_logger_func = self._gui_logger_func
        self._lifecycle_manager: ITradeLifecycleManager = lifecycle_manager
        self._rest_client = rest_client
        self._risk_service = risk_service
        self._pipeline_validator = pipeline_validator  # Store pipeline validator for order execution tracking
        self._benchmarker = benchmarker  # Store benchmarker for performance metrics
        self._correlation_logger = correlation_logger # NEW: Store the logger

        if self._correlation_logger: # NEW Log
            self.logger.info("TradeManagerService initialized WITH CorrelationLogger for IntelliSense.")
        else:
            self.logger.info("TradeManagerService initialized WITHOUT CorrelationLogger.")

        # Internal State (Trade Lifecycle now handled by lifecycle_manager)
        self._single_gui_position: Dict[str, Any] = {"open": False} # Kept for GUI summary
        self._single_gui_position_lock = threading.RLock()  # Lock for thread-safe access to _single_gui_position

        self._emergency_force_close_callback: Optional[Callable[[str, str], None]] = None
        self._is_trading_enabled = True
        self._trading_enabled_lock = threading.RLock()  # Lock for thread-safe access to _is_trading_enabled
        self._debug_mode: bool = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop_event: Optional[threading.Event] = None
        
        # Initialize async IPC publishing infrastructure
        self._ipc_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        # Thread creation deferred to start() method

        # OCR service reference
        self._ocr_service = None  # Will be set via set_ocr_service_dependency

        # OCR state tracking
        self._last_ocr_state: Dict[str, Dict[str, float]] = {}
        self._ocr_state_lock = threading.RLock()

        # Order cache removed - direct repository access is used instead
        # Ledger state removed - lifecycle manager handles ledger now

        # Duplicate event suppression
        self._recent_fingerprints: Dict[str, Set[tuple]] = {}  # symbol -> set of fingerprints
        self._fingerprint_timestamps: Dict[str, Dict[tuple, float]] = {}  # symbol -> {fingerprint -> timestamp}
        self._fingerprint_lock = threading.RLock()
        self._fingerprint_expiry_seconds = 5.0  # How long to remember fingerprints

        # <<< --- ADD THIS SECTION --- >>>
        # State for managing suppressed open attempts due to preliminary risk checks
        self._suppressed_open_attempts: Dict[str, Dict[str, Any]] = {}
        # Example structure for inner dict value:
        # {
        #     "symbol": str,
        #     "suppressed_at_ts": float,
        #     "original_reason": str,
        #     "estimated_entry_price_at_suppression": float,
        #     "retry_until_ts": float,
        #     "price_tolerance_cents": float,
        #     "original_snapshot_data": Dict[str, Any] # The snapshot that would have triggered the OPEN
        # }
        self._suppressed_open_attempts_lock = threading.RLock()
        # <<< --- END ADD --- >>>

        # NOTE: MasterActionFilter is now handled by MasterActionFilterService
        # No longer instantiated here as OCR scalping flow uses the service

        # Store strong reference to the handler to prevent garbage collection
        self._validated_order_handler = self._handle_validated_order_request

        # Event subscription moved to start() method

        # State for tracking symbols on post-meltdown hold
        self._symbols_on_post_meltdown_hold: Set[str] = set()
        self._post_meltdown_hold_lock = threading.RLock()

        # State for tracking recently synced symbols
        self._recently_synced_symbols: Dict[str, float] = {}  # symbol -> timestamp_of_last_broker_sync
        self._sync_grace_lock = threading.RLock()

        # --- BEGIN MODIFICATION 1.2: Initialize _core_logic_manager_ref ---
        self._core_logic_manager_ref = None  # Will be set via set_core_logic_manager method
        # --- END MODIFICATION 1.2 ---

        # Initialize service state
        self._is_running = False

        self.logger.info("TradeManagerService (Order Orchestrator) Initialized.")

    @property
    def is_ready(self) -> bool:
        """
        Returns True when the service is fully initialized and ready.
        """
        return hasattr(self, '_is_running') and self._is_running
    
    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="TradeManager-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
        self.logger.info("Started async IPC publishing worker thread")
    
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
                    if self._correlation_logger:
                        # Use correlation logger for event logging
                        # Parse the message to extract components
                        import json
                        msg_data = json.loads(redis_message)
                        event_type = msg_data.get('metadata', {}).get('eventType', 'UNKNOWN')
                        payload_data = msg_data.get('payload', {})
                        correlation_id = msg_data.get('metadata', {}).get('correlationId')
                        
                        # Convert event type to camel case for correlation logger
                        event_name = ''.join(word.capitalize() for word in event_type.split('_'))
                        
                        # Include correlation_id in payload if it exists
                        if correlation_id and 'correlation_id' not in payload_data:
                            payload_data['correlation_id'] = correlation_id
                        
                        self._correlation_logger.log_event(
                            source_component="TradeManagerService",
                            event_name=event_name,
                            payload=payload_data,
                            stream_override=target_stream
                        )
                        self.logger.debug(f"Correlation logged: {event_name} to stream '{target_stream}'")
                    else:
                        self.logger.debug("Telemetry: No telemetry service available")
                        
                except Exception as e:
                    self.logger.error(f"Async IPC: Error publishing: {e}")
                    
            except Exception as e:
                self.logger.error(f"Error in IPC worker loop: {e}", exc_info=True)
                
        self.logger.info("IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str):
        """Queue an IPC message for async publishing."""
        try:
            self._ipc_queue.put_nowait((target_stream, redis_message))
        except queue.Full:
            self.logger.warning(f"IPC queue full, dropping message for stream '{target_stream}'")

    """
    # DELETED: _publish_trading_state_change_to_redis method
    # This method was replaced with direct correlation logger calls
    """

    """
    # DELETED: _publish_signal_decision method
    # This method was replaced with direct correlation logger calls
    """

    def _subscribe_to_events(self):
        """Subscribe to ValidatedOrderRequestEvent for order orchestration."""
        self.logger.critical(f"TMS_SUBSCRIBE_DEBUG: _subscribe_to_events called. EventBus ID: {id(self.event_bus) if self.event_bus else 'None'}")
        self.logger.critical(f"TMS_SUBSCRIBE_DEBUG: Subscribing to event type: {ValidatedOrderRequestEvent.__name__}")
        if self.event_bus:
            # Use the stored strong reference to prevent garbage collection
            self.event_bus.subscribe(ValidatedOrderRequestEvent, self._validated_order_handler)
            self.logger.info("TradeManagerService (Order Orchestrator) subscribed to ValidatedOrderRequestEvent.")
            
            # Verify subscription
            if hasattr(self.event_bus, '_subscribers'):
                handlers = self.event_bus._subscribers.get(ValidatedOrderRequestEvent, set())
                self.logger.critical(f"TMS_SUBSCRIBE_DEBUG: After subscription, found {len(handlers)} handlers for ValidatedOrderRequestEvent")
        else:
            self.logger.warning("No event_bus available - ValidatedOrderRequestEvent handling disabled.")

    def _handle_validated_order_request(self, event: ValidatedOrderRequestEvent):
        """
        Handle ValidatedOrderRequestEvent by orchestrating order execution.
        This is the new primary role of TradeManagerService for OCR scalping.

        Args:
            event: The ValidatedOrderRequestEvent containing order request data
        """
        self.logger.critical(f"TMS_ORDER_ORCH_DEBUG: _handle_validated_order_request ENTERED for event ID: {event.event_id}")
        order_request_data: OrderRequestData = event.data
        
        # Capture correlation context
        master_correlation_id = getattr(order_request_data, 'correlation_id', None)
        # The ID of the current event is the cause of the next event
        causation_id_for_next_step = getattr(event, 'event_id', None)
        
        if not master_correlation_id:
            self.logger.error(f"CRITICAL: Missing correlation_id on event {type(event).__name__}. This breaks end-to-end tracing!")
            master_correlation_id = "MISSING_CORRELATION_ID"
        
        symbol = order_request_data.symbol.upper()
        self.logger.info(f"[{symbol}] TMS (Order Orchestrator) received ValidatedOrderRequest: "
                        f"{order_request_data.request_id}, Action via Source: {order_request_data.source_strategy}")

        # Extract validation_id for pipeline tracking
        validation_id = order_request_data.validation_id

        # Register stage: validated order received by executor (TradeManagerService acts as orchestrator)
        if validation_id and hasattr(self, '_pipeline_validator') and self._pipeline_validator:
            self._pipeline_validator.register_stage_completion(
                validation_id,
                'validated_order_received_by_executor',
                stage_data={'request_id': order_request_data.request_id, 'symbol': symbol}
            )

        # Start timing for benchmarking
        executor_handling_start_time = time.time() if validation_id and hasattr(self, '_benchmarker') and self._benchmarker else None

        if not self.is_trading_enabled():
            self.logger.warning(f"[{symbol}] Skipping validated order request {order_request_data.request_id}: "
                               f"Trading is disabled.")
            # --- NEW: Correlation ID Logging for SKIPPED action ---
            correlation_id_from_event = getattr(order_request_data, 'correlation_id', None)
            if self._correlation_logger and correlation_id_from_event:
                log_payload_skipped = {
                    "event_name_in_payload": "ValidatedOrderProcessedByExecutor", # Keep consistent event name
                    "correlation_id": correlation_id_from_event,
                    "request_id": order_request_data.request_id,
                    "symbol": symbol,
                    "status": "SKIPPED_TRADING_DISABLED",
                    "reason": "Trading_Disabled",
                    "validation_pipeline_id": validation_id
                }
                self._correlation_logger.log_event(
                    source_component="TradeManagerService",
                    event_name="TradingSkipped",
                    payload=log_payload_skipped
                )
                self.logger.info(f"Correlation Logged for Skipped ValidatedOrderRequest: CorrID {correlation_id_from_event}, ReqID {order_request_data.request_id}")
            # --- END NEW ---
            return

        try:
            # 1. Determine TradeActionType from source_strategy
            # Extract action type from source_strategy like 'OCR_SCALPING_OPEN_LONG' -> 'OPEN_LONG'
            if 'OCR_SCALPING_' in order_request_data.source_strategy:
                action_type_str = order_request_data.source_strategy.replace('OCR_SCALPING_', '')
            else:
                # Fallback for other patterns
                action_type_str = order_request_data.source_strategy.split('_')[-1]

            try:
                action_type = TradeActionType[action_type_str]
            except KeyError:
                self.logger.error(f"[{symbol}] Unknown action type '{action_type_str}' from "
                                f"source_strategy '{order_request_data.source_strategy}'. Skipping.")
                return

            # 2. Get/Create parent_trade_id using lifecycle_manager
            # Extract IntelliSense UUID for end-to-end nanosecond tracking
            intellisense_uuid_for_trade = order_request_data.correlation_id

            parent_trade_id: Optional[int] = None
            if action_type == TradeActionType.OPEN_LONG:
                parent_trade_id = self._lifecycle_manager.create_new_trade(
                    symbol,
                    origin=order_request_data.source_strategy,
                    correlation_id=intellisense_uuid_for_trade,  # <<< PASS INTELLISENSE UUID HERE
                    initial_comment=f"OCR Event: {order_request_data.related_ocr_event_id or 'N/A'}"
                )
            else:
                # For ADD, REDUCE, CLOSE - find existing trade first
                parent_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)
                if parent_trade_id is None:
                    self.logger.warning(f"[{symbol}] No active trade for {action_type.name}. Creating new with CorrID: {intellisense_uuid_for_trade}")
                    parent_trade_id = self._lifecycle_manager.create_new_trade(
                        symbol,
                        origin=f"FORCED_{order_request_data.source_strategy}",
                        correlation_id=intellisense_uuid_for_trade,  # Use current signal's CorrID
                        initial_comment="Auto-created for event with no prior trade"
                    )
                    if parent_trade_id:
                        self._lifecycle_manager.update_trade_state(
                            parent_trade_id,
                            new_state=LifecycleState.OPEN_ACTIVE,
                            comment="Auto-created for event with no prior trade")

            if parent_trade_id is None:
                self.logger.error(f"[{symbol}] Failed to get/create parent_trade_id for {action_type.name}. "
                                f"Skipping order.")
                return

            # 3. Construct TradeSignal for OrderStrategy compatibility
            temp_signal_for_strategy = TradeSignal(
                action=action_type,
                symbol=symbol,
                triggering_snapshot={"aggressive_chase": "LIQUIDATE" in order_request_data.source_strategy}
            )

            # 4. Call OrderStrategy to calculate order parameters (primarily for pricing)
            order_params: Optional[OrderParameters] = self._order_strategy.calculate_order_params(
                signal=temp_signal_for_strategy,
                position_manager=self._position_manager,
                price_provider=self._price_provider,
                config_service=self._config
            )

            if not order_params:
                self.logger.error(f"[{symbol}] OrderStrategy failed to generate parameters for "
                                f"{order_request_data.request_id}. Skipping.")
                if action_type == TradeActionType.OPEN_LONG and parent_trade_id:
                    self._lifecycle_manager.mark_trade_failed_to_open(parent_trade_id, "OrderStrategy failed")
                return

            # 5. Override quantity and side from ValidatedOrderRequestEvent
            order_params.quantity = order_request_data.quantity
            order_params.side = order_request_data.side
            order_params.parent_trade_id = parent_trade_id
            order_params.event_type = order_request_data.source_strategy
            order_params.action_type = action_type
            order_params.correlation_id = order_request_data.correlation_id # <<<< COPY CORRELATION ID >>>>

            # Use limit price hint if provided
            if (order_params.order_type == "LMT" and
                order_params.limit_price is None and
                hasattr(order_request_data, 'limit_price') and
                order_request_data.limit_price is not None):
                order_params.limit_price = order_request_data.limit_price
                self.logger.info(f"[{symbol}] Using limit price hint {order_request_data.limit_price} "
                               f"from OrderRequestData.")

            # 6. Execute the trade
            time_stamps_for_executor = {
                "ocr_frame_timestamp": order_request_data.ocr_frame_timestamp,
                "request_received_by_tms": time.time()
            }

            # Create performance timestamps
            from utils.performance_tracker import create_timestamp_dict, add_timestamp, is_performance_tracking_enabled
            perf_ts_from_event = getattr(order_request_data, 'perf_timestamps', None)
            current_perf_ts = perf_ts_from_event if perf_ts_from_event else create_timestamp_dict()
            if is_performance_tracking_enabled():
                add_timestamp(current_perf_ts, 'tms_orchestrator.validated_event_received')

            # Extract triggering cost basis for fingerprinting (robust access)
            # Check if triggering_cost_basis is directly on order_request_data
            if hasattr(order_request_data, 'triggering_cost_basis'):
                ocr_snapshot_cost_basis = order_request_data.triggering_cost_basis
            else:
                # Fallback: check if it's in a meta dictionary
                request_meta = getattr(order_request_data, 'meta', {})
                ocr_snapshot_cost_basis = request_meta.get("triggering_cost_basis")
                if ocr_snapshot_cost_basis is None:
                    self.logger.warning(f"[{symbol}] No triggering_cost_basis found in OrderRequestData. Using None for fingerprinting.")

            self.logger.debug(f"[{symbol}] Extracted ocr_snapshot_cost_basis: {ocr_snapshot_cost_basis}")

            # --- NEW: Correlation ID Logging before calling executor ---
            correlation_id_from_event = getattr(order_request_data, 'correlation_id', None)
            if self._correlation_logger and correlation_id_from_event:
                log_payload_processed = {
                    "event_name_in_payload": "ValidatedOrderProcessedByExecutor", # Expected by pipeline checker
                    "correlation_id": correlation_id_from_event,
                    "request_id": order_request_data.request_id, # Expected field name
                    "symbol": symbol,
                    "action_type": action_type.name if action_type else "UNKNOWN",
                    "order_params_side": order_params.side,
                    "order_params_quantity": order_params.quantity,
                    "order_params_type": order_params.order_type,
                    "status": "FORWARDED_TO_EXECUTOR",
                    "validation_pipeline_id": validation_id
                }
                self._correlation_logger.log_event(
                    source_component="TradeManagerService",
                    event_name="OrderProcessed",
                    payload=log_payload_processed
                )
                self.logger.info(f"Correlation Logged for Processed ValidatedOrderRequest: CorrID {correlation_id_from_event}, ReqID {order_request_data.request_id}")
            elif self._correlation_logger:
                 self.logger.warning(f"CorrelationLogger present but no correlation_id found in ValidatedOrderRequest.data for ReqID {order_request_data.request_id}")
            # --- END NEW ---

            # Prepare extra_fields with validation_id for pipeline tracking
            extra_fields_with_validation = {"source_request_id": order_request_data.request_id}
            if validation_id:
                extra_fields_with_validation["validation_id"] = validation_id

            executed_local_id = self._trade_executor.execute_trade(
                order_params=order_params,
                time_stamps=time_stamps_for_executor,
                snapshot_version=f"REQ_{order_request_data.request_id}",
                ocr_confidence=None,
                ocr_snapshot_cost_basis=ocr_snapshot_cost_basis,
                extra_fields=extra_fields_with_validation,
                is_manual_trigger=False,
                perf_timestamps=current_perf_ts
            )

            # Register stage: order sent to broker interface
            if validation_id and hasattr(self, '_pipeline_validator') and self._pipeline_validator and executed_local_id:
                self._pipeline_validator.register_stage_completion(
                    validation_id,
                    'order_sent_to_broker_interface',
                    stage_data={'request_id': order_request_data.request_id, 'local_order_id': executed_local_id}
                )

            if executed_local_id:
                self.logger.info(f"[{symbol}] TMS (Order Orchestrator): TradeExecutor initiated order "
                               f"{executed_local_id} for request {order_request_data.request_id}.")
                self._lifecycle_manager.update_on_order_submission(parent_trade_id, executed_local_id)
            else:
                self.logger.error(f"[{symbol}] TMS (Order Orchestrator): TradeExecutor FAILED to initiate "
                                f"order for request {order_request_data.request_id}.")
                if action_type == TradeActionType.OPEN_LONG and parent_trade_id:
                    self._lifecycle_manager.mark_trade_failed_to_open(parent_trade_id,
                                                                    "Executor failed post-validation")

        except Exception as e:
            self.logger.error(f"[{symbol}] Error handling ValidatedOrderRequestEvent: {e}", exc_info=True)
        finally:
            # Capture benchmarking metric for executor handling time
            if validation_id and hasattr(self, '_benchmarker') and self._benchmarker and executor_handling_start_time is not None:
                self._benchmarker.capture_metric(
                    "executor.validated_order_handling_time_ms",
                    (time.time() - executor_handling_start_time) * 1000,
                    context={'request_id': order_request_data.request_id, 'validation_id': validation_id}
                )

    # --- Lifecycle Methods ---
    @log_function_call('trade_manager')
    def start(self) -> None:
        """Start the TradeManagerService by subscribing to events."""
        self.logger.info("TradeManagerService starting...")

        # Start IPC worker thread
        self._start_ipc_worker()

        # Subscribe to EventBus events (moved from __init__)
        self._subscribe_to_events()

        self._is_running = True
        self.logger.info("TradeManagerService started.")

    def stop(self) -> None:
        """Stop the TradeManagerService and clean up resources."""
        self.logger.info("TradeManagerService stopping...")
        self._is_running = False
        
        # Stop IPC worker thread
        if hasattr(self, '_ipc_shutdown_event'):
            self._ipc_shutdown_event.set()
        
        # Wait for IPC worker to finish
        if hasattr(self, '_ipc_worker_thread') and self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            self._ipc_worker_thread.join(timeout=5.0)
        
        self.logger.info("TradeManagerService stopped.")

    @log_function_call('trade_manager')
    def initialize(self, poll_interval_s: float = 30.0) -> None:
        """Initialize service state, reconcile with broker, and start background tasks."""
        self.logger.info("TradeManagerService.initialize() called. Checking broker status...")
        if self._broker:
            self.logger.info(f"TMS.initialize: self._broker instance ID={id(self._broker)}, is_connected() -> {self._broker.is_connected()}")
            # If your broker has a more specific "listeners_active" or "handshake_complete" status, log that too.
            if hasattr(self._broker, 'are_listeners_active'):
                self.logger.info(f"TMS.initialize: self._broker listeners_active -> {self._broker.are_listeners_active()}")
        else:
            self.logger.error("TMS.initialize: self._broker IS NONE.")

        self.logger.info("TradeManagerService initializing...")

        # Reset internal state (clears PM, LCM, etc.)
        self._reset_internal_state()

        # --- Add Startup Reconciliation ---
        self.logger.info("Performing startup reconciliation with broker positions...")
        try:
            self.logger.info(f"TMS.perform_startup_reconciliation: self._broker instance ID={id(self._broker) if self._broker else 'None'}, is_connected() -> {self._broker.is_connected() if self._broker else False}")
            if not self._broker or not self._broker.is_connected():
                self.logger.warning("Broker not available or connected, cannot perform startup reconciliation.")
                return

            positions_result, success = self._broker.get_all_positions(timeout=10.0) # Use a reasonable timeout
            if not success:
                self.logger.warning("Failed to retrieve positions from broker during startup reconciliation")
                broker_positions = []
            else:
                positions_data = positions_result  # This is the raw response from get_all_positions

                # More robust type checking for positions_data
                if positions_data is None:
                    self.logger.warning("Received None positions_data from broker during startup reconciliation")
                    broker_positions = []
                elif isinstance(positions_data, list):
                    # Direct list of positions (no 'positions' key)
                    broker_positions = positions_data
                    self.logger.info(f"Received direct list of {len(broker_positions)} positions from broker")
                elif isinstance(positions_data, dict):
                    # Dictionary with 'positions' key
                    broker_positions = positions_data.get("positions", [])
                    self.logger.info(f"Extracted {len(broker_positions)} positions from broker response dictionary")
                else:
                    # Unexpected type
                    self.logger.error(f"Unexpected positions_data type: {type(positions_data)}. Using empty list.")
                    broker_positions = []

                self.logger.info(f"Successfully fetched {len(broker_positions)} positions from broker for startup reconciliation.")
                self.logger.info(f"Found {len(broker_positions)} positions at broker.")

                if broker_positions:
                    for pos_data in broker_positions:
                        symbol = pos_data.get("symbol", "").upper()
                        shares = float(pos_data.get("qty", pos_data.get("shares", 0.0)))
                        avg_price = float(pos_data.get("avg_entry_price", pos_data.get("avg_price", 0.0)))
                        # Attempt to get realized PnL from broker data, default to None if not present
                        realized_pnl_from_broker = pos_data.get("realized_pl", pos_data.get("realized_pnl"))
                        if realized_pnl_from_broker is not None:
                            try:
                                realized_pnl_from_broker = float(realized_pnl_from_broker)
                            except ValueError:
                                self.logger.warning(f"Could not parse realized_pnl '{realized_pnl_from_broker}' for {symbol} at startup. Setting to None.")
                                realized_pnl_from_broker = None

                        if symbol and abs(shares) > 1e-9: # If actual position exists
                            self.logger.info(f"Reconciling existing broker position at startup: {symbol} {shares} @ {avg_price}, RealizedPnL: {realized_pnl_from_broker}")

                            # Get/Create LCM trade and its associated correlation_id
                            lcm_parent_trade_id = self._lifecycle_manager.create_or_sync_trade(symbol, shares, avg_price, source="STARTUP_SYNC_TMS")
                            master_corr_id_for_pm = None
                            if lcm_parent_trade_id:
                                trade_info_from_lcm = self._lifecycle_manager.get_trade_by_id(lcm_parent_trade_id)
                                if trade_info_from_lcm:
                                    master_corr_id_for_pm = trade_info_from_lcm.get("correlation_id")  # Get IntelliSense UUID from LCM

                            self._position_manager.sync_broker_position(
                                symbol, shares, avg_price,
                                broker_realized_pnl=realized_pnl_from_broker,
                                source="startup_sync",
                                lcm_parent_trade_id_from_sync=lcm_parent_trade_id,
                                master_correlation_id_from_sync=master_corr_id_for_pm
                            )

                            # Mark symbol as recently synced
                            with self._sync_grace_lock:
                                self._recently_synced_symbols[symbol] = time.time()
                            self.logger.info(f"[{symbol}] Marked as recently synced by broker. OCR Close signals will be deferred for a grace period.")
                        elif symbol:
                            self.logger.debug(f"Broker position for {symbol} is flat, no sync needed.")

                    # After TMS has processed the positions for its own needs,
                    # explicitly tell OrderRepository to sync.
                    if self._order_repository:
                        try:
                            # Pass the raw positions data to OrderRepository for initial sync
                            self.logger.info("TMS: Triggering OrderRepository initial position sync.")
                            self._order_repository.perform_initial_broker_position_sync(positions_data)

                            # 🚨 NEW: Also sync existing broker orders into OrderRepository
                            self.logger.info("TMS: Triggering OrderRepository initial broker order sync.")
                            self._order_repository.perform_initial_broker_order_sync()
                        except Exception as e_or_sync:
                            self.logger.error(f"TMS: Error triggering OrderRepository sync: {e_or_sync}", exc_info=True)
                else:
                    self.logger.info("No positions reported by broker.")
        except Exception as e_recon:
            self.logger.error(f"Error during startup reconciliation: {e_recon}", exc_info=True)
            # Decide if you want to continue or halt initialization on error
            # For now, just log the error and continue
        self.logger.info("Startup reconciliation finished.")
        # --- End Startup Reconciliation ---

        # Start broker polling thread if interval is valid
        if poll_interval_s > 0:
            self._start_broker_poll_thread(poll_interval_s)
        else:
            self.logger.info("Broker polling disabled (interval <= 0).")



        # Register for LCM trade opened/closed events
        if self._lifecycle_manager:
            if hasattr(self._lifecycle_manager, 'register_trade_opened_callback'):
                self._lifecycle_manager.register_trade_opened_callback(self._handle_lcm_trade_opened)
                self.logger.info("TMS registered callback with LCM for trade_opened events.")
            if hasattr(self._lifecycle_manager, 'register_trade_closed_callback'):
                self._lifecycle_manager.register_trade_closed_callback(self._handle_lcm_trade_closed)
                self.logger.info("TMS registered callback with LCM for trade_closed events.")

        # Risk management check
        if self._risk_service:
            self.logger.info("Risk management service is available for risk assessment.")

            # Register for critical risk events from RiskManagementService
            if hasattr(self._risk_service, 'register_critical_risk_callback'):
                try:
                    self._risk_service.register_critical_risk_callback(self._handle_critical_risk_event)
                    self.logger.info("TMS registered callback with RiskManagementService for critical risk events.")
                except Exception as e_cb_reg:
                    self.logger.error(f"TMS failed to register critical risk callback: {e_cb_reg}", exc_info=True)
            else:
                self.logger.warning("TMS: RiskService available but does not have 'register_critical_risk_callback' method.")
        else:
            self.logger.warning("No risk management service available - risk assessment will be limited.")

        # Prime _last_ocr_state with current position data
        self.logger.debug("Priming _last_ocr_state with position data...")
        positions_snapshot = self._position_manager.get_all_positions_snapshot()

        with self._ocr_state_lock:
            for symbol, pos_val in positions_snapshot.items():
                total_avg_price = pos_val.get("total_avg_price", 0.0)
                overall_realized_pnl = pos_val.get("overall_realized_pnl", 0.0)

                self._last_ocr_state[symbol] = {
                    "cached_cost": total_avg_price,
                    "cached_realized": overall_realized_pnl
                }

        # Start risk monitoring for any active positions via RMS directly
        if self._risk_service and hasattr(self._risk_service, 'start_monitoring_holding'):
            for symbol, pos_val in positions_snapshot.items():
                if pos_val.get("open", False) and abs(pos_val.get("shares", 0)) > 0:
                    try:
                        self._risk_service.start_monitoring_holding(symbol)
                        self.logger.info(f"Started risk monitoring for existing active position: {symbol}")
                    except Exception as e:
                        self.logger.error(f"Error starting risk monitoring for {symbol}: {e}", exc_info=True)

        self.logger.info("TradeManagerService initialization complete.")

        # Signal that initialization is complete
        if self._initialization_complete_event:
            self.logger.info("Setting initialization_complete_event to signal OCR thread can proceed")
            self._initialization_complete_event.set()
            self.logger.info("Initialization complete event has been set.")

    @log_function_call('trade_manager')
    def shutdown(self) -> None:
        """
        Stop background tasks and clean up.
        Ensures all threads are properly stopped and resources released.
        """
        self.logger.info("TradeManagerService shutting down...")

        # Stop broker polling thread
        if self._poll_thread or self._poll_stop_event:
            self.logger.debug("Stopping broker poll thread...")
            self._stop_broker_poll_thread()

        # Unregister LCM trade opened/closed event callbacks
        if self._lifecycle_manager:
            if hasattr(self._lifecycle_manager, 'unregister_trade_opened_callback'):
                try:
                    self._lifecycle_manager.unregister_trade_opened_callback(self._handle_lcm_trade_opened)
                except Exception as e: self.logger.error(f"TMS error unregistering LCM trade_opened_callback: {e}")
            if hasattr(self._lifecycle_manager, 'unregister_trade_closed_callback'):
                try:
                    self._lifecycle_manager.unregister_trade_closed_callback(self._handle_lcm_trade_closed)
                except Exception as e: self.logger.error(f"TMS error unregistering LCM trade_closed_callback: {e}")

        # Cancel any pending orders if needed
        # This is optional and depends on your requirements
        # if self._order_repository:
        #     try:
        #         self._order_repository.cancel_all_pending_orders("Service Shutdown")
        #     except Exception as e:
        #         self.logger.error(f"Error canceling pending orders during shutdown: {e}")

        # Release any other resources
        # ...

        self.logger.info("TradeManagerService shutdown complete.")

    @log_function_call('trade_manager')
    def _reset_internal_state(self) -> None:
        """Clears internal state by resetting sub-components."""
        # Reset position manager
        self._position_manager.reset_all_positions()
        # Reset trade lifecycle manager (trades, ledger)
        self._lifecycle_manager.reset_all_trades()
        # Reset local state if any remains (e.g., GUI summary)
        with self._single_gui_position_lock:
            self._single_gui_position = {"open": False}
        with self._ocr_state_lock:
             self._last_ocr_state.clear()
        # Order cache clearing removed - direct repository access is used instead
        # Ledger clearing removed - lifecycle manager handles ledger now
        # Reset fingerprint cache
        with self._fingerprint_lock:
            self._recent_fingerprints.clear()
            self._fingerprint_timestamps.clear()

        # Reset suppressed open attempts
        with self._suppressed_open_attempts_lock:
            self._suppressed_open_attempts.clear()
        # Stop risk monitoring for all symbols
        if self._risk_service and hasattr(self._risk_service, 'stop_monitoring_holding'):
            # Get all symbols that might be monitored
            all_symbols = list(self._position_manager.get_all_positions_snapshot().keys())
            for symbol in all_symbols:
                try:
                    self._risk_service.stop_monitoring_holding(symbol)
                    self.logger.debug(f"Stopped risk monitoring for {symbol} during reset")
                except Exception as e:
                    self.logger.error(f"Error stopping risk monitoring for {symbol} during reset: {e}")

        # Reset risk service if available and it has a reset_all method
        if self._risk_service and hasattr(self._risk_service, 'reset_all'):
            self._risk_service.reset_all()
        elif self._risk_service:
            self.logger.debug("Risk service doesn't have reset_all method - skipping risk service reset")

        # NOTE: MasterActionFilter state is now managed by MasterActionFilterService
        # No longer reset here as OCR scalping flow uses the dedicated service

        # Clear post-meltdown hold list
        with self._post_meltdown_hold_lock:
            self._symbols_on_post_meltdown_hold.clear()
        self.logger.info("Cleared symbol post-meltdown hold states.")

        # Clear recently synced symbols
        with self._sync_grace_lock:
            self._recently_synced_symbols.clear()
        self.logger.info("Cleared recently synced symbols list.")

        self.logger.info("Internal state reset (delegated to sub-managers).")

    # --- Configuration Methods --- (set_debug_mode remains same)
    # Store OCR filter as a class variable to ensure we use the same instance
    _ocr_filter_instance = None

    @log_function_call('trade_manager')
    def set_debug_mode(self, flag: bool) -> None:
        """
        Sets the internal debug flag for the service and applies/removes the OCR filter
        from this logger's handlers.
        This function NO LONGER changes logger/handler levels or adds handlers,
        allowing inheritance from the root configuration in main.py.

        Args:
            flag (bool): True to enable debug mode (removes OCR filter),
                         False to disable debug mode (adds OCR filter).
        """
        self.logger.info(f"Setting internal debug mode flag to: {flag}")
        self._debug_mode = flag

        # --- Apply/Remove OCR Filter Logic ---
        try:
            from utils.logging_filter import OCRFilter

            # Create the OCRFilter instance only once as a class variable
            if TradeManagerService._ocr_filter_instance is None:
                TradeManagerService._ocr_filter_instance = OCRFilter()
                self.logger.debug("Created shared OCRFilter instance")

            handlers_to_check = list(self.logger.handlers) # Copy list for safe iteration
            self.logger.debug(f"Checking {len(handlers_to_check)} handlers for OCR filter...")

            for handler in handlers_to_check:
                current_filters = list(handler.filters)
                filter_removed = False

                # First remove any existing OCRFilter instances
                for f in current_filters:
                    if isinstance(f, OCRFilter):
                        try:
                            handler.removeFilter(f)
                            self.logger.debug(f"Removed existing OCRFilter from handler {handler.name if hasattr(handler, 'name') else handler}")
                            filter_removed = True
                        except Exception as e_remove:
                            self.logger.error(f"Error removing filter {f} from handler {handler}: {e_remove}")

                # Then add our shared instance if debug mode is OFF
                if not flag:
                    try:
                        handler.addFilter(TradeManagerService._ocr_filter_instance)
                        self.logger.debug(f"Added shared OCRFilter to handler {handler.name if hasattr(handler, 'name') else handler} (debug mode OFF).")
                    except Exception as e_add:
                        self.logger.error(f"Error adding filter to handler {handler}: {e_add}")

            if not flag:
                 self.logger.info("OCR filter applied/ensured for this logger's handlers (debug mode OFF).")
            else:
                 self.logger.info("OCR filter removed/ensured removed for this logger's handlers (debug mode ON).")

        except ImportError:
            self.logger.warning("OCR filter module (utils/logging_filter.py) not found - OCR-related logs will not be filtered.")
        except Exception as e_filter:
             self.logger.error(f"General error applying/removing OCR filter: {e_filter}", exc_info=True)

        self.logger.info(f"TradeManagerService internal debug flag is now: {'ENABLED' if self._debug_mode else 'DISABLED'}")

        if flag:
            self.logger.debug("Debug logging test message - this should appear if main log level is DEBUG")
        else:
            self.logger.info("Debug mode OFF - Debug test message should NOT appear unless main log level is DEBUG")


    @log_function_call('trade_manager')
    def _debug_print(self, msg: str) -> None:
        """Prints a debug message if debug mode is enabled."""
        if self._debug_mode:
            self.logger.debug(f"[DEBUG_TM_SERVICE] {msg}")

    # --- Price Helper --- (Removed _get_reliable_price method, now using IPriceProvider.get_reliable_price)

    # --- Trading Enable/Disable --- (set_trading_enabled, is_trading_enabled remain same)
    @log_function_call('trade_manager')
    def set_trading_enabled(self, enabled: bool):
        """Explicitly enables or disables automated trading."""
        with self._trading_enabled_lock:
            if self._is_trading_enabled != enabled:
                self._is_trading_enabled = enabled
                self.logger.warning(f"Trading explicitly set to ENABLED: {enabled}")

                # Publish trading state change via correlation logger
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeManagerService",
                        event_name="TradingStateChange",
                        payload={
                            "state_type": "TRADING_ENABLED",
                            "enabled": enabled,
                            "changed_by": "user_action",
                            "previous_state": not enabled,
                            "timestamp": time.time()
                        }
                    )

                if enabled: # If trading is being RE-ENABLED by user
                    with self._post_meltdown_hold_lock: # Use the correct lock
                        if self._symbols_on_post_meltdown_hold: # If the set is not empty
                            self.logger.info("Trading re-enabled by user: Clearing ALL symbol post-meltdown holds.")
                            self._symbols_on_post_meltdown_hold.clear()
            else:
                self.logger.info(f"Trading Enabled already set to: {enabled}")

    @log_function_call('trade_manager')
    def is_trading_enabled(self) -> bool:
        """Checks if automated trading based on OCR is currently enabled."""
        with self._trading_enabled_lock:
            return self._is_trading_enabled

    @log_function_call('trade_manager')
    def set_broker_service(self, broker_service: 'IBrokerService') -> None:
        """
        Sets the broker service instance for this TradeManagerService.

        Args:
            broker_service: The broker service instance
        """
        self._broker = broker_service
        self.logger.info(f"Broker service set in TradeManagerService: ID={id(self._broker) if self._broker else 'None'}")

    # --- BEGIN MODIFICATION 1.3: Add set_core_logic_manager method ---
    @log_function_call('trade_manager')
    def set_core_logic_manager(self, clm_ref) -> None:
        """
        Sets the reference to the CoreLogicManager instance.
        This allows the service to schedule GUI updates.

        Args:
            clm_ref: The CoreLogicManager instance
        """
        self.logger.info(f"TradeManagerService: CoreLogicManager reference set (ID: {id(clm_ref) if clm_ref else 'None'}).")
        self._core_logic_manager_ref = clm_ref
        # Optionally, pass it down to sub-components if they need to schedule GUI updates directly:
        # if self._position_manager and hasattr(self._position_manager, 'set_core_logic_manager'):
        #     self._position_manager.set_core_logic_manager(clm_ref)
        # if self._lifecycle_manager and hasattr(self._lifecycle_manager, 'set_core_logic_manager'):
        #     self._lifecycle_manager.set_core_logic_manager(clm_ref)
    # --- END MODIFICATION 1.3 ---

    @log_function_call('trade_manager')
    def set_ocr_service_dependency(self, ocr_service_instance) -> None:
        """
        Sets the OCR service instance for this TradeManagerService.
        This allows the TMS to control OCR processing during emergency events.

        Args:
            ocr_service_instance: The OCR service instance
        """
        self._ocr_service = ocr_service_instance
        self.logger.info(f"OCR service dependency set in TradeManagerService: {ocr_service_instance is not None}")
        if self._ocr_service is None:
            self.logger.warning("OCR service dependency is None - emergency stop OCR functionality will be limited")

    # --- Risk Management Callback ---
    @log_function_call('trade_manager')
    def _handle_critical_risk_event(self, symbol: str, level: RiskLevel, details: Dict[str, Any]):
        """
        Automated emergency liquidation triggered by critical risk detection.

        This method is a callback invoked by the RiskManagementService when it detects
        a critical risk condition for a monitored holding (e.g., market meltdown, extreme
        volatility, or other risk factors).

        This method follows the standardized emergency sequence:
        1. Checks global kill switch status (early exit if active)
        2. Stops OCR processing to prevent new signals
        3. Halts all automated trading to prevent new orders
        4. Adds symbol to post-meltdown hold to block new OPEN signals
        5. Verifies position existence and size
        6. Initiates aggressive liquidation with market chase
        7. Notifies GUI via emergency callback (purely informational)

        Unlike manual force close methods, this is triggered automatically by the system
        and focuses on a single symbol that triggered the risk event.

        Args:
            symbol: The symbol with critical risk
            level: The severity level of the risk event
            details: Additional information about the risk event
        """
        symbol_upper = symbol.upper()
        reason_details_str = str(details.get('factors_summary', details.get('reason', 'UnknownFactor')))

        self.logger.critical(
            f"TMS: CRITICAL RISK EVENT for {symbol_upper}! Level: {level.name}. Details: {reason_details_str}. "
            "ACTION: Halting OCR, Halting Trading, Initiating Aggressive Liquidation for symbol."
        )

        # 0. Global Kill Switch Check (should be very early)
        if get_global_kill_switch():
             self.logger.error(f"[{symbol_upper}] Cannot process critical risk: Global kill switch active. No further action taken by TMS.")
             return # Absolute stop

        # 1. Stop OCR Processing (immediately)
        # This stops new screen captures and snapshot generation.
        if self._ocr_service:
            try:
                if self._ocr_service.get_status().get("ocr_active"):
                    self.logger.info(f"TMS: Stopping OCR service due to critical risk for {symbol_upper}.")
                    self._ocr_service.stop_ocr()
                    # The on_status_update callback from OCRService will update the GUI OCR button
                else:
                    self.logger.info(f"TMS: OCR service already stopped while handling critical risk for {symbol_upper}.")
            except Exception as e_ocr_stop:
                self.logger.error(f"TMS: Error stopping OCR service for {symbol_upper}: {e_ocr_stop}", exc_info=True)
        else:
            self.logger.warning(f"TMS: OCRService not available, cannot stop OCR for {symbol_upper}.")

        # 2. Halt Automated Trading (globally and immediately)
        # This prevents any in-flight OCR snapshot processing from leading to an order.
        self.logger.warning(f"TMS: Halting ALL automated OCR-driven trading due to critical risk for {symbol_upper}.")
        self.set_trading_enabled(False) # This also updates GUI button via its logic

        # 3. SET SYMBOL-SPECIFIC POST-MELTDOWN HOLD
        with self._post_meltdown_hold_lock:
            self._symbols_on_post_meltdown_hold.add(symbol_upper)
        self.logger.warning(f"[{symbol_upper}] Placed on 'post-meltdown hold'. New OPEN signals will be blocked until trading is manually resumed.")

        # 4. Verify we still hold an open position in PositionManager
        pos_data = self._position_manager.get_position(symbol_upper)
        if not (pos_data and pos_data.get("open", False) and abs(pos_data.get("shares", 0.0)) > 1e-9):
            self.logger.warning(
                f"TMS: Critical risk event for {symbol_upper}, but PositionManager shows no open position. "
                f"Current PM state: {pos_data}. No liquidation action needed by TMS."
            )
            # Still important to stop RMS monitoring if it was active for this symbol
            if self._risk_service and hasattr(self._risk_service, 'stop_monitoring_holding'):
                self.logger.info(f"TMS telling RMS to stop monitoring {symbol_upper} as no open position found.")
                self._risk_service.stop_monitoring_holding(symbol_upper)
            return

        # 5. Initiate Aggressive Liquidation for the specific symbol
        liquidation_reason = f"CRITICAL_RISK_{level.name}_{reason_details_str.replace(' ', '_')}"[:100]
        self.logger.info(f"TMS: Calling _initiate_aggressive_liquidation_for_symbol for {symbol_upper}. Reason: {liquidation_reason}")

        liquidation_initiated = self._initiate_aggressive_liquidation_for_symbol( # Uses the consolidated method
            symbol=symbol_upper,
            reason_detail=liquidation_reason, # This will be part of the event_type/comment
            aggressive_chase=True
        )

        if liquidation_initiated:
            self.logger.info(f"TMS: Aggressive liquidation process INITIATED for {symbol_upper} due to critical risk.")
        else:
            self.logger.error(f"TMS: FAILED to initiate aggressive liquidation for {symbol_upper} despite critical risk event.")
            # At this point, trading and OCR are already stopped. Manual intervention might be needed.

        # 6. Notify GUI via emergency callback (if one is set) - purely informational
        if self._emergency_force_close_callback:
            try:
                self.logger.info(f"TMS: Invoking GUI emergency_force_close_callback for {symbol_upper} (informational only).")
                self._emergency_force_close_callback(symbol_upper, f"CRITICAL RISK ({level.name}): {reason_details_str}")
            except Exception as e_gui_cb:
                self.logger.error(f"TMS: Error in GUI emergency_force_close_callback: {e_gui_cb}")

    # --- Liquidation Logic --- (_liquidate_position uses Executor now)
    # --- Force Close --- (force_close_all uses _initiate_aggressive_liquidation_for_symbol, force_close_and_halt_trading calls force_close_all)
    @log_function_call('trade_manager')
    def force_close_all(self, reason: str = "Manual GUI Request") -> bool:
        """
        Core implementation to close all positions and disable trading.

        This method is the shared implementation used by both force_close_and_halt_trading
        and force_close_all_and_halt_system. It handles the actual position liquidation logic
        but does NOT stop OCR processing - that's the responsibility of the calling method.

        This method:
        1. Disables trading to prevent new orders
        2. Gets all open positions from PositionManager
        3. Calls _initiate_aggressive_liquidation_for_symbol for each position

        Typically not called directly, but through:
        - force_close_all_and_halt_system (preferred method that also stops OCR)
        - force_close_and_halt_trading (legacy method that doesn't stop OCR)
        - force_close_action_from_service (callback for automated risk events)

        Args:
            reason: The reason for the force close

        Returns:
            bool: True if liquidation was successfully initiated for all positions
        """
        self.logger.warning(f"FORCE CLOSE ALL initiated! Reason: {reason}")
        self.set_trading_enabled(False) # Halt trading first (already uses the lock)

        overall_initiation_success = True

        # Get symbols with non-zero shares from the position manager OR broker? Let's use PM state.
        try:
            positions_snapshot = self._position_manager.get_all_positions_snapshot()
            symbols_to_close = [sym for sym, data in positions_snapshot.items()
                                if data.get("open", False) and abs(data.get('shares', 0.0)) > 1e-9]

            self.logger.info(f"Found {len(symbols_to_close)} open positions in PositionManager to liquidate: {symbols_to_close}")
            if not symbols_to_close:
                 self.logger.info("No open positions found locally to liquidate.")
                 return True # Nothing to do

        except Exception as e:
            self.logger.exception("Error accessing positions from position manager for force_close_all")
            return False

        # Attempt liquidation for each symbol
        for symbol in symbols_to_close:
            self.logger.info(f"Initiating liquidation for {symbol} as part of Force Close All.")
            try:
                # Use the consolidated method for aggressive liquidation
                initiated = self._initiate_aggressive_liquidation_for_symbol(
                    symbol=symbol.upper(),
                    reason_detail=f"{reason} (All)",
                    aggressive_chase=True
                )

                if not initiated:
                     overall_initiation_success = False # Mark failure if any liquidation fails to initiate

                # Risk monitoring is now handled by LCM callbacks
            except Exception as e:
                self.logger.exception(f"Error initiating aggressive liquidation for {symbol} during force_close_all: {e}")
                overall_initiation_success = False

        if not overall_initiation_success:
            self.logger.error("Force Close All: Encountered errors trying to initiate liquidation for one or more symbols.")
        else:
            self.logger.info("Force Close All: Liquidation process initiated for relevant positions.")

        return overall_initiation_success # Return success based on initiation attempt

    @log_function_call('trade_manager')
    def force_close_and_halt_trading(self, reason: str = "Manual Button") -> bool:
        """
        Legacy method to close all positions and halt trading (but not OCR).

        WARNING: This method is maintained for backward compatibility only.
        It disables trading and attempts to flatten all positions, but unlike
        force_close_all_and_halt_system, it does NOT stop OCR processing.

        This method:
        1. Disables trading to prevent new orders
        2. Calls force_close_all to liquidate all positions
        3. Does NOT stop OCR processing (key limitation)

        Typically triggered by:
        - Legacy code or interfaces

        This method differs from:
        - force_close_all_and_halt_system: Preferred method that also stops OCR
        - force_close_single_symbol_and_halt_system: Only closes one position but stops OCR
        - force_close_position: Only closes one position without halting anything

        For new code, use force_close_all_and_halt_system instead, which provides
        a more comprehensive emergency shutdown by also stopping OCR.

        Args:
            reason: The reason for the force close

        Returns:
            bool: True if liquidation was successfully initiated for all positions
        """
        self.logger.warning(f"Force close and halt trading initiated. Reason: {reason}")
        self.set_trading_enabled(False)  # Already uses the lock
        return self.force_close_all(reason=reason)

    @log_function_call('trade_manager')
    def force_close_all_and_halt_system(self, reason: str = "Manual Emergency Close") -> bool:
        """
        Comprehensive emergency shutdown that closes ALL positions and halts the entire system.

        This is the primary method for manual emergency situations requiring a complete system halt.
        It provides the most thorough shutdown sequence by stopping all automated activity and
        liquidating all positions.

        This method follows the standardized emergency sequence:
        1. Stops OCR processing to prevent new signals
        2. Disables trading to prevent new orders
        3. Aggressively liquidates ALL open positions with market chase

        Typically triggered by:
        - GUI "Force Close" button (via App.force_close_action)
        - Manual emergency intervention

        This method differs from:
        - force_close_and_halt_trading: Legacy method that doesn't stop OCR
        - force_close_single_symbol_and_halt_system: Only closes one position
        - force_close_position: Only closes one position without halting the system
        - _handle_critical_risk_event: Automated response to a single symbol's risk event

        Args:
            reason: The reason for the emergency close

        Returns:
            bool: True if liquidation was successfully initiated for all positions
        """
        self.logger.warning(f"EMERGENCY CLOSE ALL & HALT SYSTEM! Reason: {reason}")

        # 1. Stop OCR Processing (immediately)
        if self._ocr_service:
            try:
                if self._ocr_service.get_status().get("ocr_active"):
                    self.logger.info(f"Stopping OCR service due to emergency close all.")
                    self._ocr_service.stop_ocr()
                    # The on_status_update callback from OCRService will update the GUI OCR button
                else:
                    self.logger.info(f"OCR service already stopped while handling emergency close all.")
            except Exception as e_ocr_stop:
                self.logger.error(f"Error stopping OCR service: {e_ocr_stop}", exc_info=True)
        else:
            self.logger.warning(f"OCRService not available, cannot stop OCR.")

        # 2. Halt Automated Trading (globally and immediately)
        self.logger.warning(f"Halting ALL automated OCR-driven trading.")
        self.set_trading_enabled(False)  # This also updates GUI button via its logic

        # 3. Close all positions (force_close_all already calls set_trading_enabled(False) internally)
        return self.force_close_all(reason=f"Emergency Close All: {reason}")


    # --- Lifecycle Manager Callbacks ---
    @log_function_call('trade_manager')
    def _handle_lcm_trade_opened(self, trade_id: int, symbol: str):
        """Callback invoked by TradeLifecycleManager when a trade is confirmed open."""
        self.logger.info(f"TMS: Received trade_opened event from LCM for trade_id={trade_id}, symbol={symbol}.")

        # Update risk service with entry time and start monitoring
        if self._risk_service:
            # Record entry time for risk_min_hold_seconds functionality
            if hasattr(self._risk_service, '_update_entry_time'):
                entry_timestamp = time.time()
                self._risk_service._update_entry_time(symbol, entry_timestamp)
                self.logger.info(f"TMS: Updated RMS entry time for {symbol} at {entry_timestamp}")

            # Start monitoring holding for risk assessment
            if hasattr(self._risk_service, 'start_monitoring_holding'):
                self.logger.info(f"TMS: Requesting RMS to START monitoring holding for newly opened trade: {symbol}")
                self._risk_service.start_monitoring_holding(symbol)

        # Any other actions TMS needs to take when a trade is confirmed open.

        # Notify GUI or other components if needed
        if self._gui_logger_func:
            self._gui_logger_func(f"Trade opened: {symbol} (ID: {trade_id})", "info")

    @log_function_call('trade_manager')
    def _handle_lcm_trade_closed(self, trade_id: int, symbol: str):
        """Callback invoked by TradeLifecycleManager when a trade is confirmed closed."""
        self.logger.info(f"TMS: Received trade_closed event from LCM for trade_id={trade_id}, symbol={symbol}.")
        if self._risk_service and hasattr(self._risk_service, 'stop_monitoring_holding'):
            self.logger.info(f"TMS: Requesting RMS to STOP monitoring holding for newly closed trade: {symbol}")
            self._risk_service.stop_monitoring_holding(symbol)

        # Also clear any suppressed open attempts for this symbol as the trade lifecycle is now complete/closed.
        with self._suppressed_open_attempts_lock:
            if symbol.upper() in self._suppressed_open_attempts:
                del self._suppressed_open_attempts[symbol.upper()]
                self.logger.info(f"TMS: Cleared suppressed open attempt for {symbol} as its trade (ID {trade_id}) is now closed.")

        # Notify GUI or other components if needed
        if self._gui_logger_func:
            self._gui_logger_func(f"Trade closed: {symbol} (ID: {trade_id})", "info")

        # Update the single GUI position display
        with self._single_gui_position_lock:
            if self._single_gui_position.get("symbol") == symbol:
                self._single_gui_position["open"] = False
                self.logger.debug(f"Updated single_gui_position to closed for {symbol}")

    # --- Emergency Close --- (set_emergency_force_close_callback for GUI notification)
    @log_function_call('trade_manager')
    def set_emergency_force_close_callback(self, func: Callable[[str, str], None]) -> None:
        """Sets the callback function to execute on meltdown detection."""
        self._emergency_force_close_callback = func
        self.logger.info("Emergency force close callback set.")

    @log_function_call('trade_manager')
    def _initiate_aggressive_liquidation_for_symbol(self, symbol: str, reason_detail: str, aggressive_chase: bool = True) -> bool:
        """
        Core internal helper to aggressively liquidate a position for a specific symbol.

        This is the central liquidation implementation used by all higher-level liquidation methods.
        It handles the actual order creation and submission logic, but does NOT handle system-level
        actions like halting OCR or disabling trading - those are the responsibility of the calling methods.

        This method:
        1. Retrieves current position information from the BrokerBridge (primary) or PositionManager (fallback)
        2. Generates a TradeSignal with CLOSE_POSITION action
        3. Calculates OrderParameters via OrderStrategy with aggressive chase hints
        4. Configures chase parameters for TradeExecutor if aggressive_chase=True
        5. Calls TradeExecutor.execute_trade to submit the order

        Typically triggered by:
        - _handle_critical_risk_event (automated risk detection)
        - force_close_single_symbol_and_halt_system (manual emergency close)
        - force_close_all/force_close_all_and_halt_system (manual force close all)
        - force_close_position (legacy manual close without system halt)

        Args:
            symbol: The symbol to liquidate (should already be uppercase)
            reason_detail: Specific reason details for logging and tracking
            aggressive_chase: If True, indicates this is an aggressive liquidation that should
                             chase the market if necessary (e.g., for critical risk events).
                             Defaults to True for backward compatibility.

        Returns:
            bool: True if liquidation was successfully initiated, False otherwise
        """
        symbol_upper = symbol.upper()  # Ensure consistency
        chase_type = "AGGRESSIVE" if aggressive_chase else "STANDARD"
        self.logger.warning(f"[{symbol_upper}] Initiating {chase_type} LIQUIDATION. Reason: {reason_detail}")

        local_id: Optional[int] = None
        # Only create timestamp dictionary if performance tracking is enabled
        perf_ts = create_timestamp_dict() if is_performance_tracking_enabled() else None

        # Only add timestamps if performance tracking is enabled and perf_ts exists
        if is_performance_tracking_enabled() and perf_ts:
            add_timestamp(perf_ts, f'tm_service.{symbol_upper}.liquidate_start')

        try:
            # --- Refined Quantity Determination Logic ---
            actual_broker_shares: Optional[float] = None
            source_of_qty: str = "Unknown"

            # --- PRIMARY: Get live position directly from BrokerBridge ---
            if self._broker and self._broker.is_connected():
                self.logger.info(f"[{symbol_upper}] Attempting to fetch LIVE broker position for liquidation quantity determination.")
                try:
                    # Using get_all_positions to get fresh broker data
                    positions_result, success = self._broker.get_all_positions(
                        timeout=getattr(self._config, 'broker_get_positions_timeout_sec', 5.0)
                    )

                    if success and positions_result is not None:
                        position_data_from_broker = next((p for p in positions_result if p.get("symbol", "").upper() == symbol_upper), None)
                        if position_data_from_broker:
                            # Try to get shares or qty field from broker data
                            actual_broker_shares = float(position_data_from_broker.get('shares', position_data_from_broker.get('qty', 0.0)))
                            source_of_qty = "LiveBrokerCall_get_all_positions"
                            self.logger.info(f"[{symbol_upper}] {source_of_qty}: {actual_broker_shares} shares.")
                        else:  # Symbol not in broker's list of positions
                            actual_broker_shares = 0.0  # Assume flat
                            source_of_qty = "LiveBrokerCall_get_all_positions (Symbol NOT found)"
                            self.logger.info(f"[{symbol_upper}] {source_of_qty}: {actual_broker_shares} shares.")
                    else:
                        self.logger.warning(f"[{symbol_upper}] Failed to get valid live broker position from get_all_positions (success={success}, result_is_None={positions_result is None}). Falling back...")
                        # actual_broker_shares remains None
                except Exception as e_broker_call:
                    self.logger.error(f"[{symbol_upper}] Exception fetching live broker position: {e_broker_call}. Falling back...", exc_info=True)
                    # actual_broker_shares remains None
            else:
                self.logger.warning(f"[{symbol_upper}] Broker not available or connected. Cannot fetch live position. Falling back...")
                # actual_broker_shares remains None

            # --- FALLBACK 1: Position Manager's view ---
            if actual_broker_shares is None:
                self.logger.warning(f"[{symbol_upper}] FALLBACK 1: Using PositionManager's view for liquidation quantity.")
                pm_pos_data = self._position_manager.get_position(symbol_upper)
                if pm_pos_data and pm_pos_data.get("open", False):  # Check if PM considers it open
                    actual_broker_shares = pm_pos_data.get("total_shares", pm_pos_data.get("shares", 0.0))
                    source_of_qty = "PositionManager_Fallback"
                    self.logger.info(f"[{symbol_upper}] {source_of_qty}: {actual_broker_shares} shares.")
                else:  # PM shows no open position or no data
                    actual_broker_shares = 0.0
                    source_of_qty = "PositionManager_Fallback (No open position or no data)"
                    self.logger.info(f"[{symbol_upper}] {source_of_qty}: {actual_broker_shares} shares.")

            # --- FALLBACK 2: OrderRepository's fill-calculated view (Optional) ---
            # Uncomment if needed as an additional fallback
            # if actual_broker_shares is None:
            #     self.logger.warning(f"[{symbol_upper}] FALLBACK 2: Using OrderRepository's fill-calculated view.")
            #     actual_broker_shares = self._order_repository.get_fill_calculated_position(symbol_upper)
            #     source_of_qty = "OrderRepository_FillCalculated_Fallback"
            #     self.logger.info(f"[{symbol_upper}] {source_of_qty}: {actual_broker_shares} shares.")

            # --- Final Quantity Check ---
            current_shares = actual_broker_shares  # This is the signed amount

            self.logger.warning(
                f"[{symbol_upper}] Determined FINAL quantity for liquidation: {current_shares:.2f} shares. "
                f"Source: {source_of_qty}. Reason: {reason_detail}"
            )

            if abs(current_shares) < 1e-9:  # Using a small epsilon for float comparison
                self.logger.warning(f"[{symbol_upper}] Determined quantity to liquidate is effectively zero ({current_shares:.2f}). No liquidation action needed by TradeExecutor.")
                add_timestamp(perf_ts, f'tm_service.{symbol_upper}.liquidate_too_small_or_flat')
                return True  # Return True indicating "nothing to do, considered success"

            side_to_liquidate = "SELL" if current_shares > 0 else "BUY"
            qty_to_liquidate = abs(current_shares)  # This is the definitive quantity

            # 2. Prepare TradeSignal for OrderStrategy
            trigger_details = {
                "reason": f"LIQUIDATION: {reason_detail}",
                "current_shares_for_strategy": current_shares  # Pass current_shares to strategy if it needs it
            }
            executor_extra_fields = {}  # For TradeExecutor

            if aggressive_chase:
                self.logger.info(f"[{symbol}] Aggressive chase liquidation triggered for reason: {reason_detail}")
                trigger_details["aggressive_chase"] = True  # Hint for OrderStrategy

                # Parameters for TradeExecutor's chase logic
                executor_extra_fields["aggressive_chase_parameters"] = {
                    "max_attempts": getattr(self._config, 'meltdown_exit_chase_attempts', 3),
                    "attempt_delay_ms": getattr(self._config, 'meltdown_exit_chase_delay_ms', 300),
                    "reprice_aggression_cents": getattr(self._config, 'meltdown_exit_chase_aggression_cents', 0.02)
                }
                # OrderStrategy might use this to set a more aggressive initial price or MKT order
                # Or, TradeExecutor might directly use these params to manage its chase loop.

            close_signal = TradeSignal(
                action=TradeActionType.CLOSE_POSITION,
                symbol=symbol,
                triggering_snapshot=trigger_details  # Pass details including aggressive_chase hint
            )

            # 3. Calculate Order Parameters using Strategy
            self.logger.debug(f"[{symbol}] Calling OrderStrategy for {chase_type} LIQUIDATION close signal...")
            order_params = self._order_strategy.calculate_order_params(
                signal=close_signal,  # Signal now carries the hint in triggering_snapshot
                position_manager=self._position_manager,  # Strategy might still use this for other context
                price_provider=self._price_provider,
                config_service=self._config
                # Note: OrderStrategy should primarily determine price/type for a close,
                # not quantity for a full liquidation.
            )

            if not order_params:
                self.logger.error(f"[{symbol}] Liquidation failed: OrderStrategy could not generate parameters for {reason_detail}.")
                add_timestamp(perf_ts, f'tm_service.{symbol}.liquidate_err_strategy')
                return False

            # !!! CRITICAL FIX: Override quantity from strategy with actual shares to liquidate !!!
            order_params.quantity = qty_to_liquidate
            order_params.side = side_to_liquidate  # Ensure side is correct too
            order_params.event_type = f"LIQUIDATE_CHASE_{reason_detail[:20].replace(' ', '_')}"  # Ensure clear event type

            # 4. Link to existing trade if possible, or create one if needed
            parent_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol_upper)

            if parent_trade_id is None:
                # No active trade found for this symbol, which is unusual for liquidation
                # but could happen if syncing a position at startup that needs immediate liquidation
                self.logger.warning(f"[{symbol_upper}] No active trade found for liquidation. Creating one.")
                parent_trade_id = self._lifecycle_manager.create_new_trade(symbol_upper)
                if parent_trade_id:
                    self.logger.info(f"[{symbol_upper}] Created new trade ID {parent_trade_id} for liquidation.")
                    # Mark the trade as open/active since we're liquidating an existing position
                    self._lifecycle_manager.update_trade_state(
                        trade_id=parent_trade_id,
                        new_state=LifecycleState.OPEN_ACTIVE,
                        comment=f"Auto-created for liquidation: {reason_detail}"
                    )
                else:
                    self.logger.error(f"[{symbol_upper}] Failed to create new trade for liquidation.")

            self.logger.info(f"[{symbol_upper}] Using parent_trade_id={parent_trade_id} for liquidation.")
            order_params.parent_trade_id = parent_trade_id

            # 5. Execute via TradeExecutor
            self.logger.info(f"[{symbol_upper}] Calling TradeExecutor for {order_params.event_type}...")
            local_id = self._trade_executor.execute_trade(
                order_params=order_params,
                is_manual_trigger=True,  # Treat liquidation like manual for some pre-checks
                snapshot_version=f"LIQUIDATE_{reason_detail[:20].replace(' ', '_')}",
                ocr_snapshot_cost_basis=None,
                perf_timestamps=perf_ts,
                extra_fields=executor_extra_fields,  # Pass aggressive_chase_parameters here
                aggressive_chase=aggressive_chase  # Explicitly pass aggressive_chase flag
            )

            if local_id:
                self.logger.info(f"Liquidation order ({order_params.event_type}) initiated for {symbol_upper}, local_id={local_id}")
                if parent_trade_id:
                    self._lifecycle_manager.update_on_order_submission(parent_trade_id, local_id)
                else:
                    self.logger.warning(f"[{symbol_upper}] No parent_trade_id available to update with local_id={local_id}")
                if is_performance_tracking_enabled() and perf_ts:
                    add_timestamp(perf_ts, f'tm_service.{symbol_upper}.liquidate_initiated')
                self._log_performance(perf_ts, f"Liquidate_{symbol_upper}")
                return True
            else:
                self.logger.error(f"Liquidation initiation ({order_params.event_type}) failed for {symbol} (TradeExecutor returned None).")
                if is_performance_tracking_enabled() and perf_ts:
                    add_timestamp(perf_ts, f'tm_service.{symbol}.liquidate_err_executor')
                self._log_performance(perf_ts, f"Liquidate_{symbol}")
                return False

        except Exception as e:
            self.logger.error(f"Error during position liquidation logic for {symbol}: {e}", exc_info=True)
            if is_performance_tracking_enabled() and perf_ts:
                add_timestamp(perf_ts, f'tm_service.{symbol}.liquidate_err_exception')
            self._log_performance(perf_ts, f"Liquidate_{symbol}")
            return False

    # _execute_emergency_close method removed - functionality consolidated into _handle_critical_risk_event


    # --- Core Processing Logic ---
    @log_function_call('trade_manager')
    def _process_single_snapshot(self, symbol: str, new_state: Dict[str, Any], time_stamps: Optional[dict], perf_timestamps: Dict[str, float]) -> None:
        """
        DEPRECATED: This method is superseded by OCRScalpingSignalOrchestratorService for OCR scalping flow.

        Processes the state update for a single symbol from the snapshot.

        NOTE: This method is now only for non-OCR signal sources that directly push snapshot data.
        The primary OCR scalping flow now uses ValidatedOrderRequestEvent handling via _handle_validated_order_request.

        IMPORTANT: Action filtering is no longer performed by this deprecated method.
        If future non-OCR signal sources require filtering, they should handle it at the signal source level.
        """
        # PROMINENT DEPRECATION WARNING
        self.logger.warning("DEPRECATED _process_single_snapshot called. This path is for non-OCR-event signals ONLY. "
                           "OCR scalping uses event-driven flow via _handle_validated_order_request.")

        # self.logger.debug(f"ENTER _process_single_snapshot for symbol: {symbol}") # Original debug
        symbol_upper = symbol.upper()
        # current_time = time.time() # Will be used by MasterActionFilter internally

        # Parse current OCR values early for _last_ocr_state update, even if skipping
        current_ocr_cost = self._parse_dollar_value(new_state.get("cost_basis", 0.0))
        current_ocr_rPnL = self._parse_dollar_value(new_state.get("realized_pnl", 0.0))
        current_ocr_strategy_token = new_state.get("strategy", "Closed").lower()

        # Check if trading is generally enabled - do this very early to avoid unnecessary processing
        with self._trading_enabled_lock:
            if not self._is_trading_enabled:
                self.logger.info(f"[{symbol_upper}] Skipping snapshot processing: Trading is disabled.")
                with self._ocr_state_lock:
                    symbol_tms_state = self._last_ocr_state.setdefault(symbol_upper, {})
                    symbol_tms_state["cached_cost"] = current_ocr_cost
                    symbol_tms_state["cached_realized"] = current_ocr_rPnL
                return

        # --- POST-MELTDOWN HOLD CHECK ---
        is_on_hold = False
        with self._post_meltdown_hold_lock:
            is_on_hold = symbol_upper in self._symbols_on_post_meltdown_hold

        if is_on_hold and current_ocr_strategy_token == "long": # Check if OCR is trying to OPEN
            self.logger.warning(
                f"[{symbol_upper}] TMS: Suppressing potential OPEN_LONG signal from OCR. "
                f"Symbol is on 'post-meltdown hold'. OCR strategy: {current_ocr_strategy_token}."
            )
            # Update _last_ocr_state and return, preventing further processing of this OPEN signal
            with self._ocr_state_lock:
                symbol_tms_state = self._last_ocr_state.setdefault(symbol_upper, {})
                symbol_tms_state["cached_cost"] = current_ocr_cost
                symbol_tms_state["cached_realized"] = current_ocr_rPnL
            return
        # --- END POST-MELTDOWN HOLD CHECK ---

        # 2. Get previous OCR values for SnapshotInterpreter
        with self._ocr_state_lock: # Protects self._last_ocr_state
            # These are from the *immediately preceding* OCR frame
            # Default to current values if no history (first snapshot for symbol)
            previous_ocr_rPnL_for_interpreter = self._last_ocr_state.get(symbol_upper, {}).get("cached_realized", current_ocr_rPnL)
            previous_ocr_cost_for_interpreter = self._last_ocr_state.get(symbol_upper, {}).get("cached_cost", current_ocr_cost)

        # --- Initial Setup: Get current PM state and previous OCR values ---
        pos_data = self._position_manager.get_position(symbol_upper)
        # Default pos_data if none exists (for first-time processing of a symbol)
        if pos_data is None:
            pos_data = {
                "symbol": symbol_upper, "open": False, "strategy": "closed", "shares": 0.0, "avg_price": 0.0,
                "realized_pnl": 0.0, "trading_status": "Unknown", "luld_high": None, "luld_low": None,
                "last_broker_fingerprint": None, "retry_count": 0, "cooldown_until": 0.0, "last_update": 0.0
            }

        # Log position data before calling SnapshotInterpreter
        self.logger.critical(f"TMS BEFORE SnapshotInterpreter for {symbol_upper}: "
                           f"PM Position: shares={pos_data.get('shares', 0.0):.2f}, "
                           f"strategy={pos_data.get('strategy', 'closed')}, "
                           f"open={pos_data.get('open', False)}, "
                           f"avg_price={pos_data.get('avg_price', 0.0):.4f}, "
                           f"Timestamp: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

        # Check for recently synced symbols
        ocr_snapshot_strategy = new_state.get("strategy", "Closed").lower()
        pm_shares = pos_data.get("shares", 0.0)

        with self._sync_grace_lock:
            last_sync_time = self._recently_synced_symbols.get(symbol_upper)

        if last_sync_time:
            sync_grace_period = getattr(self._config, 'sync_grace_period_seconds', 3.0) # New config
            if (time.time() - last_sync_time) < sync_grace_period:
                # We are in the grace period for a recently synced symbol
                if abs(pm_shares) > 1e-9 and ocr_snapshot_strategy == "closed":
                    self.logger.warning(
                        f"[{symbol_upper}] OCR shows 'Closed', but PM has {pm_shares} from recent sync. "
                        f"Suppressing OCR-driven CLOSE during sync grace period."
                    )
                    # Update _last_ocr_state and return, effectively ignoring this "closed" snapshot for action
                    with self._ocr_state_lock:
                        symbol_tms_state = self._last_ocr_state.setdefault(symbol_upper, {})
                        symbol_tms_state["cached_cost"] = current_ocr_cost # Parsed at top of method
                        symbol_tms_state["cached_realized"] = current_ocr_rPnL   # Parsed at top of method
                    return ################## EXIT EARLY #################
            elif last_sync_time: # Grace period expired
                with self._sync_grace_lock:
                    if symbol_upper in self._recently_synced_symbols: # Check again before deleting
                        del self._recently_synced_symbols[symbol_upper]
                    self.logger.info(f"[{symbol_upper}] Sync grace period expired.")

        # --- 1. Handle Expired or Stale Suppressed Attempts ---
        # Pass the current OCR snapshot ('new_state') to help decide if an attempt is stale due to OCR signal change
        self._cleanup_stale_suppressed_attempts(symbol, new_state)

        # --- 2. Check if we are currently RETRYING a Suppressed Open ---
        retry_status = self._check_and_execute_suppressed_open_retry(symbol, new_state, time_stamps, perf_timestamps)

        if retry_status == "EXECUTED" or retry_status == "RETRY_ABANDONED":
            # If retry logic handled it (executed or fully abandoned), then this snapshot's main path is done.
            # Update _last_ocr_state here as we are returning.
            with self._ocr_state_lock:
                self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
            self.logger.debug(f"[{symbol}] Suppressed open retry logic concluded (Status: {retry_status}). Exiting snapshot processing.")
            return
        # If retry_status is "RETRY_PENDING_RISK" or "NO_RETRY_ACTIVE", we continue with normal processing.

        # --- 3. Normal Snapshot Processing Starts Here ---

        # Ensure price subscription (existing logic)
        if not self._price_provider.is_subscribed(symbol):
            try:
                self._price_provider.subscribe_symbol(symbol)
            except Exception as e_sub:
                self.logger.warning(f"Error subscribing to {symbol}: {e_sub}")

        # Handle special PM states directly (OpenRetryCooldown, OpenFailed) - (existing logic)
        # This should come AFTER retry logic for suppressed opens, as those are TMS-level suppressions,
        # while OpenRetryCooldown/OpenFailed are PM-level states from execution failures.
        old_pm_strategy = pos_data.get("strategy", "closed")
        if old_pm_strategy == "OpenRetryCooldown":
            # ... (existing OpenRetryCooldown logic from your trade_manager_service.py) ...
            # Make sure to update _last_ocr_state before returning from this block
            new_ocr_strat_for_cooldown = new_state.get("strategy", "").lower()
            cooldown_until = pos_data.get("cooldown_until", 0.0)
            if new_ocr_strat_for_cooldown == "long" and time.time() >= cooldown_until:
                current_retry_count = pos_data.get("retry_count", 0)
                if current_retry_count <= self.MAX_OPEN_RETRIES:
                    self.logger.info(f"[{symbol}] TMS: Cooldown finished. Resetting PM for retry.")
                    self._position_manager.force_close_state(symbol)
                else:
                    self.logger.warning(f"[{symbol}] TMS: Cooldown finished but max retries exceeded.")
                    self._position_manager.handle_failed_open(symbol, self.MAX_OPEN_RETRIES, self.BASE_OPEN_RETRY_COOLDOWN_SEC)
            with self._ocr_state_lock: self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
            self.logger.info(f"[{symbol}] In OpenRetryCooldown. Returning early.")
            return
        elif old_pm_strategy == "OpenFailed":
            with self._ocr_state_lock: self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
            self.logger.info(f"[{symbol}] In OpenFailed state. Returning early.")
            return

        # --- 4. Preliminary Market Condition Check (for ALL signals, but critical for new OPENs) ---
        if self._risk_service:
            try:
                # Determine if this is a potential new open based on current OCR and PM state
                ocr_strategy_in_snapshot = new_state.get("strategy", "").lower() # Get current OCR strategy
                pm_current_strategy = pos_data.get("strategy", "closed")      # Get current PositionManager strategy

                # A potential new open is when PM is 'closed' and OCR shows 'long' (or 'short' if implemented)
                is_attempting_to_open_new_position = (pm_current_strategy == "closed" and
                                                     ocr_strategy_in_snapshot == "long") # Adapt if shorting exists

                market_assessment = self._risk_service.check_market_conditions(symbol)
                current_condition_identifier = f"{market_assessment.level.name}:{market_assessment.reason}"

                # De-duplicate GUI alert (logic from previous agent task)
                send_gui_alert_this_time = False
                # Assuming _market_condition_alerts_sent and _market_condition_alerts_lock exist from previous task
                if hasattr(self, '_market_condition_alerts_lock'): # Check if attributes exist
                    with self._market_condition_alerts_lock:
                        if self._market_condition_alerts_sent.get(symbol) != current_condition_identifier:
                            if market_assessment.level not in (RiskLevel.NORMAL, RiskLevel.ELEVATED): # Only update for adverse
                                self._market_condition_alerts_sent[symbol] = current_condition_identifier
                                send_gui_alert_this_time = True
                        if market_assessment.level in (RiskLevel.NORMAL, RiskLevel.ELEVATED) and symbol in self._market_condition_alerts_sent:
                            del self._market_condition_alerts_sent[symbol] # Clear alert if conditions improve
                else:
                    # Fallback if alert deduplication not set up
                    send_gui_alert_this_time = market_assessment.level not in (RiskLevel.NORMAL, RiskLevel.ELEVATED)

                if market_assessment.level not in (RiskLevel.NORMAL, RiskLevel.ELEVATED):
                    log_msg = f"[{symbol}] Market conditions ({market_assessment.reason}, Level: {market_assessment.level.name}) not suitable."
                    self.logger.warning(log_msg)
                    if send_gui_alert_this_time and self._gui_logger_func:
                        self._gui_logger_func(f"RISK WARNING: {symbol} market conditions - {market_assessment.reason}", "warning")

                    # --- MODIFIED CONDITIONAL BLOCKING ---
                    if is_attempting_to_open_new_position:
                        self.logger.warning(f"[{symbol}] Blocking potential NEW OPEN due to adverse market conditions: {market_assessment.level.name} - {market_assessment.reason}.")
                        # Record suppressed open attempt logic (as existing)
                        estimated_price = 0.0
                        try:
                            estimated_price = self._price_provider.get_reliable_price(symbol, side="BUY")
                        except Exception as e_price:
                            self.logger.warning(f"[{symbol}] Could not get reliable price for suppressed open attempt: {e_price}")

                        # Check if we are ALREADY in a retry pending risk state for this symbol (to avoid re-recording)
                        is_already_pending_retry_for_this_reason = False
                        with self._suppressed_open_attempts_lock:
                            if symbol in self._suppressed_open_attempts and self._suppressed_open_attempts[symbol]['original_reason'] == market_assessment.reason:
                                is_already_pending_retry_for_this_reason = True

                        if not is_already_pending_retry_for_this_reason and estimated_price > 0:
                            self._record_suppressed_open_attempt(symbol, new_state, market_assessment.reason, estimated_price)
                        elif not is_already_pending_retry_for_this_reason:
                            self.logger.warning(f"[{symbol}] Not recording suppressed open: bad est. price {estimated_price:.4f}")

                        with self._ocr_state_lock: # Update last OCR state before returning
                            self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
                        return # Stop further processing ONLY for new open attempts under adverse conditions
                    else:
                        self.logger.info(f"[{symbol}] Market conditions adverse ({market_assessment.level.name}), but signal is NOT a new open attempt. Proceeding with snapshot interpretation.")
                    # --- END MODIFIED CONDITIONAL BLOCKING ---

                elif market_assessment.level == RiskLevel.ELEVATED:
                    self.logger.info(f"[{symbol}] Proceeding despite ELEVATED market conditions: {market_assessment.reason}")

            except Exception as e_mkt_check:
                self.logger.error(f"[{symbol}] Error during market condition check: {e_mkt_check}", exc_info=True)
                with self._ocr_state_lock: self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
                return # Skip processing if the check fails

        # 3. Get base signal from SnapshotInterpreter
        signal: Optional[TradeSignal] = None
        try:
            # Use the 'previous_ocr_cost_for_interpreter' and 'previous_ocr_rPnL_for_interpreter' fetched at the start
            signal = self._snapshot_interpreter.interpret_single_snapshot(
                symbol=symbol_upper,
                new_state=new_state,
                pos_data=pos_data, # Current PM state
                old_ocr_cost=previous_ocr_cost_for_interpreter,
                old_ocr_realized=previous_ocr_rPnL_for_interpreter,
                perf_timestamps=perf_timestamps
            )

            # Get fresh position data after SnapshotInterpreter
            fresh_pos_data = self._position_manager.get_position(symbol_upper)

            # Log position data and signal after SnapshotInterpreter
            self.logger.critical(f"TMS AFTER SnapshotInterpreter for {symbol_upper}: "
                               f"Signal: {signal.action.name if signal else 'None'}, "
                               f"PM Position: shares={fresh_pos_data.get('shares', 0.0) if fresh_pos_data else 0.0:.2f}, "
                               f"strategy={fresh_pos_data.get('strategy', 'closed') if fresh_pos_data else 'closed'}, "
                               f"open={fresh_pos_data.get('open', False) if fresh_pos_data else False}, "
                               f"avg_price={fresh_pos_data.get('avg_price', 0.0) if fresh_pos_data else 0.0:.4f}, "
                               f"Timestamp: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")

        except Exception as e_interpret:
            self.logger.error(f"[{symbol_upper}] TMS: Error calling SnapshotInterpreter: {e_interpret}", exc_info=True)

        # 4. Signal filtering removed for deprecated path
        # NOTE: Action filtering is no longer performed by this deprecated method.
        # For OCR scalping signals, filtering is handled by MasterActionFilterService in OCRScalpingSignalGenerator.
        # If future non-OCR signal sources require filtering, they should handle it at the signal source level.
        if signal:
            self.logger.info(f"[{symbol_upper}] TMS (Deprecated Path): Interpreter generated: {signal.action.name}. No filtering applied.")

            # Clear TMS fingerprint cache for definitive OPEN/CLOSE signals
            if signal.action in [TradeActionType.OPEN_LONG, TradeActionType.CLOSE_POSITION]:
                with self._fingerprint_lock:
                    if symbol_upper in self._recent_fingerprints:
                        del self._recent_fingerprints[symbol_upper]
                        self.logger.info(f"[{symbol_upper}] TMS: Cleared TMS internal fingerprint cache for symbol due to {signal.action.name} signal processing.")
                    if symbol_upper in self._fingerprint_timestamps:
                        del self._fingerprint_timestamps[symbol_upper]

        # --- 6. Process Signal (if any) ---
        if signal:
            self.logger.info(f"[{symbol_upper}] TMS: Processing filtered action {signal.action.name}")

            # Fingerprint for duplicate event detection (existing logic)
            # Cost basis for OPEN fingerprint should ideally be 0 or based on a predictable price.
            # If signal.action is OPEN, cost_basis_for_fp uses 0.
            # For ADD, uses current pos_data avg_price.
            # For REDUCE/CLOSE, uses current pos_data avg_price.
            cost_basis_for_fp = 0.0
            if signal.action == TradeActionType.OPEN_LONG:
                cost_basis_for_fp = 0.0 # For an open, the "current cost" influencing the decision is effectively zero.
            elif signal.triggering_snapshot: # For ADD/REDUCE/CLOSE, use PM's avg_price
                cost_basis_for_fp = pos_data.get("avg_price", 0.0)


            fingerprint = self._make_fingerprint(
                event_type=signal.action.name,
                shares_float=float(self._config.initial_share_size) if signal.action == TradeActionType.OPEN_LONG else \
                            (float(self._config.manual_shares) if signal.action == TradeActionType.ADD_LONG else pos_data.get("shares",0.0)),
                cost_basis=cost_basis_for_fp,
                realized_pnl=pos_data.get("realized_pnl", 0.0),
                snapshot_version=new_state.get("snapshot_version")
            )
            if self._is_duplicate_event(symbol, fingerprint):
                self.logger.warning(f"[{symbol}] Duplicate {signal.action.name} event detected. Skipping signal processing.")
                
                # Log signal duplicate detection event via correlation logger
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="TradeManagerService",
                        event_name="SignalDuplicateDetected",
                        payload={
                            "decision_type": "SIGNAL_DUPLICATE_DETECTED",
                            "decision_reason": "Duplicate signal fingerprint detected at trade manager level",
                            "duplicate_detection_level": "SIGNAL_FINGERPRINT",
                            "symbol": symbol,
                            "action": signal.action.name,
                            "fingerprint": str(fingerprint),
                            "current_ocr_cost": current_ocr_cost,
                            "current_ocr_rpnl": current_ocr_rPnL,
                            "signal_id": getattr(signal, 'id', None),
                            "rejection_category": "DUPLICATE_SIGNAL",
                            "correlation_id": master_correlation_id,
                            "causation_id": causation_id_for_next_step,
                            "timestamp": time.time()
                        }
                    )
                
                with self._ocr_state_lock: self._last_ocr_state[symbol_upper] = {"cached_cost": current_ocr_cost, "cached_realized": current_ocr_rPnL}
                return

            # Calculate Order Parameters
            order_params: Optional[OrderParameters] = None
            try:
                order_params = self._order_strategy.calculate_order_params(
                    signal=signal, position_manager=self._position_manager,
                    price_provider=self._price_provider, config_service=self._config
                )
            except Exception as e_strat:
                self.logger.error(f"[{symbol}] TMS: Error calling OrderStrategy: {e_strat}", exc_info=True)

            if order_params:
                self.logger.info(f"[{symbol}] TMS: Received OrderParameters from Strategy: {order_params}")

                active_trade_id: Optional[int] = None
                if signal.action == TradeActionType.OPEN_LONG:
                    active_trade_id = self._lifecycle_manager.create_new_trade(symbol)
                    order_params.parent_trade_id = active_trade_id
                else:
                    active_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)
                    if active_trade_id is None and signal.action != TradeActionType.OPEN_LONG: # Should not happen if PM state is accurate
                        self.logger.error(f"[{symbol}] {signal.action.name} signal for {symbol} but no active trade found by LCM. PM State: {pos_data.get('strategy')}. Forcing one.")
                        active_trade_id = self._lifecycle_manager.create_new_trade(symbol) # Create one if missing
                        self._lifecycle_manager.update_trade_state(active_trade_id, new_state=LifecycleState.OPEN_ACTIVE, comment="LCM Sync on non-OPEN signal")
                    order_params.parent_trade_id = active_trade_id

                if active_trade_id is None: # Should only be possible if OPEN_LONG failed to create trade_id
                    self.logger.error(f"[{symbol}] TMS: Failed to get/create active_trade_id for {signal.action.name}. Skipping execution.")
                else:
                    self.logger.info(f"[{symbol}] TMS: Calling TradeExecutor for signal {signal.action.name} with trade_id {active_trade_id}")
                    executed_local_id: Optional[int] = None
                    try:
                        executed_local_id = self._trade_executor.execute_trade(
                            order_params=order_params,
                            time_stamps=time_stamps,
                            snapshot_version=new_state.get("snapshot_version"),
                            ocr_confidence=new_state.get("confidence", 0.0),
                            ocr_snapshot_cost_basis=current_ocr_cost, # Pass current snapshot's cost
                            extra_fields=new_state.get("extra_fields"), # Pass along if available
                            is_manual_trigger=False,
                            perf_timestamps=perf_timestamps
                        )
                        if executed_local_id:
                            self.logger.info(f"[{symbol_upper}] TMS: TradeExecutor initiated order with local_id: {executed_local_id}")
                            self._lifecycle_manager.update_on_order_submission(active_trade_id, executed_local_id)

                            # NOTE: MasterActionFilter recording removed for deprecated path
                            # For OCR scalping signals, action recording is handled by MasterActionFilterService
                            # in OCRScalpingSignalGenerator after successful OrderRequestEvent publication.
                            # This deprecated path no longer performs action recording.
                        else:
                            self.logger.warning(f"[{symbol_upper}] TMS: TradeExecutor did not return local_id for {signal.action.name}.")
                            if signal.action == TradeActionType.OPEN_LONG:
                                self._lifecycle_manager.mark_trade_failed_to_open(active_trade_id, "Executor Skipped/Failed")
                    except Exception as e_exec:
                        self.logger.error(f"[{symbol_upper}] TMS: Error calling TradeExecutor: {e_exec}", exc_info=True)
                        if signal.action == TradeActionType.OPEN_LONG and active_trade_id:
                            self._lifecycle_manager.mark_trade_failed_to_open(active_trade_id, "Execution Exception")
            else:
                self.logger.info(f"[{symbol_upper}] TMS: OrderStrategy returned None for {signal.action.name}.")
        else:
            self.logger.debug(f"[{symbol_upper}] TMS: No signal received from Interpreter for this snapshot.")

        # --- 7. Final Update of _last_ocr_state ---
        with self._ocr_state_lock:
            self._last_ocr_state[symbol_upper] = {
                "cached_cost": current_ocr_cost,
                "cached_realized": current_ocr_rPnL
            }
            # self.logger.debug(f"TMS: Updated _last_ocr_state for {symbol} at end of processing.") # Original log

        # --- 8. Risk Monitoring ---
        # Risk monitoring is now handled by LCM callbacks

        # self.logger.debug(f"EXIT _process_single_snapshot for {symbol} - total duration: {(time.perf_counter() - start_time)*1000:.2f}ms") # Original log

    # --- Internal Helper Methods --- (_log_event, _create_event_log, _parse_dollar_value, _make_fingerprint remain same)
    @log_function_call('trade_manager')
    # _log_event method removed - now handled by lifecycle manager


    @log_function_call('trade_manager')
    def _create_event_log(self, symbol: str, event: str, shares_change: float, new_shares: float, avg_price: float, realized_pnl: float, comment: str = "", time_stamps: Optional[dict] = None) -> Dict[str, Any]:
         """Helper to create a standardized event dictionary."""
         return {
            "timestamp": time.time(), # Overridden by _log_event if needed
            "symbol": symbol.upper(),
            "event": event.upper(),
            "shares_change": shares_change,
            "new_shares": new_shares,
            "avg_price": avg_price,
            "realized_pnl": realized_pnl,
            "comment": comment,
            "time_stamps": copy.deepcopy(time_stamps) if time_stamps else {} # Store copies
        }

    @log_function_call('trade_manager')
    def _parse_dollar_value(self, value_str) -> float:
        """Strips commas/spaces, converts to float, rounds to two decimals."""
        if isinstance(value_str, (float, int)):
            return round(float(value_str), 2)
        if isinstance(value_str, str):
            clean = re.sub(r'[,\s$]', '', value_str) # Added $ removal
            if not clean: return 0.0 # Handle empty string after cleaning
            try:
                 return round(float(clean), 2)
            except ValueError:
                 self.logger.error(f"Could not parse dollar value: '{value_str}' -> '{clean}'")
                 raise ValueError(f"Invalid dollar value format: {value_str}")
        # Handle other types if necessary, e.g., None
        if value_str is None:
            return 0.0
        self.logger.error(f"Could not parse value of type {type(value_str)}: {value_str}")
        raise ValueError(f"Unsupported type for dollar value parsing: {type(value_str)}")

    @log_function_call('trade_manager')
    def _record_suppressed_open_attempt(self,
                                        symbol: str,
                                        original_snapshot_data: Dict[str, Any],
                                        suppression_reason: str,
                                        estimated_entry_price: float) -> None:
        """
        Records an open attempt that was suppressed due to a preliminary risk check.
        """
        symbol = symbol.upper()
        with self._suppressed_open_attempts_lock:
            current_time = time.time()
            retry_window_sec = getattr(self._config, 'suppressed_open_retry_window_seconds', 10.0)
            price_tolerance_cents = getattr(self._config, 'suppressed_open_price_tolerance_cents', 0.05)

            self._suppressed_open_attempts[symbol] = {
                "symbol": symbol,
                "suppressed_at_ts": current_time,
                "original_reason": suppression_reason,
                "estimated_entry_price_at_suppression": estimated_entry_price,
                "retry_until_ts": current_time + retry_window_sec,
                "price_tolerance_cents": price_tolerance_cents,
                "original_snapshot_data": copy.deepcopy(original_snapshot_data) # Store a copy
            }
            self.logger.info(
                f"[{symbol}] Recorded suppressed OPEN attempt. Reason: {suppression_reason}. "
                f"Est.Price: {estimated_entry_price:.4f}. Retry until {self._suppressed_open_attempts[symbol]['retry_until_ts']:.0f}."
            )

    @log_function_call('trade_manager')
    def _cleanup_stale_suppressed_attempts(self, symbol_being_processed: Optional[str] = None, current_ocr_snapshot_for_symbol: Optional[Dict[str, Any]] = None) -> None:
        """
        Cleans up suppressed open attempts that have expired or are no longer relevant
        (e.g., OCR signal changed for the symbol being processed).
        """
        with self._suppressed_open_attempts_lock:
            current_time = time.time()
            symbols_to_remove = []

            # First, check the specific symbol being processed if its OCR signal changed
            if symbol_being_processed and current_ocr_snapshot_for_symbol:
                symbol_upper = symbol_being_processed.upper()
                if symbol_upper in self._suppressed_open_attempts:
                    ocr_strategy_now = current_ocr_snapshot_for_symbol.get("strategy", "").lower()
                    if ocr_strategy_now != "long":
                        symbols_to_remove.append(symbol_upper)
                        self.logger.info(f"[{symbol_upper}] Clearing suppressed OPEN attempt because OCR strategy is now '{ocr_strategy_now}'.")

            # Then, iterate all to find expired ones
            for sym, attempt_data in list(self._suppressed_open_attempts.items()): # Iterate over a copy
                if sym in symbols_to_remove: # Already marked for removal
                    continue
                if current_time > attempt_data.get("retry_until_ts", 0):
                    symbols_to_remove.append(sym)
                    self.logger.info(f"[{sym}] Suppressed OPEN attempt expired. Reason: {attempt_data.get('original_reason')}.")

            for sym_to_remove in symbols_to_remove:
                if sym_to_remove in self._suppressed_open_attempts:
                    del self._suppressed_open_attempts[sym_to_remove]

    @log_function_call('trade_manager')
    def _check_and_execute_suppressed_open_retry(self,
                                                 symbol: str,
                                                 current_ocr_snapshot: Dict[str, Any],
                                                 time_stamps: Optional[dict],
                                                 perf_timestamps: Dict[str, float]) -> str:
        """
        Checks if a suppressed open attempt should be retried and executes if conditions are met.

        Returns:
            str: "EXECUTED" if retry was attempted (success or failure of execution itself).
                 "RETRY_ABANDONED" if conditions for retry were not met and attempt cleared.
                 "RETRY_PENDING_RISK" if risk conditions still prevent retry.
                 "NO_RETRY_ACTIVE" if no relevant suppressed attempt.
        """
        symbol = symbol.upper()
        with self._suppressed_open_attempts_lock:
            attempt_data = self._suppressed_open_attempts.get(symbol)

            if not attempt_data or time.time() > attempt_data["retry_until_ts"]:
                if attempt_data: # It existed but expired now
                    self.logger.info(f"[{symbol}] Suppressed OPEN attempt expired before retry check.")
                    del self._suppressed_open_attempts[symbol]
                return "NO_RETRY_ACTIVE"

            self.logger.info(f"[{symbol}] Evaluating retry for suppressed OPEN attempt (Reason: {attempt_data['original_reason']}).")

            # 1. Check current OCR strategy
            current_ocr_strategy = current_ocr_snapshot.get("strategy", "").lower()
            if current_ocr_strategy != "long":
                self.logger.warning(f"[{symbol}] Abandoning suppressed OPEN retry: OCR strategy is now '{current_ocr_strategy}'.")
                del self._suppressed_open_attempts[symbol]
                return "RETRY_ABANDONED"

            # 2. Re-check market conditions
            if self._risk_service:
                market_assessment = self._risk_service.check_market_conditions(symbol)
                # (Optional: GUI alert de-duplication for market_assessment if needed here too)
                if market_assessment.level not in (RiskLevel.NORMAL, RiskLevel.ELEVATED):
                    self.logger.warning(f"[{symbol}] Suppressed OPEN retry STALLED: Market conditions still not suitable ({market_assessment.reason}).")
                    # Don't delete the attempt yet, let it be re-checked or expire
                    return "RETRY_PENDING_RISK"
            self.logger.info(f"[{symbol}] Suppressed OPEN retry: Market conditions are now suitable.")

            # 3. Check current price against tolerance
            current_ask_price = 0.0
            try:
                current_ask_price = self._price_provider.get_ask_price(symbol) # Or get_reliable_price("BUY")
                if not current_ask_price or current_ask_price <= 0:
                     current_ask_price = self._price_provider.get_latest_price(symbol) or 0.0
            except Exception as e_price:
                self.logger.warning(f"[{symbol}] Could not get current Ask price for retry: {e_price}")

            if current_ask_price <= 0:
                self.logger.warning(f"[{symbol}] Abandoning suppressed OPEN retry: Could not fetch valid current Ask price.")
                del self._suppressed_open_attempts[symbol]
                return "RETRY_ABANDONED"

            estimated_price_at_suppression = attempt_data["estimated_entry_price_at_suppression"]
            price_tolerance_cents = attempt_data["price_tolerance_cents"]
            max_allowable_price = estimated_price_at_suppression + price_tolerance_cents

            self.logger.info(f"[{symbol}] Suppressed OPEN retry price check: CurrentAsk={current_ask_price:.4f}, "
                             f"OriginalEst={estimated_price_at_suppression:.4f}, Tolerance={price_tolerance_cents:.4f}, MaxAllowable={max_allowable_price:.4f}")

            if current_ask_price > max_allowable_price:
                self.logger.warning(f"[{symbol}] Abandoning suppressed OPEN retry: Current Ask price {current_ask_price:.4f} "
                                    f"exceeds max allowable {max_allowable_price:.4f}.")
                del self._suppressed_open_attempts[symbol]
                return "RETRY_ABANDONED"

            self.logger.info(f"[{symbol}] Conditions MET for suppressed OPEN retry. Proceeding with execution.")
            # --- All conditions for retry are met. Proceed to execute. ---

            # Use the original snapshot that caused the suppression to form the signal.
            # This ensures we're acting on the trader's original intent as closely as possible.
            original_snapshot_to_use = attempt_data["original_snapshot_data"]

            # Create a new TradeSignal for this retry
            # Note: pos_data for interpreter should be fresh here if interpreter is called again.
            # However, for a direct retry, we might directly form OrderParameters.
            # For simplicity, let's assume we re-interpret, but this could be optimized.

            # Get fresh pos_data for the interpreter if we re-interpret
            # This is important because position state might have changed due to other factors
            # (though unlikely for a "closed" to "long" if no other trades happened)
            fresh_pos_data = self._position_manager.get_position(symbol) or {
                "symbol": symbol, "open": False, "strategy": "closed", "shares": 0.0, "avg_price": 0.0,
                "realized_pnl": 0.0, "trading_status": "Unknown", "luld_high": None, "luld_low": None,
                "last_broker_fingerprint": None, "retry_count": 0, "cooldown_until": 0.0, "last_update": 0.0
            }

            # Re-call interpreter with the original snapshot data but fresh pos_data
            # (old_ocr_cost/realized might be stale if we don't update them, but for a quick retry, less critical)
            # For a pure retry of a specific suppressed event, it might be better to bypass full re-interpretation
            # and directly use parameters derived from the *original* attempt if they were calculated.
            # Let's proceed by creating the signal and going through the regular flow for now.

            retry_signal = TradeSignal(action=TradeActionType.OPEN_LONG,
                                       symbol=symbol,
                                       triggering_snapshot=original_snapshot_to_use)

            # Add a note about the retry to the signal (or handle in OrderStrategy/Executor)
            retry_signal.triggering_snapshot["retry_info"] = (
                f"Late entry (suppressed open retry). Original reason: {attempt_data['original_reason']}. "
                f"Original est. price: {attempt_data['estimated_entry_price_at_suppression']:.4f}"
            )

            # Clear the attempt BEFORE trying to execute, to prevent re-entry loops on execution failure.
            del self._suppressed_open_attempts[symbol]

            # --- Proceed with the normal execution flow for this retry_signal ---
            # (This part is similar to the main signal processing block in _process_single_snapshot)

            # Fingerprint for this retry (use current_ocr_snapshot version for consistency if available)
            # Note: If the retry is based on the *original* snapshot, fingerprinting logic needs care.
            # For simplicity, let's assume fingerprint for retries uses current context.
            snapshot_version_for_fp = current_ocr_snapshot.get("snapshot_version", attempt_data["original_snapshot_data"].get("snapshot_version"))

            # Fingerprint cost basis: use the *current_ask_price* as it's the price we're acting on.
            fingerprint = self._make_fingerprint(
                event_type=retry_signal.action.name,
                shares_float=float(self._config.initial_share_size), # Assuming OPEN uses initial_share_size
                cost_basis=current_ask_price, # Use current ask as cost basis for this retry
                realized_pnl=fresh_pos_data.get("realized_pnl", 0.0),
                snapshot_version=snapshot_version_for_fp
            )
            if self._is_duplicate_event(symbol, fingerprint):
                self.logger.warning(f"[{symbol}] Duplicate event detected for RETRY OPEN. Skipping.")
                return "RETRY_ABANDONED" # Or a new status like "RETRY_DUPLICATE"

            order_params = self._order_strategy.calculate_order_params(
                signal=retry_signal, # Use the special retry_signal
                position_manager=self._position_manager,
                price_provider=self._price_provider,
                config_service=self._config
            )

            if order_params:
                # Potentially adjust order_params.limit_price to be based on current_ask_price for the retry
                order_params.limit_price = self._order_strategy._calculate_limit_price(current_ask_price, "BUY") # Assuming OrderStrategy has this helper
                if order_params.limit_price is None:
                    self.logger.error(f"[{symbol}] RETRY OPEN failed: Could not calculate limit price for retry from ask {current_ask_price:.4f}.")
                    return "RETRY_ABANDONED"

                order_params.event_type = f"OPEN_RETRY (Late: {attempt_data['original_reason']})" # Mark event type clearly

                self.logger.info(f"[{symbol}] TMS: Calling TradeExecutor for RETRY OPEN signal...")
                # Create active_trade_id for this new OPEN attempt
                active_trade_id = self._lifecycle_manager.create_new_trade(symbol)
                order_params.parent_trade_id = active_trade_id

                # Pass current_ocr_snapshot's cost basis for executor's fingerprint if needed, though less relevant for retry
                current_snapshot_cost_for_executor = self._parse_dollar_value(current_ocr_snapshot.get("cost_basis", 0.0))

                executed_local_id = self._trade_executor.execute_trade(
                    order_params=order_params,
                    time_stamps=time_stamps,
                    snapshot_version=current_ocr_snapshot.get("snapshot_version"), # Use current snapshot's version
                    ocr_confidence=current_ocr_snapshot.get("confidence", 0.0),
                    ocr_snapshot_cost_basis=current_snapshot_cost_for_executor, # Current snapshot's cost
                    extra_fields={"retry_info": retry_signal.triggering_snapshot["retry_info"]},
                    is_manual_trigger=False,
                    perf_timestamps=perf_timestamps if is_performance_tracking_enabled() else None
                )
                if executed_local_id:
                    self._lifecycle_manager.update_on_order_submission(active_trade_id, executed_local_id)
                    self.logger.info(f"[{symbol}] RETRY OPEN successfully initiated with local_id: {executed_local_id}")
                else:
                    self.logger.warning(f"[{symbol}] RETRY OPEN execution failed or skipped by executor.")
                    if active_trade_id:
                        self._lifecycle_manager.mark_trade_failed_to_open(active_trade_id, "Retry Execution Failed/Skipped")
                return "EXECUTED" # Indicates execution was attempted for the retry
            else:
                self.logger.warning(f"[{symbol}] OrderStrategy returned None for RETRY OPEN signal.")
                return "RETRY_ABANDONED" # No params, abandon.

    @log_function_call('trade_manager')
    def _make_fingerprint(self, event_type: str, shares_float: float, cost_basis: float, realized_pnl: float, snapshot_version: Optional[str]) -> tuple:
        """Creates a tuple fingerprint to detect duplicate events. Rounds values."""
        # TODO: Ensure 'snapshot_version' is consistently provided by the OCR processing step for reliable duplicate detection.
        try:
            shares_int = int(round(shares_float))
            # Handle potential None or non-numeric cost_basis/realized_pnl before rounding
            cost_rounded = round(float(cost_basis), 4) if isinstance(cost_basis, (int, float)) else 0.0
            pnl_rounded = round(float(realized_pnl), 2) if isinstance(realized_pnl, (int, float)) else 0.0
        except (ValueError, TypeError) as e:
            self.logger.error(f"Fingerprint creation error: Invalid numeric value - {e}. Shares: {shares_float}, Cost: {cost_basis}, PnL: {realized_pnl}")
            # Return a default or raise? Returning default might hide errors. Let's log and return default.
            shares_int = 0
            cost_rounded = 0.0
            pnl_rounded = 0.0

        fp = (event_type.upper(), shares_int, cost_rounded, pnl_rounded, snapshot_version)

        # Use a local copy of the debug flag to avoid potential race conditions
        debug_mode = self._debug_mode
        if debug_mode:
            self.logger.debug(f"[DEBUG_TM_SERVICE] Created fingerprint: {fp}")

        return fp

    @log_function_call('trade_manager')
    def _is_duplicate_event(self, symbol: str, fingerprint: tuple) -> bool:
        """
        Checks if an event with the given fingerprint has been seen recently.
        Also performs cleanup of expired fingerprints.

        Args:
            symbol: The stock symbol
            fingerprint: The event fingerprint tuple

        Returns:
            bool: True if this is a duplicate event, False otherwise
        """
        symbol = symbol.upper()
        current_time = time.time()
        is_duplicate = False

        with self._fingerprint_lock:
            # Clean up expired fingerprints first
            self._cleanup_expired_fingerprints(current_time)

            # Check if this is a duplicate
            if symbol in self._recent_fingerprints and fingerprint in self._recent_fingerprints[symbol]:
                is_duplicate = True
                self.logger.warning(f"Duplicate event detected for {symbol} with fingerprint: {fingerprint}")
            else:
                # Add the new fingerprint
                if symbol not in self._recent_fingerprints:
                    self._recent_fingerprints[symbol] = set()
                    self._fingerprint_timestamps[symbol] = {}

                self._recent_fingerprints[symbol].add(fingerprint)
                self._fingerprint_timestamps[symbol][fingerprint] = current_time
                self.logger.debug(f"Added new fingerprint for {symbol}: {fingerprint}")

        return is_duplicate

    @log_function_call('trade_manager')
    def _log_performance(self, perf_timestamps: Dict[str, float], context: str) -> None:
        """
        Logs performance metrics from the provided timestamps.

        Args:
            perf_timestamps: Dictionary of performance timestamps
            context: Context identifier for the performance logging

        This method uses the global performance tracker via add_to_stats() to update
        the global stats collector. The global tracker still exists in utils/performance_tracker.py
        and is used by multiple components for centralized performance tracking.
        """
        # Import performance tracking utilities
        try:
            from utils.performance_tracker import is_performance_tracking_enabled, calculate_durations, add_to_stats
            if is_performance_tracking_enabled() and perf_timestamps:
                durations = calculate_durations(perf_timestamps)
                # Updates the global performance tracker in utils/performance_tracker.py
                add_to_stats(perf_timestamps)
                # Log only occasionally
                if durations and random.random() < 0.1:
                    total = durations.get('total_measured', 0)*1000
                    proc_key = f'tm_service.{context.lower()}_start_to_tm_service.{context.lower()}_end'
                    proc_time = durations.get(proc_key, 0)*1000
                    self.logger.info(f"Performance [{context}]: Total={total:.2f}ms, Processing={proc_time:.2f}ms")
        except Exception as e:
            self.logger.warning(f"Error logging performance metrics: {e}")

    @log_function_call('trade_manager')
    def _cleanup_expired_fingerprints(self, current_time: float) -> None:
        """
        Removes fingerprints that have expired based on the configured expiry time.
        Must be called with self._fingerprint_lock held.

        Args:
            current_time: The current time to compare against
        """
        symbols_to_check = list(self._recent_fingerprints.keys())

        for symbol in symbols_to_check:
            # Get fingerprints to remove
            fingerprints_to_remove = []

            for fp, timestamp in list(self._fingerprint_timestamps.get(symbol, {}).items()):
                if current_time - timestamp > self._fingerprint_expiry_seconds:
                    fingerprints_to_remove.append(fp)

            # Remove expired fingerprints
            for fp in fingerprints_to_remove:
                if fp in self._recent_fingerprints.get(symbol, set()):
                    self._recent_fingerprints[symbol].remove(fp)
                if fp in self._fingerprint_timestamps.get(symbol, {}):
                    del self._fingerprint_timestamps[symbol][fp]

            # Clean up empty sets
            if symbol in self._recent_fingerprints and not self._recent_fingerprints[symbol]:
                del self._recent_fingerprints[symbol]
            if symbol in self._fingerprint_timestamps and not self._fingerprint_timestamps[symbol]:
                del self._fingerprint_timestamps[symbol]

        # Log cleanup stats occasionally
        if random.random() < 0.05:  # ~5% of the time
            total_fingerprints = sum(len(fps) for fps in list(self._recent_fingerprints.values()))
            self.logger.debug(f"Fingerprint cache stats: {len(self._recent_fingerprints)} symbols, {total_fingerprints} fingerprints")

    # REMOVED: _send_broker_event_if_new

    # --- Trade Record Management --- (methods removed - now handled by lifecycle manager)

    # _find_active_trade_for_symbol method removed - now handled by lifecycle manager

    # --- Trade Lifecycle Logic --- (methods removed - now handled by lifecycle manager)


    # --- Manual Actions --- (manual_add_shares, force_close_all, force_close_position remain mostly same, but use Executor via helpers)
    @log_function_call('trade_manager')
    def manual_add_shares(self,
                          symbol: Optional[str] = None,
                          # shares_to_add argument is removed as it always uses config.manual_shares
                          completion_callback: Optional[Callable[[bool, str, str], None]] = None
                         ) -> bool:
        """
        Handles a request to manually add shares, triggered by a direct user action (e.g., GUI button).
        ALWAYS uses the quantity specified in self._config.manual_shares.
        This action is INDEPENDENT of the configured 'add_type' for OCR-driven ADDs.

        Args:
            symbol: The stock symbol to add shares to. If None, uses the active symbol.
            completion_callback: Optional callback for GUI updates.

        Returns:
            bool: True if the order was successfully initiated, False otherwise.
        """
        operation_result = False
        result_message = ""
        final_symbol = symbol or "" # For callback

        # Only create timestamp dictionary if performance tracking is enabled
        perf_timestamps = create_timestamp_dict() if is_performance_tracking_enabled() else None

        # Only add timestamps if performance tracking is enabled and perf_timestamps exists
        if is_performance_tracking_enabled() and perf_timestamps:
            add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_start')

        try:
            # Log which value is being used for shares
            shares_to_add_val = float(self._config.manual_shares)
            self.logger.info(
                f"Manual Add Button Action: symbol_arg={symbol}, "
                f"Using config.manual_shares={shares_to_add_val}"
            )

            if not self.is_trading_enabled(): # Assumes this method handles its own locking
                result_message = "Trading is disabled."
                self.logger.warning(f"Manual Add failed: {result_message}")
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_trading_disabled')
                if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                return False

            # 1. Determine Symbol
            if symbol is None:
                symbol = self.get_active_symbol()
                if symbol is None:
                    result_message = "Cannot determine active symbol for Manual Add."
                    self.logger.error(f"Manual Add failed: {result_message}")
                    if is_performance_tracking_enabled() and perf_timestamps:
                        add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_no_active_symbol')
                    if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                    return False
            symbol = symbol.upper()
            final_symbol = symbol # For callback

            # 2. Validate Shares from Config
            if shares_to_add_val <= 0:
                result_message = f"Invalid config.manual_shares value ({shares_to_add_val}). Must be positive."
                self.logger.error(f"Manual Add failed: {result_message}")
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_invalid_config_shares')
                if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                return False

            # 3. Find Active Trade and Position (needed for parent_trade_id)
            pos_data = self._position_manager.get_position(symbol)
            parent_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)

            if parent_trade_id is None or pos_data is None or not pos_data.get("open", False):
                result_message = f"No active trade or position found for {symbol} to manually add to."
                self.logger.error(f"Manual Add failed: {result_message}")
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_no_trade_pos')
                if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                return False

            # 4. Get Reference Price for Limit Order
            ref_price = self._price_provider.get_reliable_price(symbol, side="BUY")
            if ref_price <= 0.01:
                result_message = f"Could not get valid reference price for {symbol} for Manual Add."
                self.logger.error(f"Manual Add failed: {result_message}")
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_no_price')
                if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                return False

            # Calculate a limit price (e.g., ref_price + small offset from config or hardcoded)
            # This offset logic could be from self._config or a utility
            limit_offset = getattr(self._config, 'manual_add_limit_offset_dollars', 0.15) if ref_price >= 2.0 else getattr(self._config, 'manual_add_limit_offset_cents', 0.05)
            manual_limit_price = round(ref_price + limit_offset, 4)

            # 5. Construct OrderParameters Directly
            order_params = OrderParameters(
                symbol=symbol,
                side="BUY",
                quantity=shares_to_add_val,
                order_type="LMT", # Or MKT if preferred and broker supports parameterless MKT
                limit_price=manual_limit_price,
                event_type="MANUAL_ADD", # Specific event type
                parent_trade_id=parent_trade_id, # Link to existing trade
                action_type=TradeActionType.ADD_LONG # Explicitly an ADD_LONG action
            )
            self.logger.info(f"Manual Add for {symbol}: Prepared OrderParameters: {order_params}")
            if is_performance_tracking_enabled() and perf_timestamps:
                add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_params_created')

            # 6. Execute the trade via TradeExecutor
            executed_local_id: Optional[int] = None
            current_time = time.time()
            time_stamps_for_executor = {
                "ocr_raw_time": current_time, # Not OCR-driven, but good to timestamp the action
            }

            # Only add perf_timestamps if performance tracking is enabled and perf_timestamps exists
            if is_performance_tracking_enabled() and perf_timestamps:
                time_stamps_for_executor["perf_timestamps"] = perf_timestamps
            try:
                executed_local_id = self._trade_executor.execute_trade(
                    order_params=order_params,
                    time_stamps=time_stamps_for_executor,
                    snapshot_version="MANUAL_ADD_BUTTON", # Clear source
                    ocr_confidence=100.0, # Indicates manual, high confidence
                    ocr_snapshot_cost_basis=None, # Not based on OCR snapshot's cost
                    extra_fields={"trigger_source": "manual_add_button"},
                    is_manual_trigger=True, # Important for TradeExecutor logic
                    perf_timestamps=perf_timestamps # Pass along
                )
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_executor_called')
            except Exception as e_exec:
                result_message = f"Execution error during Manual Add: {e_exec}"
                self.logger.error(f"Manual Add failed for {symbol}: {result_message}", exc_info=True)
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_executor_exception')
                executed_local_id = None

            if executed_local_id:
                result_message = (f"Manual Add order for {shares_to_add_val} shares of {symbol} "
                                  f"initiated with local_id {executed_local_id}.")
                self.logger.info(f"Manual Add successful: {result_message}")
                if self._gui_logger_func: self._gui_logger_func(result_message, "info")
                # LifecycleManager.update_on_order_submission should be called if parent_trade_id is set in order_params
                # and TradeExecutor successfully creates the order in OrderRepository,
                # which then links it to the trade.
                operation_result = True
            else:
                # If result_message wasn't set by an earlier failure point
                if not result_message:
                    result_message = f"Manual Add for {symbol}: TradeExecutor did not initiate order."
                self.logger.error(result_message)
                if self._gui_logger_func: self._gui_logger_func(result_message, "error")
                operation_result = False

            if is_performance_tracking_enabled() and perf_timestamps:
                add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_end')
            self._log_performance(perf_timestamps, "ManualAddShares")
            return operation_result

        except Exception as e:
            # Catch-all for unexpected errors during the process
            result_message = f"Unexpected error during Manual Add for {final_symbol}: {e}"
            self.logger.error(result_message, exc_info=True)
            if self._gui_logger_func: self._gui_logger_func(result_message, "error")
            if is_performance_tracking_enabled() and perf_timestamps:
                add_timestamp(perf_timestamps, 'tm_service.manual_add_shares_unexpected_exception')
            self._log_performance(perf_timestamps, "ManualAddShares")
            operation_result = False
            return False
        finally:
            # Ensure callback is always called if provided
            if completion_callback:
                try:
                    completion_callback(operation_result, final_symbol, result_message)
                except Exception as cb_err:
                    self.logger.error(f"Error in manual_add_shares completion callback: {cb_err}", exc_info=True)


    # --- Broker Position Polling --- (_start/_stop/_poll/_compare remain same)
    @log_function_call('trade_manager')
    def _start_broker_poll_thread(self, interval_s: float) -> None:
        """
        Starts the background thread for polling broker positions.
        Includes improved handling of existing threads.
        """
        # Check if thread is already running
        if self._poll_thread and self._poll_thread.is_alive():
            self.logger.warning("Broker poll thread already running. Stopping existing thread first.")
            # Stop the existing thread before starting a new one
            self._stop_broker_poll_thread()

        # Validate requirements
        if not self._broker:
             self.logger.warning("Cannot start poll thread: Broker service is not available.")
             return

        if interval_s <= 0:
             self.logger.info("Broker polling disabled (interval <= 0).")
             return

        # Create and start new thread
        self.logger.debug(f"Creating new broker poll thread with interval: {interval_s}s")
        self._poll_stop_event = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_broker_loop,
            args=(interval_s,),
            daemon=True,
            name="BrokerPollThread"
        )
        self._poll_thread.start()

        # Verify thread started successfully
        if self._poll_thread.is_alive():
            self.logger.info(f"Broker position polling thread started successfully (interval: {interval_s}s).")
        else:
            self.logger.error("Failed to start broker poll thread.")
            self._poll_thread = None
            self._poll_stop_event = None

    @log_function_call('trade_manager')
    def _stop_broker_poll_thread(self) -> None:
        """
        Stops the background polling thread.
        Uses a more robust approach to ensure the thread is properly terminated.
        """
        if not self._poll_thread:
            self.logger.debug("No broker poll thread to stop.")
            return

        if self._poll_stop_event:
            self.logger.info("Setting poll stop event for BrokerPollThread.") # Changed to INFO for clarity
            self._poll_stop_event.set()

        if self._poll_thread and self._poll_thread.is_alive():
            # Determine a reasonable join timeout.
            # The poll loop uses a broker_timeout of up to 10s for get_all_positions.
            # Add a small buffer.
            join_timeout = 11.0 # Max broker_timeout (10s) + 1s buffer
            self.logger.info(f"Waiting for BrokerPollThread to stop (timeout: {join_timeout}s)...")
            self._poll_thread.join(timeout=join_timeout)

            if self._poll_thread.is_alive():
                thread_id = self._poll_thread.ident or "N/A"
                self.logger.warning(
                    f"BrokerPollThread (id: {thread_id}) did NOT stop cleanly after {join_timeout}s join timeout. "
                    f"It might be stuck in a broker call or other long operation."
                )
            else:
                self.logger.info("BrokerPollThread joined successfully.")
        else:
            self.logger.info("BrokerPollThread was not running or already stopped.")

        self._poll_thread = None
        # self._poll_stop_event = None # Keep this so it can't be restarted by mistake unless re-initialized
        self.logger.info("Broker position polling thread cleanup completed.")

    @log_function_call('trade_manager')
    def _poll_broker_loop(self, interval_s: float) -> None:
        """
        Background loop to periodically compare internal state with broker.
        Includes improved handling of stop events and timeouts.
        """
        self.logger.info("Broker polling loop started.")

        # Ensure minimum interval and calculate timeout
        safe_interval = max(5.0, interval_s)  # Minimum 5 seconds to prevent excessive polling
        broker_timeout = min(safe_interval - 1.0, 10.0)  # Shorter timeout, max 10 seconds

        if safe_interval != interval_s and interval_s > 0: # Only log if user provided a positive, different interval
            self.logger.warning(f"Broker poll interval input {interval_s:.1f}s was adjusted to {safe_interval:.1f}s (min 5.0s).")

        # Track consecutive failures to detect persistent connection issues
        consecutive_failures = 0
        max_consecutive_failures = 3  # After this many failures, increase wait time
        failure_backoff_interval = 60.0  # Longer interval after repeated failures (1 minute)

        # Import the log_performance_durations function
        from utils.performance_tracker import log_performance_durations

        while not self._poll_stop_event.is_set():
            poll_start_time = time.time()
            current_interval = failure_backoff_interval if consecutive_failures >= max_consecutive_failures else safe_interval

            try:
                # Check stop event before potentially long operation
                if self._poll_stop_event.is_set():
                    break

                perf_ts = create_timestamp_dict()
                add_timestamp(perf_ts, 'tm_service.poll_broker_start')

                # Verify broker connection status
                add_timestamp(perf_ts, 'tm_service.poll_broker_check_connection_start')
                broker_connected = self._broker and self._broker.is_connected()
                add_timestamp(perf_ts, 'tm_service.poll_broker_check_connection_end')

                if not broker_connected:
                    self.logger.warning("Broker not connected. Skipping poll and using backoff interval.")
                    consecutive_failures += 1
                    add_timestamp(perf_ts, 'tm_service.poll_broker_not_connected')
                    # Don't try to poll if broker is not connected
                else:
                    if self._poll_stop_event.is_set(): # Extra check before broker call
                        self.logger.info("BrokerPollThread: Stop event detected just before broker call. Aborting poll cycle.")
                        break

                    # Get actual positions from broker with timeout
                    self.logger.debug(f"Polling broker positions (timeout: {broker_timeout:.1f}s)...")
                    add_timestamp(perf_ts, 'tm_service.poll_broker_get_positions_start')
                    actual_positions_data, fetch_was_successful = self._broker.get_all_positions(
                        timeout=broker_timeout,
                        perf_timestamps=perf_ts
                    )
                    add_timestamp(perf_ts, 'tm_service.poll_broker_get_positions_end')

                    # Check stop event again after potentially long operation
                    if self._poll_stop_event.is_set():
                        break

                    if fetch_was_successful:
                        self.logger.debug(f"Broker positions fetched successfully ({len(actual_positions_data) if actual_positions_data is not None else '0'} positions). Proceeding with comparison.")
                        add_timestamp(perf_ts, 'tm_service.poll_broker_compare_positions_start')
                        self._compare_positions_with_broker(actual_positions_data if actual_positions_data is not None else [], perf_ts)
                        add_timestamp(perf_ts, 'tm_service.poll_broker_compare_positions_end')
                        # Reset failure counter on success
                        consecutive_failures = 0
                    else:
                        self.logger.error(
                            "Broker positions fetch FAILED or timed out. Skipping position comparison for this poll cycle to prevent incorrect reconciliation. "
                            "The PositionManager will retain its current state until a successful broker poll can confirm or adjust it."
                        )
                        consecutive_failures += 1
                        add_timestamp(perf_ts, 'tm_service.poll_broker_fetch_failed')

                        # If we've had multiple failures, check if the broker is actually disconnected
                        if consecutive_failures >= 2 and self._broker:
                            # Force a re-check of the connection status
                            add_timestamp(perf_ts, 'tm_service.poll_broker_recheck_connection_start')
                            if not self._broker.is_connected():
                                self.logger.warning("Detected broker disconnection after multiple poll failures.")
                                add_timestamp(perf_ts, 'tm_service.poll_broker_disconnection_detected')
                                # Could notify the UI or take other actions here
                            add_timestamp(perf_ts, 'tm_service.poll_broker_recheck_connection_end')

                add_timestamp(perf_ts, 'tm_service.poll_broker_end')

                # Log performance metrics
                log_performance_durations(perf_ts, "TradeManagerService_PollBroker", threshold_ms=100.0)

                # Log the current failure state and wait interval
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.warning(f"Using extended poll interval ({failure_backoff_interval}s) due to {consecutive_failures} consecutive failures")

            except Exception as e:
                self.logger.error(f"Error during broker polling loop: {e}", exc_info=True)
                consecutive_failures += 1

            # Calculate remaining wait time
            elapsed = time.time() - poll_start_time
            wait_time = max(0.1, current_interval - elapsed)  # At least 0.1s to prevent CPU spinning

            # Wait for the next interval or until stop event
            self.logger.debug(f"Broker poll completed. Waiting {wait_time:.1f}s until next poll...")
            if self._poll_stop_event.wait(wait_time):
                # If wait returns True, the event was set
                break

        self.logger.info("Broker polling loop ended.")

    @log_function_call('trade_manager')
    def _compare_positions_with_broker(self, actual_broker_positions: list, perf_timestamps: Dict[str, float]) -> None:
        """Compares internal position state with actual broker positions.

        Phase 1 complete: This is less complex than maintaining two fully separate running
        averages within PositionManager while still giving you good diagnostic trails.
        """
        add_timestamp(perf_timestamps, 'tm_service.compare_positions_start')
        current_broker_state_map: Dict[str, Dict[str, Any]] = {}
        for p_data in actual_broker_positions:
            sym = p_data.get("symbol", "").upper()
            if sym:
                realized_pnl_val = p_data.get("realized_pl", p_data.get("realized_pnl"))
                try:
                    realized_pnl_float = float(realized_pnl_val) if realized_pnl_val is not None else None
                except ValueError:
                    realized_pnl_float = None
                    self.logger.warning(f"Could not parse realized_pnl '{realized_pnl_val}' for {sym} during poll. Setting to None.")

                current_broker_state_map[sym] = {
                    "shares": float(p_data.get("qty", p_data.get("shares", 0.0))),
                    "avg_price": float(p_data.get("avg_entry_price", p_data.get("avg_price", 0.0))),
                    "realized_pnl": realized_pnl_float
                }

        pm_snapshot = self._position_manager.get_all_positions_snapshot()
        all_symbols_to_check = set(current_broker_state_map.keys()).union(pm_snapshot.keys())

        for sym in all_symbols_to_check:
            broker_data = current_broker_state_map.get(sym, {"shares": 0.0, "avg_price": 0.0, "realized_pnl": None})
            pm_data = pm_snapshot.get(sym)

            pm_total_sh = pm_data.get("total_shares", 0.0) if pm_data else 0.0
            broker_sh = broker_data["shares"]

            # Also compare avg_price if position is not flat at broker
            pm_total_avg_px = pm_data.get("total_avg_price", 0.0) if pm_data else 0.0
            broker_avg_px = broker_data["avg_price"]

            # Compare realized P&L if available from broker
            pm_overall_realized_pnl = pm_data.get("overall_realized_pnl", 0.0) if pm_data else 0.0
            broker_realized_pnl = broker_data.get("realized_pnl")


            discrepancy_found = False
            if abs(pm_total_sh - broker_sh) > 1e-9:
                discrepancy_found = True
            elif abs(broker_sh) > 1e-9 and abs(pm_total_avg_px - broker_avg_px) > 0.0001: # If shares match but avg price differs
                discrepancy_found = True
            elif broker_realized_pnl is not None and abs(pm_overall_realized_pnl - broker_realized_pnl) > 0.01: # If PnL differs
                 discrepancy_found = True


            if discrepancy_found:
                self.logger.warning(
                    f"RECONCILE ACTION for {sym}: PM(TotalSh={pm_total_sh:.2f}, AvgPx={pm_total_avg_px:.4f}, PnL={pm_overall_realized_pnl:.2f}) vs "
                    f"Broker(Shares={broker_sh:.2f}, AvgPx={broker_avg_px:.4f}, PnL={broker_realized_pnl}). Syncing PM to broker."
                )
                # Get/Create LCM trade and its associated correlation_id
                lcm_parent_trade_id = self._lifecycle_manager.create_or_sync_trade(sym, broker_sh, broker_avg_px, source="POLL_RECONCILE_TMS")
                master_corr_id_for_pm = None
                if lcm_parent_trade_id:
                    trade_info_from_lcm = self._lifecycle_manager.get_trade_by_id(lcm_parent_trade_id)
                    if trade_info_from_lcm:
                        master_corr_id_for_pm = trade_info_from_lcm.get("correlation_id")  # Get IntelliSense UUID from LCM

                self._position_manager.sync_broker_position(
                    sym,
                    broker_sh,
                    broker_avg_px,
                    broker_realized_pnl, # Pass broker's PnL
                    source="poll_reconcile",
                    lcm_parent_trade_id_from_sync=lcm_parent_trade_id,
                    master_correlation_id_from_sync=master_corr_id_for_pm
                )

                # Synchronize OrderRepository._calculated_net_position
                if self._order_repository:
                    self.logger.info(f"TMS Polling: Forcing OR._calculated_net_position for {sym} to {broker_sh}")
                    try:
                        self._order_repository.force_set_calculated_position(sym, broker_sh)
                        self.logger.info(f"TMS Polling: Successfully forced OR._calculated_net_position for {sym}")
                    except Exception as e:
                        self.logger.error(f"TMS Polling: Error forcing OR._calculated_net_position for {sym}: {e}", exc_info=True)

                # Mark symbol as recently synced if it has a non-zero position
                if abs(broker_sh) > 1e-9:
                    with self._sync_grace_lock:
                        self._recently_synced_symbols[sym] = time.time()
                    self.logger.info(f"[{sym}] Marked as recently synced by broker. OCR Close signals will be deferred for a grace period.")

                add_timestamp(perf_timestamps, f'tm_service.compare_positions_discrepancy_reconciled_{sym}')
            # else:
                # self.logger.debug(f"RECONCILE CHECK: Position matches for {sym}")

        add_timestamp(perf_timestamps, 'tm_service.compare_positions_end')


    # --- GUI Data Getters --- (remain largely the same, relying on PositionManager/TradeLifecycleManager data)
    @log_function_call('trade_manager')
    def get_open_position_display(self) -> str:
        """Generates a formatted string for the open trades GUI display."""
        logging.debug("ENTER get_open_position_display")
        output_lines = []
        active_sym = None # Initialize
        active_trade_id = None # Initialize
        try:
            active_sym = self.get_active_symbol()
            if not active_sym:
                return "No open trade position detected.\n"

            # Get position data directly from position manager
            pos_data = self._position_manager.get_position(active_sym) or {}
            if not pos_data:
                return f"*** ERROR: No position data found for {active_sym} ***\n"

            # Get active trade ID from lifecycle manager
            active_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(active_sym)

            # Get prices
            bid_price = 0.0
            ask_price = 0.0
            live_price = 0.0
            try:
                bid_price = self._price_provider.get_bid_price(active_sym) or 0.0
                ask_price = self._price_provider.get_ask_price(active_sym) or 0.0
                live_price = self._price_provider.get_latest_price(active_sym) or 0.0
            except Exception:
                # Continue with zero prices
                pass

            # Format summary section
            # Format Summary Header
            summary_header = f"{'Symbol':<8} {'LocalAvg':>10} {'LocalSh':>8} {'RealPnL':>12} {'Bid':>8} {'Ask':>8} {'Live':>8}\n"
            output_lines.append(summary_header)

            # Format Summary Data
            local_avg = pos_data.get("avg_price", 0.0)
            local_sh = pos_data.get("shares", 0.0)
            real_pnl = pos_data.get("realized_pnl", 0.0)
            summary_data = f"{active_sym:<8} {local_avg:>10.4f} {local_sh:>8.1f} {real_pnl:>12,.2f} {bid_price:>8.2f} {ask_price:>8.2f} {live_price:>8.2f}\n"
            output_lines.append(summary_data)
            output_lines.append("-" * 80 + "\n")  # Adjust width

            # --- START: Unified Position History & Orders Display ---
            # Extract data for position history
            startup_sh_val = pos_data.get("startup_shares")
            startup_px_val = pos_data.get("startup_avg_price")
            last_upd_src = pos_data.get("last_update_source", "---")

            # Extract sync details
            last_sync_details = pos_data.get("last_sync_details", {})
            sync_src = last_sync_details.get("source", "---")
            sync_ts_unix = last_sync_details.get("timestamp", 0.0)
            sync_sh_val = last_sync_details.get("broker_shares_at_sync")
            sync_px_val = last_sync_details.get("broker_avg_price_at_sync")

            # Get orders for this trade
            local_orders_for_trade = []
            if active_trade_id:
                try:
                    if self._order_repository:
                        local_orders_for_trade = self._order_repository.get_orders_for_trade(active_trade_id)
                except Exception as e:
                    self.logger.error(f"Error fetching orders for trade_id={active_trade_id}: {e}", exc_info=True)

            # Create a unified list of all position-related events
            all_position_events = []

            # Add startup position entry
            all_position_events.append({
                'timestamp': 0.0,  # Will be displayed as "STARTUP"
                'source': 'INIT',
                'action': 'STARTUP',
                'shares': startup_sh_val,
                'price': startup_px_val,
                'pnl': None,
                'status': 'COMPLETED',
                'notes': last_upd_src,
                'is_startup': True,
                'is_sync': False,
                'is_fill': False,
                'is_order': False
            })

            # Add broker sync entry if available
            if isinstance(last_sync_details, dict) and last_sync_details:
                sync_pnl_val = last_sync_details.get("broker_realized_pnl_at_sync")
                all_position_events.append({
                    'timestamp': sync_ts_unix,
                    'source': sync_src,
                    'action': 'SYNC',
                    'shares': sync_sh_val,
                    'price': sync_px_val,
                    'pnl': sync_pnl_val,
                    'status': 'COMPLETED',
                    'notes': 'Broker Sync',
                    'is_startup': False,
                    'is_sync': True,
                    'is_fill': False,
                    'is_order': False
                })

            # Add session fills
            session_fills_ledger = pos_data.get("session_system_fills_ledger", [])
            if isinstance(session_fills_ledger, list):
                for fill_entry in session_fills_ledger:
                    fill_ts_unix = fill_entry.get("timestamp", 0.0)
                    fill_type = fill_entry.get("type", "---")
                    fill_sh_val = fill_entry.get("shares")
                    fill_px_val = fill_entry.get("price")
                    fill_pnl_val = fill_entry.get("pnl_contribution", 0.0)
                    fill_local_id = fill_entry.get("local_order_id", "---")
                    fill_source = fill_entry.get("source", "SYSTEM")

                    all_position_events.append({
                        'timestamp': fill_ts_unix,
                        'source': fill_source,
                        'action': fill_type,
                        'shares': fill_sh_val,
                        'price': fill_px_val,
                        'pnl': fill_pnl_val,
                        'status': 'FILLED',
                        'notes': f"ID:{fill_local_id}",
                        'is_startup': False,
                        'is_sync': False,
                        'is_fill': True,
                        'is_order': False
                    })

            # Add orders
            for od in local_orders_for_trade:
                ts_requested = od.get("timestamp_requested", 0.0)
                event_type = od.get("event_type", "---")
                ls_status = od.get("ls_status", "---")
                req_sh = float(od.get("requested_shares", 0.0))
                left_sh = float(od.get("leftover_shares", 0.0))

                # Calculate average fill price
                avg_fill_px = 0.0
                fills = od.get("fills", [])
                if fills:
                    total_shares = sum(f.get("shares_filled", 0.0) for f in fills)
                    total_cost = sum(f.get("shares_filled", 0.0) * f.get("fill_price", 0.0) for f in fills)
                    avg_fill_px = total_cost / total_shares if total_shares > 0 else 0.0

                # Get fill time
                fill_time_str = "---"
                if fills:
                    latest_fill = fills[-1]
                    fill_time_str = latest_fill.get("fill_time_est", "---")

                # Determine source based on event_type
                order_source = "OCR"
                if "MANUAL" in event_type:
                    order_source = "MANUAL"
                elif "FORCE" in event_type:
                    order_source = "FORCE"

                all_position_events.append({
                    'timestamp': ts_requested,
                    'source': order_source,
                    'action': event_type,
                    'shares': req_sh,
                    'price': avg_fill_px if avg_fill_px > 0 else None,
                    'pnl': None,
                    'status': ls_status,
                    'notes': f"Left:{left_sh}",
                    'is_startup': False,
                    'is_sync': False,
                    'is_fill': False,
                    'is_order': True,
                    'fill_time': fill_time_str,
                    'local_id': od.get("local_id", "---")
                })

            # Sort all events by timestamp, with startup always first
            all_position_events.sort(key=lambda x: (0 if x.get('is_startup') else 1, -x.get('timestamp', 0)))

            # Create unified position history table with all actions
            output_lines.append("UNIFIED POSITION HISTORY (All Events):\n")

            # Table header - unified format for all types of events
            header = f"{'Time':<8} | {'Source':<8} | {'Action':<12} | {'Shares':<8} | {'Price':<9} | {'Status':<10} | {'Notes/Details':<20}\n"
            output_lines.append(header)
            output_lines.append("-" * 80 + "\n")

            # Add all events to the output
            for event in all_position_events:
                # Format timestamp
                if event.get('is_startup'):
                    time_str = "STARTUP"
                else:
                    ts = event.get('timestamp', 0.0)
                    time_str = time.strftime('%H:%M:%S', time.localtime(ts)) if ts > 0 else "---"

                # Format shares
                shares_val = event.get('shares')
                shares_str = f"{shares_val:.2f}" if isinstance(shares_val, float) else str(shares_val if shares_val is not None else "---")

                # Format price
                price_val = event.get('price')
                price_str = f"{price_val:.4f}" if isinstance(price_val, float) else str(price_val if price_val is not None else "---")

                # Format source
                source_str = event.get('source', "---")

                # Format action
                action_str = event.get('action', "---")

                # Format status
                status_str = event.get('status', "---")

                # Format notes/details
                notes = event.get('notes', "")

                # Add PnL to notes if available
                pnl_val = event.get('pnl')
                if pnl_val is not None:
                    pnl_str = f"{pnl_val:.2f}" if isinstance(pnl_val, float) else str(pnl_val)
                    notes = f"{notes} PnL:{pnl_str}"

                # Add fill time to notes if it's an order
                if event.get('is_order') and event.get('fill_time') != "---":
                    notes = f"{notes} Fill:{event.get('fill_time')}"

                # Trim notes if too long
                if len(notes) > 20:
                    notes = notes[:17] + "..."

                # Add the line to output
                output_lines.append(f"{time_str:<8} | {source_str:<8} | {action_str:<12} | {shares_str:<8} | {price_str:<9} | {status_str:<10} | {notes:<20}\n")

            output_lines.append("-" * 80 + "\n") # Separator after unified history
            # --- END: Unified Position History & Orders Display ---


        except Exception as e:
            self.logger.exception("Error in get_open_position_display") # Log exception
            output_lines.append(f"*** ERROR GENERATING DISPLAY: {e} ***\n")

        result_string = "".join(output_lines)
        self.logger.debug(f"RETURN get_open_position_display (len={len(result_string)}): {result_string[:100]}...")
        return result_string


    @log_function_call('trade_manager')
    def get_historical_trades_display(self) -> str:
        """Generates a formatted string for the historical trades GUI display."""
        # TODO: Optimize if ledger becomes very large. Consider limiting entries processed (e.g., last 100) or caching formatted output.
        output_lines = []
        ledger_copy = []

        # Get ledger data from lifecycle manager
        try:
            # Get ledger snapshot from lifecycle manager
            ledger_copy = self._lifecycle_manager.get_ledger_snapshot()

            # Reverse the copy
            if ledger_copy:
                ledger_copy = list(reversed(ledger_copy))

        except Exception:
            # Let the decorator handle the logging
            return "*** ERROR ACCESSING LEDGER DATA ***\n"

        # Process each event
        for e in ledger_copy:
            try:
                # Format timestamp
                ts = e.get('timestamp', 0.0)
                ts_str = "--:--:--.---"
                try:
                    if ts:
                        ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S.%f')[:-3]
                except Exception:
                    ts_str = f"ERR:{ts}"

                # Get event fields with safe defaults
                event_type = str(e.get('event', ''))[:6].ljust(6)  # Truncate and pad
                symbol = str(e.get('symbol', ''))[:6].ljust(6)  # Truncate and pad

                # Convert numeric fields
                shares_change = 0.0
                try:
                    shares_change = float(e.get('shares_change', 0.0))
                except (ValueError, TypeError):
                    pass

                new_shares = 0.0
                try:
                    new_shares = float(e.get('new_shares', 0.0))
                except (ValueError, TypeError):
                    pass

                avg_price = 0.0
                try:
                    avg_price = float(e.get('avg_price', 0.0))
                except (ValueError, TypeError):
                    pass

                realized_pnl = 0.0
                try:
                    realized_pnl = float(e.get('realized_pnl', 0.0))
                except (ValueError, TypeError):
                    pass

                comment = str(e.get('comment', ''))

                # Format the line
                line = (f"{ts_str} | {event_type} | sym={symbol}"
                        f" | sh_chg={shares_change:>7.1f} | new_sh={new_shares:>7.1f}"
                        f" | avg_c={avg_price:>9.4f} | rPnL={realized_pnl:>9.2f}"
                        f" | {comment}")
                output_lines.append(line + "\n")

            except Exception:
                # Let the decorator handle the logging
                output_lines.append("*** Error formatting event ***\n")

        # Handle empty output
        if not output_lines:
            output_lines.append("No historical events logged yet.\n")

        # Return result
        try:
            return "".join(output_lines)
        except Exception:
            return "*** ERROR JOINING OUTPUT LINES ***\n"

    @log_function_call('trade_manager')
    def get_all_positions_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Returns a snapshot of all positions from the position manager."""
        return self._position_manager.get_all_positions_snapshot()

    @log_function_call('trade_manager')
    def get_current_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Returns the service's current view of open positions.
        Returns a dictionary with symbol keys and position detail values.
        """
        # Get all positions from position manager
        all_positions = self._position_manager.get_all_positions_snapshot()

        # Filter to only include open positions
        open_positions = {
            symbol: pos_data for symbol, pos_data in all_positions.items()
            if pos_data.get("open", False)
        }

        return open_positions

    @log_function_call('trade_manager')
    def get_position_details(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Returns detailed information for a single position.
        Returns None if the symbol is not currently held.
        """
        symbol = symbol.upper()
        position = self._position_manager.get_position(symbol) or {}

        # Return None if position doesn't exist or isn't open
        if not position or not position.get("open", False):
            return None

        return position

    @log_function_call('trade_manager')
    def get_trading_status(self, symbol: str) -> Dict[str, Any]:
        """
        Returns the trading status (LULD bands, halts) for a symbol.
        """
        symbol = symbol.upper()
        return self._position_manager.get_trading_status(symbol)



    @log_function_call('trade_manager')
    def get_total_pnl(self) -> Tuple[float, float]:
        """
        Calculates and returns the total realized and unrealized P&L.

        Returns:
            Tuple[float, float]: (total_realized_pnl, total_unrealized_pnl)
        """
        total_realized = 0.0
        total_unrealized = 0.0

        # Get all positions
        positions = self._position_manager.get_all_positions_snapshot()

        # Calculate totals
        for symbol, pos in positions.items():
            # Add realized P&L
            total_realized += pos.get("realized_pnl", 0.0)

            # Calculate and add unrealized P&L for open positions
            if pos.get("open", False) and pos.get("shares", 0.0) > 0:
                shares = pos.get("shares", 0.0)
                avg_price = pos.get("avg_price", 0.0)

                # Get current price
                current_price = 0.0
                try:
                    current_price = self._price_provider.get_latest_price(symbol, allow_api_fallback=False) # Pass flag
                    if not current_price or current_price <= 0.01: # Check for None or zero
                        # Try bid price as fallback for unrealized P&L calculation
                        current_price = self._price_provider.get_bid_price(symbol, allow_api_fallback=False) # Pass flag
                except Exception:
                    self.logger.warning(f"Could not get current price for {symbol} when calculating unrealized P&L")

                # Calculate unrealized P&L if we have a valid price
                if current_price > 0.01:
                    unrealized = (current_price - avg_price) * shares
                    total_unrealized += unrealized

        return (total_realized, total_unrealized)

    @log_function_call('trade_manager')
    def get_trade_history(self) -> List[Dict[str, Any]]:
        """
        Returns a history of completed trades.

        Returns:
            List of trade dictionaries with entry/exit details and P&L
        """
        completed_trades = []

        # Get all trades from lifecycle manager
        all_trades = self._lifecycle_manager.get_all_trades_snapshot()
        for _, trade in all_trades.items():
            # Only include closed trades
            if trade.get("lifecycle_state") == LifecycleState.CLOSED and trade.get("end_time") is not None:
                # Trade is already a copy from the snapshot
                completed_trades.append(trade)

        # Sort by end_time (most recent first)
        completed_trades.sort(key=lambda t: t.get("end_time", 0), reverse=True)

        return completed_trades

    @log_function_call('trade_manager')
    def place_manual_order(self, symbol: str, side: str, quantity: float,
                          order_type: str = "MKT", limit_price: Optional[float] = None) -> Optional[int]:
        """
        Places a manual order through the broker.

        Args:
            symbol: The stock symbol
            side: "BUY" or "SELL"
            quantity: Number of shares to trade
            order_type: Order type ("MKT" or "LMT")
            limit_price: Limit price (required for LMT orders)

        Returns:
            local_id if successful, None otherwise
        """
        with self._trading_enabled_lock:
            if not self._is_trading_enabled:
                self.logger.warning(f"Manual order failed: Trading is disabled")
                return None

        symbol = symbol.upper()
        side = side.upper()

        if quantity <= 0:
            self.logger.warning(f"Manual order failed: Invalid quantity {quantity}")
            return None

        if order_type == "LMT" and (limit_price is None or limit_price <= 0):
            self.logger.warning(f"Manual order failed: Limit price required for LMT orders")
            return None

        # Find or create a trade ID for this order
        parent_trade_id = None

        # For BUY orders on a new symbol, create a new trade
        if side == "BUY" and not self._lifecycle_manager.find_active_trade_for_symbol(symbol):
            parent_trade_id = self._lifecycle_manager.create_new_trade(
                symbol,
                origin="MANUAL_ORDER",
                correlation_id=None,  # Manual orders don't have IntelliSense correlation
                initial_comment="Manual order"
            )
            self.logger.debug(f"Created new trade {parent_trade_id} for manual BUY order for {symbol}")

        # For existing positions, find the active trade
        else:
            parent_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)

            # If SELL but no active trade, we can't proceed
            if side == "SELL" and parent_trade_id is None:
                self.logger.warning(f"Manual SELL order failed: No active trade found for {symbol}")
                return None

            # If BUY but no active trade was found earlier, create one now
            if parent_trade_id is None:
                parent_trade_id = self._lifecycle_manager.create_new_trade(symbol)
                # Update comment via lifecycle manager
                self._lifecycle_manager.update_trade_state(
                    trade_id=parent_trade_id,
                    comment="Manual order"
                )
                self.logger.debug(f"Created new trade {parent_trade_id} for manual BUY order for {symbol} (fallback)")

        # Create performance timestamps only if performance tracking is enabled
        perf_timestamps = create_timestamp_dict() if is_performance_tracking_enabled() else None

        # Create the order in the repository
        try:
            # Get position data with None check
            position_data = self._position_manager.get_position(symbol) or {}
            is_position_open = position_data.get("open", False)
            current_shares = position_data.get("shares", 0.0)

            event_type = "OPEN" if side == "BUY" and not is_position_open else \
                         "ADD" if side == "BUY" else \
                         "CLOSE"

            # Determine TradeActionType for manual order
            manual_action_type: Optional[TradeActionType] = None
            if side == "BUY":
                manual_action_type = TradeActionType.ADD_LONG if is_position_open else TradeActionType.OPEN_LONG
            elif side == "SELL":
                if is_position_open:
                    # For a manual sell, if it's against an existing position,
                    # it's generally a CLOSE_POSITION intent.
                    manual_action_type = TradeActionType.CLOSE_POSITION
                else:
                    # Selling with no position. This could be an attempt to OPEN_SHORT.
                    self.logger.warning(f"[{symbol}] Manual SELL order for {quantity} shares but no open position. Action type undetermined.")

            local_id = self._order_repository.create_copy_order(
                symbol=symbol,
                side=side,
                event_type=event_type,
                requested_shares=quantity,
                req_time=time.time(),
                parent_trade_id=parent_trade_id,
                requested_lmt_price=limit_price,
                action_type=manual_action_type, # Set the action_type
                comment=f"Manual {order_type} order",
                perf_timestamps=perf_timestamps
            )

            if local_id is None:
                self.logger.error(f"Manual order failed: Error creating order in repository")
                return None

            # Associate local_id with trade via lifecycle manager
            self._lifecycle_manager.update_on_order_submission(parent_trade_id, local_id)
            self.logger.debug(f"Associated manual order local_id {local_id} with trade_id {parent_trade_id} via LifecycleManager")

            # Send the order to the broker
            if order_type == "MKT":
                self._broker.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=int(quantity),
                    order_type="MKT",
                    local_order_id=local_id,
                    time_in_force="DAY",
                    outside_rth=True
                )
            else:  # LMT order
                self._broker.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=int(quantity),
                    order_type="LMT",
                    limit_price=limit_price,
                    local_order_id=local_id,
                    time_in_force="DAY",
                    outside_rth=True
                )

            self.logger.info(f"Manual {order_type} order placed: {symbol} {side} {quantity} shares, local_id={local_id}")
            return local_id

        except Exception as e:
            self.logger.error(f"Error placing manual order: {e}", exc_info=True)
            return None

    @log_function_call('trade_manager')
    def force_close_single_symbol_and_halt_system(self, symbol: str, reason: str = "Manual Emergency Close") -> bool:
        """
        Emergency close for a SINGLE symbol with complete system halt.

        This method provides a targeted emergency response for a specific symbol while
        still ensuring system safety by halting all automated activity. It's designed for
        situations where you need to close a specific position urgently but want to prevent
        any further automated trading.

        This method follows the standardized emergency sequence:
        1. Stops OCR processing to prevent new signals
        2. Halts all automated trading to prevent new orders
        3. Adds symbol to post-meltdown hold to block new OPEN signals
        4. Verifies position existence for the specified symbol
        5. Aggressively liquidates the position with market chase
        6. Notifies GUI via emergency callback (purely informational)
        7. Stops risk monitoring for the symbol

        Typically triggered by:
        - GUI "Emergency Close" button for the current symbol
        - Manual intervention for a specific problematic position

        This method differs from:
        - force_close_all_and_halt_system: Closes ALL positions
        - force_close_position: Only closes one position without halting the system
        - _handle_critical_risk_event: Automated response triggered by risk detection

        Args:
            symbol: The symbol to liquidate
            reason: The reason for the emergency close

        Returns:
            bool: True if liquidation was successfully initiated, False otherwise
        """
        symbol_upper = symbol.upper()
        self.logger.warning(f"EMERGENCY CLOSE for {symbol_upper}! Reason: {reason}. Initiating halt and liquidation.")

        # 1. Stop OCR Processing (immediately)
        if self._ocr_service:
            try:
                if self._ocr_service.get_status().get("ocr_active"):
                    self.logger.info(f"Stopping OCR service due to emergency close for {symbol_upper}.")
                    self._ocr_service.stop_ocr()
                    # The on_status_update callback from OCRService will update the GUI OCR button
                else:
                    self.logger.info(f"OCR service already stopped while handling emergency close for {symbol_upper}.")
            except Exception as e_ocr_stop:
                self.logger.error(f"Error stopping OCR service for {symbol_upper}: {e_ocr_stop}", exc_info=True)
        else:
            self.logger.warning(f"OCRService not available, cannot stop OCR for {symbol_upper}.")

        # 2. Halt Automated Trading (globally and immediately)
        self.logger.warning(f"Halting ALL automated OCR-driven trading due to emergency close for {symbol_upper}.")
        self.set_trading_enabled(False)  # This also updates GUI button via its logic

        # 3. Set symbol-specific post-meltdown hold
        with self._post_meltdown_hold_lock:
            self._symbols_on_post_meltdown_hold.add(symbol_upper)
        self.logger.warning(f"[{symbol_upper}] Placed on 'post-meltdown hold'. New OPEN signals will be blocked until trading is manually resumed.")

        # 4. Verify we still hold an open position
        position = self._position_manager.get_position(symbol_upper) or {}
        if not position.get("open", False) or abs(position.get("shares", 0.0)) < 1e-9:
            self.logger.warning(f"No open position found for {symbol_upper}. Nothing to close.")
            return False

        # 5. Use the consolidated method for aggressive liquidation
        liquidation_success = self._initiate_aggressive_liquidation_for_symbol(
            symbol=symbol_upper,
            reason_detail=reason,
            aggressive_chase=True
        )

        # 6. Notify GUI via emergency callback (if one is set) - purely informational
        if self._emergency_force_close_callback:
            try:
                self.logger.info(f"Invoking GUI emergency_force_close_callback for {symbol_upper} (informational only).")
                self._emergency_force_close_callback(symbol_upper, f"EMERGENCY CLOSE: {reason}")
            except Exception as e_gui_cb:
                self.logger.error(f"Error in GUI emergency_force_close_callback: {e_gui_cb}")

        if not liquidation_success:
            self.logger.error(f"Emergency liquidation failed or did not initiate for {symbol_upper}.")
        else:
            self.logger.info(f"Emergency liquidation process initiated for {symbol_upper}.")

        # 7. Update risk monitoring for this symbol (stop monitoring)
        self._update_risk_monitoring_for_symbol(symbol_upper, should_monitor=False)

        return liquidation_success

    @log_function_call('trade_manager')
    def force_close_position(self, symbol: str, reason: str = "Manual Close No Halt") -> bool:
        """
        Legacy method to close a single position WITHOUT halting the system.

        WARNING: This method only liquidates the specified position but does NOT halt
        trading or stop OCR processing. This can be risky in OCR copy trading as it may
        lead to conflicts between manual actions and automated OCR signals.

        This method:
        1. Verifies position existence for the specified symbol
        2. Aggressively liquidates the position with market chase
        3. Does NOT halt trading or stop OCR (key difference from safer methods)
        4. Does NOT place the symbol on post-meltdown hold

        Typically triggered by:
        - Legacy code or interfaces
        - Situations where continued trading is desired despite closing one position

        This method differs from:
        - force_close_single_symbol_and_halt_system: Safer version that halts the system
        - force_close_all_and_halt_system: Closes ALL positions and halts the system
        - _handle_critical_risk_event: Automated response with system halt

        For safer operation in OCR copy trading, use force_close_single_symbol_and_halt_system instead.

        Args:
            symbol: The symbol to liquidate
            reason: The reason for the close

        Returns:
            bool: True if liquidation was successfully initiated or no position exists, False otherwise
        """
        symbol_upper = symbol.upper()

        # Add prominent WARNING logs at the beginning
        self.logger.warning("=" * 80)
        self.logger.warning(f"CAUTION: Using force_close_position for {symbol_upper} - SYSTEM-WIDE OCR AND AUTOMATED TRADING WILL CONTINUE RUNNING!")
        self.logger.warning(f"This can lead to CONFLICTING ACTIONS in an OCR copy trading system as new signals may be processed!")
        self.logger.warning(f"For safer operation, use force_close_single_symbol_and_halt_system instead.")
        self.logger.warning("=" * 80)

        self.logger.warning(f"Force closing position for {symbol_upper} WITHOUT halting system. Reason: {reason}")

        # Check if we have an open position for this symbol
        # (Using PositionManager as source of truth for current holdings)
        pos_data = self._position_manager.get_position(symbol_upper)
        current_shares = pos_data.get("shares", 0.0) if pos_data and pos_data.get("open") else 0.0

        if abs(current_shares) < 1e-9:
            self.logger.warning(f"No open position found in PositionManager for {symbol_upper}. Nothing to close via force_close_position.")
            return True  # Effectively successful as there's nothing to do

        # Call the internal liquidation method with aggressive_chase=True
        initiated = self._initiate_aggressive_liquidation_for_symbol(
            symbol=symbol_upper,
            reason_detail=f"FC_POS_ONLY_{reason}",
            aggressive_chase=True
        )

        if initiated:
            self.logger.info(f"Liquidation initiated for {symbol_upper} without halting system.")
        else:
            self.logger.error(f"Failed to initiate liquidation for {symbol_upper}.")

        # Risk monitoring is now handled by LCM callbacks

        # Return success status for consistency with other liquidation methods
        return initiated

    @log_function_call('trade_manager')
    def get_ledger_snapshot(self) -> List[Dict[str, Any]]:
        """Returns a copy of the ledger for display/analysis."""
        # Delegate to the lifecycle manager which now manages the ledger
        return self._lifecycle_manager.get_ledger_snapshot()




    @log_function_call('trade_manager')
    def _update_single_gui_position(self):
         """Updates the simplified _single_gui_position dict."""
         # Get active symbol
         active_sym = self.get_active_symbol()

         # Update the single GUI position with thread safety
         with self._single_gui_position_lock:
             if active_sym:
                 pos_data = self._position_manager.get_position(active_sym) or {}
                 if pos_data.get("open", False):
                     self._single_gui_position = {
                         "symbol": active_sym,
                         "shares": pos_data.get("shares", 0.0),
                         "avg_cost": pos_data.get("avg_price", 0.0),
                         "realized_pnl": pos_data.get("realized_pnl", 0.0),
                         "open": True
                     }
                 else:
                     self._single_gui_position = {"open": False}
             else:
                 self._single_gui_position = {"open": False}


    @log_function_call('trade_manager')
    def get_simple_position_summary(self) -> Dict[str, Any]:
         """Returns the simplified single position summary."""
         # Use lock for thread-safe access to _single_gui_position
         with self._single_gui_position_lock:
             # Deep copy while holding the lock to ensure consistent state
             return copy.deepcopy(self._single_gui_position)





    @log_function_call('trade_manager')
    def get_active_symbol(self) -> Optional[str]:
        """Gets the currently active symbol (if any)."""
        # Get all positions from the position manager
        positions_snapshot = self._position_manager.get_all_positions_snapshot()

        # Find the first position that is open
        for symbol, pos in positions_snapshot.items():
            if pos.get("open", False):
                return symbol

        # If no open position, return the symbol from the single GUI position if it exists
        # and verify it against the position manager's state
        with self._single_gui_position_lock:
            if self._single_gui_position and self._single_gui_position.get("symbol"):
                fallback_symbol = self._single_gui_position.get("symbol")
                # Verify this symbol exists in the position manager (even if not open)
                if fallback_symbol in positions_snapshot:
                    return fallback_symbol

        return None


    # --- Risk Management is now handled by LCM callbacks ---
    @log_function_call('trade_manager')
    def _update_risk_monitoring_for_symbol(self, symbol: str, should_monitor: bool = False):
        """
        Updates risk monitoring for a symbol by calling the appropriate RiskManagementService method.

        Args:
            symbol: The symbol to update risk monitoring for
            should_monitor: If True, start monitoring; if False, stop monitoring
        """
        symbol_upper = symbol.upper()
        if self._risk_service:
            try:
                if should_monitor and hasattr(self._risk_service, 'start_monitoring_holding'):
                    self.logger.info(f"TMS: Requesting RMS to START monitoring for {symbol_upper} (via _update_risk_monitoring).")
                    self._risk_service.start_monitoring_holding(symbol_upper)
                elif not should_monitor and hasattr(self._risk_service, 'stop_monitoring_holding'):
                    self.logger.info(f"TMS: Requesting RMS to STOP monitoring for {symbol_upper} (via _update_risk_monitoring).")
                    self._risk_service.stop_monitoring_holding(symbol_upper)
            except Exception as e:
                self.logger.error(f"Error updating risk monitoring for {symbol_upper}: {e}", exc_info=True)
        else:
            self.logger.debug(f"TMS: Cannot update risk monitoring for {symbol_upper}, RiskService not available.")

    # --- Performance Logging Helper ---
    @log_function_call('trade_manager')
    def _log_performance(self, perf_timestamps: Dict[str, float], context: str) -> None:
         """
         Calculates and logs performance metrics if enabled.

         This method uses the global performance tracker via add_to_stats() to update
         the global stats collector. The global tracker still exists in utils/performance_tracker.py
         and is used by multiple components for centralized performance tracking.
         """
         # Import check function
         try:
             from utils.performance_tracker import is_performance_tracking_enabled
             if is_performance_tracking_enabled() and perf_timestamps:
                 durations = calculate_durations(perf_timestamps)
                 # TEMPORARILY COMMENTED OUT: Updates the global performance tracker in utils/performance_tracker.py
                 # add_to_stats(perf_timestamps)
                 # Log only occasionally
                 if durations and random.random() < 0.1:
                      total = durations.get('total_measured', 0)*1000
                      proc_key = f'tm_service.{context.lower()}_start_to_tm_service.{context.lower()}_end'
                      proc = durations.get(proc_key, 0)*1000
                      self.logger.debug(f"Perf {context}: total={total:.2f}ms, processing={proc:.2f}ms")
         except ImportError:
              pass # Performance tracking not available
         except Exception as e:
              self.logger.warning(f"Error calculating performance metrics in {context}: {e}")

    @log_function_call('trade_manager')
    def handle_manual_broker_event(self, symbol: str, side: str, status: str,
                                   fill_shares: float, fill_price: float, fill_time: float,
                                   _requested_shares: float, local_id: int, _ls_order_id: Optional[int]) -> None:
        """
        Handles events originating from manual broker orders (local_id >= 100).
        Determines if it's effectively an OPEN, ADD, or CLOSE for internal tracking.
        Creates/updates trade records and position state accordingly.
        """
        # Note: Parameters _requested_shares and _ls_order_id are not used in this method but kept for interface compatibility.
        symbol = symbol.upper()
        side = side.upper()
        self.logger.info(f"Handling manual broker event: local_id={local_id}, {symbol} {side} {status}, fill={fill_shares}@{fill_price}")

        event_type = "MANUAL" # Default internal event type
        t_id: Optional[int] = None

        # Get position data with None check
        pos = self._position_manager.get_position(symbol) or {}
        is_currently_open = pos.get("open", False) and pos.get("shares", 0.0) > 1e-9

        # Determine effective event type for internal logic
        if side == "BUY":
            event_type = "ADD" if is_currently_open else "OPEN"
        elif side == "SELL":
            event_type = "CLOSE" if is_currently_open else "MANUAL_SELL_NO_POS" # Or handle shorts differently?

        self.logger.debug(f"Manual event mapped to internal type: {event_type}")

        # Find or create trade ID using lifecycle manager
        if event_type == "OPEN":
            t_id = self._lifecycle_manager.create_new_trade(
                symbol,
                origin="MANUAL_ORDER_AUTO",
                correlation_id=None,  # Manual orders don't have IntelliSense correlation
                initial_comment=f"Auto-created for manual order {local_id}"
            )
            self.logger.debug(f"Created new trade {t_id} for manual OPEN order for {symbol}")
        elif event_type in ("ADD", "CLOSE"):
            t_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)
            if t_id is None:
                # If ADD/CLOSE but no active trade, create one to associate the order
                t_id = self._lifecycle_manager.create_new_trade(symbol)
                # Update state and comment via lifecycle manager
                self._lifecycle_manager.update_trade_state(
                    trade_id=t_id,
                    new_state=LifecycleState.OPEN_ACTIVE,
                    comment=f"Auto-created for manual {event_type} order {local_id} (no prior active trade)"
                )
                self.logger.debug(f"Created new trade {t_id} for manual {event_type} order for {symbol} (no prior active trade)")
        else: # Manual sell with no position - don't create trade?
            self.logger.warning(f"Manual SELL event {local_id} for {symbol} but no open position found. Ignoring for trade tracking.")

        # Associate local_id with trade_id if found/created
        if t_id is not None:
            # Use lifecycle manager to associate order with trade
            self._lifecycle_manager.update_on_order_submission(t_id, local_id)
            self.logger.debug(f"Associated manual order local_id {local_id} with trade_id {t_id} via LifecycleManager")

        # Process fill if applicable (updates position state)
        if status == "Filled" and fill_shares > 0:
            try:
                self._position_manager.process_fill(
                    symbol=symbol, side=side, shares_filled=fill_shares, fill_price=fill_price,
                    local_id=local_id, fill_time=fill_time
                )
                self.logger.debug(f"Processed fill for manual order {local_id}: {fill_shares} shares @ {fill_price}")

                # Add fill via LifecycleManager (which also handles recalculation)
                if t_id is not None:
                    try:
                        fill_record = {
                            "local_id": local_id,
                            "side": side,
                            "shares_filled": fill_shares, # Use correct key name
                            "fill_price": fill_price,     # Use correct key name
                            "time": fill_time,          # Use correct key name
                            # Add other fill details if available/needed (e.g., fill_time_est)
                        }
                        self._lifecycle_manager.update_trade_state(trade_id=t_id, add_fill=fill_record)
                        self.logger.debug(f"Added manual fill for trade {t_id} via update_trade_state.")
                    except Exception as e_add_fill:
                        self.logger.error(f"Error adding manual fill for trade {t_id} via lifecycle manager: {e_add_fill}", exc_info=True)

                # Update the single GUI position
                self._update_single_gui_position()

                # Update risk monitoring for this symbol based on position state
                # If we have an open position, start monitoring; otherwise stop monitoring
                pos_after_fill = self._position_manager.get_position(symbol) or {}
                should_monitor = pos_after_fill.get("open", False) and abs(pos_after_fill.get("shares", 0.0)) > 1e-9
                self._update_risk_monitoring_for_symbol(symbol, should_monitor=should_monitor)

            except Exception as pm_fill_err:
                self.logger.error(f"Error calling PositionManager.process_fill for manual event {local_id}: {pm_fill_err}", exc_info=True)

        # Update trade lifecycle if the manual order finalized
        if status in ("Filled", "Cancelled", "Rejected") and t_id is not None:
            self.logger.debug(f"Manual order {local_id} finalized ({status}). Calling LifecycleManager.handle_order_finalized for trade {t_id}.")
            try:
                self._lifecycle_manager.handle_order_finalized(
                    local_id=local_id,
                    symbol=symbol,
                    event_type=event_type, # The mapped event_type (OPEN, ADD, CLOSE)
                    final_status=status,
                    parent_trade_id=t_id
                )
            except Exception as e_lf_manual:
                self.logger.error(f"Error calling LifecycleManager.handle_order_finalized for manual event {local_id}, trade {t_id}: {e_lf_manual}", exc_info=True)




    @log_function_call('trade_manager')
    def process_ocr_snapshots(self, snapshots: Dict[str, Dict[str, Any]], perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        """
        DEPRECATED: This method is superseded by OCRScalpingSignalOrchestratorService for OCR scalping flow.

        Processes the structured data snapshots derived from OCR.
        Compares snapshots to internal state and decides on trade actions.
        This is a wrapper around process_snapshots for interface compatibility.

        NOTE: This method is now only for non-OCR signal sources that directly push snapshot data.
        The primary OCR scalping flow now uses ValidatedOrderRequestEvent handling.

        Args:
            snapshots: Dictionary of symbol -> snapshot data
            perf_timestamps: Optional performance tracking timestamps
        """
        # PROMINENT DEPRECATION WARNING
        self.logger.warning("DEPRECATED: process_ocr_snapshots called. This path is for non-OCR-event signals ONLY. "
                           "OCR scalping now uses ValidatedOrderRequestEvent flow via OCRScalpingSignalOrchestratorService.")

        # Create time_stamps dict with perf_timestamps if provided, but only if perf_timestamps exists
        time_stamps = {"perf_timestamps": perf_timestamps} if perf_timestamps else {}

        # --- BEGIN MODIFICATION 1.4: Remove core_logic_manager from this call ---
        self.process_snapshots(snapshots, time_stamps)
        # --- END MODIFICATION 1.4 ---

    @log_function_call('trade_manager') # Decorator restored
    def process_snapshots(self, snapshots: Dict[str, Dict[str, Any]], time_stamps: Optional[dict] = None) -> None:
        """
        DEPRECATED: This method is superseded by OCRScalpingSignalOrchestratorService for OCR scalping flow.

        Process a batch of snapshots for multiple symbols.

        NOTE: This method is now only for non-OCR signal sources that directly push snapshot data.
        The primary OCR scalping flow now uses ValidatedOrderRequestEvent handling via _handle_validated_order_request.

        Args:
            snapshots: Dictionary mapping symbols to their snapshot data
            time_stamps: Optional dictionary of timestamps from the OCR process
        """
        self.logger.warning("DEPRECATED: process_snapshots called. OCR scalping now uses ValidatedOrderRequestEvent flow.")
        # --- START OF RESTORED ORIGINAL BODY ---
        # Initialize performance tracking
        from utils.performance_tracker import create_timestamp_dict, add_timestamp # Import moved inside

        # Extract perf_timestamps from time_stamps if it exists, otherwise create a new one
        perf_timestamps = {} # Initialize as empty dict
        if time_stamps and "perf_timestamps" in time_stamps:
            perf_timestamps = time_stamps["perf_timestamps"]

        # Only create a new dictionary if performance tracking is enabled
        if not perf_timestamps and is_performance_tracking_enabled(): # Check if tracking is enabled
            perf_timestamps = create_timestamp_dict()
            if time_stamps is not None: # If time_stamps dict itself exists, add our new perf_timestamps to it
                time_stamps["perf_timestamps"] = perf_timestamps
            elif time_stamps is None and snapshots: # If time_stamps is None but we have snapshots, create it
                time_stamps = {"perf_timestamps": perf_timestamps}


        # Only add timestamps if performance tracking is enabled and perf_timestamps exists
        if is_performance_tracking_enabled() and perf_timestamps:
            add_timestamp(perf_timestamps, 'tm_service.process_snapshots_start')

        # Process each symbol's snapshot
        for symbol, new_state in snapshots.items():
            symbol_start_time = time.perf_counter()
            self.logger.debug(f"Processing symbol {symbol}...")
            try:
                # Only add timestamps if performance tracking is enabled and perf_timestamps exists
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, f'tm_service.symbol.{symbol}.processing_start')
                # Call the internal _process_single_snapshot
                self._process_single_snapshot(symbol, new_state, time_stamps, perf_timestamps)
                # Only add timestamps if performance tracking is enabled and perf_timestamps exists
                if is_performance_tracking_enabled() and perf_timestamps:
                    add_timestamp(perf_timestamps, f'tm_service.symbol.{symbol}.processing_end')
                symbol_duration_ms = (time.perf_counter() - symbol_start_time) * 1000
                self.logger.debug(f"Processed symbol {symbol} in {symbol_duration_ms:.2f}ms")
            except Exception as e:
                self.logger.error(f"Error processing snapshot for {symbol}: {e}", exc_info=True)

        # Only add timestamps if performance tracking is enabled and perf_timestamps exists
        if is_performance_tracking_enabled() and perf_timestamps:
            add_timestamp(perf_timestamps, 'tm_service.process_snapshots_end')
        self._log_performance(perf_timestamps, "ProcessSnapshots")

        # Schedule GUI update with batch summary
        if self._core_logic_manager_ref:
            try:
                # Create summary data
                summary_data = {
                    "status": "OCR Batch Processed",
                    "num_snapshots": len(snapshots),
                    "timestamp": time.time()
                }

                # Try both method names to ensure compatibility
                primary_method_name = 'update_ocr_batch_status_display'
                fallback_method_name = 'update_ocr_batch_status'

                # Add detailed logging to help diagnose the issue
                print(f"TMS: Attempting to schedule GUI update for OCR batch status")
                self.logger.warning(f"TMS: Attempting to schedule GUI update for OCR batch status")
                print(f"TMS: CoreLogicManager ref exists: {self._core_logic_manager_ref is not None}")
                self.logger.warning(f"TMS: CoreLogicManager ref exists: {self._core_logic_manager_ref is not None}")

                # Check if CoreLogicManager has the schedule_gui_update method
                if not hasattr(self._core_logic_manager_ref, 'schedule_gui_update'):
                    print(f"TMS: CoreLogicManager does not have schedule_gui_update method!")
                    self.logger.error(f"TMS: CoreLogicManager does not have schedule_gui_update method!")
                    return

                # Check if CoreLogicManager has the _app_instance_ref attribute
                if not hasattr(self._core_logic_manager_ref, '_app_instance_ref'):
                    print(f"TMS: CoreLogicManager does not have _app_instance_ref attribute!")
                    self.logger.error(f"TMS: CoreLogicManager does not have _app_instance_ref attribute!")
                    return

                # Check if CoreLogicManager._app_instance_ref is None
                if self._core_logic_manager_ref._app_instance_ref is None:
                    print(f"TMS: CoreLogicManager._app_instance_ref is None!")
                    self.logger.error(f"TMS: CoreLogicManager._app_instance_ref is None!")
                    return

                # Add direct checks to see what's happening
                app_instance = self._core_logic_manager_ref._app_instance_ref
                print(f"TMS: Direct check - App instance ID: {id(app_instance)}")
                self.logger.warning(f"TMS: Direct check - App instance ID: {id(app_instance)}")
                print(f"TMS: Direct check - App instance type: {type(app_instance)}")
                self.logger.warning(f"TMS: Direct check - App instance type: {type(app_instance)}")

                # Check if App instance has either of the methods
                has_primary_method = hasattr(app_instance, primary_method_name)
                has_fallback_method = hasattr(app_instance, fallback_method_name)

                print(f"TMS: Direct check - hasattr for primary method: {has_primary_method}")
                self.logger.warning(f"TMS: Direct check - hasattr for primary method: {has_primary_method}")
                print(f"TMS: Direct check - hasattr for fallback method: {has_fallback_method}")
                self.logger.warning(f"TMS: Direct check - hasattr for fallback method: {has_fallback_method}")

                # Try to get the methods directly
                try:
                    primary_method = getattr(app_instance, primary_method_name, None)
                    fallback_method = getattr(app_instance, fallback_method_name, None)
                    print(f"TMS: Direct check - getattr for primary method: {primary_method is not None}, type={type(primary_method) if primary_method else None}")
                    self.logger.warning(f"TMS: Direct check - getattr for primary method: {primary_method is not None}, type={type(primary_method) if primary_method else None}")
                    print(f"TMS: Direct check - getattr for fallback method: {fallback_method is not None}, type={type(fallback_method) if fallback_method else None}")
                    self.logger.warning(f"TMS: Direct check - getattr for fallback method: {fallback_method is not None}, type={type(fallback_method) if fallback_method else None}")
                except Exception as e:
                    print(f"TMS: Exception during direct getattr: {e}")
                    self.logger.error(f"TMS: Exception during direct getattr: {e}")

                if not has_primary_method and not has_fallback_method:
                    print(f"TMS: App instance does not have either {primary_method_name} or {fallback_method_name} methods!")
                    self.logger.error(f"TMS: App instance does not have either {primary_method_name} or {fallback_method_name} methods!")
                    # Log the available methods on the App instance to help diagnose the issue
                    app_methods = [method for method in dir(app_instance)
                                  if callable(getattr(app_instance, method))
                                  and not method.startswith('_')]
                    print(f"TMS: Available App methods: {app_methods}")
                    self.logger.info(f"TMS: Available App methods: {app_methods}")

                    # Log similar methods to help diagnose the issue
                    similar_methods = [method for method in app_methods if 'ocr' in method.lower()]
                    if similar_methods:
                        print(f"TMS: Similar OCR-related methods: {similar_methods}")
                        self.logger.info(f"TMS: Similar OCR-related methods: {similar_methods}")
                    return

                # Choose which method to call
                method_name_to_call = primary_method_name if has_primary_method else fallback_method_name
                print(f"TMS: Using method '{method_name_to_call}' for GUI update")
                self.logger.info(f"TMS: Using method '{method_name_to_call}' for GUI update")

                # Schedule the GUI update
                print(f"TMS: Scheduling GUI update for OCR batch with {len(snapshots)} snapshots")
                self.logger.debug(f"TMS: Scheduling GUI update for OCR batch with {len(snapshots)} snapshots")
                self._core_logic_manager_ref.schedule_gui_update(
                    method_name_to_call,
                    summary_data
                )
            except Exception as e:
                print(f"TMS: Error scheduling GUI update: {e}")
                self.logger.error(f"TMS: Error scheduling GUI update: {e}", exc_info=True)

        # --- BEGIN MODIFICATION 1.6: GUI update after processing all snapshots ---
        if self._core_logic_manager_ref:
            # Create a summary of the processed OCR batch
            summary_data = {
                "status": "OCR Batch Processed",
                "num_snapshots": len(snapshots),
                "timestamp": time.time(),
                "symbols_processed": list(snapshots.keys())
            }

            # Only attempt to schedule the GUI update if we have a valid CoreLogicManager reference
            try:
                # Try both method names to ensure compatibility
                primary_method_name = 'update_ocr_batch_status_display'
                fallback_method_name = 'update_ocr_batch_status'

                # Add detailed logging to help diagnose the issue
                self.logger.info(f"TMS: Attempting to schedule GUI update for OCR batch status")
                self.logger.info(f"TMS: CoreLogicManager ref exists: {self._core_logic_manager_ref is not None}")

                # Check if CoreLogicManager has the schedule_gui_update method
                if not hasattr(self._core_logic_manager_ref, 'schedule_gui_update'):
                    self.logger.error(f"TMS: CoreLogicManager does not have schedule_gui_update method!")
                    return

                # Check if CoreLogicManager has the _app_instance_ref attribute
                if not hasattr(self._core_logic_manager_ref, '_app_instance_ref'):
                    self.logger.error(f"TMS: CoreLogicManager does not have _app_instance_ref attribute!")
                    return

                # Check if CoreLogicManager._app_instance_ref is None
                if self._core_logic_manager_ref._app_instance_ref is None:
                    self.logger.error(f"TMS: CoreLogicManager._app_instance_ref is None!")
                    return

                # Add direct checks to see what's happening
                app_instance = self._core_logic_manager_ref._app_instance_ref
                self.logger.warning(f"TMS: Direct check - App instance ID: {id(app_instance)}")
                self.logger.warning(f"TMS: Direct check - App instance type: {type(app_instance)}")

                # Check if App instance has either of the methods
                has_primary_method = hasattr(app_instance, primary_method_name)
                has_fallback_method = hasattr(app_instance, fallback_method_name)

                self.logger.warning(f"TMS: Direct check - hasattr for primary method: {has_primary_method}")
                self.logger.warning(f"TMS: Direct check - hasattr for fallback method: {has_fallback_method}")

                # Try to get the methods directly
                try:
                    primary_method = getattr(app_instance, primary_method_name, None)
                    fallback_method = getattr(app_instance, fallback_method_name, None)
                    self.logger.warning(f"TMS: Direct check - getattr for primary method: {primary_method is not None}, type={type(primary_method) if primary_method else None}")
                    self.logger.warning(f"TMS: Direct check - getattr for fallback method: {fallback_method is not None}, type={type(fallback_method) if fallback_method else None}")
                except Exception as e:
                    self.logger.error(f"TMS: Exception during direct getattr: {e}")

                if not has_primary_method and not has_fallback_method:
                    self.logger.error(f"TMS: App instance does not have either {primary_method_name} or {fallback_method_name} methods!")
                    # Log the available methods on the App instance to help diagnose the issue
                    app_methods = [method for method in dir(app_instance)
                                  if callable(getattr(app_instance, method))
                                  and not method.startswith('_')]
                    self.logger.info(f"TMS: Available App methods: {app_methods}")

                    # Log similar methods to help diagnose the issue
                    similar_methods = [method for method in app_methods if 'ocr' in method.lower()]
                    if similar_methods:
                        self.logger.info(f"TMS: Similar OCR-related methods: {similar_methods}")
                    return

                # Choose which method to call
                method_name_to_call = primary_method_name if has_primary_method else fallback_method_name
                self.logger.info(f"TMS: Using method '{method_name_to_call}' for GUI update")

                # Schedule the GUI update
                self.logger.debug(f"TMS: Scheduling GUI update for OCR batch with {len(snapshots)} snapshots")
                self._core_logic_manager_ref.schedule_gui_update(
                    method_name_to_call,
                    summary_data
                )
                self.logger.info(f"TMS: Scheduled GUI update for OCR batch completion ({len(snapshots)} snapshots).")
            except Exception as e:
                # Log the error but don't let it propagate
                self.logger.error(f"TMS: Error scheduling GUI update: {e}", exc_info=True)
        else:
            self.logger.debug("[TMS on CLM Thread] CoreLogicManager ref not set. Cannot schedule GUI update for OCR batch.")
        # --- END MODIFICATION 1.6 ---

        # --- END OF RESTORED ORIGINAL BODY ---
    
    def shutdown(self):
        """Shutdown the TradeManagerService and clean up resources."""
        self.logger.info("TradeManagerService shutdown initiated")
        
        # Stop the async IPC worker thread
        if hasattr(self, '_ipc_shutdown_event'):
            self._ipc_shutdown_event.set()
            if hasattr(self, '_ipc_worker_thread') and self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
                self.logger.info("Waiting for IPC worker thread to shutdown...")
                self._ipc_worker_thread.join(timeout=5.0)
                if self._ipc_worker_thread.is_alive():
                    self.logger.warning("IPC worker thread did not shutdown gracefully within timeout")
                else:
                    self.logger.info("IPC worker thread shutdown complete")
        
        # Clean up any remaining queued messages
        if hasattr(self, '_ipc_queue'):
            queue_size = self._ipc_queue.qsize()
            if queue_size > 0:
                self.logger.warning(f"Discarding {queue_size} queued IPC messages during shutdown")
                # Clear the queue
                while not self._ipc_queue.empty():
                    try:
                        self._ipc_queue.get_nowait()
                    except queue.Empty:
                        break
        
        self.logger.info("TradeManagerService shutdown complete")