# interfaces/order_management/state_service.py
"""
Core Order State Service Interface

This interface defines the contract for the single source of truth for order state.
All order state mutations and queries go through this service.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from data_models.order_data_types import Order


class IOrderStateService(ABC):
    """
    Interface for the core service that manages the in-memory state of all orders.
    This is the single source of truth for Order objects.
    
    Thread-safety: All implementations must be thread-safe.
    Immutability: All Order objects returned are immutable.
    """

    @abstractmethod
    def create_order(self, initial_order_data: Dict[str, Any]) -> Optional["Order"]:
        """
        Creates a new Order object from initial data, assigns a local_id,
        and stores it. Returns the newly created immutable Order object.
        
        Args:
            initial_order_data: Dictionary containing all order fields except local_id
            
        Returns:
            The newly created Order object, or None if creation failed
        """
        pass

    @abstractmethod
    def update_order(self, local_id: int, updates: Dict[str, Any]) -> Optional["Order"]:
        """
        Applies a set of updates to an existing order, creating a new version
        of the immutable Order object and replacing the old one. Returns the
        new Order object.
        
        Args:
            local_id: The local order ID to update
            updates: Dictionary of fields to update
            
        Returns:
            The updated Order object, or None if update failed
        """
        pass
        
    @abstractmethod
    def get_order(self, local_id: int) -> Optional["Order"]:
        """
        Retrieves a single immutable Order object by its local ID.
        
        Args:
            local_id: The local order ID
            
        Returns:
            The Order object, or None if not found
        """
        pass

    @abstractmethod
    def get_all_orders_snapshot(self) -> List["Order"]:
        """
        Retrieves a list of all current Order objects.
        
        Returns:
            List of all Order objects (may be empty)
        """
        pass