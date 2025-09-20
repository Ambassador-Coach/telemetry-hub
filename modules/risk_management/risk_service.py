# modules/risk_management/risk_service.py
import logging
import time
from time import time_ns
import threading
import uuid
import queue
from typing import Dict, Any, Optional, List, Set, Callable, Tuple
from queue import SimpleQueue, Empty as QueueEmpty
from concurrent.futures import ThreadPoolExecutor

# Import Interfaces
from interfaces.risk.services import IRiskManagementService, IMarketDataReceiver, RiskLevel, RiskAssessment, CriticalRiskCallback
# Import necessary components (assuming they are moved here or kept in utils)
from utils.price_meltdown_detector import PriceMeltdownDetector, MeltdownConfig
# Import dependencies needed for logic moved from risk_management_module
# e.g., from utils.flags import ... (if still used here)

# Import interfaces/types for dependencies
from interfaces.core.services import IEventBus, ILifecycleService
# Removed ITelemetryService - now using ICorrelationLogger
from utils.global_config import GlobalConfig # For type hinting config
from interfaces.data.services import IPriceProvider
from interfaces.order_management.services import IOrderRepository
from interfaces.trading.services import OrderParameters, IPositionManager, TradeActionType

# Import event classes for EventBus integration
from core.events import (
    OrderRequestEvent, OrderRequestData,
    ValidatedOrderRequestEvent,
    OrderRejectedByRiskEvent, OrderRejectedByRiskData,
    RiskAssessmentEvent, RiskAssessmentEventData,
    AccountUpdateEvent
)

# Import ICorrelationLogger for IntelliSense
from interfaces.logging.services import ICorrelationLogger

logger = logging.getLogger(__name__)

class RiskManagementService(IRiskManagementService, IMarketDataReceiver, ILifecycleService):
    """
    Concrete implementation of the risk management service.
    Encapsulates PriceMeltdownDetector and other risk logic.
    Receives market data and provides risk assessments.
    """

    # All constants now moved to config

    def __init__(self,
                 event_bus: IEventBus,
                 config_service: GlobalConfig,
                 correlation_logger: ICorrelationLogger, # <<< MODIFIED - NOW REQUIRED
                 price_provider: Optional[IPriceProvider] = None, # Expect IPriceProvider
                 order_repository: Optional[IOrderRepository] = None,
                 position_manager: Optional[IPositionManager] = None,
                 # ---->>> ADD 'gui_logger_func' PARAMETER HERE <<<----
                 gui_logger_func: Optional[Callable[[str, str], None]] = None,
                 benchmarker: Optional[Any] = None,
                 pipeline_validator: Optional[Any] = None):
        """
        Initializes the Risk Management Service.

        Args:
            event_bus (IEventBus): The event bus for subscribing to and publishing events.
            config_service (GlobalConfig): The main application configuration object.
            correlation_logger (ICorrelationLogger): Logger for IntelliSense correlation events.
            price_provider (Optional[IPriceProvider]): Service to get price data.
            order_repository (Optional[IOrderRepository]): Service to access order/position state.
            position_manager (Optional[IPositionManager]): Service to access position data.
            gui_logger_func (Optional[Callable[[str, str], None]]): Optional function to log messages to GUI.
            benchmarker (Optional[Any]): Performance benchmarker for metrics.
            pipeline_validator (Optional[Any]): Pipeline validator for price_to_pmd pipeline validation.
        """
        self.logger = logging.getLogger(__name__) # Use instance logger
        self.logger.info("Initializing RiskManagementService...")

        self._event_bus = event_bus
        self._config_service = config_service
        self._correlation_logger = correlation_logger # <<< MODIFIED - Store correlation logger
        self._price_provider = price_provider # Store the price provider
        self._order_repository = order_repository
        self._position_manager = position_manager

        # ---->>> STORE THE PASSED ARGUMENT <<<----
        self._gui_logger_func = gui_logger_func
        self._benchmarker = benchmarker
        self._telemetry_service = None  # Initialize to None since we removed it from constructor

        # Store the pipeline validator for price_to_pmd pipeline validation
        self._pipeline_validator = pipeline_validator
        if self._pipeline_validator:
            self.logger.info("RiskManagementService initialized WITH PipelineValidator for price_to_pmd pipeline validation.")
        else:
            self.logger.info("RiskManagementService initialized WITHOUT PipelineValidator.")

        if self._correlation_logger: # NEW Log
            self.logger.info("RiskManagementService initialized WITH CorrelationLogger for IntelliSense.")
        else:
            self.logger.info("RiskManagementService initialized WITHOUT CorrelationLogger.")

        # Ensure price provider was passed
        if not self._price_provider:
             self.logger.error("RiskManagementService initialized WITHOUT a Price Provider. Market condition checks will fail.")
             
             # INSTRUMENTATION: Publish price provider missing error
             if self._correlation_logger:
                 self._correlation_logger.log_event(
                     source_component="RiskManagementService",
                     event_name="RiskSystemError",
                     payload={
                         "error_type": "PRICE_PROVIDER_MISSING",
                         "message": "RiskManagementService initialized without price provider - market condition checks will fail",
                         "initialization_timestamp": time.time(),
                         "risk_service_state": "DEGRADED",
                         "market_condition_checks_available": False,
                         "critical_risk_monitoring_affected": True,
                         "error_severity": "CRITICAL"
                     },
                     stream_override="testrade:risk-actions"
                 )
             # Consider raising an error if it's essential
             # raise ValueError("RiskManagementService requires an IPriceProvider.")

        # --- Instantiate Internal Components ---
        # (Keep existing MeltdownDetector instantiation logic here)
        md_config_params = {
            'short_window_sec': getattr(self._config_service, 'meltdown_short_window_sec', 30.0),
            'long_window_sec': getattr(self._config_service, 'meltdown_long_window_sec', 90.0),
            'price_drop_window': getattr(self._config_service, 'meltdown_price_drop_window', 1.5),
            'momentum_window': getattr(self._config_service, 'meltdown_momentum_window', 3.0),
            'heavy_sell_multiplier': getattr(self._config_service, 'meltdown_heavy_sell_multiplier', 2.5),
            'price_drop_pct': getattr(self._config_service, 'meltdown_price_drop_pct', 0.025),
            'consecutive_bid_hits': getattr(self._config_service, 'meltdown_consecutive_bid_hits', 3),
            'block_multiplier': getattr(self._config_service, 'meltdown_block_multiplier', 4.0),
            'spread_widen_factor': getattr(self._config_service, 'meltdown_spread_widen_factor', 3.0),
            'sell_ratio_threshold': getattr(self._config_service, 'meltdown_sell_ratio_threshold', 0.85),
            'spread_threshold_pct': getattr(self._config_service, 'meltdown_spread_threshold_pct', 0.20),
            'halt_inactivity': getattr(self._config_service, 'meltdown_halt_inactivity', 10.0),
            'halt_min_trades': getattr(self._config_service, 'meltdown_halt_min_trades', 5)
        }
        md_config = MeltdownConfig(**md_config_params)
        self._meltdown_detector = PriceMeltdownDetector(config=md_config, benchmarker=benchmarker)
        self.logger.info("Internal PriceMeltdownDetector instantiated using configured settings.")
        self.logger.debug(f"MeltdownConfig used: {md_config}")

        # Add grace period config
        self._entry_grace_period_sec = getattr(self._config_service, 'meltdown_entry_grace_period_sec', 5.0)

        # --- State ---
        # Replace self._entry_times with a more comprehensive dict
        self._monitored_positions: Dict[str, dict] = {} # Key: symbol, Value: { entry_price: float, entry_timestamp: float, ... }
        self._lock = threading.RLock()

        # New state for critical risk monitoring
        self._monitored_holdings: Set[str] = set()
        self._critical_risk_callbacks: List[CriticalRiskCallback] = []
        self._last_notified_critical_risk: Dict[str, RiskLevel] = {} # To avoid spamming
        self._risk_event_lock = threading.RLock() # New lock for these shared states

        # Task 1.3: Initialize PMD queue configuration (worker pool created in start())
        self._MAX_PMD_QUEUE_SIZE = getattr(config_service, 'risk_pmd_queue_max_size', 500)
        self._pmd_input_queue = SimpleQueue()
        self._num_pmd_workers = getattr(config_service, 'risk_pmd_workers', 1)

        # Heavy setup moved to start() method
        self._pmd_worker_pool = None  # Created in start()
        self._stop_pmd_event = threading.Event()
        self._is_running = False

        # TODO: Task 1.4 - Add queue monitoring when _process_pmd_queue worker loop is implemented
        # The following queue monitoring pattern should be added to the worker loop:
        #
        # def _process_pmd_queue(self):
        #     """Process PMD input tasks from the queue in a worker thread."""
        #     while not self._stop_pmd_event.is_set():
        #         try:
        #             # Periodic queue size logging
        #             current_q_time = time.time()
        #             if not hasattr(self, '_last_q_log_time_risk_pmd') or \
        #                (current_q_time - getattr(self, '_last_q_log_time_risk_pmd', 0) > 5.0):  # Log every 5 seconds
        #                 if hasattr(self, '_pmd_input_queue'):  # Check if queue exists
        #                     q_size = self._pmd_input_queue.qsize()
        #                     self.logger.info(f"RiskManagementService: PMD input queue size = {q_size}")
        #                     if hasattr(self, '_pmd_input_queue') and hasattr(self._pmd_input_queue, 'maxsize') and self._pmd_input_queue.maxsize > 0 and \
        #                        q_size > self._pmd_input_queue.maxsize * 0.8:
        #                         self.logger.warning(f"RiskManagementService: PMD input queue NEAR CAPACITY! Size: {q_size}")
        #                 setattr(self, '_last_q_log_time_risk_pmd', current_q_time)
        #
        #             # Queue processing logic here...
        #         except QueueEmpty:
        #             continue

        if self._gui_logger_func:
            self.logger.info("RiskManagementService initialized WITH a GUI logger function.")
        else:
            self.logger.info("RiskManagementService initialized WITHOUT a GUI logger function.")

        # Initialize account state tracking
        self._latest_account_state = None
        
        # Async IPC infrastructure removed - using correlation logger instead
        
        # Event subscription moved to start() method
        self.logger.info("RiskManagementService initialized.")

    @property
    def is_ready(self) -> bool:
        """
        Returns True when the service is fully initialized and ready.
        """
        return hasattr(self, '_is_running') and self._is_running

    def _handle_position_update(self, event):
        """
        Listens for new positions to start monitoring them, and stops
        monitoring when they are closed.
        """
        symbol = event.data.symbol.upper()

        # is_open is now a property on the event data, let's assume it exists
        is_open = getattr(event.data, 'is_open', False)

        if is_open and symbol not in self._monitored_positions:
            # A new trade has been entered. Start monitoring.
            entry_price = getattr(event.data, 'average_entry_price', 0.0)
            self.logger.critical(f"RISK: New position detected in {symbol} at ${entry_price:.2f}. Starting meltdown monitoring.")
            
            # Store entry timestamp
            self._monitored_positions[symbol] = {
                "entry_price": entry_price,
                "entry_timestamp": time.time() # Record when we saw the entry
            }

        elif not is_open and symbol in self._monitored_positions:
            # The trade has closed. Stop monitoring.
            self.logger.critical(f"RISK: Position in {symbol} closed. Stopping meltdown monitoring.")
            del self._monitored_positions[symbol]

    # --- IMarketDataReceiver Implementation ---
    def on_trade(self, symbol: str, price: float, size: int, timestamp: float, conditions: Optional[List[str]] = None):
        """Receives trade data and forwards it to the meltdown detector after grace period."""
        # Add grace period check
        monitored_trade = self._monitored_positions.get(symbol)
        if not monitored_trade:
            return

        time_since_entry = time.time() - monitored_trade.get("entry_timestamp", 0)
        if time_since_entry < self._entry_grace_period_sec:
            self.logger.debug(f"RISK: In grace period for {symbol} ({time_since_entry:.1f}s < {self._entry_grace_period_sec}s). Skipping meltdown check.")
            return

        self.logger.debug(f"RiskService received trade: {symbol}")
        try:
            self._meltdown_detector.process_trade(symbol, price, float(size), timestamp)
            self._check_and_notify_holding_risk(symbol)
        except Exception as e:
            self.logger.error(f"Error processing trade in meltdown detector for {symbol}: {e}", exc_info=True)
            
            # INSTRUMENTATION: Publish meltdown detector trade processing error
            if self._correlation_logger:

                self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="RiskSystemError",
                payload={
                    "error_type": "MELTDOWN_DETECTOR_TRADE_ERROR",
                    "message": f"Error processing trade in meltdown detector for {symbol}",
                    "symbol": symbol
                },
                stream_override="testrade:risk-actions"
            )

    def on_quote(self, symbol: str, bid: float, ask: float, bid_size: int, ask_size: int, timestamp: float, conditions: Optional[List[str]] = None):
        """Receives quote data and forwards it to the meltdown detector after grace period."""
        # Add grace period check
        monitored_trade = self._monitored_positions.get(symbol)
        if not monitored_trade:
            return
            
        time_since_entry = time.time() - monitored_trade.get("entry_timestamp", 0)
        if time_since_entry < self._entry_grace_period_sec:
            # We still want to process the quote to establish the initial spread, but we won't trigger an exit.
            self.logger.debug(f"RISK: In grace period for {symbol} ({time_since_entry:.1f}s < {self._entry_grace_period_sec}s). Processing quote for spread, but skipping meltdown check.")
            self._meltdown_detector.process_quote(symbol, bid, ask, time.time())
            return

        self.logger.debug(f"RiskService received quote: {symbol}")
        try:
            self._meltdown_detector.process_quote(symbol, bid, ask, timestamp)
            self._check_and_notify_holding_risk(symbol)
        except Exception as e:
            self.logger.error(f"Error processing quote in meltdown detector for {symbol}: {e}", exc_info=True)
            
            # INSTRUMENTATION: Publish meltdown detector quote processing error
            if self._correlation_logger:

                self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="RiskSystemError",
                payload={
                    "error_type": "MELTDOWN_DETECTOR_QUOTE_ERROR",
                    "message": f"Error processing quote in meltdown detector for {symbol}",
                    "symbol": symbol
                },
                stream_override="testrade:risk-actions"
            )

    # Task 1.3: Add enqueue_market_data method
    def enqueue_market_data(self, market_data: Dict):
        """
        Enqueue market data for processing by PMD worker threads.
        Called directly by PriceRepository when bypassing EventBus.

        Args:
            market_data: Dict containing market data with keys like 'symbol', 'type', 'price', etc.
        """
        # Extract validation_id for pipeline validation
        validation_id = market_data.get('__validation_id')
        correlation_id = market_data.get('__correlation_id')
        
        self.logger.info(f"[RISK_SERVICE] Enqueuing market data with CorrID: {correlation_id}, ValidationID: {validation_id}")

        # Pipeline validation: Register stage completion for price data enqueued to risk
        if validation_id and self._pipeline_validator:
            try:
                self._pipeline_validator.register_stage_completion(
                    validation_id,
                    'price_data_enqueued_to_risk',
                    stage_data={'symbol': market_data.get('symbol'), 'type': market_data.get('type')}
                )
            except Exception as e:
                self.logger.error(f"RiskManagementService: Error registering pipeline stage completion for validation_id {validation_id}: {e}", exc_info=True)

        if not self._is_running:
            self.logger.warning("RiskManagementService not running/initialized, dropping market data for PMD.")
            return

        q_size = self._pmd_input_queue.qsize()
        if q_size >= self._MAX_PMD_QUEUE_SIZE:
            try:
                dropped_item = self._pmd_input_queue.get_nowait()
                self.logger.warning(f"RiskManagementService: PMD queue full (size {q_size}/{self._MAX_PMD_QUEUE_SIZE}). Dropped oldest market data ({dropped_item.get('type')} for {dropped_item.get('symbol')}) to make space.")
            except QueueEmpty:
                pass # Race condition, queue emptied.
            except Exception as e_drop:
                self.logger.error(f"RiskManagementService: Error dropping oldest from full PMD queue: {e_drop}")

        self._pmd_input_queue.put(market_data)
        if q_size % 100 == 0: # Log occasionally
             self.logger.debug(f"RiskManagementService: Enqueued market data for PMD. Queue size: {self._pmd_input_queue.qsize()}")

    def _process_pmd_queue(self):
        """Process PMD input tasks from the queue in a worker thread."""
        self.logger.info(f"PMD Worker Thread ({threading.current_thread().name}) started.")
        
        # REMOVED: Startup delay no longer needed with Dual Buffer architecture
        # IPC operations are now guaranteed non-blocking

        while not self._stop_pmd_event.is_set():
            try:
                # Periodic queue size logging from Task 0.5.4
                current_q_time = time.time()
                if not hasattr(self, '_last_q_log_time_risk_pmd') or \
                   (current_q_time - getattr(self, '_last_q_log_time_risk_pmd', 0) > 5.0):  # Log every 5 seconds
                    if hasattr(self, '_pmd_input_queue'):  # Check if queue exists
                        q_size = self._pmd_input_queue.qsize()
                        self.logger.info(f"RiskManagementService: PMD input queue size = {q_size}")

                        # Capture PMD queue size metric (Enhanced with convenience method)
                        if self._benchmarker:
                            self._benchmarker.gauge(
                                "risk.pmd_queue_size_current",
                                float(q_size),
                                context={'max_size': self._MAX_PMD_QUEUE_SIZE, 'worker_thread': threading.current_thread().name}
                            )

                        WARN_THRESHOLD_PMD_QUEUE = getattr(self._config_service, 'risk_pmd_queue_warn_threshold', int(self._MAX_PMD_QUEUE_SIZE * 0.8))
                        if q_size > WARN_THRESHOLD_PMD_QUEUE:
                            self.logger.warning(f"RiskManagementService: PMD input queue NEAR CAPACITY! Size: {q_size} (Threshold: {WARN_THRESHOLD_PMD_QUEUE})")
                    setattr(self, '_last_q_log_time_risk_pmd', current_q_time)
                
                # INSTRUMENTATION: Periodic health status monitoring
                if not hasattr(self, '_last_health_status_time_risk') or \
                   (current_q_time - getattr(self, '_last_health_status_time_risk', 0) > 30.0):  # Health check every 30 seconds
                    if self._correlation_logger:

                        self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="PeriodicHealthStatus",
                payload={
                    "status": "HEALTHY",
                    "timestamp": time.time()
                },
                stream_override="testrade:system-health"
            )
                    setattr(self, '_last_health_status_time_risk', current_q_time)

                # Get market data from queue
                market_data: Dict = self._pmd_input_queue.get(timeout=0.2)

                # Extract symbol, type, price, size, etc. from the dict
                symbol = market_data.get('symbol', '').upper()
                data_type = market_data.get('type', '')

                # Extract validation_id for pipeline validation
                validation_id = market_data.get('__validation_id')

                # Pipeline validation: Register stage completion for risk PMD processing started
                if validation_id and self._pipeline_validator:
                    try:
                        self._pipeline_validator.register_stage_completion(
                            validation_id,
                            'risk_pmd_processing_started',
                            stage_data={'symbol': symbol, 'type': data_type}
                        )
                    except Exception as e:
                        self.logger.error(f"RiskManagementService: Error registering PMD processing started for validation_id {validation_id}: {e}", exc_info=True)

                if data_type == 'trade':
                    price = market_data.get('price', 0.0)
                    size = market_data.get('size', 0.0)
                    timestamp = market_data.get('timestamp', time.time())

                    # Call meltdown detector
                    self._meltdown_detector.process_trade(symbol, price, float(size), timestamp)

                elif data_type == 'quote':
                    bid = market_data.get('bid', 0.0)
                    ask = market_data.get('ask', 0.0)
                    timestamp = market_data.get('timestamp', time.time())

                    # Call meltdown detector
                    self._meltdown_detector.process_quote(symbol, bid, ask, timestamp)

                # Pipeline validation: Register stage completion for risk PMD processing completed
                if validation_id and self._pipeline_validator:
                    try:
                        self._pipeline_validator.register_stage_completion(
                            validation_id,
                            'risk_pmd_processing_completed',
                            stage_data={'symbol': symbol, 'type': data_type}
                        )
                    except Exception as e:
                        self.logger.error(f"RiskManagementService: Error registering PMD processing completed for validation_id {validation_id}: {e}", exc_info=True)

                # After processing, check for risk level changes
                self._check_and_notify_holding_risk(symbol)

                # Check if meltdown detector indicates a significant state change
                # and publish SymbolRiskLevelChangedEvent if needed
                try:
                    current_risk_level = self.assess_holding_risk(symbol)
                    previous_risk_level = self._last_notified_critical_risk.get(symbol, RiskLevel.NORMAL)

                    if current_risk_level != previous_risk_level:
                        # Risk level changed - publish event
                        from core.events import SymbolRiskLevelChangedEvent, SymbolRiskLevelChangedEventData

                        risk_details = self.get_risk_details(symbol)
                        risk_score = risk_details.get('risk_score', 0.0)

                        event_data = SymbolRiskLevelChangedEventData(
                            symbol=symbol,
                            previous_level=previous_risk_level.name if previous_risk_level else None,
                            new_level=current_risk_level.name,
                            risk_score=risk_score,
                            details=risk_details
                        )

                        self._event_bus.publish(SymbolRiskLevelChangedEvent(data=event_data))
                        self.logger.info(f"RiskManagementService: Published SymbolRiskLevelChangedEvent for {symbol}: {previous_risk_level.name if previous_risk_level else 'None'} -> {current_risk_level.name}")

                        # Update last notified level
                        self._last_notified_critical_risk[symbol] = current_risk_level

                except Exception as e:
                    self.logger.error(f"Error checking risk level changes for {symbol}: {e}", exc_info=True)
                    
                    # INSTRUMENTATION: Publish risk level assessment error
                    if self._correlation_logger:

                        self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="RiskSystemError",
                payload={
                    "error_type": "RISK_LEVEL_ASSESSMENT_ERROR",
                    "message": f"Error checking risk level changes for symbol {symbol}",
                    "symbol": symbol
                },
                stream_override="testrade:risk-actions"
            )

            except QueueEmpty:
                continue
            except Exception as e:
                self.logger.error(f"PMD Worker: Error processing market data: {e}", exc_info=True)
                
                # INSTRUMENTATION: Publish PMD worker error
                if self._correlation_logger:

                    self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="RiskSystemError",
                payload={
                    "error_type": "PMD_WORKER_ERROR",
                    "message": "PMD worker thread error processing market data"
                }
            )

        self.logger.info(f"PMD Worker Thread ({threading.current_thread().name}) stopped.")

    # --- EventBus Integration ---
    def _subscribe_to_events(self):
        """Subscribe to relevant events from the event_bus."""
        # Create wrapper function to prevent garbage collection with WeakSet
        def order_request_wrapper(event):
            return self.handle_order_request(event)

        # Store wrapper as instance variable to prevent garbage collection
        self._order_request_handler = order_request_wrapper
        self._event_bus.subscribe(OrderRequestEvent, self._order_request_handler)
        self.logger.info("RiskManagementService subscribed to OrderRequestEvent.")
        
        # Subscribe to AccountUpdateEvent for monitoring account-level metrics
        def account_update_wrapper(event):
            return self.handle_account_update(event)
        
        self._account_update_handler = account_update_wrapper
        self._event_bus.subscribe(AccountUpdateEvent, self._account_update_handler)
        self.logger.info("RiskManagementService subscribed to AccountUpdateEvent.")

    def handle_account_update(self, event: AccountUpdateEvent):
        """
        Handle incoming AccountUpdateEvent to monitor account-level risk metrics.
        
        Args:
            event: The AccountUpdateEvent containing account summary data
        """
        try:
            account_data = event.data
            
            # Capture correlation context
            master_correlation_id = getattr(event, 'correlation_id', None)
            # The ID of the current event is the cause of the next event
            causation_id_for_next_step = getattr(event, 'event_id', None)
            
            if not master_correlation_id:
                master_correlation_id = str(uuid.uuid4())
                self.logger.warning(f"Missing correlation_id on event {type(event).__name__}. Generated new one: {master_correlation_id}")
            
            # Log account update for monitoring
            self.logger.info(f"RiskManagementService received AccountUpdateEvent: "
                           f"Equity=${account_data.equity:.2f}, "
                           f"BuyingPower=${account_data.buying_power:.2f}, "
                           f"Cash=${account_data.cash:.2f}, "
                           f"RealizedPnL=${account_data.realized_pnl_day}, "
                           f"UnrealizedPnL=${account_data.unrealized_pnl_day}")
            
            # Store latest account state for risk calculations
            self._latest_account_state = {
                'equity': account_data.equity,
                'buying_power': account_data.buying_power,
                'cash': account_data.cash,
                'realized_pnl_day': account_data.realized_pnl_day,
                'unrealized_pnl_day': account_data.unrealized_pnl_day,
                'timestamp': account_data.timestamp
            }
            
            # Check for critical account conditions
            if account_data.buying_power < 0:
                self.logger.critical(f"RISK ALERT: Negative buying power detected: ${account_data.buying_power:.2f}")
                
            # Check for large daily losses (configurable threshold)
            max_daily_loss = getattr(self._config_service, 'risk_max_daily_loss', 5000.0)
            if account_data.realized_pnl_day is not None and account_data.realized_pnl_day < -max_daily_loss:
                self.logger.critical(f"RISK ALERT: Daily loss exceeds threshold: ${account_data.realized_pnl_day:.2f} "
                                   f"(threshold: -${max_daily_loss:.2f})")
            
            # Update metrics if benchmarker available
            if self._benchmarker:
                self._benchmarker.gauge("risk.account_equity", account_data.equity)
                self._benchmarker.gauge("risk.account_buying_power", account_data.buying_power)
                if account_data.realized_pnl_day is not None:
                    self._benchmarker.gauge("risk.account_realized_pnl_day", account_data.realized_pnl_day)
                if account_data.unrealized_pnl_day is not None:
                    self._benchmarker.gauge("risk.account_unrealized_pnl_day", account_data.unrealized_pnl_day)
            
            # Publish account update to Redis stream for visibility
            try:
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="AccountUpdate",
                        payload={
                            "correlation_id": master_correlation_id,
                            "account_data": account_data.__dict__ if hasattr(account_data, '__dict__') else str(account_data),
                            "causation_id": causation_id_for_next_step
                        },
                        stream_override=getattr(self._config_service, 'redis_stream_account_updates', 'testrade:account-updates')
                    )
            except Exception as e:
                self.logger.warning(f"Failed to publish account update to Redis (non-critical): {e}")
                    
        except Exception as e:
            self.logger.error(f"Error handling AccountUpdateEvent: {e}", exc_info=True)

    def handle_order_request(self, event: OrderRequestEvent):
        """
        Handle incoming OrderRequestEvent by performing risk checks and publishing
        either ValidatedOrderRequestEvent or OrderRejectedByRiskEvent.

        Args:
            event: The OrderRequestEvent to process
        """
        try:
            order_request_data: OrderRequestData = event.data
            
            # Capture correlation context - correlation_id is in the data object
            master_correlation_id = getattr(order_request_data, 'correlation_id', None)
            # The ID of the current event is the cause of the next event
            causation_id_for_next_step = getattr(event, 'event_id', None)
            
            if not master_correlation_id:
                self.logger.error(f"CRITICAL: Missing correlation_id in OrderRequestData. This breaks end-to-end tracing!")
                master_correlation_id = "MISSING_CORRELATION_ID"
            
            # Log ALL IDs for complete tracing
            self.logger.info(f"[RISK] Processing OrderRequest with ALL IDs - "
                           f"CorrID: {master_correlation_id}, "
                           f"RequestID: {order_request_data.request_id}, "
                           f"ValidationID: {order_request_data.validation_id}, "
                           f"RelatedOCREventID: {getattr(order_request_data, 'related_ocr_event_id', 'N/A')}, "
                           f"EventID: {causation_id_for_next_step}, "
                           f"Symbol: {order_request_data.symbol}, "
                           f"Side: {order_request_data.side}, "
                           f"Qty: {order_request_data.quantity}")

            # Capture request received count metric (Enhanced with convenience method)
            if self._benchmarker:
                self._benchmarker.increment(
                    "risk.order_request_received_count",
                    1.0,
                    context={'request_id': order_request_data.request_id, 'symbol': order_request_data.symbol}
                )

            if not order_request_data:
                self.logger.error("ORDER_PATH_DEBUG: Received OrderRequestEvent with no data payload")
                return

            # Extract validation_id for pipeline tracking
            validation_id = order_request_data.validation_id

            # Register stage: order request received by risk management
            if validation_id and self._pipeline_validator:
                self._pipeline_validator.register_stage_completion(
                    validation_id,
                    'order_request_received_by_risk',
                    stage_data={'request_id': order_request_data.request_id, 'symbol': order_request_data.symbol}
                )

            # Start timing for benchmarking
            risk_handling_start_time = time.time() if validation_id and self._benchmarker else None

            # T13 MILESTONE: Risk Check Pipeline Start
            T13_RiskCheckStart_ns = time_ns()

            # Enhanced logging for order request receipt
            self.logger.info(f"RISK_DEBUG: RiskManagementService.handle_order_request ENTRY")
            self.logger.debug(f"ORDER_PATH_DEBUG: RiskManagementService received OrderRequestEvent")
            self.logger.debug(f"ORDER_PATH_DEBUG: Event ID: {event.event_id}")
            self.logger.debug(f"ORDER_PATH_DEBUG: Full event data: {order_request_data}")
            self.logger.info(f"ORDER_PATH_DEBUG: Processing order request - Symbol: {order_request_data.symbol}, "
                           f"Side: {order_request_data.side}, Quantity: {order_request_data.quantity}, "
                           f"Request ID: {order_request_data.request_id}")
            self.logger.info(f"T13_MILESTONE: Risk check pipeline started at {T13_RiskCheckStart_ns}ns for request {order_request_data.request_id}")

            # Perform Basic Risk Checks
            self.logger.info(f"RISK_DEBUG: Calling _perform_basic_risk_checks")
            rejection_reason = self._perform_basic_risk_checks(order_request_data)
            self.logger.info(f"RISK_DEBUG: _perform_basic_risk_checks returned: {rejection_reason}")

            # T14 MILESTONE: Risk Check Pipeline End
            T14_RiskCheckEnd_ns = time_ns()
            assessment_duration_ns = T14_RiskCheckEnd_ns - T13_RiskCheckStart_ns
            self.logger.info(f"T14_MILESTONE: Risk check pipeline completed at {T14_RiskCheckEnd_ns}ns for request {order_request_data.request_id}, duration: {assessment_duration_ns}ns")

            # --- NEW: Correlation ID Logging ---
            correlation_id_from_event = getattr(order_request_data, 'correlation_id', None)
            if self._correlation_logger and correlation_id_from_event:
                log_payload = {
                    "event_name": "OrderRequestEvent", # Expected by EventConditionChecker
                    "correlation_id": correlation_id_from_event,
                    "request_id": order_request_data.request_id, # Expected field name
                    "symbol": order_request_data.symbol,
                    "side": order_request_data.side.value if hasattr(order_request_data.side, 'value') else str(order_request_data.side), # Handle enum
                    "quantity": order_request_data.quantity,
                    "order_type": order_request_data.order_type.value if hasattr(order_request_data.order_type, 'value') else str(order_request_data.order_type),
                    "risk_assessment_status": "REJECTED" if rejection_reason else "VALIDATED",
                    "rejection_reason": rejection_reason if rejection_reason else None,
                    "validation_pipeline_id": validation_id # Include existing validation_id
                }
                self._correlation_logger.log_event(
                    source_component="RiskManagementService",
                    event_name="RiskAssessmentCompleted",
                    payload=log_payload
                )
                self.logger.info(f"Correlation Logged for RiskAssessment: CorrID {correlation_id_from_event}, ReqID {order_request_data.request_id}")
            elif self._correlation_logger:
                 self.logger.warning(f"CorrelationLogger present but no correlation_id found in OrderRequestData for ReqID {order_request_data.request_id}")
            # --- END NEW Correlation ID Logging ---

            if rejection_reason:
                self.logger.info(f"RISK_DEBUG: Risk check FAILED - publishing RiskAssessmentEvent and OrderRejectedByRiskEvent")

                # Register stage: risk assessment completed with rejection
                if validation_id and self._pipeline_validator:
                    self._pipeline_validator.register_stage_completion(
                        validation_id,
                        'risk_assessment_completed',
                        status="REJECTED_BY_RISK",
                        stage_data={'request_id': order_request_data.request_id, 'reason': rejection_reason}
                    )

                # Create RiskAssessmentEvent for REJECTED scenario
                risk_assessment_data = RiskAssessmentEventData(
                    original_request_id=order_request_data.request_id,
                    symbol=order_request_data.symbol,
                    side=str(order_request_data.side),
                    quantity=order_request_data.quantity,
                    order_type=str(order_request_data.order_type),
                    decision="REJECTED",
                    rejection_reason=rejection_reason,
                    T13_RiskCheckStart_ns=T13_RiskCheckStart_ns,
                    T14_RiskCheckEnd_ns=T14_RiskCheckEnd_ns,
                    assessment_duration_ns=assessment_duration_ns,
                    source_event_id=causation_id_for_next_step,
                    correlation_id=master_correlation_id,
                    validation_id=validation_id,
                    current_price=getattr(order_request_data, 'current_price', None),
                    risk_check_details={
                        'rejection_reason': rejection_reason,
                        'validation_passed': False,
                        'risk_checks_performed': ['price_validation', 'quantity_limits', 'symbol_validation']
                    }
                )

                # Create RiskAssessmentEvent
                risk_assessment_event = RiskAssessmentEvent(data=risk_assessment_data)

                # T15 MILESTONE: Risk Assessment Event Published
                T15_RiskEventPublished_ns = time_ns()
                risk_assessment_data.T15_RiskEventPublished_ns = T15_RiskEventPublished_ns
                self.logger.info(f"T15_MILESTONE: Risk assessment event published at {T15_RiskEventPublished_ns}ns for request {order_request_data.request_id}")

                # Publish RiskAssessmentEvent first for complete instrumentation chain
                self._event_bus.publish(risk_assessment_event)
                self.logger.info(f"INSTRUMENTATION: Published RiskAssessmentEvent for REJECTED order {order_request_data.request_id}")

                # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T13-T15 milestone telemetry (REJECTED)
                if self._telemetry_service:
                    try:
                        telemetry_payload = {
                            # Milestone timestamps (nanoseconds)
                            "T13_RiskCheckStart_ns": T13_RiskCheckStart_ns,
                            "T14_RiskCheckEnd_ns": T14_RiskCheckEnd_ns,
                            "T15_RiskEventPublished_ns": T15_RiskEventPublished_ns,
                            
                            # Calculated latencies
                            "T13_to_T14_assessment_ms": assessment_duration_ns / 1_000_000,
                            "T14_to_T15_publish_ms": (T15_RiskEventPublished_ns - T14_RiskCheckEnd_ns) / 1_000_000,
                            "total_T13_to_T15_ms": (T15_RiskEventPublished_ns - T13_RiskCheckStart_ns) / 1_000_000,
                            
                            # Risk decision context
                            "decision": "REJECTED",
                            "rejection_reason": rejection_reason,
                            "symbol": order_request_data.symbol,
                            "quantity": order_request_data.quantity,
                            "side": order_request_data.side.value if hasattr(order_request_data.side, 'value') else str(order_request_data.side),
                            "order_value": order_value,
                            "position_after": position_after,
                            
                            # Risk metrics
                            "risk_level": assessment.level.value if hasattr(assessment.level, 'value') else str(assessment.level),
                            "approved": assessment.approved,
                            "risk_score": assessment.score,
                            
                            # Causation chain
                            "request_id": order_request_data.request_id,
                            "correlation_id": master_correlation_id,
                            "assessment_event_id": risk_assessment_event.event_id,
                            "causation_id": causation_id_for_next_step
                        }
                        
                        # Fire and forget - no blocking!
                        if self._correlation_logger:
                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="RiskRejectionMilestones",
                                payload=telemetry_payload,
                                stream_override="testrade:instrumentation:risk-assessment"
                            )
                    except Exception as telemetry_error:
                        # Never let telemetry errors affect trading
                        self.logger.debug(f"Telemetry enqueue error (non-critical): {telemetry_error}")

                # Risk check failed - publish rejection event with proper causation
                rejection_data = OrderRejectedByRiskData(
                    original_request_id=order_request_data.request_id,
                    symbol=order_request_data.symbol,
                    side=order_request_data.side,
                    quantity=order_request_data.quantity,
                    order_type=order_request_data.order_type,
                    reason=rejection_reason,
                    original_request=order_request_data,
                    risk_check_details={
                        'assessment_event_id': risk_assessment_event.event_id,
                        'T13_RiskCheckStart_ns': T13_RiskCheckStart_ns,
                        'T14_RiskCheckEnd_ns': T14_RiskCheckEnd_ns,
                        'T15_RiskEventPublished_ns': T15_RiskEventPublished_ns,
                        'assessment_duration_ns': assessment_duration_ns,
                        'causation_chain': f"OrderRequest[{causation_id_for_next_step}] -> RiskAssessment[{risk_assessment_event.event_id}] -> OrderRejected"
                    }
                )

                # Propagate validation_id to rejection event (using correct name-mangled attribute)
                if validation_id:
                    rejection_data._OrderRejectedByRiskData__validation_id = validation_id

                rejection_event = OrderRejectedByRiskEvent(
                    data=rejection_data
                )

                # Enhanced logging for rejection
                self.logger.error(f"ORDER_PATH_DEBUG: RISK REJECTION - Request ID: {order_request_data.request_id}")
                self.logger.error(f"ORDER_PATH_DEBUG: Rejection reason: {rejection_reason}")
                self.logger.error(f"ORDER_PATH_DEBUG: Full OrderRejectedByRiskData: {rejection_data}")
                self.logger.warning(f"ORDER_PATH_DEBUG: Publishing OrderRejectedByRiskEvent to EventBus")

                self._event_bus.publish(rejection_event)

                self.logger.warning(f"ORDER_PATH_DEBUG: Order request {order_request_data.request_id} "
                                  f"for {order_request_data.symbol} REJECTED: {rejection_reason}")

                # Publish risk rejection to Redis stream for GUI visibility
                try:
                    if self._correlation_logger and master_correlation_id:

                        self._correlation_logger.log_event(
                            source_component="RiskManagementService",
                            event_name="RiskRejection",
                            payload={
                                "correlation_id": master_correlation_id,
                                "request_id": order_request_data.request_id,
                                "symbol": order_request_data.symbol,
                                "rejection_reason": rejection_reason,
                                "validation_id": validation_id,
                                "T13_ns": T13_RiskCheckStart_ns,
                                "T14_ns": T14_RiskCheckEnd_ns,
                                "causation_id": causation_id
                            },
                            stream_override="testrade:risk-actions"
                        )
                except Exception as e:
                    self.logger.warning(f"Redis publishing failed for risk rejection (non-critical): {e}")

                # Log to GUI if available (COMMENTED OUT - now using Redis stream)
                # if self._gui_logger_func:
                #     self._gui_logger_func(f"RISK REJECTION: {order_request_data.symbol} {order_request_data.side} "
                #                         f"{order_request_data.quantity} - {rejection_reason}", "warning")
            else:
                self.logger.info(f"RISK_DEBUG: Risk check PASSED - publishing RiskAssessmentEvent and ValidatedOrderRequestEvent")

                # Register stage: risk assessment completed with validation
                if validation_id and self._pipeline_validator:
                    self._pipeline_validator.register_stage_completion(
                        validation_id,
                        'risk_assessment_completed',
                        status="VALIDATED",
                        stage_data={'request_id': order_request_data.request_id}
                    )

                # Create RiskAssessmentEvent for VALIDATED scenario
                risk_assessment_data = RiskAssessmentEventData(
                    original_request_id=order_request_data.request_id,
                    symbol=order_request_data.symbol,
                    side=str(order_request_data.side),
                    quantity=order_request_data.quantity,
                    order_type=str(order_request_data.order_type),
                    decision="VALIDATED",
                    rejection_reason=None,
                    T13_RiskCheckStart_ns=T13_RiskCheckStart_ns,
                    T14_RiskCheckEnd_ns=T14_RiskCheckEnd_ns,
                    assessment_duration_ns=assessment_duration_ns,
                    source_event_id=causation_id_for_next_step,
                    correlation_id=master_correlation_id,
                    validation_id=validation_id,
                    current_price=getattr(order_request_data, 'current_price', None),
                    risk_check_details={
                        'validation_passed': True,
                        'risk_checks_performed': ['price_validation', 'quantity_limits', 'symbol_validation'],
                        'all_checks_passed': True
                    }
                )

                # Create RiskAssessmentEvent
                risk_assessment_event = RiskAssessmentEvent(data=risk_assessment_data)

                # T15 MILESTONE: Risk Assessment Event Published
                T15_RiskEventPublished_ns = time_ns()
                risk_assessment_data.T15_RiskEventPublished_ns = T15_RiskEventPublished_ns
                self.logger.info(f"T15_MILESTONE: Risk assessment event published at {T15_RiskEventPublished_ns}ns for request {order_request_data.request_id}")

                # Publish RiskAssessmentEvent first for complete instrumentation chain
                self._event_bus.publish(risk_assessment_event)
                self.logger.info(f"INSTRUMENTATION: Published RiskAssessmentEvent for VALIDATED order {order_request_data.request_id}")

                # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T13-T15 milestone telemetry
                if self._telemetry_service:
                    try:
                        telemetry_payload = {
                            # Milestone timestamps (nanoseconds)
                            "T13_RiskCheckStart_ns": T13_RiskCheckStart_ns,
                            "T14_RiskCheckEnd_ns": T14_RiskCheckEnd_ns,
                            "T15_RiskEventPublished_ns": T15_RiskEventPublished_ns,
                            
                            # Calculated latencies
                            "T13_to_T14_assessment_ms": assessment_duration_ns / 1_000_000,
                            "T14_to_T15_publish_ms": (T15_RiskEventPublished_ns - T14_RiskCheckEnd_ns) / 1_000_000,
                            "total_T13_to_T15_ms": (T15_RiskEventPublished_ns - T13_RiskCheckStart_ns) / 1_000_000,
                            
                            # Risk decision context
                            "decision": "VALIDATED",
                            "symbol": order_request_data.symbol,
                            "quantity": order_request_data.quantity,
                            "side": order_request_data.side.value if hasattr(order_request_data.side, 'value') else str(order_request_data.side),
                            "order_value": order_value,
                            "position_after": position_after,
                            "validation_id": validation_id,
                            
                            # Risk metrics
                            "risk_level": assessment.level.value if hasattr(assessment.level, 'value') else str(assessment.level),
                            "approved": assessment.approved,
                            "risk_score": assessment.score,
                            
                            # Causation chain
                            "request_id": order_request_data.request_id,
                            "correlation_id": master_correlation_id,
                            "assessment_event_id": risk_assessment_event.event_id,
                            "causation_id": causation_id_for_next_step
                        }
                        
                        # Fire and forget - no blocking!
                        if self._correlation_logger:
                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="RiskAssessmentMilestones",
                                payload=telemetry_payload,
                                stream_override="testrade:instrumentation:risk-assessment"
                            )
                    except Exception as telemetry_error:
                        # Never let telemetry errors affect trading
                        self.logger.debug(f"Telemetry enqueue error (non-critical): {telemetry_error}")

                # Propagate validation_id to validated event
                if validation_id:
                    order_request_data.validation_id = validation_id

                # Ensure the correlation ID is set in the order request data
                if master_correlation_id:
                    order_request_data.correlation_id = master_correlation_id

                # Add causation metadata to order request data for downstream tracing
                # Use the existing 'meta' field instead of trying to add a new attribute
                if not order_request_data.meta:
                    order_request_data.meta = {}
                
                order_request_data.meta['risk_assessment_metadata'] = {
                    'assessment_event_id': risk_assessment_event.event_id,
                    'T13_RiskCheckStart_ns': T13_RiskCheckStart_ns,
                    'T14_RiskCheckEnd_ns': T14_RiskCheckEnd_ns,
                    'T15_RiskEventPublished_ns': T15_RiskEventPublished_ns,
                    'assessment_duration_ns': assessment_duration_ns,
                    'causation_chain': f"OrderRequest[{causation_id_for_next_step}] -> RiskAssessment[{risk_assessment_event.event_id}] -> ValidatedOrder"
                }
                
                # All risk checks passed - publish validated event
                validated_event = ValidatedOrderRequestEvent(
                    data=order_request_data
                )

                # Capture validated count metric (successful validation)
                if self._benchmarker:
                    self._benchmarker.increment(
                        "risk.order_requests_validated_count",
                        1.0,
                        context={'request_id': order_request_data.request_id, 'symbol': order_request_data.symbol}
                    )

                # Enhanced logging for validation
                self.logger.info(f"ORDER_PATH_DEBUG: RISK VALIDATION PASSED - Request ID: {order_request_data.request_id}")
                self.logger.info(f"ORDER_PATH_DEBUG: Publishing ValidatedOrderRequestEvent to EventBus")
                self.logger.critical(f"RMS_PUBLISH_DEBUG: EventBus ID: {id(self._event_bus)}, Event Type: {type(validated_event).__name__}")
                self.logger.debug(f"ORDER_PATH_DEBUG: ValidatedOrderRequestEvent data: {order_request_data}")

                self._event_bus.publish(validated_event)

                # Publish validated order to Redis stream (NON-BLOCKING)
                try:
                    if self._correlation_logger and master_correlation_id:
                        # Log validation asynchronously to avoid blocking
                        def log_validation():
                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="OrderValidated",
                                payload={
                                    "correlation_id": master_correlation_id,
                                    "request_id": order_request_data.request_id,
                                    "symbol": order_request_data.symbol,
                                    "causation_id": causation_id
                                },
                                stream_override=getattr(self._config_service, 'redis_stream_validated_orders', 'testrade:validated-orders')
                            )
                        threading.Thread(target=log_validation, daemon=True).start()
                except Exception as e:
                    self.logger.warning(f"Redis publishing failed (non-critical): {e}")

                self.logger.info(f"ORDER_PATH_DEBUG: Order request {order_request_data.request_id} "
                               f"for {order_request_data.symbol} {order_request_data.side} {order_request_data.quantity} VALIDATED")

                # Log to GUI if available (COMMENTED OUT - now using Redis stream)
                # if self._gui_logger_func:
                #     self._gui_logger_func(f"RISK APPROVED: {order_request_data.symbol} {order_request_data.side} "
                #                         f"{order_request_data.quantity}", "info")

            # Capture benchmarking metric for risk handling time
            if validation_id and self._benchmarker and risk_handling_start_time is not None:
                self._benchmarker.capture_metric(
                    "risk.order_request_handling_time_ms",
                    (time.time() - risk_handling_start_time) * 1000,
                    context={'request_id': order_request_data.request_id, 'validation_id': validation_id}
                )
                # Capture processed count metric (successful processing) - Enhanced with convenience method
                self._benchmarker.increment(
                    "risk.order_requests_processed_count",
                    1.0,
                    context={'request_id': order_request_data.request_id, 'validation_id': validation_id}
                )

        except Exception as e:
            self.logger.error(f"Error handling OrderRequestEvent: {e}", exc_info=True)

    def _perform_basic_risk_checks(self, order_request_data: OrderRequestData) -> Optional[str]:
        """
        Perform basic risk checks on an order request.

        Args:
            order_request_data: The order request to validate

        Returns:
            Optional[str]: None if all checks pass, otherwise a rejection reason string
        """
        try:
            symbol = order_request_data.symbol
            quantity = order_request_data.quantity
            side = order_request_data.side

            self.logger.debug(f"ORDER_PATH_DEBUG: Starting risk checks for {symbol} {side} {quantity}")

            # Check 0: STRICT PRICE VALIDATION - Orders must have valid current_price for live trading
            if order_request_data.current_price is None or order_request_data.current_price <= 0.0:
                failure_reason = f"LIVE_TRADING: Order rejected - No valid current_price provided. Price: {order_request_data.current_price}"
                self.logger.error(f"ORDER_PATH_DEBUG: Risk Check 0 FAILED: {failure_reason}")
                return failure_reason
            self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 0 PASSED - Valid current_price: ${order_request_data.current_price:.4f}")

            # Check 1: Max Order Quantity
            max_qty = getattr(self._config_service, 'RISK_MAX_ORDER_QTY', 1000)
            self.logger.critical(f"CONFIG_DEBUG: RISK_MAX_ORDER_QTY = {max_qty} (config_service type: {type(self._config_service)})")
            self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 1 - Max Order Quantity: {quantity} vs {max_qty}")
            if quantity > max_qty:
                failure_reason = f"Order quantity {quantity} exceeds max allowed {max_qty}"
                self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 1 FAILED: {failure_reason}")
                
                # INSTRUMENTATION: Publish max order quantity violation
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="AccountLimitViolation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "violation_type": "UNKNOWN",
                            "symbol": order_request_data.symbol,
                            "details": "See logs for details",
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return failure_reason
            self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 1 PASSED")

            # Check 2: Max Shares Per Trade (using existing config)
            max_shares = getattr(self._config_service, 'risk_max_shares_per_trade', 1000)
            self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 2 - Max Shares Per Trade: {quantity} vs {max_shares}")
            if quantity > max_shares:
                failure_reason = f"Order quantity {quantity} exceeds max shares per trade {max_shares}"
                self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 2 FAILED: {failure_reason}")
                
                # INSTRUMENTATION: Publish max shares per trade violation
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="AccountLimitViolation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "violation_type": "UNKNOWN",
                            "symbol": order_request_data.symbol,
                            "details": "See logs for details",
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return failure_reason
            self.logger.debug(f"ORDER_PATH_DEBUG: Risk Check 2 PASSED")

            # Check 3: Max Notional Value
            max_notional = getattr(self._config_service, 'risk_max_notional_per_trade', 25000.0)

            # Try to get price for notional check
            price_for_notional = None
            if order_request_data.limit_price:
                price_for_notional = order_request_data.limit_price
            elif order_request_data.current_price:
                price_for_notional = order_request_data.current_price
            elif self._price_provider:
                try:
                    # Pass correlation_id from order_request_data for price fetching traceability
                    correlation_id = getattr(order_request_data, 'correlation_id', None)
                    price_for_notional = self._price_provider.get_reliable_price(symbol, side, correlation_id=correlation_id)
                    self.logger.info(f"RiskManagementService: Retrieved price for notional check for {symbol}: {price_for_notional} from PriceRepository (CorrelationId={correlation_id})")
                    
                    # INSTRUMENTATION: Check if price fetch failed
                    if price_for_notional is None or price_for_notional <= 0:
                        if self._correlation_logger and master_correlation_id:

                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="PriceProviderFailure",
                                payload={
                                    "correlation_id": master_correlation_id,
                                    "failure_type": "UNKNOWN",
                                    "symbol": symbol,
                                    "message": "Price provider failure",
                                    "causation_id": causation_id
                                }
                            )
                        
                except Exception as e:
                    self.logger.warning(f"RiskManagementService: Could not get price for notional check: {e}")
                    
                    # INSTRUMENTATION: Publish price provider exception
                    if self._correlation_logger and master_correlation_id:

                        self._correlation_logger.log_event(
                            source_component="RiskManagementService",
                            event_name="PriceProviderFailure",
                            payload={
                                "correlation_id": master_correlation_id,
                                "failure_type": "UNKNOWN",
                                "symbol": symbol,
                                "message": "Price provider failure",
                                "causation_id": causation_id
                            },
                            stream_override="testrade:risk-actions"
                        )
            else:
                # INSTRUMENTATION: Publish price provider unavailable
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="PriceProviderFailure",
                        payload={
                            "correlation_id": master_correlation_id,
                            "failure_type": "UNKNOWN",
                            "symbol": symbol,
                            "message": "Price provider failure",
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )

            if price_for_notional and price_for_notional > 0:
                notional_value = quantity * price_for_notional
                if notional_value > max_notional:
                    failure_reason = f"Order notional value ${notional_value:.2f} exceeds max allowed ${max_notional:.2f}"
                    
                    # INSTRUMENTATION: Publish max notional value violation
                    if self._correlation_logger and master_correlation_id:

                        self._correlation_logger.log_event(
                            source_component="RiskManagementService",
                            event_name="AccountLimitViolation",
                            payload={
                                "correlation_id": master_correlation_id,
                                "violation_type": "UNKNOWN",
                                "symbol": order_request_data.symbol,
                                "details": "See logs for details",
                                "causation_id": causation_id
                            },
                            stream_override="testrade:risk-actions"
                        )
                    return failure_reason

            # Check 4: Max Position Size (for BUY orders)
            if side.upper() == "BUY" and self._position_manager:
                try:
                    max_position = getattr(self._config_service, 'risk_max_position_size', 2000)
                    current_position_data = self._position_manager.get_position(symbol)
                    current_shares = current_position_data.get("shares", 0.0) if current_position_data else 0.0

                    new_position = current_shares + quantity
                    if new_position > max_position:
                        failure_reason = f"New position {new_position:.0f} would exceed max position size {max_position} (current: {current_shares:.0f})"
                        
                        # INSTRUMENTATION: Publish max position size violation
                        if self._correlation_logger and master_correlation_id:

                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="AccountLimitViolation",
                                payload={
                                    "correlation_id": master_correlation_id,
                                    "violation_type": "UNKNOWN",
                                    "symbol": order_request_data.symbol,
                                    "details": "See logs for details",
                                    "causation_id": causation_id
                                },
                                stream_override="testrade:risk-actions"
                            )
                        return failure_reason

                except Exception as e:
                    self.logger.warning(f"Could not check position size for {symbol}: {e}")

            # Check 5: Market Conditions (if price provider available)
            if self._price_provider:
                try:
                    correlation_id = getattr(order_request_data, 'correlation_id', None)
                    market_assessment = self.check_market_conditions(symbol, correlation_id=correlation_id)
                    if market_assessment.level in (RiskLevel.HIGH, RiskLevel.CRITICAL, RiskLevel.HALTED):
                        return f"Market conditions unfavorable: {market_assessment.reason}"
                except Exception as e:
                    self.logger.warning(f"Could not check market conditions for {symbol}: {e}")

            # All checks passed
            return None

        except Exception as e:
            self.logger.error(f"Error in basic risk checks: {e}", exc_info=True)
            return f"Risk check error: {str(e)}"

    # --- IRiskManagementService Implementation ---
    def assess_holding_risk(self, symbol: str, correlation_id: str = None) -> RiskLevel:
        """
        Assesses risk, returns status level.
        Moves logic from old risk_management_module.check_risk_meltdown.
        
        Args:
            symbol: The stock symbol to assess
            correlation_id: Optional correlation ID for end-to-end traceability
        """
        symbol = symbol.upper()
        self.logger.debug(f"Assessing holding risk for {symbol}")

        # Check minimum hold time if entry time is available
        # Get entry time from monitored_positions structure
        position_data = self._monitored_positions.get(symbol, {})
        entry_time = position_data.get('entry_timestamp', time.time())
        held_seconds = time.time() - entry_time
        min_hold_seconds = getattr(self._config_service, 'risk_min_hold_seconds', 6.0)
        if held_seconds < min_hold_seconds:
            self.logger.debug(f"Risk Assessment: Hold time {held_seconds:.1f}s < {min_hold_seconds}s. Risk: NORMAL (Hold)")
            return RiskLevel.NORMAL # Indicate normal due to hold time

        # Check catastrophic first (using internal detector)
        try:
            cat_result = self._meltdown_detector.is_fast_price_drop(symbol)
            cat_triggered, cat_drop_pct = cat_result
            catastrophic_threshold = getattr(self._config_service, 'risk_catastrophic_drop_threshold', 0.10)
            if cat_triggered and cat_drop_pct >= catastrophic_threshold:
                self.logger.warning(f"Risk Assessment: Catastrophic drop detected for {symbol} ({cat_drop_pct*100:.1f}%). Risk: CRITICAL")
                
                # INSTRUMENTATION: Publish catastrophic drop risk escalation
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="RiskEscalation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "event_type": "RISK_LEVEL_ESCALATED",
                            "risk_level": "CRITICAL",
                            "message": f"Catastrophic drop detected (down {drop_pct*100:.2f}%)",
                            "symbol": symbol,
                            "drop_percentage": drop_pct,
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return RiskLevel.CRITICAL
        except Exception as e:
             self.logger.error(f"Error checking catastrophic drop for {symbol}: {e}")
             return RiskLevel.UNKNOWN # Return unknown on error

        # Normal meltdown check
        try:
            conditions = self._meltdown_detector.check_meltdown_conditions(symbol, correlation_id)
            
            # INSTRUMENTATION: Publish detailed meltdown factor analysis for ML
            factors = conditions.get("factors", {})
            risk_score = conditions.get("risk_score", 0.0)
            metrics = conditions.get("metrics", {})
            
            # Always publish factor analysis for ML training data (both normal and high risk situations)
            if self._correlation_logger:

                self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="MeltdownFactorAnalysis",
                payload={
                    "symbol": symbol,
                    "factors": factors,
                    "risk_score": risk_score,
                    "metrics": metrics
                },
                stream_override="testrade:risk-actions"
            )
            
            if conditions.get("is_halted", False):
                self.logger.info(f"Risk Assessment: Symbol {symbol} is halted. Risk: HALTED")
                
                # INSTRUMENTATION: Publish halted risk escalation
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="RiskEscalation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "event_type": "RISK_LEVEL_ESCALATED",
                            "risk_level": "HALTED",
                            "message": "Trading halted",
                            "symbol": symbol,
                            "is_halted": True,
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return RiskLevel.HALTED

            risk_score = conditions.get("risk_score", 0.0)
            self.logger.debug(f"Risk Assessment: Meltdown risk score for {symbol}: {risk_score:.2f}")

            # Get thresholds from config
            critical_threshold = getattr(self._config_service, 'risk_score_critical_threshold', 0.75)
            high_threshold = getattr(self._config_service, 'risk_score_high_threshold', 0.60)
            elevated_threshold = getattr(self._config_service, 'risk_score_elevated_threshold', 0.40)

            if risk_score > critical_threshold:
                self.logger.warning(f"Risk Assessment: High risk score for {symbol} ({risk_score:.2f}). Risk: CRITICAL")
                
                # INSTRUMENTATION: Publish critical risk escalation
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="RiskEscalation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "event_type": "RISK_LEVEL_ESCALATED",
                            "risk_level": "CRITICAL",
                            "message": "Critical risk conditions met",
                            "symbol": symbol,
                            "factors": factors,
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return RiskLevel.CRITICAL
            elif risk_score > high_threshold:
                 self.logger.warning(f"Risk Assessment: Elevated risk score for {symbol} ({risk_score:.2f}). Risk: HIGH")
                 
                 # INSTRUMENTATION: Publish high risk escalation
                 if self._correlation_logger and master_correlation_id:

                     self._correlation_logger.log_event(
                         source_component="RiskManagementService",
                         event_name="RiskEscalation",
                         payload={
                             "correlation_id": master_correlation_id,
                             "event_type": "RISK_LEVEL_ESCALATED",
                             "risk_level": "HIGH",
                             "message": "High risk conditions met",
                             "symbol": symbol,
                             "factors": factors,
                             "causation_id": causation_id
                         }
                     )
                 return RiskLevel.HIGH
            elif risk_score > elevated_threshold:
                 self.logger.info(f"Risk Assessment: Moderate risk score for {symbol} ({risk_score:.2f}). Risk: ELEVATED")
                 
                 # INSTRUMENTATION: Publish elevated risk escalation
                 if self._correlation_logger and master_correlation_id:

                     self._correlation_logger.log_event(
                         source_component="RiskManagementService",
                         event_name="RiskEscalation",
                         payload={
                             "correlation_id": master_correlation_id,
                             "event_type": "RISK_LEVEL_ESCALATED",
                             "risk_level": "ELEVATED",
                             "message": "Elevated risk conditions met",
                             "symbol": symbol,
                             "factors": factors,
                             "causation_id": causation_id
                         }
                     )
                 return RiskLevel.ELEVATED
            else:
                self.logger.debug(f"Risk Assessment: Low risk score for {symbol} ({risk_score:.2f}). Risk: NORMAL")
                
                # INSTRUMENTATION: Publish normal risk level (de-escalation or initial assessment)
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="RiskEscalation",
                        payload={
                            "correlation_id": master_correlation_id,
                            "event_type": "RISK_LEVEL_NORMAL",
                            "risk_level": "NORMAL",
                            "message": "Risk conditions normal",
                            "symbol": symbol,
                            "factors": factors,
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )
                return RiskLevel.NORMAL

        except Exception as e:
             self.logger.error(f"Error checking meltdown conditions for {symbol}: {e}")
             return RiskLevel.UNKNOWN # Return unknown on error

    def get_risk_details(self, symbol: str) -> Dict[str, Any]:
        """Returns detailed factors from the meltdown detector."""
        self.logger.debug(f"Getting risk details for {symbol}")
        try:
            # Use analyze_symbol to get factors and metrics
            details = self._meltdown_detector.analyze_symbol(symbol)
            # Remove perf_timestamps before returning if present
            details.pop('perf_timestamps', None)
            return details
        except Exception as e:
            self.logger.error(f"Error getting risk details for {symbol}: {e}")
            return {"error": str(e)}

    def reset_all(self) -> None:
        """
        Resets all internal risk tracking state.
        Called when the system needs to clear all risk-related state,
        such as during a full system reset.
        """
        self.logger.info("Resetting all risk management state")
        with self._lock:
            # Reset entry times (use new monitored_positions structure)
            self._monitored_positions.clear()

        # Reset critical risk monitoring state with appropriate lock
        with self._risk_event_lock: # Use the new lock
            self._monitored_holdings.clear()
            # self._critical_risk_callbacks.clear() # Usually, callbacks are re-registered on init, not cleared on reset_all.
                                                  # If cleared, TMS needs to re-register after RMS reset.
                                                  # Let's NOT clear callbacks on reset_all, but clear last notified.
            self._last_notified_critical_risk.clear()
        self.logger.info("RMS internal states for monitored holdings and notifications reset.")

        # Reset meltdown detector state
        if self._meltdown_detector:
            try:
                self.logger.info("Calling PriceMeltdownDetector.reset()...")
                self._meltdown_detector.reset()
                self.logger.info("Meltdown detector state reset")
            except Exception as e:
                self.logger.error(f"Error calling PriceMeltdownDetector.reset(): {e}", exc_info=True)

    # --- Additional IRiskManagementService Methods ---
    def assess_order_risk(self, order_params: OrderParameters) -> RiskAssessment:
        """
        Assesses the risk of placing a specific order.
        Uses action_type to differentiate logic for entry/increase orders vs. exit/reduce orders.
        """
        symbol = order_params.symbol
        action = order_params.action_type  # Use the new field

        self.logger.debug(f"Assessing order risk for {symbol}: Action={action.name if action else 'N/A'}, Side={order_params.side} Qty={order_params.quantity}")

        # Determine if this is an opening or position-increasing action
        is_opening_or_increasing_long = False
        if action == TradeActionType.OPEN_LONG or action == TradeActionType.ADD_LONG:
            is_opening_or_increasing_long = True
        # Add similar logic if you handle shorts:
        # elif action == TradeActionType.OPEN_SHORT or action == TradeActionType.ADD_SHORT:
        #     is_opening_or_increasing_short = True

        if is_opening_or_increasing_long:  # Or is_opening_or_increasing_short
            self.logger.info(f"[{symbol}] Applying full pre-trade risk checks for OPEN/ADD action: {action.name if action else 'N/A'}")
            # --- Apply All Entry-Related Checks ---
            # Example: Check buying power (requires account data from OrderRepository)
            # if self._order_repository and hasattr(self._order_repository, 'get_account_data'):
            #     account_data = self._order_repository.get_account_data()
            #     buying_power = account_data.get('buying_power', 0.0)
            #     estimated_cost = order_params.quantity * (order_params.limit_price or self._price_provider.get_latest_price(symbol, True) or 0.0)
            #     if estimated_cost > buying_power:
            #         return RiskAssessment(level=RiskLevel.HALTED, reason=f"Insufficient buying power for {symbol}")

            # Example: Max notional (this is also in check_pre_trade_risk, decide where it lives best)
            if self._price_provider:
                max_notional = getattr(self._config_service, 'risk_max_notional_per_trade', 25000.0)
                try:
                    # Pass correlation_id from order_params for price fetching traceability
                    correlation_id = getattr(order_params, 'correlation_id', None)
                    current_price_for_notional = order_params.limit_price or self._price_provider.get_reliable_price(symbol, order_params.side, correlation_id=correlation_id)
                    
                    # INSTRUMENTATION: Check if price fetch failed in assess_order_risk
                    if current_price_for_notional is None or current_price_for_notional <= 0:
                        if self._correlation_logger and master_correlation_id:

                            self._correlation_logger.log_event(
                                source_component="RiskManagementService",
                                event_name="PriceProviderFailure",
                                payload={
                                    "correlation_id": master_correlation_id,
                                    "failure_type": "UNKNOWN",
                                    "symbol": symbol,
                                    "message": "Price provider failure",
                                    "causation_id": causation_id
                                }
                            )
                    elif current_price_for_notional and current_price_for_notional > 0:
                        notional_value = order_params.quantity * current_price_for_notional
                        if notional_value > max_notional:
                            return RiskAssessment(level=RiskLevel.HIGH, reason=f"Trade notional ${notional_value:.2f} exceeds limit ${max_notional:.2f}")
                            
                except Exception as e:
                    # INSTRUMENTATION: Publish price provider exception in assess_order_risk
                    if self._correlation_logger and master_correlation_id:

                        self._correlation_logger.log_event(
                            source_component="RiskManagementService",
                            event_name="PriceProviderFailure",
                            payload={
                                "correlation_id": master_correlation_id,
                                "failure_type": "UNKNOWN",
                                "symbol": symbol,
                                "message": "Price provider failure",
                                "causation_id": causation_id
                            },
                            stream_override="testrade:risk-actions"
                        )
            else:
                # INSTRUMENTATION: Publish price provider unavailable in assess_order_risk
                if self._correlation_logger and master_correlation_id:

                    self._correlation_logger.log_event(
                        source_component="RiskManagementService",
                        event_name="PriceProviderFailure",
                        payload={
                            "correlation_id": master_correlation_id,
                            "failure_type": "UNKNOWN",
                            "symbol": symbol,
                            "message": "Price provider failure",
                            "causation_id": causation_id
                        },
                        stream_override="testrade:risk-actions"
                    )

            # Return normal if entry checks specific to this method pass
            return RiskAssessment(level=RiskLevel.NORMAL, reason="Entry checks passed")

        elif action == TradeActionType.CLOSE_POSITION or action == TradeActionType.REDUCE_PARTIAL:
            self.logger.info(f"[{symbol}] Bypassing most entry-specific risk checks in assess_order_risk for CLOSE/REDUCE action: {action.name}")
            # For exits, most detailed checks might be skipped here, relying on check_pre_trade_risk for basic sanity.
            # Or, you could have exit-specific checks (e.g., "is price too far from NBBO for an exit?").
            return RiskAssessment(level=RiskLevel.NORMAL, reason="Exit order, assess_order_risk checks largely bypassed.")

        self.logger.debug(f"[{symbol}] No specific risk rule matched in assess_order_risk for action {action.name if action else 'N/A'}. Defaulting to NORMAL.")
        return RiskAssessment(level=RiskLevel.NORMAL, reason="Basic assessment (default)")

    def check_market_conditions(self, symbol: str, gui_logger_func=None, correlation_id: str = None) -> RiskAssessment:
        """
        Checks spread, basic liquidity based on current quote data.

        Args:
            symbol: The stock symbol to check
            gui_logger_func: Optional function to log messages to GUI, overrides instance's logger
            correlation_id: Optional correlation ID for end-to-end traceability
        """
        # Determine which logger to use for GUI messages
        logger_to_use = gui_logger_func if gui_logger_func is not None else self._gui_logger_func

        symbol_upper = symbol.upper()
        self.logger.debug(f"RiskService checking market conditions for {symbol_upper}")

        if not self._price_provider:
             self.logger.warning(f"Market condition check skipped for {symbol_upper}: No price provider available.")
             if logger_to_use:
                 logger_to_use(f"{symbol_upper} Market Risk: No price provider available", "warning")
             return RiskAssessment(level=RiskLevel.UNKNOWN, reason="No price provider")

        try:
            # Get current quote data using the injected price provider
            bid = self._price_provider.get_bid_price(symbol_upper)
            ask = self._price_provider.get_ask_price(symbol_upper)
            # Optional: Get sizes if the provider interface supports it
            # bid_size = self._price_provider.get_bid_size(symbol_upper)
            # ask_size = self._price_provider.get_ask_size(symbol_upper)

            # --- Add Checks Here ---

            # 1. Check if essential data is missing/invalid
            if bid is None or ask is None or bid <= 0 or ask <= 0 or (ask < bid): # Simpler ask < bid if both are numbers
                msg = (
                    f"Market conditions check for {symbol_upper}: Invalid or missing quote data "
                    f"(Bid: {bid if bid is not None else 'None'}, Ask: {ask if ask is not None else 'None'}). Setting HIGH risk."
                )
                self.logger.warning(msg)
                if logger_to_use:
                    logger_to_use(f"{symbol_upper} Market Risk: Invalid quote data", "warning")
                return RiskAssessment(level=RiskLevel.HIGH, reason="Invalid/missing quote data from price provider")

            # 2. Check Spread Percentage
            # If we reach here, bid and ask should be valid positive numbers due to the check above.
            mid_price = (ask + bid) / 2
            spread = ask - bid

            if mid_price <= 0: # This catches if ask+bid resulted in zero, e.g. if they were very small negatives somehow
                msg = f"Market conditions check for {symbol_upper}: Mid-price is zero or negative ({mid_price:.4f}) from (Bid: {bid:.4f}, Ask: {ask:.4f}). Setting HIGH risk."
                self.logger.warning(msg)
                if logger_to_use:
                    logger_to_use(f"{symbol_upper} Market Risk: Invalid mid-price (zero or negative)", "warning")
                return RiskAssessment(level=RiskLevel.HIGH, reason="Invalid mid-price (zero or negative) from quote")

            # The existing spread_pct calculation should now be safe if mid_price > 0
            spread_pct = spread / mid_price
            max_spread_threshold = getattr(self._config_service, 'risk_max_spread_pct_threshold', 0.01)
            self.logger.debug(f"RISK_SVC_DEBUG: Using max_spread_threshold = {max_spread_threshold} (Type: {type(max_spread_threshold)}) for spread_pct {spread_pct:.4f}") # Keep this debug
            if spread_pct < 0 : # Should not happen if ask >= bid (already checked by `ask < bid`)
                msg = f"Market conditions check for {symbol_upper}: Negative spread percentage ({spread_pct*100:.2f}%) indicates crossed quote (Ask {ask:.4f} < Bid {bid:.4f}). Setting HIGH risk."
                self.logger.warning(msg)
                if logger_to_use:
                    logger_to_use(f"{symbol_upper} Market Risk: Crossed quote detected (spread {spread_pct*100:.2f}%)", "warning")
                return RiskAssessment(level=RiskLevel.HIGH, reason=f"Crossed quote detected (spread {spread_pct*100:.2f}%)")

            if spread_pct > max_spread_threshold:
                msg = f"Market conditions check for {symbol_upper}: Spread too wide ({spread_pct*100:.2f}% > {max_spread_threshold*100:.2f}%)"
                self.logger.warning(msg)
                if logger_to_use:
                    logger_to_use(f"{symbol_upper} Market Risk: Spread too wide ({spread_pct*100:.2f}%)", "warning")

                # Publish risk trading stopped event for market halt conditions
                if self._correlation_logger:

                    self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="MarketHaltRiskEvent",
                payload={
                    "symbol": symbol_upper,
                    "halt_type": "MARKET_HALT_WIDE_SPREAD",
                    "message": f"Spread too wide ({spread_pct*100:.2f}% > {max_spread_threshold*100:.2f}%)",
                    "spread_pct": spread_pct,
                    "threshold": max_spread_threshold
                },
                stream_override="testrade:risk-actions"
            )

                return RiskAssessment(level=RiskLevel.HIGH, reason=f"Spread wide ({spread_pct*100:.2f}%)")

            # 3. Optional: Check Basic Liquidity (requires size info)
            # if bid_size < self.MIN_BID_ASK_SIZE or ask_size < self.MIN_BID_ASK_SIZE:
            #     self.logger.warning(f"Market conditions check for {symbol_upper}: Low liquidity (BidSz: {bid_size}, AskSz: {ask_size})")
            #     return RiskAssessment(level=RiskLevel.ELEVATED, reason="Low NBBO liquidity")

            # If all checks pass
            self.logger.debug(f"Market conditions check OK for {symbol_upper} (Spread: {spread:.2f})")
            return RiskAssessment(level=RiskLevel.NORMAL, reason="Market conditions nominal")

        except TypeError as te:
            # Specifically catch TypeError which might occur if None values are compared or used in calculations
            msg = f"TypeError in market conditions check for {symbol_upper}: {te}. This may indicate None values from price provider."
            self.logger.error(msg, exc_info=True)
            if logger_to_use:
                logger_to_use(f"{symbol_upper} Market Risk: TypeError in market check (possible None values)", "error")
            return RiskAssessment(level=RiskLevel.HIGH, reason=f"TypeError in market conditions check (possible None values): {te}")
        except Exception as e:
            msg = f"Error checking market conditions for {symbol_upper}: {e}"
            self.logger.error(msg, exc_info=True)
            if logger_to_use:
                logger_to_use(f"{symbol_upper} Market Risk: Error checking market conditions", "error")
            return RiskAssessment(level=RiskLevel.UNKNOWN, reason=f"Error checking conditions: {e}")

    def check_pre_trade_risk(self, symbol: str, side: str, quantity: float, limit_price: Optional[float],
                         order_action_type: Optional[TradeActionType] = None, # <<< ADDED PARAMETER
                         gui_logger_func=None) -> bool:
        """
        Checks if a proposed trade meets risk criteria before execution.

        Args:
            symbol: The stock symbol
            side: "BUY" or "SELL"
            quantity: Number of shares
            limit_price: Optional limit price
            order_action_type: Optional TradeActionType to differentiate logic for entry vs exit orders
            gui_logger_func: Optional callback function to log messages to the GUI

        Returns:
            bool: True if trade passes risk checks, False otherwise
        """
        # Determine which logger to use for GUI messages
        logger_to_use = gui_logger_func if gui_logger_func is not None else self._gui_logger_func

        # Determine if this is a reducing or closing action
        is_reducing_or_closing_long = (order_action_type in (TradeActionType.CLOSE_POSITION, TradeActionType.REDUCE_PARTIAL) and side.upper() == "SELL")
        # Add similar for closing shorts if applicable:
        # is_reducing_or_closing_short = (order_action_type in (TradeActionType.CLOSE_POSITION, TradeActionType.REDUCE_PARTIAL) and side.upper() == "BUY")

        if order_action_type:
            self.logger.info(f"[{symbol}] Pre-trade risk check for {order_action_type.name} action, side={side}, qty={quantity}")
        else:
            self.logger.info(f"[{symbol}] Pre-trade risk check for side={side}, qty={quantity} (no action_type provided)")

        # Get risk parameters from config service
        max_shares = getattr(self._config_service, 'risk_max_shares_per_trade', 1000)
        max_notional = getattr(self._config_service, 'risk_max_notional_per_trade', 25000.0)

        # Check quantity limit (can apply to all order types to catch fat fingers)
        if quantity > max_shares:
            warning_msg = f"RISK: Trade size {quantity} for {symbol} {side} ({order_action_type.name if order_action_type else 'N/A'}) exceeds limit {max_shares}"
            self.logger.warning(warning_msg)
            if logger_to_use:
                logger_to_use(f"RISK WARNING: {symbol} trade stopped - Share size {quantity} exceeds limit of {max_shares}", "warning")
            return False

        # Check notional value limit if price is available (can apply to all order types)
        if limit_price is not None:
            notional_value = quantity * limit_price
            if notional_value > max_notional:
                warning_msg = f"RISK: Trade value ${notional_value:.2f} exceeds limit ${max_notional:.2f} for {symbol}"
                self.logger.warning(warning_msg)
                if logger_to_use:
                    logger_to_use(f"RISK WARNING: {symbol} trade stopped - Value ${notional_value:.2f} exceeds limit of ${max_notional:.2f}", "warning")
                return False
        elif self._price_provider:
            # Try to get current price from price provider
            try:
                current_price = self._price_provider.get_latest_price_sync(symbol)
                if current_price is not None and current_price > 0:
                    notional_value = quantity * current_price
                    if notional_value > max_notional:
                        warning_msg = f"RISK: Estimated trade value ${notional_value:.2f} exceeds limit ${max_notional:.2f} for {symbol}"
                        self.logger.warning(warning_msg)
                        if logger_to_use:
                            logger_to_use(f"RISK WARNING: {symbol} trade stopped - Estimated value ${notional_value:.2f} exceeds limit of ${max_notional:.2f}", "warning")
                        return False
                elif current_price is None:
                    self.logger.warning(f"RISK: Cannot check notional value for {symbol} - price provider returned None")
                    # Optionally, you could return False here to be extra cautious when price is None
                    # For now, we'll continue with other checks
            except TypeError as te:
                self.logger.error(f"TypeError getting price for risk check: {te}. This may indicate None values from price provider.")
            except Exception as e:
                self.logger.error(f"Error getting price for risk check: {e}")

        # Position concentration checks (most relevant for opening/increasing positions)
        if not is_reducing_or_closing_long:  # (and not is_reducing_or_closing_short if implemented)
            try:
                current_position = 0.0
                if self._position_manager:
                    pos_data = self._position_manager.get_position(symbol)
                    current_position = pos_data.get("shares", 0.0) if pos_data else 0.0

                    # For buys, check if new position would exceed max position size
                    if side.upper() == "BUY":
                        max_position = getattr(self._config_service, 'risk_max_position_size', 2000)
                        new_position = current_position + quantity

                        # Check absolute prospective position against limit
                        if new_position > max_position:
                            warning_msg = f"RISK: Prospective position size {new_position:.0f} for {symbol} would exceed max {max_position} (Current: {current_position:.0f}, Order: {side} {quantity})"
                            self.logger.warning(warning_msg)
                            if logger_to_use:
                                logger_to_use(f"RISK WARNING: {symbol} trade stopped - New position {new_position:.0f} would exceed max {max_position}", "warning")
                            return False
                else:
                    self.logger.warning(f"[{symbol}] Cannot check position concentration in check_pre_trade_risk: PositionManager not available.")
            except Exception as e:
                self.logger.error(f"Error checking position for risk assessment: {e}")
        else:
            self.logger.info(f"[{symbol}] Skipping position concentration check for closing/reducing action: {order_action_type.name if order_action_type else 'N/A'}")

        # All checks passed
        return True

    # --- Service Lifecycle Methods ---
    def start(self) -> None:
        """Start the RiskManagementService and its worker threads."""
        self.logger.info("RiskManagementService starting...")

        # Create ThreadPoolExecutor (moved from __init__)
        if self._pmd_worker_pool is None:
            self._pmd_worker_pool = ThreadPoolExecutor(
                max_workers=self._num_pmd_workers,
                thread_name_prefix="RiskPMDWorker"
            )

        # Subscribe to EventBus events (moved from __init__)
        self._subscribe_to_events()

        self._is_running = True

        # Start PMD worker threads
        for _ in range(self._num_pmd_workers):
            self._pmd_worker_pool.submit(self._process_pmd_queue)

        # IPC worker no longer needed - using correlation logger instead
        self.logger.info("RiskService using correlation logger for instrumentation")

        self.logger.info(f"RiskManagementService started with {self._num_pmd_workers} PMD worker thread(s).")
        
        # INSTRUMENTATION: Publish service health status - STARTED
        # Now safe to call directly thanks to Dual Buffer architecture
        if self._correlation_logger:

            self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="ServiceHealthStatus",
                payload={
                    "status": "STARTED",
                    "message": "RiskManagementService started successfully",
                    "start_time": time.time()
                },
                stream_override="testrade:system-health"
            )

    # --- New IRiskManagementService Interface Methods ---
    def start_monitoring_holding(self, symbol: str) -> None:
        symbol_upper = symbol.upper()
        with self._risk_event_lock: # Use the new lock
            if symbol_upper not in self._monitored_holdings:
                self._monitored_holdings.add(symbol_upper)
                self.logger.info(f"RMS started monitoring holding for: {symbol_upper}")
            else:
                self.logger.debug(f"RMS already monitoring holding for: {symbol_upper}")

    def stop_monitoring_holding(self, symbol: str) -> None:
        symbol_upper = symbol.upper()
        with self._risk_event_lock: # Use the new lock
            if symbol_upper in self._monitored_holdings:
                self._monitored_holdings.remove(symbol_upper)
                # Also clear last notified state for this symbol if it's no longer monitored
                if symbol_upper in self._last_notified_critical_risk:
                    del self._last_notified_critical_risk[symbol_upper]
                self.logger.info(f"RMS stopped monitoring holding for: {symbol_upper}")

    def stop(self) -> None:
        """Stop the RiskManagementService and clean up resources."""
        logger.info("RiskManagementService stopping...")

        self._is_running = False

        # Signal PMD workers to stop
        self._stop_pmd_event.set()

        # Drain the PMD queue to prevent processing stale items
        drained_count = 0
        while not self._pmd_input_queue.empty():
            try:
                self._pmd_input_queue.get_nowait()
                drained_count += 1
            except QueueEmpty:
                break

        if drained_count > 0:
            self.logger.info(f"RiskManagementService: Drained {drained_count} items from PMD queue during stop.")

        # Shutdown PMD worker pool - DEFENSIVE SHUTDOWN LOGIC
        if self._pmd_worker_pool:
            self.logger.info("Shutting down PMD worker pool...")
            try:
                import sys
                if sys.version_info >= (3, 9):
                    self._pmd_worker_pool.shutdown(wait=True, cancel_futures=True)
                else:
                    self._pmd_worker_pool.shutdown(wait=True)
                self.logger.info("PMD worker pool shut down successfully.")
            except Exception as e:
                self.logger.error(f"Error shutting down PMD worker pool: {e}", exc_info=True)
        else:
            # This will be logged if start() was never called successfully
            self.logger.warning("PMD worker pool was not initialized; nothing to shut down.")
            
        # INSTRUMENTATION: Publish service health status - STOPPED
        if self._correlation_logger:

            self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="ServiceHealthStatus",
                payload={
                    "status": "STOPPED",
                    "message": "RiskManagementService stopped and cleaned up resources",
                    "stop_time": time.time()
                },
                stream_override="testrade:system-health"
            )

        # Clear monitored holdings and callbacks
        with self._risk_event_lock:
            self._monitored_holdings.clear()
            self._critical_risk_callbacks.clear()
            self._last_notified_critical_risk.clear()

        # Reset all risk tracking state
        self.reset_all()

        logger.info("RiskManagementService stopped.")

    def register_critical_risk_callback(self, callback: CriticalRiskCallback) -> None:
        with self._risk_event_lock: # Use the new lock
            if callback not in self._critical_risk_callbacks:
                self._critical_risk_callbacks.append(callback)
                self.logger.info(f"RMS registered critical risk callback: {callback}")

    def unregister_critical_risk_callback(self, callback: CriticalRiskCallback) -> None:
        with self._risk_event_lock: # Use the new lock
            if callback in self._critical_risk_callbacks:
                try:
                    self._critical_risk_callbacks.remove(callback)
                    self.logger.info(f"RMS unregistered critical risk callback: {callback}")
                except ValueError: # Should not happen if 'in' check is done
                    pass

    # --- Internal Helper Methods ---
    def _check_and_notify_holding_risk(self, symbol: str) -> None:
        """Assesses holding risk for a symbol and notifies if critical and new/changed."""
        symbol_upper = symbol.upper()

        # Only proceed if this symbol is actively monitored for holding risk
        with self._risk_event_lock: # Check _monitored_holdings under lock
            if symbol_upper not in self._monitored_holdings:
                return

        current_level = self.assess_holding_risk(symbol_upper) # This uses PriceMeltdownDetector

        with self._risk_event_lock: # Lock for _last_notified_critical_risk and _critical_risk_callbacks
            last_notified = self._last_notified_critical_risk.get(symbol_upper)

            # Determine if this risk level implies a trading stop decision initiated by RMS
            # This depends on how RMS interacts with TradeManagerService or trading flags.
            # For this event, we care about when RMS *itself* determines a stop is needed.

            risk_implies_stop = current_level in [RiskLevel.CRITICAL, RiskLevel.HALTED]
                                # Add any other levels that trigger a stop

            if risk_implies_stop and last_notified != current_level: # New or changed critical/halted state
                self._last_notified_critical_risk[symbol_upper] = current_level # Update last notified
                details = self.get_risk_details(symbol_upper)

                # Prepare RiskTradingStoppedData
                from core.events import RiskTradingStoppedData
                stop_data = RiskTradingStoppedData(
                    symbol=symbol_upper,
                    reason_code=f"{current_level.name}_{details.get('primary_factor','GENERAL_RISK')}".upper(),
                    description=f"{current_level.name} risk detected for {symbol_upper}: {details.get('factors_summary', 'See details')}",
                    risk_level_at_stoppage=current_level.name,
                    details=details,
                    # triggering_correlation_id: If an order request (with correlation_id) led to this risk assessment,
                    # that ID should be passed through. This is harder if check_and_notify is called from on_trade/on_quote.
                    # For now, assume None unless explicitly available.
                    triggering_correlation_id=details.get("triggering_correlation_id") # If PMD can capture this
                )
                if self._correlation_logger and stop_data.triggering_correlation_id:

                    self._correlation_logger.log_event(
                source_component="RiskManagementService",
                event_name="RiskTradingStopped",
                payload={
                    "correlation_id": stop_data.triggering_correlation_id,
                    "symbol": stop_data.symbol,
                    "stop_reason": stop_data.stop_reason,
                    "stop_details": stop_data.stop_details,
                    "source": stop_data.source,
                    "timestamp": stop_data.timestamp
                },
                stream_override="testrade:risk-actions"
            ) # Call the new helper

                # Also invoke existing critical_risk_callbacks for TMS
                self.logger.critical(f"RMS: {current_level.name} risk for {symbol_upper}. Notifying TMS callbacks. Details: {details.get('factors')}")
                for cb in self._critical_risk_callbacks:
                    try:
                        cb(symbol_upper, current_level, details)
                    except Exception as e:
                        self.logger.error(f"RMS: Error in critical risk callback {cb} for {symbol_upper}: {e}", exc_info=True)

            elif not risk_implies_stop and last_notified is not None: # Condition cleared
                if symbol_upper in self._last_notified_critical_risk:
                    del self._last_notified_critical_risk[symbol_upper]
                self.logger.info(f"RMS: Risk condition for {symbol_upper} cleared or downgraded from CRITICAL/HALTED. Current level: {current_level.name}")
                # Optionally publish a "TRADING_RESUMED_BY_RISK" event if needed.

    def _get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        from datetime import datetime
        return datetime.utcnow().isoformat() + 'Z'

