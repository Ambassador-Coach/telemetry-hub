"""
OCR ZMQ Command Listener - Phase 5

This module provides ZMQ-based command listening for the OCR process,
replacing direct Redis command consumption with Babysitter → ZMQ → OCR architecture.
"""

import logging
import threading
import time
import json
import os
import uuid
from typing import Dict, Any, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .ocr_service import OCRService
    from interfaces.logging.services import ICorrelationLogger

logger = logging.getLogger(__name__)


class OCRZMQCommandListener:
    """
    Phase 5: ZMQ command listener for OCR process.
    
    Receives commands from Babysitter via ZMQ PULL socket and executes them
    on the OCR service instance. Sends responses back via the OCR pipe.
    """
    
    def __init__(self, ocr_service_instance: 'OCRService', config_dict: Dict[str, Any], 
                 pipe_to_parent, logger_instance: Optional[logging.Logger] = None,
                 correlation_logger: Optional['ICorrelationLogger'] = None):
        """
        Initialize the ZMQ command listener.
        
        Args:
            ocr_service_instance: The OCR service instance to execute commands on
            config_dict: Configuration dictionary from parent process
            pipe_to_parent: Pipe connection to send responses back to ApplicationCore
            logger_instance: Optional logger instance
            correlation_logger: Correlation logger for publishing command execution metrics
        """
        self.ocr_service = ocr_service_instance
        self.config_dict = config_dict
        self.pipe_to_parent = pipe_to_parent
        self.logger = logger_instance or logger
        self._correlation_logger = correlation_logger
        
        # ZMQ configuration
        self.zmq_pull_address = config_dict.get('ocr_ipc_command_pull_address', 'tcp://127.0.0.1:5559')
        
        # Thread control
        self.stop_event = threading.Event()
        self.listener_thread = None
        
        # ZMQ components
        self.zmq_context = None
        self.zmq_socket = None
        
        # Resilience state
        self.last_known_roi = config_dict.get('initial_roi', [0, 0, 100, 100])
        
        self.logger.info(f"SUCCESS: Phase 5: OCR ZMQ Command Listener initialized on {self.zmq_pull_address}")
    
    def start(self):
        """Start the ZMQ command listener."""
        try:
            import zmq
            
            # Initialize ZMQ context and socket
            self.zmq_context = zmq.Context.instance()
            self.zmq_socket = self.zmq_context.socket(zmq.PULL)
            self.zmq_socket.bind(self.zmq_pull_address)
            
            # Start listener thread
            self.listener_thread = threading.Thread(target=self._listen_loop, daemon=True, name="OCRZMQCommandListener")
            self.listener_thread.start()
            
            self.logger.info(f"SUCCESS: Phase 5: OCR ZMQ command listener started on {self.zmq_pull_address}")
            return True

        except Exception as e:
            self.logger.error(f"ERROR: Phase 5: Failed to start OCR ZMQ command listener: {e}", exc_info=True)
            return False
    
    def stop(self):
        """Stop the ZMQ command listener."""
        self.logger.info("SUCCESS: Phase 5: Stopping OCR ZMQ command listener...")
        
        # Signal stop
        self.stop_event.set()
        
        # Close ZMQ socket
        if self.zmq_socket:
            try:
                self.zmq_socket.close(linger=0)
            except Exception as e:
                self.logger.error(f"Phase 5: Error closing ZMQ socket: {e}")
        
        # Wait for thread to finish
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2.0)
            if self.listener_thread.is_alive():
                self.logger.warning("WARNING: Phase 5: ZMQ listener thread did not stop cleanly")

        self.logger.info("SUCCESS: Phase 5: OCR ZMQ command listener stopped")
    
    def _listen_loop(self):
        """Main listening loop for ZMQ commands."""
        import zmq
        
        self.logger.info("SUCCESS: Phase 5: OCR ZMQ command listen loop started")
        
        # Set up ZMQ poller for non-blocking receive
        poller = zmq.Poller()
        poller.register(self.zmq_socket, zmq.POLLIN)
        
        while not self.stop_event.is_set():
            try:
                # Poll for messages with timeout
                socks = dict(poller.poll(timeout=100))  # 100ms timeout
                
                if self.zmq_socket in socks and socks[self.zmq_socket] == zmq.POLLIN:
                    # Receive multipart message: [command_type_bytes, inner_payload_json_bytes]
                    message_parts = self.zmq_socket.recv_multipart(flags=zmq.DONTWAIT)
                    
                    if len(message_parts) != 2:
                        self.logger.warning(f"Phase 5: Received malformed command message with {len(message_parts)} parts. Expected 2.")
                        continue
                    
                    # Decode message parts
                    command_type_bytes, inner_payload_json_bytes = message_parts
                    command_type_str = command_type_bytes.decode('utf-8')
                    inner_payload_json_str = inner_payload_json_bytes.decode('utf-8')
                    
                    # Parse inner payload
                    try:
                        parameters_dict = json.loads(inner_payload_json_str)
                    except json.JSONDecodeError as e:
                        self.logger.error(f"Phase 5: JSON decode error for command '{command_type_str}': {e}")
                        continue
                    
                    # Extract command ID for correlation
                    command_id = parameters_dict.get("command_id", "unknown")
                    command_parameters = parameters_dict.get("parameters", {})
                    
                    self.logger.info(f"SUCCESS: Phase 5: Received OCR command '{command_type_str}' (ID: {command_id}) with params: {command_parameters}")
                    
                    # Process the command
                    self._process_command(command_id, command_type_str, command_parameters)
                
            except zmq.Again:
                # No message available, continue polling
                continue
            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    self.logger.info("SUCCESS: Phase 5: ZMQ context terminated. OCR command listener stopping.")
                    break
                else:
                    self.logger.error(f"ERROR: Phase 5: ZMQ error in OCR command listen loop: {e}")
                    break
            except Exception as e:
                self.logger.error(f"ERROR: Phase 5: Unexpected error in OCR command listen loop: {e}", exc_info=True)
                if self.stop_event.is_set():
                    break
                # Continue on non-fatal errors
        
        self.logger.info("SUCCESS: Phase 5: OCR ZMQ command listen loop stopped")
    
    def _process_command(self, command_id: str, command_type: str, parameters: Dict[str, Any]):
        """Process a single OCR command and send response via pipe."""
        start_time_ns = time.perf_counter_ns()
        try:
            success = False
            message = "Unknown command"
            response_data = {}
            
            if command_type == "SET_ROI_ABSOLUTE":
                success, message, response_data = self._handle_set_roi(parameters)
            elif command_type == "ROI_ADJUST":
                success, message, response_data = self._handle_roi_adjust(parameters)
            elif command_type == "GET_ROI":
                success, message, response_data = self._handle_get_roi()
            elif command_type == "START_OCR":
                success, message, response_data = self._handle_start_ocr()
            elif command_type == "STOP_OCR":
                success, message, response_data = self._handle_stop_ocr()
            elif command_type == "GET_OCR_STATUS":
                success, message, response_data = self._handle_get_ocr_status()
            elif command_type == "UPDATE_OCR_PREPROCESSING_FULL":
                success, message, response_data = self._handle_update_ocr_preprocessing(parameters)
            else:
                message = f"Unknown OCR command type: {command_type}"
                self.logger.error(message)
            
            # Send response via pipe to ApplicationCore
            self._send_response_via_pipe(command_id, command_type, success, message, response_data)
            
            # Publish command execution metrics to telemetry
            execution_time_ms = (time.perf_counter_ns() - start_time_ns) / 1_000_000
            self._publish_command_execution_metrics(command_type, success, execution_time_ms, command_id, parameters)
            
        except Exception as e:
            execution_time_ms = (time.perf_counter_ns() - start_time_ns) / 1_000_000
            self.logger.error(f"Phase 5: Error processing OCR command '{command_type}' (ID: {command_id}): {e}", exc_info=True)
            self._send_response_via_pipe(command_id, command_type, False, f"Internal error: {e}", {})
            self._publish_command_execution_metrics(command_type, False, execution_time_ms, command_id, parameters, error=str(e))
    
    def _send_response_via_pipe(self, command_id: str, command_type: str, success: bool, message: str, data: Dict[str, Any]):
        """
        Phase 5: Send command response back to ApplicationCore via pipe.
        ApplicationCore will then forward it to GUI via Babysitter → Redis.
        """
        try:
            response_payload = {
                "original_command_id": command_id,
                "command_type_echo": command_type,
                "status": "success" if success else "error",
                "message": message,
                "data": data,
                "response_timestamp_ns": time.perf_counter_ns(),
                "response_timestamp": time.perf_counter_ns() / 1_000_000_000,
                "source": "OCRProcess_Phase5"
            }
            
            # Send structured response via pipe
            response_message = {
                "type": "ocr_command_response",
                "data": response_payload
            }
            
            self.pipe_to_parent.send(response_message)
            self.logger.info(f"SUCCESS: Phase 5: Sent OCR response for '{command_type}' (ID: {command_id}): {'success' if success else 'error'}")

        except Exception as e:
            self.logger.error(f"ERROR: Phase 5: Error sending OCR response via pipe (ID: {command_id}): {e}", exc_info=True)
    
    def _publish_command_execution_metrics(self, command_type: str, success: bool, execution_time_ms: float, 
                                         command_id: str, parameters: Dict[str, Any], error: str = None):
        """Publish command execution metrics to telemetry for IntelliSense MCP."""
        if self._correlation_logger:
            try:
                timestamp_ns = time.perf_counter_ns()
                self._correlation_logger.log_event(
                    source_component="OCRZMQCommandListener",
                    event_name="OCRCommandExecution",
                    payload={
                        "command_type": command_type,
                        "command_id": command_id,
                        "success": success,
                        "execution_time_ms": execution_time_ms,
                        "parameters": parameters,
                        "error": error,
                        "timestamp_ns": timestamp_ns,
                        "timestamp": timestamp_ns / 1_000_000_000,
                        "correlation_id": str(uuid.uuid4())
                    }
                )
            except Exception as e:
                self.logger.error(f"Failed to publish OCR command execution metrics to correlation logger: {e}")
    
    def _handle_set_roi(self, parameters: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        """Handle SET_ROI_ABSOLUTE command."""
        try:
            x1 = parameters.get('x1', 0)
            y1 = parameters.get('y1', 0)
            x2 = parameters.get('x2', 100)
            y2 = parameters.get('y2', 100)
            
            roi = [x1, y1, x2, y2]
            
            if self.ocr_service and hasattr(self.ocr_service, 'set_roi'):
                self.ocr_service.set_roi(x1, y1, x2, y2)  # Use set_roi with individual coordinates
                self.last_known_roi = roi  # Update fallback state

                # MISSING LINK: Publish ROI update to Redis for GUI and config persistence
                self._publish_roi_update(roi, "SET_ROI_ABSOLUTE")

                return True, f"SUCCESS: Phase 5: ROI updated to ({x1}, {y1}, {x2}, {y2})", {"roi": roi}
            else:
                # Fallback: Just update our state
                self.last_known_roi = roi

                # MISSING LINK: Publish ROI update even in fallback mode
                self._publish_roi_update(roi, "SET_ROI_ABSOLUTE_FALLBACK")

                return True, f"FALLBACK: Phase 5: ROI updated in fallback mode to ({x1}, {y1}, {x2}, {y2})", {"roi": roi}

        except Exception as e:
            return False, f"ERROR: Phase 5: Error setting ROI: {e}", {}
    
    def _handle_roi_adjust(self, parameters: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        """Handle ROI_ADJUST command with support for individual edge adjustments."""
        try:
            direction = parameters.get('direction', '')
            edge = parameters.get('edge', None)  # New parameter for individual edge control
            step_size = parameters.get('step_size', 1)

            # Get current ROI
            current_roi = self.last_known_roi
            if self.ocr_service and hasattr(self.ocr_service, 'get_roi'):
                try:
                    current_roi = self.ocr_service.get_roi() or self.last_known_roi
                except Exception as e:
                    pass  # Use fallback

            # Apply adjustment
            new_roi = current_roi.copy()

            # Individual edge adjustments (new behavior)
            if edge:
                if edge == 'x1' and direction == 'left':
                    new_roi[0] -= step_size  # Move left edge left (expand)
                elif edge == 'x1' and direction == 'right':
                    new_roi[0] += step_size  # Move left edge right (contract)
                elif edge == 'x2' and direction == 'left':
                    new_roi[2] -= step_size  # Move right edge left (contract)
                elif edge == 'x2' and direction == 'right':
                    new_roi[2] += step_size  # Move right edge right (expand)
                elif edge == 'y1' and direction == 'up':
                    new_roi[1] -= step_size  # Move top edge up (expand)
                elif edge == 'y1' and direction == 'down':
                    new_roi[1] += step_size  # Move top edge down (contract)
                elif edge == 'y2' and direction == 'up':
                    new_roi[3] -= step_size  # Move bottom edge up (contract)
                elif edge == 'y2' and direction == 'down':
                    new_roi[3] += step_size  # Move bottom edge down (expand)
                else:
                    return False, f"ERROR: Invalid edge/direction combination: {edge}/{direction}", {}

            # Whole ROI movements (legacy behavior for compatibility)
            else:
                if direction == 'left':
                    new_roi[0] -= step_size
                    new_roi[2] -= step_size
                elif direction == 'right':
                    new_roi[0] += step_size
                    new_roi[2] += step_size
                elif direction == 'up':
                    new_roi[1] -= step_size
                    new_roi[3] -= step_size
                elif direction == 'down':
                    new_roi[1] += step_size
                    new_roi[3] += step_size
                else:
                    return False, f"ERROR: Invalid direction: {direction}", {}

            # Validate ROI bounds
            if new_roi[0] >= new_roi[2] or new_roi[1] >= new_roi[3]:
                return False, f"ERROR: Invalid ROI bounds after adjustment: {new_roi}", {}
            if new_roi[0] < 0 or new_roi[1] < 0:
                return False, f"ERROR: ROI coordinates cannot be negative: {new_roi}", {}

            # Apply the new ROI
            if self.ocr_service and hasattr(self.ocr_service, 'set_roi'):
                self.ocr_service.set_roi(new_roi[0], new_roi[1], new_roi[2], new_roi[3])  # Use set_roi with individual coordinates

            self.last_known_roi = new_roi

            # Publish ROI update to Redis for GUI and config persistence
            self._publish_roi_update(new_roi, f"ROI_ADJUST_{direction}")

            return True, f"SUCCESS: Phase 5: ROI adjusted {direction} by {step_size} to {new_roi}", {"roi": new_roi}

        except Exception as e:
            return False, f"ERROR: Phase 5: Error adjusting ROI: {e}", {}
    
    def _handle_get_roi(self) -> tuple[bool, str, Dict[str, Any]]:
        """Handle GET_ROI command."""
        try:
            roi = self.last_known_roi
            if self.ocr_service and hasattr(self.ocr_service, 'get_roi'):
                try:
                    roi = self.ocr_service.get_roi() or self.last_known_roi
                    self.last_known_roi = roi  # Update fallback state
                except:
                    pass  # Use fallback
            
            return True, f"SUCCESS: Phase 5: Current ROI: {roi}", {"roi": roi}

        except Exception as e:
            return False, f"ERROR: Phase 5: Error getting ROI: {e}", {}

    def _publish_roi_update(self, roi: List[int], source: str):
        """Publish ROI update to Redis stream for GUI and config persistence."""
        try:
            # Send ROI state update via pipe to ApplicationCore for ROI service processing
            roi_update_message = {
                "type": "roi_state_update",  # Use new message type that triggers ROI service
                "data": {
                    "roi": roi,
                    "source": f"OCRZMQCommandListener/{source}",
                    "timestamp": time.time()
                }
            }

            if hasattr(self, 'pipe_to_parent') and self.pipe_to_parent:
                self.pipe_to_parent.send(roi_update_message)
                self.logger.info(f"OCR_SERVICE: Sent 'roi_state_update' via pipe with ROI: {roi}")
            else:
                self.logger.error("OCR_SERVICE: pipe_to_parent_conn not available. Cannot send 'roi_state_update'.")

        except Exception as e:
            self.logger.error(f"OCR_SERVICE: Failed to send 'roi_state_update' via pipe: {e}", exc_info=True)

    def _send_ocr_parameters_state_update(self, parameters: Dict[str, Any]):
        """Send OCR parameters update to ApplicationCore via pipe for persistence to control.json."""
        try:
            # Send OCR parameters state update via pipe to ApplicationCore (like ROI does)
            ocr_params_update_message = {
                "type": "ocr_parameters_state_update",  # New message type for OCR parameter persistence
                "data": {
                    "parameters": parameters,
                    "source": "OCRZMQCommandListener/UPDATE_OCR_PREPROCESSING_FULL",
                    "timestamp": time.time()
                }
            }

            if hasattr(self, 'pipe_to_parent') and self.pipe_to_parent:
                self.pipe_to_parent.send(ocr_params_update_message)
                self.logger.info(f"OCR_SERVICE: Sent 'ocr_parameters_state_update' via pipe with {len(parameters)} parameters")
            else:
                self.logger.error("OCR_SERVICE: pipe_to_parent not available. Cannot send 'ocr_parameters_state_update'.")

        except Exception as e:
            self.logger.error(f"OCR_SERVICE: Failed to send 'ocr_parameters_state_update' via pipe: {e}", exc_info=True)

    def _send_ocr_status_update(self, status_event: str):
        """Send OCR status update to GUI via pipe to ApplicationCore."""
        try:
            status_message = {
                "type": "ocr_status_update",
                "data": {
                    "status_event": status_event,
                    "timestamp": time.time(),
                    "component": "OCR_PROCESS"
                }
            }

            self.pipe_to_parent.send(status_message)
            self.logger.info(f"Sent OCR status update '{status_event}' to ApplicationCore via pipe")

        except Exception as e:
            self.logger.error(f"Failed to send OCR status update '{status_event}': {e}", exc_info=True)
    
    def _handle_start_ocr(self) -> tuple[bool, str, Dict[str, Any]]:
        """Handle START_OCR command."""
        try:
            if self.ocr_service and hasattr(self.ocr_service, 'start_ocr'):
                self.ocr_service.start_ocr()
                # Send status update to GUI
                self._send_ocr_status_update("OCR_STARTED")
                return True, "SUCCESS: Phase 5: OCR started", {}
            else:
                self._send_ocr_status_update("OCR_START_FAILED")
                return False, "ERROR: Phase 5: OCR service not available", {}
        except Exception as e:
            self._send_ocr_status_update("OCR_START_FAILED")
            return False, f"ERROR: Phase 5: Error starting OCR: {e}", {}
    
    def _handle_stop_ocr(self) -> tuple[bool, str, Dict[str, Any]]:
        """Handle STOP_OCR command."""
        try:
            if self.ocr_service and hasattr(self.ocr_service, 'stop_ocr'):
                self.ocr_service.stop_ocr()
                # Send status update to GUI
                self._send_ocr_status_update("OCR_STOPPED")
                return True, "SUCCESS: Phase 5: OCR stopped", {}
            else:
                self._send_ocr_status_update("OCR_STOP_FAILED")
                return False, "ERROR: Phase 5: OCR service not available", {}
        except Exception as e:
            self._send_ocr_status_update("OCR_STOP_FAILED")
            return False, f"ERROR: Phase 5: Error stopping OCR: {e}", {}

    def _handle_get_ocr_status(self) -> tuple[bool, str, Dict[str, Any]]:
        """Handle GET_OCR_STATUS command - check current OCR state and send status update."""
        try:
            is_active = False
            if self.ocr_service and hasattr(self.ocr_service, '_ocr_active'):
                is_active = getattr(self.ocr_service, '_ocr_active', False)

            # Send appropriate status update to GUI
            if is_active:
                self._send_ocr_status_update("OCR_STARTED")
                return True, "SUCCESS: Phase 5: OCR is currently active", {"ocr_active": True}
            else:
                self._send_ocr_status_update("OCR_STOPPED")
                return True, "SUCCESS: Phase 5: OCR is currently inactive", {"ocr_active": False}

        except Exception as e:
            self._send_ocr_status_update("OCR_STOPPED")  # Default to stopped on error
            return False, f"ERROR: Phase 5: Error checking OCR status: {e}", {"ocr_active": False}

    def _handle_update_ocr_preprocessing(self, parameters: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        """Handle UPDATE_OCR_PREPROCESSING_FULL command - Update OCR preprocessing parameters."""
        try:
            self.logger.info(f"Processing UPDATE_OCR_PREPROCESSING_FULL with {len(parameters)} parameters")

            if not parameters:
                return False, "ERROR: No OCR parameters provided", {}

            # Update OCR service parameters if available
            if self.ocr_service and hasattr(self.ocr_service, 'set_preprocessing_params'):
                try:
                    # Convert parameters dict to the specific arguments expected by set_preprocessing_params
                    self._apply_parameters_to_ocr_service(parameters)
                    self.logger.info(f"SUCCESS: OCR preprocessing parameters updated: {list(parameters.keys())}")

                    # Send OCR parameters back to ApplicationCore for persistence (like ROI does)
                    self._send_ocr_parameters_state_update(parameters)

                    return True, f"SUCCESS: OCR preprocessing parameters updated ({len(parameters)} parameters)", {"parameters_updated": list(parameters.keys())}
                except Exception as e:
                    self.logger.error(f"Error updating OCR preprocessing parameters: {e}")
                    return False, f"ERROR: Failed to update OCR preprocessing parameters: {e}", {}
            else:
                # Fallback: Send to ApplicationCore for persistence even if OCR service doesn't support dynamic updates
                self.logger.warning("OCR service doesn't support set_preprocessing_params method, sending to ApplicationCore for persistence")
                self._send_ocr_parameters_state_update(parameters)
                return True, f"FALLBACK: OCR parameters sent to ApplicationCore for persistence ({len(parameters)} parameters)", {"parameters_saved": list(parameters.keys())}

        except Exception as e:
            return False, f"ERROR: Error processing OCR preprocessing update: {e}", {}

    def _save_ocr_parameters_to_config(self, parameters: Dict[str, Any]):
        """Save OCR parameters to control.json configuration."""
        try:
            from utils.global_config import update_config_key

            saved_count = 0
            for key, value in parameters.items():
                if key.startswith('ocr_'):
                    success = update_config_key(key, value)
                    if success:
                        saved_count += 1
                        self.logger.debug(f"Saved OCR parameter {key} = {value} to control.json")
                    else:
                        self.logger.warning(f"Failed to save OCR parameter {key} to control.json")

            self.logger.info(f"Saved {saved_count}/{len(parameters)} OCR parameters to control.json")

        except Exception as e:
            self.logger.error(f"Error saving OCR parameters to config: {e}", exc_info=True)

    def _apply_parameters_to_ocr_service(self, parameters: Dict[str, Any]):
        """Apply OCR parameters to the OCR service using the correct method signature."""
        try:
            # Extract parameters with defaults from current OCR service state
            current_params = self.ocr_service.get_preprocessing_params()

            # Map parameters to the expected arguments for set_preprocessing_params
            upscale_factor = parameters.get('ocr_upscale_factor', current_params[0])
            force_black_text_on_white = parameters.get('ocr_force_black_text_on_white', current_params[1])
            unsharp_strength = parameters.get('ocr_unsharp_strength', current_params[2])
            threshold_block_size = parameters.get('ocr_threshold_block_size', current_params[3])
            threshold_c = parameters.get('ocr_threshold_c', current_params[4])
            red_boost = parameters.get('ocr_red_boost', current_params[5])
            green_boost = parameters.get('ocr_green_boost', current_params[6])
            apply_text_mask_cleaning = parameters.get('ocr_apply_text_mask_cleaning', current_params[7])
            text_mask_min_contour_area = parameters.get('ocr_text_mask_min_contour_area', current_params[8])
            text_mask_min_width = parameters.get('ocr_text_mask_min_width', current_params[9])
            text_mask_min_height = parameters.get('ocr_text_mask_min_height', current_params[10])
            enhance_small_symbols = parameters.get('ocr_enhance_small_symbols', current_params[11])
            symbol_max_height = parameters.get('ocr_symbol_max_height', current_params[12])
            period_comma_ratio = parameters.get('ocr_period_comma_ratio_min', current_params[13][0]), parameters.get('ocr_period_comma_ratio_max', current_params[13][1])
            period_comma_radius = parameters.get('ocr_period_comma_radius', current_params[14])
            hyphen_min_ratio = parameters.get('ocr_hyphen_min_ratio', current_params[15])
            hyphen_min_height = parameters.get('ocr_hyphen_min_height', current_params[16])

            # Call the OCR service method with all parameters
            self.ocr_service.set_preprocessing_params(
                upscale_factor=upscale_factor,
                force_black_text_on_white=force_black_text_on_white,
                unsharp_strength=unsharp_strength,
                threshold_block_size=threshold_block_size,
                threshold_c=threshold_c,
                red_boost=red_boost,
                green_boost=green_boost,
                apply_text_mask_cleaning=apply_text_mask_cleaning,
                text_mask_min_contour_area=text_mask_min_contour_area,
                text_mask_min_width=text_mask_min_width,
                text_mask_min_height=text_mask_min_height,
                enhance_small_symbols=enhance_small_symbols,
                symbol_max_height_for_enhancement_upscaled=symbol_max_height,
                period_comma_aspect_ratio_range_upscaled=period_comma_ratio,
                period_comma_draw_radius_upscaled=period_comma_radius,
                hyphen_like_min_aspect_ratio_upscaled=hyphen_min_ratio,
                hyphen_like_draw_min_height_upscaled=hyphen_min_height
            )

            self.logger.info(f"Applied {len(parameters)} OCR parameters to OCR service successfully")

        except Exception as e:
            self.logger.error(f"Error applying OCR parameters to service: {e}", exc_info=True)
            raise
