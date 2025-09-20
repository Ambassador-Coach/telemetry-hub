# modules/risk_management/interfaces.py
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Callable
from enum import Enum, auto
from dataclasses import dataclass

# Import OrderParameters for assess_order_risk method
from interfaces.trading.services import OrderParameters, IPositionManager

# Define an Enum for clear risk status returns
class RiskLevel(Enum):
    UNKNOWN = auto()
    NORMAL = auto()
    ELEVATED = auto()
    HIGH = auto()
    CRITICAL = auto() # Indicates potential need for immediate action
    HALTED = auto()   # Market or LULD halt detected

@dataclass
class RiskAssessment:
    level: RiskLevel = RiskLevel.UNKNOWN
    reason: str = "No assessment performed"
    # Add any other relevant details, e.g., recommended_action

# Define the callback signature
CriticalRiskCallback = Callable[[str, RiskLevel, Dict[str, Any]], None] # Args: symbol, level, details

# --- Define the primary service interface ---
class IRiskManagementService(ABC):
    """
    Interface for services responsible for assessing market risk,
    detecting patterns (like meltdowns), and potentially providing
    other market analysis (e.g., Time & Sales).
    """

    @abstractmethod
    def assess_holding_risk(self, symbol: str) -> RiskLevel: # Or RiskAssessmentResult
        """
        Assesses the current risk level for holding a position in the symbol.
        Considers meltdown factors, halts, etc.

        Returns:
            RiskLevel: An enum indicating the assessed risk level.
                       (Or return a more detailed dict/object)
        """
        pass

    @abstractmethod
    def get_risk_details(self, symbol: str) -> Dict[str, Any]:
        """
        Returns a dictionary containing more detailed risk factors and metrics
        for the given symbol (e.g., for display or logging).
        """
        pass

    @abstractmethod
    def reset_all(self) -> None:
        """
        Resets all internal risk tracking state.
        Called when the system needs to clear all risk-related state,
        such as during a full system reset.
        """
        pass

    @abstractmethod
    def assess_order_risk(self, order_params: OrderParameters) -> RiskAssessment:
        """Assesses the risk of placing a specific order."""
        pass

    @abstractmethod
    def check_pre_trade_risk(self, symbol: str, side: str, quantity: float, limit_price: Optional[float]) -> bool:
        """
        Checks if a proposed trade meets risk criteria before execution.

        Args:
            symbol: The stock symbol
            side: "BUY" or "SELL"
            quantity: Number of shares
            limit_price: Optional limit price

        Returns:
            bool: True if trade passes risk checks, False otherwise
        """
        pass

    @abstractmethod
    def check_market_conditions(self, symbol: str, gui_logger_func: Optional[Callable[[str, str], None]] = None) -> RiskAssessment:
        """
        Assesses current market conditions (e.g., spread, liquidity, volatility)
        for a symbol to determine if proceeding with trade signal processing
        is advisable *before* calculating specific order parameters.

        Args:
            symbol: The stock symbol to check.
            gui_logger_func: Optional function to log messages to GUI, overrides instance's logger.

        Returns:
            RiskAssessment: An object indicating the risk level (e.g., NORMAL, HALTED,
                           CRITICAL if spread is too wide) and a reason.
        """
        pass

    # Add other methods as needed, e.g.:
    # @abstractmethod
    # def assess_entry_risk(self, symbol: str, side: str, entry_price: float) -> bool:
    #     """Assesses if entering a trade now is advisable based on risk."""
    #     pass

    # @abstractmethod
    # def get_tas_summary(self, symbol: str) -> Dict[str, Any]:
    #     """Returns Time & Sales analysis summary."""
    #     pass

    @abstractmethod
    def start_monitoring_holding(self, symbol: str) -> None:
        """Informs RMS that TMS is now holding this symbol and it should be monitored for critical risk events."""
        pass

    @abstractmethod
    def stop_monitoring_holding(self, symbol: str) -> None:
        """Informs RMS that TMS is no longer holding this symbol for critical risk event monitoring."""
        pass

    @abstractmethod
    def register_critical_risk_callback(self, callback: CriticalRiskCallback) -> None:
        """Registers a callback function to be invoked on critical risk events for monitored holdings."""
        pass

    @abstractmethod
    def unregister_critical_risk_callback(self, callback: CriticalRiskCallback) -> None:
        """Unregisters a previously registered critical risk callback."""
        pass

# --- Add IMarketDataReceiver Interface ---
# RiskService needs to receive market data. It should implement IMarketDataReceiver.
# We re-declare it here or import from price_fetching.interfaces for clarity,
# ensuring RiskService implements these methods.
# Import from the consolidated location
from interfaces.data.services import IMarketDataReceiver