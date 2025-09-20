"""
Command Listener Service

This service manages ZMQ command listening, including:
- ZMQ socket initialization and management
- Command receiving and parsing
- Command routing to appropriate handlers
- Response publishing back to clients
- Error handling and recovery

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import threading
import time
import json
from typing import Optional, Dict, Any, Callable
from threading import Lock

from interfaces.core.services import ILifecycleService
# Import our custom interface (will be relative path when integrated)
from interfaces.core.services import ICommandListener, IIPCManager

logger = logging.getLogger(__name__)


class CommandListener(ILifecycleService, ICommandListener):
    """
    Manages ZMQ command listening and routing.
    
    This service encapsulates all command listening logic that was
    previously scattered throughout ApplicationCore.
    """
    
    def __init__(self,
                 config_service: Any,
                 ipc_manager: IIPCManager,
                 zmq_context: Optional[Any] = None,
                 gui_command_service: Optional[Any] = None):
        """
        Initialize the Command Listener.
        
        Args:
            config_service: Global configuration object
            ipc_manager: IPC manager for publishing responses
            zmq_context: ZMQ context for socket creation
            gui_command_service: GUI command service for handling commands
        """
        self.logger = logger
        self.config = config_service
        self.ipc_manager = ipc_manager
        self.zmq_context = zmq_context
        self.gui_command_service = gui_command_service
        
        # Command listener attributes
        self._command_listener_thread: Optional[threading.Thread] = None
        self._command_listener_stop_event = threading.Event()
        self._is_running = False
        self._lock = Lock()
        
        # ZMQ socket
        self.ipc_command_socket: Optional[Any] = None
        
        # Configuration
        self.core_command_ipc_pull_address = getattr(
            self.config, 'core_ipc_command_pull_address', "tcp://*:5560"
        )
        
        # Command handlers registry
        self._command_handlers: Dict[str, Callable] = {}
        
        # Statistics
        self._commands_received = 0
        self._commands_processed = 0
        self._command_errors = 0
        self._start_time = None
        
    @property
    def is_ready(self) -> bool:
        """Check if the command listener is ready."""
        with self._lock:
            return (self._is_running and 
                   self._command_listener_thread is not None and
                   self.ipc_command_socket is not None)
    
    def start(self) -> None:
        """Start the command listener."""
        with self._lock:
            if self._is_running:
                self.logger.warning("CommandListener already running")
                return
                
            self._is_running = True
            self._start_time = time.time()
            
            # Initialize ZMQ socket and start listener
            if self._initialize_core_command_listener():
                self.logger.info("CommandListener started successfully")
            else:
                self._is_running = False
                self.logger.error("Failed to start CommandListener")
    
    def stop(self) -> None:
        """Stop the command listener."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            
            # Stop command listener thread
            self._command_listener_stop_event.set()
            if self._command_listener_thread and self._command_listener_thread.is_alive():
                self.logger.info("Stopping command listener thread...")
                self._command_listener_thread.join(timeout=2.0)
                
            # Close ZMQ socket
            if self.ipc_command_socket:
                try:
                    self.ipc_command_socket.close(linger=0)
                except Exception as e:
                    self.logger.warning(f"Error closing command socket: {e}")
                self.ipc_command_socket = None
                
            self._command_listener_thread = None
            self.logger.info("CommandListener stopped")
    
    def _initialize_core_command_listener(self) -> bool:
        """Initialize core command listener with ZMQ socket."""
        if not self.core_command_ipc_pull_address:
            self.logger.warning("core_command_ipc_pull_address not configured. Command listener disabled.")
            return False
            
        try:
            # Check if ZMQ is available
            try:
                import zmq
            except ImportError:
                self.logger.error("PyZMQ not available. Command listener disabled.")
                return False
                
            self.logger.info(f"Initializing command listener at {self.core_command_ipc_pull_address}...")
            
            # Use existing ZMQ context or create new one
            if not self.zmq_context:
                self.zmq_context = zmq.Context.instance()
                
            # Create and configure socket
            self.ipc_command_socket = self.zmq_context.socket(zmq.PULL)
            self.ipc_command_socket.setsockopt(zmq.LINGER, 0)  # Ensure quick close
            self.ipc_command_socket.bind(self.core_command_ipc_pull_address)
            
            self.logger.info(f"Command listener PULL socket bound to {self.core_command_ipc_pull_address}")
            
            # Start listener thread
            self._command_listener_stop_event.clear()
            self._command_listener_thread = threading.Thread(
                target=self._core_command_receive_loop,
                name="CommandListenerThread",
                daemon=True
            )
            self._command_listener_thread.start()
            
            self.logger.info("Command listener thread started successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize command listener: {e}", exc_info=True)
            return False
    
    def _core_command_receive_loop(self):
        """Core command receiving loop with comprehensive error handling."""
        self.logger.info("Command receive loop started")
        
        if not self.ipc_command_socket:
            self.logger.error("Command socket not initialized. Listener thread exiting.")
            return
            
        try:
            import zmq
            
            # Set up poller for non-blocking receive
            poller = zmq.Poller()
            poller.register(self.ipc_command_socket, zmq.POLLIN)
            
            while not self._command_listener_stop_event.is_set():
                try:
                    # Poll for messages with timeout
                    socks = dict(poller.poll(timeout=100))  # 100ms timeout
                    if not socks.get(self.ipc_command_socket) == zmq.POLLIN:
                        continue
                        
                    # Receive message
                    message_parts = self.ipc_command_socket.recv_multipart(flags=zmq.DONTWAIT)
                    self._commands_received += 1
                    
                    # Process command
                    self._process_command_message(message_parts)
                    self._commands_processed += 1
                    
                except zmq.Again:
                    # Should not happen with poller, but DONTWAIT might still raise it
                    continue
                except zmq.ZMQError as e:
                    if e.errno == zmq.ETERM:
                        self.logger.info("Command socket context terminated. Listener stopping.")
                        break
                    self.logger.error(f"ZMQError in command receive loop: {e}")
                    self._command_listener_stop_event.set()
                    break
                except Exception as e:
                    self.logger.error(f"Error in command receive loop: {e}", exc_info=True)
                    self._command_errors += 1
                    
        except Exception as e:
            self.logger.error(f"Fatal error in command receive loop: {e}", exc_info=True)
            
        self.logger.info("Command receive loop stopped")
    
    def _process_command_message(self, message_parts: list):
        """Process a received command message."""
        if len(message_parts) != 2:
            self.logger.warning(f"Malformed command message received ({len(message_parts)} parts)")
            return
            
        try:
            # Parse message parts
            command_type_str = message_parts[0].decode('utf-8')
            inner_payload_json_str = message_parts[1].decode('utf-8')
            
            # Parse command payload
            actual_command_payload_dict = json.loads(inner_payload_json_str)
            command_id = actual_command_payload_dict.get("command_id", f"cmd_{int(time.time_ns())}")
            parameters = actual_command_payload_dict.get("parameters", {})
            
            self.logger.info(f"Received command '{command_type_str}' (ID: {command_id})")
            
            # Route command to appropriate handler
            self._route_command(command_type_str, parameters, command_id, actual_command_payload_dict)
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to decode JSON payload: {e}")
            self._command_errors += 1
        except Exception as e:
            self.logger.error(f"Error processing command message: {e}", exc_info=True)
            self._command_errors += 1
    
    def _route_command(self, command_type: str, parameters: Dict[str, Any], 
                      command_id: str, full_payload: Dict[str, Any]):
        """Route command to appropriate handler."""
        try:
            # Check for custom handlers first
            if command_type.upper() in self._command_handlers:
                handler = self._command_handlers[command_type.upper()]
                result = handler(command_type, parameters, command_id)
                self._send_command_response(command_id, command_type, result)
                return
            
            # Default to GUI command service if available
            if self.gui_command_service:
                result = self.gui_command_service.handle_command(
                    command_type.upper(),
                    parameters,
                    command_id
                )
                
                # Send response if result available
                if result and hasattr(result, 'status'):
                    response_data = {
                        "status": result.status.value if hasattr(result.status, 'value') else str(result.status),
                        "message": getattr(result, 'message', ''),
                        "data": getattr(result, 'data', {})
                    }
                    self._send_command_response(command_id, command_type, response_data)
            else:
                self.logger.error(f"No handler available for command '{command_type}' (ID: {command_id})")
                self._send_error_response(command_id, command_type, "No handler available")
                
        except Exception as e:
            self.logger.error(f"Error routing command '{command_type}': {e}", exc_info=True)
            self._send_error_response(command_id, command_type, f"Handler error: {str(e)}")
    
    def register_command_handler(self, command_type: str, handler: Callable):
        """
        Register a custom command handler.
        
        Args:
            command_type: Command type to handle
            handler: Function that takes (command_type, parameters, command_id) and returns response
        """
        self._command_handlers[command_type.upper()] = handler
        self.logger.info(f"Registered command handler for: {command_type}")
    
    def unregister_command_handler(self, command_type: str):
        """
        Unregister a command handler.
        
        Args:
            command_type: Command type to unregister
        """
        command_type_upper = command_type.upper()
        if command_type_upper in self._command_handlers:
            del self._command_handlers[command_type_upper]
            self.logger.info(f"Unregistered command handler for: {command_type}")
    
    def set_gui_command_service(self, gui_command_service: Any):
        """Set the GUI command service reference."""
        self.gui_command_service = gui_command_service
        self.logger.info("GUI command service reference set")
    
    def _send_command_response(self, command_id: str, command_type: str, response_data: Dict[str, Any]):
        """Send command response via IPC manager."""
        try:
            response_payload = {
                "command_id": command_id,
                "command_type": command_type,
                "response": response_data,
                "timestamp": time.time()
            }
            
            # Use IPC manager to publish response
            target_stream = getattr(self.config, 'redis_stream_responses_to_gui', 'testrade:responses:to_gui')
            
            success = self.ipc_manager.publish_to_redis(
                stream_name=target_stream,
                payload=response_payload,
                event_type="TESTRADE_COMMAND_RESPONSE",
                correlation_id=command_id,
                source_component="CommandListener"
            )
            
            if success:
                self.logger.debug(f"Response sent for command {command_id}")
            else:
                self.logger.warning(f"Failed to send response for command {command_id}")
                
        except Exception as e:
            self.logger.error(f"Error sending command response: {e}", exc_info=True)
    
    def _send_error_response(self, command_id: str, command_type: str, error_message: str):
        """Send error response for a command."""
        response_data = {
            "status": "error",
            "message": error_message,
            "data": {}
        }
        self._send_command_response(command_id, command_type, response_data)
    
    def send_response(self, command_id: str, response: Dict[str, Any]) -> bool:
        """
        Send a response to a command (interface implementation).
        
        Args:
            command_id: ID of the command to respond to
            response: Response data
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self._send_command_response(command_id, "DIRECT_RESPONSE", response)
            return True
        except Exception as e:
            self.logger.error(f"Error sending direct response: {e}", exc_info=True)
            return False
    
    def get_listener_status(self) -> Dict[str, Any]:
        """Get command listener status and statistics."""
        with self._lock:
            uptime = time.time() - self._start_time if self._start_time else 0
            
            return {
                "is_running": self._is_running,
                "is_ready": self.is_ready,
                "uptime_seconds": uptime,
                "commands_received": self._commands_received,
                "commands_processed": self._commands_processed,
                "command_errors": self._command_errors,
                "success_rate": (self._commands_processed / max(self._commands_received, 1)) * 100,
                "registered_handlers": list(self._command_handlers.keys()),
                "socket_address": self.core_command_ipc_pull_address
            }