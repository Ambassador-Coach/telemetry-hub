"""
Enhanced Broker Interface

This module provides an enhanced version of the LightspeedBroker that integrates
with the IntelliSense capture interfaces for correlation data capture.
This version is designed to work with broker response processing.
"""

import logging
import time
import threading
import random
from typing import List, Dict, Any, Optional, TYPE_CHECKING, Union # Added Union

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from utils.global_config import GlobalConfig
    from core.interfaces import IEventBus
    from interfaces.data.services import IPriceProvider
    # Import actual base class if it exists
    # from modules.broker_bridge.lightspeed_broker import LightspeedBroker as ActualBaseLightspeedBroker
    # Actual broker types from your system
    from modules.broker_bridge.data_types import BrokerResponseData, OrderUpdateData
else:
    # Runtime placeholders for missing types
    class IntelliSenseApplicationCore: pass
    class GlobalConfig: pass
    class IEventBus: pass
    class IPriceProvider: pass

# Assuming original LightspeedBroker and its client are importable/accessible
# For placeholder:
class LightspeedBridgeClientPlaceholder:
    _last_bootstrap_order_id_simulated: Optional[str] = None

    def send_message_no_read(self, msg_dict: dict) -> bool: # Simplified signature for bootstrap
        # This mock now needs to handle the specific "cmd" for place_order for bootstrap
        command_details = msg_dict.get("cmd", "unknown")
        logger.info(f"LSBridgeClientPlaceholder: send_message_no_read CMD: '{command_details}', MSG: {msg_dict}")
        if command_details == "place_order": # Corresponds to LightspeedBroker.place_order logic
            if random.random() > 0.05: # 95% success
                 # Simulate broker assigning an ID for bootstrap trades.
                 # This ID is what execute_bootstrap_trade would ideally return.
                 # If the actual C++ bridge returns an ID in an immediate synchronous response
                 # to such a direct send, this mock reflects that. Otherwise, it's async.
                 self._last_bootstrap_order_id_simulated = f"bs_ord_{int(time.time()*1000_000)}_{random.randint(100,999)}"
                 logger.debug(f"LSBridgeClientPlaceholder: Simulated bootstrap order ID: {self._last_bootstrap_order_id_simulated}")
                 return True
            return False
        return True # Default success for other commands

import random # For simulating success/failure in placeholder

class LightspeedBroker: # Placeholder Base Class
    def __init__(self, *args, **kwargs):
        logger.info("Base LightspeedBroker placeholder initialized.")
        self.client = LightspeedBridgeClientPlaceholder() # Base has a client
        self._listeners_lock = threading.RLock() # For listeners list
        self._broker_data_listeners: List = [] # Will be typed properly when IBrokerDataListener is imported

    def _on_order_update(self, broker_response: Dict[str, Any]) -> None:
        logger.debug(f"Base LightspeedBroker _on_order_update: {broker_response.get('local_order_id')} (placeholder)")

    # Add placeholder for add/remove listeners if not in base from previous chunks
    def add_broker_data_listener(self, listener: Any): self._broker_data_listeners.append(listener)
    def remove_broker_data_listener(self, listener: Any):
        try: self._broker_data_listeners.remove(listener)
        except ValueError: pass

    # Placeholder for _notify_broker_listeners
    def _notify_broker_listeners(self, response_data: Dict[str, Any], processing_latency_ns: int) -> None:
        logger.debug(f"Base LightspeedBroker (Placeholder) would notify listeners with: {response_data}")
        # In Enhanced version, this calls actual listeners.


# Import capture interfaces
from intellisense.capture.interfaces import IBrokerDataListener
from .metrics_collector import ComponentCaptureMetrics

logger = logging.getLogger(__name__)


class EnhancedLightspeedBroker(LightspeedBroker):
    """Broker interface enhanced with data capture observation and bootstrap trade execution."""

    def __init__(self,
                 app_core_ref_for_intellisense: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig',
                 original_event_bus: 'IEventBus',
                 original_price_provider: 'IPriceProvider',
                 *args, **kwargs):
        """Initialize with ComponentFactory dependencies."""
        # Call super with arguments the base class expects
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         price_provider=original_price_provider,
                         *args, **kwargs)

        self._app_core_ref: 'IntelliSenseApplicationCore' = app_core_ref_for_intellisense # Store with string hint
        # Ensure required attributes are initialized
        if not hasattr(self, '_broker_data_listeners'): self._broker_data_listeners = []
        if not hasattr(self, '_listeners_lock'): self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()

        logger.info(f"EnhancedLightspeedBroker constructed via factory. AppCoreRef: {type(self._app_core_ref).__name__}")

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
                logger.warning(f"Attempted to unregister Broker listener not found: {type(listener).__name__}")

    # ... (add_broker_data_listener, remove_broker_data_listener as from Chunk 9)
    # ... (_on_order_update override as from Chunk 9, calling self._notify_broker_listeners)
    # ... (_notify_broker_listeners as from Chunk 9)
    # ... (get_capture_metrics as from Chunk 9)

    def execute_bootstrap_trade(self,
                              symbol: str,
                              side: str,
                              quantity: int,
                              order_type: str,
                              limit_price: Optional[float] = None,
                              account: str = "BOOTSTRAP_ACCT" # Default as per SME
                             ) -> Optional[str]:
        """
        Executes a bootstrap trade using low-level direct messaging, bypassing normal
        OrderRepository creation for this specific action. Notifies listeners about the attempt.
        Based on SME guidance (Option 2 - direct lower-level mechanism).

        Args:
            symbol: Trading symbol.
            side: "BUY" or "SELL".
            quantity: Number of shares (integer).
            order_type: "MKT" or "LMT".
            limit_price: Required if order_type is "LMT".
            account: The specific account to use (defaults to "BOOTSTRAP_ACCT").

        Returns:
            Optional[str]: The broker-assigned order ID if the send was successful and
                           an ID is immediately available/simulated; None otherwise.
        """
        if not self.client:
            logger.error(f"Bootstrap trade for {symbol} on account {account} failed: Broker client not available.")
            self._notify_bootstrap_trade_attempt(symbol, side, quantity, order_type, limit_price, account, False, "No client", 0, None)
            return None

        # Validate inputs
        if order_type.upper() == "LMT" and limit_price is None:
            logger.error(f"Bootstrap LMT order for {symbol} on account {account} requires a limit_price.")
            self._notify_bootstrap_trade_attempt(symbol, side, quantity, order_type, limit_price, account, False, "LMT price missing", 0, None)
            return None

        # Construct minimal order message payload for the C++ bridge
        # This must align with what the C++ bridge's direct "place_order" command expects
        order_msg_payload = {
            "cmd": "place_order",  # Command for the C++ bridge
            "symbol": symbol.upper(),
            "action": side.upper(), # Should be "BUY" or "SELL"
            "quantity": int(quantity),
            "orderType": order_type.upper(), # Should be "MKT" or "LMT"
            "account": account,
            "bootstrap_trade": True,  # Special flag for broker backend / C++ bridge
            "local_copy_id": -999 # Special ID for bootstrap trades in C++ logs, per SME
            # Add other fields the C++ bridge might require for a minimal order,
            # e.g., "outsideRth": True, if that's standard for Lightspeed.
        }
        if order_type.upper() == "LMT" and limit_price is not None:
            order_msg_payload["limitPrice"] = limit_price

        broker_order_id_simulated: Optional[str] = None
        send_successful = False
        error_msg: Optional[str] = None
        latency_ns = 0
        send_start_ns = time.perf_counter_ns()

        try:
            logger.info(f"Attempting BOOTSTRAP trade via C++ bridge: {order_msg_payload}")

            # Direct message send without OrderRepository involvement for this action
            # This send_message_no_read is from LightspeedBridgeClientPlaceholder
            if hasattr(self.client, 'send_message_no_read'):
                send_successful = self.client.send_message_no_read(order_msg_payload)
                if send_successful and hasattr(self.client, '_last_bootstrap_order_id_simulated'):
                    # Retrieve the simulated ID if the mock client provides it
                    broker_order_id_simulated = self.client._last_bootstrap_order_id_simulated
                    self.client._last_bootstrap_order_id_simulated = None # Clear after retrieval
            else:
                error_msg = "Broker client missing 'send_message_no_read' method."
                logger.error(error_msg)
                send_successful = False

            latency_ns = time.perf_counter_ns() - send_start_ns

            if send_successful and broker_order_id_simulated:
                logger.info(f"Bootstrap trade for {symbol} on account {account} sent. Simulated Broker Order ID: {broker_order_id_simulated}. Latency: {latency_ns / 1e6:.2f}ms")
            elif send_successful:
                error_msg = "Bootstrap trade sent, but no broker_order_id was immediately available/simulated by client."
                logger.warning(error_msg)
            else:
                if not error_msg: error_msg = "Failed to send bootstrap trade message to C++ bridge."
                logger.error(error_msg)

        except Exception as e:
            latency_ns = time.perf_counter_ns() - send_start_ns
            error_msg = f"Exception during bootstrap trade send for {symbol} on account {account}: {e}"
            logger.error(error_msg, exc_info=True)
            send_successful = False

        # Notify listeners about the bootstrap trade attempt using a distinct payload
        self._notify_bootstrap_trade_attempt(
            symbol, side, quantity, order_type, limit_price, account,
            send_successful, error_msg, latency_ns, broker_order_id_simulated
        )

        return broker_order_id_simulated if send_successful and broker_order_id_simulated else None

    def _notify_bootstrap_trade_attempt(
        self, symbol: str, side: str, quantity: int, order_type: str,
        limit_price: Optional[float], account: str,
        success: bool, error: Optional[str],
        processing_latency_ns: int, broker_order_id: Optional[str]
    ):
        """Helper to notify listeners about a bootstrap trade attempt."""
        # This uses the existing _notify_broker_listeners but with a custom payload structure
        # to differentiate bootstrap trade events from regular order lifecycle events.
        payload = {
            "broker_response_type": "BootstrapOrderAttempt", # Custom type
            "bootstrap_details": {
                "symbol": symbol,
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type.upper(),
                "limit_price": limit_price,
                "account": account,
                "send_successful": success,
                "broker_order_id_returned": broker_order_id, # If one was simulated/returned
                "error_message": error
            },
            "capture_timestamp": time.time() # Timestamp of this notification
        }
        # The existing _notify_broker_listeners takes (response_data, processing_latency_ns)
        # We are re-purposing response_data here to be our bootstrap payload.
        # The source_component_name is handled within _notify_broker_listeners.
        if hasattr(self, '_notify_broker_listeners'): # Check if the method exists (it should in EnhancedLSB)
             self._notify_broker_listeners(payload, processing_latency_ns) # Call our own method
        else: # Fallback if _notify_broker_listeners isn't available for some reason
             logger.error("Cannot notify bootstrap listeners: _notify_broker_listeners method missing.")

    def _on_order_update(self, broker_response: Dict[str, Any]) -> Any:
        """Enhanced broker response processing with capture observation."""
        if not self._broker_data_listeners:  # Optimization
            return super()._on_order_update(broker_response)

        processing_start_ns = time.perf_counter_ns()
        processing_error: Optional[Exception] = None
        result = None

        try:
            result = super()._on_order_update(broker_response)
        except Exception as e:
            processing_error = e
        finally:
            processing_latency_ns = time.perf_counter_ns() - processing_start_ns
            self._capture_metrics.record_item_processed(
                processing_latency_ns,
                success=(processing_error is None)
            )

            # Construct payload for listener. This is the 'event_payload' for CorrelationLogger.
            # It needs to contain all info for BrokerTimelineEvent.from_correlation_log_entry
            payload_for_listener = {
                "broker_response_type": broker_response.get('event', broker_response.get('status', 'UNKNOWN_BROKER_MSG')),
                "original_order_id": str(broker_response.get('local_copy_id', broker_response.get('ephemeral_corr'))), # Best guess for local ID
                "broker_order_id_on_event": str(broker_response.get('ls_order_id')) if broker_response.get('ls_order_id') is not None else None,
                "broker_data": broker_response, # Pass the whole original dict as 'broker_data'
                "capture_timestamp": time.time() # When this capture hook processed it
            }
            if processing_error:
                payload_for_listener['processing_error'] = str(processing_error)

            self._notify_broker_listeners(payload_for_listener, processing_latency_ns)

        if processing_error:
            raise processing_error
        return result

    def _create_broker_payload(self, broker_response: Dict[str, Any], 
                             processing_latency_ns: int, processing_error: Optional[Exception]) -> Dict[str, Any]:
        """Create comprehensive broker response payload for listeners."""
        payload = broker_response.copy()  # Start with original response
        
        # Add processing metadata
        payload.update({
            'processing_timestamp': time.time(),
            'processing_latency_ns': processing_latency_ns,
            'processing_success': processing_error is None,
            'broker_name': 'Lightspeed'
        })
        
        if processing_error:
            payload['processing_error'] = str(processing_error)
        
        # Enhance with response-type-specific data if available
        response_type = broker_response.get('response_type', broker_response.get('broker_response_type', 'UNKNOWN'))
        
        if response_type in ['FILL', 'PARTIAL_FILL']:
            payload.update({
                'broker_response_type': response_type,
                'original_order_id': broker_response.get('local_order_id', broker_response.get('order_id', '')),
                'broker_data': {
                    'filled_quantity': broker_response.get('filled_quantity', broker_response.get('qty_filled', 0)),
                    'fill_price': broker_response.get('fill_price', broker_response.get('price', 0.0)),
                    'remaining_quantity': broker_response.get('remaining_quantity', broker_response.get('qty_remaining', 0)),
                    'execution_id': broker_response.get('execution_id', broker_response.get('exec_id', '')),
                    'fill_timestamp': broker_response.get('fill_timestamp', broker_response.get('timestamp', time.time()))
                }
            })
        elif response_type in ['ACK', 'ACKNOWLEDGED']:
            payload.update({
                'broker_response_type': response_type,
                'original_order_id': broker_response.get('local_order_id', broker_response.get('order_id', '')),
                'broker_data': {
                    'acknowledged_quantity': broker_response.get('quantity', broker_response.get('qty', 0)),
                    'ack_price': broker_response.get('price', 0.0),
                    'order_status': broker_response.get('status', 'ACKNOWLEDGED'),
                    'ack_timestamp': broker_response.get('timestamp', time.time())
                }
            })
        elif response_type in ['REJECT', 'REJECTED']:
            payload.update({
                'broker_response_type': response_type,
                'original_order_id': broker_response.get('local_order_id', broker_response.get('order_id', '')),
                'broker_data': {
                    'rejection_reason': broker_response.get('rejection_reason', broker_response.get('reason', 'Unknown')),
                    'error_code': broker_response.get('error_code', broker_response.get('code', '')),
                    'rejection_timestamp': broker_response.get('timestamp', time.time()),
                    'attempted_quantity': broker_response.get('quantity', broker_response.get('qty', 0))
                }
            })
        elif response_type in ['CANCEL_ACK', 'CANCELLED']:
            payload.update({
                'broker_response_type': response_type,
                'original_order_id': broker_response.get('local_order_id', broker_response.get('order_id', '')),
                'broker_data': {
                    'cancelled_quantity': broker_response.get('cancelled_quantity', broker_response.get('qty_cancelled', 0)),
                    'remaining_quantity': broker_response.get('remaining_quantity', broker_response.get('qty_remaining', 0)),
                    'cancel_timestamp': broker_response.get('timestamp', time.time()),
                    'cancel_reason': broker_response.get('cancel_reason', 'User requested')
                }
            })
        else:
            # For unknown or other response types, preserve original structure
            payload.update({
                'broker_response_type': response_type,
                'original_order_id': broker_response.get('local_order_id', broker_response.get('order_id', '')),
                'broker_data': {k: v for k, v in broker_response.items() if k not in ['processing_timestamp', 'processing_latency_ns', 'processing_success', 'broker_name']}
            })
        
        return payload

    def _notify_broker_listeners(self, response_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered broker data listeners."""
        if not self._broker_data_listeners:
            return
            
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._broker_data_listeners)
        
        for listener in listeners_snapshot:
            try:
                listener.on_broker_response("EnhancedBroker", response_payload, processing_latency_ns)
                notification_latency_ns = time.perf_counter_ns() - notification_start_ns
                self._capture_metrics.record_notification_attempt(notification_latency_ns, success=True)
            except Exception as e:
                self._capture_metrics.record_notification_attempt(0, success=False)
                logger.error(f"Error notifying Broker listener {type(listener).__name__}: {e}", exc_info=True)

    def get_capture_metrics(self) -> Dict[str, Any]:
        """Get comprehensive broker processing and capture metrics."""
        metrics = self._capture_metrics.get_metrics()
        metrics['listener_count'] = len(self._broker_data_listeners)
        metrics['component_type'] = 'LightspeedBroker'
        return metrics




class EnhancedGenericBrokerInterface:
    """Generic enhanced broker interface for other broker implementations."""
    
    def __init__(self, broker_name: str = "GenericBroker"):
        """Initialize generic enhanced broker interface."""
        self.broker_name = broker_name
        self._broker_data_listeners: List[IBrokerDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()
        logger.info(f"EnhancedGenericBrokerInterface initialized for {broker_name}.")
    
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

    def notify_order_update(self, broker_response: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify listeners that a broker order update was processed."""
        if not self._broker_data_listeners:
            return

        # Create comprehensive broker response payload
        payload = broker_response.copy()
        payload.update({
            'processing_timestamp': time.time(),
            'processing_latency_ns': processing_latency_ns,
            'broker_name': self.broker_name
        })
        
        self._notify_broker_listeners(payload, processing_latency_ns)

    def _notify_broker_listeners(self, response_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        """Notify all registered broker data listeners."""
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock:
            listeners_snapshot = list(self._broker_data_listeners)
        
        for listener in listeners_snapshot:
            try:
                listener.on_broker_response(self.broker_name, response_payload, processing_latency_ns)
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
