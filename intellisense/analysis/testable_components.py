import logging
import time
import threading
from typing import Dict, Any, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from utils.global_config import GlobalConfig
    from core.interfaces import IEventBus, IBroker
    from data_models.order_data_types import Order # Actual Order dataclass
    from intellisense.core.types import PositionState # Actually, this is for setting, not getting

# ... (imports and IntelliSenseTestableOrderRepository as before) ...
from intellisense.capture.feedback_isolation_types import PositionState # Import new type

# Placeholder for BaseOrderRepository and BasePositionManager
# In a real system, these would be your actual production classes.
class ApplicationCorePlaceholder: intellisense_test_mode_active: bool = False
class BaseOrderRepository:
    def __init__(self, event_bus: Any, broker_service: Any, position_manager: Any, app_core_instance_for_test_mode_check: Any, *args, **kwargs):
        self.app_core = app_core_instance_for_test_mode_check # For test_mode check
        logger.info("BaseOrderRepository placeholder init.")
    def get_order_by_local_id(self, local_id: int) -> Optional[Dict[str, Any]]: # Placeholder, returns dict
        logger.debug(f"BaseOR.get_order_by_local_id for {local_id}")
        # Simulate returning an order-like dict if it exists conceptually
        if hasattr(self, '_internal_orders') and local_id in self._internal_orders:
            return self._internal_orders[local_id]
        return None
    def _process_broker_update_from_event_data(self, event_type: str, payload: Dict[str, Any]):
        logger.info(f"BaseOrderRepository (Placeholder): _process_broker_update_from_event_data for {event_type}")
        # Simulate updating an internal order store
        if not hasattr(self, '_internal_orders'): self._internal_orders = {}
        local_id = payload.get('local_order_id')
        if local_id:
            if local_id not in self._internal_orders: self._internal_orders[local_id] = {'local_id': local_id}
            self._internal_orders[local_id]['status'] = payload.get('status', {}).get('value', 'UNKNOWN') # Assuming status is an enum
            self._internal_orders[local_id]['filled_quantity'] = payload.get('filled_quantity', self._internal_orders[local_id].get('filled_quantity',0))
            self._internal_orders[local_id]['last_updated'] = time.time()


class BasePositionManager:
    def __init__(self, event_bus: Any, app_core_instance_for_test_mode_check: Any, *args, **kwargs):
        self.app_core = app_core_instance_for_test_mode_check
        logger.info("BasePositionManager placeholder init.")
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]: # Placeholder, returns dict
        logger.debug(f"BasePM.get_position for {symbol}")
        if hasattr(self, '_internal_positions') and symbol in self._internal_positions:
            return self._internal_positions[symbol]
        return {"symbol": symbol, "shares": 0, "avg_price": 0, "open": False, "last_updated": time.time()} # Default
    def _apply_fill_from_event_data(self, fill_payload: Dict[str, Any]):
        logger.info(f"BasePositionManager (Placeholder): _apply_fill_from_event_data for {fill_payload.get('symbol')}")
        # Simulate updating an internal position store
        if not hasattr(self, '_internal_positions'): self._internal_positions = {}
        symbol = fill_payload.get('symbol')
        if symbol:
            if symbol not in self._internal_positions: self._internal_positions[symbol] = {"symbol": symbol, "shares": 0, "avg_price": 0, "open": False}
            # Simplified fill logic for placeholder
            side = fill_payload.get('side',{}).get('value','BUY') # Assuming side is enum with value
            qty = fill_payload.get('filled_quantity', 0.0)
            price = fill_payload.get('fill_price', 0.0)
            current_shares = self._internal_positions[symbol]['shares']
            current_avg_price = self._internal_positions[symbol]['avg_price']
            if side == "BUY":
                self._internal_positions[symbol]['avg_price'] = ((current_avg_price * current_shares) + (price * qty)) / (current_shares + qty) if (current_shares + qty) != 0 else price
                self._internal_positions[symbol]['shares'] += qty
            elif side == "SELL":
                self._internal_positions[symbol]['shares'] -= qty # Avg price unchanged on sell unless flat
            self._internal_positions[symbol]['open'] = abs(self._internal_positions[symbol]['shares']) > 1e-9
            self._internal_positions[symbol]['last_updated'] = time.time()

class BasePriceRepository:
    def __init__(self, *args, **kwargs):
        self.app_core_ref_for_intellisense: Optional['IntelliSenseApplicationCore'] = None # Will be set by ISAppCore
        logger.info("BasePriceRepository placeholder init.")
        # Placeholder internal state for get_cached_state
        self.last_trades: Dict[str, Dict[str,Any]] = {} # symbol -> {'price': float, 'timestamp': float}
        self.bids: Dict[str, Dict[str,Any]] = {}
        self.asks: Dict[str, Dict[str,Any]] = {}
        self.last_trade_timestamps: Dict[str,float] = {}
        self.last_bid_timestamps: Dict[str,float] = {}
        self.last_ask_timestamps: Dict[str,float] = {}

    def _process_trade_update(self, payload: Dict[str, Any]): # Conceptual internal method
        symbol = payload.get('symbol')
        price = payload.get('price')
        ts = payload.get('timestamp', time.time())
        if symbol and price is not None:
            self.last_trades[symbol] = {'price': price, 'timestamp': ts}
            self.last_trade_timestamps[symbol] = ts
            logger.info(f"BasePriceRepo (Placeholder): _process_trade_update for {symbol} @ {price}")

    def _process_quote_update(self, payload: Dict[str, Any]): # Conceptual internal method
        symbol = payload.get('symbol')
        bid = payload.get('bid_price') # Assuming payload keys match LA spec
        ask = payload.get('ask_price')
        ts = payload.get('timestamp', time.time())
        if symbol:
            if bid is not None:
                self.bids[symbol] = {'price': bid, 'timestamp': ts}
                self.last_bid_timestamps[symbol] = ts
            if ask is not None:
                self.asks[symbol] = {'price': ask, 'timestamp': ts}
                self.last_ask_timestamps[symbol] = ts
            logger.info(f"BasePriceRepo (Placeholder): _process_quote_update for {symbol} B:{bid} A:{ask}")

logger = logging.getLogger(__name__)

class IntelliSenseTestablePriceRepository(BasePriceRepository):
    """
    Enhanced PriceRepository with test hooks for IntelliSense validation and precise injection.
    """
    def __init__(self,
                 app_core_ref_for_intellisense: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig',
                 original_event_bus: 'IEventBus',
                 *args, **kwargs):
        """Initialize with ComponentFactory dependencies."""
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         *args, **kwargs)
        self.app_core_ref_for_intellisense = app_core_ref_for_intellisense
        logger.info(f"IntelliSenseTestablePriceRepository constructed via factory. AppCoreRef: {type(self.app_core_ref_for_intellisense).__name__}")

    def on_test_price_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """
        Direct injection point for replayed price events during IntelliSense validation.
        Calls internal processing methods of the base PriceRepository.
        """
        if not self.app_core_ref_for_intellisense or \
           not self.app_core_ref_for_intellisense.intellisense_test_mode_active: # Check flag on app_core
            logger.error("CRITICAL: IntelliSenseTestablePriceRepository.on_test_price_event "
                         "called when not in IntelliSense test mode or app_core_ref not set!")
            # raise RuntimeError("Test method on_test_price_event called in potentially live mode.")
            return # Fail silently in placeholder to avoid crashing tests if app_core_ref is not set up yet

        symbol = payload.get('symbol', 'UNKNOWN_SYM')
        logger.debug(f"TestablePriceRepo.on_test_price_event: Processing '{event_type}' for {symbol}")

        # Process through normal update path by calling base class's internal methods
        # These method names (_process_trade_update, _process_quote_update) are conceptual
        # and must match methods in your actual BasePriceRepository that do the core work
        # after initial parsing/validation in on_trade/on_quote.
        try:
            if event_type.upper() == "TRADE":
                if hasattr(self, '_process_trade_update'): # Check if method exists
                    self._process_trade_update(payload)
                else: # Fallback to calling public on_trade if internal not exposed/available
                    logger.warning("TestablePriceRepo: _process_trade_update not found, calling public on_trade (less ideal for pure internal logic test).")
                    super().on_trade(payload) # This assumes payload is compatible with on_trade input
            elif event_type.upper() in ["QUOTE", "QUOTE_BID", "QUOTE_ASK", "QUOTE_BOTH"]: # Handle various quote types
                if hasattr(self, '_process_quote_update'):
                    self._process_quote_update(payload)
                else:
                    logger.warning("TestablePriceRepo: _process_quote_update not found, calling public on_quote.")
                    super().on_quote(payload) # Assumes payload compatible
            else:
                logger.warning(f"TestablePriceRepo: Unknown event_type '{event_type}' in on_test_price_event for {symbol}.")
        except Exception as e:
            logger.error(f"Error in TestablePriceRepo.on_test_price_event processing {symbol}: {e}", exc_info=True)
            # Decide if this should re-raise or just log

    def get_cached_state(self, symbol: str) -> Dict[str, Any]:
        """
        Get detailed internal state for a symbol for validation purposes.
        This requires knowing the internal storage structure of BasePriceRepository.
        """
        symbol_upper = symbol.upper()
        logger.debug(f"TestablePriceRepo: Getting cached state for {symbol_upper}")

        # These access patterns need to match your actual BasePriceRepository internals
        last_trade_info = getattr(self, 'last_trades', {}).get(symbol_upper, {})
        bid_info = getattr(self, 'bids', {}).get(symbol_upper, {})
        ask_info = getattr(self, 'asks', {}).get(symbol_upper, {})

        return {
            "symbol": symbol_upper,
            "last_trade_price": last_trade_info.get('price'),
            "last_trade_timestamp": last_trade_info.get('timestamp'),
            "bid_price": bid_info.get('price'),
            "bid_timestamp": bid_info.get('timestamp'), # Assuming BasePriceRepository stores this
            "ask_price": ask_info.get('price'),
            "ask_timestamp": ask_info.get('timestamp'), # Assuming BasePriceRepository stores this
        }

    # _get_timestamps method as per LA spec (if these specific dicts exist in base)
    # def _get_timestamps(self, symbol: str) -> Dict[str, float]:
    #     return {
    #         "trade": getattr(self, 'last_trade_timestamps', {}).get(symbol, 0.0),
    #         "bid": getattr(self, 'last_bid_timestamps', {}).get(symbol, 0.0),
    #         "ask": getattr(self, 'last_ask_timestamps', {}).get(symbol, 0.0)
    #     }


class IntelliSenseTestableOrderRepository(BaseOrderRepository):
    def __init__(self,
                 app_core_instance_for_test_mode_check: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig',
                 original_event_bus: 'IEventBus',
                 original_broker_service: 'IBroker',
                 original_position_manager: Optional['BasePositionManager'],
                 *args, **kwargs):
        """Initialize with ComponentFactory dependencies."""
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         broker_service=original_broker_service,
                         position_manager=original_position_manager,
                         *args, **kwargs)
        self.app_core_instance_for_test_mode_check = app_core_instance_for_test_mode_check
        logger.info(f"IntelliSenseTestableOrderRepository constructed via factory. AppCoreRef: {type(self.app_core_instance_for_test_mode_check).__name__}")

    def on_test_broker_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not hasattr(self.app_core, 'intellisense_test_mode_active') or \
           not self.app_core.intellisense_test_mode_active:
            logger.error("CRITICAL: TestableOR.on_test_broker_event called when not in IntelliSense test mode!")
            return
        logger.debug(f"TestableOR.on_test_broker_event: Processing {event_type} for {payload.get('local_order_id')}")
        if hasattr(self, '_process_broker_update_from_event_data'): # Check if base method exists
             self._process_broker_update_from_event_data(event_type, payload)
        else: logger.info(f"TestableOR (Placeholder): Simulating processing for {event_type} via on_test_broker_event.")


    def get_cached_order_state(self, local_order_id_str: str) -> Dict[str, Any]:
        """Get detailed internal state of an order for validation."""
        logger.debug(f"TestableOR: Getting cached state for local_order_id '{local_order_id_str}'")
        try:
            local_id = int(local_order_id_str) # OrderRepository uses int keys mostly
        except ValueError:
            logger.warning(f"TestableOR: Invalid local_order_id format '{local_order_id_str}' for get_cached_order_state.")
            return {"local_id": local_order_id_str, "error": "Invalid ID format", "exists": False}

        # Assuming self.get_order_by_local_id(local_id) returns your actual Order dataclass instance
        # or a dict that resembles it.
        order_obj = self.get_order_by_local_id(local_id) # Call method from BaseOrderRepository or its override

        if not order_obj:
            return {"local_id": local_order_id_str, "exists": False, "status": "NOT_FOUND"}

        # Convert Order object (dataclass) to dict, extracting key fields for validation
        # This needs to align with your actual Order dataclass structure
        # Using getattr for safety with placeholder Order object.
        return {
            "local_id": str(getattr(order_obj, 'local_id', local_order_id_str)), # Ensure string for consistency if needed
            "exists": True,
            "status": getattr(getattr(order_obj, 'ls_status', None), 'value', "UNKNOWN_STATUS"), # Get enum value
            "filled_quantity": getattr(order_obj, 'filled_quantity', 0.0),
            "requested_shares": getattr(order_obj, 'requested_shares', 0.0),
            "leftover_shares": getattr(order_obj, 'leftover_shares', getattr(order_obj, 'requested_shares', 0.0) - getattr(order_obj, 'filled_quantity', 0.0)),
            "avg_fill_price": getattr(order_obj, 'avg_fill_price', None), # If Order dataclass has this calculated
            "broker_order_id": getattr(order_obj, 'ls_order_id', None),
            "last_updated": getattr(order_obj, 'timestamp_last_update', getattr(order_obj, 'timestamp_broker_final_status', None)), # Example
            # Add other relevant fields from your Order dataclass
            "fills_count": len(getattr(order_obj, 'fills', [])),
        }


class IntelliSenseTestablePositionManager(BasePositionManager):
    def __init__(self,
                 app_core_instance_for_test_mode_check: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig',
                 original_event_bus: 'IEventBus',
                 *args, **kwargs):
        """Initialize with ComponentFactory dependencies."""
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         *args, **kwargs)
        self.app_core_instance_for_test_mode_check = app_core_instance_for_test_mode_check
        self._forced_states: Dict[str, PositionState] = {} # Store externally set states
        logger.info(f"IntelliSenseTestablePositionManager constructed via factory. AppCoreRef: {type(self.app_core_instance_for_test_mode_check).__name__}")

    def on_test_broker_fill(self, fill_payload: Dict[str, Any]) -> None:
        # ... (implementation from Chunk 15, calling super()._apply_fill_from_event_data or similar)
        if not hasattr(self.app_core, 'intellisense_test_mode_active') or not self.app_core.intellisense_test_mode_active:
            logger.error("CRITICAL: TestablePM.on_test_broker_fill called out of test mode!"); return
        logger.debug(f"TestablePM.on_test_broker_fill for {fill_payload.get('symbol')}")
        if hasattr(self, '_apply_fill_from_event_data'): self._apply_fill_from_event_data(fill_payload)
        else: logger.info(f"TestablePM (Placeholder): Simulating applying fill for {fill_payload.get('symbol')}.")


    def get_cached_position_state(self, symbol: str) -> Dict[str, Any]:
        """Get detailed internal state of a position for validation."""
        symbol_upper = symbol.upper()
        logger.debug(f"TestablePM: Getting cached state for {symbol_upper}")

        # This method should return the current state as understood by PositionManager
        # It might call the overridden self.get_position() or access internal caches directly
        # For consistency with the overridden get_position:
        pos_data = self.get_position(symbol_upper) # This will return forced state if set

        if not pos_data: # If symbol not found or get_position returns None
            return {"symbol": symbol_upper, "exists": False, "open": False, "total_shares": 0.0}

        # Ensure all fields from PositionState TypedDict are present
        return {
            "symbol": pos_data.get('symbol', symbol_upper),
            "exists": True,
            "open": pos_data.get('open', False),
            "strategy": pos_data.get('strategy', "UNKNOWN"),
            "total_shares": pos_data.get('total_shares', pos_data.get('shares', 0.0)),
            "total_avg_price": pos_data.get('total_avg_price', pos_data.get('avg_price', 0.0)),
            "overall_realized_pnl": pos_data.get('overall_realized_pnl', 0.0),
            "unrealized_pnl": pos_data.get('unrealized_pnl'), # May be None
            "market_value": pos_data.get('market_value'),   # May be None
            "last_updated": pos_data.get('last_update', pos_data.get('last_update_timestamp', time.time()))
        }
    # ... (force_set_internal_state, get_position override, reset_forced_state as from Chunk 15)

    def force_set_internal_state(self, symbol: str, state_to_set: PositionState) -> bool:
        """
        Directly sets the internal state for a symbol, mimicking PositionManager's internal dict.
        This allows FeedbackIsolationManager to control what get_position() returns.
        """
        if not (self.app_core_instance_for_test_mode_check and hasattr(self.app_core_instance_for_test_mode_check, 'intellisense_test_mode_active') and \
                self.app_core_instance_for_test_mode_check.intellisense_test_mode_active):
            logger.error("CRITICAL: TestablePM.force_set_internal_state called when not in IntelliSense test mode!")
            return False

        symbol_upper = symbol.upper()

        # Validate state_to_set has all keys from PositionState (or handle gracefully)
        # This is where PositionState TypedDict helps define the contract.
        # The actual _positions dict in your real PM might have more fields;
        # this method needs to update all relevant ones for consistent state.

        # For placeholder BasePositionManager, assume self._internal_positions is Dict[str, Dict[str,Any]]
        # and matches the structure used by its _get_position_no_lock and get_position.

        current_internal_pos = self._internal_positions.get(symbol_upper, {}) # Get current or empty

        # Update with values from PositionState TypedDict
        current_internal_pos['symbol'] = symbol_upper # from state['symbol']
        current_internal_pos['open'] = state_to_set['open']
        current_internal_pos['strategy'] = state_to_set['strategy']
        current_internal_pos['total_shares'] = state_to_set['total_shares']
        current_internal_pos['total_avg_price'] = state_to_set['total_avg_price']
        current_internal_pos['overall_realized_pnl'] = state_to_set['overall_realized_pnl']

        # Optional fields from PositionState
        current_internal_pos['unrealized_pnl'] = state_to_set.get('unrealized_pnl') # Handles if key is missing
        current_internal_pos['market_value'] = state_to_set.get('market_value')

        # Fields PositionManager usually maintains internally, set them based on forced state
        current_internal_pos['last_update'] = time.time()
        current_internal_pos['last_update_timestamp'] = current_internal_pos['last_update']
        current_internal_pos['last_update_source'] = "INTELLISENSE_FORCED_STATE"
        current_internal_pos['last_broker_fingerprint'] = None # Clear fingerprint when forcing state
        current_internal_pos['retry_count'] = 0
        current_internal_pos['cooldown_until'] = 0.0

        # If not open, ensure shares and avg_price are zeroed out for consistency,
        # unless the PositionState explicitly sets shares for a non-open state (unusual).
        if not state_to_set['open']:
            current_internal_pos['total_shares'] = 0.0
            current_internal_pos['total_avg_price'] = 0.0
            # Unrealized PnL and Market Value also become 0 if not open
            current_internal_pos['unrealized_pnl'] = 0.0
            current_internal_pos['market_value'] = 0.0

        self._internal_positions[symbol_upper] = current_internal_pos

        # Also update the _forced_states dict that the overridden get_position uses
        self._forced_states[symbol_upper] = state_to_set.copy()

        logger.info(f"TestablePositionManager: Forced internal state for symbol '{symbol_upper}'.")
        logger.debug(f"New internal state for '{symbol_upper}': {current_internal_pos}")
        return True

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]: # Override base method
        """
        Returns the forced state if active for the symbol, otherwise calls super().get_position().
        """
        symbol_upper = symbol.upper()
        if symbol_upper in self._forced_states:
            logger.debug(f"TestablePositionManager: Returning forced state for '{symbol_upper}'.")
            # Ensure the returned dict matches the structure of PositionManager.get_position() output
            # The PositionState TypedDict should already align with this.
            return self._forced_states[symbol_upper].copy()

        # If no forced state, call the base class's get_position
        if hasattr(super(), 'get_position'):
            return super().get_position(symbol)
        else: # Fallback for placeholder BasePositionManager
            logger.warning(f"TestablePositionManager: Base class has no get_position. Returning None for {symbol_upper}.")
            return None

    def reset_forced_state(self, symbol: str):
        # ... (as from Chunk 15) ...
        symbol_upper = symbol.upper()
        if hasattr(self, '_forced_states') and symbol_upper in self._forced_states:
            del self._forced_states[symbol_upper]
            logger.info(f"TestablePositionManager: Cleared forced state for '{symbol_upper}'.")
            # Optionally, also reset the corresponding entry in _internal_positions or reload from a base.
            # For now, just clearing the forced override.

    def reset_all_forced_states(self):
        # ... (as from Chunk 15) ...
        if hasattr(self, '_forced_states'): self._forced_states.clear()
        logger.info("TestablePositionManager: Cleared all forced states.")
