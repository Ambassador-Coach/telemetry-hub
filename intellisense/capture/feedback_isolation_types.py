from typing import TypedDict, Optional # Ensure Optional is imported if any fields become optional later

class PositionMockProfile(TypedDict):
    """Mocks OCR'd position window data per symbol, as processed by PythonOCRDataConditioner."""
    strategy_hint: str      # e.g., "Long", "Closed", "SCALP_STRAT_NAME"
    cost_basis: float       # OCR'd average cost per share for the current position display
    pnl_per_share: float    # OCR'd unrealized P/L per share
    realized_pnl: float     # OCR'd total realized P/L for the symbol/session
    unrealized_pnl: float   # OCR'd total unrealized P/L for the current position
    total_shares: float     # OCR'd current position size
    current_value: float    # OCR'd current market value of the position

class PositionState(TypedDict):
    """Represents internal position manager state to be set for testing."""
    symbol: str
    open: bool                      # Is the position considered open?
    strategy: str                   # Current strategy state string (e.g., "long", "closed", "opening")
    total_shares: float             # Net shares held
    total_avg_price: float          # Weighted average entry price for current shares
    overall_realized_pnl: float     # Cumulative realized P&L for the symbol
    # The following might be derived or also settable if PM tracks them based on external prices
    unrealized_pnl: Optional[float]   # Current unrealized P&L (optional, as it depends on live market price)
    market_value: Optional[float]     # Current position market value (optional)
