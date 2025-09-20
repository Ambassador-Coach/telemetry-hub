# interfaces/order_management/services.py
"""
Segregated Order Management Service Interfaces

This module defines focused, single-responsibility interfaces for order management,
replacing the monolithic IOrderRepository interface with five cohesive services:
- IOrderStateService: Core order state management
- IOrderEventProcessor: Event-driven order updates
- IOrderLinkingService: ID mapping and correlation
- IOrderQueryService: Read-only order queries
- IStaleOrderService: Stale order detection and cancellation
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from data_models.order_data_types import Order

from interfaces.core.services import ILifecycleService


# ===== 1. CORE STATE SERVICE =====
class IOrderStateService(ABC):
    """
    Core order state management service.
    Responsible for creating, updating, and retrieving order records.
    This is the single source of truth for order state.
    """
    
    @abstractmethod
    def create_copy_order(
        self,
        symbol: str,
        side: str,
        event_type: str,
        requested_shares: float,
        req_time: float,
        parent_trade_id: int,
        requested_lmt_price: float,
        ocr_time: Optional[float] = None,
        comment: str = "",
        requested_cost: float = 0.0,
        *,
        timestamp_ocr_processed: Optional[float] = None,
        real_time_price: Optional[float] = None,
        trader_price: Optional[float] = None,
        ocr_confidence: Optional[float] = None,
        perf_timestamps: Optional[Dict[str, float]] = None,
        **kwargs
    ) -> Optional[int]:
        """
        Creates a new local copy order record and returns the local_id.
        
        This is the primary method for order creation, maintaining all
        the original parameters from the monolithic interface.
        """
        pass
    
    @abstractmethod
    def update_order_status_locally(self, local_id: int, new_status: str, reason: Optional[str] = None) -> bool:
        """
        Updates the status of a local order copy directly.
        Does NOT invoke trade_finalized_callback as this is not a broker-confirmed status.
        
        Returns:
            bool: True if the order was found and updated, False otherwise
        """
        pass
    
    @abstractmethod
    def get_order_by_local_id(self, local_id: int) -> Optional["Order"]:
        """Gets an immutable Order object by its local ID."""
        pass
    
    @abstractmethod
    def get_all_orders(self) -> List["Order"]:
        """Gets all orders."""
        pass
    
    @abstractmethod
    def reprice_buy_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        """
        Reprices a buy order by creating a new order with updated price.
        Returns the local ID of the new order, or None if repricing failed.
        """
        pass
    
    @abstractmethod
    def reprice_sell_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        """
        Reprices a sell order by creating a new order with updated price.
        Returns the local ID of the new order, or None if repricing failed.
        """
        pass
    
    @abstractmethod
    def request_cancellation(self, local_id: int, reason: str = "Cancel Requested") -> Optional[int]:
        """
        Marks a local order as 'CancelRequested' if it's in an active state.
        Returns the associated ls_order_id if found and cancellation is requested.
        """
        pass


# ===== 2. EVENT PROCESSOR SERVICE =====
class IOrderEventProcessor(ILifecycleService):
    """
    Event-driven order update processor.
    Handles broker messages and updates order state accordingly.
    Implements ILifecycleService for proper startup/shutdown.
    """
    
    @abstractmethod
    def process_broker_message(
        self,
        msg: Dict[str, Any],
        perf_timestamps: Optional[Dict[str, float]] = None
    ) -> None:
        """
        Processes a message from the broker, updating order state as needed.
        This is the primary entry point for all broker-originated updates.
        """
        pass
    
    @abstractmethod
    def handle_order_status_update(self, event: "OrderStatusUpdateEvent") -> None:
        """Handles order status updates from the event bus."""
        pass

    @abstractmethod
    def handle_order_fill(self, event: "OrderFilledEvent") -> None:
        """Handles order fill events from the event bus."""
        pass
        
    @abstractmethod
    def handle_order_rejection(self, event: "OrderRejectedEvent") -> None:
        """Handles order rejection events from the event bus."""
        pass
    
    @abstractmethod
    def set_trade_finalized_callback(self, callback_func: Callable[[int, str, str, str, Optional[int]], None]) -> None:
        """
        Sets the callback function to be called when a trade is finalized.
        Callback args: (local_id, symbol, event_type, final_status, parent_trade_id)
        """
        pass


# ===== 3. ID LINKING SERVICE =====
class IOrderLinkingService(ABC):
    """
    Order ID mapping and correlation service.
    Manages the relationships between local IDs, broker IDs, and ephemeral IDs.
    """
    
    @abstractmethod
    def link_broker_and_local_ids(self, local_id: int, broker_id: int) -> None:
        """Creates a bidirectional mapping between a local ID and a final broker ID."""
        pass

    @abstractmethod
    def link_ephemeral_id(self, local_id: int, ephemeral_id: int) -> None:
        """Links a local ID to a temporary, ephemeral broker ID."""
        pass
        
    @abstractmethod
    def get_local_id_from_broker_id(self, broker_id: int) -> Optional[int]:
        """Finds a local ID given a final broker ID."""
        pass
        
    @abstractmethod
    def get_local_id_from_ephemeral_id(self, ephemeral_id: int) -> Optional[int]:
        """Finds a local ID given a temporary, ephemeral broker ID."""
        pass
    
    # Keep the original methods for backward compatibility during migration
    @abstractmethod
    def link_ls_order(
        self,
        local_id: int,
        ls_order_id: int,
        is_ephemeral: bool = False,
        perf_timestamps: Optional[Dict[str, float]] = None
    ) -> None:
        """
        DEPRECATED: Use link_broker_and_local_ids or link_ephemeral_id instead.
        Links a local copy order to a Lightspeed order ID.
        """
        pass
    
    @abstractmethod
    def get_order_by_broker_id(self, broker_order_id: str) -> Optional["Order"]:
        """
        DEPRECATED: Use get_local_id_from_broker_id with IOrderStateService.
        Gets an immutable Order object by its broker ID.
        """
        pass
    
    @abstractmethod
    def get_order_by_ephemeral_corr(self, ephemeral_corr: str) -> Optional["Order"]:
        """
        DEPRECATED: Use get_local_id_from_ephemeral_id with IOrderStateService.
        Gets an immutable Order object by its C++ ephemeral correlation ID.
        """
        pass


# ===== 4. QUERY SERVICE =====
class IOrderQueryService(ABC):
    """
    Read-only order query service.
    Provides various views and aggregations of order data for consumers.
    """
    
    @abstractmethod
    def get_order_by_local_id(self, local_id: int) -> Optional["Order"]:
        """Retrieves a single order by its local ID."""
        pass
        
    @abstractmethod
    def get_all_orders(self) -> List["Order"]:
        """Retrieves a list of all current order objects."""
        pass

    @abstractmethod
    def get_open_orders(self, symbol: Optional[str] = None) -> List["Order"]:
        """Retrieves all non-terminal orders, optionally filtered by symbol."""
        pass
    
    @abstractmethod
    def get_orders_for_trade(self, trade_id: int) -> List[Dict[str, Any]]:
        """Gets all orders associated with a trade."""
        pass
    
    @abstractmethod
    def get_leftover_shares(self, local_id: int) -> float:
        """Gets the number of unfilled shares for an order."""
        pass
    
    @abstractmethod
    def is_order_final(self, local_id: int) -> bool:
        """Checks if an order is in a final state (Filled, Cancelled, or Rejected)."""
        pass
    
    @abstractmethod
    def compute_leftover_shares_for_trade(self, trade_id: int) -> float:
        """Computes the total unfilled shares for a trade."""
        pass
    
    @abstractmethod
    def get_cumulative_filled_for_trade(self, parent_trade_id: int) -> float:
        """Computes the total filled shares for all orders associated with a trade."""
        pass
    
    @abstractmethod
    def get_symbol_total_fills(self, symbol: str) -> float:
        """
        Gets the net filled position for a symbol based on the processed fills cache.
        Returns positive for long, negative for short.
        """
        pass
    
    @abstractmethod
    def get_fill_calculated_position(self, symbol: str) -> float:
        """
        Returns the net position for a symbol calculated purely from processed fills.
        Positive for long, negative for short.
        """
        pass
    
    @abstractmethod
    def get_account_data(self) -> Dict[str, Any]:
        """Gets the current account data."""
        pass
    
    @abstractmethod
    def get_broker_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Gets the broker position for a symbol."""
        pass
    
    @abstractmethod
    def get_all_broker_positions(self) -> Dict[str, Dict[str, Any]]:
        """Gets all broker positions."""
        pass
    
    @abstractmethod
    def export_orders_to_csv(self, filepath: str) -> None:
        """Exports all orders to a CSV file."""
        pass
    
    @abstractmethod
    def export_account_summary_to_csv(self, filepath: str) -> None:
        """Exports account summary data to a CSV file."""
        pass
    
    @abstractmethod
    def get_orders_for_gui_bootstrap(self, filter_date: str = "today") -> List["Order"]:
        """Gets a list of orders formatted for the GUI bootstrap."""
        pass


# ===== 5. STALE ORDER SERVICE =====
class IStaleOrderService(ILifecycleService):
    """
    Stale order detection and cancellation service.
    Runs background checks to identify and cancel orders that have been pending too long.
    Implements ILifecycleService for proper lifecycle management.
    """
    
    @abstractmethod
    def start_auto_cancel_stale_pending(self, interval: float = 5.0, max_age: float = 11.0) -> None:
        """
        Starts automatic cancellation of stale pending orders.
        
        Args:
            interval: Check interval in seconds
            max_age: Maximum age before an order is considered stale
        """
        pass
    
    @abstractmethod
    def stop_auto_cancel_stale_pending(self) -> None:
        """Stops automatic cancellation of stale pending orders."""
        pass


# ===== ORIGINAL MONOLITHIC INTERFACE (DEPRECATED) =====
# This interface is kept for backward compatibility during migration
class IOrderRepository(ABC):
    """
    DEPRECATED: Monolithic order repository interface.
    This interface is being decomposed into the five services above.
    New code should use the segregated interfaces instead.
    
    During migration, OrderRepository will implement this interface
    by delegating to the new services.
    """
    
    # All the original methods from the monolithic interface
    @abstractmethod
    def create_copy_order(
        self,
        symbol: str,
        side: str,
        event_type: str,
        requested_shares: float,
        req_time: float,
        parent_trade_id: int,
        requested_lmt_price: float,
        ocr_time: Optional[float] = None,
        comment: str = "",
        requested_cost: float = 0.0,
        *,
        timestamp_ocr_processed: Optional[float] = None,
        real_time_price: Optional[float] = None,
        trader_price: Optional[float] = None,
        ocr_confidence: Optional[float] = None,
        perf_timestamps: Optional[Dict[str, float]] = None,
        **kwargs
    ) -> Optional[int]:
        pass

    @abstractmethod
    def process_broker_message(
        self,
        msg: Dict[str, Any],
        perf_timestamps: Optional[Dict[str, float]] = None
    ) -> None:
        pass

    @abstractmethod
    def link_ls_order(
        self,
        local_id: int,
        ls_order_id: int,
        is_ephemeral: bool = False,
        perf_timestamps: Optional[Dict[str, float]] = None
    ) -> None:
        pass

    @abstractmethod
    def get_order_by_local_id(self, local_id: int) -> Optional["Order"]:
        pass

    @abstractmethod
    def get_order_by_broker_id(self, broker_order_id: str) -> Optional["Order"]:
        pass

    @abstractmethod
    def get_order_by_ephemeral_corr(self, ephemeral_corr: str) -> Optional["Order"]:
        pass

    @abstractmethod
    def get_orders_for_trade(self, trade_id: int) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_orders(self) -> List["Order"]:
        pass

    @abstractmethod
    def get_leftover_shares(self, local_id: int) -> float:
        pass

    @abstractmethod
    def is_order_final(self, local_id: int) -> bool:
        pass

    @abstractmethod
    def compute_leftover_shares_for_trade(self, trade_id: int) -> float:
        pass

    @abstractmethod
    def get_cumulative_filled_for_trade(self, parent_trade_id: int) -> float:
        pass

    @abstractmethod
    def get_symbol_total_fills(self, symbol: str) -> float:
        pass

    @abstractmethod
    def get_fill_calculated_position(self, symbol: str) -> float:
        pass

    @abstractmethod
    def get_account_data(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_broker_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    def get_all_broker_positions(self) -> Dict[str, Dict[str, Any]]:
        pass

    @abstractmethod
    def set_trade_finalized_callback(self, callback_func: Callable[[int, str, str, str, Optional[int]], None]) -> None:
        pass

    @abstractmethod
    def export_orders_to_csv(self, filepath: str) -> None:
        pass

    @abstractmethod
    def export_account_summary_to_csv(self, filepath: str) -> None:
        pass

    @abstractmethod
    def reprice_buy_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        pass

    @abstractmethod
    def reprice_sell_order(self, symbol: str, original_local_id: int, new_limit_price: float) -> Optional[int]:
        pass

    @abstractmethod
    def request_cancellation(self, local_id: int, reason: str = "Cancel Requested") -> Optional[int]:
        pass

    @abstractmethod
    def update_order_status_locally(self, local_id: int, new_status: str, reason: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    def start_auto_cancel_stale_pending(self, interval: float = 5.0, max_age: float = 11.0) -> None:
        pass

    @abstractmethod
    def stop_auto_cancel_stale_pending(self) -> None:
        pass