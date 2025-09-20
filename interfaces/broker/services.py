# interfaces/broker/services.py
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Union, Tuple
from interfaces.core.services import ILifecycleService

# Consider defining standardized data classes/TypedDicts for return types
# Example:
# from typing import TypedDict
# class PositionData(TypedDict):
#     symbol: str
#     shares: float
#     avg_price: float
#     # ... other common fields ...
# class AccountData(TypedDict):
#     equity: float
#     buying_power: float
#     # ... other common fields ...

class IBrokerService(ILifecycleService):
    """
    Interface defining the essential, generic operations required
    to interact with a trading broker's execution platform.
    Implementations will handle broker-specific protocols and APIs.
    
    Inherits from ILifecycleService to provide managed lifecycle (start, stop, is_ready).
    """

    @abstractmethod
    def connect(self) -> None:
        """Establishes and verifies the connection to the broker."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Closes the connection to the broker."""
        pass

    @abstractmethod
    def start_listeners(self) -> None:
        """Starts any necessary background listeners for broker messages."""
        # Separated from connect to allow flexible initialization order
        pass

    @abstractmethod
    def stop_listeners(self) -> None:
        """Stops background listeners."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Returns True if the connection is active and authenticated."""
        pass

    @abstractmethod
    def submit_order(self, order_params: Dict[str, Any]) -> None:
        """
        Submits an order. This is a fire-and-forget operation.
        This is the primary method for submitting orders from higher-level services.
        Implementations will delegate this to their specific place_order method.
        Results are communicated via events on the EventBus.
        """
        pass

    @abstractmethod
    def place_order(self,
                    symbol: str,
                    side: str, # e.g., "BUY", "SELL"
                    quantity: float, # Always positive
                    order_type: str, # e.g., "LMT", "MKT", "STP", "STP LMT"
                    limit_price: Optional[float] = None, # Required for LMT, STP LMT
                    stop_price: Optional[float] = None, # Required for STP, STP LMT
                    time_in_force: str = "DAY", # e.g., "DAY", "GTC"
                    local_order_id: Optional[int] = None, # Link back to OrderRepository ID
                    correlation_id: Optional[str] = None, # Master correlation ID for end-to-end tracing
                    # Add other common parameters if needed: outside_rth, etc.
                    **kwargs # Allow broker-specific extra params
                   ) -> Optional[str]: # Return broker's order ID if available immediately, else None
        """
        Places an order with the broker.
        Translates generic parameters into the broker's specific format.
        Returns the broker's native order ID string/int if known immediately, otherwise None.
        """
        pass

    @abstractmethod
    def cancel_order(self, broker_order_id: Union[str, int]) -> bool:
        """
        Requests cancellation of an order identified by the broker's native ID.
        Returns True if the cancel request was sent successfully, False otherwise.
        """
        pass

    # Optional: Methods to request data explicitly. The primary flow might be
    # the broker pushing updates processed by OrderRepository, but explicit
    # requests can be useful for synchronization or initial state loading.
    # @abstractmethod
    # def request_account_summary(self) -> None:
    #     """Requests the broker to send an account summary update."""
    #     pass

    # @abstractmethod
    # def request_open_positions(self) -> None:
    #     """Requests the broker to send an update for all open positions."""
    #     pass

    # Optional: Direct getters - less preferred if OrderRepository holds state
    # @abstractmethod
    # def get_account_summary(self) -> Optional[AccountData]: # Use TypedDict/DataClass
    #     pass

    @abstractmethod
    def get_all_positions(self, timeout: float = 5.0, perf_timestamps: Optional[Dict[str, float]] = None) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        """
        Retrieves all current positions directly from the broker.

        Args:
            timeout (float): Maximum time to wait for a response if applicable.
            perf_timestamps (Optional[Dict[str, float]]): For performance tracking.

        Returns:
            Tuple[Optional[List[Dict[str, Any]]], bool]: A tuple containing:
                - The list of positions if successful, None if there was any failure
                - A boolean indicating success (True) or failure (False)
                Each position is a dictionary (e.g., {'symbol': 'AAPL', 'shares': 100.0, 'avg_price': 150.25, ...})
        """
        pass
