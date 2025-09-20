# Trade Management Interfaces - Single Source of Truth
# This file contains trade management interfaces from:
# - modules/trade_management/interfaces.py
# Note: Order management interfaces have been moved to order_repository.py

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable, Union, Tuple, Set, TYPE_CHECKING
from enum import Enum

# Import TradeActionType from canonical source
from data_models import TradeActionType

if TYPE_CHECKING:
    from data_models.order_data_types import Order

class LifecycleState(Enum):
    NONE = 0
    OPEN_PENDING = 1 # Waiting for first OPEN fill/reject/cancel
    OPEN_ACTIVE = 2  # Position is open, may have active ADD/REDUCE/CLOSE orders
    CLOSE_PENDING = 3 # CLOSE order sent, waiting for final fill/reject/cancel
    CLOSED = 4        # Trade fully exited or failed to open
    ERROR = 5         # Unexpected error state

# --- Data Classes (Examples - Define structure for data passing) ---
# Consider using these or TypedDicts for clarity
from dataclasses import dataclass

@dataclass
class TradeSignal:
    action: TradeActionType
    symbol: str
    # Optional fields depending on action
    triggering_snapshot: Optional[Dict[str, Any]] = None
    cost_basis_change: Optional[float] = None
    pnl_change: Optional[float] = None
    reference_price: Optional[float] = None

@dataclass
class OrderParameters:
    symbol: str
    side: str # "BUY" or "SELL"
    quantity: float
    order_type: str # "LMT", "MKT"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    # Link back to originating signal/trade if needed
    parent_trade_id: Optional[int] = None
    event_type: Optional[str] = None # e.g. "OPEN", "ADD", "CLOSE"
    action_type: Optional[TradeActionType] = None # NEW FIELD: Stores the TradeActionType enum value
    local_order_id: Optional[int] = None # Filled in after repo create
    correlation_id: Optional[str] = None # <<<< NEW FIELD for Master Correlation ID >>>>


# --- Service Interfaces ---

class ISnapshotInterpreter(ABC):
    """Interprets OCR snapshots against position state to generate TradeSignals."""

    @abstractmethod
    def interpret_single_snapshot(self,
                                  symbol: str,
                                  new_state: Dict[str, Any],
                                  pos_data: Dict[str, Any],
                                  old_ocr_cost: float,
                                  old_ocr_realized: float,
                                  perf_timestamps: Optional[Dict[str, float]] = None
                                 ) -> Optional[TradeSignal]:
        """
        Interprets a single symbol's snapshot against current position state
        and previous OCR state to determine if a trade signal should be generated.
        """
        pass

    # Note: The 'interpret_snapshots' method has been removed from this interface
    # as TradeManagerService calls 'interpret_single_snapshot' in a loop.
    # If a bulk processing method is ever needed again by a different consumer,
    # it could be added back, but its implementation within SnapshotInterpreter
    # should not manage its own _last_ocr_state.


class IMasterActionFilter(ABC):
    """Filters trade signals based on settling periods and override conditions."""

    @abstractmethod
    def record_system_add_action(self, symbol: str, cost_basis_at_action: float, market_price_at_action: Optional[float] = None) -> None:
        """
        Record that a system ADD action was taken.

        Args:
            symbol: Trading symbol
            cost_basis_at_action: Cost basis at the time of action
            market_price_at_action: Optional market price at time of action
        """
        pass

    @abstractmethod
    def record_system_reduce_action(self, symbol: str, rPnL_at_action: float, market_price_at_action: float) -> None:
        """
        Record that a system REDUCE action was taken.

        Args:
            symbol: Trading symbol
            rPnL_at_action: Realized P&L at the time of action
            market_price_at_action: Market price at time of action
        """
        pass

    @abstractmethod
    def filter_signal(self, signal: Optional[TradeSignal], current_ocr_cost: float, current_ocr_rPnL: float) -> Optional[TradeSignal]:
        """
        Filter a trade signal based on settling periods and override conditions.

        Args:
            signal: The trade signal to filter
            current_ocr_cost: Current cost basis from OCR
            current_ocr_rPnL: Current realized P&L from OCR

        Returns:
            Filtered signal (may be None if suppressed)
        """
        pass

    @abstractmethod
    def reset_symbol_state(self, symbol: str) -> None:
        """
        Reset the action filter state for a specific symbol.

        Args:
            symbol: Trading symbol to reset
        """
        pass

    @abstractmethod
    def reset_all_states(self) -> None:
        """Reset all action filter states for all symbols."""
        pass

class IPositionManager(ABC):
    """Manages the application's internal view of positions based on fills."""
    @abstractmethod
    def process_fill(self,
                     symbol: str,
                     side: str,
                     shares_filled: float,
                     fill_price: float,
                     local_id: int, # Link to order
                     fill_time: float,
                     master_correlation_id: Optional[str] = None  # Master correlation ID from the order
                    ) -> None:
        """Updates internal position state (shares, avg_cost, PnL) based on a fill."""
        pass

    @abstractmethod
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Gets the current internal state for a symbol (shares, avg_cost, etc.). Returns copy."""
        pass

    @abstractmethod
    def get_all_positions_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Gets a snapshot copy of all internal positions."""
        pass

    @abstractmethod
    def get_trading_status(self, symbol: str) -> Dict[str, Any]:
        """Gets LULD/Halt status (state likely updated externally)."""
        pass

    @abstractmethod
    def update_trading_status(self, symbol: str, status_data: Dict[str, Any]) -> None:
        """Allows external components (like BrokerBridge via OR/TMS) to update status."""
        pass

    @abstractmethod
    def reset_position(self, symbol: str) -> None:
         """Force resets a symbol's internal state to closed/zero."""
         pass

    @abstractmethod
    def reset_all_positions(self) -> None:
         """Resets all internal positions."""
         pass

    @abstractmethod
    def update_fingerprint(self, symbol: str, fingerprint: tuple) -> None:
        """Updates the last broker fingerprint for a symbol."""
        pass

    @abstractmethod
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
        pass

    @abstractmethod
    def reset_retry_state(self, symbol: str) -> None:
        """Resets the retry count and cooldown for a symbol."""
        pass

    @abstractmethod
    def clear_failed_state(self, symbol: str) -> None:
        """Clears any failed state for a symbol."""
        pass

    @abstractmethod
    def force_close_state(self, symbol: str) -> None:
        """Forces a position to closed state."""
        pass

    @abstractmethod
    def mark_position_opening(self, symbol: str, potential_avg_price: float) -> None:
        """Marks a position as in the process of opening."""
        pass

    @abstractmethod
    def get_retry_count(self, symbol: str) -> int:
        """Gets the current retry count for a failed open."""
        pass

    @abstractmethod
    def sync_broker_position(self, symbol: str, shares: float, avg_price: float) -> None:
        """Forcibly sets the internal state to match an existing broker position."""
        pass


    # Optional: Method to sync with broker state
    # @abstractmethod
    # def reconcile_with_broker(self, broker_positions: Dict[str, Dict[str, Any]]) -> None:
    #    pass

class IOrderStrategy(ABC):
    """Calculates specific order parameters based on a trade signal."""
    @abstractmethod
    def calculate_order_params(self,
                               signal: TradeSignal,
                               position_manager: IPositionManager,
                               price_provider: Any, # Use actual IPriceProvider type hint
                               config_service: Any # Use actual GlobalConfig type hint
                              ) -> Optional[OrderParameters]:
        """Determines quantity, order type, price based on signal and config."""
        pass

class ITradeExecutor(ABC):
    """Handles placing orders with the broker and managing fingerprints."""
    @abstractmethod
    def execute_trade(self,
                      order_params: OrderParameters,
                      # Extra context needed for order repo / fingerprinting
                      time_stamps: Optional[Dict[str, Any]] = None,
                      snapshot_version: Optional[str] = None,
                      ocr_confidence: Optional[float] = None,
                      extra_fields: Optional[Dict[str, Any]] = None,
                      is_manual_trigger: bool = False, # Flag for manual orders bypassing some checks
                      perf_timestamps: Optional[Dict[str, float]] = None,
                      aggressive_chase: bool = False # Flag for aggressive liquidation
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
        """
        pass

# Callback Signatures for Trade Lifecycle Events
TradeOpenedCallback = Callable[[int, str], None]  # Args: trade_id, symbol
TradeClosedCallback = Callable[[int, str], None]  # Args: trade_id, symbol

class ITradeLifecycleManager(ABC):
    """Manages the overall state (lifecycle) of trades."""
    @abstractmethod
    def create_new_trade(self, symbol: str) -> int:
        """Creates a new trade record, returns trade_id."""
        pass

    @abstractmethod
    def find_active_trade_for_symbol(self, symbol: str) -> Optional[int]:
        """Finds the trade_id for the currently active trade for a symbol."""
        pass

    @abstractmethod
    def update_on_order_submission(self, trade_id: int, local_id: int) -> None:
         """Updates trade state when an initial order is submitted."""
         pass

    @abstractmethod
    def mark_trade_failed_to_open(self, trade_id: int, reason: str) -> None:
        """Marks a trade as failed to open if execution fails before any broker response."""
        pass

    @abstractmethod
    def update_trade_state(self,
                          trade_id: int,
                          new_state: Optional[LifecycleState] = None,
                          comment: Optional[str] = None,
                          add_fill: Optional[Dict[str, Any]] = None) -> None:
        """
        Updates specific attributes of a trade record like its state or comment,
        or appends a fill record.
        Only provided arguments are updated.
        """
        pass

    @abstractmethod
    def handle_order_finalized(self,
                               local_id: int,
                               symbol: str,
                               event_type: str,
                               final_status: str,
                               parent_trade_id: Optional[int]) -> None:
        """Processes order finalization to update trade lifecycle state."""
        pass

    @abstractmethod
    def get_trade_details(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """Gets details for a specific trade."""
        pass

    @abstractmethod
    def get_trade_by_id(self, trade_id: int) -> Optional[Dict[str, Any]]:
        """Gets details for a specific trade by its LCM integer t_id. Alias for get_trade_details."""
        pass

    @abstractmethod
    def get_all_trades_snapshot(self) -> Dict[int, Dict[str, Any]]:
         """Gets a snapshot of all trade records."""
         pass

    @abstractmethod
    def get_ledger_snapshot(self) -> List[Dict[str, Any]]:
         """Gets a snapshot of the event ledger."""
         pass

    @abstractmethod
    def log_event(self, evt: dict) -> int:
         """Adds an event to the internal ledger."""
         pass

    @abstractmethod
    def reset_all_trades(self) -> None:
         """Resets all trade records and the ledger."""
         pass

    @abstractmethod
    def load_trade_from_dict(self, trade_dict: Dict[str, Any]) -> int:
        """
        Loads a trade from a dictionary (e.g., from JSON).
        Ensures that the lifecycle_state is properly converted to an enum.
        Returns the trade_id of the loaded trade.
        """
        pass

    @abstractmethod
    def create_or_sync_trade(self, symbol: str, shares: float, avg_price: float) -> Optional[int]:
        """Finds an active trade or creates/updates one to match a synced position."""
        pass

    @abstractmethod
    def register_trade_opened_callback(self, callback: TradeOpenedCallback) -> None:
        """Registers a callback to be invoked when a trade is confirmed fully open."""
        pass

    @abstractmethod
    def unregister_trade_opened_callback(self, callback: TradeOpenedCallback) -> None:
        """Unregisters a trade opened callback."""
        pass

    @abstractmethod
    def register_trade_closed_callback(self, callback: TradeClosedCallback) -> None:
        """Registers a callback to be invoked when a trade is confirmed fully closed."""
        pass

    @abstractmethod
    def unregister_trade_closed_callback(self, callback: TradeClosedCallback) -> None:
        """Unregisters a trade closed callback."""
        pass


# --- Refined ITradeManagerService ---
# Keep the original name for now, but its role changes
class ITradeManagerService(ABC):
    """
    Orchestrates the trade management process. Receives external triggers
    (snapshots, manual actions) and coordinates sub-components
    (Interpreter, Strategy, Executor, PositionMgr, LifecycleMgr, RiskMgr)
    to manage trades and positions. Provides data for the GUI.
    """
    # --- Lifecycle & Config ---
    @abstractmethod
    def initialize(self, poll_interval_s: float = 30.0) -> None: pass
    @abstractmethod
    def shutdown(self) -> None: pass
    @abstractmethod
    def set_debug_mode(self, flag: bool) -> None: pass
    @abstractmethod
    def set_trading_enabled(self, enabled: bool) -> None: pass
    @abstractmethod
    def is_trading_enabled(self) -> bool: pass
    @abstractmethod
    def set_emergency_force_close_callback(self, func: Callable[[str, str], None]) -> None: pass

    # --- Core Processing ---
    @abstractmethod
    def process_snapshots(self, snapshots: Dict[str, Dict[str, Any]], time_stamps: Optional[dict] = None) -> None: pass

    # --- Manual Actions ---
    @abstractmethod
    def manual_add_shares(self,
                          symbol: Optional[str] = None,
                          # shares_to_add: Optional[float] = None, # <<< REMOVE THIS PARAMETER
                          completion_callback: Optional[Callable[[bool, str, str], None]] = None
                         ) -> bool:
        """
        Adds shares to the currently active position or specified symbol,
        using the quantity from config.manual_shares. Triggered by manual user action.
        Independent of OCR-driven add_type.
        """
        pass
    @abstractmethod
    def force_close_all(self, reason: str = "Manual Trigger") -> bool: pass
    @abstractmethod
    def force_close_and_halt_trading(self, reason: str = "Manual Button") -> bool: pass

    # --- Data Provision for GUI ---
    @abstractmethod
    def get_open_position_display(self) -> str: pass
    @abstractmethod
    def get_historical_trades_display(self) -> str: pass
    @abstractmethod
    def get_current_positions(self) -> Dict[str, Dict[str, Any]]: pass
    @abstractmethod
    def get_position_details(self, symbol: str) -> Optional[Dict[str, Any]]: pass
    @abstractmethod
    def get_total_pnl(self) -> Tuple[float, float]: pass
    @abstractmethod
    def get_trade_history(self) -> List[Dict[str, Any]]: pass
    @abstractmethod
    def place_manual_order(self, symbol: str, side: str, quantity: float, order_type: str = "MKT", limit_price: Optional[float] = None) -> Optional[int]: pass
    @abstractmethod
    def force_close_position(self, symbol: str, reason: str = "Manual Close No Halt") -> bool: pass
    @abstractmethod
    def force_close_single_symbol_and_halt_system(self, symbol: str, reason: str = "Manual Emergency Close") -> bool: pass
    @abstractmethod
    def force_close_all_and_halt_system(self, reason: str = "Manual Button") -> bool: pass
    @abstractmethod
    def force_close_and_halt_trading(self, reason: str = "Manual Button") -> bool: pass
    # Add other getters if GUI needs more specific data (e.g., get_active_symbol_details)

    # --- Callbacks (Maybe move to specific sub-components if appropriate?) ---
    # handle_order_finalized is likely handled internally by TradeLifecycleManager now
    # process_fill is likely handled internally by PositionManager now
    # handle_manual_broker_event might be handled by OrderRepository now
    # update_trading_status likely handled by PositionManager now


