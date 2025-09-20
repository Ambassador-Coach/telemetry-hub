# modules/order_management/order_linking_service.py
"""
Order Linking Service Implementation

This service manages the bidirectional mapping between internal local IDs,
broker IDs, and ephemeral correlation IDs in a thread-safe manner.
"""

import threading
import logging
from typing import Dict, Optional, TYPE_CHECKING

from interfaces.order_management.services import IOrderLinkingService
from interfaces.order_management.state_service import IOrderStateService

if TYPE_CHECKING:
    from data_models.order_data_types import Order

logger = logging.getLogger(__name__)


class OrderLinkingService(IOrderLinkingService):
    """
    Concrete implementation of IOrderLinkingService.
    
    Manages the mapping between:
    - Local IDs (internal order identifiers)
    - Broker IDs (external broker order identifiers)
    - Ephemeral IDs (temporary correlation IDs from C++)
    
    Thread-safe with comprehensive logging.
    """
    
    def __init__(self, state_service: Optional[IOrderStateService] = None):
        """
        Initialize the linking service.
        
        Args:
            state_service: Optional state service for backward compatibility methods
        """
        # Core state - moved from OrderRepository
        self._lock = threading.RLock()
        self._ls_to_local: Dict[int, int] = {}  # broker_id -> local_id
        self._local_to_ls: Dict[int, int] = {}  # local_id -> broker_id
        self._correlation_to_local: Dict[int, int] = {}  # ephemeral_id -> local_id
        self._ephemeral_ls_ids: Dict[int, int] = {}  # local_id -> ephemeral_broker_id
        
        # For backward compatibility methods that return Order objects
        self._state_service = state_service
        
        logger.info("OrderLinkingService initialized with empty mappings")

    # ===== Core Linking Methods =====
    
    def link_broker_and_local_ids(self, local_id: int, broker_id: int) -> None:
        """
        Creates a bidirectional mapping between a local ID and a broker ID.
        
        Args:
            local_id: Internal order identifier
            broker_id: External broker order identifier
        """
        with self._lock:
            # Remove any existing ephemeral mapping for this local_id
            old_ephemeral = self._ephemeral_ls_ids.pop(local_id, None)
            if old_ephemeral:
                self._ls_to_local.pop(old_ephemeral, None)
                logger.debug(f"Removed old ephemeral ID {old_ephemeral} for local_id {local_id}")
            
            # Create bidirectional mapping
            self._ls_to_local[broker_id] = local_id
            self._local_to_ls[local_id] = broker_id
            
            logger.info(
                f"Linked IDs: Local={local_id} <-> Broker={broker_id} "
                f"(Total linked: {len(self._ls_to_local)})"
            )

    def link_ephemeral_id(self, local_id: int, ephemeral_id: int) -> None:
        """
        Links a local ID to a temporary ephemeral correlation ID.
        
        Args:
            local_id: Internal order identifier
            ephemeral_id: Temporary correlation ID from C++
        """
        with self._lock:
            self._correlation_to_local[ephemeral_id] = local_id
            logger.info(f"Linked Ephemeral ID: {ephemeral_id} => Local={local_id}")

    # ===== Core Query Methods =====
    
    def get_local_id_from_broker_id(self, broker_id: int) -> Optional[int]:
        """
        Finds a local ID given a broker ID.
        
        Args:
            broker_id: External broker order identifier
            
        Returns:
            The local ID if found, None otherwise
        """
        with self._lock:
            return self._ls_to_local.get(broker_id)

    def get_local_id_from_ephemeral_id(self, ephemeral_id: int) -> Optional[int]:
        """
        Finds a local ID given an ephemeral correlation ID.
        
        First checks the direct correlation mapping, then searches
        the ephemeral broker ID mappings.
        
        Args:
            ephemeral_id: Temporary correlation ID
            
        Returns:
            The local ID if found, None otherwise
        """
        with self._lock:
            # First check direct correlation mapping
            local_id = self._correlation_to_local.get(ephemeral_id)
            if local_id is not None:
                return local_id
            
            # Then check ephemeral broker ID mappings
            for lid, eph_broker_id in self._ephemeral_ls_ids.items():
                if eph_broker_id == ephemeral_id:
                    return lid
            
            return None

    # ===== Backward Compatibility Methods =====
    
    def link_ls_order(
        self,
        local_id: int,
        ls_order_id: int,
        is_ephemeral: bool = False,
        perf_timestamps: Optional[Dict[str, float]] = None
    ) -> None:
        """
        DEPRECATED: Legacy method for linking orders.
        
        Args:
            local_id: Internal order identifier
            ls_order_id: Lightspeed order ID
            is_ephemeral: Whether this is a temporary ID
            perf_timestamps: Performance tracking (ignored)
        """
        if is_ephemeral:
            # Store as ephemeral broker ID
            with self._lock:
                self._ephemeral_ls_ids[local_id] = ls_order_id
            logger.info(f"Linked ephemeral broker ID: Local={local_id} -> Ephemeral Broker={ls_order_id}")
        else:
            # Regular broker ID linking
            self.link_broker_and_local_ids(local_id, ls_order_id)

    def get_order_by_broker_id(self, broker_order_id: str) -> Optional["Order"]:
        """
        DEPRECATED: Gets an Order object by broker ID.
        
        This method requires a state service to be injected.
        
        Args:
            broker_order_id: Broker order ID as string
            
        Returns:
            The Order object if found, None otherwise
        """
        if not self._state_service:
            logger.warning("get_order_by_broker_id called but no state service available")
            return None
        
        try:
            broker_id_int = int(broker_order_id)
            local_id = self.get_local_id_from_broker_id(broker_id_int)
            if local_id is not None:
                return self._state_service.get_order(local_id)
        except (ValueError, TypeError):
            logger.debug(f"Invalid broker_order_id format: {broker_order_id}")
        
        return None

    def get_order_by_ephemeral_corr(self, ephemeral_corr: str) -> Optional["Order"]:
        """
        DEPRECATED: Gets an Order object by ephemeral correlation ID.
        
        This method requires a state service to be injected.
        
        Args:
            ephemeral_corr: Ephemeral correlation ID as string
            
        Returns:
            The Order object if found, None otherwise
        """
        if not self._state_service:
            logger.warning("get_order_by_ephemeral_corr called but no state service available")
            return None
        
        try:
            ephemeral_id_int = int(ephemeral_corr)
            local_id = self.get_local_id_from_ephemeral_id(ephemeral_id_int)
            if local_id is not None:
                return self._state_service.get_order(local_id)
        except (ValueError, TypeError):
            logger.debug(f"Invalid ephemeral_corr format: {ephemeral_corr}")
        
        return None

    # ===== Utility Methods =====
    
    def cleanup_ephemeral_mappings(self, local_id: int) -> None:
        """
        Removes all ephemeral mappings for a finalized order.
        
        Args:
            local_id: The order that was finalized
        """
        with self._lock:
            # Remove from correlation mappings
            ephemeral_ids_to_remove = [
                eph_id for eph_id, mapped_local_id in self._correlation_to_local.items()
                if mapped_local_id == local_id
            ]
            for eph_id in ephemeral_ids_to_remove:
                self._correlation_to_local.pop(eph_id, None)
                logger.debug(f"Cleaned up ephemeral correlation {eph_id} for order {local_id}")
            
            # Remove from ephemeral broker ID mappings
            self._ephemeral_ls_ids.pop(local_id, None)

    def get_broker_id_for_local_id(self, local_id: int) -> Optional[int]:
        """
        Gets the broker ID for a given local ID.
        
        Args:
            local_id: Internal order identifier
            
        Returns:
            The broker ID if found, None otherwise
        """
        with self._lock:
            return self._local_to_ls.get(local_id)

    def get_mapping_stats(self) -> Dict[str, int]:
        """
        Returns statistics about current mappings.
        
        Returns:
            Dictionary with mapping counts
        """
        with self._lock:
            return {
                "broker_to_local_mappings": len(self._ls_to_local),
                "local_to_broker_mappings": len(self._local_to_ls),
                "correlation_mappings": len(self._correlation_to_local),
                "ephemeral_broker_mappings": len(self._ephemeral_ls_ids)
            }