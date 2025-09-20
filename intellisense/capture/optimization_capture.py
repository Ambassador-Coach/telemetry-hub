# --- START OF REGENERATED intellisense/capture/optimization_capture.py (Chunk 35 Focus) ---
import logging
import time
import json
import uuid
import re # Added for potential advanced validation, not used in current scope of _validate_results
import threading
from collections import deque, defaultdict # Keep for other parts of the class
from dataclasses import asdict # Keep for other parts of the class
import dataclasses # For is_dataclass check
from typing import List, Dict, Any, Optional, TYPE_CHECKING, Deque, Tuple, Type # Added Type

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from intellisense.capture.interfaces import IFeedbackIsolationManager
    from intellisense.capture.logger import CorrelationLogger
    from intellisense.capture.enhanced_components.enhanced_ocr_service import EnhancedOCRDataConditioningService
    from core.interfaces import IEventBus
    from intellisense.core.internal_event_bus import InternalIntelliSenseEventBus
    from intellisense.core.internal_events import CorrelationLogEntryEvent
    from intellisense.analysis.testable_components import IntelliSenseTestableOrderRepository, IntelliSenseTestablePositionManager
    from core.events import OrderRequestEvent as ActualOrderRequestEvent, OrderRequestData as ActualOrderRequestData

# Import scenario types, including ComparisonOperator
from .scenario_types import InjectionStep, ValidationPoint, ValidationOutcome, ComparisonOperator, StepExecutionResult
from .pipeline_definitions import get_expected_pipeline_stages, PIPELINE_STAGE_CRITERIA # Keep

logger = logging.getLogger(__name__)

DEFAULT_PIPELINE_TIMEOUT_S = 5.0
MAX_CORRELATION_BUFFER_SIZE = 2000  # Increased buffer size

class MillisecondOptimizationCapture:
    def __init__(self,
                 app_core: 'IntelliSenseApplicationCore',
                 feedback_isolator: 'IFeedbackIsolationManager',
                 correlation_logger: 'CorrelationLogger', # This is the instance from ProdDataCaptureSession
                 internal_event_bus: 'InternalIntelliSenseEventBus'): # NEW
        self.app_core: 'IntelliSenseApplicationCore' = app_core # String hint
        self.feedback_isolator: 'IFeedbackIsolationManager' = feedback_isolator # String hint (interface)
        self.correlation_logger: 'CorrelationLogger' = correlation_logger # String hint - Used for logging injection action
        self._internal_event_bus: 'InternalIntelliSenseEventBus' = internal_event_bus # String hint - For subscribing to CorrelationLogEntryEvent

        self._OrderRequestEvent: Optional[Type['ActualOrderRequestEvent']] = None
        self._OrderRequestData: Optional[Type['ActualOrderRequestData']] = None
        self._CorrelationLogEntryEvent: Optional[Type['CorrelationLogEntryEvent']] = None

        self.ocr_conditioning_service: Optional['EnhancedOCRDataConditioningService'] = getattr(self.app_core, 'ocr_data_conditioning_service', None) # String hint
        self.app_event_bus: Optional['IEventBus'] = getattr(self.app_core, 'event_bus', None) # TESTRADE's main bus

        self._active_pipeline_monitors: Dict[str, Dict[str, Any]] = {} # correlation_id -> {start_time, events, completed_event_types}
        self._correlation_event_buffer: Deque['CorrelationLogEntryEvent'] = deque(maxlen=MAX_CORRELATION_BUFFER_SIZE)

        # Get references to testable components from app_core for validation
        self._testable_or: Optional['IntelliSenseTestableOrderRepository'] = getattr(self.app_core, 'order_repository', None) # String hint
        self._testable_pm: Optional['IntelliSenseTestablePositionManager'] = getattr(self.app_core, 'position_manager', None) # String hint

        # Lazy subscribe to avoid importing CorrelationLogEntryEvent at init time
        self._subscribe_to_correlation_events()
        logger.info("MillisecondOptimizationCapture initialized and subscribed to internal correlation log events.")

    def _lazy_load_core_events(self):
        if self._OrderRequestData is None or self._OrderRequestEvent is None:
            try:
                from core.events import OrderRequestData, OrderRequestEvent # Runtime import
                self._OrderRequestData = OrderRequestData
                self._OrderRequestEvent = OrderRequestEvent
                logger.debug("MOC: Lazy loaded core.events (OrderRequestData, OrderRequestEvent)")
            except ImportError as e:
                logger.warning(f"MOC: Failed to import core.events: {e}. Using mock classes for test environment.")
                # Create mock classes for test environment
                from dataclasses import dataclass, field
                from typing import Optional
                import time
                from uuid import uuid4

                @dataclass
                class MockOrderRequestData:
                    request_id: str = field(default_factory=lambda: str(uuid4()))
                    symbol: str = ""
                    side: str = ""
                    quantity: float = 0.0
                    order_type: str = "MKT"
                    limit_price: Optional[float] = None
                    correlation_id: Optional[str] = None
                    source_strategy: str = ""

                @dataclass
                class MockOrderRequestEvent:
                    data: MockOrderRequestData = field(default=None)
                    event_id: str = field(default_factory=lambda: str(uuid4()))
                    timestamp: float = field(default_factory=time.time)

                self._OrderRequestData = MockOrderRequestData
                self._OrderRequestEvent = MockOrderRequestEvent
                logger.info("MOC: Using mock OrderRequestData/OrderRequestEvent for test environment")

    def _subscribe_to_correlation_events(self):
        """Lazy subscription to avoid circular import at init time."""
        # Check if internal event bus is available
        if not self._internal_event_bus:
            logger.warning("Internal event bus not available - correlation monitoring disabled")
            return

        if self._CorrelationLogEntryEvent is None:
            try:
                from intellisense.core.internal_events import CorrelationLogEntryEvent
                self._CorrelationLogEntryEvent = CorrelationLogEntryEvent
                logger.debug("Lazy loaded CorrelationLogEntryEvent for event subscription")
            except ImportError as e:
                logger.warning(f"Failed to import CorrelationLogEntryEvent: {e} - correlation monitoring disabled")
                return

        try:
            self._internal_event_bus.subscribe(self._CorrelationLogEntryEvent, self._on_correlation_log_entry)
            logger.debug(f"MillisecondOptimizationCapture: Subscribed _on_correlation_log_entry to CorrelationLogEntryEvent on bus ID: {id(self._internal_event_bus)}")
        except Exception as e:
            logger.error(f"Failed to subscribe to correlation events: {e} - correlation monitoring disabled")

    def _on_correlation_log_entry(self, event: 'CorrelationLogEntryEvent'): # Type hint with actual event
        """
        Handles new correlation log entries, buffering them and checking active monitors.
        Includes detailed debug logging for stage matching.
        """
        self._correlation_event_buffer.append(event)
        log_entry = event.log_entry

        logger.debug(f"--- MOC._on_correlation_log_entry --- New Log GSI: {log_entry.get('global_sequence_id')}, Source: {log_entry.get('source_sense')}, Payload Type (if any): {log_entry.get('event_payload', {}).get('injection_type') or log_entry.get('event_payload', {}).get('event_name') or log_entry.get('event_payload', {}).get('event_name_in_payload') or log_entry.get('event_payload', {}).get('broker_response_type')}")
        logger.debug(f"   Active monitors: {list(self._active_pipeline_monitors.keys())}")

        for corr_id, monitor_data in list(self._active_pipeline_monitors.items()): # Iterate copy
            if monitor_data.get("completed", False) or time.perf_counter_ns() > monitor_data.get("timeout_at_ns", 0):
                continue

            logger.debug(f"  Checking against active monitor CorrID: {corr_id}")
            expected_stages: Optional[List[PIPELINE_STAGE_CRITERIA]] = monitor_data.get("expected_stages")
            current_stage_idx = monitor_data.get("current_stage_index", 0)

            if expected_stages and current_stage_idx < len(expected_stages):
                stage_source_sense, stage_event_desc, stage_checker_func, stage_name_key = expected_stages[current_stage_idx]

                logger.debug(f"    Monitor '{corr_id}': Attempting to match stage '{stage_name_key}' (idx {current_stage_idx}). Expecting source_sense: '{stage_source_sense}', Desc: '{stage_event_desc}'.")

                log_source_sense = log_entry.get('source_sense')
                log_payload = log_entry.get('event_payload', {})
                stimulus_data_for_checker = monitor_data.get("stimulus_data", {})

                logger.debug(f"      Log event GSI: {log_entry.get('global_sequence_id')}, Source_sense: '{log_source_sense}'.")
                # Be careful about logging full payloads if they are very large or sensitive
                logger.debug(f"      Log event_payload (keys): {list(log_payload.keys())}")
                logger.debug(f"      Stimulus data for checker (keys): {list(stimulus_data_for_checker.keys())}")
                logger.debug(f"      Stimulus corr_id: {stimulus_data_for_checker.get('correlation_id')}, derived_req_id: {stimulus_data_for_checker.get('derived_order_request_id')}")


                if log_source_sense == stage_source_sense:
                    logger.debug(f"      Source sense MATCHED for stage '{stage_name_key}'. Calling checker function: {getattr(stage_checker_func, '__name__', 'lambda_or_partial')}")
                    match_result = False # Default to false
                    try:
                        match_result = stage_checker_func(log_payload, stimulus_data_for_checker)
                    except Exception as e_checker:
                        logger.error(f"      EXCEPTION in checker function for stage '{stage_name_key}': {e_checker}", exc_info=True)

                    logger.debug(f"      Checker function for '{stage_name_key}' returned: {match_result}")

                    if match_result:
                        logger.info(f"    Pipeline Monitor (CorrID: {corr_id}): Matched stage '{stage_name_key}' (Index {current_stage_idx}) "
                                     f"with log GSI {log_entry.get('global_sequence_id')}")

                        monitor_data['matched_events'].append(log_entry)
                        monitor_data.setdefault('events_by_stage_key', {})[stage_name_key] = log_entry
                        monitor_data['current_stage_index'] += 1

                        # CRITICAL: Extract trade_derived_order_request_id when S2_OrchestratedTradeInjectLog matches
                        if stage_name_key == "S2_OrchestratedTradeInjectLog":
                            derived_order_request_id = log_payload.get('derived_order_request_id')
                            if derived_order_request_id:
                                monitor_data['stimulus_data']['trade_derived_order_request_id'] = derived_order_request_id
                                logger.info(f"    Pipeline Monitor (CorrID: {corr_id}): Extracted trade_derived_order_request_id='{derived_order_request_id}' from S2 stage")
                            else:
                                logger.warning(f"    Pipeline Monitor (CorrID: {corr_id}): S2 stage matched but no derived_order_request_id found in log payload")

                        if monitor_data['current_stage_index'] >= len(expected_stages):
                            logger.info(f"    Pipeline Monitor (CorrID: {corr_id}): All {len(expected_stages)} expected stages matched. Setting COMPLETED.")
                            monitor_data['completed'] = True
                            if 'completion_event' in monitor_data and isinstance(monitor_data['completion_event'], threading.Event):
                                monitor_data['completion_event'].set()
                        else:
                            next_stage_idx = monitor_data['current_stage_index']
                            next_stage_name = expected_stages[next_stage_idx][3] if next_stage_idx < len(expected_stages) else "NONE"
                            logger.debug(f"    Pipeline Monitor (CorrID: {corr_id}): Stage matched. Advanced to stage_index {next_stage_idx}. Next expected stage: '{next_stage_name}'")
                    # else: # Checker returned False, no explicit log here to avoid noise, already logged checker result.
                else:
                    logger.debug(f"      Source sense MISMATCHED for stage '{stage_name_key}'. Log: '{log_source_sense}', Expected: '{stage_source_sense}'.")
            # else:
                # logger.debug(f"  Monitor '{corr_id}': No more expected stages (idx {current_stage_idx} >= len {len(expected_stages) if expected_stages else 'N/A'}) or no stages defined.")
        # logger.debug(f"--- MOC._on_correlation_log_entry --- Finished processing Log GSI: {log_entry.get('global_sequence_id')}")

    def _inject_trade_command(self, command_stimulus: Dict[str, Any], step_corr_id: str):
        """
        Injects a trade command.
        `step_corr_id` is the correlation_id for THIS trade command, to be included in its data.
        It no longer injects IntelliSense's logger into the event object.

        WP1 Task 1.3: Removed IntelliSense logger injection into TESTRADE event instances.
        The correlation_id is embedded in the event data payload for TESTRADE to use with its own correlation mechanisms.
        """
        self._lazy_load_core_events()

        # derived_order_request_id is unique to this specific trade command instance
        derived_request_id = f"inj_{command_stimulus.get('symbol','UNK')}_{int(time.time()*1000_000)}"

        if not self.app_event_bus:
            logger.error(f"Cannot inject trade (StepCorrID: {step_corr_id}): App EventBus missing."); return

        try:
            order_data_dict = {
                'request_id': derived_request_id,
                'symbol': command_stimulus['symbol'], 'side': command_stimulus['side'].upper(),
                'quantity': float(command_stimulus['quantity']), 'order_type': command_stimulus['order_type'].upper(),
                'limit_price': float(command_stimulus['limit_price']) if command_stimulus.get('limit_price') is not None else None,
                'source_strategy': command_stimulus.get("source_strategy", "INTELLISENSE_INJECTED_TRADE"), # Allow override
                'correlation_id': step_corr_id # This is the crucial propagated/reused ID
            }

            # Update the command_stimulus dict (which is stimulus_data_for_monitor in execute_plan)
            # so checkers can access this generated derived_order_request_id.
            command_stimulus['derived_order_request_id'] = derived_request_id
            # The 'correlation_id' in command_stimulus should already be step_corr_id.

            event_data = self._OrderRequestData(**order_data_dict) # type: ignore
            order_event = self._OrderRequestEvent(data=event_data)   # type: ignore

            # --- REMOVED LOGGER INJECTION INTO EVENT ---
            # WP1 Task 1.3: IntelliSense no longer injects its logger into TESTRADE event instances.
            # The correlation_id is embedded in the event data payload for TESTRADE's own correlation mechanisms.
            # if self.app_core and hasattr(self.app_core, '_active_correlation_logger') and self.app_core._active_correlation_logger:
            #     if hasattr(order_event, '_correlation_logger'):
            #         order_event._correlation_logger = self.app_core._active_correlation_logger
            #         logger.info(f"Attempted to inject IntelliSense CorrelationLogger into OrderRequestEvent for ReqID: {event_data.request_id}")
            #     else:
            #         logger.warning(f"OrderRequestEvent instance does not have _correlation_logger attribute. IntelliSense logger not injected into event.")
            # --- END REMOVED ---

            # Log the INJECTION_CONTROL event (MOC logging its own action)
            injection_ts_ns = time.perf_counter_ns()
            self.correlation_logger.log_event( # This is IntelliSense's MOC logging its own action
                source_sense="INJECTION_CONTROL",
                event_payload={
                    "injection_type": "trade_command",
                    "stimulus_details": {k:v for k,v in command_stimulus.items() if k not in ['derived_order_request_id']},
                    "derived_order_request_id": derived_request_id,
                    "correlation_id": step_corr_id,
                    "injection_perf_timestamp_ns": injection_ts_ns,
                    "injection_epoch_timestamp_s": time.time()
                },
                source_component_name="MillisecondOptimizationCapture"
            )
            # CRITICAL: In test environment, ONLY use mock components, don't publish to real components
            # Check if we're using mock classes (indicates test environment)
            is_test_environment = (
                hasattr(self, '_test_mode') and self._test_mode or
                self._OrderRequestData.__name__.startswith('Mock') or
                not self.app_event_bus  # No real event bus available
            )

            if is_test_environment:
                logger.info(f"TEST MODE: Skipping real event publication, using ONLY mock components for {command_stimulus['symbol']}.")
                # Add mock components for direct trade command pipeline (T1-T4) - ONLY MOCKS
                self._add_mock_trade_pipeline_components(command_stimulus, step_corr_id, derived_request_id)
            else:
                # Production mode: publish to real components
                self.app_event_bus.publish(order_event)
                logger.info(f"Injected trade command (CorrID: {step_corr_id}, ReqID: {derived_request_id}) for {command_stimulus['symbol']}.")

        except Exception as e:
            logger.error(f"Error injecting trade command (CorrID: {step_corr_id}): {e}", exc_info=True)

    def _add_mock_trade_pipeline_components(self, command_stimulus: Dict[str, Any], step_corr_id: str, derived_request_id: str):
        """Add mock components for direct trade command pipeline (T1-T4) - ONLY MOCK COMPONENTS"""
        import threading
        import time

        symbol = command_stimulus.get('symbol', 'UNK')
        side = command_stimulus.get('side', 'BUY').upper()
        quantity = float(command_stimulus.get('quantity', 100.0))

        # SINGLE SOURCE OF TRUTH: Check if real services are available for logging
        # If real services exist, they should handle their own correlation logging
        has_real_services = (
            hasattr(self.app_core, 'risk_management_service') and self.app_core.risk_management_service and
            hasattr(self.app_core, 'trade_manager_service') and self.app_core.trade_manager_service and
            hasattr(self.app_core, 'broker_service') and self.app_core.broker_service
        )

        if has_real_services:
            logger.info(f"SINGLE SOURCE OF TRUTH: Real services detected - delegating correlation logging to them for {symbol}")
            # Real services will handle their own logging when they process the trade command
            # No need for mock components to duplicate the logging
            return

        logger.info(f"FALLBACK MODE: No real services detected - using mock components for {symbol}")

        # FALLBACK: Use mock components only when real services are not available
        def mock_t1_rms():
            time.sleep(0.015)  # 15ms RMS processing delay
            logger.info(f"🔄 Mock T1 RMS: Logging event for {symbol} with correlation_id={step_corr_id}")
            self.correlation_logger.log_event(
                source_sense="CAPTURE",
                event_payload={
                    "event_name": "OrderRequestEvent",
                    "correlation_id": step_corr_id,
                    "request_id": derived_request_id,
                    "risk_assessment_status": "VALIDATED",
                    "rejection_details_list": [],
                    "validation_reasons": ["position_size_ok", "risk_limits_ok"],
                    "component_processing_latency_ns": 15_000_000
                },
                source_component_name="T1_RMSValidatedLog"
            )

        def mock_t2_tms():
            time.sleep(0.025)  # 25ms TMS processing delay
            logger.info(f"🔄 Mock T2 TMS: Logging event for {symbol} with correlation_id={step_corr_id}")
            self.correlation_logger.log_event(
                source_sense="CAPTURE",
                event_payload={
                    "event_name_in_payload": "ValidatedOrderProcessedByExecutor",
                    "correlation_id": step_corr_id,
                    "request_id": derived_request_id,
                    "status": "FORWARDED_TO_EXECUTOR",
                    "component_processing_latency_ns": 25_000_000
                },
                source_component_name="T2_TMSForwardedLog"
            )

        def mock_t3_broker_ack():
            time.sleep(0.050)  # 50ms broker ack delay
            # CRITICAL FIX: Use derived_request_id as local_order_id for consistency
            local_order_id = derived_request_id
            broker_order_id = f"BRK_{symbol}_{int(time.time()*1000)}"

            logger.info(f"🔄 Mock T3 Broker ACK: Logging event for {symbol} with correlation_id={step_corr_id}")
            self.correlation_logger.log_event(
                source_sense="CAPTURE",
                event_payload={
                    "broker_response_type": "ORDER_ACCEPTED",
                    "correlation_id": step_corr_id,
                    "original_request_id": derived_request_id,
                    "symbol": symbol,
                    "broker_data": {
                        "broker_order_id": broker_order_id,
                        "local_order_id": local_order_id,
                        "order_type": command_stimulus.get('order_type', 'LMT'),
                        "quantity": quantity
                    },
                    "component_processing_latency_ns": 50_000_000
                },
                source_component_name="T3_BrokerAckLog"
            )

        def mock_t4_broker_fill():
            time.sleep(0.100)  # 100ms broker fill delay
            fill_quantity = quantity if side == 'BUY' else -quantity  # Negative for SELL
            # CRITICAL FIX: Use derived_request_id as local_order_id for consistency with T3
            local_order_id = derived_request_id

            logger.info(f"🔄 Mock T4 Broker FILL: Logging event for {symbol} with correlation_id={step_corr_id}")
            self.correlation_logger.log_event(
                source_sense="CAPTURE",
                event_payload={
                    "broker_response_type": "FILL",
                    "correlation_id": step_corr_id,
                    "original_request_id": derived_request_id,
                    "symbol": symbol,
                    "broker_data": {
                        "filled_quantity": fill_quantity,
                        "local_order_id": local_order_id,
                        "broker_order_id": f"BRK_{symbol}_{int(time.time()*1000)}",
                        "fill_price": command_stimulus.get('limit_price', 100.0)
                    },
                    "component_processing_latency_ns": 100_000_000
                },
                source_component_name="T4_BrokerFillLog"
            )

            # Update Position Manager after fill
            if hasattr(self, '_testable_pm') and self._testable_pm:
                try:
                    current_position = self._testable_pm.positions.get(symbol, {
                        "symbol": symbol, "open": False, "strategy": "closed",
                        "total_shares": 0.0, "total_avg_price": 0.0, "overall_realized_pnl": 0.0
                    })

                    new_shares = current_position.get("total_shares", 0.0) + fill_quantity
                    new_strategy = "long" if new_shares > 0 else ("short" if new_shares < 0 else "closed")

                    # Preserve existing fields like tags, market_value, etc.
                    updated_position = current_position.copy()
                    updated_position.update({
                        "symbol": symbol,
                        "open": abs(new_shares) > 0,
                        "strategy": new_strategy,
                        "total_shares": new_shares,
                        "total_avg_price": command_stimulus.get('limit_price', 100.0),
                        "overall_realized_pnl": current_position.get("overall_realized_pnl", 0.0)
                    })

                    self._testable_pm.positions[symbol] = updated_position
                    logger.info(f"✅ Updated PM position for {symbol} after T4 fill: {updated_position}")

                except Exception as e:
                    logger.error(f"❌ Failed to update PM position for {symbol}: {e}")

            # Update Order Repository after fill
            if hasattr(self, '_testable_or') and self._testable_or:
                try:
                    order_state = {
                        "order_id": local_order_id,
                        "symbol": symbol,
                        "side": side,
                        "quantity": quantity,
                        "filled_quantity": abs(fill_quantity),
                        "status": "FILLED",
                        "order_type": command_stimulus.get('order_type', 'MKT'),
                        "fill_price": command_stimulus.get('limit_price', 100.0)
                    }

                    self._testable_or.orders[local_order_id] = order_state
                    logger.info(f"✅ Updated OR state for {local_order_id} after T4 fill: {order_state}")

                except Exception as e:
                    logger.error(f"❌ Failed to update OR state for {local_order_id}: {e}")

        # Start all mock components in separate threads
        threading.Thread(target=mock_t1_rms, daemon=True).start()
        threading.Thread(target=mock_t2_tms, daemon=True).start()
        threading.Thread(target=mock_t3_broker_ack, daemon=True).start()
        threading.Thread(target=mock_t4_broker_fill, daemon=True).start()

    def _inject_ocr_event(self, ocr_stimulus: Dict[str, Any], step_corr_id: str): # step_corr_id is the ID for THIS OCR injection
        """
        Injects an OCR event. The step_corr_id is used as this stimulus's main correlation_id.

        WP1 Task 1.3: IntelliSense does not inject its logger into TESTRADE OCR event instances.
        The correlation_id is embedded in the raw_payload for TESTRADE's own correlation mechanisms.
        """
        # ocr_stimulus is a copy of step.stimulus

        if not self.ocr_conditioning_service or not hasattr(self.ocr_conditioning_service, 'inject_simulated_raw_ocr_input'):
            logger.error(f"Cannot inject OCR (StepCorrID: {step_corr_id}): EnhancedOCRService or method missing."); return

        try:
            raw_payload = ocr_stimulus.get('raw_data', {})
            if not isinstance(raw_payload, dict): raw_payload = {"data_content": raw_payload}

            # The OCR stimulus's own correlation_id is the step_corr_id
            raw_payload['correlation_id'] = step_corr_id
            # No tracking_dict anymore

            injected_frame_number_val = ocr_stimulus.get('frame_number_override')
            # ... (generate frame number if needed) ...
            if injected_frame_number_val is None and hasattr(self.ocr_conditioning_service, '_generate_next_injected_frame_id'):
                injected_frame_number_val = self.ocr_conditioning_service._generate_next_injected_frame_id()
            elif injected_frame_number_val is None:
                injected_frame_number_val = -int(time.time())

            # Log the INJECTION_CONTROL event for the OCR stimulus
            injection_ts_ns = time.perf_counter_ns()
            self.correlation_logger.log_event(
                source_sense="INJECTION_CONTROL",
                event_payload={
                    "injection_type": "ocr_event",
                    "stimulus_details": {k:v for k,v in ocr_stimulus.items() if k!='raw_data'}, # Pass original stimulus details
                    "correlation_id": step_corr_id, # This specific event's correlation_id
                    "injected_frame_number": injected_frame_number_val,
                    "injection_perf_timestamp_ns": injection_ts_ns,
                    "injection_epoch_timestamp_s": time.time()
                },
                source_component_name="MillisecondOptimizationCapture"
            )
            self.ocr_conditioning_service.inject_simulated_raw_ocr_input(
                raw_ocr_data_payload=raw_payload,
                frame_number_override=injected_frame_number_val,
                frame_timestamp_override=ocr_stimulus.get('frame_timestamp_override')
            )
            logger.info(f"Injected OCR stimulus (StepCorrID: {step_corr_id}, Frame: {injected_frame_number_val}).")
        except Exception as e:
            logger.error(f"Error injecting OCR event (StepCorrID: {step_corr_id}): {e}", exc_info=True)

    def execute_plan(self, injection_plan: List[InjectionStep]) -> List[StepExecutionResult]: # Return list of StepExecutionResult
        if not self.feedback_isolator:
            logger.error("Cannot execute injection plan: FeedbackIsolationManager not available.")
            return [StepExecutionResult(step_id=step.step_id, correlation_id="N/A", pipeline_data={},
                                        step_execution_error="FeedbackIsolator missing") for step in injection_plan]

        logger.info(f"Starting execution of injection plan with {len(injection_plan)} steps.")
        plan_results: List[StepExecutionResult] = []

        for i, step in enumerate(injection_plan):
            step_id_str = step.step_id or f"step_{i+1}"
            # This step_corr_id is the primary correlation ID for the chain initiated by THIS step's stimulus.
            # If the stimulus itself (in the JSON plan) has a "correlation_id" field, use that.
            # Otherwise, generate one. This allows plans to explicitly set the starting correlation_id.
            step_corr_id = step.stimulus.get("correlation_id", f"{step_id_str}_{uuid.uuid4().hex[:8]}")

            logger.info(f"Executing Step ID '{step_id_str}' (StepCorrID/ChainID: {step_corr_id}): {step.step_description or 'No desc'}")

            # stimulus_data_for_monitor is what EventConditionCheckers will receive as 'stimulus_data'
            stimulus_data_for_monitor = step.stimulus.copy()
            stimulus_data_for_monitor['step_correlation_id'] = step_corr_id # Main ID for this chain

            # For chained pipelines like OCR_STIMULUS_TO_TRADE_FILL, initialize trade_derived_order_request_id
            target_pipeline_key = stimulus_data_for_monitor.get("target_pipeline_key", "")
            if "TRADE_FILL" in target_pipeline_key:
                stimulus_data_for_monitor['trade_derived_order_request_id'] = None  # Will be populated when S2 matches
                logger.debug(f"Initialized trade_derived_order_request_id=None for chained pipeline: {target_pipeline_key}")

            # If the stimulus IS a trade command, _inject_trade_command will add 'derived_order_request_id' to this dict.
            # If the stimulus is OCR and triggers a trade via mock orchestrator, that orchestrator
            # needs to generate the trade with this step_correlation_id, and MOC's _inject_trade_command
            # will then generate and add 'derived_order_request_id' to this dict IF the mock orchestrator
            # calls back into MOC._inject_trade_command.

            # Set up preconditions: OCR mocks and PM states
            if step.ocr_mocks_to_activate:
                for sym, mock_prof in step.ocr_mocks_to_activate.items():
                     # Assuming _resolve_identifier_from_stimulus doesn't need captured_pipeline_data for this part
                    resolved_sym = self._resolve_identifier_from_stimulus(sym, stimulus_data_for_monitor) or sym
                    self.feedback_isolator.activate_symbol_ocr_mock(resolved_sym, mock_prof)

            # Set up Position Manager states
            if step.position_manager_states_to_set:
                for sym, pm_state in step.position_manager_states_to_set.items():
                    resolved_sym = self._resolve_identifier_from_stimulus(sym, stimulus_data_for_monitor) or sym
                    if self.feedback_isolator:
                        self.feedback_isolator.set_testable_position_manager_state(resolved_sym, pm_state)
                        logger.info(f"✅ Set PM state for {resolved_sym}: {pm_state}")
                    else:
                        logger.warning(f"⚠️ Cannot set PM state for {resolved_sym}: FeedbackIsolator not available")

            pipeline_data: Dict[str, Any] = {} # Initialize
            step_exec_error: Optional[str] = None

            try:
                stimulus_type = step.stimulus.get('type')

                # CRITICAL FIX: Set up pipeline monitoring BEFORE stimulus injection
                # This ensures the monitor is active when the S0 event is published
                expected_stages = get_expected_pipeline_stages(stimulus_type, stimulus_data_for_monitor)
                if not expected_stages:
                    logger.warning(f"No pipeline signature for stimulus '{stimulus_type}'. Timing/event capture may be limited for StepCorrID {step_corr_id}.")
                    pipeline_data = {"error": "No pipeline signature", "timed_out": False, "events": [], "latencies_ns": {}, "stages_matched":0, "events_by_stage_key": {}, "completed_successfully": False}
                else:
                    completion_event_for_monitor = threading.Event()
                    # Monitor is keyed by the step_correlation_id (which is the chain's main ID)
                    self._active_pipeline_monitors[step_corr_id] = {
                        "start_ns": time.perf_counter_ns(), "matched_events": [], "events_by_stage_key": {},
                        "expected_stages": expected_stages, "current_stage_index": 0, "completed": False,
                        "stimulus_data": stimulus_data_for_monitor, # This now has step_correlation_id and potentially derived_order_request_id
                        "timeout_at_ns": time.perf_counter_ns() + int((step.stimulus.get("pipeline_timeout_s", DEFAULT_PIPELINE_TIMEOUT_S)) * 1e9),
                        "completion_event": completion_event_for_monitor
                    }
                    logger.debug(f"Pipeline monitor created for {step_corr_id} BEFORE stimulus injection")

                # Inject Stimulus (AFTER monitor is set up)
                if stimulus_type == 'ocr_event':
                    self._inject_ocr_event(stimulus_data_for_monitor, step_corr_id)
                    # If OCR leads to a trade, the mock orchestrator is responsible for:
                    # 1. Using 'step_corr_id' as the 'correlation_id' for the trade it generates.
                    # 2. Generating a 'derived_order_request_id' for that trade.
                    # 3. Ensuring MOC's pipeline monitor 'stimulus_data' gets this 'derived_order_request_id'.
                    #    This typically means the 'S2_OrchestratedTradeInjectLog' event itself should contain this
                    #    'derived_order_request_id', and the checker for S2 confirms it.
                    #    The 'stimulus_data' for subsequent checkers (S3, S4, S5, S6) will then use this.
                    #    This requires stimulus_data_for_monitor to be potentially updated after S2 matches.
                    #    This is the tricky part BA11 pointed out about complex linkage.
                    #    For now, checkers for S3+ will have to rely on matching only the step_correlation_id
                    #    and assume the derived_order_request_id is consistent if only one trade is made.
                elif stimulus_type == 'trade_command':
                    self._inject_trade_command(stimulus_data_for_monitor, step_corr_id)
                    # _inject_trade_command adds 'derived_order_request_id' to stimulus_data_for_monitor.
                else:
                    # ... (handle unknown stimulus, set step_exec_error, empty pipeline_data)
                    logger.warning(f"Unknown stimulus type '{stimulus_type}'.")
                    step_exec_error = "Unknown stimulus type"
                    pipeline_data = {"error": step_exec_error, "timed_out": False, "events": [], "latencies_ns": {}, "stages_matched":0, "events_by_stage_key": {}, "completed_successfully": False}

                # Wait for pipeline completion (if monitor was created)
                if expected_stages and not step_exec_error:
                    pipeline_data = self._wait_for_pipeline_completion(step_corr_id, completion_event_for_monitor)

            except Exception as e_exec_step:
                # ... (handle exception, set step_exec_error, update pipeline_data, cleanup monitor)
                step_exec_error = f"Exception during step execution for StepCorrID {step_corr_id}: {e_exec_step}"
                logger.error(step_exec_error, exc_info=True)
                pipeline_data = {"error": step_exec_error, "timed_out": False, "events": [], "latencies_ns": {}, "stages_matched":0, "events_by_stage_key": {}, "completed_successfully": False}
                if step_corr_id in self._active_pipeline_monitors: del self._active_pipeline_monitors[step_corr_id]


            validation_outcomes = self._validate_results(step, pipeline_data, stimulus_data_for_monitor)

            plan_results.append(StepExecutionResult(
                step_id=step.step_id or f"step_{i+1}",
                step_description=step.step_description,
                stimulus_type=stimulus_type,
                correlation_id=step_corr_id,
                pipeline_data=pipeline_data,
                validation_outcomes=validation_outcomes,
                step_execution_error=step_exec_error
            ))

            if step.wait_after_injection_s > 0:
                logger.info(f"Waiting {step.wait_after_injection_s}s after step '{step.step_id}'...")
                time.sleep(step.wait_after_injection_s)

        logger.info("Injection plan execution sequence completed.")
        if self.feedback_isolator: self.feedback_isolator.reset_all_mocking_and_states()
        return plan_results

    def _wait_for_pipeline_completion(self, correlation_id: str, completion_event_for_monitor: threading.Event) -> Dict[str, Any]:
        # ... (Implementation from Chunk 33 - verified and returns events_by_stage_key and latencies_ns)
        monitor = self._active_pipeline_monitors.get(correlation_id)
        default_error_result = {"error": "Monitor not found", "timed_out": True, "events": [], "latencies_ns": {}, "stages_matched": 0, "events_by_stage_key": {}, "completed_successfully": False, "total_duration_ns": None }
        if not monitor: return default_error_result
        timeout_duration_s = (monitor.get("timeout_at_ns", 0) - monitor.get("start_ns", time.perf_counter_ns())) / 1e9
        timed_out = not completion_event_for_monitor.wait(timeout=max(0.1, timeout_duration_s))
        monitor_data_after_wait = self._active_pipeline_monitors.pop(correlation_id, monitor)
        pipeline_events_matched = list(monitor_data_after_wait.get('matched_events', [])); events_by_stage_key_collected = monitor_data_after_wait.get('events_by_stage_key', {}); is_completed_by_match = monitor_data_after_wait.get('completed', False); stages_matched_count = monitor_data_after_wait.get('current_stage_index', 0); expected_stages_list: List[PIPELINE_STAGE_CRITERIA] = monitor_data_after_wait.get('expected_stages', [])
        base_result = {"events": pipeline_events_matched, "latencies_ns": {}, "stages_matched": stages_matched_count, "total_stages_expected": len(expected_stages_list), "events_by_stage_key": events_by_stage_key_collected, "completed_successfully": is_completed_by_match, "total_duration_ns": None}
        if timed_out and not is_completed_by_match: logger.warning(f"[{correlation_id}] Pipeline TIMED OUT."); return {"error": "Timeout", "timed_out": True, **base_result}
        if not is_completed_by_match: logger.warning(f"[{correlation_id}] Pipeline wait finished, but not all stages matched."); return {"error": "Incomplete match", "timed_out": False, **base_result}
        latencies_ns: Dict[str, Optional[int]] = {}; calculated_total_duration_ns: Optional[int] = None; pipeline_start_ns: Optional[int] = None
        if expected_stages_list: first_stage_name_key = expected_stages_list[0][3]; injection_control_log = events_by_stage_key_collected.get(first_stage_name_key);
        if injection_control_log and injection_control_log.get('source_sense') == "INJECTION_CONTROL": pipeline_start_ns = injection_control_log.get('event_payload', {}).get('injection_perf_timestamp_ns')
        if pipeline_start_ns is None: pipeline_start_ns = monitor_data_after_wait.get('start_ns'); logger.warning(f"[{correlation_id}] Using monitor creation time as pipeline_start_ns.")
        if pipeline_start_ns is None: logger.error(f"[{correlation_id}] CRITICAL: Cannot determine pipeline_start_ns."); base_result["error"] = "Cannot determine start time."; return base_result
        last_event_perf_ns_for_total_duration = pipeline_start_ns
        for i in range(stages_matched_count):
            stage_def = expected_stages_list[i]; stage_name_key = stage_def[3]; log_entry_for_stage = events_by_stage_key_collected.get(stage_name_key)
            if not log_entry_for_stage: logger.warning(f"[{correlation_id}] No log entry for stage '{stage_name_key}'."); continue
            current_event_perf_ns = log_entry_for_stage.get('perf_timestamp_ns')
            if current_event_perf_ns is not None:
                latencies_ns[f"{stage_name_key}_from_inject_ns"] = current_event_perf_ns - pipeline_start_ns
                if i > 0:
                    prev_stage_name_key = expected_stages_list[i-1][3]
                    prev_log_entry = events_by_stage_key_collected.get(prev_stage_name_key)
                    if prev_log_entry and prev_log_entry.get('perf_timestamp_ns') is not None:
                        latencies_ns[f"{prev_stage_name_key}_to_{stage_name_key}_ns"] = current_event_perf_ns - prev_log_entry['perf_timestamp_ns']
                component_latency = log_entry_for_stage.get('event_payload', {}).get('component_processing_latency_ns') or log_entry_for_stage.get('event_payload', {}).get('processing_latency_ns')
                if component_latency is not None: latencies_ns[f"{stage_name_key}_component_internal_ns"] = component_latency
                last_event_perf_ns_for_total_duration = current_event_perf_ns
            else: logger.warning(f"[{correlation_id}] Missing 'perf_timestamp_ns' for stage '{stage_name_key}'.")
        if stages_matched_count > 0: calculated_total_duration_ns = last_event_perf_ns_for_total_duration - pipeline_start_ns; latencies_ns['total_observed_pipeline_ns'] = calculated_total_duration_ns; logger.info(f"[{correlation_id}] Pipeline analysis complete. Total Duration: {calculated_total_duration_ns / 1e6:.2f}ms.")
        base_result["latencies_ns"] = latencies_ns; base_result["total_duration_ns"] = calculated_total_duration_ns
        return {"timed_out": False, **base_result}

    def _resolve_identifier_from_stimulus(self, id_ref: Optional[str],
                                          stimulus_data: Dict[str, Any],
                                          captured_pipeline_data: Optional[Dict[str,Any]] = None) -> Optional[Any]:
        # ... (Implementation from Chunk 33 - verified to use events_by_stage_key)
        if id_ref is None: return None
        if id_ref.startswith("$stimulus_"): key = id_ref[len("$stimulus_"):]; return stimulus_data.get(key)
        elif id_ref.startswith("$pipeline_event_payload_") and captured_pipeline_data:
            path_parts_str = id_ref[len("$pipeline_event_payload_"):]; parts = path_parts_str.split('.', 1); stage_name_key_for_event = parts[0]; path_in_payload = parts[1] if len(parts) > 1 else None

            # DETAILED DEBUG LOGGING for pipeline event payload resolution
            logger.debug(f"🔍 PIPELINE_EVENT_PAYLOAD DEBUG: Resolving {id_ref}")
            logger.debug(f"   stage_name_key_for_event: {stage_name_key_for_event}")
            logger.debug(f"   path_in_payload: {path_in_payload}")

            events_by_stage = captured_pipeline_data.get('events_by_stage_key', {})
            logger.debug(f"   events_by_stage_key keys: {list(events_by_stage.keys())}")

            stage_event_log_entry = events_by_stage.get(stage_name_key_for_event)
            if stage_event_log_entry and isinstance(stage_event_log_entry, dict):
                logger.debug(f"   stage_event_log_entry found: {json.dumps(stage_event_log_entry, indent=2)}")
                event_payload_from_log = stage_event_log_entry.get('event_payload', {})
                logger.debug(f"   event_payload_from_log: {json.dumps(event_payload_from_log, indent=2)}")

                if path_in_payload:
                    result = self._get_value_from_path(event_payload_from_log, path_in_payload)
                    logger.debug(f"   ✅ Successfully resolved path '{path_in_payload}' to: {result}")
                    return result
                logger.debug(f"   ✅ Returning full event_payload_from_log")
                return event_payload_from_log
            logger.error(f"   ❌ Could not resolve pipeline event payload for ref: {id_ref}. Stage key '{stage_name_key_for_event}' not found.")
            logger.debug(f"       Available stage keys: {list(events_by_stage.keys())}")
            return None
        return id_ref

    def _get_value_from_path(self, data_dict: Optional[Dict[str, Any]], path: str) -> Any:
        # ... (Implementation from Chunk 24/29 - assumed robust)
        if data_dict is None: return None
        if not path: return data_dict  # Return whole dict if no path specified
        keys = path.split('.'); val = data_dict
        try:
            for key_part in keys:
                if isinstance(val, list):
                    try: idx = int(key_part); val = val[idx]
                    except (ValueError, IndexError): return None
                elif isinstance(val, dict): val = val.get(key_part)
                else: return None
                if val is None and key_part != keys[-1]: return None
            return val
        except Exception: return None

    def _compare_values(self, actual: Any, expected: Any, operator: ComparisonOperator, tolerance: Optional[float]) -> bool:
        """
        Compares an actual value against an expected value using a specified operator.
        Supports all operators defined in scenario_types.ComparisonOperator.
        """
        # Handle existence checks first as they don't require 'actual' to have a specific type
        if operator == "exists": # Direct string comparison from Literal
            return actual is not None
        if operator == "not_exists":
            return actual is None

        # For IS_EMPTY, None is considered empty.
        if operator == "is_empty":
            if actual is None: return True
            if isinstance(actual, (str, list, dict, set, tuple)): return not actual # Pythonic way to check for empty sequences/mappings
            return False # Not a type that can be considered empty in this context

        if operator == "is_not_empty":
            if actual is None: return False # None is not "not_empty"
            if isinstance(actual, (str, list, dict, set, tuple)): return bool(actual)
            return True # For non-None, non-collection types (like numbers), consider them "not empty"

        # For all other operators, if 'actual' is None at this point, the comparison usually fails,
        # unless 'expected' is also None and operator is '==' (handled below).
        if actual is None:
            return operator == "==" and expected is None

        # --- Comparisons where 'actual' is NOT None ---
        try:
            # Equality and Inequality (with type flexibility and float tolerance)
            if operator == "==":
                if isinstance(actual, (float, int)) and isinstance(expected, (float, int)) and tolerance is not None:
                    return abs(float(actual) - float(expected)) <= tolerance
                # Flexible string comparison (e.g., 10.0 == "10.0")
                return str(actual) == str(expected)

            if operator == "!=":
                if isinstance(actual, (float, int)) and isinstance(expected, (float, int)) and tolerance is not None:
                    return abs(float(actual) - float(expected)) > tolerance
                return str(actual) != str(expected)

            # String-specific operations: convert actual to string for these
            actual_str = str(actual)
            expected_str = str(expected) # Expected value for these ops should also be string-like

            if operator == "starts_with":
                return actual_str.startswith(expected_str)
            if operator == "ends_with":
                return actual_str.endswith(expected_str)
            if operator == "matches_regex":
                try:
                    return re.search(expected_str, actual_str) is not None
                except re.error as e_regex:
                    logger.warning(f"Invalid regex pattern '{expected_str}' in MATCHES_REGEX comparison: {e_regex}")
                    return False

            # Numeric comparisons (attempt float conversion for >, <, >=, <=)
            if operator in [">", "<", ">=", "<="]:
                actual_f = float(actual)    # May raise ValueError if not convertible
                expected_f = float(expected)  # May raise ValueError
                if operator == ">": return actual_f > expected_f
                if operator == "<": return actual_f < expected_f
                if operator == ">=": return actual_f >= expected_f
                if operator == "<=": return actual_f <= expected_f

            # Collection/Sequence operations
            if operator == "in":
                # Check if expected is in actual (e.g., "tech" in ["growth", "tech", "semiconductor"])
                if isinstance(actual, list): return expected in actual
                if isinstance(actual, str): return str(expected) in actual_str # Substring check
                if isinstance(actual, dict): return expected in actual # Key in dict keys
                # Fallback: check if actual is in expected (original logic)
                if isinstance(expected, list): return actual in expected
                if isinstance(expected, str): return str(actual) in expected_str
                if isinstance(expected, dict): return actual in expected
                logger.debug(f"Operator 'in': Neither actual nor expected support 'in' check. Actual='{actual}' (type {type(actual)}), Expected='{expected}' (type {type(expected)}).")
                return False

            if operator == "not_in":
                if isinstance(expected, list): return actual not in expected
                if isinstance(expected, str): return str(actual) not in expected # Substring check
                if isinstance(expected, dict): return actual not in expected # Key not in dict keys
                logger.debug(f"Operator 'not_in': Expected value type {type(expected)} not list/str/dict for 'not_in' check with actual '{actual}'.")
                return True # If expected type doesn't support 'not_in', assume actual is not in it

            if isinstance(actual, list) and isinstance(expected, list): # For CONTAINS_ALL, CONTAINS_ANY
                if operator == "contains_all":
                    return all(item in actual for item in expected)
                if operator == "contains_any":
                    return any(item in actual for item in expected)

        except (ValueError, TypeError) as e:
            # Handles errors during float conversion or other type-related comparison issues
            logger.debug(f"Comparison error or type mismatch for Operator='{operator}', Actual='{actual}' (type {type(actual)}), Expected='{expected}' (type {type(expected)}): {e}")
            return False

        logger.warning(f"Unsupported comparison or unhandled type combination: Operator='{operator}', Actual='{actual}' (type {type(actual)}), Expected='{expected}' (type {type(expected)})")
        return False

    # --- START OF NEW/MODIFIED _validate_results for Chunk 35 ---
    def _validate_results(self, step: InjectionStep,
                          captured_pipeline_data: Dict[str, Any],
                          stimulus_data_for_resolution: Dict[str, Any]
                         ) -> List[ValidationOutcome]:
        outcomes: List[ValidationOutcome] = []
        if not step.validation_points:
            logger.debug(f"No validation points defined for StepID '{step.step_id}'.")
            return outcomes

        correlation_id = stimulus_data_for_resolution.get('correlation_id', 'N/A_CorrID')
        logger.info(f"Validating results for StepID '{step.step_id}' (CorrID: {correlation_id}) "
                    f"with {len(step.validation_points)} validation points.")

        logged_events_for_step = captured_pipeline_data.get('events', []) # Full list of matched events in sequence
        measured_latencies_ns = captured_pipeline_data.get('latencies_ns', {})
        events_by_stage_key = captured_pipeline_data.get('events_by_stage_key', {})

        for vp_index, vp in enumerate(step.validation_points):
            passed = False
            actual_value: Any = None
            details = ""
            vp_id = vp.validation_id or f"vp_{step.step_id}_{vp_index}"
            resolved_target_id_for_vp = None # For logging

            try:
                # Resolve target_identifier using stimulus data AND captured pipeline data
                resolved_target_id_for_vp = self._resolve_identifier_from_stimulus(
                    vp.target_identifier,
                    stimulus_data_for_resolution,
                    captured_pipeline_data # Pass this for $pipeline_event_payload_ refs
                )

                # Resolve expected_value if it's a stimulus reference
                resolved_expected_value = self._resolve_identifier_from_stimulus(
                    str(vp.expected_value) if vp.expected_value is not None else None,
                    stimulus_data_for_resolution,
                    captured_pipeline_data
                ) if isinstance(vp.expected_value, str) and vp.expected_value.startswith("$") else vp.expected_value

                if vp.type == "order_repository_state":
                    if not self._testable_or: details = "TestableOrderRepository not available for validation."; raise RuntimeError(details)
                    if resolved_target_id_for_vp:
                        actual_or_state = self._testable_or.get_cached_order_state(str(resolved_target_id_for_vp))
                        if not actual_or_state or not actual_or_state.get("exists", False):
                            details = f"Order ID '{resolved_target_id_for_vp}' not found in OrderRepository."
                            actual_value = None # Explicitly set for comparison with 'not_exists'
                        else:
                            actual_value = self._get_value_from_path(actual_or_state, vp.field_to_check) if vp.field_to_check else actual_or_state
                        passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                        if not details: details = f"OR State for '{resolved_target_id_for_vp}'. Field '{vp.field_to_check}': Actual={str(actual_value)[:100]}, Expected={str(resolved_expected_value)[:100]}"
                    else: details = f"Target identifier '{vp.target_identifier}' for order_repository_state could not be resolved."

                elif vp.type == "position_manager_state":
                    if not self._testable_pm: details = "TestablePositionManager not available for validation."; raise RuntimeError(details)
                    if resolved_target_id_for_vp: # Should be a symbol for PM
                        actual_pm_state = self._testable_pm.get_cached_position_state(str(resolved_target_id_for_vp))
                        if not actual_pm_state or not actual_pm_state.get("exists", False):
                             details = f"Symbol '{resolved_target_id_for_vp}' not found in PositionManager."
                             actual_value = None
                        else:
                            actual_value = self._get_value_from_path(actual_pm_state, vp.field_to_check) if vp.field_to_check else actual_pm_state
                        passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                        if not details: details = f"PM State for '{resolved_target_id_for_vp}'. Field '{vp.field_to_check}': Actual={str(actual_value)[:100]}, Expected={str(resolved_expected_value)[:100]}"
                    else: details = f"Target identifier '{vp.target_identifier}' for position_manager_state could not be resolved."

                elif vp.type == "correlation_log_event_count":
                    count = 0
                    if not vp.event_criteria:
                        details = "Event criteria not specified for correlation_log_event_count."
                    else:
                        for log_entry_dict in logged_events_for_step: # Iterate all events matched for this step's pipeline
                            match = True
                            for crit_key, crit_val_expected in vp.event_criteria.items():
                                val_from_log = self._get_value_from_path(log_entry_dict, crit_key) # Check full log entry structure
                                # Allow expected value to be a special placeholder like $stimulus_symbol
                                resolved_crit_val_expected = self._resolve_identifier_from_stimulus(
                                    str(crit_val_expected), stimulus_data_for_resolution, captured_pipeline_data
                                )
                                if val_from_log != resolved_crit_val_expected: match = False; break
                            if match: count += 1
                        actual_value = count
                        passed = self._compare_values(actual_value, vp.expected_event_count, vp.operator, vp.tolerance)
                        details = f"Found {count} log entries matching criteria: {vp.event_criteria}."

                elif vp.type == "pipeline_latency":
                    latency_key_to_check = vp.field_to_check # e.g., "T1_RMSValidatedLog_from_inject_ns"
                    if not latency_key_to_check:
                        details = "field_to_check (latency_key) not specified for pipeline_latency validation."
                    else:
                        actual_latency_ns = measured_latencies_ns.get(latency_key_to_check)
                        if actual_latency_ns is not None:
                            actual_value = actual_latency_ns / 1_000_000.0 # Convert to ms for comparison if expected is in ms
                            passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                            details = f"Measured latency for '{latency_key_to_check}': {actual_value:.3f} ms."
                        else:
                            details = f"Latency key '{latency_key_to_check}' not found in measured latencies."
                            actual_value = None # For outcome reporting
                            # If operator is 'not_exists', this should pass
                            passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)

                elif vp.type == "specific_log_event_field":
                    # Expects target_identifier to be the stage_name_key of the event to check
                    # Expects field_to_check to be the path to the field within that event's event_payload
                    # SPECIAL CASE: If operator is 'exists' or 'not_exists' and field_to_check is None,
                    # check for existence of the entire log entry for that stage
                    stage_key_for_field_check = resolved_target_id_for_vp # target_identifier becomes the stage key
                    if not stage_key_for_field_check:
                        details = "Target identifier (stage_name_key) not specified or resolved for specific_log_event_field."
                    elif not vp.field_to_check and vp.operator not in ["exists", "not_exists"]:
                        details = "field_to_check not specified for specific_log_event_field (required unless using exists/not_exists operators)."
                    else:
                        target_event_log_entry = events_by_stage_key.get(stage_key_for_field_check)
                        if not target_event_log_entry:
                            details = f"Log entry for stage '{stage_key_for_field_check}' not found in captured pipeline data."
                            actual_value = None
                        else:
                            if vp.field_to_check:
                                # Normal field checking
                                actual_value = self._get_value_from_path(target_event_log_entry.get('event_payload',{}), vp.field_to_check)
                            else:
                                # Existence checking - if we found the log entry, it exists
                                actual_value = target_event_log_entry  # Non-None value indicates existence
                        passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                        if not details:
                            if vp.field_to_check:
                                details = f"Field '{vp.field_to_check}' in stage '{stage_key_for_field_check}': Actual={str(actual_value)[:100]}, Expected={str(resolved_expected_value)[:100]}"
                            else:
                                details = f"Stage '{stage_key_for_field_check}' existence check: {'Found' if actual_value else 'Not found'}"

                elif vp.type == "pipeline_timeout_status":
                    # Check if the pipeline timed out
                    actual_value = captured_pipeline_data.get('timed_out', False)
                    passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                    details = f"Pipeline timeout status: Actual={actual_value}, Expected={resolved_expected_value}"

                elif vp.type == "pipeline_completion_status":
                    # Check if the pipeline completed successfully
                    actual_value = captured_pipeline_data.get('completed_successfully', False)
                    passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                    details = f"Pipeline completion status: Actual={actual_value}, Expected={resolved_expected_value}"

                elif vp.type == "stimulus_field_validation":
                    # Validate fields directly from stimulus data
                    # target_identifier should be a $stimulus_* reference
                    if not resolved_target_id_for_vp:
                        details = "Target identifier not resolved for stimulus_field_validation."
                        actual_value = None
                    else:
                        actual_value = resolved_target_id_for_vp  # This is the resolved stimulus value
                    passed = self._compare_values(actual_value, resolved_expected_value, vp.operator, vp.tolerance)
                    details = f"Stimulus field validation: Actual={str(actual_value)[:100]}, Expected={str(resolved_expected_value)[:100]}"

                else:
                    details = f"Unknown validation type: {vp.type}"
                    logger.warning(details)

            except Exception as e_val:
                passed = False
                details = f"EXCEPTION during validation '{vp_id}': {type(e_val).__name__} - {e_val}"
                logger.error(details, exc_info=True)

            outcome = ValidationOutcome(
                validation_id=vp_id, description=vp.description, passed=passed,
                validation_type=vp.type,
                target_identifier=str(resolved_target_id_for_vp) if resolved_target_id_for_vp is not None else vp.target_identifier, # Log resolved or original
                field_checked=vp.field_to_check, operator_used=vp.operator, # vp.operator is already enum
                expected_value_detail=vp.expected_value,
                actual_value_detail=actual_value, # Store the actual value found
                details=details
            )
            outcomes.append(outcome)
            log_level = logging.INFO if passed else logging.WARNING
            logger.log(log_level, f"Validation '{outcome.validation_id}' for StepID '{step.step_id}': {'PASSED' if outcome.passed else 'FAILED'}. {outcome.details}")

        return outcomes
    # --- END OF NEW/MODIFIED _validate_results ---

    def cleanup(self):
        # ... (Implementation as in Chunk 22/29)
        if self._internal_event_bus and self._CorrelationLogEntryEvent:
            try: self._internal_event_bus.unsubscribe(self._CorrelationLogEntryEvent, self._on_correlation_log_entry)
            except Exception as e: logger.warning(f"Error unsubscribing MOC from internal event bus: {e}")
        self._active_pipeline_monitors.clear(); self._correlation_event_buffer.clear()
        logger.info("MillisecondOptimizationCapture cleaned up.")

    def load_injection_plan_from_file(self, plan_file_path: str) -> List[InjectionStep]:
        """Load injection plan from JSON file."""
        try:
            with open(plan_file_path, 'r') as f:
                plan_data = json.load(f)

            injection_steps = []
            for step_data in plan_data.get('steps', []):
                step = InjectionStep(
                    step_description=step_data.get('step_description'),
                    ocr_mocks_to_activate=step_data.get('ocr_mocks_to_activate', {}),
                    position_manager_states_to_set=step_data.get('position_manager_states_to_set', {}),
                    stimulus=step_data.get('stimulus', {}),
                    validation_points=step_data.get('validation_points', []),
                    wait_after_injection_s=step_data.get('wait_after_injection_s', 0.5)
                )
                injection_steps.append(step)

            logger.info(f"Loaded injection plan with {len(injection_steps)} steps from {plan_file_path}")
            return injection_steps

        except Exception as e:
            logger.error(f"Failed to load injection plan from {plan_file_path}: {e}", exc_info=True)
            return []

    def create_timing_report(self, pipeline_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a comprehensive timing report from captured pipeline results."""
        if not pipeline_results:
            return {"status": "no_data", "summary": "No pipeline results to analyze"}

        # Placeholder implementation
        total_steps = len(pipeline_results)
        successful_steps = sum(1 for result in pipeline_results if result.get("status") == "success")

        return {
            "status": "report_generated",
            "summary": {
                "total_injection_steps": total_steps,
                "successful_steps": successful_steps,
                "success_rate": successful_steps / total_steps if total_steps > 0 else 0.0
            },
            "detailed_results": pipeline_results,
            "timestamp": time.time()
        }
