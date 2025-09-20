# modules/order_management/order_state_service.py
"""
Order State Service Implementation

This service is the single source of truth for all order state in the system.
It manages the core dictionary of Order objects in a thread-safe manner.
"""

import threading
import logging
import time
from typing import Dict, Any, List, Optional

from interfaces.order_management.state_service import IOrderStateService
from data_models.order_data_types import Order, StatusHistoryEntry
from data_models import OrderStatus, OrderLSAgentStatus

logger = logging.getLogger(__name__)


class OrderStateService(IOrderStateService):
    """
    Concrete implementation of IOrderStateService.
    
    Thread-safe management of Order objects with:
    - Atomic ID generation
    - Thread-safe state updates
    - Immutable Order objects
    - Comprehensive logging
    """
    
    def __init__(self):
        """Initialize the order state service with empty state."""
        # Core state variables moved from OrderRepository
        self._lock = threading.RLock()  # Reentrant lock for nested calls
        self._copy_orders: Dict[int, Order] = {}
        self._next_local_id: int = 1
        self._id_lock = threading.Lock()  # Separate lock for ID generation
        
        logger.info("OrderStateService initialized with empty state")

    def _get_next_local_id(self) -> int:
        """
        Generate the next unique local order ID in a thread-safe manner.
        
        Returns:
            The next available local ID
        """
        with self._id_lock:
            new_id = self._next_local_id
            self._next_local_id += 1
            return new_id

    def create_order(self, initial_order_data: Dict[str, Any]) -> Optional[Order]:
        """
        Creates a new Order object from initial data.
        
        This method:
        1. Generates a unique local_id
        2. Creates an immutable Order object
        3. Stores it in the internal dictionary
        
        Args:
            initial_order_data: All order fields except local_id
            
        Returns:
            The newly created Order object, or None if creation failed
        """
        try:
            # Generate ID outside the main lock to reduce contention
            new_local_id = self._get_next_local_id()
            
            with self._lock:
                # Add required fields if not present
                if 'ls_status' not in initial_order_data:
                    initial_order_data['ls_status'] = OrderLSAgentStatus.PENDING
                
                if 'status_history' not in initial_order_data:
                    initial_order_data['status_history'] = [
                        StatusHistoryEntry(
                            status=OrderLSAgentStatus.PENDING,
                            generic_status=OrderStatus.PENDING_SUBMISSION,
                            timestamp=time.time(),
                            reason="Order created"
                        )
                    ]
                
                if 'version' not in initial_order_data:
                    initial_order_data['version'] = 1
                
                # Create the immutable Order object
                order_to_store = Order(
                    local_id=new_local_id,
                    **initial_order_data
                )
                
                # Store in our dictionary
                self._copy_orders[new_local_id] = order_to_store
                
                logger.info(
                    f"Created new Order: local_id={new_local_id}, "
                    f"symbol={initial_order_data.get('symbol')}, "
                    f"side={initial_order_data.get('side')}, "
                    f"event_type={initial_order_data.get('event_type')}, "
                    f"shares={initial_order_data.get('requested_shares')}"
                )
                
                return order_to_store
                
        except Exception as e:
            logger.error(f"Error in OrderStateService.create_order: {e}", exc_info=True)
            return None

    def update_order(self, local_id: int, updates: Dict[str, Any]) -> Optional[Order]:
        """
        Updates an existing order by creating a new immutable version.
        
        Uses the Order.with_update() method to maintain immutability.
        
        Args:
            local_id: The order to update
            updates: Fields to update
            
        Returns:
            The updated Order object, or None if update failed
        """
        with self._lock:
            current_order = self._copy_orders.get(local_id)
            if not current_order:
                logger.warning(f"Update failed: Order with local_id={local_id} not found")
                return None
            
            try:
                # Use the immutable update pattern
                updated_order = current_order.with_update(**updates)
                
                # Replace the old order with the new version
                self._copy_orders[local_id] = updated_order
                
                logger.info(
                    f"Updated Order {local_id}: version {current_order.version} -> {updated_order.version}, "
                    f"updates={list(updates.keys())}"
                )
                
                return updated_order
                
            except Exception as e:
                logger.error(
                    f"Error updating Order {local_id}: {e}", 
                    exc_info=True
                )
                return None

    def get_order(self, local_id: int) -> Optional[Order]:
        """
        Retrieves a single Order object by its local ID.
        
        Args:
            local_id: The order ID to retrieve
            
        Returns:
            The Order object, or None if not found
        """
        with self._lock:
            return self._copy_orders.get(local_id)

    def get_all_orders_snapshot(self) -> List[Order]:
        """
        Retrieves a snapshot of all current Order objects.
        
        Returns:
            List of all Order objects (immutable, so safe to return)
        """
        with self._lock:
            # Return a list copy of the immutable order objects
            return list(self._copy_orders.values())
    
    # Debug/monitoring methods
    def get_order_count(self) -> int:
        """Get the total number of orders in the system."""
        with self._lock:
            return len(self._copy_orders)
    
    def get_next_local_id_preview(self) -> int:
        """Preview the next local ID without incrementing."""
        with self._id_lock:
            return self._next_local_id
    
    def update_order_status_locally(self, local_id: int, new_status: str, reason: Optional[str] = None) -> bool:
        """
        Updates the status of a local order copy directly.
        Does NOT invoke trade_finalized_callback as this is not a broker-confirmed status.
        
        This method updates the ls_status field, which automatically derives the generic status
        via the Order.__post_init__ method when with_update() is called.
        
        Args:
            local_id: The order's local ID
            new_status: The new status (should be OrderLSAgentStatus string value)
            reason: Optional reason for the status change
            
        Returns:
            bool: True if the order was found and updated, False otherwise
        """
        try:
            # Convert string to OrderLSAgentStatus enum
            if isinstance(new_status, str):
                ls_status = OrderLSAgentStatus.from_str(new_status)
            else:
                ls_status = new_status
            
            # Create status history entry
            status_entry = StatusHistoryEntry(
                status=ls_status,
                generic_status=OrderStatus.UNKNOWN,  # Will be corrected when order is updated
                timestamp=time.time(),
                reason=reason or f"Status updated to {ls_status.value}"
            )
            
            # Update the order - this will trigger __post_init__ and re-derive the generic status
            updates = {
                'ls_status': ls_status,
                'status_history': tuple(list(self.get_order(local_id).status_history) + [status_entry])
                                        if self.get_order(local_id) else (status_entry,)
            }
            
            updated_order = self.update_order(local_id, updates)
            
            if updated_order:
                logger.info(f"Updated order {local_id} status to {ls_status.value} -> {updated_order.status.value}")
                return True
            else:
                logger.warning(f"Failed to update order {local_id} status: order not found")
                return False
                
        except Exception as e:
            logger.error(f"Error updating order {local_id} status: {e}", exc_info=True)
            return False