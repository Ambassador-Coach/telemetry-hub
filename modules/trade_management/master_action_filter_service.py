"""
MasterActionFilterService module.

This module provides the MasterActionFilterService class that implements IMasterActionFilter.
It filters trade signals based on settling periods and override conditions to prevent
excessive trading during volatile periods. Ported from legacy MasterActionFilter.
"""

import time
import logging
import threading
import uuid
from typing import Dict, Any, Optional

from interfaces.trading.services import IMasterActionFilter, TradeSignal, TradeActionType
from interfaces.logging.services import ICorrelationLogger
from utils.global_config import GlobalConfig as IConfigService # This is how it's currently typed
from interfaces.data.services import IPriceProvider
from core.events import MAFDutyCycleEvent, MAFDutyCycleEventData
from utils.thread_safe_uuid import get_thread_safe_uuid

# ADDED: Import IPC client interface
try:
    from core.ipc_interfaces import IBulletproofBabysitterIPCClient
except ImportError:
    IBulletproofBabysitterIPCClient = type("IBulletproofBabysitterIPCClient", (), {}) # Placeholder

logger = logging.getLogger(__name__)


class MasterActionFilterService(IMasterActionFilter):
    """
    Service that filters trade signals based on settling periods and override conditions.
    
    Manages settling periods after ADD and REDUCE actions to prevent excessive trading.
    Provides override mechanisms for significant market moves or value changes.
    """

    def __init__(self,
                 config_service: IConfigService,
                 price_provider: IPriceProvider,
                 ipc_client: Optional[IBulletproofBabysitterIPCClient] = None,
                 correlation_logger: ICorrelationLogger = None
                ):
        """
        Initialize the MasterActionFilterService.

        Args:
            config_service: Global configuration service
            price_provider: Price provider service for market data
            ipc_client: IPC client for publishing MAF decisions
        """
        self.logger = logging.getLogger(__name__)
        self._config = config_service
        self._price_provider = price_provider
        self._correlation_logger = correlation_logger
        self._symbol_states: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.RLock()

        # Store ipc_client for TANK mode support
        self.ipc_client = ipc_client
        self.config_stream_name_maf_decisions = getattr(config_service, 'redis_stream_maf_decisions', "testrade:maf-decisions")

        # Tank Sealing: Use correlation logger for MAF decisions instead of IPC client
        if self._correlation_logger:
            self.logger.info("MasterActionFilterService initialized with correlation logger for publishing MAF decisions.")
        else:
            self.logger.warning("MasterActionFilterService: Correlation logger not provided. MAF decisions will not be published.")

        self.logger.info("MasterActionFilterService initialized.")

    def record_system_add_action(self, symbol: str, cost_basis_at_action: float, market_price_at_action: Optional[float] = None) -> None:
        """
        Record that a system ADD action was taken.
        
        Args:
            symbol: Trading symbol
            cost_basis_at_action: Cost basis at the time of action
            market_price_at_action: Optional market price at time of action
        """
        symbol_upper = symbol.upper()
        current_time = time.time()
        settling_duration = float(self._config.cost_basis_settling_duration_seconds)

        with self._state_lock:
            state = self._symbol_states.setdefault(symbol_upper, {})
            state["is_cost_add_settling"] = True
            state["cost_add_settling_end_ts"] = current_time + settling_duration
            state["cost_at_last_system_add"] = cost_basis_at_action
            
            # Clear any rPnL settling state when ADD occurs
            state.pop("is_rPnL_reduce_settling", None)
            state.pop("rPnL_reduce_settling_end_ts", None)
            state.pop("rPnL_at_last_system_reduce", None)
            state.pop("price_at_last_system_reduce", None)

            self.logger.info(
                f"[{symbol_upper}] MAFService: Add action recorded. Cost settling for {settling_duration:.1f}s. "
                f"Baseline Cost={cost_basis_at_action:.4f}."
            )
            
            # Publish action recording event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="MasterActionFilterService",
                    event_name="MAFDecision",
                    payload={
                        "event_type": "ACTION_RECORDED",
                        "symbol": symbol_upper,
                        "action": "ADD",
                        "decision": "SETTLING_PERIOD_STARTED",
                        "details": {
                            "reason": "System ADD action recorded",
                            "cost_basis_at_action": cost_basis_at_action,
                            "market_price_at_action": market_price_at_action,
                            "settling_duration_seconds": settling_duration,
                            "settling_end_timestamp": current_time + settling_duration,
                            "cleared_rpnl_settling": True,
                            "action_timestamp": current_time
                        },
                        "timestamp": current_time
                    }
                )

    def record_system_reduce_action(self, symbol: str, rPnL_at_action: float, market_price_at_action: float) -> None:
        """
        Record that a system REDUCE action was taken.
        
        Args:
            symbol: Trading symbol
            rPnL_at_action: Realized P&L at the time of action
            market_price_at_action: Market price at time of action
        """
        symbol_upper = symbol.upper()
        current_time = time.time()
        settling_duration = float(self._config.rPnL_settling_duration_seconds)

        with self._state_lock:
            state = self._symbol_states.setdefault(symbol_upper, {})
            state["is_rPnL_reduce_settling"] = True
            state["rPnL_reduce_settling_end_ts"] = current_time + settling_duration
            state["rPnL_at_last_system_reduce"] = rPnL_at_action
            state["price_at_last_system_reduce"] = market_price_at_action
            
            # Clear any cost settling state when REDUCE occurs
            state.pop("is_cost_add_settling", None)
            state.pop("cost_add_settling_end_ts", None)
            state.pop("cost_at_last_system_add", None)

            self.logger.info(
                f"[{symbol_upper}] MAFService: Reduce action recorded. rPnL settling for {settling_duration:.1f}s. "
                f"Baselines: rPnL={rPnL_at_action:.2f}, MktPrice={market_price_at_action:.4f}."
            )
            
            # Publish action recording event
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="MasterActionFilterService",
                    event_name="MAFDecision",
                    payload={
                        "event_type": "ACTION_RECORDED",
                        "symbol": symbol_upper,
                        "action": "REDUCE",
                        "decision": "SETTLING_PERIOD_STARTED",
                        "details": {
                            "reason": "System REDUCE action recorded",
                            "rpnl_at_action": rPnL_at_action,
                            "market_price_at_action": market_price_at_action,
                            "settling_duration_seconds": settling_duration,
                            "settling_end_timestamp": current_time + settling_duration,
                            "cleared_cost_settling": True,
                            "action_timestamp": current_time
                        },
                        "timestamp": current_time
                    }
                )

    def filter_signal(self, signal: Optional[TradeSignal], current_ocr_cost: float, current_ocr_rPnL: float, 
                      correlation_id: Optional[str] = None, causation_id: Optional[str] = None) -> Optional[TradeSignal]:
        """
        Filter a trade signal based on settling periods and override conditions.
        
        Args:
            signal: The trade signal to filter
            current_ocr_cost: Current cost basis from OCR
            current_ocr_rPnL: Current realized P&L from OCR
            correlation_id: Optional correlation ID for tracing
            causation_id: Optional causation ID for tracing
            
        Returns:
            Filtered signal (may be None if suppressed)
        """
        # T9 Milestone: MAF Filtering Pipeline Start
        t9_filtering_start_ns = time.perf_counter_ns()
        
        if not signal:
            return None

        # Extract metadata from incoming signal for instrumentation
        signal_metadata = {
            "signal_id": getattr(signal, 'signal_id', None) or get_thread_safe_uuid(),
            "symbol": signal.symbol,
            "action": signal.action.value,
            "quantity": getattr(signal, 'quantity', None),
            "triggering_snapshot": signal.triggering_snapshot,
            "original_correlation_id": correlation_id,
            "original_causation_id": causation_id
        }

        # Extract master correlation ID from signal metadata if available
        master_correlation_id = correlation_id
        if hasattr(signal, 'metadata') and signal.metadata:
            master_correlation_id = signal.metadata.get('master_correlation_id', correlation_id)

        try:
            # Validate input parameters
            if current_ocr_cost < 0:
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="MasterActionFilterService",
                        event_name="MAFError",
                        payload={
                            "error_type": "INVALID_INPUT_DATA",
                            "symbol": signal.symbol,
                            "error_message": f"Negative cost basis received: {current_ocr_cost}",
                            "context": {"current_ocr_cost": current_ocr_cost, "signal_action": signal.action.value},
                            "error_timestamp": time.time(),
                            "severity": "WARNING"
                        }
                    )
                self.logger.warning(f"[{signal.symbol}] Invalid negative cost basis: {current_ocr_cost}")

            # Process signal through appropriate filtering logic
            filtered_signal = None
            decision_details = {}
            filtering_reason = "Unknown"
            decision_type = "ERROR"

            if signal.action == TradeActionType.ADD_LONG and \
               signal.triggering_snapshot and "cost_change" in signal.triggering_snapshot:
                filtered_signal, decision_details = self._filter_cost_add_signal_internal(signal, current_ocr_cost, correlation_id, causation_id)
                filtering_reason = decision_details.get("reason", "Cost ADD filtering applied")
                decision_type = "SIGNAL_PASSED" if filtered_signal else "SIGNAL_SUPPRESSED"
                if decision_details.get("override_applied", False):
                    decision_type = "SETTLING_OVERRIDE"
                    
            elif signal.action == TradeActionType.REDUCE_PARTIAL and \
                 signal.triggering_snapshot and "pnl_change" in signal.triggering_snapshot:
                filtered_signal, decision_details = self._filter_rPnL_reduce_signal_internal(signal, current_ocr_rPnL, correlation_id, causation_id)
                filtering_reason = decision_details.get("reason", "rPnL REDUCE filtering applied")
                decision_type = "SIGNAL_PASSED" if filtered_signal else "SIGNAL_SUPPRESSED"
                if decision_details.get("override_applied", False):
                    decision_type = "SETTLING_OVERRIDE"
                    
            elif signal.action in [TradeActionType.OPEN_LONG, TradeActionType.CLOSE_POSITION]:
                # For OPEN/CLOSE signals, reset state and pass through
                self.reset_symbol_state(signal.symbol)
                filtered_signal = signal
                decision_details = {"reason": "OPEN/CLOSE signal - state reset and passed through"}
                filtering_reason = "OPEN/CLOSE signal passthrough"
                decision_type = "PASSTHROUGH"
                
            else:
                # Pass through other signal types
                filtered_signal = signal
                decision_details = {"reason": "Signal type not subject to MAF filtering"}
                filtering_reason = "Signal type passthrough"
                decision_type = "PASSTHROUGH"

            # T10 Milestone: MAF Filtering Pipeline End
            t10_filtering_end_ns = time.perf_counter_ns()

            # Attach MAF metadata to passed-through signals
            if filtered_signal:
                maf_metadata = {
                    "maf_decision_id": get_thread_safe_uuid(),
                    "maf_decision_type": decision_type,
                    "maf_filtering_reason": filtering_reason,
                    "maf_processing_time_ns": t10_filtering_end_ns - t9_filtering_start_ns,
                    "maf_timestamp": time.time(),
                    "master_correlation_id": master_correlation_id,
                    "causation_id": causation_id
                }
                
                # Attach metadata to signal if it has a metadata field
                if hasattr(filtered_signal, 'metadata'):
                    if filtered_signal.metadata is None:
                        filtered_signal.metadata = {}
                    filtered_signal.metadata.update(maf_metadata)

            # Create and publish MAFDutyCycleEvent
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="MasterActionFilterService",
                    event_name="MAFDutyCycle",
                    payload={
                        "decision_type": decision_type,
                        "symbol": signal.symbol if signal else "UNKNOWN",
                        "action": signal.action.value if signal else "UNKNOWN",
                        "timestamp": time.time(),
                        "master_correlation_id": master_correlation_id,
                        "causation_id": causation_id,
                        "origin_signal_event_id": signal_metadata.get("signal_id"),
                        "filtering_reason": filtering_reason,
                        "decision_details": decision_details,
                        "t9_filtering_start_ns": t9_filtering_start_ns,
                        "t10_filtering_end_ns": t10_filtering_end_ns,
                        "signal_action": signal.action.value if signal else None,
                        "signal_quantity": signal_metadata.get("quantity"),
                        "signal_triggering_snapshot": signal_metadata.get("triggering_snapshot"),
                        "current_ocr_cost": current_ocr_cost,
                        "current_ocr_rpnl": current_ocr_rPnL,
                        "current_market_price": decision_details.get("current_market_price"),
                        "is_cost_settling": decision_details.get("settling_active") and decision_details.get("action") == "ADD",
                        "is_rpnl_settling": decision_details.get("settling_active") and decision_details.get("action") == "REDUCE",
                        "settling_end_timestamp": decision_details.get("settling_end_ts"),
                        "time_remaining_in_settling": decision_details.get("time_remaining"),
                        "baseline_cost": decision_details.get("baseline_cost"),
                        "baseline_rpnl": decision_details.get("baseline_rpnl"),
                        "baseline_price": decision_details.get("baseline_price"),
                        "abs_delta_cost": decision_details.get("abs_delta"),
                        "delta_rpnl": decision_details.get("delta_rpnl"),
                        "price_delta_abs": decision_details.get("price_delta_abs"),
                        "price_delta_pct": decision_details.get("price_delta_pct"),
                        "override_thresholds": decision_details.get("override_thresholds"),
                        "settling_durations": {
                            "cost_settling_duration": self._config.cost_basis_settling_duration_seconds,
                            "rpnl_settling_duration": self._config.rPnL_settling_duration_seconds
                        },
                        "output_signal_id": getattr(filtered_signal, 'signal_id', None) if filtered_signal else None,
                        "signal_modified": filtered_signal is not signal if filtered_signal else False,
                        "attached_metadata": getattr(filtered_signal, 'metadata', None) if filtered_signal else None
                    },
                    stream_override=getattr(self._config, 'redis_stream_maf_duty_cycle', 'testrade:maf-duty-cycle')
                )

            return filtered_signal
            
        except Exception as e:
            # T10 Milestone for error case
            t10_filtering_end_ns = time.perf_counter_ns()
            
            # Publish error event for any unexpected failures
            error_details = {
                "signal_action": signal.action.value if signal else None,
                "current_ocr_cost": current_ocr_cost,
                "current_ocr_rPnL": current_ocr_rPnL,
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "exception_type": type(e).__name__,
                "processing_time_ns": t10_filtering_end_ns - t9_filtering_start_ns
            }
            
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="MasterActionFilterService",
                    event_name="MAFError",
                    payload={
                        "error_type": "FILTER_PROCESSING_ERROR",
                        "symbol": signal.symbol if signal else "UNKNOWN",
                        "error_message": f"Unexpected error in signal filtering: {str(e)}",
                        "context": error_details,
                        "error_timestamp": time.time(),
                        "severity": "CRITICAL"
                    }
                )
                
                # Publish MAFDutyCycleEvent for error case
                self._correlation_logger.log_event(
                    source_component="MasterActionFilterService",
                    event_name="MAFDutyCycle",
                    payload={
                        "decision_type": "ERROR",
                        "symbol": signal.symbol if signal else "UNKNOWN",
                        "action": signal.action.value if signal else "UNKNOWN",
                        "timestamp": time.time(),
                        "master_correlation_id": master_correlation_id,
                        "causation_id": causation_id,
                        "origin_signal_event_id": signal_metadata.get("signal_id"),
                        "filtering_reason": f"Processing error: {str(e)}",
                        "decision_details": error_details,
                        "t9_filtering_start_ns": t9_filtering_start_ns,
                        "t10_filtering_end_ns": t10_filtering_end_ns,
                        "current_ocr_cost": current_ocr_cost,
                        "current_ocr_rpnl": current_ocr_rPnL
                    },
                    stream_override=getattr(self._config, 'redis_stream_maf_duty_cycle', 'testrade:maf-duty-cycle')
                )
            
            self.logger.error(f"Error in MAF signal filtering: {e}", exc_info=True)
            
            # Return None to suppress the signal for safety
            return None

    def reset_symbol_state(self, symbol: str) -> None:
        """
        Reset the action filter state for a specific symbol.
        
        Args:
            symbol: Trading symbol to reset
        """
        symbol_upper = symbol.upper()
        # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
        publish_data = None
        with self._state_lock:
            if symbol_upper in self._symbol_states:
                old_state = self._symbol_states[symbol_upper].copy()
                del self._symbol_states[symbol_upper]
                self.logger.info(f"[{symbol_upper}] MAFService: State reset.")
                
                # Prepare publish data while holding lock
                publish_data = {
                    "event_type": "STATE_RESET",
                    "symbol": symbol_upper,
                    "action": "RESET",
                    "decision": "SYMBOL_STATE_CLEARED",
                    "details": {
                        "reason": "Symbol state reset (likely due to OPEN/CLOSE signal)",
                        "previous_state": old_state,
                        "reset_timestamp": time.time()
                    }
                }
        
        # LOCK SAFETY FIX: Publish AFTER releasing the lock
        if publish_data and self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="MasterActionFilterService",
                event_name="MAFDecision",
                payload={
                    "event_type": publish_data["event_type"],
                    "symbol": publish_data["symbol"],
                    "action": publish_data["action"],
                    "decision": publish_data["decision"],
                    "details": publish_data["details"],
                    "timestamp": publish_data["details"]["reset_timestamp"]
                }
            )

    def reset_all_states(self) -> None:
        """Reset all action filter states for all symbols."""
        # LOCK SAFETY FIX: Create snapshot while holding lock, then publish outside lock
        publish_data = None
        with self._state_lock:
            symbols_count = len(self._symbol_states)
            symbols_list = list(self._symbol_states.keys())
            self._symbol_states.clear()
            self.logger.info("MAFService: All symbol states reset.")
            
            # Prepare publish data while holding lock
            publish_data = {
                "event_type": "STATE_RESET",
                "symbol": "ALL",
                "action": "RESET",
                "decision": "ALL_STATES_CLEARED",
                "details": {
                    "reason": "Global MAF state reset",
                    "symbols_cleared_count": symbols_count,
                    "symbols_cleared": symbols_list,
                    "reset_timestamp": time.time()
                }
            }
        
        # LOCK SAFETY FIX: Publish AFTER releasing the lock
        if publish_data and self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="MasterActionFilterService",
                event_name="MAFDecision",
                payload={
                    "event_type": publish_data["event_type"],
                    "symbol": publish_data["symbol"],
                    "action": publish_data["action"],
                    "decision": publish_data["decision"],
                    "details": publish_data["details"],
                    "timestamp": publish_data["details"]["reset_timestamp"]
                }
            )

    def _filter_cost_add_signal_internal(self, signal: TradeSignal, current_ocr_cost: float, 
                                         correlation_id: Optional[str] = None, causation_id: Optional[str] = None) -> tuple[Optional[TradeSignal], Dict[str, Any]]:
        """
        Internal method to filter ADD signals based on cost settling periods.

        Args:
            signal: Original ADD signal
            current_ocr_cost: Current cost basis from OCR
            correlation_id: Optional correlation ID for tracing
            causation_id: Optional causation ID for tracing

        Returns:
            Tuple of (filtered_signal, decision_details)
            - filtered_signal: Signal if allowed, None if suppressed
            - decision_details: Dictionary with detailed decision context
        """
        symbol_upper = signal.symbol.upper()
        current_time = time.time()
        
        # Decision details to return to caller
        decision_details = {
            "symbol": symbol_upper,
            "action": "ADD",
            "current_time": current_time,
            "current_cost": current_ocr_cost,
            "correlation_id": correlation_id,
            "causation_id": causation_id
        }
        
        result_signal = None
        
        with self._state_lock:
            state = self._symbol_states.get(symbol_upper)
            if not state or not state.get("is_cost_add_settling", False):
                # Not in cost_add settling, allow signal
                decision_details.update({
                    "reason": "Not in cost ADD settling period",
                    "decision": "ALLOWED",
                    "has_state": state is not None,
                    "is_settling": state.get("is_cost_add_settling", False) if state else False,
                    "settling_active": False
                })
                result_signal = signal
            else:
                settling_end_ts = float(state.get("cost_add_settling_end_ts", 0))
                decision_details.update({
                    "settling_active": True,
                    "settling_end_ts": settling_end_ts,
                    "time_remaining": settling_end_ts - current_time
                })
                
                if current_time < settling_end_ts:
                    cost_baseline = state.get("cost_at_last_system_add", current_ocr_cost)
                    abs_delta_cost_since_last_action = abs(current_ocr_cost - cost_baseline)

                    override_thresh_key = 'cost_basis_settling_override_abs_delta_threshold'
                    override_thresh = float(self._config.cost_basis_settling_override_abs_delta_threshold)

                    # Add detailed context to decision_details
                    decision_details.update({
                        "baseline_cost": cost_baseline,
                        "abs_delta": abs_delta_cost_since_last_action,
                        "override_threshold": override_thresh,
                        "override_thresh_key": override_thresh_key
                    })

                    self.logger.info(f"[{symbol_upper}] MAFService Cost ADD Filter: CurrentCost={current_ocr_cost:.4f}, "
                                   f"BaselineCost={cost_baseline:.4f}, AbsDelta={abs_delta_cost_since_last_action:.4f}, "
                                   f"OverrideThresh={override_thresh:.4f} (from '{override_thresh_key}')")

                    if abs_delta_cost_since_last_action >= override_thresh:
                        self.logger.warning(
                            f"[{symbol_upper}] MAFService: OVERRIDING Cost ADD settling. "
                            f"Large abs_delta_cost={abs_delta_cost_since_last_action:.4f} >= {override_thresh:.4f}."
                        )
                        state["is_cost_add_settling"] = False  # Clear settling state
                        
                        decision_details.update({
                            "reason": "Large cost basis change",
                            "decision": "OVERRIDE",
                            "override_applied": True
                        })
                        
                        result_signal = signal  # Allow action
                    else:
                        self.logger.info(
                            f"[{symbol_upper}] MAFService: Suppressing ADD. In Cost ADD settling (ends {settling_end_ts:.1f}). "
                            f"Abs delta since last action: {abs_delta_cost_since_last_action:.4f} < {override_thresh:.4f}."
                        )
                        
                        decision_details.update({
                            "reason": "In settling period",
                            "decision": "SUPPRESSED",
                            "override_applied": False
                        })
                        
                        result_signal = None  # Suppress signal
                else:
                    # Settling period just ended naturally
                    self.logger.info(f"[{symbol_upper}] MAFService: Cost ADD settling period ended naturally.")
                    state["is_cost_add_settling"] = False
                    
                    decision_details.update({
                        "reason": "Cost ADD settling period ended naturally",
                        "decision": "NATURAL_EXPIRATION",
                        "override_applied": False,
                        "natural_expiration": True
                    })
                    
                    result_signal = signal  # Allow signal

        return result_signal, decision_details

    def _filter_rPnL_reduce_signal_internal(self, signal: TradeSignal, current_ocr_rPnL: float,
                                            correlation_id: Optional[str] = None, causation_id: Optional[str] = None) -> tuple[Optional[TradeSignal], Dict[str, Any]]:
        """
        Internal method to filter REDUCE signals based on rPnL settling periods.

        Args:
            signal: Original REDUCE signal
            current_ocr_rPnL: Current realized P&L from OCR
            correlation_id: Optional correlation ID for tracing
            causation_id: Optional causation ID for tracing

        Returns:
            Tuple of (filtered_signal, decision_details)
            - filtered_signal: Signal if allowed, None if suppressed
            - decision_details: Dictionary with detailed decision context
        """
        symbol_upper = signal.symbol.upper()
        current_time = time.time()
        
        # Decision details to return to caller
        decision_details = {
            "symbol": symbol_upper,
            "action": "REDUCE",
            "current_time": current_time,
            "current_rpnl": current_ocr_rPnL,
            "correlation_id": correlation_id,
            "causation_id": causation_id
        }
        
        result_signal = None

        with self._state_lock:
            state = self._symbol_states.get(symbol_upper)
            if not state or not state.get("is_rPnL_reduce_settling", False):
                # Not in rPnL_reduce settling, allow signal
                decision_details.update({
                    "reason": "Not in rPnL REDUCE settling period",
                    "decision": "ALLOWED",
                    "has_state": state is not None,
                    "is_settling": state.get("is_rPnL_reduce_settling", False) if state else False,
                    "settling_active": False
                })
                result_signal = signal
            else:
                settling_end_ts = float(state.get("rPnL_reduce_settling_end_ts", 0))
                decision_details.update({
                    "settling_active": True,
                    "settling_end_ts": settling_end_ts,
                    "time_remaining": settling_end_ts - current_time
                })
                
                if current_time < settling_end_ts:
                    rPnL_baseline = state.get("rPnL_at_last_system_reduce", current_ocr_rPnL)
                    price_baseline = state.get("price_at_last_system_reduce", 0.0)
                    delta_rPnL_since_last_system_action = current_ocr_rPnL - rPnL_baseline

                    # Get configuration thresholds with fallbacks
                    cfg_override_rPnL_delta_key = 'rPnL_settling_override_increase_threshold'
                    cfg_override_rPnL_delta_fallback_key = 'rPnL_settling_override_delta_threshold'
                    cfg_override_rPnL_delta = float(self._config.rPnL_settling_override_increase_threshold)

                    cfg_moderate_rPnL_delta_key = 'rPnL_initial_reduce_increase_threshold'
                    cfg_moderate_rPnL_delta_fallback_key = 'rPnL_initial_reduce_delta_threshold'
                    cfg_moderate_rPnL_delta = float(self._config.rPnL_initial_reduce_increase_threshold)

                    cfg_price_override_pct_key = 'rPnL_settling_price_override_threshold_percent'
                    cfg_price_override_pct = float(self._config.rPnL_settling_price_override_threshold_percent) / 100.0

                    cfg_price_override_abs_key = 'rPnL_settling_price_override_threshold_abs'
                    cfg_price_override_abs = float(self._config.rPnL_settling_price_override_threshold_abs)

                    # Add detailed context to decision_details
                    decision_details.update({
                        "baseline_rpnl": rPnL_baseline,
                        "baseline_price": price_baseline,
                        "delta_rpnl": delta_rPnL_since_last_system_action,
                        "override_thresholds": {
                            "override_rpnl_delta": cfg_override_rPnL_delta,
                            "moderate_rpnl_delta": cfg_moderate_rPnL_delta,
                            "price_override_pct": cfg_price_override_pct,
                            "price_override_abs": cfg_price_override_abs
                        },
                        "config_keys": {
                            "override_rpnl_delta_key": cfg_override_rPnL_delta_key,
                            "moderate_rpnl_delta_key": cfg_moderate_rPnL_delta_key,
                            "price_override_pct_key": cfg_price_override_pct_key,
                            "price_override_abs_key": cfg_price_override_abs_key
                        }
                    })

                    self.logger.info(f"[{symbol_upper}] MAFService rPnL REDUCE Filter: CurrentRPNL={current_ocr_rPnL:.2f}, "
                                   f"BaselineRPNL={rPnL_baseline:.2f}, BaselinePrice={price_baseline:.4f}, "
                                   f"DeltaRPNL={delta_rPnL_since_last_system_action:.2f}")

                    self.logger.info(f"[{symbol_upper}] MAFService Configs: OverrideRPNLDeltaKey='{cfg_override_rPnL_delta_key}' "
                                   f"Val={cfg_override_rPnL_delta}, ModRPNLDeltaKey='{cfg_moderate_rPnL_delta_key}' "
                                   f"Val={cfg_moderate_rPnL_delta}, PricePctKey='{cfg_price_override_pct_key}' "
                                   f"Val={cfg_price_override_pct}, PriceAbsKey='{cfg_price_override_abs_key}' Val={cfg_price_override_abs}")

                    # Get current market price for unfavorable price movement check
                    current_market_price = self._price_provider.get_bid_price(symbol_upper, allow_api_fallback=True) or \
                                           self._price_provider.get_latest_price(symbol_upper, allow_api_fallback=True) or 0.0

                    price_moved_unfavorably = False
                    price_delta_val = 0.0
                    price_delta_pct_val = 0.0
                    
                    if current_market_price > 0.001 and price_baseline > 0.001:
                        price_delta_val = price_baseline - current_market_price  # Positive if current price is lower (unfavorable for long)
                        price_delta_pct_val = price_delta_val / price_baseline
                        if price_delta_val >= cfg_price_override_abs or price_delta_pct_val >= cfg_price_override_pct:
                            price_moved_unfavorably = True
                            self.logger.info(f"[{symbol_upper}] MAFService: Price moved unfavorably by {price_delta_val:.4f} ({price_delta_pct_val:.2%})")

                    # Add price analysis to decision_details
                    decision_details.update({
                        "current_market_price": current_market_price,
                        "price_delta_abs": price_delta_val,
                        "price_delta_pct": price_delta_pct_val,
                        "price_moved_unfavorably": price_moved_unfavorably
                    })

                    # Check override conditions
                    if delta_rPnL_since_last_system_action >= cfg_override_rPnL_delta:
                        self.logger.warning(f"[{symbol_upper}] MAFService: OVERRIDING rPnL REDUCE settling. "
                                          f"Large rPnL increase={delta_rPnL_since_last_system_action:.2f}.")
                        state["is_rPnL_reduce_settling"] = False
                        
                        decision_details.update({
                            "reason": "Large rPnL increase",
                            "decision": "OVERRIDE",
                            "override_applied": True,
                            "override_type": "large_rpnl_increase"
                        })
                        
                        result_signal = signal
                    elif price_moved_unfavorably and delta_rPnL_since_last_system_action >= cfg_moderate_rPnL_delta:
                        self.logger.warning(f"[{symbol_upper}] MAFService: OVERRIDING rPnL REDUCE settling. "
                                          f"Moderate rPnL increase={delta_rPnL_since_last_system_action:.2f} AND unfavorable price move.")
                        state["is_rPnL_reduce_settling"] = False
                        
                        decision_details.update({
                            "reason": "Moderate rPnL increase with unfavorable price movement",
                            "decision": "OVERRIDE",
                            "override_applied": True,
                            "override_type": "moderate_rpnl_with_price_movement"
                        })
                        
                        result_signal = signal
                    else:
                        self.logger.info(f"[{symbol_upper}] MAFService: Suppressing REDUCE. In rPnL settling (ends {settling_end_ts:.1f}). "
                                       f"rPnL increase since last action: {delta_rPnL_since_last_system_action:.2f}.")
                        
                        decision_details.update({
                            "reason": "In settling period",
                            "decision": "SUPPRESSED",
                            "override_applied": False
                        })
                        
                        result_signal = None
                else:
                    self.logger.info(f"[{symbol_upper}] MAFService: rPnL REDUCE settling period ended naturally.")
                    state["is_rPnL_reduce_settling"] = False
                    
                    decision_details.update({
                        "reason": "rPnL REDUCE settling period ended naturally",
                        "decision": "NATURAL_EXPIRATION",
                        "override_applied": False,
                        "natural_expiration": True
                    })
                    
                    result_signal = signal

        return result_signal, decision_details
    



    def handle_configuration_change(self, config_key: str, old_value: Any, new_value: Any) -> None:
        """
        Handle dynamic configuration changes in the MAF service.
        
        Args:
            config_key: Configuration parameter that changed
            old_value: Previous value
            new_value: New value
        """
        self.logger.info(f"MAF configuration change: {config_key} = {old_value} -> {new_value}")
        
        # Publish configuration change event
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="MasterActionFilterService",
                event_name="MAFConfiguration",
                payload={
                    "config_key": config_key,
                    "old_value": old_value,
                    "new_value": new_value,
                    "reason": "Dynamic configuration update",
                    "change_timestamp": time.time(),
                    "service": "MasterActionFilter",
                    "event_type": "CONFIGURATION_CHANGE"
                }
            )
        
        # Handle specific configuration changes
        if config_key == "cost_basis_settling_duration_seconds":
            # Validate the new settling duration
            if new_value <= 0:
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="MasterActionFilterService",
                        event_name="MAFError",
                        payload={
                            "error_type": "CONFIG_INVALID",
                            "symbol": "GLOBAL",
                            "error_message": f"Invalid settling duration: {new_value}",
                            "context": {"config_key": config_key, "invalid_value": new_value},
                            "error_timestamp": time.time(),
                            "severity": "CRITICAL"
                        }
                    )
                self.logger.error(f"Invalid cost basis settling duration: {new_value}")
                return
                
        elif config_key == "rPnL_settling_duration_seconds":
            # Validate the new rPnL settling duration
            if new_value <= 0:
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="MasterActionFilterService",
                        event_name="MAFError",
                        payload={
                            "error_type": "CONFIG_INVALID",
                            "symbol": "GLOBAL",
                            "error_message": f"Invalid rPnL settling duration: {new_value}",
                            "context": {"config_key": config_key, "invalid_value": new_value},
                            "error_timestamp": time.time(),
                            "severity": "CRITICAL"
                        }
                    )
                self.logger.error(f"Invalid rPnL settling duration: {new_value}")
                return
                
        # Log successful configuration update
        self.logger.info(f"Successfully applied MAF configuration change: {config_key}")

    # --- Service Lifecycle and Health Monitoring ---
    def start(self):
        """Start the MasterActionFilterService."""
        self.logger.info("Starting MasterActionFilterService...")
        
        # Initialize health monitoring tracking
        self._is_running = True
        self._last_health_check_time = time.time()
        self._filter_operations_count = 0
        self._filter_failures = 0
        self._last_filter_operation_time = 0.0
        
        # INSTRUMENTATION: Publish service health status - STARTED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="MasterActionFilterService",
                event_name="ServiceHealthStatus",
                payload={
                    "health_status": "STARTED",
                    "status_reason": "MasterActionFilterService started successfully",
                    "service_name": "MasterActionFilterService",
                    "config_service_available": self._config is not None,
                    "price_provider_available": self._price_provider is not None,
                    "correlation_logger_available": self._correlation_logger is not None,
                    "symbol_states_count": len(self._symbol_states),
                    "maf_decisions_stream": self.config_stream_name_maf_decisions,
                    "health_status": "HEALTHY",
                    "startup_timestamp": time.time()
                }
            )
        
        self.logger.info("MasterActionFilterService started successfully")
    
    def stop(self):
        """Stop the MasterActionFilterService."""
        self.logger.info("Stopping MasterActionFilterService...")
        
        self._is_running = False
        
        # Get final metrics
        symbol_states_count = len(self._symbol_states)
        filter_operations_count = getattr(self, '_filter_operations_count', 0)
        filter_failures = getattr(self, '_filter_failures', 0)
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="MasterActionFilterService",
                event_name="ServiceHealthStatus",
                payload={
                    "health_status": "STOPPED",
                    "status_reason": "MasterActionFilterService stopped and cleaned up resources",
                    "service_name": "MasterActionFilterService",
                    "total_filter_operations": filter_operations_count,
                    "total_failures": filter_failures,
                    "success_rate": ((filter_operations_count - filter_failures) / filter_operations_count * 100) if filter_operations_count > 0 else 100,
                    "symbol_states_count": symbol_states_count,
                    "shutdown_timestamp": time.time()
                }
            )
        
        self.logger.info("MasterActionFilterService stopped successfully")



    def check_and_publish_health_status(self) -> None:
        """
        Public method to trigger health status check and publishing.
        Can be called periodically by external services or after filter operations.
        """
        # This method is maintained for interface compatibility but no longer publishes
        # health status since telemetry service has been removed
        pass
            
    def _track_filter_operation_metrics(self, success: bool) -> None:
        """
        Track filter operation metrics for health monitoring.
        
        Args:
            success: Whether the filter operation was successful
        """
        if not hasattr(self, '_filter_operations_count'):
            self._filter_operations_count = 0
        if not hasattr(self, '_filter_failures'):
            self._filter_failures = 0
            
        self._filter_operations_count += 1
        if not success:
            self._filter_failures += 1
        self._last_filter_operation_time = time.time()