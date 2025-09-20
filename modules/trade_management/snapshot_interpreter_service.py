"""
SnapshotInterpreterService module.

This module provides the SnapshotInterpreterService class that implements ISnapshotInterpreter.
It interprets OCR snapshots to generate trade signals based on position state transitions and value changes.
Ported from legacy SnapshotInterpreter with enhanced pre-OPEN checks.
"""

import logging
import re
import time
from typing import Dict, Any, Optional

from interfaces.trading.services import (
    ISnapshotInterpreter,
    ITradeLifecycleManager,
    IPositionManager,
    TradeSignal,
    TradeActionType,
    LifecycleState
)
from interfaces.order_management.services import IOrderRepository
from interfaces.logging.services import ICorrelationLogger
from utils.global_config import GlobalConfig as IConfigService
from typing import TYPE_CHECKING

# For type hints
if TYPE_CHECKING:
    from core.ipc_interfaces import IBulletproofBabysitterIPCClient

logger = logging.getLogger(__name__)


class SnapshotInterpreterService(ISnapshotInterpreter):
    """
    Service that interprets OCR snapshots to generate trade signals.
    
    Analyzes position state transitions (closed->long, long->closed, long->long)
    and value changes to determine appropriate trading actions.
    """

    def __init__(self,
                 lifecycle_manager: ITradeLifecycleManager,
                 config_service: IConfigService,
                 position_manager: IPositionManager,
                 order_repository: IOrderRepository,
                 ipc_client: Optional['IBulletproofBabysitterIPCClient'] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initialize the SnapshotInterpreterService.

        Args:
            lifecycle_manager: Trade lifecycle management service
            config_service: Global configuration service
            position_manager: Position management service for pre-OPEN checks
            order_repository: Order repository for pending order checks
            ipc_client: IPC client for bulletproof Redis publishing
            correlation_logger: Optional correlation logger for event logging
        """
        self.logger = logging.getLogger(__name__)
        self._lifecycle_manager = lifecycle_manager
        self._config_service = config_service
        self._position_manager = position_manager
        self._order_repository = order_repository
        # self._telemetry_service = telemetry_service  # REMOVED: Using correlation logger instead
        self._ipc_client = ipc_client
        self._correlation_logger = correlation_logger
        
        # Configure signal decisions stream
        self._signal_decisions_stream = getattr(config_service, 'redis_stream_signal_decisions', 'testrade:signal-decisions')
        
        if self._correlation_logger:
            self.logger.info("SnapshotInterpreterService initialized with correlation logger for signal decision publishing.")
        else:
            self.logger.info("SnapshotInterpreterService initialized without correlation logger - signal decisions will not be published.")
        
        self.logger.info("SnapshotInterpreterService initialized.")

    def interpret_snapshot(self, ocr_snapshot_data: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
        """
        Interpret OCR snapshot data for IntelliSense OCR engine compatibility.

        This method provides compatibility with the IntelliSense OCR engine which expects
        interpret_snapshot(ocr_snapshot_data, context) method signature.

        Args:
            ocr_snapshot_data: OCR snapshot data from IntelliSense
            context: Position context from PositionManager

        Returns:
            Dict[symbol, position_data] or None if interpretation fails
        """
        try:
            # Extract symbol from snapshot data
            symbol = ocr_snapshot_data.get('symbol', 'UNKNOWN')
            if symbol == 'UNKNOWN':
                self.logger.warning("interpret_snapshot: No symbol found in OCR snapshot data")
                return None

            # Get conditioned data from snapshot
            conditioned_data = ocr_snapshot_data.get('conditioned_data_snapshot', {})
            if not conditioned_data:
                self.logger.warning(f"interpret_snapshot: No conditioned data for symbol {symbol}")
                return None

            # Get position data from context
            pos_data = context.get(symbol, {})

            # Extract OCR values with defaults
            old_ocr_cost = 0.0  # Could be enhanced to track previous values
            old_ocr_realized = 0.0  # Could be enhanced to track previous values

            # Call the main interpretation method
            signal = self.interpret_single_snapshot(
                symbol=symbol,
                new_state=conditioned_data,
                pos_data=pos_data,
                old_ocr_cost=old_ocr_cost,
                old_ocr_realized=old_ocr_realized
            )

            # Convert signal to position format expected by IntelliSense
            if signal:
                # Return position data based on signal
                return {
                    symbol: {
                        'qty': signal.quantity if hasattr(signal, 'quantity') else 0,
                        'avg_price': signal.price if hasattr(signal, 'price') else 0.0,
                        'action': signal.action.name if hasattr(signal, 'action') else 'HOLD'
                    }
                }

            # Return current position if no signal generated
            return {
                symbol: {
                    'qty': pos_data.get('shares', 0),
                    'avg_price': pos_data.get('avg_price', 0.0),
                    'action': 'HOLD'
                }
            }

        except Exception as e:
            self.logger.error(f"interpret_snapshot failed for {ocr_snapshot_data.get('symbol', 'UNKNOWN')}: {e}")
            return None

    def _parse_dollar_value(self, value_str) -> float:
        """
        Parse dollar value from various formats (direct port from legacy).
        
        Args:
            value_str: Value to parse (string, float, int, or None)
            
        Returns:
            Parsed float value rounded to 2 decimal places
            
        Raises:
            ValueError: If value cannot be parsed
        """
        if isinstance(value_str, (float, int)):
            return round(float(value_str), 2)
        if isinstance(value_str, str):
            clean = re.sub(r'[,\s$]', '', value_str)
            if not clean:
                return 0.0
            try:
                return round(float(clean), 2)
            except ValueError:
                self.logger.error(f"Could not parse dollar value: '{value_str}' -> '{clean}'")
                raise ValueError(f"Invalid dollar value format: {value_str}")
        if value_str is None:
            return 0.0
        self.logger.error(f"Could not parse value of type {type(value_str)}: {value_str}")
        raise ValueError(f"Unsupported type for dollar value parsing: {type(value_str)}")

    def interpret_single_snapshot(self,
                                  symbol: str,
                                  new_state: Dict[str, Any],
                                  pos_data: Dict[str, Any],
                                  old_ocr_cost: float,
                                  old_ocr_realized: float,
                                  correlation_id: Optional[str] = None) -> Optional[TradeSignal]:
        """
        Interpret a single OCR snapshot to generate trade signals.
        
        Args:
            symbol: Trading symbol
            new_state: Current OCR snapshot data from CleanedOCRSnapshotEventData
            pos_data: Current position state from PositionManager
            old_ocr_cost: Previous OCR cost basis
            old_ocr_realized: Previous OCR realized P&L
            
        Returns:
            TradeSignal if action should be taken, None otherwise
        """
        self.logger.debug(f"[{symbol}] InterpreterService processing snapshot...")
        
        try:
            # Parse new state from CleanedOCRSnapshotEventData.snapshots[symbol]
            new_strat_str = new_state.get("strategy_hint", "Closed")
            new_strat = new_strat_str.lower().strip() if isinstance(new_strat_str, str) else "closed"

            raw_cost = new_state.get("cost_basis", 0.0)
            raw_real = new_state.get("realized_pnl", 0.0)
            new_tCost = self._parse_dollar_value(raw_cost)
            new_tReal = self._parse_dollar_value(raw_real)
            
            self.logger.debug(f"[{symbol}] InterpreterService Parsed: Strat={new_strat}, Cost={new_tCost:.4f}, Real={new_tReal:.4f}")
        except ValueError as e:
            self.logger.error(f"[{symbol}] InterpreterService: Invalid numeric value in snapshot: {e} - Cost: '{raw_cost}', Realized: '{raw_real}'")
            return None

        # Get current position strategy from PositionManager
        old_pm_strat = pos_data.get("strategy", "closed")
        self.logger.debug(f"[{symbol}] InterpreterService State: Current PM Strat={old_pm_strat}, Snapshot Strat={new_strat}")
        
        signal: Optional[TradeSignal] = None

        # State transition analysis
        if old_pm_strat == "closed" and new_strat == "long":
            # "Closed" -> "Long" Transition (OPEN Signal) with enhanced pre-OPEN check
            signal = self._handle_open_signal(symbol, new_state)
            
        elif old_pm_strat == "long" and new_strat == "closed":
            # "Long" -> "Closed" Transition (CLOSE Signal)
            self.logger.info(f"[{symbol}] InterpreterService: Detected long -> closed transition. Generating CLOSE signal.")
            signal = TradeSignal(action=TradeActionType.CLOSE_POSITION, symbol=symbol, triggering_snapshot=new_state.copy())
            
        elif old_pm_strat == "long" and new_strat == "long":
            # "Long" -> "Long" Transition (ADD/REDUCE Signals)
            signal = self._handle_long_to_long_transition(symbol, new_state, new_tCost, new_tReal, old_ocr_cost, old_ocr_realized)

        # Final logging and return
        if signal:
            self.logger.info(f"[{symbol}] InterpreterService: Generated Signal = {signal.action.name}")
        else:
            self.logger.debug(f"[{symbol}] InterpreterService: No signal generated for PM_strat={old_pm_strat} -> OCR_strat={new_strat} with value changes.")

        # Publish signal interpretation decision to Redis for IntelliSense visibility
        interpretation_context = {
            "state_transition": f"{old_pm_strat} -> {new_strat}",
            "old_ocr_cost": old_ocr_cost,
            "old_ocr_realized": old_ocr_realized,
            "new_cost": new_tCost,
            "new_realized": new_tReal,
            "cost_change": new_tCost - old_ocr_cost,
            "realized_change": new_tReal - old_ocr_realized,
            "position_data": pos_data.copy() if pos_data else {}
        }
        
        # Extract causation_id from the OCR snapshot event metadata if available
        causation_id = None
        if isinstance(new_state, dict):
            # Look for event metadata in the snapshot
            event_metadata = new_state.get('_event_metadata', {})
            causation_id = event_metadata.get('eventId')
        
        self._log_signal_interpretation(
            symbol=symbol,
            signal=signal,
            interpretation_context=interpretation_context,
            correlation_id=correlation_id,
            causation_id=causation_id
        )

        return signal

    def _handle_open_signal(self, symbol: str, new_state: Dict[str, Any]) -> Optional[TradeSignal]:
        """
        Handle OPEN signal generation with comprehensive pre-OPEN checks.

        Args:
            symbol: Trading symbol
            new_state: Current OCR snapshot data

        Returns:
            TradeSignal for OPEN_LONG if checks pass, None if suppressed
        """
        # PRE-OPEN CHECK (more comprehensive than legacy)
        pm_pos_data = self._position_manager.get_position(symbol)
        pm_is_open = pm_pos_data.get('open', False) if pm_pos_data else False
        pm_current_shares = pm_pos_data.get('shares', 0.0) if pm_pos_data else 0.0

        if pm_is_open and abs(pm_current_shares) > 1e-9:
            self.logger.warning(f"[{symbol}] InterpreterService: Suppressed OPEN. PositionManager shows existing open position: {pm_current_shares} shares.")
            return None

        # Check OrderRepository for active (non-final) OPEN BUY orders for this symbol
        # Note: This requires a method in IOrderRepository to check for pending orders
        # For now, we'll implement a basic check if the method exists
        pending_open_orders = []
        if hasattr(self._order_repository, 'get_active_open_buy_orders'):
            try:
                pending_open_orders = self._order_repository.get_active_open_buy_orders(symbol)
            except Exception as e:
                self.logger.warning(f"[{symbol}] InterpreterService: Error checking pending orders: {e}")

        if pending_open_orders:
            self.logger.warning(f"[{symbol}] InterpreterService: Suppressed OPEN. OrderRepository shows {len(pending_open_orders)} pending OPEN BUY order(s).")
            return None

        # Legacy check using TradeLifecycleManager (optional, can be enabled if needed)
        # active_trade_id = self._lifecycle_manager.find_active_trade_for_symbol(symbol)
        # if active_trade_id is not None:
        #     trade_details = self._lifecycle_manager.get_trade_details(active_trade_id)
        #     current_trade_state = trade_details.get("lifecycle_state") if trade_details else None
        #     if current_trade_state in (LifecycleState.OPEN_PENDING, LifecycleState.OPEN_ACTIVE, LifecycleState.CLOSE_PENDING):
        #         self.logger.debug(f"[{symbol}] InterpreterService: Skipping OPEN signal, active trade {active_trade_id} found in state {current_trade_state.name if current_trade_state else 'Unknown'}.")
        #         return None

        self.logger.info(f"[{symbol}] InterpreterService: Detected closed -> long transition. Generating OPEN signal.")
        return TradeSignal(action=TradeActionType.OPEN_LONG, symbol=symbol, triggering_snapshot=new_state.copy())

    def _handle_long_to_long_transition(self, symbol: str, new_state: Dict[str, Any],
                                       new_tCost: float, new_tReal: float,
                                       old_ocr_cost: float, old_ocr_realized: float) -> Optional[TradeSignal]:
        """
        Handle Long -> Long transition for ADD/REDUCE signal generation.

        Args:
            symbol: Trading symbol
            new_state: Current OCR snapshot data
            new_tCost: New cost basis value
            new_tReal: New realized P&L value
            old_ocr_cost: Previous OCR cost basis
            old_ocr_realized: Previous OCR realized P&L

        Returns:
            TradeSignal for ADD_LONG or REDUCE_PARTIAL if thresholds met, None otherwise
        """
        self.logger.debug(f"[{symbol}] InterpreterService: Detected long -> long transition. Checking value changes...")
        abs_cost_diff = abs(new_tCost - old_ocr_cost)

        # Check for ADD signal based on cost basis change
        add_cost_threshold_key = 'cost_basis_initial_add_abs_delta_threshold'
        add_cost_threshold = float(getattr(self._config_service, add_cost_threshold_key, 0.02))
        self.logger.info(f"[{symbol}] InterpreterService L2L Cost Check: New={new_tCost:.4f}, Old={old_ocr_cost:.4f}, AbsDiff={abs_cost_diff:.4f}, Threshold={add_cost_threshold:.4f} (from config '{add_cost_threshold_key}')")

        if abs_cost_diff >= add_cost_threshold:
            self.logger.info(f"[{symbol}] InterpreterService L2L Cost AbsDiff EXCEEDED threshold. Generating ADD Signal.")
            # Ensure 'cost_change' key is added to triggering_snapshot
            trigger_snapshot_for_add = new_state.copy()
            trigger_snapshot_for_add["cost_change"] = new_tCost - old_ocr_cost
            signal = TradeSignal(
                action=TradeActionType.ADD_LONG,
                symbol=symbol,
                triggering_snapshot=trigger_snapshot_for_add
            )
            self.logger.info(f"[{symbol}] InterpreterService: Generated Signal = {signal.action.name}")
            return signal  # ADD takes precedence
        else:
            # Check for REDUCE signal based on realized P&L increase
            rPnL_increase = new_tReal - old_ocr_realized

            reduce_rPnL_threshold_key = 'rPnL_initial_reduce_increase_threshold'
            reduce_rPnL_threshold = float(getattr(self._config_service, reduce_rPnL_threshold_key, 5.0))
            self.logger.info(f"[{symbol}] InterpreterService L2L rPnL Check: New={new_tReal:.2f}, Old={old_ocr_realized:.2f}, Increase={rPnL_increase:.2f}, Threshold={reduce_rPnL_threshold:.2f} (from config '{reduce_rPnL_threshold_key}')")

            if rPnL_increase >= reduce_rPnL_threshold:
                self.logger.info(f"[{symbol}] InterpreterService L2L rPnL Increase EXCEEDED threshold. Generating REDUCE Signal.")
                # Ensure 'pnl_change' key is added to triggering_snapshot
                trigger_snapshot_for_reduce = new_state.copy()
                trigger_snapshot_for_reduce["pnl_change"] = rPnL_increase
                return TradeSignal(
                    action=TradeActionType.REDUCE_PARTIAL,
                    symbol=symbol,
                    triggering_snapshot=trigger_snapshot_for_reduce
                )

        return None

    def _log_signal_interpretation(self, symbol: str, signal: Optional[TradeSignal], 
                                               interpretation_context: Dict[str, Any], 
                                               correlation_id: Optional[str] = None,
                                               causation_id: Optional[str] = None):
        """
        Publish signal interpretation decisions to Redis for IntelliSense visibility.
        
        Args:
            symbol: Trading symbol
            signal: Generated signal (or None if no signal)
            interpretation_context: Context data for the interpretation
            correlation_id: Correlation ID for tracing
            causation_id: ID of the event that caused this interpretation
        """
        if not self._correlation_logger:
            return  # No correlation logger available
            
        # TANK Mode: Check if telemetry publishing is disabled
        if not getattr(self._config_service, 'ENABLE_IPC_DATA_DUMP', True):
            self.logger.debug(f"TANK_MODE: Skipping telemetry send for signal interpretation (Symbol: {symbol})")
            return
            
        try:
            from utils.thread_safe_uuid import get_thread_safe_uuid
            
            # Determine event type and decision
            if signal:
                event_type = "TESTRADE_SIGNAL_INTERPRETATION"
                decision_type = "SIGNAL_GENERATED"
                signal_data = {
                    "action": signal.action.name,
                    "triggering_snapshot": signal.triggering_snapshot
                }
            else:
                event_type = "TESTRADE_SIGNAL_INTERPRETATION"
                decision_type = "NO_SIGNAL_GENERATED"
                signal_data = None
            
            # Create event payload
            payload = {
                "symbol": symbol,
                "decision_type": decision_type,
                "signal_data": signal_data,
                "interpretation_context": interpretation_context,
                "timestamp": time.time()
            }
            
            # Create Redis message in standard format
            message = {
                "metadata": {
                    "eventId": get_thread_safe_uuid(),
                    "correlationId": correlation_id or get_thread_safe_uuid(),
                    "causationId": causation_id or correlation_id,  # Use causation_id if provided, else correlation_id
                    "timestamp_ns": time.perf_counter_ns(),
                    "epoch_timestamp_s": time.time(),
                    "eventType": event_type,
                    "sourceComponent": "SnapshotInterpreterService"
                },
                "payload": payload
            }
            
            # Use telemetry service for clean, fire-and-forget publishing
            telemetry_data = {
                "payload": payload,
                "correlation_id": correlation_id,
                "causation_id": causation_id or correlation_id  # Use causation_id if provided, else correlation_id
            }
            
            # self._telemetry_service.enqueue(
            #     source_component="SnapshotInterpreterService",
            #     event_type=event_type,
            #     payload=telemetry_data,
            #     stream_override=self._signal_decisions_stream
            # )
            
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="SnapshotInterpreterService",
                    event_name="SignalInterpretation",
                    payload=telemetry_data,
                    stream_override=self._signal_decisions_stream
                )
            self.logger.debug(f"Published signal interpretation: {decision_type} for {symbol}")
                
        except Exception as e:
            self.logger.error(f"Error publishing signal interpretation for {symbol}: {e}", exc_info=True)
