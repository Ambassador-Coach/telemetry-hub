"""
Enhanced Broker Services

This module provides enhanced versions of broker-related services that integrate
with the IntelliSense capture interfaces for correlation data capture.
"""

import logging
import time
import threading
from typing import List, Dict, Any, Optional

# Placeholder classes for broker data types (would normally import from modules.broker)
class BrokerResponse:
    """Placeholder for broker response data."""
    def __init__(self, response_type: str, order_id: str, data: Dict[str, Any], timestamp: float):
        self.response_type = response_type
        self.order_id = order_id
        self.data = data
        self.timestamp = timestamp


class LightspeedBroker:
    """Placeholder base class for Lightspeed broker."""
    def __init__(self, *args, **kwargs):
        logger.info("Base LightspeedBroker initialized (placeholder).")
    
    def process_response(self, response: BrokerResponse) -> bool:
        """Process broker response. Returns True if successful."""
        logger.debug(f"Base LightspeedBroker processing response: {response.response_type} for order {response.order_id}")
        # Simulate some processing
        return response.response_type in ['ACK', 'FILL', 'PARTIAL_FILL', 'CANCEL_ACK']


# Import capture interfaces
from intellisense.capture.interfaces import IBrokerDataListener
from .metrics_collector import ComponentCaptureMetrics

logger = logging.getLogger(__name__)


class EnhancedLightspeedBroker(LightspeedBroker):
    """Lightspeed broker enhanced with data capture observation."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._broker_data_listeners: List[IBrokerDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()
        logger.info("EnhancedLightspeedBroker initialized.")
    
    def add_broker_data_listener(self, listener: IBrokerDataListener) -> None:
        """Add a broker data listener for capture observation."""
        with self._listeners_lock:
            if listener not in self._broker_data_listeners:
                self._broker_data_listeners.append(listener)
                logger.debug(f"Registered broker data listener: {type(listener).__name__}")
    
    def remove_broker_data_listener(self, listener: IBrokerDataListener) -> None:
        """Remove a broker data listener."""
        with self._listeners_lock:
            try:
                self._broker_data_listeners.remove(listener)
                logger.debug(f"Unregistered broker data listener: {type(listener).__name__}")
            except ValueError:
                logger.warning(f"Attempted to unregister broker listener not found: {type(listener).__name__}")

    def process_response(self, response: BrokerResponse) -> bool:
        """Enhanced broker response processing with capture observation."""
        if not self._broker_data_listeners:  # Optimization: no listeners, just call super
            return super().process_response(response)

        processing_start_ns = time.perf_counter_ns()
        processing_result = False
        processing_error: Optional[Exception] = None
        
        try:
            processing_result = super().process_response(response)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns, 
                success=(processing_error is None and processing_result)
            )

            # Create comprehensive broker response payload
            broker_payload = {
                'broker_response_type': response.response_type,
                'original_order_id': response.order_id,
                'broker_data': response.data.copy() if response.data else {},
                'response_timestamp': response.timestamp,
                'processing_timestamp': time.time(),
                'processing_latency_ns': processing_latency_ns,
                'processing_success': processing_result,
                'processing_error': str(processing_error) if processing_error else None,
                'broker_name': 'Lightspeed'
            }
            
            # Add response-specific data based on type
            if response.response_type in ['FILL', 'PARTIAL_FILL']:
                broker_payload['broker_data'].update({
                    'filled_quantity': response.data.get('filled_quantity', 0),
                    'fill_price': response.data.get('fill_price', 0.0),
                    'remaining_quantity': response.data.get('remaining_quantity', 0),
                    'execution_id': response.data.get('execution_id', ''),
                    'fill_timestamp': response.data.get('fill_timestamp', response.timestamp)
                })
            elif response.response_type == 'ACK':
                broker_payload['broker_data'].update({
                    'acknowledged_quantity': response.data.get('acknowledged_quantity', 0),
                    'ack_price': response.data.get('ack_price', 0.0),
                    'order_status': response.data.get('order_status', 'ACKNOWLEDGED'),
                    'ack_timestamp': response.data.get('ack_timestamp', response.timestamp)
                })
            elif response.response_type in ['REJECT', 'REJECTED']:
                broker_payload['broker_data'].update({
                    'rejection_reason': response.data.get('rejection_reason', 'Unknown'),
                    'error_code': response.data.get('error_code', ''),
                    'rejection_timestamp': response.data.get('rejection_timestamp', response.timestamp),
                    'attempted_quantity': response.data.get('attempted_quantity', 0)
                })
            elif response.response_type == 'CANCEL_ACK':
                broker_payload['broker_data'].update({
                    'cancelled_quantity': response.data.get('cancelled_quantity', 0),
                    'remaining_quantity': response.data.get('remaining_quantity', 0),
                    'cancel_timestamp': response.data.get('cancel_timestamp', response.timestamp),
                    'cancel_reason': response.data.get('cancel_reason', 'User requested')
                })
            
            self._notify_broker_listeners(
                source_component_name='LightspeedBroker',
                response_data=broker_payload,
                processing_latency_ns=processing_latency_ns
            )

        if processing_error:
            raise processing_error  # Re-raise after notification
        return processing_result

    def _notify_broker_listeners(self, source_component_name: str, 
                               response_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered broker data listeners."""
        if not self._broker_data_listeners:
            return

        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._broker_data_listeners)  # Create a copy
        
        for listener in listeners_snapshot:
            try:
                listener.on_broker_response(
                    source_component_name=source_component_name,
                    response_data=response_data,
                    processing_latency_ns=processing_latency_ns
                )
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying broker listener {type(listener).__name__}: {e}", exc_info=True)

    def get_capture_metrics(self) -> Dict[str, Any]:
        """Get comprehensive broker processing and capture metrics."""
        metrics = self._capture_metrics.get_metrics()
        metrics['listener_count'] = len(self._broker_data_listeners)
        metrics['component_type'] = 'LightspeedBroker'
        return metrics


class EnhancedBrokerService:
    """Generic enhanced broker service for other broker implementations."""
    
    def __init__(self, broker_name: str = "GenericBroker"):
        self.broker_name = broker_name
        self._broker_data_listeners: List[IBrokerDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()
        logger.info(f"EnhancedBrokerService initialized for {broker_name}.")
    
    def add_broker_data_listener(self, listener: IBrokerDataListener) -> None:
        """Add a broker data listener for capture observation."""
        with self._listeners_lock:
            if listener not in self._broker_data_listeners:
                self._broker_data_listeners.append(listener)
                logger.debug(f"Registered broker data listener: {type(listener).__name__}")
    
    def remove_broker_data_listener(self, listener: IBrokerDataListener) -> None:
        """Remove a broker data listener."""
        with self._listeners_lock:
            try:
                self._broker_data_listeners.remove(listener)
                logger.debug(f"Unregistered broker data listener: {type(listener).__name__}")
            except ValueError:
                logger.warning(f"Attempted to unregister broker listener not found: {type(listener).__name__}")

    def notify_response_processed(self, response_type: str, order_id: str, 
                                response_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify listeners that a broker response was processed."""
        if not self._broker_data_listeners:
            return

        broker_payload = {
            'broker_response_type': response_type,
            'original_order_id': order_id,
            'broker_data': response_data.copy() if response_data else {},
            'processing_timestamp': time.time(),
            'processing_latency_ns': processing_latency_ns,
            'broker_name': self.broker_name
        }
        
        self._notify_broker_listeners(
            source_component_name=self.broker_name,
            response_data=broker_payload,
            processing_latency_ns=processing_latency_ns
        )

    def _notify_broker_listeners(self, source_component_name: str, 
                               response_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered broker data listeners."""
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._broker_data_listeners)  # Create a copy
        
        for listener in listeners_snapshot:
            try:
                listener.on_broker_response(
                    source_component_name=source_component_name,
                    response_data=response_data,
                    processing_latency_ns=processing_latency_ns
                )
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying broker listener {type(listener).__name__}: {e}", exc_info=True)

    def get_capture_metrics(self) -> Dict[str, Any]:
        """Get comprehensive broker processing and capture metrics."""
        metrics = self._capture_metrics.get_metrics()
        metrics['listener_count'] = len(self._broker_data_listeners)
        metrics['component_type'] = self.broker_name
        return metrics
