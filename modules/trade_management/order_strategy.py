# modules/trade_management/order_strategy.py
import logging
import math
from typing import Dict, Any, Optional, Union

# Interfaces and Data Classes
from interfaces.trading.services import (
    IOrderStrategy,
    IPositionManager,
    TradeSignal,
    OrderParameters
)
from data_models import TradeActionType
# Dependencies
from interfaces.data.services import IPriceProvider # Assuming correct path
from utils.global_config import GlobalConfig as IConfigService # Assuming correct path
from interfaces.logging.services import ICorrelationLogger
import re # For parsing dollar values (moved from SnapshotInterpreter as it's used here now)

logger = logging.getLogger(__name__)

class OrderStrategy(IOrderStrategy):
    """
    Calculates specific order parameters based on a trade signal, configuration,
    current position state, and market prices.
    """
    def __init__(self,
                 config_service: IConfigService,
                 price_provider: IPriceProvider,
                 position_manager: IPositionManager,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initializes the OrderStrategy with injected dependencies.

        Args:
            config_service: Configuration service for strategy parameters
            price_provider: Price provider for market data
            position_manager: Position manager for current position state
            correlation_logger: Correlation logger for publishing strategy decisions
        """
        self.config_service = config_service
        self.price_provider = price_provider
        self.position_manager = position_manager
        self.correlation_logger = correlation_logger
        logger.info("OrderStrategy initialized with correlation logger for decision tracking.")

    # --- Helper for parsing ---
    def _parse_dollar_value(self, value_str) -> float:
        """Strips commas/spaces/$, converts to float, rounds to two decimals."""
        if isinstance(value_str, (float, int)):
            return round(float(value_str), 2)
        if isinstance(value_str, str):
            clean = re.sub(r'[,\s$]', '', value_str)
            if not clean: return 0.0
            try:
                 return round(float(clean), 2)
            except ValueError:
                 logger.error(f"Could not parse dollar value: '{value_str}' -> '{clean}'")
                 raise ValueError(f"Invalid dollar value format: {value_str}")
        if value_str is None:
            return 0.0
        logger.error(f"Could not parse value of type {type(value_str)}: {value_str}")
        raise ValueError(f"Unsupported type for dollar value parsing: {type(value_str)}")

    def calculate_order_params(self,
                               signal: TradeSignal,
                               position_manager: IPositionManager,
                               price_provider: IPriceProvider,
                               config_service: IConfigService
                              ) -> Optional[OrderParameters]:
        """
        Determines quantity, order type, price based on signal and context.

        Returns:
            OrderParameters if an order should be placed, None otherwise.
        """
        symbol = signal.symbol
        action = signal.action
        logger.info(f"[{symbol}] Calculating order parameters for action: {action.name}")

        # Fetch current position state needed for calculations
        pos_data = position_manager.get_position(symbol)
        # Use default values if position doesn't exist yet (e.g., for OPEN)
        current_shares = pos_data.get("shares", 0.0) if pos_data else 0.0
        avg_price = pos_data.get("avg_price", 0.0) if pos_data else 0.0
        # Note: realised_pnl, last_fingerprint etc. are not typically needed for strategy calc

        # --- OPEN Action ---
        if action == TradeActionType.OPEN_LONG:
            return self._calculate_open_params(symbol, config_service, price_provider, action)

        # --- ADD Action ---
        elif action == TradeActionType.ADD_LONG:
             # Check if we actually have shares to add to
             logger.info(f"[{symbol}] DIAGNOSTIC: Before ADD check - position_manager.get_position() returned: {pos_data}")
             logger.info(f"[{symbol}] DIAGNOSTIC: Before ADD check - current_shares={current_shares}, pos_data.get('total_shares', 0.0)={pos_data.get('total_shares', 0.0) if pos_data else 0.0}")

             if current_shares < 1e-9:
                 logger.warning(f"[{symbol}] ADD signal received, but no current shares found in PositionManager. Skipping.")
                 return None
             return self._calculate_add_params(symbol, signal, current_shares, avg_price, config_service, price_provider, action)

        # --- REDUCE Action ---
        elif action == TradeActionType.REDUCE_PARTIAL:
             # Check if we actually have shares to reduce
             if current_shares < 1e-9:
                 logger.warning(f"[{symbol}] REDUCE signal received, but no current shares found in PositionManager. Skipping.")
                 return None
             return self._calculate_reduce_params(symbol, current_shares, config_service, price_provider, action)

        # --- CLOSE Action ---
        elif action == TradeActionType.CLOSE_POSITION:
             # Check if we actually have shares to close
             if current_shares < 1e-9:
                 logger.warning(f"[{symbol}] CLOSE signal received, but no current shares found in PositionManager. Skipping.")
                 return None

             # Check if this is an aggressive chase liquidation
             aggressive_chase = False
             if signal.triggering_snapshot and signal.triggering_snapshot.get("aggressive_chase", False):
                 aggressive_chase = True
                 logger.info(f"[{symbol}] Detected aggressive_chase flag in CLOSE signal")

             return self._calculate_close_params(symbol, current_shares, price_provider, aggressive_chase, config_service, action)

        else:
            logger.warning(f"[{symbol}] Unknown or unsupported signal action: {action.name}")
            return None

    # --- Private Helper Methods for Calculations ---

    def _calculate_open_params(self, symbol: str, config: IConfigService, price_provider: IPriceProvider, action: TradeActionType) -> Optional[OrderParameters]:
        """Calculates parameters for an OPEN order."""
        shares_to_open = float(config.initial_share_size)
        if shares_to_open < 1.0:
            logger.error(f"[{symbol}] Cannot OPEN: Invalid initial_share_size ({shares_to_open}) in config.")
            return None

        # Get reference price using the new reliable price method
        ref_price = price_provider.get_reliable_price(symbol, side="BUY")
        if ref_price <= 0.01:
             logger.error(f"[{symbol}] Cannot OPEN: Failed to get valid reference price.")
             return None

        # Add offset for LMT order
        limit_price = self._calculate_limit_price(ref_price, "BUY")
        if limit_price is None:
            logger.error(f"[{symbol}] Cannot OPEN: Failed to calculate limit price from ref {ref_price:.4f}.")
            return None

        logger.info(f"[{symbol}] OPEN Params: BUY {shares_to_open:.0f} @ LMT {limit_price:.4f} (Ref: {ref_price:.4f})")
        return OrderParameters(
            symbol=symbol,
            side="BUY",
            quantity=shares_to_open,
            order_type="LMT",
            limit_price=limit_price,
            event_type="OPEN", # Add event type for clarity downstream
            action_type=action # Use the action from the signal
        )

    def _calculate_add_params(self, symbol: str, signal: TradeSignal, current_shares: float, current_avg_price: float, config: IConfigService, price_provider: IPriceProvider, action: TradeActionType) -> Optional[OrderParameters]:
        """Calculates parameters for an ADD order."""
        add_type = config.add_type
        shares_to_add = 0.0

        # Get reference price using the new reliable price method
        ref_price = price_provider.get_reliable_price(symbol, side="BUY")
        if ref_price <= 0.01:
             logger.error(f"[{symbol}] Cannot ADD: Failed to get valid reference price.")
             return None

        # NOTE: CostBasis ADD quantity calculation has been moved to OCRScalpingSignalGenerator
        # for OCR-driven signals. OrderStrategy now only handles "Equal" type for manual orders.

        if add_type == "Equal":
            # For "Equal" add_type (OCR-driven), use the quantity from config.manual_shares
            shares_to_add = float(config.manual_shares)
            logger.info(f"[{symbol}] ADD type 'Equal': Using config.manual_shares = {shares_to_add:.0f}")
            if shares_to_add <= 0:
                logger.error(f"[{symbol}] Cannot ADD (Equal): config.manual_shares ({shares_to_add}) is not positive. Skipping.")
                return None

        # "Manual" block can be removed if "Equal" now covers its intent for OCR
        # elif add_type == "Manual":
        #     shares_to_add = float(config.manual_shares) # This is now handled by "Equal"

        else: # For any other unknown add_type
            logger.error(f"[{symbol}] Unknown add_type '{add_type}'. Skipping ADD.")
            return None

        shares_added_rounded = float(round(shares_to_add))
        if shares_added_rounded < 1.0:
             logger.warning(f"[{symbol}] Calculated shares to ADD < 1 ({shares_to_add:.2f}). Skipping.")
             return None

        limit_price = self._calculate_limit_price(ref_price, "BUY")
        if limit_price is None:
            logger.error(f"[{symbol}] Cannot ADD: Failed to calculate limit price from ref {ref_price:.4f}.")
            return None

        logger.info(f"[{symbol}] ADD Params: BUY {shares_added_rounded:.0f} @ LMT {limit_price:.4f} (Ref: {ref_price:.4f}, OCR Add Type: {add_type})")
        return OrderParameters(
            symbol=symbol,
            side="BUY",
            quantity=shares_added_rounded,
            order_type="LMT",
            limit_price=limit_price,
            event_type="ADD",
            action_type=action # Use the action from the signal
        )

    def _calculate_reduce_params(self, symbol: str, current_shares: float, config: IConfigService, price_provider: IPriceProvider, action: TradeActionType) -> Optional[OrderParameters]:
         """Calculates parameters for a REDUCE order."""
         reduce_percentage = config.reduce_percentage
         shares_to_sell = float(round(current_shares * (reduce_percentage / 100.0)))

         if shares_to_sell < 1.0:
             logger.warning(f"[{symbol}] Calculated shares to REDUCE < 1 ({shares_to_sell:.0f}). Selling 1 share.")
             shares_to_sell = 1.0 # Sell at least 1 share? Or skip? Let's sell 1 for now.

         # Ensure we don't sell more than we have (can happen with rounding)
         shares_to_sell = min(shares_to_sell, current_shares)

         # Get reference price using the new reliable price method
         ref_price = price_provider.get_reliable_price(symbol, side="SELL")
         if ref_price <= 0.01:
              logger.error(f"[{symbol}] Cannot REDUCE: Failed to get valid reference price.")
              return None

         limit_price = self._calculate_limit_price(ref_price, "SELL")
         if limit_price is None:
            logger.error(f"[{symbol}] Cannot REDUCE: Failed to calculate limit price from ref {ref_price:.4f}.")
            return None

         logger.info(f"[{symbol}] REDUCE Params: SELL {shares_to_sell:.0f} @ LMT {limit_price:.4f} (Ref: {ref_price:.4f}, Pct: {reduce_percentage}%)")
         return OrderParameters(
             symbol=symbol,
             side="SELL",
             quantity=shares_to_sell,
             order_type="LMT",
             limit_price=limit_price,
             event_type="REDUCE", # Distinguish partial reduce from full close
             action_type=action # Use the action from the signal
         )

    def _get_tick_size(self, symbol: str, current_price: Optional[float]) -> float:
        """
        Determines the tick size for a given symbol and price.

        Implements standard US market tick size rules:
        - Price < $1.00: $0.0001 tick size
        - Price >= $1.00: $0.01 tick size

        Args:
            symbol: Trading symbol (for future symbol-specific rules)
            current_price: Current price of the symbol

        Returns:
            Appropriate tick size for the price level
        """
        if current_price is None or current_price <= 0:
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

    def _calculate_close_params(self, symbol: str, current_shares: float,
                                price_provider: IPriceProvider,
                                aggressive_chase: bool = False, # This flag comes from TMS/Signal
                                config_service: Optional[IConfigService] = None, # Made Optional for safety
                                action: TradeActionType = None # Added action parameter
                               ) -> Optional[OrderParameters]:
        """
        Calculates parameters for a CLOSE order.

        Args:
            symbol: The symbol to close
            current_shares: Current shares held
            price_provider: Price provider for getting reference prices
            aggressive_chase: If True, use more aggressive pricing strategy for critical risk events
            config_service: Optional configuration service for chase parameters
            action: The TradeActionType from the signal
        """
        shares_to_sell = current_shares # Sell all
        if shares_to_sell < 1e-9: # Use a small epsilon
             logger.error(f"[{symbol}] Cannot CLOSE: Current shares ({shares_to_sell:.2f}) too small.")
             return None

        # Get reference price using the new reliable price method
        ref_price = price_provider.get_reliable_price(symbol, side="SELL", allow_api_fallback=True) # Allow fallback for critical exit
        if ref_price <= 0.01:
             logger.error(f"[{symbol}] Cannot CLOSE: Failed to get valid reference price (got {ref_price}). Attempting to use last known price.")
             # Fallback to a last known price from position data if possible or a very low price.
             # This is critical, we need *some* price for a limit if not MKT.
             # For now, if ref_price is bad, might indicate market is un-tradeable or data issue.
             # Using MKT if aggressive_chase might be better here.
             if not aggressive_chase: # If not aggressive and no ref_price, safer to not place LMT
                 return None
             # Else, aggressive_chase will likely lead to MKT below.

        order_type = "LMT"
        limit_price = None
        event_type = "CLOSE" # Default event type

        if aggressive_chase:
            event_type = "LIQUIDATE_AGGRESSIVE_CHASE" # Specific event type for executor
            # Get chase parameters from global config (if not passed via signal)
            # For now, assume OrderStrategy provides initial aggressive placement
            use_mkt_for_chase = getattr(config_service, 'meltdown_chase_use_initial_mkt', False) if config_service else False
            is_regular_hours = self._is_regular_trading_hours() # Use the helper method

            if use_mkt_for_chase and is_regular_hours:
                order_type = "MKT"
                logger.info(f"[{symbol}] Aggressive Chase CLOSE: Using MARKET order.")
            else:
                # Calculate a very aggressive limit price for the first attempt
                # e.g., current bid or bid - X ticks
                current_bid = price_provider.get_bid_price(symbol, allow_api_fallback=True) # Allow API for this critical step
                if current_bid and current_bid > 0.01:
                    tick_size = self._get_tick_size(symbol, current_bid)
                    initial_aggression_ticks = getattr(config_service, 'meltdown_chase_initial_aggression_ticks', 1) if config_service else 1
                    limit_price = round(current_bid - (initial_aggression_ticks * tick_size), 2)
                    # Handle the case where limit_price might be a MagicMock in tests
                    if isinstance(limit_price, (int, float)):
                        limit_price = max(0.01, limit_price)
                    # Handle the case where limit_price might be a MagicMock in tests
                    limit_price_str = f"{limit_price:.4f}" if isinstance(limit_price, (int, float)) else str(limit_price)
                    current_bid_str = f"{current_bid:.4f}" if isinstance(current_bid, (int, float)) else str(current_bid)
                    logger.info(f"[{symbol}] Aggressive Chase CLOSE: Initial aggressive LMT {limit_price_str} (Bid: {current_bid_str})")
                elif ref_price > 0.01: # Fallback to ref_price if bid is bad
                    limit_price = self._calculate_aggressive_limit_price(ref_price, "SELL") # Use existing helper
                    logger.info(f"[{symbol}] Aggressive Chase CLOSE: Fallback aggressive LMT {limit_price:.4f} (Ref: {ref_price:.4f})")
                else: # No good price for aggressive LMT, may need MKT or fail
                    logger.error(f"[{symbol}] Aggressive Chase CLOSE: Cannot determine aggressive LMT price. Consider MKT if configured.")
                    if not (use_mkt_for_chase and is_regular_hours):
                        return None # Fail if cannot set aggressive LMT and MKT not allowed/viable
                    order_type = "MKT" # Force MKT if all else fails and config allows
        else: # Standard non-aggressive close
            limit_price = self._calculate_limit_price(ref_price, "SELL")

        # Handle the case where limit_price might be a MagicMock in tests
        is_invalid_limit = limit_price is None
        if isinstance(limit_price, (int, float)):
            is_invalid_limit = is_invalid_limit or limit_price <= 0.0

        if order_type == "LMT" and is_invalid_limit:
           # Handle the case where ref_price might be a MagicMock in tests
           ref_price_str = f"{ref_price:.4f}" if isinstance(ref_price, (int, float)) else str(ref_price)
           logger.error(f"[{symbol}] Cannot CLOSE ({event_type}): Failed to calculate valid LMT price (Ref: {ref_price_str}, Calc LMT: {limit_price}).")
           return None

        # Handle the case where shares_to_sell and ref_price might be MagicMock objects in tests
        shares_str = f"{shares_to_sell:.0f}" if isinstance(shares_to_sell, (int, float)) else str(shares_to_sell)
        ref_price_str = f"{ref_price:.4f}" if isinstance(ref_price, (int, float)) else str(ref_price)
        logger.info(f"[{symbol}] {event_type} Params: SELL {shares_str} @ {order_type} {limit_price if order_type == 'LMT' else ''} (Ref: {ref_price_str})")

        # Use the provided action or default to CLOSE_POSITION if none provided
        actual_action = action if action is not None else TradeActionType.CLOSE_POSITION

        return OrderParameters(
            symbol=symbol,
            side="SELL",
            quantity=shares_to_sell,
            order_type=order_type,
            limit_price=limit_price,
            event_type=event_type,
            action_type=actual_action # Use the action from the signal
        )

    def _calculate_limit_price(self, ref_price: float, side: str) -> Optional[float]:
        """Calculates a limit price with an offset."""
        if ref_price <= 0.01:
            return None
        try:
            # Use config for offset? Defaulting for now.
            offset = 0.15 if ref_price >= 2.0 else 0.05 # Example offset
            limit_price = 0.0
            if side == "BUY":
                limit_price = ref_price + offset
            elif side == "SELL":
                limit_price = ref_price - offset
                # Ensure sell limit isn't zero or negative
                if limit_price <= 0.01:
                    limit_price = 0.01 # Minimum price
            else:
                return None # Invalid side

            return round(limit_price, 4) # Round to typical price precision
        except Exception as e:
            logger.error(f"Error calculating limit price from ref {ref_price}: {e}")
            return None

    def _calculate_aggressive_limit_price(self, ref_price: float, side: str) -> Optional[float]:
        """
        Calculates a more aggressive limit price with a smaller offset for emergency liquidations.

        Args:
            ref_price: Reference price to base the limit on
            side: "BUY" or "SELL"

        Returns:
            A more aggressive limit price (closer to the reference price)
        """
        if ref_price <= 0.01:
            return None
        try:
            # Use a much smaller offset for aggressive liquidations
            # This increases the chance of immediate execution
            offset = 0.03 if ref_price >= 2.0 else 0.01  # Much smaller offset than standard

            limit_price = 0.0
            if side == "BUY":
                limit_price = ref_price + offset
            elif side == "SELL":
                limit_price = ref_price - offset
                # Ensure sell limit isn't zero or negative
                if limit_price <= 0.01:
                    limit_price = 0.01  # Minimum price
            else:
                return None  # Invalid side

            return round(limit_price, 4)  # Round to typical price precision
        except Exception as e:
            logger.error(f"Error calculating aggressive limit price from ref {ref_price}: {e}")
            return None
