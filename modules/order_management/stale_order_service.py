# modules/order_management/stale_order_service.py
"""
Stale Order Service Implementation

This service manages the detection and cancellation of stale pending orders.
It runs a background thread that periodically checks for orders that have been
pending too long and cancels them automatically.
"""

import logging
import threading
import time
from typing import Optional, List

from interfaces.order_management.services import IStaleOrderService
from interfaces.order_management.state_service import IOrderStateService
from interfaces.order_management.services import IOrderQueryService
from interfaces.broker.services import IBrokerService
from interfaces.logging.services import ICorrelationLogger
from data_models import OrderStatus

logger = logging.getLogger(__name__)


class StaleOrderService(IStaleOrderService):
    """
    Concrete implementation of IStaleOrderService.
    
    Monitors pending orders and automatically cancels those that exceed
    the configured age threshold. This helps prevent orders from being
    stuck in pending state indefinitely.
    """
    
    def __init__(self,
                 state_service: IOrderStateService,
                 query_service: IOrderQueryService,
                 broker_service: IBrokerService,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initialize the stale order service.
        
        Args:
            state_service: Service for updating order state
            query_service: Service for querying orders
            broker_service: Service for broker operations
            correlation_logger: Optional correlation logger for event logging
        """
        self.logger = logger
        self._state_service = state_service
        self._query_service = query_service
        self._broker_service = broker_service
        # self._telemetry_service = telemetry_service  # REMOVED: Using correlation logger instead
        self._correlation_logger = correlation_logger
        
        # Background thread management
        self._auto_cancel_thread: Optional[threading.Thread] = None
        self._auto_cancel_enabled = False
        self._cancel_check_interval = 5.0  # seconds
        self._stale_pending_max_age = 11.0  # seconds
        
        # Thread synchronization
        self._shutdown_event = threading.Event()
        
        logger.info("StaleOrderService initialized")
    
    # ===== ILifecycleService Implementation =====
    
    def start(self) -> None:
        """
        Start the service.
        
        This is called during application startup but does not automatically
        start the stale order monitoring. Call start_auto_cancel_stale_pending()
        to begin monitoring.
        """
        logger.info("StaleOrderService started (monitoring not yet active)")
    
    def stop(self) -> None:
        """
        Stop the service and any background monitoring.
        """
        self.stop_auto_cancel_stale_pending()
        logger.info("StaleOrderService stopped")
    
    @property
    def is_ready(self) -> bool:
        """
        Check if the service is ready.
        
        The service is ready as long as its dependencies are available.
        Background monitoring may or may not be active.
        """
        return True
    
    # ===== Auto-Cancel Implementation =====
    
    def start_auto_cancel_stale_pending(self, interval: float = 5.0, max_age: float = 11.0) -> None:
        """
        Starts automatic cancellation of stale pending orders.
        
        Args:
            interval: Check interval in seconds (default: 5.0)
            max_age: Maximum age in seconds before order is considered stale (default: 11.0)
        """
        if self._auto_cancel_enabled:
            logger.warning("Auto-cancel stale pending orders is already running")
            return
        
        self._cancel_check_interval = interval
        self._stale_pending_max_age = max_age
        self._auto_cancel_enabled = True
        self._shutdown_event.clear()
        
        # Start background thread
        self._auto_cancel_thread = threading.Thread(
            target=self._auto_cancel_loop,
            name="StaleOrderCanceller",
            daemon=True
        )
        self._auto_cancel_thread.start()
        
        logger.info(
            f"Started auto-cancel stale pending orders "
            f"(interval={interval}s, max_age={max_age}s)"
        )
    
    def stop_auto_cancel_stale_pending(self) -> None:
        """
        Stops automatic cancellation of stale pending orders.
        """
        if not self._auto_cancel_enabled:
            return
        
        logger.info("Stopping auto-cancel stale pending orders...")
        self._auto_cancel_enabled = False
        self._shutdown_event.set()
        
        # Wait for thread to finish
        if self._auto_cancel_thread and self._auto_cancel_thread.is_alive():
            self._auto_cancel_thread.join(timeout=2.0)
            if self._auto_cancel_thread.is_alive():
                logger.warning("Auto-cancel thread did not stop within timeout")
        
        self._auto_cancel_thread = None
        logger.info("Auto-cancel stale pending orders stopped")
    
    def _auto_cancel_loop(self) -> None:
        """
        Background loop that checks for and cancels stale pending orders.
        """
        logger.info("Auto-cancel loop started")
        
        while self._auto_cancel_enabled and not self._shutdown_event.is_set():
            try:
                # Check for stale orders
                self._check_and_cancel_stale_orders()
                
                # Wait for next interval or shutdown
                if self._shutdown_event.wait(timeout=self._cancel_check_interval):
                    break
                    
            except Exception as e:
                logger.error(f"Error in auto-cancel loop: {e}", exc_info=True)
                # Continue running despite errors
                if self._shutdown_event.wait(timeout=1.0):
                    break
        
        logger.info("Auto-cancel loop exited")
    
    def _check_and_cancel_stale_orders(self) -> None:
        """
        Check all orders and cancel any that are stale pending.
        """
        current_time = time.time()
        stale_orders_found = 0
        orders_cancelled = 0
        
        # Get all orders
        all_orders = self._query_service.get_all_orders()
        
        for order in all_orders:
            # Check if order is pending
            if order.status != OrderStatus.PENDING_SUBMISSION:
                continue
            
            # Check if order is stale
            order_age = current_time - order.timestamp_requested
            if order_age <= self._stale_pending_max_age:
                continue
            
            stale_orders_found += 1
            
            # Log the stale order
            logger.warning(
                f"Found stale pending order: local_id={order.local_id}, "
                f"symbol={order.symbol}, age={order_age:.1f}s, "
                f"ls_order_id={order.ls_order_id}"
            )
            
            # Cancel the order
            if self._cancel_stale_order(order.local_id, order.ls_order_id, order_age):
                orders_cancelled += 1
        
        if stale_orders_found > 0:
            logger.info(
                f"Stale order check complete: found={stale_orders_found}, "
                f"cancelled={orders_cancelled}"
            )
    
    def _cancel_stale_order(self, local_id: int, ls_order_id: Optional[int], age: float) -> bool:
        """
        Cancel a single stale order.
        
        Args:
            local_id: Local order ID
            ls_order_id: Broker order ID (if available)
            age: Age of the order in seconds
            
        Returns:
            True if cancellation was initiated, False otherwise
        """
        try:
            # Update local status to CancelRequested
            reason = f"Auto-cancelled: stale pending order (age={age:.1f}s)"
            self._state_service.update_order_status_locally(
                local_id=local_id,
                new_status="CancelRequested",
                reason=reason
            )
            
            # If we have a broker ID, request cancellation from broker
            if ls_order_id is not None:
                try:
                    # Create cancellation event for telemetry
                    cancel_event = {
                        "event_type": "STALE_ORDER_CANCEL",
                        "local_id": local_id,
                        "ls_order_id": ls_order_id,
                        "age_seconds": age,
                        "reason": reason,
                        "timestamp": time.time()
                    }
                    
                    # Publish cancellation event
                    # self._telemetry_service.enqueue(
                    #     source_component="StaleOrderService",
                    #     event_type="STALE_ORDER_CANCEL",
                    #     payload=cancel_event,
                    #     stream_override="order_events"
                    # )
                    
                    if self._correlation_logger:
                        self._correlation_logger.log_event(
                            source_component="StaleOrderService",
                            event_name="StaleOrderCancel",
                            payload=cancel_event,
                            stream_override="order_events"
                        )
                    
                    # Request broker cancellation
                    self._broker_service.cancel_order(ls_order_id)
                    
                    logger.info(
                        f"Initiated cancellation for stale order: "
                        f"local_id={local_id}, ls_order_id={ls_order_id}"
                    )
                    return True
                    
                except Exception as e:
                    logger.error(
                        f"Failed to cancel stale order {local_id} "
                        f"(ls_order_id={ls_order_id}): {e}"
                    )
                    return False
            else:
                # No broker ID, just mark as cancelled locally
                self._state_service.update_order_status_locally(
                    local_id=local_id,
                    new_status=OrderStatus.CANCELLED.value,
                    reason=reason
                )
                logger.info(
                    f"Cancelled stale order locally (no broker ID): "
                    f"local_id={local_id}"
                )
                return True
                
        except Exception as e:
            logger.error(
                f"Error cancelling stale order {local_id}: {e}",
                exc_info=True
            )
            return False
    
    def get_stale_order_stats(self) -> dict:
        """
        Get statistics about stale order monitoring.
        
        Returns:
            Dictionary with monitoring status and configuration
        """
        return {
            "enabled": self._auto_cancel_enabled,
            "check_interval_seconds": self._cancel_check_interval,
            "max_age_seconds": self._stale_pending_max_age,
            "thread_alive": (
                self._auto_cancel_thread.is_alive()
                if self._auto_cancel_thread else False
            )
        }
