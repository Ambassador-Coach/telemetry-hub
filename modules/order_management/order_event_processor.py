# modules/order_management/order_event_processor.py
"""
Order Event Processor Service Implementation

This service handles all broker events and updates order state by delegating
to the OrderStateService and OrderLinkingService. It implements the event-driven
portion of order management, keeping event handling logic separate from state management.
"""

import logging
import time
import uuid
from typing import Dict, Any, Optional, Callable, TYPE_CHECKING

# Import core interfaces
from interfaces.core.services import IEventBus, ILifecycleService
from interfaces.order_management.services import IOrderEventProcessor
from interfaces.logging.services import ICorrelationLogger
from interfaces.order_management.state_service import IOrderStateService
from interfaces.order_management.services import IOrderLinkingService
from interfaces.order_management.services import IOrderQueryService

# Import position manager interface for fill notifications
if TYPE_CHECKING:
    from interfaces.trading.services import IPositionManager

# Import event classes
from core.events import (
    BrokerConnectionEvent, BrokerConnectionEventData,
    OrderStatusUpdateEvent, OrderStatusUpdateData,
    OrderFilledEvent, OrderFilledEventData,
    OrderRejectedEvent, OrderRejectedData,
    BrokerErrorEvent, BrokerErrorData,
    BrokerRawMessageRelayEvent, BrokerRawMessageRelayEventData,
    # FUZZY'S FIX: Add missing broker gateway events
    BrokerOrderSentEvent, BrokerOrderSentEventData,
    BrokerOrderSubmittedEvent, BrokerOrderSubmittedEventData
)

# Import data types and enums
from data_models.order_data_types import (
    Order, FillRecord, StatusHistoryEntry,
    OrderEventType, OrderOrigin
)
from data_models import OrderSide, OrderLSAgentStatus, OrderStatus

# Import performance tracking functions
try:
    from utils.performance_tracker import (
        create_timestamp_dict, add_timestamp, calculate_durations, 
        add_to_stats, is_performance_tracking_enabled
    )
except ImportError:
    # Define dummy functions if import fails
    def is_performance_tracking_enabled(): return False
    def create_timestamp_dict(): return None
    def add_timestamp(*args, **kwargs): return args[0] if args else None
    def calculate_durations(*args, **kwargs): return {}
    def add_to_stats(*args, **kwargs): pass

logger = logging.getLogger(__name__)


class OrderEventProcessor(IOrderEventProcessor, ILifecycleService):
    """
    Concrete implementation of IOrderEventProcessor.
    
    Subscribes to broker events via EventBus and updates order state by
    delegating to the OrderStateService and OrderLinkingService. This
    separation of concerns keeps event handling logic isolated from
    state management and querying.
    """
    
    def __init__(self,
                 event_bus: IEventBus,
                 state_service: IOrderStateService,
                 linking_service: IOrderLinkingService,
                 query_service: IOrderQueryService,
                 position_manager: Optional['IPositionManager'] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initialize the OrderEventProcessor.
        
        Args:
            event_bus: Service for subscribing to and publishing events
            state_service: Service for updating order state
            linking_service: Service for managing ID mappings
            query_service: Service for querying order data
            position_manager: Optional service for notifying position updates
            correlation_logger: Optional correlation logger for event logging
        """
        self.logger = logger
        self._event_bus = event_bus
        self._state_service = state_service
        self._linking_service = linking_service
        self._query_service = query_service
        # self._telemetry_service = telemetry_service  # REMOVED: Using correlation logger instead
        self._position_manager = position_manager
        self._correlation_logger = correlation_logger
        
        # Lifecycle state
        self._is_running = False
        
        # Trade finalization callback
        self._trade_finalized_callback: Optional[Callable[[int, str, str, str, Optional[int]], None]] = None
        
        logger.info("OrderEventProcessor initialized")
    
    # ===== ILifecycleService Implementation =====
    
    def start(self) -> None:
        """
        Start the service and subscribe to events.
        """
        if self._is_running:
            logger.warning("OrderEventProcessor is already running")
            return
            
        self._subscribe_to_events()
        self._is_running = True
        logger.info("OrderEventProcessor started and subscribed to events")
    
    def stop(self) -> None:
        """
        Stop the service.
        
        Note: EventBus subscriptions are typically not unsubscribed as the
        entire application is shutting down when services are stopped.
        """
        self._is_running = False
        logger.info("OrderEventProcessor stopped")
    
    @property
    def is_ready(self) -> bool:
        """
        Check if the service is ready to process events.
        """
        return self._is_running
    
    # ===== Event Subscription =====
    
    def _subscribe_to_events(self) -> None:
        """
        Subscribe to all relevant broker events.
        """
        # Order lifecycle events
        self._event_bus.subscribe(OrderStatusUpdateEvent, self.handle_order_status_update)
        self._event_bus.subscribe(OrderFilledEvent, self.handle_order_fill)
        self._event_bus.subscribe(OrderRejectedEvent, self.handle_order_rejection)
        
        # Broker connection and error events
        self._event_bus.subscribe(BrokerConnectionEvent, self._handle_broker_connection_status)
        self._event_bus.subscribe(BrokerErrorEvent, self._handle_broker_error)
        
        # Raw message relay for legacy processing
        self._event_bus.subscribe(BrokerRawMessageRelayEvent, self._handle_broker_raw_message_relay)

        # FUZZY'S FIX: Add missing broker gateway events
        self._event_bus.subscribe(BrokerOrderSentEvent, self._handle_broker_order_sent)
        self._event_bus.subscribe(BrokerOrderSubmittedEvent, self._handle_broker_order_submitted)

        logger.info("OrderEventProcessor now subscribed to BrokerOrderSent and BrokerOrderSubmitted events.")
        logger.info("OrderEventProcessor subscribed to all relevant events")
    
    # ===== Public Interface Methods =====
    
    def process_broker_message(self,
                               msg: Dict[str, Any],
                               perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        """
        Process a raw broker message and update order state accordingly.
        
        This method handles legacy-style broker messages that contain order
        status updates, fills, or other order-related information.
        
        Args:
            msg: Raw broker message dictionary
            perf_timestamps: Optional performance tracking timestamps
        """
        try:
            perf_dict = perf_timestamps if is_performance_tracking_enabled() else None
            perf_dict = add_timestamp(perf_dict, "t18_OrderEventProcessor_process_start")
            
            event_type = msg.get("event")
            
            if event_type == "orderstatus":
                self._process_order_status_message(msg, perf_dict)
            elif event_type == "fill":
                self._process_fill_message(msg, perf_dict)
            elif event_type == "rejection":
                self._process_rejection_message(msg, perf_dict)
            else:
                logger.debug(f"Unhandled broker message type: {event_type}")
            
            perf_dict = add_timestamp(perf_dict, "t19_OrderEventProcessor_process_end")
            
        except Exception as e:
            logger.error(f"Error processing broker message: {e}", exc_info=True)
    
    def set_trade_finalized_callback(self, 
                                     callback_func: Callable[[int, str, str, str, Optional[int]], None]) -> None:
        """
        Set the callback function to be called when a trade is finalized.
        
        The callback will be invoked with:
        - local_id: Local order ID
        - symbol: Trading symbol
        - event_type: Type of order event
        - final_status: Final order status
        - parent_trade_id: Parent trade ID (if applicable)
        
        Args:
            callback_func: Callback function to invoke on trade finalization
        """
        self._trade_finalized_callback = callback_func
        logger.info("Trade finalized callback set")
    
    # ===== Event Handlers =====
    
    def handle_order_status_update(self, event: OrderStatusUpdateEvent) -> None:
        """
        Handle order status update events from the broker.
        
        Args:
            event: OrderStatusUpdateEvent with status information
        """
        try:
            data = event.data
            
            # Capture correlation context
            correlation_id = getattr(event, 'correlation_id', str(uuid.uuid4()))
            
            logger.info(f"Processing order status update - CorrID: {correlation_id}, "
                       f"BrokerID: {data.broker_order_id}, Status: {data.status}")
            
            # Find the local order ID using the linking service
            local_id = None
            if data.broker_order_id:
                try:
                    broker_id_int = int(data.broker_order_id)
                    local_id = self._linking_service.get_local_id_from_broker_id(broker_id_int)
                except ValueError:
                    logger.warning(f"Invalid broker_order_id format: {data.broker_order_id}")
            
            if local_id is None:
                logger.warning(f"No order found for broker_order_id: {data.broker_order_id}")
                self._log_order_validation_failure(
                    "ORPHANED_STATUS_UPDATE",
                    data.broker_order_id,
                    data.status,
                    correlation_id
                )
                return
            
            # Get current order state
            order = self._query_service.get_order_by_local_id(local_id)
            if not order:
                logger.error(f"Order {local_id} not found despite having ID mapping")
                return
            
            # Update order status - pass raw broker status, let OrderStateService handle conversion
            # The Order.__post_init__ will automatically derive the generic status
            success = self._state_service.update_order_status_locally(
                local_id=local_id,
                new_status=data.status,  # Raw broker status (OrderLSAgentStatus string)
                reason=f"Broker status update: {data.status}"
            )
            
            if success:
                logger.info(f"Order {local_id} status updated from broker: {data.status}")
                
                # Get the updated order to check its derived generic status
                updated_order = self._query_service.get_order_by_local_id(local_id)
                if updated_order and self._is_final_status(updated_order.status.value):
                    self._handle_trade_finalization(updated_order, updated_order.status.value)
                    
                # Publish telemetry event
                self._log_order_status_update(updated_order or order, data.status, correlation_id)
            else:
                logger.error(f"Failed to update status for order {local_id}")
                
        except Exception as e:
            logger.error(f"Error handling order status update: {e}", exc_info=True)
    
    def handle_order_fill(self, event: OrderFilledEvent) -> None:
        """
        Handle order fill events from the broker.
        
        Args:
            event: OrderFilledEvent with fill information
        """
        try:
            # Performance tracking
            t21_start_ns = time.time_ns() if is_performance_tracking_enabled() else None
            
            data = event.data
            
            # Capture correlation context
            correlation_id = getattr(event, 'correlation_id', str(uuid.uuid4()))
            causation_id = getattr(event, 'event_id', None)
            
            logger.info(f"Processing order fill - CorrID: {correlation_id}, "
                       f"LocalID: {data.local_order_id}, BrokerID: {data.order_id}, "
                       f"Symbol: {data.symbol}, Qty: {data.fill_quantity}, Price: {data.fill_price}")
            
            # Find the local order ID
            local_id = self._resolve_local_id(data.local_order_id, data.order_id)
            
            if local_id is None:
                logger.warning(f"No order found for fill - local_id: {data.local_order_id}, "
                             f"broker_id: {data.order_id}")
                self._log_orphaned_fill_event(data, correlation_id)
                return
            
            # Get current order state
            order = self._query_service.get_order_by_local_id(local_id)
            if not order:
                logger.error(f"Order {local_id} not found despite having ID mapping")
                return
            
            # Validate fill data
            if not self._validate_fill_data(data, order, correlation_id):
                return
            
            # Create fill record
            fill_record = FillRecord(
                shares_filled=data.fill_quantity,
                fill_price=data.fill_price,
                fill_time=data.fill_timestamp,
                side=data.side
            )
            
            # Process the fill in the state service
            success = self._process_fill_in_state(local_id, order, fill_record, data)
            
            if success:
                # Notify position manager
                if self._position_manager:
                    self._notify_position_manager_of_fill(
                        order, data, local_id, correlation_id
                    )
                
                # Check if order is now fully filled
                updated_order = self._query_service.get_order_by_local_id(local_id)
                if updated_order and updated_order.leftover_shares < 1e-9:
                    self._handle_trade_finalization(updated_order, updated_order.status.value)
                
                # Publish telemetry event
                self._log_fill_event(order, data, correlation_id)
                
                # Performance tracking
                if is_performance_tracking_enabled() and t21_start_ns:
                    t21_end_ns = time.time_ns()
                    duration_us = (t21_end_ns - t21_start_ns) / 1000
                    add_to_stats("t21_repository_fill_update_us", duration_us)
            
        except Exception as e:
            logger.error(f"Error handling order fill: {e}", exc_info=True)
    
    def handle_order_rejection(self, event: OrderRejectedEvent) -> None:
        """
        Handle order rejection events from the broker.
        
        Args:
            event: OrderRejectedEvent with rejection information
        """
        try:
            data = event.data
            
            # Capture correlation context
            correlation_id = getattr(event, 'correlation_id', str(uuid.uuid4()))
            
            logger.warning(f"Processing order rejection - CorrID: {correlation_id}, "
                          f"LocalID: {data.local_order_id}, BrokerID: {data.broker_order_id}, "
                          f"Symbol: {data.symbol}, Reason: {data.reason}")
            
            # Find the local order ID
            local_id = self._resolve_local_id(data.local_order_id, data.broker_order_id)
            
            if local_id is None:
                logger.warning(f"No order found for rejection - local_id: {data.local_order_id}, "
                             f"broker_id: {data.broker_order_id}")
                return
            
            # Get current order state
            order = self._query_service.get_order_by_local_id(local_id)
            if not order:
                logger.error(f"Order {local_id} not found despite having ID mapping")
                return
            
            # Update order status to rejected - use raw broker status
            success = self._state_service.update_order_status_locally(
                local_id=local_id,
                new_status="REJECTED",  # Raw broker status string
                reason=data.reason
            )
            
            if success:
                logger.warning(f"Order {local_id} marked as REJECTED: {data.reason}")
                
                # Get updated order and handle trade finalization
                updated_order = self._query_service.get_order_by_local_id(local_id)
                if updated_order:
                    self._handle_trade_finalization(updated_order, updated_order.status.value)
                
                # Publish telemetry event
                self._log_rejection_event(order, data, correlation_id)
            else:
                logger.error(f"Failed to update rejection status for order {local_id}")
                
        except Exception as e:
            logger.error(f"Error handling order rejection: {e}", exc_info=True)
    
    # ===== Private Event Handlers =====
    
    def _handle_broker_connection_status(self, event: BrokerConnectionEvent) -> None:
        """
        Handle broker connection status events.
        
        Args:
            event: BrokerConnectionEvent with connection information
        """
        try:
            data = event.data
            logger.info(f"Broker connection status: connected={data.connected}, "
                       f"message='{data.message}'")
            
            if not data.connected:
                # Mark all open/pending orders as potentially stale
                self._mark_orders_connection_lost()
                
        except Exception as e:
            logger.error(f"Error handling broker connection status: {e}", exc_info=True)
    
    def _handle_broker_error(self, event: BrokerErrorEvent) -> None:
        """
        Handle broker error events.
        
        Args:
            event: BrokerErrorEvent with error information
        """
        try:
            data = event.data
            
            log_level = logging.ERROR if data.is_critical else logging.WARNING
            logger.log(log_level, f"Broker error: {data.message} (critical={data.is_critical})")
            
            if data.details:
                logger.debug(f"Broker error details: {data.details}")
            
            # If critical error, might need to mark orders as uncertain
            if data.is_critical:
                logger.warning("Critical broker error - order states may be uncertain")
                
        except Exception as e:
            logger.error(f"Error handling broker error event: {e}", exc_info=True)
    
    def _handle_broker_raw_message_relay(self, event: BrokerRawMessageRelayEvent) -> None:
        """
        Handle raw broker message relay events for legacy processing.
        
        Args:
            event: BrokerRawMessageRelayEvent with raw message data
        """
        try:
            data = event.data
            raw_msg = data.raw_message_dict
            
            logger.debug(f"Processing raw broker message relay: event_type={raw_msg.get('event')}, "
                        f"local_copy_id={data.original_local_copy_id}")
            
            # Delegate to the main process_broker_message method
            self.process_broker_message(raw_msg, data.perf_timestamps)
            
        except Exception as e:
            logger.error(f"Error handling raw message relay: {e}", exc_info=True)
    
    # ===== Helper Methods =====
    
    def _resolve_local_id(self, 
                          local_id_str: Optional[str], 
                          broker_id_str: Optional[str]) -> Optional[int]:
        """
        Resolve the local order ID from either local ID string or broker ID string.
        
        Args:
            local_id_str: Local ID as string (may be None)
            broker_id_str: Broker ID as string (may be None)
            
        Returns:
            Local order ID as integer, or None if not found
        """
        # Try local ID first
        if local_id_str:
            try:
                return int(local_id_str)
            except ValueError:
                logger.warning(f"Invalid local_id format: {local_id_str}")
        
        # Try broker ID mapping
        if broker_id_str:
            try:
                broker_id_int = int(broker_id_str)
                return self._linking_service.get_local_id_from_broker_id(broker_id_int)
            except ValueError:
                logger.warning(f"Invalid broker_id format: {broker_id_str}")
        
        return None
    
    def _is_final_status(self, status: str) -> bool:
        """
        Check if a status is a final (terminal) status.
        
        Args:
            status: Order status string
            
        Returns:
            True if status is final, False otherwise
        """
        return status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
    
    def _validate_fill_data(self, 
                            fill_data: OrderFilledEventData, 
                            order: Order,
                            correlation_id: str) -> bool:
        """
        Validate fill data before processing.
        
        Args:
            fill_data: Fill event data
            order: Current order state
            correlation_id: Correlation ID for tracking
            
        Returns:
            True if valid, False otherwise
        """
        # Validate fill quantity
        if fill_data.fill_quantity <= 0:
            logger.error(f"Invalid fill quantity: {fill_data.fill_quantity}")
            self._log_validation_failure(
                "INVALID_FILL_QUANTITY",
                order.local_id,
                fill_data,
                correlation_id
            )
            return False
        
        # Validate fill price
        if fill_data.fill_price <= 0:
            logger.error(f"Invalid fill price: {fill_data.fill_price}")
            self._log_validation_failure(
                "INVALID_FILL_PRICE",
                order.local_id,
                fill_data,
                correlation_id
            )
            return False
        
        # Validate against order capacity
        remaining_shares = order.requested_shares - order.filled_quantity
        if fill_data.fill_quantity > remaining_shares + 1e-9:  # Small epsilon for float comparison
            logger.warning(f"Fill quantity {fill_data.fill_quantity} exceeds remaining "
                         f"shares {remaining_shares} for order {order.local_id}")
            # This is a warning, not an error - broker might allow over-fills
        
        return True
    
    def _process_fill_in_state(self,
                               local_id: int,
                               order: Order,
                               fill_record: FillRecord,
                               fill_data: OrderFilledEventData) -> bool:
        """
        Process a fill by updating the order state.
        
        Args:
            local_id: Local order ID
            order: Current order state
            fill_record: New fill record to add
            fill_data: Fill event data
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # The state service will handle the actual update
            # For now, we'll update the status based on fill completion
            new_filled_qty = order.filled_quantity + fill_data.fill_quantity
            new_leftover = order.requested_shares - new_filled_qty
            
            # Determine new status - use raw broker status strings
            if new_leftover < 1e-9:  # Fully filled
                new_status = "Filled"  # OrderLSAgentStatus.FILLED.value
            else:
                new_status = "PartiallyFilled"  # OrderLSAgentStatus.PARTIALLY_FILLED.value
            
            # Update order status (state service will handle fill record internally)
            return self._state_service.update_order_status_locally(
                local_id=local_id,
                new_status=new_status,
                reason=f"Fill: {fill_data.fill_quantity} @ {fill_data.fill_price}"
            )
            
        except Exception as e:
            logger.error(f"Error processing fill in state: {e}", exc_info=True)
            return False
    
    def _handle_trade_finalization(self, order: Order, final_status: str) -> None:
        """
        Handle trade finalization by invoking the callback if set.
        
        Args:
            order: The finalized order
            final_status: Final status of the order
        """
        if self._trade_finalized_callback:
            try:
                self._trade_finalized_callback(
                    order.local_id,
                    order.symbol,
                    order.event_type,
                    final_status,
                    order.parent_trade_id
                )
                logger.info(f"Trade finalized callback invoked for order {order.local_id}")
            except Exception as e:
                logger.error(f"Error in trade finalized callback: {e}", exc_info=True)
    
    def _notify_position_manager_of_fill(self,
                                         order: Order,
                                         fill_data: OrderFilledEventData,
                                         local_id: int,
                                         correlation_id: str) -> None:
        """
        Notify the position manager of a new fill.
        
        Args:
            order: The order that was filled
            fill_data: Fill event data
            local_id: Local order ID
            correlation_id: Correlation ID for tracking
        """
        try:
            if hasattr(self._position_manager, 'process_fill'):
                self._position_manager.process_fill(
                    symbol=fill_data.symbol,
                    side=fill_data.side.value,
                    shares_filled=fill_data.fill_quantity,
                    fill_price=fill_data.fill_price,
                    local_id=local_id,
                    fill_time=fill_data.fill_timestamp,
                    master_correlation_id=correlation_id
                )
                logger.debug(f"Notified position manager of fill for order {local_id}")
        except Exception as e:
            logger.warning(f"Error notifying position manager of fill: {e}")
    
    def _mark_orders_connection_lost(self) -> None:
        """
        Mark all open/pending orders as potentially stale due to connection loss.
        """
        try:
            # Get all open orders
            open_orders = self._query_service.get_open_orders()
            
            for order in open_orders:
                if order.status in [OrderStatus.PENDING_SUBMISSION, OrderStatus.SUBMITTED, OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED]:
                    self._state_service.update_order_status_locally(
                        local_id=order.local_id,
                        new_status=order.ls_status.value,  # Keep same broker status
                        reason="Broker connection lost - status uncertain"
                    )
            
            logger.warning(f"Marked {len(open_orders)} orders as potentially stale due to connection loss")
            
        except Exception as e:
            logger.error(f"Error marking orders connection lost: {e}", exc_info=True)
    
    # ===== Legacy Message Processing =====
    
    def _process_order_status_message(self, msg: Dict[str, Any], perf_dict: Optional[Dict[str, float]]) -> None:
        """
        Process a legacy order status message.
        """
        # Extract relevant fields and create an OrderStatusUpdateEvent
        broker_order_id = msg.get("orderId")
        status_str = msg.get("status", "").upper()
        
        # Map legacy status to enum
        status_map = {
            "PENDING": OrderLSAgentStatus.PENDING,
            "OPEN": OrderLSAgentStatus.OPEN,
            "FILLED": OrderLSAgentStatus.FILLED,
            "CANCELLED": OrderLSAgentStatus.CANCELLED,
            "REJECTED": OrderLSAgentStatus.REJECTED
        }
        
        status = status_map.get(status_str, OrderLSAgentStatus.UNKNOWN)
        
        # Create and handle event
        event_data = OrderStatusUpdateData(
            broker_order_id=str(broker_order_id),
            status=status,
            timestamp=time.time()
        )
        
        event = OrderStatusUpdateEvent(data=event_data)
        self.handle_order_status_update(event)
    
    def _process_fill_message(self, msg: Dict[str, Any], perf_dict: Optional[Dict[str, float]]) -> None:
        """
        Process a legacy fill message.
        """
        # Extract relevant fields
        event_data = OrderFilledEventData(
            order_id=str(msg.get("orderId", "")),
            local_order_id=str(msg.get("localId", "")),
            symbol=msg.get("symbol", ""),
            side=OrderSide.BUY if msg.get("side") == "B" else OrderSide.SELL,
            fill_quantity=float(msg.get("quantity", 0)),
            fill_price=float(msg.get("price", 0)),
            fill_timestamp=float(msg.get("timestamp", time.time()))
        )
        
        event = OrderFilledEvent(data=event_data)
        self.handle_order_fill(event)
    
    def _process_rejection_message(self, msg: Dict[str, Any], perf_dict: Optional[Dict[str, float]]) -> None:
        """
        Process a legacy rejection message.
        """
        # Extract relevant fields
        event_data = OrderRejectedData(
            broker_order_id=str(msg.get("orderId", "")),
            local_order_id=str(msg.get("localId", "")),
            symbol=msg.get("symbol", ""),
            reason=msg.get("reason", "Unknown rejection reason")
        )
        
        event = OrderRejectedEvent(data=event_data)
        self.handle_order_rejection(event)
    
    # ===== Telemetry Publishing =====
    
    def _log_order_status_update(self, order: Order, new_status: str, correlation_id: str) -> None:
        """
        Publish order status update to telemetry.
        """
        event_data = {
            "event_type": "ORDER_STATUS_UPDATE",
            "local_id": order.local_id,
            "broker_id": order.ls_order_id,
            "symbol": order.symbol,
            "old_status": order.status,
            "new_status": new_status,
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="ORDER_STATUS_UPDATE",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="OrderStatusUpdate",
                payload=event_data,
                stream_override="order_events"
            )
    
    def _log_fill_event(self, order: Order, fill_data: OrderFilledEventData, correlation_id: str) -> None:
        """
        Publish fill event to telemetry.
        """
        event_data = {
            "event_type": "ORDER_FILL",
            "local_id": order.local_id,
            "broker_id": order.ls_order_id,
            "symbol": fill_data.symbol,
            "side": fill_data.side.value,
            "fill_quantity": fill_data.fill_quantity,
            "fill_price": fill_data.fill_price,
            "fill_timestamp": fill_data.fill_timestamp,
            "remaining_quantity": order.leftover_shares,
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="ORDER_FILL",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="OrderFill",
                payload=event_data,
                stream_override="order_events"
            )
    
    def _log_rejection_event(self, order: Order, rejection_data: OrderRejectedData, correlation_id: str) -> None:
        """
        Publish rejection event to telemetry.
        """
        event_data = {
            "event_type": "ORDER_REJECTION",
            "local_id": order.local_id,
            "broker_id": order.ls_order_id,
            "symbol": rejection_data.symbol,
            "reason": rejection_data.reason,
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="ORDER_REJECTION",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="OrderRejection",
                payload=event_data,
                stream_override="order_events"
            )
    
    def _log_order_validation_failure(self,
                                          failure_type: str,
                                          broker_order_id: str,
                                          status: Any,
                                          correlation_id: str) -> None:
        """
        Publish order validation failure event.
        """
        event_data = {
            "event_type": "ORDER_VALIDATION_FAILURE",
            "failure_type": failure_type,
            "broker_order_id": broker_order_id,
            "status": str(status),
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="ORDER_VALIDATION_FAILURE",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="OrderValidationFailure",
                payload=event_data,
                stream_override="order_events"
            )
    
    def _log_orphaned_fill_event(self, fill_data: OrderFilledEventData, correlation_id: str) -> None:
        """
        Publish orphaned fill event.
        """
        event_data = {
            "event_type": "ORPHANED_FILL",
            "local_order_id": fill_data.local_order_id,
            "broker_order_id": fill_data.order_id,
            "symbol": fill_data.symbol,
            "side": fill_data.side.value,
            "fill_quantity": fill_data.fill_quantity,
            "fill_price": fill_data.fill_price,
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="ORPHANED_FILL",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="OrphanedFill",
                payload=event_data,
                stream_override="order_events"
            )
    
    def _log_validation_failure(self,
                                    failure_type: str,
                                    local_id: int,
                                    fill_data: OrderFilledEventData,
                                    correlation_id: str) -> None:
        """
        Publish validation failure event.
        """
        event_data = {
            "event_type": "FILL_VALIDATION_FAILURE",
            "failure_type": failure_type,
            "local_id": local_id,
            "broker_id": fill_data.order_id,
            "symbol": fill_data.symbol,
            "invalid_value": fill_data.fill_quantity if "QUANTITY" in failure_type else fill_data.fill_price,
            "timestamp": time.time(),
            "correlation_id": correlation_id
        }
        
        # self._telemetry_service.enqueue(
        #     source_component="OrderEventProcessor",
        #     event_type="FILL_VALIDATION_FAILURE",
        #     payload=event_data,
        #     correlation_id=correlation_id,
        #     stream_override="order_events"
        # )
        
        if self._correlation_logger:
            self._correlation_logger.log_event(
                source_component="OrderEventProcessor",
                event_name="FillValidationFailure",
                payload=event_data,
                stream_override="order_events"
            )

    # FUZZY'S FIX: Add missing event handlers

    def _handle_broker_order_sent(self, event: BrokerOrderSentEvent) -> None:
        """
        Handle broker order sent events.

        Args:
            event: BrokerOrderSentEvent with order sent information
        """
        try:
            data = event.data
            logger.info(f"Broker order sent: {data.symbol} {data.order_side} {data.order_quantity} "
                       f"(Local ID: {data.local_order_id}, Correlation: {data.correlation_id})")

            # Log telemetry if available
            if self._correlation_logger and data.correlation_id:
                self._correlation_logger.log_event(
                    source_component="OrderEventProcessor",
                    event_name="BrokerOrderSent",
                    payload={
                        "symbol": data.symbol,
                        "local_order_id": data.local_order_id,
                        "correlation_id": data.correlation_id,
                        "submission_successful": data.submission_successful,
                        "T18_BrokerSend_ns": data.T18_BrokerSend_ns
                    }
                )

        except Exception as e:
            logger.error(f"Error handling broker order sent event: {e}", exc_info=True)

    def _handle_broker_order_submitted(self, event: BrokerOrderSubmittedEvent) -> None:
        """
        Handle broker order submitted events.

        Args:
            event: BrokerOrderSubmittedEvent with order submission information
        """
        try:
            data = event.data
            logger.info(f"Broker order submitted: {data.symbol} {data.order_side} {data.order_quantity} "
                       f"(Local ID: {data.local_order_id}, Correlation: {data.correlation_id})")

            # Log telemetry if available
            if self._correlation_logger and data.correlation_id:
                self._correlation_logger.log_event(
                    source_component="OrderEventProcessor",
                    event_name="BrokerOrderSubmitted",
                    payload={
                        "symbol": data.symbol,
                        "local_order_id": data.local_order_id,
                        "correlation_id": data.correlation_id,
                        "submission_successful": data.submission_successful,
                        "T16_ExecutionStart_ns": data.T16_ExecutionStart_ns,
                        "T17_BrokerSubmission_ns": data.T17_BrokerSubmission_ns
                    }
                )

        except Exception as e:
            logger.error(f"Error handling broker order submitted event: {e}", exc_info=True)