# --- START OF FILE lightspeed_broker.py ---

import os
import logging
import socket
import json
import threading
import time
import re
import random
import concurrent.futures
import sys
import queue
from datetime import datetime, timezone # Added for timestamp conversion

# CRITICAL FIX: Remove sys.path manipulation that causes multiple module loading
# from config import LOG_BASE_DIR  # Import removed to avoid path issues
from typing import Optional, Dict, Any, Set, List, Union, Tuple 

try:
    from utils.network_diagnostics import log_network_send, log_network_recv, enable_network_diagnostics
except ImportError:
    def log_network_send(*args, **kwargs): pass
    def log_network_recv(*args, **kwargs): pass
    def enable_network_diagnostics(*args, **kwargs): pass

from interfaces.core.services import IEventBus, ILifecycleService
from interfaces.broker.services import IBrokerService
from interfaces.core.telemetry_interfaces import ITelemetryService
from core.events import (
    BrokerConnectionEvent, BrokerConnectionEventData,
    OrderStatusUpdateEvent, OrderStatusUpdateData,
    OrderFilledEvent, OrderFilledEventData,
    OrderRejectedEvent, OrderRejectedData,
    AllPositionsEvent, AllPositionsData, BrokerSnapshotPositionData, # This is for snapshot of all positions
    AccountUpdateEvent, AccountSummaryData,
    # MarketDataTickEvent, MarketDataTickData, # Assuming not used directly by LS broker for now
    BrokerErrorEvent, BrokerErrorData,
    BrokerRawMessageRelayEvent, BrokerRawMessageRelayEventData,
    BrokerPositionQuantityOnlyPushEvent, BrokerPositionQuantityOnlyPushData,
    BrokerFullPositionPushEvent, BrokerFullPositionPushData
)
from data_models import OrderLSAgentStatus, OrderSide

try:
    from interfaces.data.services import IPriceProvider
except ImportError:
    print("WARNING: Could not import IPriceProvider from modules.price_fetching.interfaces. Using placeholder.")
    class IPriceProvider: pass

try:
    from interfaces.order_management.services import IOrderRepository
except ImportError:
    print("WARNING: Could not import IOrderRepository from interfaces.order_management.services. Using placeholder.")
    class IOrderRepository: pass

from utils.performance_tracker import create_timestamp_dict, add_timestamp, calculate_durations, add_to_stats, is_performance_tracking_enabled
from utils.decorators import log_function_call
from utils.performance_tracker import log_performance_durations # Explicit import

# Import request handling functions
try:
    from utils.position_request_manager import get_next_req_id, store_pending_request, pop_pending_request
except ImportError:
    # Use local implementations if not available
    pass

# Global state for pending requests
_pending_requests: Dict[int, Union[concurrent.futures.Future, threading.Event]] = {}
_requests_lock = threading.Lock()
_next_req_id = 1000

def get_next_req_id() -> int:
    global _next_req_id
    with _requests_lock:
        rid = _next_req_id
        _next_req_id += 1
    return rid

def store_pending_request(rid: int, future_or_event: Union[concurrent.futures.Future, threading.Event]):
    with _requests_lock:
        _pending_requests[rid] = future_or_event

def pop_pending_request(rid: int) -> Optional[Union[concurrent.futures.Future, threading.Event]]:
    with _requests_lock:
        return _pending_requests.pop(rid, None)

def setup_broker_bridge_logger() -> logging.Logger:
    logger = logging.getLogger("broker_bridge_logger")
    # Assuming setLevel and handlers are configured externally by main.py or a central logging config
    # If not, you'd add:
    # logger.setLevel(logging.WARNING) 
    # log_file_path = os.path.join(LOG_BASE_DIR, "broker_bridge.log")
    # formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] => %(message)s")
    # file_handler = logging.FileHandler(log_file_path, mode="a")
    # file_handler.setFormatter(formatter)
    # logger.addHandler(file_handler)
    return logger

###############################################################################
# LightspeedBridgeClient
###############################################################################
class LightspeedBridgeClient:
    def __init__(self, host="127.0.0.1", port=9999, timeout=5):
        self.logger = logging.getLogger("broker_bridge_logger")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buffer = b""

    @log_function_call('broker_bridge')
    def connect(self) -> None:
        self.logger.debug(f"LightspeedBridgeClient.connect => {self.host}:{self.port}")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        self.sock = s 
        self.logger.info("Lightspeed extension connected.")

    @log_function_call('broker_bridge')
    def close(self) -> None:
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR) # Graceful shutdown
            except OSError:
                pass # Ignore if already closed or error during shutdown
            try:
                self.sock.close()
                self.logger.info("Lightspeed extension disconnected.") 
            except OSError:
                pass
            self.sock = None

    @log_function_call('broker_bridge')
    def send_message_no_read(self, msg_dict: dict) -> bool:
        if not self.sock:
            self.logger.error("Cannot send message: No connection to Lightspeed extension.")
            return False
        try:
            line = json.dumps(msg_dict) + "\n"
            encoded_line = line.encode("utf-8")
            data_size = len(encoded_line)
            command_details = msg_dict.get("cmd", "unknown")

            self.logger.critical(f"NET_SEND_LS_BRIDGE: Command='{command_details}', PayloadSize={data_size}, Thread='{threading.current_thread().name}'")
            log_network_send("LIGHTSPEED_BROKER_SEND", msg_dict) # Assumes msg_dict is suitable for this log
            
            self.logger.debug(f"Sending => {line.strip()}")
            self.sock.sendall(encoded_line)
            return True
        except OSError as e:
            self.logger.error(f"Socket error in send_message_no_read: {e}. Marking as disconnected.")
            self.close() # Ensure socket is closed and set to None
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error in send_message_no_read: {e}", exc_info=True)
            self.close()
            return False

    @log_function_call('broker_bridge')
    def _recv_line(self) -> Optional[str]: # Return Optional[str] to indicate potential no-data
        if not self.sock:
            return None

        try:
            # self.sock.settimeout(self.timeout) # Timeout set on connect, or should be non-blocking with select
            
            # Check buffer first
            nl_index = self._buffer.find(b"\n")
            if nl_index >= 0:
                line = self._buffer[:nl_index]
                self._buffer = self._buffer[nl_index + 1:]
                decoded_line = line.decode("utf-8", errors="replace")
                data_size = len(line)
                self.logger.critical(f"NET_RECV_LS_BRIDGE: FromBuffer, PayloadSize={data_size}, Thread='{threading.current_thread().name}'")
                log_network_recv("LIGHTSPEED_BROKER_RECV", decoded_line)
                return decoded_line

            # If not in buffer, attempt to receive more data
            # This part assumes the caller (_listen_loop) uses select or similar for non-blocking checks
            # For a direct call, it might block if socket is blocking and no data.
            # The original _listen_loop implies non-blocking or timeout-based reads.
            # For simplicity here, assuming it's called when data is expected or timeout is handled by caller.
            # Let's make it more robust for direct calls by adding a timeout temporarily if not set.
            current_timeout = self.sock.gettimeout()
            if current_timeout is None or current_timeout <= 0: # If blocking or no timeout
                 self.sock.settimeout(0.1) # Short timeout for this read attempt

            try:
                chunk = self.sock.recv(4096) # Increased buffer size
            finally:
                if current_timeout is None or current_timeout <= 0:
                    self.sock.settimeout(current_timeout) # Restore original timeout

            if not chunk: # Connection closed by peer
                self.logger.info("_recv_line: Socket connection closed by peer.")
                self.close() # Close our end
                if self._buffer: # Process any remaining buffer
                    line = self._buffer
                    self._buffer = b""
                    decoded_line = line.decode("utf-8", errors="replace")
                    self.logger.critical(f"NET_RECV_LS_BRIDGE: RemainingBufferOnClose, PayloadSize={len(line)}, Thread='{threading.current_thread().name}'")
                    return decoded_line
                return None # Indicate connection closed

            self._buffer += chunk
            nl_index = self._buffer.find(b"\n")
            if nl_index >= 0:
                line = self._buffer[:nl_index]
                self._buffer = self._buffer[nl_index + 1:]
                decoded_line = line.decode("utf-8", errors="replace")
                data_size = len(line)
                self.logger.critical(f"NET_RECV_LS_BRIDGE: FromNetwork, PayloadSize={data_size}, Thread='{threading.current_thread().name}'")
                log_network_recv("LIGHTSPEED_BROKER_RECV", decoded_line)
                return decoded_line
            else: # Did not receive a full line yet
                return None 
        except socket.timeout:
            # This timeout is for the explicit recv call if it was blocking
            # If _listen_loop uses select, this timeout here might be redundant
            # or indicate a very slow stream.
            if self._buffer: # If there's partial data from previous reads, don't lose it.
                 self.logger.debug("_recv_line: Socket timeout, but partial data remains in buffer.")
                 return None # Indicate timeout but data might still be accumulating
            return None # Indicate timeout
        except OSError as e:
            self.logger.error(f"Socket error in _recv_line: {e}. Marking as disconnected.")
            self.close()
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error in _recv_line: {e}", exc_info=True)
            self.close()
            return None

###############################################################################
# LightspeedBroker
###############################################################################
class LightspeedBroker(IBrokerService):
    """
    Manages the connection and command execution for the Lightspeed gateway.
    
    Implements ILifecycleService to be managed by the ServiceLifecycleManager.
    This ensures proper startup ordering and readiness checking in the
    application lifecycle.
    """
    
    @log_function_call('broker_bridge')
    def __init__(self,
                 event_bus: IEventBus,
                 price_provider: IPriceProvider,
                 telemetry_service: ITelemetryService,
                 config_service,
                 order_repository: Optional[IOrderRepository] = None,
                 host="127.0.0.1", port=9999,
                 rest_client=None,
                 pipeline_validator=None,
                 benchmarker=None):
        self.logger = logging.getLogger("broker_bridge_logger")
        self.event_bus = event_bus
        self._telemetry_service = telemetry_service
        self._order_repository = order_repository # <<< STORE THE REFERENCE
        self.host = host
        self.port = port
        self._config = config_service # Store config service
        self._pipeline_validator = pipeline_validator # Store pipeline validator for order execution tracking
        self._benchmarker = benchmarker # Store benchmarker for performance metrics
        # ApplicationCore dependency removed - using TelemetryService and ConfigService directly

        self._broker_msg_count = 0
        self._broker_last_log_time = time.time()
        self._broker_log_interval = 10
        
        # TANK MODE: Initialize flag for TANK mode operation
        self._tank_mode = False

        self._last_published_full_broker_position_state: Dict[str, Tuple[float, float, Optional[float]]] = {}
        self._last_full_position_event_publish_time: Dict[str, float] = {}
        self._full_position_event_min_interval_sec: Optional[float] = 0.5 # Default
        self._position_push_filter_lock = threading.RLock()
        
        # Correlation tracking for broker operations (Break #3 & #4 fix)
        self._order_correlations: Dict[str, str] = {}  # local_order_id -> correlation_id
        self._correlation_lock = threading.RLock()

        self._price_provider: IPriceProvider = price_provider
        self._rest_client = rest_client

        self.client = LightspeedBridgeClient(host, port)
        self._stop_event = threading.Event()
        self._listen_thread_obj: Optional[threading.Thread] = None # Renamed from _thread for clarity
        self.connected = False
        
        # CRITICAL: Initialize connection state tracking for aggressive monitoring
        self._is_fully_connected = False  # Default to False - must be explicitly set to True
        self._handshake_completed = False
        self._lock = threading.RLock()  # Thread-safe state management
        self.logger.critical("LSB_STATE: Initialized. Connected: False, HandshakeCompleted: False.")

        # Mapping for correlating local_order_id to validation_id for pipeline tracking
        self._local_order_to_validation_id: Dict[str, str] = {}  # local_order_id -> validation_id
        self._validation_id_lock = threading.RLock()

        if not self._order_repository:
            self.logger.error("LSB_INIT_ERROR: LightspeedBroker initialized WITHOUT OrderRepository. Compensatory ID lookup WILL FAIL.")
        else:
            self.logger.info("LSB_INIT_INFO: LightspeedBroker initialized WITH OrderRepository reference.")

        # Redis publishing now handled by TelemetryService - no ApplicationCore needed
        self.logger.info("LSB_INIT_INFO: LightspeedBroker initialized with TelemetryService for Redis publishing.")

        # Queue-based Redis publishing worker to prevent blocking listen thread
        self._redis_publish_queue = queue.Queue(maxsize=2000) # Increased size for safety
        self._redis_worker_stop_event = threading.Event()
        self._redis_worker_thread = None  # Initialize to None, will be created in start()
        self.logger.info("LightspeedBroker: Thread creation deferred to start() method.")

        self.logger.info("LightspeedBroker initialized.")

    def start(self):
        """
        Implementation of ILifecycleService's start method.
        Initiates the connection and starts all background listener threads.
        This method is non-blocking - connection happens in background.
        """
        self.logger.critical("LSB_STATE: start() called. Initiating connection and listeners.")
        self.logger.info("LightspeedBroker start() called. Initiating connection and listeners.")
        
        try:
            self.logger.critical("LSB_START: Entering start() method main logic.")
            
            # TANK MODE: In TANK mode, skip Redis worker threads (telemetry publishing)
            # but still allow broker connection for trading operations
            self.logger.critical("LSB_START: Checking TANK mode configuration...")
            tank_mode = hasattr(self._config, 'ENABLE_IPC_DATA_DUMP') and not self._config.ENABLE_IPC_DATA_DUMP
            self.logger.critical(f"LSB_START: TANK mode = {tank_mode}")
            if tank_mode:
                self.logger.info("TANK_MODE: IPC data dumping disabled. Skipping Redis worker threads but allowing broker connection.")
                self._tank_mode = True
            else:
                self._tank_mode = False
            
            # 1. Start the Redis publishing worker thread (only if not in TANK mode)
            self.logger.critical("LSB_START: Setting up Redis worker thread...")
            if not self._tank_mode:
                if not (self._redis_worker_thread and self._redis_worker_thread.is_alive()):
                    self.logger.critical("LSB_START: Creating Redis worker thread...")
                    self.logger.info("Starting Redis publishing worker thread...")
                    self._redis_worker_thread = threading.Thread(
                        target=self._redis_publish_worker,
                        name="LSRedisPublishWorker",
                        daemon=True
                    )
                    self._redis_worker_thread.start()
                    self.logger.critical("LSB_START: Redis worker thread started successfully.")
                    self.logger.info("LightspeedBroker: Redis publishing worker thread started.")
                else:
                    self.logger.debug("Redis worker thread already running.")
            else:
                self.logger.info("TANK_MODE: Skipping Redis worker thread creation (telemetry publishing disabled)")
            
            # 2. Start single unified worker thread for connection and listening
            self.logger.critical("LSB_START: Checking if broker worker thread already exists...")
            if self._listen_thread_obj and self._listen_thread_obj.is_alive():
                self.logger.warning("Broker worker thread already running.")
                return
                
            self.logger.critical("LSB_START: Creating unified broker worker thread...")
            self.logger.critical("LSB_STATE: Starting unified connection and listener thread...")
            self.logger.info("Starting broker connection and listener thread...")
            
            # Reset state on each start attempt
            self.logger.critical("LSB_START: Resetting connection state...")
            with self._lock:
                self._is_fully_connected = False
                self._handshake_completed = False
                
            self.logger.critical("LSB_START: Creating Thread object...")
            self._listen_thread_obj = threading.Thread(
                target=self._connection_and_listen_loop,
                args=(self._handle_worker_thread_exception,),  # Pass the hard exit callback
                daemon=True,
                name="LightspeedWorker"
            )
            
            self.logger.critical("LSB_START: Starting thread...")
            self._listen_thread_obj.start()
            self.logger.critical("LSB_WORKER: Unified worker thread started.")
            
            self.logger.critical("LSB_STATE: start() completed. Connection proceeding in background.")
            self.logger.info("LightspeedBroker start() completed. Connection proceeding in background.")
            
        except Exception as start_exception:
            self.logger.critical(f"LSB_START: FATAL ERROR in start() method: {start_exception}", exc_info=True)
            raise

    def _handle_worker_thread_exception(self, e: Exception):
        """
        This is the "red button". Called by the worker thread on a fatal,
        unhandled exception. It logs the error and terminates the process.
        """
        self.logger.critical("############################################################")
        self.logger.critical("#####   FATAL, UNRECOVERABLE BROKER THREAD FAILURE   #####")
        self.logger.critical(f"#####   Exception: {e}                             #####")
        self.logger.critical("#####   APPLICATION WILL NOW TERMINATE.            #####")
        self.logger.critical("############################################################")
        # os._exit(1) is a hard exit. It does not run finally blocks.
        # It's used for situations where the application state is corrupted
        # and a graceful shutdown is not safe or possible.
        os._exit(1)

    def _redis_publish_worker(self):
        """
        Worker thread that consumes from a queue and publishes to Redis.
        This ensures the listen thread is never blocked by Redis I/O and preserves order.
        Enhanced with timeout protection and error resilience.
        """
        self.logger.info("Redis publish worker loop started.")
        while not self._redis_worker_stop_event.is_set():
            try:
                # Block until an item is available or timeout occurs
                item = self._redis_publish_queue.get(timeout=1.0)
                
                # Unpack the item
                target_method = item['target']
                args = item['args']
                kwargs = item['kwargs']
                
                # Execute the actual Redis publishing method with timeout protection
                try:
                    # Add timeout protection to prevent indefinite blocking
                    import signal
                    
                    def timeout_handler(signum, frame):
                        raise TimeoutError("Redis publish operation timed out")
                    
                    # Set 5-second timeout for Redis operations
                    signal.signal(signal.SIGALRM, timeout_handler)
                    signal.alarm(5)
                    
                    target_method(*args, **kwargs)
                    
                    # Clear the alarm
                    signal.alarm(0)
                    
                except TimeoutError:
                    self.logger.warning("Redis publish operation timed out after 5 seconds - skipping message")
                except Exception as publish_error:
                    self.logger.error(f"Redis publish method failed: {publish_error}", exc_info=True)
                finally:
                    # Always clear the alarm
                    signal.alarm(0)

            except queue.Empty:
                # This is a normal timeout, continue loop to check stop event.
                continue
            except Exception as e:
                self.logger.error(f"Redis publish worker encountered an error: {e}", exc_info=True)
                time.sleep(1) # Prevent fast error loop
        self.logger.info("Redis publish worker loop stopped.")

    def _publish_broker_raw_message_to_redis(self, raw_message_dict: dict, message_type: str = "INBOUND", correlation_id_override: Optional[str] = None):
        """Publish raw broker message to Redis stream via TelemetryService."""
        if not self._telemetry_service:
            # Don't log error for every message - just debug
            self.logger.debug("Cannot publish broker raw message to Redis: TelemetryService not available.")
            return

        try:
            import uuid

            # Determine correlation ID (Break #3 & #4 fix)
            correlation_id_to_use = correlation_id_override
            if not correlation_id_to_use:
                # Try to extract correlation ID from stored order correlations
                local_copy_id = raw_message_dict.get("local_copy_id")
                if local_copy_id:
                    # Minimize lock scope to reduce contention
                    try:
                        with self._correlation_lock:
                            correlation_id_to_use = self._order_correlations.get(str(local_copy_id))
                    except Exception as lock_error:
                        self.logger.warning(f"Failed to acquire correlation lock: {lock_error}")
                        correlation_id_to_use = None
                        
            # Fallback if no correlation found
            if not correlation_id_to_use:
                correlation_id_to_use = "MISSING_CORRELATION_ID"
                self.logger.error("CRITICAL: No correlation ID found for broker message. This breaks end-to-end tracing!")

            # Create payload with raw message and metadata
            payload = {
                "raw_message": raw_message_dict,
                "message_type": message_type,  # "INBOUND" or "OUTBOUND"
                "broker_type": "LIGHTSPEED",
                "timestamp": time.time(),
                "event_type": raw_message_dict.get("event", "unknown"),
                "status": raw_message_dict.get("status", "unknown")
            }

            # Check if IPC data dumping is disabled FIRST
            if hasattr(self._config, 'ENABLE_IPC_DATA_DUMP') and not self._config.ENABLE_IPC_DATA_DUMP:
                self.logger.debug("TANK_MODE: Skipping IPC send for broker raw message - ENABLE_IPC_DATA_DUMP is False")
                return

            target_stream = getattr(self._config, 'redis_stream_broker_raw_messages', 'testrade:broker-raw-messages')
            
            # Use central telemetry queue if available
            # Use telemetry service:
            telemetry_data = {
                "payload": payload,
                "correlation_id": correlation_id_to_use,
                "causation_id": None
            }
                
            success = self._telemetry_service.enqueue(
                source_component="LightspeedBroker",
                event_type="TESTRADE_BROKER_RAW_MESSAGE",
                payload=telemetry_data,
                stream_override=target_stream
            )
            
            if success:
                self.logger.debug(f"Broker raw message published to stream '{target_stream}': {raw_message_dict.get('event', 'unknown')}")
            else:
                self.logger.debug(f"Failed to publish broker raw message to stream '{target_stream}'")

        except Exception as e:
            self.logger.error(f"LightspeedBroker: Error publishing raw message to Redis: {e}", exc_info=True)

    def _publish_broker_error_to_redis(self, error_message: str, error_details: str = "", is_critical: bool = False):
        """Publish broker error to Redis stream via TelemetryService."""
        if not self._telemetry_service:
            return

        try:
            import uuid

            # Create payload with error information
            payload = {
                "error_message": error_message,
                "error_details": error_details,
                "is_critical": is_critical,
                "broker_type": "LIGHTSPEED",
                "timestamp": time.time()
            }

            # Check if IPC data dumping is disabled FIRST
            if hasattr(self._config, 'ENABLE_IPC_DATA_DUMP') and not self._config.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping IPC send for broker error - ENABLE_IPC_DATA_DUMP is False")
                return

            target_stream = getattr(self._config, 'redis_stream_broker_errors', 'testrade:broker-errors')
            
            # Use central telemetry queue if available
            # Use telemetry service:
            telemetry_data = {
                "payload": payload,
                "correlation_id": str(uuid.uuid4()),
                "causation_id": None
            }
                
            success = self._telemetry_service.enqueue(
                source_component="LightspeedBroker",
                event_type="TESTRADE_BROKER_ERROR",
                payload=telemetry_data,
                stream_override=target_stream
            )
            
            if success:
                self.logger.debug(f"Broker error published to stream '{target_stream}': {error_message}")
            else:
                self.logger.debug(f"Failed to publish broker error to stream '{target_stream}'")

        except Exception as e:
            self.logger.error(f"LightspeedBroker: Error publishing broker error to Redis: {e}", exc_info=True)

    def _publish_broker_submission_error_to_redis(self, local_order_id: Optional[str], symbol: str, 
                                                 error_type: str, error_message: str, 
                                                 order_params: Optional[Dict[str, Any]] = None,
                                                 correlation_id: Optional[str] = None):
        """Publish broker order submission error to Redis stream via BulletproofBabysitterIPCClient."""
        if not self._telemetry_service:
            return

        try:
            import uuid

            # Get correlation ID from order correlations if available
            correlation_id_to_use = correlation_id
            if not correlation_id_to_use and local_order_id:
                with self._correlation_lock:
                    correlation_id_to_use = self._order_correlations.get(str(local_order_id))
            
            # Fallback if no correlation found
            if not correlation_id_to_use:
                correlation_id_to_use = "MISSING_CORRELATION_ID"
                self.logger.error(f"CRITICAL: No correlation ID found for order submission error (LocalID: {local_order_id}). This breaks end-to-end tracing!")

            # Create payload with submission error information
            payload = {
                "error_type": error_type,
                "error_message": error_message,
                "local_order_id": local_order_id,
                "symbol": symbol,
                "broker_type": "LIGHTSPEED",
                "timestamp": time.time(),
                "order_params": order_params or {}
            }

            # Check if IPC data dumping is disabled FIRST
            if hasattr(self._config, 'ENABLE_IPC_DATA_DUMP') and not self._config.ENABLE_IPC_DATA_DUMP:
                self.logger.debug(f"TANK_MODE: Skipping IPC send for broker submission error - ENABLE_IPC_DATA_DUMP is False")
                return

            target_stream = getattr(self._config, 'redis_stream_broker_submission_errors', 'testrade:broker-submission-errors')
            
            # Use central telemetry queue if available
            # Use telemetry service:
            telemetry_data = {
                    "payload": payload,
                    "correlation_id": correlation_id_to_use,
                    "causation_id": local_order_id
                }
                
            success = self._telemetry_service.enqueue(
                source_component="LightspeedBroker",
                event_type="TESTRADE_BROKER_SUBMISSION_ERROR",
                payload=telemetry_data,
                stream_override=target_stream
            )
            
            if success:
                self.logger.debug(f"Broker submission error published to stream '{target_stream}': {error_type} for {symbol}")
            else:
                self.logger.warning(f"Failed to publish broker submission error to stream '{target_stream}'")

        except Exception as e:
            self.logger.error(f"LightspeedBroker: Error publishing submission error to Redis: {e}", exc_info=True)

    def _perform_compensatory_id_lookup(self, broker_order_id: Optional[str], ephemeral_corr: Optional[str]) -> Optional[str]:
        """
        Performs compensatory ID lookup when local_copy_id is -1 or invalid.
        Looks up the correct Python local_order_id using broker_order_id or ephemeral_corr via OrderRepository.

        Args:
            broker_order_id: The broker order ID from the C++ message
            ephemeral_corr: The ephemeral correlation ID from the C++ message

        Returns:
            The correct Python local_order_id if found, None otherwise
        """
        if not self._order_repository:
            self.logger.error("COMPENSATORY_LOOKUP_ERROR: OrderRepository not available for ID lookup.")
            return None

        try:
            # First try lookup by broker_order_id
            if broker_order_id:
                order = self._order_repository.get_order_by_broker_id(str(broker_order_id))
                if order:
                    local_order_id = getattr(order, 'local_order_id', None)
                    if local_order_id:
                        self.logger.info(f"COMPENSATORY_LOOKUP_SUCCESS: Found local_order_id='{local_order_id}' via broker_order_id='{broker_order_id}'")
                        return str(local_order_id)

            # Then try lookup by ephemeral_corr
            if ephemeral_corr:
                order = self._order_repository.get_order_by_ephemeral_corr(str(ephemeral_corr))
                if order:
                    local_order_id = getattr(order, 'local_order_id', None)
                    if local_order_id:
                        self.logger.info(f"COMPENSATORY_LOOKUP_SUCCESS: Found local_order_id='{local_order_id}' via ephemeral_corr='{ephemeral_corr}'")
                        return str(local_order_id)

            # If both lookups failed
            self.logger.warning(f"COMPENSATORY_LOOKUP_FAILED: No order found for broker_order_id='{broker_order_id}' or ephemeral_corr='{ephemeral_corr}'")
            return None

        except Exception as e:
            self.logger.error(f"COMPENSATORY_LOOKUP_ERROR: Exception during ID lookup: {e}", exc_info=True)
            return None

    def set_order_repository(self, order_repository: IOrderRepository) -> None:
        """
        Sets the OrderRepository reference after initialization.
        Used for dependency injection when OrderRepository is created after LightspeedBroker.

        Args:
            order_repository: The OrderRepository instance to inject
        """
        self._order_repository = order_repository
        self.logger.info("LSB_DEPENDENCY_INJECTION: OrderRepository injected into LightspeedBroker for compensatory ID lookup.")

    def map_local_order_id_to_validation_id(self, local_order_id: str, validation_id: str) -> None:
        """
        Maps a local_order_id to a validation_id for pipeline tracking.
        Called by TradeExecutor when it gets the local_order_id after sending an order.

        Args:
            local_order_id: The local order ID returned by the broker
            validation_id: The validation ID from the original pipeline
        """
        with self._validation_id_lock:
            self._local_order_to_validation_id[str(local_order_id)] = validation_id
            self.logger.debug(f"Mapped local_order_id {local_order_id} to validation_id {validation_id}")

    def get_validation_id_for_local_order(self, local_order_id: str) -> Optional[str]:
        """
        Retrieves the validation_id for a given local_order_id.

        Args:
            local_order_id: The local order ID to look up

        Returns:
            The validation_id if found, None otherwise
        """
        with self._validation_id_lock:
            return self._local_order_to_validation_id.get(str(local_order_id))

    def _connection_and_listen_loop(self, on_failure_callback: callable):
        """
        A single, permanent thread that connects, performs handshake,
        and then enters the listening loop. If it ever fails unexpectedly,
        it calls the failure callback to terminate the entire application.
        """
        try:
            self.logger.critical("LS_WORKER: THREAD STARTED. Entering main try block.")
            # --- CONNECTION PHASE ---
            self.logger.critical("LSB_WORKER: Attempting TCP socket connection to Lightspeed...")
            self.client.connect()
            self.logger.critical("LSB_WORKER: Socket TCP connection SUCCEEDED.")
            
            # --- HANDSHAKE PHASE ---
            self.logger.critical("LSB_HANDSHAKE: Starting handshake process...")
            
            # Get configuration for position event interval
            if self._config:
                self._full_position_event_min_interval_sec = float(self._config.get('broker_full_position_push_min_interval_sec', 0.5))
            else:
                self.logger.warning("LightspeedBroker: Config service not available, using default for full_position_event_min_interval_sec.")
                self._full_position_event_min_interval_sec = 0.5

            # Prepare handshake
            rid = get_next_req_id()
            fut = concurrent.futures.Future()
            store_pending_request(rid, fut)
            msg = {"cmd": "ping", "req_id": rid}
            handshake_start_time = time.time()
            
            self.logger.critical("LSB_HANDSHAKE: Sending ping message...")
            send_success = self.client.send_message_no_read(msg)
            if not send_success:
                raise ConnectionRefusedError("Handshake failed: Could not send ping message.")
            
            # Wait for handshake response
            try:
                resp = fut.result(timeout=5.0)
                handshake_duration_ms = (time.time() - handshake_start_time) * 1000
                
                if resp and resp.get("ok") is True and resp.get("reply") == "pong" and resp.get("req_id") == rid:
                    # --- SUCCESSFUL HANDSHAKE - SET STATE ---
                    with self._lock:
                        self.connected = True
                        self._is_fully_connected = True
                        self._handshake_completed = True
                    
                    self.logger.critical("LS_WORKER: STATE SET TO CONNECTED.")
                    self.logger.critical(f"LSB_HANDSHAKE: Handshake SUCCEEDED. Latency: {handshake_duration_ms:.2f} ms.")
                    self.logger.critical("LSB_STATE: Connection established. Connected: True, HandshakeCompleted: True.")
                    
                    # Publish successful connection event
                    event_payload = BrokerConnectionEventData(connected=True, message="Lightspeed handshake successful, listeners active.", latency_ms=handshake_duration_ms)
                    self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                else:
                    raise ConnectionRefusedError(f"Invalid handshake response: {str(resp)[:100]}")
                    
            except concurrent.futures.TimeoutError:
                handshake_duration_ms = (time.time() - handshake_start_time) * 1000
                raise ConnectionRefusedError(f"Handshake timeout after {handshake_duration_ms:.2f} ms")
            
            # --- LISTENING PHASE ---
            self.logger.critical("LS_WORKER: ENTERING LISTENING LOOP...")
            self.logger.critical("LSB_WORKER: Entering listening loop...")
            self._worker_listen_loop()
            
        except Exception as e:
            # Any unexpected exception during connect, handshake, or listen
            # is considered fatal and will terminate the entire application
            with self._lock:
                self._is_fully_connected = False
                self._handshake_completed = False
                self.connected = False
            
            self.logger.critical(f"LSB_WORKER: FATAL ERROR in connection/listen loop: {e}", exc_info=True)
            
            # CALL THE HARD EXIT CALLBACK - This will terminate the entire process
            on_failure_callback(e)
        finally:
            # This will only be reached on a graceful shutdown (stop_event is set)
            # because any unexpected exception calls os._exit(1) which bypasses finally blocks
            self.logger.critical("LS_WORKER: THREAD EXITING. Entering finally block, setting connected=False.")
            # Ensure state is properly reset on exit
            with self._lock:
                self._is_fully_connected = False
                self._handshake_completed = False
                self.connected = False
            self.logger.critical("LSB_WORKER: Loop exited. Connection state set to DISCONNECTED.")
            
            # Clean up socket
            try:
                self.client.close()
            except Exception as cleanup_e:
                self.logger.debug(f"Socket cleanup error (expected): {cleanup_e}")

    def _worker_listen_loop(self):
        """
        The listening portion of the unified worker thread.
        This runs continuously after successful connection and handshake.
        """
        self.logger.critical("LSB_LISTEN_THREAD: Loop started.")
        
        # Wait for dependencies to be ready
        initial_wait_cycles = 0
        max_initial_wait_cycles = 50
        while not self._order_repository and initial_wait_cycles < max_initial_wait_cycles and not self._stop_event.is_set():
            self.logger.warning(f"_worker_listen_loop: OrderRepository not yet set in LightspeedBroker. Waiting... (Cycle: {initial_wait_cycles + 1})")
            time.sleep(0.1)
            initial_wait_cycles += 1

        if not self._order_repository and not self._stop_event.is_set():
            self.logger.error("_worker_listen_loop: OrderRepository was NOT set after waiting. Enrichment/compensatory logic will fail for early messages.")
        elif not self._stop_event.is_set():
            self.logger.info(f"_worker_listen_loop: OrderRepository confirmed set in LightspeedBroker (ID: {id(self._order_repository)}). Proceeding.")
        
        # Main listening loop
        loop_count = 0
        while not self._stop_event.is_set():
            loop_count += 1
            if loop_count % 10 == 0:  # Log every 10th iteration (approximately every second if select timeout is 0.1s)
                self.logger.critical("LS_WORKER: Still alive inside listening loop.")
                
            with self._lock:
                if not self._is_fully_connected:
                    self.logger.critical("LS_WORKER: EXITING LOOP - _is_fully_connected is False.")
                    break
                    
            line: Optional[str] = None
            try:
                if self._stop_event.is_set():
                    break
                
                # Check socket health
                if not self.client.sock:
                    self.logger.warning("_worker_listen_loop: Socket is None, exiting.")
                    break
                    
                # Use select for non-blocking check
                import select
                ready_to_read, _, _ = select.select([self.client.sock], [], [], 0.1)

                if self._stop_event.is_set():
                    break

                if ready_to_read:
                    line = self.client._recv_line()
                else:
                    continue

                if self._stop_event.is_set():
                    break
                    
                if line is None:
                    if not self.client.sock:
                        self.logger.info("_worker_listen_loop: _recv_line indicated socket closed. Exiting loop.")
                        break
                    else:
                        continue

                # Process the received message
                try:
                    self.logger.debug(f"ORDER_PATH_DEBUG: RAW_RECV_LINE from C++: {line}")
                    data = json.loads(line)
                    evt_type = data.get("event")
                    req_id_from_data = data.get("req_id")
                    self.logger.debug(f"ORDER_PATH_DEBUG: PARSED_DATA - Event: '{evt_type}', ReqID: '{req_id_from_data}'")
                    if evt_type in ["order_status", "raw_share_update", "positions_update"]:
                         self.logger.critical(f"BROKER_RAW_DATA: Event='{evt_type}', FullData={json.dumps(data)}")
                    
                except json.JSONDecodeError as e:
                    self.logger.error(f"ORDER_PATH_DEBUG: JSON decode error. Raw line: '{line}', Error: {e}")
                    continue
                
                # Handle the message
                perf_timestamps_msg = create_timestamp_dict() if is_performance_tracking_enabled() else None
                self._handle_inbound_message(data, perf_timestamps_msg)

            except Exception as ex:
                self.logger.critical(f"LSB_LISTEN_THREAD: FATAL LISTENER ERROR. Connection lost: {ex}", exc_info=True)
                break

        self.logger.critical("LSB_LISTEN_THREAD: Loop exited.")

    @log_function_call('broker_bridge')
    def connect(self) -> None:
        try:
            self.logger.critical("LSB_CONN_THREAD: Attempting TCP socket connection to Lightspeed...")
            self.logger.debug("Attempting LightspeedBridgeClient.connect()...")
            self.client.connect()
            self.logger.critical("LSB_CONN_THREAD: Socket TCP connection SUCCEEDED.")
            # Connection event published after handshake in start_listeners
        except Exception as e:
            self.logger.critical(f"LSB_CONN_THREAD: FATAL SOCKET CONNECTION ERROR: {e}", exc_info=True)
            self.logger.error(f"Initial socket connection to Lightspeed failed: {e}", exc_info=True)
            event_payload = BrokerConnectionEventData(
                connected=False,
                message=f"Connection to Lightspeed failed: {str(e)}"
            )
            self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
            raise # Re-raise to signal failure

    @log_function_call('broker_bridge')
    def disconnect(self) -> None:
        self.client.close() # This will set self.client.sock to None
        self.connected = False # Set our internal flag
        self.logger.info("Disconnected from Lightspeed broker (client.close called).")
        event_payload = BrokerConnectionEventData(
            connected=False,
            message="Disconnected from Lightspeed."
        )
        self.event_bus.publish(BrokerConnectionEvent(data=event_payload))

    @log_function_call('broker_bridge')
    def start_listeners(self) -> None:
        if self._config:
            self._full_position_event_min_interval_sec = float(self._config.get('broker_full_position_push_min_interval_sec', 0.5))
        else:
            self.logger.warning("LightspeedBroker: Config service not available, using default for full_position_event_min_interval_sec.")
            self._full_position_event_min_interval_sec = 0.5

        if not self.client.sock: # Check if initial connection succeeded
            try:
                self.logger.info("start_listeners: No existing socket, attempting to connect.")
                self.connect() # Try to connect if not already
            except Exception as e:
                 self.logger.error(f"start_listeners: Connection attempt failed: {e}")
                 raise ConnectionRefusedError(f"Failed to establish initial connection for listeners: {e}")


        self.logger.critical("LSB_HANDSHAKE: Starting handshake process...")
        rid = get_next_req_id()
        fut = concurrent.futures.Future()
        store_pending_request(rid, fut)
        msg = {"cmd": "ping", "req_id": rid}
        handshake_start_time = time.time() 
        
        self.logger.critical("LSB_HANDSHAKE: Sending ping message...")
        send_success = self.client.send_message_no_read(msg)
        if not send_success:
            self.connected = False
            self._handshake_completed = False
            self.logger.critical("LSB_HANDSHAKE: FATAL ERROR - Could not send ping message.")
            self.logger.error("Handshake failed: Could not send ping message.")
            pop_pending_request(rid) 
            event_payload = BrokerConnectionEventData(connected=False, message="Lightspeed handshake failed: Could not send ping.")
            self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
            raise ConnectionRefusedError("Handshake failed: Could not send ping.")

        self._stop_event.clear()
        self._listen_thread_obj = threading.Thread(target=self._listen_loop, daemon=True, name="LightspeedListenThread")
        self._listen_thread_obj.start()

        try:
            resp = fut.result(timeout=5.0) # Increased timeout slightly for handshake
            handshake_duration_ms = (time.time() - handshake_start_time) * 1000 
            if resp and resp.get("ok") is True and resp.get("reply") == "pong" and resp.get("req_id") == rid:
                self.connected = True
                self._handshake_completed = True
                self.logger.critical(f"LSB_HANDSHAKE: Handshake SUCCEEDED. Latency: {handshake_duration_ms:.2f} ms.")
                self.logger.info(f"Handshake succeeded. Latency: {handshake_duration_ms:.2f} ms. Listeners active.")
                event_payload = BrokerConnectionEventData(connected=True, message="Lightspeed handshake successful, listeners active.", latency_ms=handshake_duration_ms)
                self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                
                # INSTRUMENTATION: Publish service health status - STARTED
                # FIX: Make IPC publishing truly fire-and-forget to prevent blocking broker startup
                try:
                    self._publish_service_health_status(
                        "STARTED",
                        "LightspeedBroker connected and listeners started successfully",
                        {
                            "service_name": "LightspeedBroker",
                            "connection_status": "CONNECTED",
                            "handshake_latency_ms": handshake_duration_ms,
                            "host": self.host,
                            "port": self.port,
                            "listen_thread_active": self._listen_thread_obj and self._listen_thread_obj.is_alive(),
                            "redis_worker_active": (hasattr(self, '_redis_worker_thread') and 
                                                   self._redis_worker_thread is not None and 
                                                   self._redis_worker_thread.is_alive()),
                            "event_bus_available": self.event_bus is not None,
                            "price_provider_available": self._price_provider is not None,
                            "order_repository_available": self._order_repository is not None,
                            "health_status": "HEALTHY",
                            "startup_timestamp": time.time()
                        }
                    )
                except Exception as e:
                    # Log warning but continue startup regardless - IPC must not block broker connection
                    self.logger.warning(f"Failed to publish startup health status (non-blocking): {e}")
                    # Continue startup without re-raising - this is critical for broker readiness
            else:
                self.connected = False
                self.logger.error(f"Handshake failed: Invalid response: {str(resp)[:200]}")
                event_payload = BrokerConnectionEventData(connected=False, message=f"Lightspeed handshake failed: Invalid response: {str(resp)[:100]}")
                self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                self.stop_listeners() # Ensure listener thread is stopped
                self.disconnect()   # Ensure socket is closed
                raise ConnectionRefusedError(f"No valid pong reply. Response: {str(resp)[:100]}")
        except concurrent.futures.TimeoutError:
            handshake_duration_ms = (time.time() - handshake_start_time) * 1000 
            self.connected = False
            self._handshake_completed = False
            self.logger.critical(f"LSB_HANDSHAKE: FATAL TIMEOUT after {handshake_duration_ms:.2f} ms.")
            self.logger.error(f"Handshake timed out after {handshake_duration_ms:.2f} ms.")
            event_payload = BrokerConnectionEventData(connected=False, message=f"Lightspeed handshake failed: timeout ({handshake_duration_ms:.2f} ms) waiting for pong.")
            self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
            self.stop_listeners()
            self.disconnect()
            raise ConnectionRefusedError(f"No 'pong' from extension (timeout after {handshake_duration_ms:.2f} ms).")
        except Exception as e: 
            self.connected = False
            self._handshake_completed = False
            self.logger.critical(f"LSB_HANDSHAKE: FATAL UNEXPECTED ERROR: {e}", exc_info=True)
            self.logger.error(f"Handshake failed (unexpected): {e}", exc_info=True)
            event_payload = BrokerConnectionEventData(connected=False, message=f"Lightspeed handshake failed: Unexpected error: {str(e)[:100]}")
            self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
            self.stop_listeners()
            self.disconnect()
            raise ConnectionRefusedError(f"Handshake failed unexpectedly: {str(e)[:100]}")

    @log_function_call('broker_bridge')
    def stop_listeners(self) -> None:
        # Stop Redis publishing worker first
        self._redis_worker_stop_event.set()
        if hasattr(self, '_redis_worker_thread') and self._redis_worker_thread is not None and self._redis_worker_thread.is_alive():
            self.logger.info("Stopping LightspeedBroker Redis worker thread...")
            self._redis_worker_thread.join(timeout=2.0)
            if self._redis_worker_thread.is_alive():
                self.logger.warning("LightspeedBroker Redis worker thread did not terminate in time.")

        self._stop_event.set()
        if self._listen_thread_obj and self._listen_thread_obj.is_alive():
            self.logger.debug("stop_listeners: Joining Lightspeed listen thread...")
            self._listen_thread_obj.join(timeout=3.0) # Increased join timeout slightly
            if self._listen_thread_obj.is_alive():
                self.logger.warning("stop_listeners: Lightspeed listen thread did not terminate in time.")
        self._listen_thread_obj = None
        self.logger.info("Stopped Lightspeed broker listeners.") # Changed to INFO
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        # FIX: Make shutdown IPC publishing also non-blocking
        try:
            self._publish_service_health_status(
                "STOPPED",
                "LightspeedBroker stopped and cleaned up resources",
                {
                    "service_name": "LightspeedBroker",
                    "connection_status": "DISCONNECTED",
                    "listen_thread_terminated": True,
                    "redis_worker_terminated": True,
                    "health_status": "STOPPED",
                    "shutdown_timestamp": time.time()
                }
            )
        except Exception as e:
            # During shutdown, IPC failures are expected and should not block
            self.logger.debug(f"Failed to publish shutdown health status (expected during shutdown): {e}")

    @log_function_call('broker_bridge')
    def is_connected(self) -> bool:
        return self.connected and self.client and self.client.sock is not None

    @property
    def is_ready(self) -> bool:
        """
        The broker is ready ONLY if the connection flag is true and the
        single worker thread is alive. This eliminates the "exiting thread" problem.
        """
        with self._lock:
            is_connected_flag = self._is_fully_connected
            handshake_completed_flag = self._handshake_completed
        
        # Check if the single worker thread is alive
        listen_thread_alive = (hasattr(self, '_listen_thread_obj') and 
                              self._listen_thread_obj is not None and 
                              self._listen_thread_obj.is_alive())
        
        # Check if Redis worker thread is alive (only if not in TANK mode)
        redis_worker_alive = True  # Default to True for TANK mode
        if not (hasattr(self, '_tank_mode') and self._tank_mode):
            redis_worker_alive = (hasattr(self, '_redis_worker_thread') and 
                                 self._redis_worker_thread is not None and 
                                 self._redis_worker_thread.is_alive())
        
        is_truly_ready = (is_connected_flag and 
                         handshake_completed_flag and 
                         listen_thread_alive and 
                         redis_worker_alive)
        
        if not is_truly_ready:
            self.logger.warning(f"LSB_READINESS_CHECK: FAILED. "
                              f"IsConnectedFlag={is_connected_flag}, "
                              f"HandshakeCompleted={handshake_completed_flag}, "
                              f"ListenThreadAlive={listen_thread_alive}, "
                              f"RedisWorkerAlive={redis_worker_alive}")
            return False
            
        return True

    def stop(self) -> None:
        """
        Implementation of the ILifecycleService's stop method.
        Stops all background threads and disconnects from the broker.
        """
        self.logger.info("Stopping LightspeedBroker service...")
        
        # Stop the listeners first (this includes Redis worker if it exists)
        self.stop_listeners()
        
        # Then disconnect from broker
        if self.is_connected():
            self.disconnect()
        
        # Ensure any other threads are properly stopped
        if hasattr(self, 'connect_thread') and self.connect_thread and self.connect_thread.is_alive():
            self.logger.debug("Waiting for connect thread to finish...")
            self.connect_thread.join(timeout=2.0)
            
        self.logger.info("LightspeedBroker service stopped.")

    @log_function_call('broker_bridge')
    def place_order(self,
                    symbol: str, side: str, quantity: float, order_type: str,
                    limit_price: Optional[float] = None, stop_price: Optional[float] = None,
                    time_in_force: str = "DAY", local_order_id: Optional[int] = None,
                    correlation_id: Optional[str] = None,
                    **kwargs ) -> Optional[str]:

        # Start timing for broker response metrics
        broker_start_time = time.time()

        # Log ALL IDs for complete tracing
        corr_id = correlation_id or "NO_CORR_ID"
        self.logger.info(f"[BROKER] Placing order with ALL IDs - "
                       f"CorrID: {corr_id}, "
                       f"LocalID: {local_order_id}, "
                       f"Symbol: {symbol}, "
                       f"Side: {side}, "
                       f"Qty: {quantity}, "
                       f"Type: {order_type}, "
                       f"Price: {limit_price if limit_price else 'MKT'}")

        self.logger.debug(f"ORDER_PATH_DEBUG: LightspeedBroker.place_order called with: symbol={symbol}, side={side}, quantity={quantity}, order_type={order_type}, limit_price={limit_price}, local_order_id={local_order_id}, correlation_id={correlation_id}")

        if not self.is_connected():
            self.logger.error("ORDER_PATH_DEBUG: Cannot place order: Not connected to Lightspeed.")
            
            # Publish broker submission error for connection failure
            order_params_dict = {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
                "limit_price": limit_price,
                "stop_price": stop_price
            }
            self._publish_broker_submission_error_to_redis(
                local_order_id=str(local_order_id) if local_order_id is not None else None,
                symbol=symbol,
                error_type="CONNECTION_FAILURE",
                error_message="Cannot place order: Not connected to Lightspeed broker",
                order_params=order_params_dict,
                correlation_id=correlation_id
            )
            
            rejection_data = OrderRejectedData(local_order_id=str(local_order_id) if local_order_id is not None else None, symbol=symbol, reason="Not connected to Lightspeed broker", timestamp=time.time())
            self.event_bus.publish(OrderRejectedEvent(data=rejection_data))
            return None

        # Parameter Validations
        error_msg = None
        if not symbol: error_msg = "Symbol cannot be empty."
        elif quantity <= 0: error_msg = f"Quantity must be positive, got {quantity}."
        elif side.upper() not in ["BUY", "SELL"]: error_msg = f"Invalid side: {side}."
        elif order_type.upper() not in ["LMT", "MKT", "STP", "STP LMT"]: error_msg = f"Unsupported order type: {order_type}."
        elif order_type.upper() in ["LMT", "STP LMT"] and (limit_price is None or limit_price <= 0):
            error_msg = f"Invalid limit price ({limit_price}) for {order_type} order."
        elif order_type.upper() in ["STP", "STP LMT"] and (stop_price is None or stop_price <= 0):
             error_msg = f"Invalid stop price ({stop_price}) for {order_type} order."

        if error_msg:
            self.logger.error(f"ORDER_PATH_DEBUG: {error_msg}")
            
            # Publish broker submission error for parameter validation failure
            order_params_dict = {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
                "limit_price": limit_price,
                "stop_price": stop_price
            }
            self._publish_broker_submission_error_to_redis(
                local_order_id=str(local_order_id) if local_order_id is not None else None,
                symbol=symbol,
                error_type="PARAMETER_VALIDATION_ERROR",
                error_message=error_msg,
                order_params=order_params_dict,
                correlation_id=correlation_id
            )
            
            rejection_data = OrderRejectedData(local_order_id=local_order_id, symbol=symbol, reason=error_msg, timestamp=time.time())
            self.event_bus.publish(OrderRejectedEvent(data=rejection_data))
            return None
        
        ls_order_type_map = {"MKT": "MKT", "LMT": "LMT", "STP": "STP", "STP LMT": "STPLMT"} # STPLMT for C++ bridge if it expects that
        ls_order_type = ls_order_type_map.get(order_type.upper())
        ls_action = side.upper() # C++ bridge expects "BUY" or "SELL"

        order_quantity_int = int(round(quantity))
        
        # Use local_order_id directly as int (now that interface is corrected)
        local_copy_id_int: Optional[int] = local_order_id

        # Generate request ID for tracking the request/response
        rid = get_next_req_id()
        fut = concurrent.futures.Future()
        store_pending_request(rid, fut)

        req = {
            "cmd": "place_order", 
            "req_id": rid,  # Add the request ID for proper tracking
            "symbol": symbol, 
            "action": ls_action,
            "quantity": order_quantity_int, 
            "orderType": ls_order_type,
            "outsideRth": kwargs.get("outsideRth", True), # C++ expects outsideRth
            "local_copy_id": local_copy_id_int if local_copy_id_int is not None else -1  # Pass actual ID for tracking
        }
        
        # Add correlation_id if provided (for IntelliSense end-to-end tracking)
        if correlation_id:
            req["correlation_id"] = correlation_id

        if ls_order_type in ["LMT", "STPLMT"] and limit_price is not None: req["limitPrice"] = limit_price
        if ls_order_type in ["STP", "STPLMT"] and stop_price is not None: req["stopPrice"] = stop_price # C++ needs stopPrice for STP too
        
        # TIF handling (assuming C++ bridge handles specific TIF values)
        # if time_in_force: req["timeInForce"] = time_in_force.upper() # Example: "DAY", "GTC"

        self.logger.critical(f"ORDER_PATH_DEBUG: Sending order request to C++ bridge: {req}")
        send_result = self.client.send_message_no_read(req)

        if send_result:
            self.logger.info(f"ORDER_PATH_DEBUG: Order request sent for {symbol} {ls_action} {order_quantity_int}")
            
            # Store correlation ID for this order (Break #3 & #4 fix)
            if correlation_id and local_order_id is not None:
                with self._correlation_lock:
                    self._order_correlations[str(local_order_id)] = correlation_id
                    self.logger.debug(f"Stored correlation {correlation_id} for local_order_id {local_order_id}")
            
            status_data = OrderStatusUpdateData(
                local_order_id=str(local_order_id) if local_order_id is not None else None, broker_order_id=None, symbol=symbol,
                status=OrderLSAgentStatus.PENDING_SUBMISSION, filled_quantity=0.0, average_fill_price=None,
                remaining_quantity=float(order_quantity_int),
                message=f"Order sent to Lightspeed: {ls_action} {order_quantity_int} {symbol} @{limit_price if limit_price else 'MKT'}",
                timestamp=time.time(),
                correlation_id=correlation_id  # Pass master correlation ID for status tracking
            )
            self.event_bus.publish(OrderStatusUpdateEvent(data=status_data))
            # Capture broker response time metric for successful orders (Enhanced with convenience method)
            if self._benchmarker:
                broker_response_time_ms = (time.time() - broker_start_time) * 1000
                self._benchmarker.gauge(
                    "lightspeed_bridge.broker_response_time_ms",
                    broker_response_time_ms,
                    context={'symbol': symbol, 'side': side, 'order_type': order_type, 'local_order_id': local_order_id, 'success': True}
                )
            return None # Ephemeral ID comes back via event
        else:
            self.logger.error(f"ORDER_PATH_DEBUG: Order request send FAILED for {symbol} {ls_action} {order_quantity_int}")
            
            # Clean up the pending request since send failed
            pop_pending_request(rid)

            # Capture broker response time metric for failed orders (Enhanced with convenience method)
            if self._benchmarker:
                broker_response_time_ms = (time.time() - broker_start_time) * 1000
                self._benchmarker.gauge(
                    "lightspeed_bridge.broker_response_time_ms",
                    broker_response_time_ms,
                    context={'symbol': symbol, 'side': side, 'order_type': order_type, 'local_order_id': local_order_id, 'success': False}
                )

            # Publish broker submission error to Redis
            order_params_dict = {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": order_quantity_int,
                "ls_action": ls_action,
                "limit_price": limit_price,
                "stop_price": stop_price
            }
            self._publish_broker_submission_error_to_redis(
                local_order_id=local_order_id,
                symbol=symbol,
                error_type="SOCKET_SEND_FAILURE",
                error_message=f"Failed to send {ls_action} order for {symbol} to broker bridge",
                order_params=order_params_dict,
                correlation_id=correlation_id
            )

            rejection_data = OrderRejectedData(local_order_id=local_order_id, symbol=symbol, reason="Failed to send order to broker bridge", timestamp=time.time())
            self.event_bus.publish(OrderRejectedEvent(data=rejection_data))
            return None

    @log_function_call('broker_bridge')
    def cancel_order(self, broker_order_id: Union[str, int], local_order_id: Optional[str] = None) -> bool:
        self.logger.debug(f"ORDER_PATH_DEBUG: LightspeedBroker.cancel_order called for broker_order_id={broker_order_id}, local_order_id={local_order_id}")
        if not self.is_connected():
            error_msg = f"Cannot cancel order {broker_order_id}: Not connected."
            self.logger.error(error_msg)
            
            # Publish order cancellation send failure event
            self._publish_broker_submission_error_to_redis(
                local_order_id=local_order_id,
                symbol="UNKNOWN",  # Symbol not available in cancel context
                error_type="CANCELLATION_CONNECTION_FAILURE",
                error_message=error_msg,
                order_params={"broker_order_id": str(broker_order_id), "cancellation_requested": True}
            )
            
            if local_order_id: # Try to update status if local_order_id known
                status_data = OrderStatusUpdateData(local_order_id=local_order_id, broker_order_id=str(broker_order_id), symbol="", status=OrderLSAgentStatus.CANCEL_REJECTED, message=error_msg, timestamp=time.time())
                self.event_bus.publish(OrderStatusUpdateEvent(data=status_data))
            return False
        try:
            ls_order_id = int(broker_order_id)
            if ls_order_id <= 0: raise ValueError("Broker order ID must be positive.")
        except (ValueError, TypeError) as e:
            error_msg = f"Invalid broker_order_id format/value: {broker_order_id} ({e})"
            self.logger.error(error_msg)
            
            # Publish order cancellation validation failure event
            self._publish_broker_submission_error_to_redis(
                local_order_id=local_order_id,
                symbol="UNKNOWN",
                error_type="CANCELLATION_PARAMETER_VALIDATION_ERROR",
                error_message=error_msg,
                order_params={"invalid_broker_order_id": str(broker_order_id), "exception_type": type(e).__name__}
            )
            
            if local_order_id:
                status_data = OrderStatusUpdateData(local_order_id=local_order_id, broker_order_id=str(broker_order_id), symbol="", status=OrderLSAgentStatus.CANCEL_REJECTED, message=error_msg, timestamp=time.time())
                self.event_bus.publish(OrderStatusUpdateEvent(data=status_data))
            return False

        cancel_req = {"cmd": "cancel_order", "ls_order_id": ls_order_id}
        # local_copy_id for cancel is not typically used by LS C++ side for cancel, but can be added if needed
        # if local_order_id: cancel_req["local_copy_id"] = local_order_id 

        self.logger.critical(f"ORDER_PATH_DEBUG: Sending cancel request to C++ bridge: {cancel_req}")
        send_result = self.client.send_message_no_read(cancel_req)
        
        if send_result:
            self.logger.info(f"ORDER_PATH_DEBUG: Cancel request sent for ls_order_id={ls_order_id}")
            if local_order_id: # Publish pending cancel status
                status_data = OrderStatusUpdateData(local_order_id=local_order_id, broker_order_id=str(ls_order_id), symbol="", status=OrderLSAgentStatus.PENDING_CANCEL, message=f"Cancel request sent for order ID {ls_order_id}", timestamp=time.time())
                self.event_bus.publish(OrderStatusUpdateEvent(data=status_data))
            return True
        else:
            error_msg = f"Failed to send cancel request for ls_order_id={ls_order_id}"
            self.logger.error(f"ORDER_PATH_DEBUG: {error_msg}")
            
            # Publish order cancellation socket send failure event
            self._publish_broker_submission_error_to_redis(
                local_order_id=local_order_id,
                symbol="UNKNOWN",
                error_type="CANCELLATION_SOCKET_SEND_FAILURE",
                error_message=error_msg,
                order_params={"broker_order_id": ls_order_id, "cancellation_requested": True}
            )
            
            if local_order_id:
                status_data = OrderStatusUpdateData(local_order_id=local_order_id, broker_order_id=str(ls_order_id), symbol="", status=OrderLSAgentStatus.CANCEL_REJECTED, message=error_msg, timestamp=time.time())
                self.event_bus.publish(OrderStatusUpdateEvent(data=status_data))
            return False
            
    def submit_order(self, order_params: Dict[str, Any]) -> Optional[str]:
        """
        Fulfills the IBrokerService interface contract.
        This method translates the generic 'order_params' dict into the specific
        arguments required by the existing place_order method.
        """
        # Extract correlation ID from order_params if available
        correlation_id = order_params.get("correlation_id") or order_params.get("client_order_id") or "NO_CORR_ID"
        local_order_id = order_params.get("local_order_id", "UNKNOWN")
        symbol = order_params.get("symbol", "UNKNOWN")
        side = order_params.get("side", "UNKNOWN")
        quantity = order_params.get("quantity", 0)
        
        # Log correlation ID for broker order submission
        self.logger.info(f"[BROKER] Submitting order with CorrID: {correlation_id}, LocalID: {local_order_id}, "
                        f"Symbol: {symbol}, Side: {side}, Qty: {quantity}")
        
        self.logger.debug(f"submit_order called (forwarding to place_order). Params: {order_params}")
        
        # This assumes the calling code (like TradeExecutor) will pass a dictionary
        # with keys that match the parameters of place_order.
        return self.place_order(
            symbol=order_params.get("symbol"),
            side=order_params.get("side"),
            quantity=order_params.get("quantity"),
            order_type=order_params.get("order_type"),
            limit_price=order_params.get("limit_price"),
            stop_price=order_params.get("stop_price"),
            time_in_force=order_params.get("time_in_force", "DAY"),
            local_order_id=order_params.get("local_order_id"),
            # Pass any other keyword arguments through
            **order_params.get("kwargs", {}) 
        )

    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Fulfills the IBrokerService interface contract.
        Currently, Lightspeed updates are push-based. This method is a placeholder
        for a potential future implementation that could query the C++ bridge.
        """
        self.logger.warning(
            f"get_order_status for order '{order_id}' was called, but this broker "
            f"implementation relies on push-based updates, not synchronous queries. Returning None."
        )
        # This method is required to exist, but since our architecture is
        # event-driven, we don't have a way to synchronously query an order's status.
        # The correct status will come via an OrderStatusUpdateEvent.
        return None
            
    @log_function_call('broker_bridge')
    def request_account_summary(self) -> None:
        if not self.is_connected(): self.logger.error("Cannot request account summary: Not connected."); return
        req = {"cmd": "get_account_data"} # C++ command is get_account_data
        if self.client.send_message_no_read(req): self.logger.debug("Account summary request sent.")
        else: self.logger.error("Failed to send account summary request.")

    @log_function_call('broker_bridge')
    def request_open_positions(self) -> None:
        if not self.is_connected(): self.logger.error("Cannot request open positions: Not connected."); return
        req = {"cmd": "get_all_positions"}
        if self.client.send_message_no_read(req): self.logger.debug("Open positions request sent.")
        else: self.logger.error("Failed to send open positions request.")

    @log_function_call('broker_bridge')
    def get_all_orders(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """
        🚨 NEW METHOD: Get all orders from Lightspeed broker.
        This method calls the C++ bridge 'get_orders_detailed' command.
        """
        if not self.is_connected():
            self.logger.error("Cannot get orders: Not connected.")
            return []

        try:
            fut = concurrent.futures.Future()
            rid = get_next_req_id()
            store_pending_request(rid, fut)
            msg = {"cmd": "get_orders_detailed", "req_id": rid}

            self.logger.info(f"🚨 ORDER SYNC: Requesting all orders from broker (req_id={rid})")
            send_success = self.client.send_message_no_read(msg)

            if not send_success:
                self.logger.error(f"🚨 ORDER SYNC: Failed to send get_orders_detailed request")
                return []

            # Wait for response
            response = fut.result(timeout=timeout)

            if not response or not response.get("ok", False):
                self.logger.warning(f"🚨 ORDER SYNC: Invalid orders response. req_id={rid}, Response: {str(response)[:200]}")
                return []

            # Extract orders from response
            raw_orders = response.get("orders", [])
            self.logger.info(f"🚨 ORDER SYNC: Received {len(raw_orders)} orders from broker")

            return raw_orders

        except concurrent.futures.TimeoutError:
            self.logger.error(f"🚨 ORDER SYNC: Timeout waiting for orders response (req_id={rid})")
            return []
        except Exception as e:
            self.logger.error(f"🚨 ORDER SYNC: Error getting orders: {e}", exc_info=True)
            return []

    @log_function_call('broker_bridge')
    def get_all_positions(self, timeout: float = 5.0) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        perf_timestamps = create_timestamp_dict() if is_performance_tracking_enabled() else {}
        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.get_positions_start')

        if not self.is_connected():
            self.logger.error("Cannot get positions: Not connected.")
            if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.get_positions_not_connected'); log_performance_durations(perf_timestamps, "BrokerBridge_GetAllPositions_NotConnected")
            self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=[])))
            self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message="Failed to get positions: Not connected", is_critical=False)))
            return None, False

        # Simplified retry logic for brevity, original was more complex
        for attempt in range(1): # Original had 2 attempts with reconnect, simplified here
            try:
                if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_attempt_{attempt}_start')
                fut = concurrent.futures.Future()
                rid = get_next_req_id()
                store_pending_request(rid, fut)
                msg = {"cmd": "get_all_positions", "req_id": rid}

                if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_before_send_{attempt}')
                send_success = self.client.send_message_no_read(msg)
                if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_after_send_{attempt}')

                if not send_success:
                    self.logger.error(f"get_all_positions: Failed to send request (req_id={rid}). Socket may be disconnected.")
                    pop_pending_request(rid)
                    self.connected = False # Assume disconnected on send fail
                    if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_send_failed_{attempt}'); log_performance_durations(perf_timestamps, f"BrokerBridge_GetAllPositions_SendFailed_{attempt}")
                    self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=[])))
                    self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message="Failed to send positions request: Socket disconnected", is_critical=True)))
                    return None, False
                
                try:
                    if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_wait_start_{attempt}')
                    response = fut.result(timeout=timeout)
                    if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_wait_end_{attempt}')
                except concurrent.futures.TimeoutError:
                    self.logger.error(f"get_all_positions: Timeout ({timeout}s) waiting for req_id={rid}.")
                    pop_pending_request(rid)
                    if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_timeout_{attempt}'); log_performance_durations(perf_timestamps, f"BrokerBridge_GetAllPositions_Timeout_{attempt}")
                    self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=[])))
                    self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message=f"Timeout waiting for positions response ({timeout}s)", is_critical=False)))
                    return None, False

                if not response or not response.get("ok", False):
                    self.logger.warning(f"get_all_positions: API error or empty response. req_id={rid}, Response: {str(response)[:200]}")
                    if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_invalid_response_{attempt}'); log_performance_durations(perf_timestamps, f"BrokerBridge_GetAllPositions_InvalidResponse_{attempt}")
                    self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=[])))
                    self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message=f"Invalid positions response: {str(response)[:100]}", is_critical=False)))
                    return None, False

                raw_positions = response.get("positions", [])
                position_data_list = []
                for pos in raw_positions:
                    try:
                        position_data_list.append(PositionData(
                            symbol=pos.get("symbol", ""),
                            quantity=float(pos.get("shares", 0.0)),
                            average_price=float(pos.get("avg_price", 0.0)),
                            unrealized_pnl=float(pos.get("unrealized_pnl")) if pos.get("unrealized_pnl") is not None else None
                        ))
                    except (ValueError, TypeError) as e: self.logger.error(f"Error converting position data: {e}, raw: {pos}")
                
                self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=position_data_list)))
                if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.get_positions_success'); log_performance_durations(perf_timestamps, "BrokerBridge_GetAllPositions_Success")
                self.logger.info(f"get_all_positions: Successfully fetched {len(raw_positions)} positions (req_id={rid}).")
                return raw_positions, True

            except OSError as e: # Catch socket/network errors during send/receive
                self.logger.error(f"OSError in get_all_positions (attempt {attempt}): {e}. Marking as disconnected.")
                self.connected = False # Crucial: update connection state
                self.client.close()    # Ensure client socket is closed
                if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_os_error_{attempt}')
                # No reconnect logic here for simplicity, caller should handle disconnect
            except Exception as e:
                self.logger.error(f"Unexpected error in get_all_positions (attempt {attempt}): {e}", exc_info=True)
                if perf_timestamps: add_timestamp(perf_timestamps, f'broker_bridge.get_positions_exception_{attempt}')
        
        # If loop finishes without returning success
        if perf_timestamps: log_performance_durations(perf_timestamps, "BrokerBridge_GetAllPositions_AllAttemptsFailed")
        self.logger.error("get_all_positions: Failed after all attempts.")
        self.event_bus.publish(AllPositionsEvent(data=AllPositionsData(positions=[])))
        self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message="Failed to get positions after all attempts", is_critical=False)))
        return None, False

    def _log_position_performance(self, perf_timestamps: Dict[str, float]) -> None: # Was present but unused
        pass # Original was commented out, keeping it minimal

    @log_function_call('broker_bridge')
    def _listen_loop(self) -> None:
        self.logger.critical("LSB_LISTEN_THREAD: Loop started.")
        self.logger.info("Entering Lightspeed listen loop.") # Changed to INFO

        # --- START ADDITION: Wait if core dependencies aren't ready ---
        initial_wait_cycles = 0
        max_initial_wait_cycles = 50 # e.g., 50 * 0.1s = 5 seconds
        while not self._order_repository and initial_wait_cycles < max_initial_wait_cycles and not self._stop_event.is_set():
            self.logger.warning(f"_listen_loop: OrderRepository not yet set in LightspeedBroker. Waiting... (Cycle: {initial_wait_cycles + 1})")
            time.sleep(0.1)
            initial_wait_cycles += 1

        if not self._order_repository and not self._stop_event.is_set():
            self.logger.error("_listen_loop: OrderRepository was NOT set after waiting. Enrichment/compensatory logic will fail for early messages.")
        elif not self._stop_event.is_set():
            self.logger.info(f"_listen_loop: OrderRepository confirmed set in LightspeedBroker (ID: {id(self._order_repository)}). Proceeding.")
        # --- END ADDITION ---

        while not self._stop_event.is_set():
            line: Optional[str] = None
            try:
                if self._stop_event.is_set(): break
                
                # Use select for non-blocking check on socket readability
                if not self.client.sock: # Socket might have been closed by another operation
                    self.logger.warning("_listen_loop: Socket is None, attempting to reconnect or exiting.")
                    if not self._stop_event.is_set(): time.sleep(1) # Avoid tight loop if reconnect fails
                    # Reconnect logic could be added here, or simply let the loop break
                    # For now, if socket is gone, we can't proceed.
                    self.connected = False # Mark as disconnected
                    event_payload = BrokerConnectionEventData(connected=False, message="Lightspeed connection lost: socket became None.")
                    self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                    break # Exit loop
                    
                ready_to_read, _, _ = select.select([self.client.sock], [], [], 0.1) # 0.1s timeout for select

                if self._stop_event.is_set(): break

                if ready_to_read:
                    line = self.client._recv_line()
                else: # Timeout from select, no data
                    continue 

                if self._stop_event.is_set(): break
                if line is None: # _recv_line returns None on clean disconnect or error
                    if not self.client.sock: # If socket was closed by _recv_line
                        self.logger.info("_listen_loop: _recv_line indicated socket closed. Exiting loop.")
                        self.connected = False
                        event_payload = BrokerConnectionEventData(connected=False, message="Lightspeed connection lost: _recv_line reported closed socket.")
                        self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                        break
                    continue # Go back to select

                # Message counting
                self._broker_msg_count += 1
                current_time = time.time()
                if current_time - self._broker_last_log_time > self._broker_log_interval:
                    rate = self._broker_msg_count / (current_time - self._broker_last_log_time)
                    self.logger.info(f"BrokerSocket: Received {self._broker_msg_count} msgs in last {current_time - self._broker_last_log_time:.1f}s (Rate: {rate:.1f} msg/s)")
                    self._broker_msg_count = 0
                    self._broker_last_log_time = current_time
                
                # Cross-own detection (simple string check)
                if "You cannot cross your own" in line: # This was your original logic
                    self.logger.warning(f"Detected cross-your-own => {line}")
                    ls_oid = self._extract_order_id_from_line(line)
                    cross_own_data = {"event": "cross_own_error", "ls_order_id": ls_oid, "error_message": line}
                    self._handle_inbound_message(cross_own_data) # No perf_timestamps for this specific path
                    continue

                try:
                    self.logger.debug(f"ORDER_PATH_DEBUG: RAW_RECV_LINE from C++: {line}")
                    data = json.loads(line)
                    evt_type = data.get("event")
                    req_id_from_data = data.get("req_id")
                    self.logger.debug(f"ORDER_PATH_DEBUG: PARSED_DATA - Event: '{evt_type}', ReqID: '{req_id_from_data}'")
                    if evt_type in ["order_status", "raw_share_update", "positions_update"]:
                         self.logger.critical(f"BROKER_RAW_DATA: Event='{evt_type}', FullData={json.dumps(data)}") # Use dumps for better readability if data is complex
                    
                except json.JSONDecodeError as e:
                    self.logger.error(f"ORDER_PATH_DEBUG: JSON decode error. Raw line: '{line}', Error: {e}")
                    continue
                
                perf_timestamps_msg = create_timestamp_dict() if is_performance_tracking_enabled() else None
                self._handle_inbound_message(data, perf_timestamps_msg)

            except Exception as ex: # Catch-all for unexpected issues in the loop
                self._is_fully_connected = False
                self._handshake_completed = False
                self.logger.critical(f"LSB_LISTEN_THREAD: FATAL LISTENER ERROR. Connection lost: {ex}", exc_info=True)
                self.logger.error(f"Critical error in _listen_loop: {ex}", exc_info=True)
                self.connected = False
                event_payload = BrokerConnectionEventData(connected=False, message=f"Lightspeed connection lost: Unhandled exception in listen loop: {str(ex)[:100]}")
                self.event_bus.publish(BrokerConnectionEvent(data=event_payload))
                self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message="Critical exception in Lightspeed listen_loop", details=str(ex), is_critical=True)))
                break # Exit loop on critical error

        self._is_fully_connected = False
        self._handshake_completed = False
        self.logger.critical("LSB_LISTEN_THREAD: Loop exited. Connected: False.")
        self.logger.info("Leaving Lightspeed listen loop.") # Changed to INFO

    def _extract_order_id_from_line(self, line: str) -> Optional[int]: # Return Optional[int]
        match = re.search(r"\s(\d+)[A-Za-z]*\b", line) # Original regex
        if match:
            try:
                return int(match.group(1))
            except ValueError: # Be specific about exception
                self.logger.warning(f"_extract_order_id_from_line: Could not convert '{match.group(1)}' to int.")
        return None # Return None if not found or conversion error

    @log_function_call('broker_bridge')
    def _handle_inbound_message(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        evt = data.get("event")
        status_from_data = data.get("status") # Renamed to avoid conflict with OrderLSAgentStatus
        rid = data.get("req_id")
        
        # --- CIRCULAR WAIT FIX: Handle request/response IMMEDIATELY ---
        # STEP 1: Resolve any pending futures FIRST, before any other processing.
        # This unblocks threads waiting on responses (like the handshake).
        if rid is not None:
            future_obj = pop_pending_request(rid)
            if future_obj:
                if isinstance(future_obj, concurrent.futures.Future):
                    future_obj.set_result(data)
                elif isinstance(future_obj, threading.Event):
                    future_obj.set()
                self.logger.info(f"Resolved pending request for req_id={rid} (event='{evt}').")
                
                # If this was a response event, we're done - no need for further processing
                if evt and evt.endswith("_response"):
                    return
        # --- END CIRCULAR WAIT FIX ---
        
        self.logger.critical(f"BROKER_RAW_DATA_DISPATCH: Event='{evt}', Status='{status_from_data}', FullData={json.dumps(data)}")

        # STEP 2: Now publish telemetry - safe because waiting threads are unblocked
        # Publish raw broker message to Redis stream for IntelliSense
        # Queue-based publishing to prevent blocking listen thread
        publish_task = {
            "target": self._publish_broker_raw_message_to_redis,
            "args": (data, "INBOUND"),
            "kwargs": {}
        }
        try:
            # Add queue monitoring for better visibility
            queue_size = self._redis_publish_queue.qsize()
            if queue_size > 1500:  # Warn at 75% capacity (2000 max)
                self.logger.warning(f"Redis publishing queue is {queue_size/20:.1f}% full ({queue_size}/2000)")
            
            self._redis_publish_queue.put_nowait(publish_task)
        except queue.Full:
            self.logger.warning("Redis publishing queue is full. Raw message was dropped. Consider increasing queue size or reducing message volume.")

        if evt is None and status_from_data:
            # This was your original logic to infer order_status
            if status_from_data in ["SentToLightspeed", "Submitted", "Accepted", "Working", "Filled", "Cancelled", "Rejected", "PartiallyFilled"]:
                self.logger.info(f"ORDER_PATH_DEBUG: Message missing 'event' but has status '{status_from_data}' - treating as order_status")
                evt = "order_status" 
            else: # Added PartiallyFilled
                self.logger.error(f"ORDER_PATH_DEBUG: Received message with None event and unrecognized status '{status_from_data}'. Data: {json.dumps(data)}")
                # Relay as raw if it has correlation IDs
                local_copy_id_raw = data.get("local_copy_id")
                ephemeral_corr_raw = data.get("ephemeral_corr")
                if local_copy_id_raw is not None or ephemeral_corr_raw is not None:
                    relay_data_raw = BrokerRawMessageRelayEventData(raw_message_dict=data, original_local_copy_id=str(local_copy_id_raw) if local_copy_id_raw is not None else None)
                    self.event_bus.publish(BrokerRawMessageRelayEvent(data=relay_data_raw))
                return # Don't process further if event is truly unknown

        if perf_timestamps is None and is_performance_tracking_enabled(): perf_timestamps = create_timestamp_dict()
        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_message_start')

        # Response events are now handled at the beginning of the method to prevent circular wait
        # If we get here with a response event, it means it was already processed
        if evt in ["ping_response", "get_all_positions_response", "get_orders_response", "get_position_response"]:
            # Already handled above, just log performance if needed
            if perf_timestamps: 
                add_timestamp(perf_timestamps, f'broker_bridge.handle_message_end_{evt}')
                self._log_broker_message_performance(perf_timestamps)
            return
        
        handler_map = {
            "order_status": self._handle_order_status_update,
            # "order_fill": self._handle_order_fill, # C++ sends fills within order_status
 # C++ sends rejections within order_status mostly
            "positions_update": lambda d, p: self._handle_broker_full_positions_push(d.get("positions", []), p), # Full position snapshots from C++
            "raw_share_update": self._handle_broker_quantity_only_push, # Lean quantity-only updates from C++
            "account_update": self._handle_account_update,
            "cross_own_error": self._handle_cross_own_error, # From manual detection in _listen_loop
            "broker_error": self._handle_broker_error, # If C++ sends explicit error events
            "trading_status": self._handle_trading_status_update # For LULD, Halts, Resumes
        }

        handler_func = handler_map.get(evt)
        if handler_func:
            self.logger.info(f"ORDER_PATH_DEBUG: Dispatching event '{evt}' to handler: {handler_func.__name__}")
            try:
                handler_func(data, perf_timestamps) # Pass perf_timestamps to handlers that might use it
            except Exception as e_handler:
                self.logger.error(f"Error in handler {handler_func.__name__} for event '{evt}': {e_handler}", exc_info=True)
                self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message=f"Error in handler for {evt}", details=str(e_handler), is_critical=False)))
        else:
            self.logger.debug(f"ORDER_PATH_DEBUG: Unhandled event type in dispatch: {evt}, data: {json.dumps(data)}")
            local_copy_id_unh = data.get("local_copy_id")
            ephemeral_corr_unh = data.get("ephemeral_corr")
            if local_copy_id_unh is not None or ephemeral_corr_unh is not None:
                self.logger.info(f"BROKER_BRIDGE: Relaying unhandled event '{evt}' with order data. LocalCopyID={local_copy_id_unh}, EphemeralCorr={ephemeral_corr_unh}")
                relay_data_unh = BrokerRawMessageRelayEventData(raw_message_dict=data, original_local_copy_id=str(local_copy_id_unh) if local_copy_id_unh is not None else None)
                self.event_bus.publish(BrokerRawMessageRelayEvent(data=relay_data_unh))
            else:
                self.logger.info(f"BROKER_BRIDGE: Received unhandled event type '{evt}'. No order data found. Ignoring. Event data: {json.dumps(data)}")

        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_message_end'); self._log_broker_message_performance(perf_timestamps)

    def _handle_order_status_update(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        try:
            local_order_id_str = str(data.get("local_copy_id")) if data.get("local_copy_id") is not None else None
            broker_order_id_str = str(data.get("ls_order_id")) if data.get("ls_order_id") is not None else None
            symbol_str = data.get("symbol", "")
            raw_broker_status_str = data.get("status", "UNKNOWN").upper()
            ephemeral_corr_str = str(data.get("ephemeral_corr")) if data.get("ephemeral_corr") is not None else None # Get ephemeral_corr

            actual_local_id_to_use = local_order_id_str # Start with what C++ gave

            # --- START COMPENSATORY LOGIC ---
            if not actual_local_id_to_use or actual_local_id_to_use == '-1' or actual_local_id_to_use == '0':
                self.logger.warning(
                    f"[{symbol_str}] LSB_COMPENSATE: Received status '{raw_broker_status_str}' with invalid local_copy_id='{local_order_id_str}'. "
                    f"Attempting lookup. BrokerID='{broker_order_id_str}', EphemeralCorr='{ephemeral_corr_str}'"
                )
                if self._order_repository:
                    found_order_obj = None # Using the Order type from order_data_types

                    # Try broker_order_id first
                    if broker_order_id_str and broker_order_id_str != '-1' and broker_order_id_str != '0':
                        try:
                            found_order_obj = self._order_repository.get_order_by_broker_id(broker_order_id_str)
                            if found_order_obj:
                                actual_local_id_to_use = str(found_order_obj.local_id)
                                self.logger.info(
                                    f"[{symbol_str}] LSB_COMPENSATE: Found local_id '{actual_local_id_to_use}' using broker_id '{broker_order_id_str}' for status '{raw_broker_status_str}'."
                                )
                        except Exception as e_lookup_broker:
                            self.logger.error(f"[{symbol_str}] LSB_COMPENSATE: Error looking up by broker_id '{broker_order_id_str}': {e_lookup_broker}")

                    # Fallback to ephemeral_corr if not found by broker_id
                    if (not found_order_obj or not actual_local_id_to_use or actual_local_id_to_use == '-1') and \
                       ephemeral_corr_str and ephemeral_corr_str != '-1' and ephemeral_corr_str != '0':
                        try:
                            if hasattr(self._order_repository, 'get_order_by_ephemeral_corr'):
                                 found_order_obj_eph = self._order_repository.get_order_by_ephemeral_corr(ephemeral_corr_str)
                                 if found_order_obj_eph:
                                     actual_local_id_to_use = str(found_order_obj_eph.local_id)
                                     self.logger.info(
                                         f"[{symbol_str}] LSB_COMPENSATE: Found local_id '{actual_local_id_to_use}' using ephemeral_corr '{ephemeral_corr_str}' for status '{raw_broker_status_str}'."
                                     )
                            else:
                                self.logger.error(f"[{symbol_str}] LSB_COMPENSATE: OrderRepository missing 'get_order_by_ephemeral_corr' method!")

                        except Exception as e_lookup_eph:
                             self.logger.error(f"[{symbol_str}] LSB_COMPENSATE: Error looking up by ephemeral_corr '{ephemeral_corr_str}': {e_lookup_eph}")

                    if not actual_local_id_to_use or actual_local_id_to_use == '-1' or actual_local_id_to_use == '0':
                        self.logger.error(
                            f"[{symbol_str}] LSB_COMPENSATE: FAILED to find valid local_id for status '{raw_broker_status_str}'. "
                            f"Original local_copy_id='{local_order_id_str}', BrokerID='{broker_order_id_str}', EphemeralCorr='{ephemeral_corr_str}'. "
                            f"Order update will likely be MISSED for the original order."
                        )
                        actual_local_id_to_use = local_order_id_str
                    else:
                        self.logger.info(f"[{symbol_str}] LSB_COMPENSATE: Using CORRECTED local_order_id='{actual_local_id_to_use}' for status '{raw_broker_status_str}'.")
                else:
                    self.logger.error(f"[{symbol_str}] LSB_COMPENSATE: OrderRepository not available. Cannot correct invalid local_copy_id='{local_order_id_str}'.")
                    actual_local_id_to_use = local_order_id_str # Proceed with potentially incorrect ID
            # --- END COMPENSATORY LOGIC ---

            # Get validation_id for pipeline tracking (using the potentially corrected ID)
            validation_id = None
            if actual_local_id_to_use and actual_local_id_to_use != '-1': # Only get validation_id if we have a valid-looking local_id
                validation_id = self.get_validation_id_for_local_order(actual_local_id_to_use)

            # Register pipeline stages based on broker status
            if validation_id and self._pipeline_validator:
                if raw_broker_status_str in ["SUBMITTED", "ACCEPTED", "WORKING"]:
                    # Broker acknowledged the order
                    self._pipeline_validator.register_stage_completion(
                        validation_id,
                        'broker_ack_received',
                        stage_data={'local_order_id': actual_local_id_to_use, 'broker_status': raw_broker_status_str}
                    )
            
            # These come from C++ for Exec type order_status
            fill_quantity_val = float(data.get("fill_shares", 0.0)) 
            average_fill_price_val = float(data.get("fill_price")) if data.get("fill_price") is not None else None
            
            # Remaining quantity might not be sent by C++ in order_status for fills, calculate if needed or default
            requested_shares = float(data.get("requested_shares", 0.0))
            # If it's a fill, C++ gives 'executedShares' in the L_Order object, not directly in message for remaining.
            # Let's assume remaining_quantity is implicitly (requested_shares - total_filled_so_far)
            # This handler might need state or rely on OrderRepository to calculate true remaining.
            # For now, if it's a fill, remaining might be requested - this_fill if not otherwise provided.
            # This is a simplification; true remaining needs full order state.
            current_remaining_qty = requested_shares - fill_quantity_val if fill_quantity_val > 0 else requested_shares


            message_str = data.get("reason", data.get("message", "")) # Prefer "reason" if it's a rejection status
            if not message_str and raw_broker_status_str == "REJECTED": # C++ sends "reason" for rejections
                message_str = "Order rejected by broker."


            status_mapping = {
                "SENTTOLIGHTSPEED": OrderLSAgentStatus.SENT_TO_LIGHTSPEED, # From C++ place_order optimistic response (if it were an event)
                "SUBMITTED": OrderLSAgentStatus.LS_SUBMITTED, # C++ L_OrderChange::Create
                "ACCEPTED": OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE, # C++ L_OrderChange::Receive
                "PARTIALLYFILLED": OrderLSAgentStatus.PARTIALLY_FILLED, # C++ L_OrderChange::Exec (partial)
                "FILLED": OrderLSAgentStatus.FILLED, # C++ L_OrderChange::Exec (full)
                "CANCELPENDING": OrderLSAgentStatus.PENDING_CANCEL, # C++ L_OrderChange::CancelCreate
                "CANCELLED": OrderLSAgentStatus.CANCELLED, # C++ L_OrderChange::Cancel
                "REJECTED": OrderLSAgentStatus.LS_REJECTION_FROM_BROKER, # C++ L_MsgOrderRequested or L_OrderChange::Rejection
                "CANCELREJECTED": OrderLSAgentStatus.CANCEL_REJECTED, # C++ L_OrderChange::CancelRejection
                "KILLED": OrderLSAgentStatus.LS_KILLED, # C++ L_OrderChange::Kill
                "UNKNOWN": OrderLSAgentStatus.UNKNOWN
            }
            # Add common variations to mapping
            status_mapping["WORKING"] = OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE # Common alias for accepted/live
            status_mapping["CANCELED"] = OrderLSAgentStatus.CANCELLED # Alternate spelling

            standardized_status_enum = status_mapping.get(raw_broker_status_str.replace(" ", ""), OrderLSAgentStatus.UNKNOWN) # Remove spaces for matching

            # If status is REJECTED and no specific reason, check C++ "reason" field
            if standardized_status_enum == OrderLSAgentStatus.LS_REJECTION_FROM_BROKER and not message_str:
                message_str = data.get("reason", "Rejected by broker")

            # --- START ENRICHMENT LOGIC ---
            # Initialize final variables with data from the C++ message (or defaults)
            final_symbol_str = data.get("symbol", "")
            final_requested_shares = float(data.get("requested_shares", 0.0))
            # initial_remaining uses requested_shares from C++ message if available, else full requested_shares
            initial_remaining_qty = final_requested_shares - fill_quantity_val if fill_quantity_val > 0 else final_requested_shares
            final_remaining_quantity = initial_remaining_qty
            final_message_str = message_str # message_str was already populated from data.get("reason", data.get("message", ""))
            final_side_str = data.get("side", "").upper() # Get side from C++ message if available

            is_lean_status_from_cpp = standardized_status_enum == OrderLSAgentStatus.SENT_TO_LIGHTSPEED

            # Determine if critical info is missing for non-terminal/non-fill statuses
            non_final_non_fill_status = standardized_status_enum not in [
                OrderLSAgentStatus.FILLED, OrderLSAgentStatus.PARTIALLY_FILLED,
                OrderLSAgentStatus.LS_REJECTION_FROM_BROKER, OrderLSAgentStatus.CANCELLED,
                OrderLSAgentStatus.CANCEL_REJECTED, OrderLSAgentStatus.LS_KILLED
            ]
            critical_info_missing = (not final_symbol_str) or (non_final_non_fill_status and final_requested_shares == 0.0)

            if (is_lean_status_from_cpp or critical_info_missing):
                order_looked_up = False
                # Ensure actual_local_id_to_use is valid before attempting int conversion and lookup
                if actual_local_id_to_use and actual_local_id_to_use not in ['-1', '0', 'None', None]:
                    if self._order_repository: # Check if OrderRepository is available
                        try:
                            repo_local_id = int(actual_local_id_to_use)
                            order_from_repo = self._order_repository.get_order_by_local_id(repo_local_id)
                            order_looked_up = True

                            if order_from_repo:
                                self.logger.info(f"[{order_from_repo.symbol if hasattr(order_from_repo, 'symbol') else 'RepoOrder'}] Enriching status update for local_id '{actual_local_id_to_use}' from OrderRepository.")

                                if not final_symbol_str and hasattr(order_from_repo, 'symbol'):
                                    final_symbol_str = order_from_repo.symbol

                                if not final_side_str and hasattr(order_from_repo, 'side'):
                                    final_side_str = order_from_repo.side.value if hasattr(order_from_repo.side, 'value') else str(order_from_repo.side)

                                repo_req_shares = getattr(order_from_repo, 'requested_shares', 0.0)
                                repo_leftover_shares = getattr(order_from_repo, 'leftover_shares', repo_req_shares)

                                if is_lean_status_from_cpp: # Specifically for SentToLightspeed
                                    final_requested_shares = repo_req_shares
                                    final_remaining_quantity = repo_req_shares # Full amount is remaining
                                elif final_requested_shares == 0.0 and non_final_non_fill_status:
                                    final_requested_shares = repo_req_shares
                                    final_remaining_quantity = repo_leftover_shares

                                # Enrich message string if it's empty for certain statuses
                                if not final_message_str:
                                    repo_sym_for_msg = getattr(order_from_repo, 'symbol', 'N/A')
                                    repo_qty_for_msg = getattr(order_from_repo, 'requested_shares', 'N/A')
                                    repo_side_for_msg = "UNKNOWN"
                                    if hasattr(order_from_repo, 'side'):
                                        repo_side_for_msg = order_from_repo.side.value if hasattr(order_from_repo.side, 'value') else str(order_from_repo.side)

                                    if standardized_status_enum == OrderLSAgentStatus.SENT_TO_LIGHTSPEED:
                                        final_message_str = f"Broker ACK for {repo_side_for_msg} {repo_qty_for_msg} {repo_sym_for_msg}"
                                    elif standardized_status_enum == OrderLSAgentStatus.LS_SUBMITTED:
                                        final_message_str = f"Order Submitted ({repo_side_for_msg} {repo_qty_for_msg} {repo_sym_for_msg})"
                                    elif standardized_status_enum == OrderLSAgentStatus.LS_ACCEPTED_BY_EXCHANGE:
                                        final_message_str = f"Order Accepted ({repo_side_for_msg} {repo_qty_for_msg} {repo_sym_for_msg})"
                            else: # order_from_repo is None
                                self.logger.warning(f"Could not find order with local_id '{repo_local_id}' in OrderRepository for enrichment.")
                        except ValueError: # Handles if actual_local_id_to_use is not a valid int string
                            self.logger.warning(f"Invalid local_id format '{actual_local_id_to_use}' for OrderRepository lookup during enrichment.")
                        except Exception as e_repo_lookup:
                            self.logger.error(f"Error looking up/using order '{actual_local_id_to_use}' in OrderRepository: {e_repo_lookup}", exc_info=True)
                    else: # self._order_repository is None
                        # This log is already present in your latest logs, indicating this path was hit.
                        self.logger.warning(f"OrderRepository not available for enriching status update for local_id '{actual_local_id_to_use}'.")

                if not order_looked_up and (is_lean_status_from_cpp or critical_info_missing):
                     self.logger.warning(f"Skipped OrderRepository enrichment for local_id '{actual_local_id_to_use}' due to invalid ID or missing repo. Using data from C++ message only.")

            # Ensure remaining_quantity is non-negative and consistent
            if final_requested_shares > 0 and fill_quantity_val >= 0: # Basic sanity for calculation
                calculated_rem_qty = final_requested_shares - fill_quantity_val
                if abs(calculated_rem_qty - final_remaining_quantity) > 1e-9 and is_lean_status_from_cpp : # If lean and different, trust calc from repo data
                    final_remaining_quantity = calculated_rem_qty
                elif non_final_non_fill_status and final_remaining_quantity == 0.0 and final_requested_shares > 0.0:
                    # If broker sent 0 remaining for a non-final/non-fill, but we know requested, assume full remaining
                    final_remaining_quantity = final_requested_shares

            final_remaining_quantity = max(0.0, final_remaining_quantity)
            # --- END ENRICHMENT LOGIC ---

            # Retrieve stored correlation ID for this order (Break #4 fix)
            stored_correlation_id = None
            if actual_local_id_to_use:
                with self._correlation_lock:
                    stored_correlation_id = self._order_correlations.get(str(actual_local_id_to_use))
                    if stored_correlation_id:
                        self.logger.debug(f"Retrieved correlation {stored_correlation_id} for local_order_id {actual_local_id_to_use}")
            
            # Log ALL IDs for complete tracing
            corr_id = stored_correlation_id or "MISSING_CORRELATION_ID"
            self.logger.info(f"[BROKER] Processing order status update with ALL IDs - "
                           f"CorrID: {corr_id}, "
                           f"LocalID: {actual_local_id_to_use}, "
                           f"BrokerID: {broker_order_id_str}, "
                           f"EphemeralCorr: {ephemeral_corr_str}, "
                           f"Symbol: {final_symbol_str}, "
                           f"Status: {standardized_status_enum.value}, "
                           f"FillQty: {fill_quantity_val}, "
                           f"FillPx: {average_fill_price_val}")

            # Now create status_data using the 'final_' prefixed variables
            status_data = OrderStatusUpdateData(
                local_order_id=actual_local_id_to_use,
                broker_order_id=broker_order_id_str,
                symbol=final_symbol_str, # Use enriched symbol
                status=standardized_status_enum,
                filled_quantity=fill_quantity_val,
                average_fill_price=average_fill_price_val,
                remaining_quantity=final_remaining_quantity, # Use enriched/calculated remaining quantity
                message=final_message_str, # Use enriched message
                timestamp=time.time(),
                correlation_id=stored_correlation_id  # Include master correlation ID for end-to-end tracing
                # If C++ sends a precise timestamp for the event, prefer that:
                # timestamp=data.get("broker_event_timestamp_utc_sec", time.time())
            )
            
            # Get causation_id from order if available
            causation_id = None
            if 'order_from_repo' in locals() and order_from_repo:
                causation_id = getattr(order_from_repo, 'client_order_id', None)
            
            self.event_bus.publish(OrderStatusUpdateEvent(
                data=status_data
            ))
            # Log the final values that were published
            self.logger.info(f"Published OrderStatusUpdateEvent for LocalID='{actual_local_id_to_use}', BrokerID='{broker_order_id_str}', Symbol='{final_symbol_str}', Status='{standardized_status_enum.value}', RemQty={final_remaining_quantity}")

            # If this is a rejection, also publish OrderRejectedEvent for OrderRepository
            if standardized_status_enum == OrderLSAgentStatus.LS_REJECTION_FROM_BROKER:
                rejection_data = OrderRejectedData(
                    local_order_id=actual_local_id_to_use, # Use the corrected ID
                    broker_order_id=broker_order_id_str,
                    symbol=final_symbol_str, # Use enriched symbol
                    reason=final_message_str, # Use enriched message
                    timestamp=time.time()
                )
                self.event_bus.publish(OrderRejectedEvent(
                    data=rejection_data
                ))
                self.logger.info(f"Published OrderRejectedEvent for LocalID='{actual_local_id_to_use}', Symbol='{final_symbol_str}', Reason='{final_message_str}'")

            # If this status update IS A FILL (partial or full), also publish OrderFilledEvent
            if standardized_status_enum in [OrderLSAgentStatus.FILLED, OrderLSAgentStatus.PARTIALLY_FILLED] and fill_quantity_val > 0:
                if average_fill_price_val is None:
                    self.logger.error(f"Cannot publish OrderFilledEvent for {final_symbol_str} ({broker_order_id_str}): "
                                      f"Status is {standardized_status_enum.value} with fill_quantity {fill_quantity_val} but average_fill_price is None. Data: {data}")
                    return

                # Use enriched side if available, otherwise fall back to broker data
                raw_broker_side_str = final_side_str if final_side_str else data.get("side", "").upper()
                side_enum = OrderSide.BUY if raw_broker_side_str == "BUY" else OrderSide.SELL if raw_broker_side_str == "SELL" else OrderSide.UNKNOWN
                if side_enum == OrderSide.UNKNOWN:
                     self.logger.warning(f"Unknown side '{raw_broker_side_str}' in order_status fill event for {final_symbol_str}. Defaulting to BUY for fill event.")
                     side_enum = OrderSide.BUY # Fallback for safety, but should be logged

                # Use precise broker timestamps if C++ added them to this order_status message
                fill_ts_utc_sec = data.get("broker_exec_time_utc_sec")
                fill_ts_ms_est_midnight = data.get("broker_exec_time_ms_est_midnight") # For logging or future use
                
                final_fill_timestamp = time.time() # Default
                if fill_ts_utc_sec is not None:
                    try:
                        final_fill_timestamp = float(fill_ts_utc_sec)
                        # Optionally add millisecond precision from fill_ts_ms_est_midnight if needed and parseable
                        # e.g., final_fill_timestamp += (fill_ts_ms_est_midnight % 1000) / 1000.0
                        self.logger.debug(f"Using broker_exec_time_utc_sec ({final_fill_timestamp}) for OrderFilledEvent.")
                    except ValueError:
                        self.logger.warning(f"Could not convert broker_exec_time_utc_sec '{fill_ts_utc_sec}' to float. Using current time for fill.")
                
                fill_data = OrderFilledEventData(
                    order_id=broker_order_id_str,
                    local_order_id=actual_local_id_to_use, # Use the corrected ID
                    symbol=final_symbol_str, # Use enriched symbol
                    side=side_enum,
                    fill_quantity=fill_quantity_val,
                    fill_price=average_fill_price_val,
                    fill_timestamp=final_fill_timestamp
                )
                
                # Get correlation context from the order if available
                master_correlation_id = None
                causation_id = None
                if 'order_from_repo' in locals() and order_from_repo:
                    master_correlation_id = getattr(order_from_repo, 'master_correlation_id', None)
                    causation_id = getattr(order_from_repo, 'client_order_id', None)  # The order ID is the cause of the fill
                
                # --- AGENT_DEBUG: START ---
                self.logger.critical(f"AGENT_DEBUG_EB_PUBLISH_ENTRY: Attempting to publish Event Type: OrderFilledEvent, Event ID: {fill_data.event_id if hasattr(fill_data, 'event_id') else 'N/A_pre_publish'}, Symbol (if any): {fill_data.symbol}, Event Type Object ID: {id(OrderFilledEvent)}")
                # --- AGENT_DEBUG: END ---
                self.event_bus.publish(OrderFilledEvent(
                    data=fill_data
                ))
                self.logger.info(f"Published OrderFilledEvent (from order_status) for LocalID='{actual_local_id_to_use}', Symbol='{final_symbol_str}', Qty={fill_quantity_val}, Px={average_fill_price_val}, Timestamp={final_fill_timestamp}")

                # Register pipeline stage for order fill
                if validation_id and self._pipeline_validator:
                    self._pipeline_validator.register_stage_completion(
                        validation_id,
                        'order_fill_received',
                        stage_data={
                            'local_order_id': actual_local_id_to_use, # Use the corrected ID
                            'fill_quantity': fill_quantity_val,
                            'fill_price': average_fill_price_val,
                            'broker_status': raw_broker_status_str
                        }
                    )

        except Exception as e:
            self.logger.error(f"Error in _handle_order_status_update: {e}. Data: {str(data)[:500]}", exc_info=True)
            self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message=f"Error handling order status: {e}", details=str(data)[:200], is_critical=False)))

    # _handle_order_fill method is likely not called if C++ sends fills within order_status
    # def _handle_order_fill(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
    #     # This method would only be used if C++ sent a dedicated "event": "order_fill"
    #     # Current C++ logic sends fill details within "event": "order_status"
    #     self.logger.warning("_handle_order_fill called, but C++ typically sends fills via order_status. Data: " + str(data))
    #     # If it were called, it would be similar to the fill logic now in _handle_order_status_update




    # Legacy position handlers have been replaced by specialized handlers:
    # - _handle_broker_full_positions_push: Handles "positions_update" events from C++ (full position snapshots)
    # - _handle_broker_quantity_only_push: Handles "raw_share_update" events from C++ (lean quantity-only updates)
    # The C++ bridge sends "positions_update" with a list of all positions for full reconciliation.

    def _handle_broker_quantity_only_push(self, data_dict: Dict[str, Any], perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_quantity_only_push_start')
        symbol = data_dict.get("symbol", "").upper()
        if not symbol:
            self.logger.warning(f"LSBROKER_QTY_PUSH: Received quantity update missing symbol. Data: {data_dict}")
            if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_quantity_only_push_end_err_sym')
            return

        try:
            new_qty = float(data_dict.get("new_total_shares_at_broker")) # This is the key from C++
            
            broker_ts_ms_est = data_dict.get("broker_timestamp_ms_est_midnight")
            broker_ts_utc_sec = data_dict.get("broker_timestamp_utc_sec_epoch")

            final_broker_ts = time.time() # Default
            if broker_ts_utc_sec is not None:
                try:
                    final_broker_ts = float(broker_ts_utc_sec)
                    # If you want to try and add millisecond precision from broker_ts_ms_est:
                    if broker_ts_ms_est is not None:
                         final_broker_ts += (float(broker_ts_ms_est) % 1000.0) / 1000.0
                    self.logger.debug(f"[{symbol}] LSBROKER_QTY_PUSH: Using UTC epoch timestamp: {final_broker_ts}")
                except (ValueError, TypeError):
                    self.logger.warning(f"[{symbol}] LSBROKER_QTY_PUSH: Error converting broker_timestamp_utc_sec_epoch. Falling back. Value: {broker_ts_utc_sec}")
            elif broker_ts_ms_est is not None:
                self.logger.warning(f"[{symbol}] LSBROKER_QTY_PUSH: Only broker_timestamp_ms_est_midnight available ({broker_ts_ms_est}). Precise conversion to full datetime is complex without date. Using current time for event.")
            
        except (ValueError, TypeError, KeyError) as e: # Added KeyError for missing "new_total_shares_at_broker"
            self.logger.error(f"[{symbol}] LSBROKER_QTY_PUSH: Error parsing data: {e}. Data: {data_dict}", exc_info=True)
            if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_quantity_only_push_end_err_parse')
            return

        self.logger.info(f"[{symbol}] LSBROKER_QTY_PUSH: Publishing BrokerPositionQuantityOnlyPushEvent. Qty={new_qty:.0f}, Timestamp={final_broker_ts}")
        event_data = BrokerPositionQuantityOnlyPushData(
            symbol=symbol,
            quantity=new_qty,
            broker_timestamp=final_broker_ts 
        )
        if self.event_bus:
            self.event_bus.publish(BrokerPositionQuantityOnlyPushEvent(data=event_data))
        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_quantity_only_push_end_ok')

    def _handle_broker_full_positions_push(self, positions_data_list: List[Dict[str, Any]], perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        if not isinstance(positions_data_list, list): # Ensure it's a list
            self.logger.warning(f"LSBROKER_FULL_POS_PUSH: Expected a list of positions, got {type(positions_data_list)}. Data: {str(positions_data_list)[:200]}")
            return
        if not positions_data_list and positions_data_list is not None: # Empty list is valid (e.g. flat)
             self.logger.info(f"LSBROKER_FULL_POS_PUSH: Received empty positions list. Publishing empty BrokerFullPositionPushEvent list.")


        min_interval = self._full_position_event_min_interval_sec if self._full_position_event_min_interval_sec is not None else 0.5
        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_full_positions_push_start')

        events_to_publish: List[BrokerFullPositionPushEvent] = []
        current_time_python = time.time() # Timestamp for events if not provided by broker msg

        processed_symbols_from_push: Set[str] = set() # To track symbols in this push

        for pos_data_dict in positions_data_list:
            symbol = pos_data_dict.get("symbol", "").upper()
            if not symbol:
                self.logger.warning(f"LSBROKER_FULL_POS_PUSH: Pushed position data missing symbol. Data: {pos_data_dict}")
                continue
            
            processed_symbols_from_push.add(symbol)

            try:
                new_qty = float(pos_data_dict.get("shares", 0.0))
                new_avg_price = float(pos_data_dict.get("avg_price", 0.0))

                # Parse P&L fields with proper None handling
                raw_realized_pnl = pos_data_dict.get("realized_pnl")
                new_realized_pnl_day = float(raw_realized_pnl) if raw_realized_pnl is not None else None

                raw_unrealized_pnl = pos_data_dict.get("unrealized_pnl")
                new_unrealized_pnl_day = float(raw_unrealized_pnl) if raw_unrealized_pnl is not None else None

                raw_total_pnl = pos_data_dict.get("total_pnl")
                new_total_pnl_day = float(raw_total_pnl) if raw_total_pnl is not None else None

                # Fallback calculation if total_pnl not directly provided but others are
                if new_total_pnl_day is None and new_realized_pnl_day is not None and new_unrealized_pnl_day is not None:
                    new_total_pnl_day = new_realized_pnl_day + new_unrealized_pnl_day

                # C++ "positions_update" does not have per-position timestamps. Use current Python time.
                broker_ts_for_pos = current_time_python
            except (ValueError, TypeError) as e:
                self.logger.error(f"[{symbol}] LSBROKER_FULL_POS_PUSH: Error parsing numeric data: {e}. Data: {pos_data_dict}", exc_info=True)
                continue

            with self._position_push_filter_lock:
                last_state_tuple = self._last_published_full_broker_position_state.get(symbol)
                last_publish_time = self._last_full_position_event_publish_time.get(symbol, 0)

                # Check for material change (quantity, avg_price, realized_pnl, or unrealized_pnl)
                materially_changed = False
                if last_state_tuple is None:
                    materially_changed = True # First time seeing this symbol or it's flat
                else:
                    # Update state tuple to include unrealized P&L: (qty, avg_px, realized_pnl, unrealized_pnl)
                    if len(last_state_tuple) == 3:
                        # Legacy tuple format, add None for unrealized_pnl
                        last_qty, last_avg_px, last_r_pnl = last_state_tuple
                        last_u_pnl = None
                    else:
                        last_qty, last_avg_px, last_r_pnl, last_u_pnl = last_state_tuple

                    if abs(new_qty - last_qty) > 1e-9: materially_changed = True
                    elif abs(new_qty) > 1e-9 and abs(new_avg_price - last_avg_px) > 0.0001: materially_changed = True # Price change if not flat
                    elif new_realized_pnl_day is not None and (last_r_pnl is None or abs(new_realized_pnl_day - last_r_pnl) > 0.01): materially_changed = True
                    elif new_unrealized_pnl_day is not None and (last_u_pnl is None or abs(new_unrealized_pnl_day - last_u_pnl) > 0.01): materially_changed = True
                
                # Additionally, always publish if quantity is non-zero and we haven't published recently (covers P&L updates for existing positions)
                # Or if quantity becomes zero (flattened)
                always_publish_due_to_pnl_or_flatten = (new_qty != 0) or (new_qty == 0 and (last_state_tuple and last_state_tuple[0] != 0))


                if materially_changed or always_publish_due_to_pnl_or_flatten:
                    if (current_time_python - last_publish_time) >= min_interval or new_qty == 0: # Publish if interval met OR if it's a flatten
                        self.logger.info(f"[{symbol}] LSBROKER_FULL_POS_PUSH: Data CHANGED/PNL_UPDATE/FLAT & Interval Met. New: Qty={new_qty:.0f}, AvgPx={new_avg_price:.4f}, RPNL={new_realized_pnl_day}, UPNL={new_unrealized_pnl_day}")
                        # Update cached state tuple with all relevant fields including unrealized P&L
                        self._last_published_full_broker_position_state[symbol] = (new_qty, new_avg_price, new_realized_pnl_day, new_unrealized_pnl_day)
                        self._last_full_position_event_publish_time[symbol] = current_time_python

                        event_data = BrokerFullPositionPushData(
                            symbol=symbol, quantity=new_qty, average_price=new_avg_price,
                            realized_pnl_day=new_realized_pnl_day,
                            unrealized_pnl_day=new_unrealized_pnl_day,  # Fixed field name
                            total_pnl_day=new_total_pnl_day,           # Added total P&L
                            broker_timestamp=broker_ts_for_pos
                        )
                        events_to_publish.append(BrokerFullPositionPushEvent(data=event_data))
                    else:
                        self.logger.debug(f"[{symbol}] LSBROKER_FULL_POS_PUSH: Data CHANGED but Min Interval NOT MET ({current_time_python - last_publish_time:.2f}s < {min_interval:.2f}s). Suppressing event.")
        
        # Handle symbols that were previously in state but are now missing from the push (implicitly flat)
        with self._position_push_filter_lock:
            symbols_to_flatten = set(self._last_published_full_broker_position_state.keys()) - processed_symbols_from_push
            for sym_flat in symbols_to_flatten:
                last_state_flat = self._last_published_full_broker_position_state.get(sym_flat, (0,0,0,0))
                # Handle both legacy 3-tuple and new 4-tuple formats
                if len(last_state_flat) == 3:
                    last_qty_flat, _, last_rpnl_flat = last_state_flat
                    last_upnl_flat = None
                else:
                    last_qty_flat, _, last_rpnl_flat, last_upnl_flat = last_state_flat

                if abs(last_qty_flat) > 1e-9 : # Only publish flatten if it was not already flat
                    self.logger.info(f"[{sym_flat}] LSBROKER_FULL_POS_PUSH: Symbol missing from push, previously had Qty={last_qty_flat}. Publishing as FLAT.")
                    # Update state with new 4-tuple format: (qty, avg_px, realized_pnl, unrealized_pnl)
                    self._last_published_full_broker_position_state[sym_flat] = (0.0, 0.0, last_rpnl_flat, 0.0)
                    self._last_full_position_event_publish_time[sym_flat] = current_time_python
                    event_data_flat = BrokerFullPositionPushData(
                        symbol=sym_flat, quantity=0.0, average_price=0.0,
                        realized_pnl_day=last_rpnl_flat, # Carry over last known RPNL
                        unrealized_pnl_day=0.0, # Flat means no unrealized PNL
                        total_pnl_day=last_rpnl_flat if last_rpnl_flat is not None else None, # Total = realized when flat
                        broker_timestamp=current_time_python
                    )
                    events_to_publish.append(BrokerFullPositionPushEvent(data=event_data_flat))
                # else: symbol was already flat or never seen, no need to publish another flat event


        if self.event_bus and events_to_publish:
            for event_to_publish in events_to_publish:
                self.event_bus.publish(event_to_publish)
        elif not events_to_publish and positions_data_list : # Log if data came but nothing met criteria
             self.logger.debug(f"LSBROKER_FULL_POS_PUSH: Received {len(positions_data_list)} positions, but no events met publishing criteria (change/interval).")


        if perf_timestamps: add_timestamp(perf_timestamps, 'broker_bridge.handle_full_positions_push_end')

    def _handle_account_update(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        try:
            account_id_str = data.get("account_id", "") # C++ does not send account_id with account_update
            
            # Fields directly from C++ "account_update"
            closed_pl_val = float(data.get("closed_pl", 0.0))
            open_pl_val = float(data.get("open_pl", 0.0))
            net_pl_val = float(data.get("net_pl", 0.0))
            buying_power_val = float(data.get("buying_power", 0.0))
            # bp_in_use = float(data.get("bp_in_use", 0.0)) # Not directly in AccountSummaryData
            # base_bp = float(data.get("base_bp", 0.0))     # Not directly in AccountSummaryData
            equity_val = float(data.get("equity", 0.0))
            running_balance_val = float(data.get("running_balance", 0.0)) # Use this as cash
            # long_value = float(data.get("long_value", 0.0)) # Not directly in AccountSummaryData
            # short_value = float(data.get("short_value", 0.0))# Not directly in AccountSummaryData
            # net_dollar_value = float(data.get("net_dollar_value", 0.0)) # Could be related to equity or total value

            account_summary = AccountSummaryData(
                account_id=account_id_str if account_id_str else "LIGHTSPEED_ACCOUNT", # Use a default if not provided
                equity=equity_val,
                buying_power=buying_power_val,
                cash=running_balance_val, # Map running_balance to cash
                realized_pnl_day=closed_pl_val, # Map closed_pl to realized_pnl_day
                unrealized_pnl_day=open_pl_val, # Map open_pl to unrealized_pnl_day
                timestamp=time.time() # C++ account_update does not provide a timestamp
            )
            self.event_bus.publish(AccountUpdateEvent(data=account_summary))
            self.logger.info(f"Published AccountUpdateEvent: Equity={equity_val:.2f}, BP={buying_power_val:.2f}, Cash={running_balance_val:.2f}")
        except (ValueError, TypeError, KeyError) as e:
            self.logger.error(f"Error in _handle_account_update: {e}. Data: {str(data)[:500]}", exc_info=True)
            self.event_bus.publish(BrokerErrorEvent(data=BrokerErrorData(message=f"Error handling account update: {e}", details=str(data)[:200],is_critical=False)))

    def _handle_cross_own_error(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        # This is for the manually detected "You cannot cross your own" string in _listen_loop
        try:
            ls_order_id_cross = data.get("ls_order_id") # This comes from _extract_order_id_from_line
            error_message_cross = data.get("error_message", "Cross-own order detected by string match.")

            error_data = BrokerErrorData(
                message=f"Cross-own error (string match) for potential LS Order ID: {ls_order_id_cross}",
                code="CROSS_OWN_STRING_MATCH",
                details=error_message_cross,
                is_critical=False # Usually results in a formal rejection event too
            )
            self.event_bus.publish(BrokerErrorEvent(data=error_data))
            self.logger.warning(f"Published BrokerErrorEvent for string-matched cross-own error: {error_message_cross}")
        except Exception as e:
            self.logger.error(f"Error in _handle_cross_own_error: {e}", exc_info=True)

    def _handle_broker_error(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        # This is for explicit "event": "broker_error" messages if C++ were to send them
        try:
            message_be = data.get("message", "Unknown broker error from C++ bridge")
            code_be = data.get("code")
            details_be = data.get("details")
            is_critical_be = data.get("is_critical", False)
            
            # Log broker error (no correlation ID available for general broker errors)
            self.logger.info(f"[BROKER] Processing broker error: Critical={is_critical_be}, Message={message_be[:50]}...")

            error_data = BrokerErrorData(
                message=message_be, code=code_be, details=details_be, is_critical=is_critical_be
            )
            self.event_bus.publish(BrokerErrorEvent(data=error_data))
            self.logger.warning(f"Received and published explicit BrokerErrorEvent: {message_be}")

            # Publish broker error to Redis stream for IntelliSense
            # Queue-based publishing to prevent blocking listen thread
            publish_task = {
                "target": self._publish_broker_error_to_redis,
                "args": (message_be, details_be or "", is_critical_be),
                "kwargs": {}
            }
            try:
                # Add queue monitoring for better visibility
                queue_size = self._redis_publish_queue.qsize()
                if queue_size > 1500:  # Warn at 75% capacity (2000 max)
                    self.logger.warning(f"Redis publishing queue is {queue_size/20:.1f}% full ({queue_size}/2000)")
                
                self._redis_publish_queue.put_nowait(publish_task)
            except queue.Full:
                self.logger.warning("Redis publishing queue is full. Broker error message was dropped. Consider increasing queue size or reducing message volume.")
        except Exception as e:
            self.logger.error(f"Error in _handle_broker_error: {e}", exc_info=True)

    def _handle_trading_status_update(self, data: dict, perf_timestamps: Optional[Dict[str, float]] = None) -> None:
        """Handles LULD, Halt, Resume messages from C++"""
        try:
            symbol_ts = data.get("symbol")
            status_ts = data.get("status") # "LULD_Update", "Halted", "Resumed"
            timestamp_ts = data.get("timestamp") # This is epoch seconds from C++ for Halt/Resume, or time.time() for LULD

            if not symbol_ts or not status_ts:
                self.logger.warning(f"Invalid trading_status message: {data}")
                return
            
            # For now, we don't have a dedicated event for these in core.events
            # We can log them or create a generic "BrokerNotificationEvent" if needed.
            # Or, if MarketDataManager needs this, it might subscribe to a raw relay or a specific new event.
            
            log_level = logging.INFO
            if status_ts == "Halted": log_level = logging.WARNING

            self.logger.log(log_level, f"TRADING_STATUS_UPDATE: Symbol: {symbol_ts}, Status: {status_ts}, Data: {data}")

            # Example: Publishing as a raw message relay for other modules to pick up if interested
            # This allows flexibility without defining a strict event type yet.
            relay_event_data = BrokerRawMessageRelayEventData(
                raw_message_dict=data
                # Note: message_type is not a valid parameter for BrokerRawMessageRelayEventData
            )
            self.event_bus.publish(BrokerRawMessageRelayEvent(data=relay_event_data))

        except Exception as e:
            self.logger.error(f"Error in _handle_trading_status_update: {e}. Data: {data}", exc_info=True)


    def _log_broker_message_performance(self, perf_timestamps: Dict[str, float]) -> None:
        if not is_performance_tracking_enabled() or not perf_timestamps: return
        try:
            durations = calculate_durations(perf_timestamps)
            if durations and random.random() < 0.05: # Log less frequently
                total_duration_ms = durations.get('total_measured', durations.get('total',0)) * 1000
                self.logger.debug(f"Broker Message Handling Perf: Total={total_duration_ms:.2f}ms. All durations: { {k: f'{v*1000:.2f}ms' for k,v in durations.items()} }")
        except Exception as e:
            self.logger.warning(f"Error in _log_broker_message_performance: {e}")

    # --- Service Health Monitoring ---
    def _publish_service_health_status(self, health_status: str, status_reason: str, 
                                     context: Dict[str, Any], correlation_id: str = None) -> None:
        """
        Publish service health status event to Redis stream via app_core.
        
        Args:
            health_status: Health status (e.g., "STARTED", "STOPPED", "HEALTHY", "DEGRADED", "FAILED")
            status_reason: Human-readable reason for the health status
            context: Additional context data for the health status
            correlation_id: Optional correlation ID for end-to-end tracking
        """
        if not self._telemetry_service:
            self.logger.debug("Cannot publish service health status: TelemetryService not available")
            return
        
        try:
            # CRITICAL FIX: Check TANK mode FIRST to prevent blocking when babysitter down
            if (hasattr(self._config, 'ENABLE_IPC_DATA_DUMP') and 
                not self._config.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping service health status - ENABLE_IPC_DATA_DUMP is False")
                return
            
            import uuid
            # Create service health status payload
            payload = {
                "health_status": health_status,
                "status_reason": status_reason,
                "context": context,
                "status_timestamp": time.time(),
                "component": "LightspeedBroker"
            }
            
            # Get target stream from config
            target_stream = getattr(self._config, 'redis_stream_system_health', 'testrade:system-health')
            
            # Publish via telemetry service
            success = self._telemetry_service.enqueue(
                source_component="LightspeedBroker",
                event_type="TESTRADE_SERVICE_HEALTH_STATUS",
                payload=payload,
                stream_override=target_stream
            )
            
            # Use central telemetry queue for non-blocking publish
            # Use telemetry service:
            success = self._telemetry_service.enqueue(
                source_component="LightspeedBroker",
                event_type="SERVICE_HEALTH_STATUS",
                payload=payload,
                stream_override=target_stream
            )
            
            if success:
                self.logger.debug(f"Service health status enqueued for stream '{target_stream}': {health_status}")
            else:
                self.logger.warning(f"Failed to enqueue service health status for stream '{target_stream}'")
                
        except Exception as e:
            self.logger.error(f"Error publishing service health status: {e}", exc_info=True)

    def _publish_periodic_health_status(self) -> None:
        """
        Publish periodic health status based on current broker connection state and performance metrics.
        """
        try:
            # Check various health indicators
            health_status = "HEALTHY"
            status_issues = []
            
            # Connection status check
            if not self.is_connected():
                health_status = "CRITICAL"
                status_issues.append("Broker not connected")
            
            # Listen thread health check
            if not self._listen_thread_obj or not self._listen_thread_obj.is_alive():
                if health_status != "CRITICAL":
                    health_status = "DEGRADED"
                status_issues.append("Listen thread not running")
            
            # Redis worker thread health check
            if not (hasattr(self, '_redis_worker_thread') and self._redis_worker_thread is not None and self._redis_worker_thread.is_alive()):
                if health_status != "CRITICAL":
                    health_status = "DEGRADED"
                status_issues.append("Redis worker thread not running")
            
            # Redis queue capacity check
            if hasattr(self, '_redis_publish_queue'):
                queue_size = self._redis_publish_queue.qsize()
                if queue_size > 1800:  # Near capacity
                    health_status = "CRITICAL"
                    status_issues.append(f"Redis publish queue near capacity: {queue_size}")
                elif queue_size > 1000:
                    if health_status != "CRITICAL":
                        health_status = "DEGRADED"
                    status_issues.append(f"Redis publish queue high usage: {queue_size}")
            
            # Socket availability check
            if self.client and not self.client.sock:
                if health_status not in ["CRITICAL", "DEGRADED"]:
                    health_status = "DEGRADED"
                status_issues.append("Socket not available")
            
            # Critical dependency availability checks
            critical_dependencies_missing = []
            if not self.event_bus:
                critical_dependencies_missing.append("event_bus")
            if not self._price_provider:
                critical_dependencies_missing.append("price_provider")
            
            if critical_dependencies_missing:
                if health_status != "CRITICAL":
                    health_status = "DEGRADED"
                status_issues.append(f"Critical dependencies missing: {', '.join(critical_dependencies_missing)}")
            
            status_reason = f"Service operational - {', '.join(status_issues) if status_issues else 'All systems healthy'}"
            
            # Build context with performance metrics
            context = {
                "service_name": "LightspeedBroker",
                "health_status": health_status,
                "connection_status": "CONNECTED" if self.is_connected() else "DISCONNECTED",
                "host": self.host,
                "port": self.port,
                "socket_available": self.client and self.client.sock is not None,
                "listen_thread_alive": self._listen_thread_obj and self._listen_thread_obj.is_alive(),
                "redis_worker_alive": (hasattr(self, '_redis_worker_thread') and 
                                      self._redis_worker_thread is not None and 
                                      self._redis_worker_thread.is_alive()),
                "redis_queue_size": getattr(self, '_redis_publish_queue', {}).get('qsize', lambda: 0)() if hasattr(self, '_redis_publish_queue') else 0,
                "broker_msg_count": getattr(self, '_broker_msg_count', 0),
                "event_bus_available": self.event_bus is not None,
                "price_provider_available": self._price_provider is not None,
                "order_repository_available": self._order_repository is not None,
                "telemetry_service_available": self._telemetry_service is not None,
                "health_check_timestamp": time.time()
            }
            
            # Only publish if status has changed or it's been a while (reduces noise)
            last_published_status = getattr(self, '_last_published_health_status', None)
            time_since_last_publish = time.time() - getattr(self, '_last_health_publish_time', 0)
            
            if (last_published_status != health_status or 
                time_since_last_publish > 300):  # Force publish every 5 minutes
                
                self._publish_service_health_status(health_status, status_reason, context)
                setattr(self, '_last_published_health_status', health_status)
                setattr(self, '_last_health_publish_time', time.time())
                
        except Exception as e:
            self.logger.error(f"Error in periodic health status publishing: {e}", exc_info=True)

    def check_and_publish_health_status(self) -> None:
        """
        Public method to trigger health status check and publishing.
        Can be called periodically by external services or after broker operations.
        """
        current_time = time.time()
        if not hasattr(self, '_last_health_check_time') or \
           (current_time - getattr(self, '_last_health_check_time', 0) > 30.0):  # Check every 30 seconds
            self._publish_periodic_health_status()
            self._last_health_check_time = current_time

# Select import needed for _listen_loop
import select

# --- END OF FILE lightspeed_broker.py ---