import asyncio
import websockets
import json
import threading
import time
import logging
import socket
import copy # Ensure 'copy' is imported
import uuid
from typing import Dict, Set, Optional, Any, List, Callable
from datetime import datetime, timezone
from concurrent.futures import TimeoutError  # Import TimeoutError for future.result() timeout handling

# Import network diagnostics
try:
    from utils.network_diagnostics import log_network_send, log_network_recv, enable_network_diagnostics
except ImportError:
    # Create dummy functions if module not available
    def log_network_send(*args, **kwargs): pass
    def log_network_recv(*args, **kwargs): pass
    def enable_network_diagnostics(*args, **kwargs): pass

# Import for type hinting
try:
    import alpaca_trade_api as tradeapi
except ImportError:
    tradeapi = None  # Type will be handled by Optional['tradeapi.REST']

# Import the receiver interface
from interfaces.data.services import IMarketDataReceiver
from interfaces.core.services import ILifecycleService
from interfaces.logging.services import ICorrelationLogger

# Optional: Import performance tracker if adding detailed timing here
# from utils.performance_tracker import create_timestamp_dict, add_timestamp, calculate_durations

# Module-level logger for static methods or functions
_logger = logging.getLogger(__name__)
# Set logger level to INFO to reduce console spam
_logger.setLevel(logging.ERROR)

DEFAULT_ALPACA_SIP_WS_URL = "wss://stream.data.alpaca.markets/v2/sip"

class PriceFetchingService(ILifecycleService):
    """
    Handles the connection to the Alpaca SIP WebSocket feed (v2),
    manages authentication and subscriptions, parses incoming messages,
    and forwards trade/quote data to a receiver.
    """

    # <<< MODIFY Signature >>> Accept LIST of receivers for two-path architecture
    def __init__(self,
                 config: Dict[str, Any],
                 receiver: IMarketDataReceiver, # MODIFIED: Back to a single receiver
                 initial_symbols: Optional[Set[str]] = None,
                 rest_client: Optional['tradeapi.REST'] = None, 
                 tkinter_root_for_callbacks: Optional[Any] = None,
                 benchmarker: Optional[Any] = None,
                 correlation_logger: ICorrelationLogger = None):
        """
        Initializes the service.

        Args:
            config (Dict[str, Any]): Configuration dictionary containing:
                'key': Alpaca API Key ID
                'secret': Alpaca API Secret Key
                'paper': Boolean indicating paper trading (affects auth) - currently unused by SIP v2 WS, but good practice
                'sip_url': Optional WS URL override (defaults to Alpaca SIP v2)
            receiver (IMarketDataReceiver): Single high-performance receiver (MarketDataDisruptor) that implements
                                           the receiver interface to handle incoming trade and quote data.
            initial_symbols (Optional[Set[str]]): Optional set of symbols to subscribe to initially.
            rest_client (Optional['tradeapi.REST']): Optional Alpaca REST client for fallback price fetching.
            tkinter_root_for_callbacks (Optional[Any]): Optional tkinter root window for thread-safe callbacks.
            correlation_logger (Optional[ICorrelationLogger]): Correlation logger for event logging.
        """
        # Initialize logger as instance variable
        self.logger = logging.getLogger(__name__)
        # Set logger level to ERROR to reduce console spam
        self.logger.setLevel(logging.ERROR)

        # PFS-VERIFY: Log when PriceFetchingService is instantiated
        self.logger.info(f"PFS_INSTANCE_CREATED: ID={id(self)}")

        # --- BEGIN MODIFICATION: Add message counting variables ---
        self._alpaca_msg_count = 0
        self._alpaca_last_log_time = time.time()
        self._alpaca_log_interval = 10  # Log every 10 seconds
        # --- END MODIFICATION ---

        self._config = config
        # MODIFIED: Store the single receiver (which will be the Disruptor)
        self._receiver = receiver
        self._ws_url = config.get('sip_url', DEFAULT_ALPACA_SIP_WS_URL)

        # Store the REST client
        self._rest_client = rest_client
        if self._rest_client:
            self.logger.info("REST client provided for fallback price fetching.")
        else:
            self.logger.info("No REST client provided, fallback price fetching disabled.")

        # ---- STORE THE TKINTER ROOT ----
        self._tkinter_root = tkinter_root_for_callbacks

        # Store PerformanceBenchmarker for metrics
        self._benchmarker = benchmarker
        
        # Store ApplicationCore reference for Redis publishing
        # ApplicationCore dependency removed - using correlation logger
        
        # Store correlation logger
        self._correlation_logger = correlation_logger

        # Ensure required config keys are present
        if not config.get('key') or not config.get('secret'):
            raise ValueError("Alpaca API key and secret must be provided in config.")
        
        # Validate receivers list
        if not receivers:
            raise ValueError("PriceFetchingService requires at least one receiver.")
        
        # Log receiver setup
        receiver_names = [r.__class__.__name__ for r in receivers]
        self.logger.info(f"PriceFetchingService will fan out data to {len(receivers)} receivers: {receiver_names}")

        self._api_key = config['key']
        self._api_secret = config['secret']

        # <<< ADD Lines >>> Initialize _active_symbols set
        self._active_symbols: Set[str] = set(symbol.upper() for symbol in initial_symbols) if initial_symbols else set()
        if self._active_symbols:
            self.logger.info(f"PriceFetchingService initialized with {len(self._active_symbols)} initial symbols.")
        else:
            self.logger.info("PriceFetchingService initialized with no initial symbols.")
        # --- End ADD ---

        # Track confirmed subscriptions from the server
        self._confirmed_subscriptions: Set[str] = set()

        self._ws_thread: Optional[threading.Thread] = None
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event() # Signal to stop the thread/loop
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None # Current WebSocket connection
        self._lock = threading.Lock() # Lock for accessing shared state like _active_symbols if modified externally

        # Connection status tracking
        self._is_connected_and_authenticated: bool = False
        self._last_data_message_ts: float = 0.0
        self._status_change_callback: Optional[Callable[[Dict[str, Any]], None]] = None # For GUI

        if self._tkinter_root:
            self.logger.info("PriceFetchingService initialized with tkinter_root for status callbacks.")
        else:
            # Silently initialize without tkinter_root - this is normal for headless operation
            pass

        self.logger.info(f"PriceFetchingService initialized for URL: {self._ws_url}")
        self.logger.info(f"PriceFetchingService initialized. It will produce data for: {self._receiver.__class__.__name__}")

    # Legacy set_receiver method removed - now uses list of receivers in constructor

    @property
    def is_ready(self) -> bool:
        """Returns True if the service is fully initialized and ready to operate."""
        return (self._ws_thread is not None and 
                self._ws_thread.is_alive() and 
                self._is_connected_and_authenticated and 
                not self._stop_event.is_set())

    def start(self):
        """Starts the background WebSocket connection thread."""
        # A1-Verify: Add special logging to confirm this method is called
        self.logger.info("PFS_START_CALLED: PriceFetchingService.start() method has been entered.")

        if self._ws_thread and self._ws_thread.is_alive():
            self.logger.warning("WebSocket thread already running.")
            return

        self._stop_event.clear()
        self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True, name="PriceServiceWSThread")
        self._ws_thread.start()
        self.logger.info("WebSocket background thread started.")
        
        # INSTRUMENTATION: Publish service health status - STARTED
        self._publish_service_health_status(
            "STARTED",
            "Price Fetching Service started successfully",
            {
                "service_name": "PriceFetchingService",
                "ws_url": self._ws_url,
                "initial_symbols_count": len(self._active_symbols),
                "receiver_available": self._receivers is not None and len(self._receivers) > 0,
                "rest_client_available": self._rest_client is not None,
                "ws_thread_alive": self._ws_thread and self._ws_thread.is_alive(),
                "health_status": "HEALTHY",
                "startup_timestamp": time.time()
            }
        )

    def stop(self):
        self.logger.info("PriceFetchingService.stop() called.")
        if self._stop_event.is_set() and (not self._ws_thread or not self._ws_thread.is_alive()):
            self.logger.info("Stop event already set and thread already stopped/gone.")
            return

        self._stop_event.set() # Ensure it's set
        self.logger.info("Stop event set for PriceFetchingService.")

        # Attempt to close the WebSocket from its own loop by waking it up if needed
        # The loop should see _stop_event and then the 'finally' block of 'async with' or its own 'finally' should close ws.
        # If the loop is truly stuck on ws.recv(), scheduling another coro might not run immediately.
        # The primary mechanism for shutdown is _stop_event causing the loop to terminate.

        if self._ws_loop and self._ws_loop.is_running():
            self.logger.info(f"Asyncio loop {id(self._ws_loop)} is running. Scheduling websocket close.")
            if self._websocket and self._websocket.open:
                self.logger.info("WebSocket object exists and is open. Scheduling close task.")
                # Schedule close but don't wait for it here with future.result()
                future = asyncio.run_coroutine_threadsafe(
                    self._websocket.close(code=1000, reason="Client shutting down by stop()"),
                    self._ws_loop
                )
                try:
                    future.result(timeout=1.0) # Give it a very short time to execute
                    self.logger.info("WebSocket close task scheduled and completed/timed out quickly.")
                except TimeoutError:
                    self.logger.warning("WebSocket close task scheduling timed out waiting for result.")
                except Exception as e_close_sched:
                    self.logger.error(f"Exception scheduling websocket close: {e_close_sched}")
            else:
                self.logger.info("No open WebSocket to explicitly schedule close for in stop(). Loop should handle.")

            # --- REMOVED: Direct call to self._ws_loop.stop() ---
            # The _stop_event being set, along with the _websocket.close() coroutine being scheduled,
            # should be enough to signal the _ws_connect_subscribe_loop to terminate.
            # The loop will then complete, and run_until_complete will return normally.
        else:
            self.logger.info("Asyncio loop not running or not initialized during stop().")

        if self._ws_thread and self._ws_thread.is_alive():
            self.logger.info(f"Joining WebSocket thread: {self._ws_thread.name} (timeout 5s)...")
            self._ws_thread.join(timeout=5.0)
            if self._ws_thread.is_alive():
                self.logger.warning(f"WebSocket thread ({self._ws_thread.name}) DID NOT STOP CLEANLY after join timeout. It might be stuck.")
                # At this point, the thread is still running.
                # If the loop is Python's, it might be forcibly terminated when the main program exits if it's a daemon.
            else:
                self.logger.info(f"WebSocket thread ({self._ws_thread.name}) joined successfully.")
        else:
            self.logger.info("WebSocket thread was not running or already joined when stop() was called.")

        self._ws_thread = None
        # self._ws_loop should be closed by _run_ws_loop's finally block
        self._websocket = None

        self.logger.info("PriceFetchingService stop() method finished.")
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        self._publish_service_health_status(
            "STOPPED",
            "Price Fetching Service stopped and cleaned up resources",
            {
                "service_name": "PriceFetchingService",
                "ws_thread_stopped": not (self._ws_thread and self._ws_thread.is_alive()),
                "confirmed_subscriptions_count": len(getattr(self, '_confirmed_subscriptions', set())),
                "messages_received": getattr(self, '_alpaca_msg_count', 0),
                "stop_reason": "manual_stop",
                "health_status": "STOPPED",
                "shutdown_timestamp": time.time()
            }
        )

    def get_connection_status(self) -> dict:
        """
        Returns the current status of the WebSocket connection.

        Returns:
            dict: A dictionary containing connection status information
        """
        status = {
            "thread_running": self._ws_thread is not None and self._ws_thread.is_alive(),
            "websocket_connected": self._websocket is not None,
            "stop_event_set": self._stop_event.is_set(),
            "active_symbols_count": len(self._active_symbols),
            "timestamp": time.time(),
            "is_connected_and_authenticated": self._is_connected_and_authenticated,
            "last_data_message_ts": self._last_data_message_ts
        }

        self.logger.info(f"WebSocket connection status: {status}")
        return status

    def get_connection_status_details(self) -> Dict[str, Any]:
        """
        Returns detailed information about the current connection status.

        Returns:
            Dict with connection status details
        """
        connected_to_server = self._websocket is not None
        websocket_is_open = self._websocket.open if self._websocket else False

        # Calculate time since last data message
        time_since_last_data = 0.0
        if self._last_data_message_ts > 0:
            time_since_last_data = time.time() - self._last_data_message_ts

        # Determine status message based on connection state
        status_message = "Disconnected"
        if connected_to_server:
            if self._is_connected_and_authenticated:
                if time_since_last_data > 5.0:
                    status_message = f"Connected but data stale ({time_since_last_data:.1f}s)"
                else:
                    status_message = "Fully operational"
            elif websocket_is_open:
                status_message = "Connected but not fully authenticated/subscribed"
            else:
                status_message = "TCP connected but WebSocket not open"

        return {
            "connected_to_server": connected_to_server,
            "authenticated": self._is_connected_and_authenticated,  # This is now "fully operational"
            "subscribed_and_streaming": self._is_connected_and_authenticated,
            "is_fully_operational": self._is_connected_and_authenticated,
            "last_data_message_ts": self._last_data_message_ts,
            "time_since_last_data": time_since_last_data,
            "status_message_from_service": status_message,
            "websocket_is_open": websocket_is_open,
            "active_symbols_count": len(self._active_symbols),
            "confirmed_subscriptions_count": len(self._confirmed_subscriptions),
            "thread_running": self._ws_thread is not None and self._ws_thread.is_alive(),
            "stop_event_set": self._stop_event.is_set()
        }

    def set_status_change_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """
        Sets the callback function for status changes.

        If tkinter_root was provided in __init__, callbacks will be scheduled on the tkinter main thread
        using tkinter_root.after(0, ...), ensuring thread-safety for GUI updates.
        Otherwise, callbacks will run directly on the WebSocket thread, which may cause GUI freezes or crashes.

        Args:
            callback: Function to call with status updates. Should accept a dictionary with connection status details.
        """
        self._status_change_callback = callback
        self.logger.info("Status change callback set for PriceFetchingService.")


    def _update_connection_status(self,
                             connected_to_server: bool,
                             authenticated: bool,
                             subscribed_and_streaming: bool,
                             status_message: str = ""):
        # Service is ready if connected and authenticated
        # Streaming is only required if we have symbols to stream
        has_symbols_to_stream = len(self._active_symbols) > 0
        new_overall_status = connected_to_server and authenticated and (subscribed_and_streaming or not has_symbols_to_stream)

        # Log what status we are about to send
        self.logger.debug(f"PFS Status Update: Conn={connected_to_server}, Auth={authenticated}, " # CHANGED to DEBUG
                     f"Stream={subscribed_and_streaming}, FullyOp={new_overall_status}, Msg='{status_message}'")

        # Determine overall operational status
        self._is_connected_and_authenticated = connected_to_server and authenticated

        # --- REMOVED THIS IF BLOCK ---
        # if subscribed_and_streaming:
        #     self._last_data_message_ts = time.time()
        # --- END REMOVAL ---

        if self._status_change_callback:
            details = {
                "connected_to_server": connected_to_server,
                "authenticated": authenticated,
                "subscribed_and_streaming": subscribed_and_streaming, # Reflects if we *should* be streaming
                "is_fully_operational": self._is_connected_and_authenticated,
                "last_data_message_ts": self._last_data_message_ts, # Now accurately reflects last MARKET data time
                "status_message_from_service": status_message,
                "websocket_is_open": self._websocket.open if self._websocket else False
            }
            try:
                self.logger.debug(f"PFS: Invoking status_change_callback with: {details}")

                # Send a deepcopy of details to avoid modification issues if details is complex/mutable
                final_details_to_send = copy.deepcopy(details)

                if self._tkinter_root and hasattr(self._tkinter_root, 'winfo_exists') and self._tkinter_root.winfo_exists():
                    try:
                        self.logger.debug(f"PFS: Scheduling status_change_callback on GUI thread. Details: {final_details_to_send}")
                        # Use lambda to correctly capture the current 'final_details_to_send'
                        self._tkinter_root.after(0, lambda d=final_details_to_send: self._status_change_callback(d))
                    except Exception as e_after:
                        self.logger.error(f"PFS: Error scheduling status_change_callback with root.after: {e_after}. "
                                          f"Calling directly from {threading.current_thread().name} as fallback (VERY RISKY).", exc_info=True)
                        try:
                            self._status_change_callback(final_details_to_send) # Fallback direct call
                        except Exception as e_direct_fallback:
                            self.logger.error(f"PFS: Error in direct fallback status_change_callback from {threading.current_thread().name}: {e_direct_fallback}", exc_info=True)
                else:  # This means _tkinter_root is None or destroyed
                    # Silently skip GUI status updates when tkinter_root is not available
                    # This is normal during shutdown or when running headless
                    pass
                    # DO NOT make a direct call to self._status_change_callback here
                    # if the root is gone. The callback is for the GUI.
            except Exception as cb_ex:
                self.logger.error(f"Error in status_change_callback: {cb_ex}", exc_info=True)

    def subscribe_symbol(self, symbol: str):
        """
        Adds a symbol to the active subscription list and sends a subscribe
        message to the live WebSocket if connected.
        """
        symbol_upper = symbol.upper()
        with self._lock:
            if symbol_upper not in self._active_symbols:
                self._active_symbols.add(symbol_upper)
                self.logger.critical(f"PFS_DYNAMIC_SUB: Symbol {symbol_upper} added to subscription list. Attempting to send live subscribe message.")
                
                # If we are already connected, send a subscribe message immediately.
                # This requires careful handling of the asyncio loop from a different thread.
                if self._ws_loop and self._websocket and self._websocket.open:
                    self.logger.critical(f"PFS_DYNAMIC_SUB_SENDING: WebSocket connected. Sending live subscription for {symbol_upper}.")
                    asyncio.run_coroutine_threadsafe(
                        self._send_dynamic_subscription([symbol_upper]), 
                        self._ws_loop
                    )
                else:
                    self.logger.critical(f"PFS_DYNAMIC_SUB_DEFERRED: WebSocket not connected (loop={self._ws_loop is not None}, ws={self._websocket is not None}, open={self._websocket.open if self._websocket else False}). {symbol_upper} will be included in next full subscription on reconnect.")
            else:
                self.logger.debug(f"PFS: Symbol {symbol_upper} already in subscription list.")

    def unsubscribe_symbol(self, symbol: str):
        """Removes a symbol from the active subscription list."""
        symbol_upper = symbol.upper()
        with self._lock:
            if symbol_upper in self._active_symbols:
                self._active_symbols.discard(symbol_upper)
                self.logger.info(f"Symbol {symbol_upper} removed from subscription list.")
                # TODO: Implement dynamic unsubscription logic if needed later
                # Example (Conceptual):
                # if self._ws_loop and self._websocket and self._websocket.open:
                #    asyncio.run_coroutine_threadsafe(self._send_dynamic_unsubscription([symbol_upper]), self._ws_loop)
            else:
                self.logger.debug(f"Symbol {symbol_upper} not in subscription list.")

    async def _send_dynamic_subscription(self, symbols: List[str]):
        """
        Coroutine to send a subscription message for new symbols to an active WebSocket.
        """
        if not self._websocket or not self._websocket.open:
            self.logger.critical("PFS_DYNAMIC_SUB_FAILED: Cannot send dynamic subscription, WebSocket is not open.")
            return

        try:
            sub_msg = { "action": "subscribe", "trades": symbols, "quotes": symbols }
            sub_json = json.dumps(sub_msg)
            
            self.logger.critical(f"PFS_DYNAMIC_SUB_SENDING_MSG: Sending live subscription for: {symbols} - Message: {sub_json}")
            await self._websocket.send(sub_json)
            self.logger.critical(f"PFS_DYNAMIC_SUB_SUCCESS: Live subscription message sent successfully for {symbols}.")
        except Exception as e:
            self.logger.critical(f"PFS_DYNAMIC_SUB_ERROR: Error sending dynamic subscription for {symbols}: {e}", exc_info=True)

    def _run_ws_loop(self):
        """Target function for the background thread. Sets up and runs the asyncio loop."""
        # A1-Verify: Add special logging to confirm this method is called
        self.logger.info("PFS_RUN_WS_LOOP_CALLED: PriceFetchingService._run_ws_loop() method has been entered.")
        self.logger.info(f"PriceServiceWSThread: Starting new asyncio event loop.")
        try:
            self._ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            self.logger.info(f"PriceServiceWSThread: Event loop created and set: {id(self._ws_loop)}")
            self._ws_loop.run_until_complete(self._ws_connect_subscribe_loop())
        except Exception as e:
            self.logger.exception(f"PriceServiceWSThread: Exception in _run_ws_loop: {e}")
        finally:
            if self._ws_loop: # Check if loop object exists
                self.logger.info(f"PriceServiceWSThread: Entering finally block for asyncio event loop: {id(self._ws_loop)}")
                if not self._ws_loop.is_closed(): # Only try to close if not already closed
                    try:
                        # Stop the loop if it's still running (might be redundant if stop() called it)
                        if self._ws_loop.is_running():
                            self.logger.info("PFS _run_ws_loop finally: Loop is running, calling stop().")
                            self._ws_loop.stop()

                        # Gather and cancel all pending tasks
                        pending_tasks = asyncio.all_tasks(loop=self._ws_loop)
                        if pending_tasks:
                            self.logger.info(f"PFS _run_ws_loop finally: Cancelling {len(pending_tasks)} pending asyncio tasks...")
                            for task in pending_tasks:
                                if not task.done(): # Only cancel if not already done
                                    task.cancel()
                            # Run loop for a moment to allow cancellations to process
                            # This needs to be done carefully to avoid blocking if loop can't process.
                            try:
                                self._ws_loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))
                                self.logger.info("PFS _run_ws_loop finally: Pending tasks processed after cancellation.")
                            except RuntimeError as re_gather: # e.g. "cannot call run_until_complete from a running event loop"
                                self.logger.warning(f"PFS _run_ws_loop finally: RuntimeError during task gathering: {re_gather}. Loop state: running={self._ws_loop.is_running()}, closed={self._ws_loop.is_closed()}")
                                # If loop is not running, we might not be able to run_until_complete
                                # This part is tricky; the goal is graceful cleanup.

                    except Exception as task_cancel_err:
                        self.logger.error(f"PFS _run_ws_loop finally: Error cancelling pending tasks: {task_cancel_err}", exc_info=True)
                    finally:
                        if not self._ws_loop.is_closed():
                            self._ws_loop.close()
                            self.logger.info(f"PFS _run_ws_loop finally: Asyncio event loop {id(self._ws_loop)} explicitly closed.")
                        else:
                            self.logger.info(f"PFS _run_ws_loop finally: Asyncio event loop {id(self._ws_loop)} was already closed.")
                else:
                    self.logger.info(f"PFS _run_ws_loop finally: Asyncio event loop {id(self._ws_loop)} was already closed when finally block entered.")
            self.logger.info("PriceServiceWSThread: _run_ws_loop finished.")

    async def _ws_connect_subscribe_loop(self):
        self.logger.info(f"PriceServiceWSThread: _ws_connect_subscribe_loop started on loop {id(asyncio.get_running_loop())}.")
        while not self._stop_event.is_set(): # OUTER RECONNECT LOOP
            self._update_connection_status(False, False, False, "Attempting Connect")
            self._websocket = None
            ws_connection_instance = None

            try:
                # Check stop event before attempting connection
                if self._stop_event.is_set():
                    self.logger.info("Stop event detected before connection attempt. Exiting loop.")
                    break

                self.logger.info(f"Attempting WebSocket connection to {self._ws_url}...")
                ws_connection_instance = await asyncio.wait_for(
                    websockets.connect(self._ws_url, ping_interval=20, ping_timeout=30, close_timeout=5), # Added close_timeout
                    timeout=10.0
                )
                self._websocket = ws_connection_instance

                # Check stop event after connection
                if self._stop_event.is_set():
                    self.logger.info("Stop event detected after connection. Closing and exiting loop.")
                    await asyncio.wait_for(ws_connection_instance.close(code=1000, reason="Stop event after connect"), timeout=2.0)
                    break

                self._update_connection_status(True, False, False, "TCP Connected")
                self.logger.critical("PFS_CONN_SUCCESS: WebSocket TCP connection ESTABLISHED.")

                # --- Authentication ---
                auth_msg = { "action": "auth", "key": self._api_key, "secret": self._api_secret }
                auth_json = json.dumps(auth_msg)
                data_size = len(auth_json.encode("utf-8"))

                # AGGRESSIVE NETWORK LOGGING
                self.logger.critical(f"PFS_AUTH_SEND: Sending auth message: {auth_json}")
                self.logger.critical(f"NET_SEND_ALPACA: Method='send', Command='auth', PayloadSize={data_size}, Thread='{threading.current_thread().name}'")

                # Network diagnostics
                log_network_send("ALPACA_WS_AUTH", auth_msg)

                await ws_connection_instance.send(auth_json)
                self.logger.critical("PFS_AUTH_SENT: Auth message has been sent.")

                # --- Wait for Auth Confirmation ---
                # Instead of optimistically setting authenticated, wait for server response
                self.logger.critical("PFS_AUTH_WAIT: Now waiting for server response to auth...")
                try:
                    response = await asyncio.wait_for(ws_connection_instance.recv(), timeout=10.0)
                    self.logger.critical(f"PFS_AUTH_RECV: Received first message from server: {response}")

                    # Parse the response to check for authentication success
                    try:
                        response_data = json.loads(response)
                        if isinstance(response_data, list) and len(response_data) > 0:
                            first_msg = response_data[0]
                            if first_msg.get('T') == 'success':
                                msg_content = first_msg.get('msg', '')
                                if 'authenticated' in msg_content:
                                    self.logger.critical("PFS_AUTH_SUCCESS: Authentication CONFIRMED by server.")
                                    self._update_connection_status(True, True, False, "Auth Confirmed")
                                elif 'connected' in msg_content:
                                    self.logger.critical("PFS_AUTH_CONNECTED: Server confirmed connection, waiting for authentication...")
                                    # Wait for the actual authentication confirmation
                                    auth_response = await asyncio.wait_for(ws_connection_instance.recv(), timeout=10.0)
                                    self.logger.critical(f"PFS_AUTH_RECV2: Received second message from server: {auth_response}")
                                    
                                    auth_data = json.loads(auth_response)
                                    if isinstance(auth_data, list) and len(auth_data) > 0:
                                        auth_msg = auth_data[0]
                                        if auth_msg.get('T') == 'success' and 'authenticated' in auth_msg.get('msg', ''):
                                            self.logger.critical("PFS_AUTH_SUCCESS: Authentication CONFIRMED by server.")
                                            self._update_connection_status(True, True, False, "Auth Confirmed")
                                        else:
                                            self.logger.critical(f"PFS_AUTH_FAILURE: Expected authentication confirmation but got: {auth_msg}")
                                            await ws_connection_instance.close()
                                            continue
                                    else:
                                        self.logger.critical(f"PFS_AUTH_FAILURE: Invalid second response format: {auth_data}")
                                        await ws_connection_instance.close()
                                        continue
                                else:
                                    self.logger.critical(f"PFS_AUTH_UNEXPECTED: Unexpected success message: {msg_content}")
                                    await ws_connection_instance.close()
                                    continue
                            elif first_msg.get('T') == 'error':
                                self.logger.critical(f"PFS_AUTH_FAILURE: Server returned an error: {first_msg}")
                                await ws_connection_instance.close()
                                continue
                            else:
                                self.logger.critical(f"PFS_AUTH_UNEXPECTED: Received unexpected message type after auth: {first_msg}")
                                await ws_connection_instance.close()
                                continue
                        else:
                            self.logger.critical(f"PFS_AUTH_FAILURE: Invalid response format: {response_data}")
                            await ws_connection_instance.close()
                            continue

                    except json.JSONDecodeError:
                        self.logger.critical(f"PFS_AUTH_ERROR: Server response was not valid JSON: {response}")
                        await ws_connection_instance.close()
                        continue

                except asyncio.TimeoutError:
                    self.logger.critical("PFS_AUTH_TIMEOUT: Timed out waiting for authentication response from server.")
                    await ws_connection_instance.close()
                    continue

                # Check stop event after authentication
                if self._stop_event.is_set():
                    self.logger.info("Stop event detected after authentication. Closing and exiting loop.")
                    await asyncio.wait_for(ws_connection_instance.close(code=1000, reason="Stop event after auth"), timeout=2.0)
                    break

                # --- Subscription ---
                # Only proceed with subscription if authentication was successful
                if self._is_connected_and_authenticated:
                    self.logger.critical("PFS_SUB_SEND: Proceeding with subscription...")
                    with self._lock:
                        symbols_to_subscribe = list(self._active_symbols)
                        self.logger.info(f"PriceFetchingService: Subscribing to {len(symbols_to_subscribe)} symbols")
                    if symbols_to_subscribe:
                        sub_msg = { "action": "subscribe", "trades": symbols_to_subscribe, "quotes": symbols_to_subscribe }
                        sub_json = json.dumps(sub_msg)
                        data_size = len(sub_json.encode("utf-8"))

                        # AGGRESSIVE NETWORK LOGGING
                        self.logger.critical(f"NET_SEND_ALPACA: Method='send', Command='subscribe', PayloadSize={data_size}, SymbolCount={len(symbols_to_subscribe)}, Thread='{threading.current_thread().name}'")

                        # Network diagnostics
                        log_network_send("ALPACA_WS_SUBSCRIBE", sub_msg)

                        await ws_connection_instance.send(sub_json)
                        self.logger.critical("PFS_SUB_SENT: Subscription message sent.")
                        # Optimistically consider subscribed after sending (market may be closed, no confirmation expected)
                        self._update_connection_status(True, True, True, "Sub Sent")
                    else:
                        self.logger.warning("No symbols to subscribe to initially.")
                        # If no symbols, we might still be "authenticated" but not "streaming"
                        self._update_connection_status(True, True, False, "Auth OK, No Subs")
                else:
                    self.logger.critical("PFS_SUB_SKIPPED: Authentication failed, skipping subscription.")
                    await ws_connection_instance.close()
                    continue

                # Check stop event after subscription
                if self._stop_event.is_set():
                    self.logger.info("Stop event detected after subscription. Closing and exiting loop.")
                    await asyncio.wait_for(ws_connection_instance.close(code=1000, reason="Stop event after subscribe"), timeout=2.0)
                    break

                # --- Receive Loop ---
                self.logger.info("Entering receive loop...")
                while not self._stop_event.is_set(): # INNER RECEIVE LOOP
                    if not ws_connection_instance or ws_connection_instance.closed:
                        self.logger.warning("PFS Receive loop: WebSocket is already closed or None. Breaking inner loop.")
                        break # Exit immediately
                    try:
                        message = None
                        try:
                            # Try to receive with a very short timeout
                            message = await asyncio.wait_for(ws_connection_instance.recv(), timeout=0.1)
                        except asyncio.TimeoutError:
                            if self._stop_event.is_set():
                                self.logger.info("PFS: Stop event detected during short recv timeout (0.1s). Breaking inner loop.")
                                break # Primary exit path when stopping
                            await asyncio.sleep(0.05) # Brief pause, then re-check _stop_event at loop top
                            continue # Re-check _stop_event at the start of the while

                        # If a message was received
                        if message:
                            if self._stop_event.is_set(): # Check after a successful recv
                                self.logger.info("PFS: Stop event detected post-recv. Breaking inner loop.")
                                break
                            await self._handle_message(message) # Process the message

                        # Stale data check can remain, but ensure it doesn't call _update_connection_status if _stop_event is set
                        if self._is_connected_and_authenticated and \
                           self._last_data_message_ts > 0 and \
                           (time.time() - self._last_data_message_ts > 10.0) and \
                           not self._stop_event.is_set(): # <<< ADDED STOP CHECK HERE
                             self.logger.warning(f"PFS: WebSocket seems live but no data for >10s for {len(self._active_symbols)} symbols.")
                             self._update_connection_status(True, True, True, "Data Potentially Stale")

                    except websockets.exceptions.ConnectionClosed as cc_exc:
                        self.logger.warning(f"PFS: WebSocket connection closed during recv: Code={cc_exc.code}, Reason='{cc_exc.reason}'")
                        if not self._stop_event.is_set(): # Only update status if not already stopping
                            self._update_connection_status(False, False, False, f"WS ClosedRecv:{cc_exc.code}")
                        break # Exit inner receive loop
                    except Exception as msg_err:
                        self.logger.exception(f"PFS: Error in recv loop: {msg_err}")
                        if not self._stop_event.is_set(): # Only update status if not already stopping
                            self._update_connection_status(False, False, False, f"Receive Loop Error: {type(msg_err).__name__}")
                        break # Exit inner receive loop on other errors too for safety

                self.logger.info(f"PFS: Exited inner receive loop. Stop event is: {self._stop_event.is_set()}")

                # After inner loop exits (due to break or _stop_event)
                if self._stop_event.is_set():
                    self.logger.info("Receive loop (inner while) exited due to stop_event.")
                else:
                    self.logger.info("Receive loop exited for other reason (e.g. ConnectionClosed).")

                # 'async with' is not used, so explicit close is needed if ws_connection_instance is still open
                if ws_connection_instance and not ws_connection_instance.closed:
                    self.logger.info("Inner loop exited, attempting to close ws_connection_instance.")
                    try:
                        await asyncio.wait_for(ws_connection_instance.close(code=1000, reason="Inner loop exit"), timeout=2.0)
                        self.logger.info("WebSocket closed after inner loop exit.")
                    except Exception as e_inner_close:
                        self.logger.error(f"Error closing WebSocket after inner loop exit: {e_inner_close}")

            except asyncio.TimeoutError: # Timeout on websockets.connect() or the ws.close() in finally
                self.logger.warning(f"Timeout during WebSocket connect/close for {self._ws_url}")
            except (websockets.exceptions.WebSocketException, ConnectionRefusedError, socket.gaierror, OSError) as conn_err:
                self.logger.warning(f"WebSocket connection/establishment error for {self._ws_url}: {type(conn_err).__name__} - {conn_err}")
            except Exception as e:
                self.logger.exception(f"Unexpected error in WebSocket _ws_connect_subscribe_loop (outer): {e}")
            finally:
                self.logger.info(f"PFS: Entering _ws_connect_subscribe_loop finally block. WebSocket is: {'Open' if ws_connection_instance and ws_connection_instance.open else 'Closed/None'}")
                if ws_connection_instance and ws_connection_instance.open: # Check if it's still open
                    self.logger.info(f"PFS: WebSocket for {self._ws_url} is still open in outer finally, attempting explicit close...")
                    try:
                        await asyncio.wait_for(ws_connection_instance.close(code=1001, reason="Outer loop finally"), timeout=1.0)
                        self.logger.info(f"PFS: WebSocket for {self._ws_url} closed in outer finally.")
                    except Exception as e_close_finally:
                        self.logger.error(f"PFS: Error closing WebSocket in outer finally for {self._ws_url}: {e_close_finally}")

                self._websocket = None # Clear self._websocket reference
                if not self._stop_event.is_set(): # Only call status update if we are not already stopping due to external signal
                    self._update_connection_status(False, False, False, "Connection Cycle Ended")

                if not self._stop_event.is_set():
                    wait_time = 5.0
                    self.logger.info(f"Waiting {wait_time} seconds before next WebSocket connection attempt...")
                    try:
                        # This sleep needs to be interruptible if _stop_event is set
                        for _ in range(int(wait_time / 0.1)): # Check stop event frequently
                            if self._stop_event.is_set():
                                self.logger.info("Stop event detected during reconnect sleep. Exiting loop.")
                                break
                            await asyncio.sleep(0.1)
                    except asyncio.CancelledError:
                        self.logger.info("Reconnect sleep cancelled, likely due to shutdown.")
                        break
                else:
                    self.logger.info("Stop event is set. Not attempting WebSocket reconnect.")

                # Final check before continuing the outer loop
                if self._stop_event.is_set():
                    self.logger.info("Stop event detected at end of finally block. Breaking outer loop.")
                    break

        self.logger.info("Exiting WebSocket connection loop (_ws_connect_subscribe_loop fully).")

    async def _handle_message(self, message_str: str):
        """Parses incoming JSON messages and calls the receiver."""
        # Start timing for performance metrics
        start_time = time.time()

        # --- BEGIN MODIFICATION: Add message counting ---
        # Increment message count
        self._alpaca_msg_count += 1

        # Log message rate periodically
        current_time = time.time()
        if current_time - self._alpaca_last_log_time > self._alpaca_log_interval:
            rate = self._alpaca_msg_count / (current_time - self._alpaca_last_log_time)
            self.logger.info(f"AlpacaStream: Received {self._alpaca_msg_count} msgs in last {current_time - self._alpaca_last_log_time:.1f}s (Rate: {rate:.1f} msg/s)")

            # Capture market data rate metric (Enhanced with convenience method)
            if self._benchmarker:
                self._benchmarker.gauge(
                    "price_fetching.market_data_rate_per_second",
                    rate,
                    context={'source': 'alpaca_sip', 'interval_seconds': self._alpaca_log_interval}
                )
            self._alpaca_msg_count = 0
            self._alpaca_last_log_time = current_time
        # --- END MODIFICATION ---

        # AGGRESSIVE NETWORK LOGGING - DISABLED TO REDUCE CONSOLE SPAM
        # data_size = len(message_str.encode("utf-8"))
        # self.logger.critical(f"NET_RECV_ALPACA: Method='_handle_message', PayloadSize={data_size}, Thread='{threading.current_thread().name}'")

        # Network diagnostics
        log_network_recv("ALPACA_WS_RECV", message_str)

        try:
            # Don't log raw messages to reduce console spam
            # self.logger.debug(f"Raw WebSocket message received: {message_str[:200]}...")

            # Time JSON parsing for performance metrics
            parse_start_time = time.time()
            data = json.loads(message_str)
            parse_time_ms = (time.time() - parse_start_time) * 1000

            # Capture JSON parsing time metric (Enhanced with convenience method)
            if self._benchmarker:
                self._benchmarker.gauge(
                    "price_fetching.market_data_parse_time_ms",
                    parse_time_ms,
                    context={'source': 'alpaca_sip', 'message_size_bytes': len(message_str)}
                )
        except json.JSONDecodeError:
            self.logger.warning(f"Received non-JSON message: {message_str[:500]}")
            return
        except Exception as e:
            self.logger.error(f"Error decoding JSON message: {e}", exc_info=True)
            return

        # Alpaca v2 messages are lists of objects
        if not isinstance(data, list):
            self.logger.warning(f"Received unexpected message format (not a list): {data}")
            return

        # Don't log the number of items to reduce console spam
        # self.logger.debug(f"Processing WebSocket message with {len(data)} items")

        # Track if any data messages were processed
        is_data_message_processed = False

        for item in data:
            try:
                msg_type = item.get("T")
                symbol = item.get("S")

                # Don't log message type to reduce console spam
                # self.logger.debug(f"Processing WebSocket message type: {msg_type}, Symbol: {symbol}")

                if not msg_type or not symbol:
                    # Could be control messages like subscription confirmations
                    if msg_type == "success" and item.get("msg") == "subscribed":
                        self.logger.info(f"PRICE_FETCHING_SERVICE: Subscription success message received: {item}")
                        subscribed_trades = item.get("trades", [])
                        subscribed_quotes = item.get("quotes", [])
                        # ... (other channels like bars if you use them)

                        current_confirmed_count = len(self._confirmed_subscriptions)
                        newly_confirmed_this_message = set()

                        # Define symbols to specifically check for
                        symbols_to_check = ["KDLY", "ATXG", "CING"]

                        if subscribed_trades:
                            self.logger.info(f"Successfully subscribed to TRADES for: {subscribed_trades}")
                            newly_confirmed_this_message.update(subscribed_trades)

                            # Check if specific symbols are in the trades subscription
                            for sym in symbols_to_check:
                                if sym in subscribed_trades:
                                    self.logger.critical(f"CONFIRMED: Symbol {sym} is in TRADES subscription")
                                else:
                                    self.logger.critical(f"MISSING: Symbol {sym} is NOT in TRADES subscription")

                        if subscribed_quotes:
                            self.logger.info(f"Successfully subscribed to QUOTES for: {subscribed_quotes}")
                            newly_confirmed_this_message.update(subscribed_quotes)

                            # Check if specific symbols are in the quotes subscription
                            for sym in symbols_to_check:
                                if sym in subscribed_quotes:
                                    self.logger.critical(f"CONFIRMED: Symbol {sym} is in QUOTES subscription")
                                else:
                                    self.logger.critical(f"MISSING: Symbol {sym} is NOT in QUOTES subscription")

                        if newly_confirmed_this_message:
                            with self._lock: # Protect _confirmed_subscriptions
                                self._confirmed_subscriptions.update(newly_confirmed_this_message)
                            self.logger.info(f"Total confirmed subscriptions now: {len(self._confirmed_subscriptions)} (was {current_confirmed_count}). Just added: {list(newly_confirmed_this_message)}")

                        # Determine if streaming based on confirmed subscriptions
                        is_streaming_now = len(self._confirmed_subscriptions) > 0 and len(self._active_symbols) > 0
                        self._update_connection_status(
                            connected_to_server=True,
                            authenticated=True, # Assuming auth must happen before subs
                            subscribed_and_streaming=is_streaming_now,
                            status_message="Subscription Update Received"
                        )

                        # REMOVED: is_data_message_processed = True for control messages
                        # Only set is_data_message_processed = True for actual market data (T/Q)
                    elif msg_type == "success" and item.get("msg") == "authenticated":
                         # Should not happen with SIP v2, but handle just in case
                         self.logger.info("Explicit authentication confirmation received.")
                         self._update_connection_status(
                             connected_to_server=True,
                             authenticated=True,
                             subscribed_and_streaming=False, # Not yet streaming data
                             status_message="Authenticated"
                         )
                         # REMOVED: is_data_message_processed = True for control messages
                         # Only set is_data_message_processed = True for actual market data (T/Q)
                    elif msg_type == "success" and item.get("msg") == "connected":
                         # Initial connection ack
                         self.logger.info("WebSocket 'connected' message (initial handshake with server).")
                         # This is pre-auth/sub, so connected=True, auth=False, streaming=False
                         self._update_connection_status(
                             connected_to_server=True,
                             authenticated=False, # Not yet authenticated
                             subscribed_and_streaming=False, # Not yet streaming
                             status_message="Server Connected"
                         )
                         # REMOVED: is_data_message_processed = True for control messages
                         # Only set is_data_message_processed = True for actual market data (T/Q)
                    elif msg_type == "error": # Check for specific auth error codes if Alpaca provides them
                         code = item.get('code')
                         msg = item.get('msg', 'Unknown server error')
                         self.logger.error(f"Received error message from server: {msg} (Code: {code})")
                         if code == 401 or "auth" in msg.lower(): # Example check for auth errors
                             self._update_connection_status(True, False, False, "Authentication Failed")
                             # Consider closing the websocket and forcing a reconnect attempt here
                             if self._websocket and self._websocket.open:
                                 asyncio.create_task(self._websocket.close(code=1000, reason="Auth failed"))
                         # Handle other errors as needed
                    else:
                         # Comment out debug logging to reduce console spam
                         pass
                    continue

                symbol = symbol.upper() # Ensure uppercase

                # Get timestamp (Alpaca uses 't' with RFC3339 format)
                ts_rfc3339_str = item.get("t") # Get the timestamp string
                timestamp = time.time() # Default to current time as fallback

                if isinstance(ts_rfc3339_str, str) and ts_rfc3339_str:
                    try:
                        # <<< SIMPLIFIED Timestamp Parsing Logic >>>
                        # Convert 'Z' to '+00:00' for ISO format compatibility
                        ts_str_processed = ts_rfc3339_str.replace('Z', '+00:00')

                        # Handle timestamps with nanoseconds (more than 6 digits after decimal)
                        dot_index = ts_str_processed.find('.')
                        if dot_index != -1:
                            # Find timezone marker or end of string
                            tz_marker_index = -1
                            for marker in ['+', '-']:
                                marker_pos = ts_str_processed.find(marker, dot_index)
                                if marker_pos != -1 and (tz_marker_index == -1 or marker_pos < tz_marker_index):
                                    tz_marker_index = marker_pos

                            # If no timezone marker found, use end of string
                            if tz_marker_index == -1:
                                tz_marker_index = len(ts_str_processed)

                            # Extract parts
                            before_dot = ts_str_processed[:dot_index]
                            fractional_part = ts_str_processed[dot_index+1:tz_marker_index]
                            tz_part = ts_str_processed[tz_marker_index:]

                            # Ensure exactly 6 digits for microseconds (Python's datetime limit)
                            if len(fractional_part) > 6:
                                # Truncate to 6 digits
                                fractional_part = fractional_part[:6]
                            elif len(fractional_part) < 6:
                                # Pad with zeros
                                fractional_part = fractional_part.ljust(6, '0')

                            # Reconstruct the timestamp string
                            ts_str_processed = f"{before_dot}.{fractional_part}{tz_part}"

                        # Parse with datetime
                        dt_obj = datetime.fromisoformat(ts_str_processed)
                        timestamp = dt_obj.timestamp()
                        # --- End SIMPLIFIED ---
                    except (ValueError, TypeError) as ts_err:
                         # Log the original string and the processed one if error occurs
                         processed_val_str = ts_str_processed if 'ts_str_processed' in locals() else ts_rfc3339_str
                         self.logger.warning(f"Could not parse RFC3339 timestamp '{ts_rfc3339_str}' (processed as '{processed_val_str}'): {ts_err}")
                         # timestamp remains time.time() (fallback)
                elif ts_rfc3339_str:
                     self.logger.warning(f"Timestamp field 't' is not a string: {ts_rfc3339_str} (type: {type(ts_rfc3339_str)})")
                     # Try converting if it's numeric (e.g., nanoseconds int/float)
                     try:
                         timestamp = float(ts_rfc3339_str) / 1_000_000_000.0
                     except (ValueError, TypeError):
                          self.logger.warning(f"Could not convert non-string timestamp '{ts_rfc3339_str}' to float seconds.")
                          # timestamp remains time.time()
                else:
                    # 't' field is missing or empty
                    self.logger.warning(f"Timestamp field 't' missing or empty in message item: {item}")
                    # timestamp remains time.time()

                # Ensure timestamp is float
                timestamp = float(timestamp) # Final conversion just in case

                # Dispatch based on type
                if msg_type == "t": # Trade
                    price = float(item.get("p", 0.0))
                    size = int(item.get("s", 0))
                    conditions = item.get("c") # List of conditions

                    # Don't log trade data to reduce console spam
                    # self.logger.debug(f"TRADE DATA: Symbol={symbol}, Price={price}, Size={size}, Time={timestamp}")

                    # --- THE FIX: Make a single, fast call to the Disruptor ---
                    self._receiver.on_trade(symbol, price, size, timestamp)
                    self._last_data_message_ts = timestamp
                    is_data_message_processed = True

                elif msg_type == "q": # Quote (NBBO)
                    bid = float(item.get("bp", 0.0))
                    ask = float(item.get("ap", 0.0))
                    bid_size = int(item.get("bs", 0)) # Lot size
                    ask_size = int(item.get("as", 0)) # Lot size
                    conditions = item.get("c") # List of conditions

                    # DIAGNOSTIC LOG: Raw quote item and extracted values
                    self.logger.info(f"PFS_HANDLING_QUOTE_RAW_ITEM for {symbol}: RawItem={item}")
                    self.logger.info(f"PFS_EXTRACTED_QUOTE_TO_RECEIVER: Sym={symbol}, Bid={bid}, Ask={ask}, TS={timestamp}")

                    # --- THE FIX: Make a single, fast call to the Disruptor ---
                    self._receiver.on_quote(symbol, bid, ask, bid_size, ask_size, timestamp)
                    self._last_data_message_ts = timestamp
                    is_data_message_processed = True

                # Add handlers for other message types (bars, statuses, lulds) if needed
                # elif msg_type == "b": # Bar
                #     # Extract bar data (o, h, l, c, v, n, vw, t)
                #     # self._receiver.on_bar(...) # Requires on_bar in IMarketDataReceiver
                # elif msg_type == "s": # Status
                #     # Extract trading status data
                #     # self._receiver.on_status(...) # Requires on_status in IMarketDataReceiver
                # elif msg_type == "l": # LULD
                #     # Extract LULD data
                #     # self._receiver.on_luld(...) # Requires on_luld in IMarketDataReceiver

                else:
                    # Comment out debug logging to reduce console spam
                    pass  # No action needed for unhandled message types

            except Exception as item_err:
                self.logger.error(f"Error processing item {item}: {item_err}", exc_info=True)

        # If any item in the list was actual data/good status, update connection status
        if is_data_message_processed: # This will only be true for T or Q messages now
            self._update_connection_status(
                connected_to_server=True,
                authenticated=True, # If we got data, we must be auth'd
                subscribed_and_streaming=True, # If we got data, we are streaming
                status_message="Data Received" # This message is now accurate
            )

        # CRITICAL MEMORY LEAK FIX: Reduce frequency of market data metrics to prevent memory exhaustion
        # Only capture metrics every 100 messages instead of every message
        self._alpaca_msg_count += 1
        if self._benchmarker and self._alpaca_msg_count % 100 == 0:
            # Increment message received count (Enhanced with convenience method) - sampled
            self._benchmarker.increment(
                "price_fetching.market_data_messages_received_count",
                100.0,  # Count 100 messages at once
                context={'source': 'alpaca_sip', 'message_items': len(data) if isinstance(data, list) else 1}
            )

            # Capture message processing time metric (Enhanced with convenience method) - sampled
            processing_time_ms = (time.time() - start_time) * 1000
            self._benchmarker.gauge(
                "price_fetching.market_data_processing_time_ms",
                processing_time_ms,
                context={'source': 'alpaca_sip', 'message_items': len(data) if isinstance(data, list) else 1}
            )

    # --- Health Status Publishing Methods ---

    def _publish_service_health_status(self, status: str, message: str, context: dict, correlation_id: str = None):
        """
        Publish health status events to Redis stream via correlation logger.
        
        Args:
            status: Service status (STARTED, STOPPED, HEALTHY, DEGRADED, etc.)
            message: Human-readable status message
            context: Additional context data for the health event
            correlation_id: Optional correlation ID for end-to-end tracking
        """
        try:
            # Check if IPC data dumping is disabled (TANK mode) FIRST
            # Note: TANK mode check would need config service access
            # For now, proceed with publishing
            
            # Create service health status payload with correlation ID for proper tracing
            payload = {
                "health_status": status,
                "status_reason": message,
                "context": context,
                "status_timestamp": time.time(),
                "component": "PriceFetchingService",
                "correlation_id": correlation_id or str(uuid.uuid4())  # Include correlation ID for tracing
            }
            
            # Use telemetry service if available
            """
            if self._telemetry_service:
                success = self._telemetry_service.enqueue(
                    source_component="PriceFetchingService",
                    event_type="TESTRADE_SERVICE_HEALTH_STATUS",
                    payload=payload,
                    stream_override="testrade:system-health"  # Use standard health stream
                )
                if success:
                    self.logger.debug(f"Price fetching service health status published via telemetry service: {status}")
                else:
                    self.logger.warning(f"Failed to publish price fetching service health status")
            else:
                self.logger.warning("Telemetry service not available for price fetching service health status publishing")
            """
            # Use correlation logger if available
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="PriceFetchingService",
                    event_name="ServiceHealthStatus",
                    payload=payload
                )
                self.logger.debug(f"Price fetching service health status published via correlation logger: {status}")
                
        except Exception as e:
            # Never let health publishing errors affect main functionality
            self.logger.error(f"PriceFetchingService: Error publishing health status: {e}", exc_info=True)

    def _publish_periodic_health_status(self):
        """
        Publish periodic health status with operational metrics.
        Called during normal operation to provide health monitoring data.
        """
        try:
            # Calculate operational metrics
            current_time = time.time()
            
            # Check connection status
            is_connected = getattr(self, '_is_connected_and_authenticated', False)
            ws_thread_alive = self._ws_thread and self._ws_thread.is_alive()
            
            # Count active subscriptions
            active_subscriptions = len(getattr(self, '_confirmed_subscriptions', set()))
            initial_symbols_count = len(getattr(self, '_active_symbols', set()))
            
            # Basic health indicators
            health_context = {
                "service_name": "PriceFetchingService",
                "is_connected": is_connected,
                "ws_thread_alive": ws_thread_alive,
                "ws_url": getattr(self, '_ws_url', 'unknown'),
                "active_subscriptions_count": active_subscriptions,
                "initial_symbols_count": initial_symbols_count,
                "messages_received": getattr(self, '_alpaca_msg_count', 0),
                "receiver_available": getattr(self, '_receivers', None) is not None and len(getattr(self, '_receivers', [])) > 0,
                "rest_client_available": getattr(self, '_rest_client', None) is not None,
                "health_status": "HEALTHY",
                "last_health_check": current_time
            }
            
            # Check for potential health issues
            health_issues = []
            
            # Check connection status
            if not is_connected:
                health_issues.append("websocket_not_connected")
            
            # Check thread status
            if not ws_thread_alive:
                health_issues.append("websocket_thread_not_alive")
            
            # Check subscription discrepancy
            if active_subscriptions < initial_symbols_count:
                health_issues.append("subscription_discrepancy")
            
            if health_issues:
                health_context["health_status"] = "DEGRADED"
                health_context["health_issues"] = health_issues
            
            # Publish health status
            self._publish_service_health_status(
                "PERIODIC_HEALTH_CHECK",
                f"PriceFetchingService periodic health check - Status: {health_context['health_status']}",
                health_context
            )
            
        except Exception as e:
            # Never let health publishing errors affect main functionality
            self.logger.error(f"PriceFetchingService: Error in periodic health status publishing: {e}", exc_info=True)

    def check_and_publish_health_status(self):
        """
        Public method to trigger health status publishing.
        Can be called externally to force a health check.
        """
        try:
            self._publish_periodic_health_status()
        except Exception as e:
            self.logger.error(f"PriceFetchingService: Error in external health status check: {e}", exc_info=True)
