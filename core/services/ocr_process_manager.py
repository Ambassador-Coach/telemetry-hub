"""
OCR Process Manager Service

This service manages the OCR subprocess and its communication with the main application.
It handles the lifecycle of the OCR process, pipe communication, and data routing.
Uses the proven multiprocessing pattern with top-level function target.
"""

import logging
import threading
import time
import uuid
import os
import multiprocessing
from typing import Optional, Dict, Any
from multiprocessing import Process, Pipe
from multiprocessing.connection import Connection

from interfaces.core.services import IConfigService, ILifecycleService, IBulletproofBabysitterIPCClient, IEventBus
from interfaces.core.telemetry_interfaces import ITelemetryService
from interfaces.ocr.services import IOCRProcessManager
from interfaces.ocr.services import IOCRDataConditioningService
from modules.ocr.data_types import OCRParsedData

logger = logging.getLogger(__name__)


class OCRProcessManager(IOCRProcessManager, ILifecycleService):
    """
    Manages the OCR subprocess and its communication with the main application.
    
    This service handles:
    - Starting and stopping the OCR subprocess using the proven multiprocessing pattern
    - Managing pipe communication
    - Routing OCR data to appropriate services
    - Health monitoring of the OCR process
    """
    
    def __init__(self, 
                 config_service: IConfigService,
                 telemetry_service: ITelemetryService,
                 ocr_data_conditioning_service: IOCRDataConditioningService,
                 event_bus: Optional[IEventBus] = None,
                 ipc_client: Optional[IBulletproofBabysitterIPCClient] = None):
        """
        Initialize the OCR Process Manager.
        
        Args:
            config_service: Configuration service
            telemetry_service: Telemetry service for publishing metrics
            ocr_data_conditioning_service: OCR data conditioning service for processing results
            event_bus: Optional event bus for application events
            ipc_client: Optional IPC client for external communication
        """
        self.logger = logger
        self._config = config_service.get_config() if hasattr(config_service, 'get_config') else config_service
        self._telemetry = telemetry_service
        self._ocr_data_conditioning_service = ocr_data_conditioning_service
        self.event_bus = event_bus
        self.ipc_client = ipc_client
        
        # Process management - following the original proven pattern
        self._process: Optional[multiprocessing.Process] = None
        self._pipe_conn: Optional[multiprocessing.connection.Connection] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # State tracking
        self._is_running = False
        
        self.logger.info("OCRProcessManager initialized")
        
    def start(self):
        """Start the OCR process (called by ServiceLifecycleManager)."""
        if getattr(self._config, 'disable_ocr', False):
            self.logger.info("OCR is disabled in configuration. Skipping OCR startup.")
            return
            
        start_ocr_on_init = getattr(self._config, 'start_ocr_on_init', True)
        
        if start_ocr_on_init:
            self.logger.info("Starting OCR process on initialization (start_ocr_on_init=True)")
            self._start_ocr_process()
        else:
            self.logger.info("OCR process will be started on-demand (start_ocr_on_init=False)")
            
    def stop(self):
        """Stop the OCR process and cleanup resources."""
        self.logger.info("Stopping OCR process...")
        self._stop_event.set()
        
        if self._process and self._process.is_alive():
            self._process.terminate()
            self._process.join(2.0)
            if self._process.is_alive():
                self.logger.warning("OCR process did not terminate gracefully, killing...")
                self._process.kill()
                
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(1.0)
            
        if self._pipe_conn:
            self._pipe_conn.close()
            
        self._cleanup_state()
        self.logger.info("OCR process stopped")
        
    def _start_ocr_process(self):
        """Start the OCR subprocess using the proven pattern."""
        if self._process and self._process.is_alive():
            self.logger.warning("OCR process already running")
            return
            
        try:
            # 1. Create the Pipe for primary data communication
            parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
            self._pipe_conn = parent_conn

            # 2. Build the specific OcrConfigData object
            ocr_config_data = self._build_ocr_config_data()
            log_base_dir = getattr(self._config, 'LOG_BASE_DIR', 'logs')

            # 3. CRITICAL FIX: Build the configuration dictionary for the child process's logger
            telemetry_config_for_child = self._build_telemetry_config()

            # 4. Import the target function
            from modules.ocr.ocr_process_main import run_ocr_service_process

            # 5. THE FIX APPLIED: The `args` tuple now includes the telemetry config.
            # This ensures the child process has the information it needs to construct
            # its own fully functional ICorrelationLogger instance.
            self._process = multiprocessing.Process(
                target=run_ocr_service_process,
                args=(child_conn, ocr_config_data, log_base_dir, telemetry_config_for_child),
                name="OCRChildProcess"
            )
            self._process.daemon = True
            
            # 6. Start the process and its listener thread
            self._process.start()
            
            self._listener_thread = threading.Thread(
                target=self._listen_for_data,
                name="OCRPipeListener",
                daemon=True
            )
            self._listener_thread.start()
            
            self._is_running = True
            self.logger.info(f"OCRProcessManager started. Process PID: {self._process.pid}")
            
        except Exception as e:
            self.logger.error(f"Failed to start OCR process: {e}", exc_info=True)
            self._cleanup_failed_start()
            
    def _cleanup_failed_start(self):
        """Clean up after failed process start."""
        if self._pipe_conn:
            self._pipe_conn.close()
            self._pipe_conn = None
        if self._process:
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        self._is_running = False
        
    def _cleanup_state(self):
        """Clean up internal state."""
        self._process = None
        self._pipe_conn = None
        self._listener_thread = None
        self._is_running = False
        
    def _listen_for_data(self):
        """Listen for data from the OCR subprocess."""
        while not self._stop_event.is_set():
            try:
                if self._pipe_conn and self._pipe_conn.poll(timeout=0.2):
                    message = self._pipe_conn.recv()
                    self._handle_ocr_message(message)
            except (EOFError, BrokenPipeError):
                self.logger.warning("OCR process pipe closed unexpectedly.")
                break
            except Exception as e:
                self.logger.error(f"Error in OCR listener thread: {e}", exc_info=True)
                break
                
    def _handle_ocr_message(self, message: Dict[str, Any]):
        """Handle messages received from the OCR subprocess."""
        try:
            message_type = message.get("type")
            
            if message_type == "ocr_parsed_data":
                # Enqueue the message to the conditioning service
                ocr_data = message.get("data")
                if self._ocr_data_conditioning_service and ocr_data:
                    # --- CAPTURE T4 ---
                    t4_handoff_to_conditioner_ns = time.perf_counter_ns()
                    
                    # Ensure we have an OCRParsedData object
                    if isinstance(ocr_data, dict):
                        from modules.ocr.data_types import OCRParsedData
                        parsed_data = OCRParsedData(**ocr_data)
                    else:
                        parsed_data = ocr_data
                    
                    # Add the T4 timestamp to the object before passing it on
                    if hasattr(parsed_data, 't4_handoff_to_conditioner_ns'):
                        parsed_data.t4_handoff_to_conditioner_ns = t4_handoff_to_conditioner_ns
                    
                    # Enqueue the modified data object
                    self._ocr_data_conditioning_service.enqueue_raw_ocr_data(parsed_data)
                    
                    # --- Report T0 -> T4 Latency ---
                    t0_ns = getattr(parsed_data, 'raw_image_grab_ns', None)
                    correlation_id = getattr(parsed_data, 'master_correlation_id', 'unknown')
                    
                    if self._telemetry and t0_ns:
                        duration_ms = (t4_handoff_to_conditioner_ns - t0_ns) / 1_000_000
                        self._telemetry.enqueue(
                            source_component="OCRProcessManager",
                            event_type="OCR_PROCESS_LATENCY",
                            payload={
                                "correlation_id": correlation_id,
                                "t0_to_t4_ms": duration_ms,
                                "timestamps_ns": {"t0": t0_ns, "t4": t4_handoff_to_conditioner_ns}
                            },
                            stream_override="testrade:telemetry:performance"
                        )
                        self.logger.info(f"[PIPELINE_METRIC] T0-T4 for CorrID {correlation_id}: {duration_ms:.2f}ms")
                    
            elif message_type == "ocr_status_update":
                # Handle status updates
                status_data = message.get("data", {})
                self.logger.debug(f"OCR status update: {status_data}")
                
            elif message_type == "ocr_error_event":
                # Handle error events
                error_data = message.get("data", {})
                self.logger.warning(f"OCR error event: {error_data}")
                
            else:
                self.logger.debug(f"Unknown OCR message type: {message_type}")
                
        except Exception as e:
            self.logger.error(f"Error handling OCR message: {e}", exc_info=True)
            
    def _build_ocr_config_data(self):
        """
        Build proper OcrConfigData object from global config.
        This ensures the OCR subprocess gets the complete configuration it needs.
        """
        from interfaces.ocr.services import OcrConfigData
        
        # Convert flicker_filter object to dict if needed
        flicker_params = getattr(self._config, 'flicker_filter', None)
        if flicker_params and hasattr(flicker_params, '__dict__'):
            flicker_params = flicker_params.__dict__
        
        return OcrConfigData(
            # Core configuration
            tesseract_cmd=getattr(self._config, 'TESSERACT_CMD', ''),
            initial_roi=getattr(self._config, 'ROI_COORDINATES', [0, 0, 800, 600]),
            include_all_symbols=getattr(self._config, 'include_all_symbols', False),
            flicker_params=flicker_params,
            debug_ocr_handler=getattr(self._config, 'enable_ocr_debug_logging', False),
            
            # Video input configuration
            video_file_path=getattr(self._config, 'OCR_INPUT_VIDEO_FILE_PATH', None),
            video_loop_enabled=getattr(self._config, 'VIDEO_LOOP_ENABLED', False),
            
            # Timing configuration
            ocr_capture_interval_seconds=getattr(self._config, 'ocr_capture_interval_seconds', 0.2),
            
            # ZMQ Command Listener configuration
            ocr_ipc_command_pull_address=getattr(self._config, 'ocr_ipc_command_pull_address', 'tcp://127.0.0.1:5559'),
            
            # Preprocessing parameters (matching OcrConfigData field names exactly)
            upscale_factor=getattr(self._config, 'ocr_upscale_factor', 2.5),
            force_black_text_on_white=getattr(self._config, 'ocr_force_black_text_on_white', True),
            unsharp_strength=getattr(self._config, 'ocr_unsharp_strength', 1.8),
            threshold_block_size=getattr(self._config, 'ocr_threshold_block_size', 25),
            threshold_c=getattr(self._config, 'ocr_threshold_c', -3),
            red_boost=getattr(self._config, 'ocr_red_boost', 1.8),
            green_boost=getattr(self._config, 'ocr_green_boost', 1.8),
            apply_text_mask_cleaning=getattr(self._config, 'ocr_apply_text_mask_cleaning', True),
            text_mask_min_contour_area=getattr(self._config, 'ocr_text_mask_min_contour_area', 10),
            text_mask_min_width=getattr(self._config, 'ocr_text_mask_min_width', 2),
            text_mask_min_height=getattr(self._config, 'ocr_text_mask_min_height', 2),
            enhance_small_symbols=getattr(self._config, 'ocr_enhance_small_symbols', True),
            symbol_max_height_for_enhancement_upscaled=getattr(self._config, 'ocr_symbol_max_height', 20),
            period_comma_aspect_ratio_range_upscaled=(
                getattr(self._config, 'ocr_period_comma_ratio_min', 0.5),
                getattr(self._config, 'ocr_period_comma_ratio_max', 1.8)
            ),
            period_comma_draw_radius_upscaled=getattr(self._config, 'ocr_period_comma_radius', 3),
            hyphen_like_min_aspect_ratio_upscaled=getattr(self._config, 'ocr_hyphen_min_ratio', 1.8),
            hyphen_like_draw_min_height_upscaled=getattr(self._config, 'ocr_hyphen_min_height', 2)
        )
    
    def _build_telemetry_config(self) -> Dict[str, Any]:
        """
        Build telemetry configuration for CorrelationLogger initialization in OCR process.
        This extracts Redis and telemetry-related config for cross-process initialization.
        """
        return {
            # Redis configuration
            'REDIS_HOST': getattr(self._config, 'REDIS_HOST', '172.22.202.120'),
            'REDIS_PORT': getattr(self._config, 'REDIS_PORT', 6379),
            'REDIS_DB': getattr(self._config, 'REDIS_DB', 0),
            'REDIS_PASSWORD': getattr(self._config, 'REDIS_PASSWORD', None),
            
            # Redis connection settings
            'redis_connect_timeout_sec': getattr(self._config, 'redis_connect_timeout_sec', 2.0),
            'redis_socket_timeout_sec': getattr(self._config, 'redis_socket_timeout_sec', 2.0),
            'redis_retry_on_timeout': getattr(self._config, 'redis_retry_on_timeout', True),
            
            # Stream configurations for OCR
            'redis_stream_raw_ocr_data': getattr(self._config, 'redis_stream_raw_ocr_data', 'testrade:raw-ocr-data'),
            'redis_stream_raw_ocr': getattr(self._config, 'redis_stream_raw_ocr', 'testrade:raw-ocr-events'),
            
            # Stream settings
            'redis_stream_max_length': getattr(self._config, 'redis_stream_max_length', 500),
            'redis_stream_ttl_seconds': getattr(self._config, 'redis_stream_ttl_seconds', 3600),
            
            # Telemetry settings
            'enable_raw_ocr_recording': getattr(self._config, 'enable_raw_ocr_recording', True),
            'ENABLE_IPC_DATA_DUMP': getattr(self._config, 'ENABLE_IPC_DATA_DUMP', False),
            
            # Performance settings
            'redis_default_queue_max_size': getattr(self._config, 'redis_default_queue_max_size', 10000),
            'redis_default_publisher_threads': getattr(self._config, 'redis_default_publisher_threads', 1)
        }
        
    def send_ocr_command(self, command_type: str, parameters: Dict[str, Any]) -> str:
        """Send a command to the OCR service (interface method)."""
        return self.send_command(command_type, parameters)
        
    def check_ocr_health(self) -> Dict[str, Any]:
        """Check OCR process health status."""
        health_data = {
            "process_running": self._process is not None and self._process.is_alive(),
            "pipe_connected": self._pipe_conn is not None,
            "listener_thread_alive": self._listener_thread is not None and self._listener_thread.is_alive(),
            "is_ready": self.is_ready
        }
        
        if self._process:
            health_data.update({
                "process_pid": self._process.pid,
                "process_name": self._process.name,
                "process_exitcode": self._process.exitcode
            })
            
        return health_data
        
    def send_command(self, command_type: str, parameters: Optional[Dict[str, Any]] = None) -> str:
        """
        Send a command to the OCR process via IPC client.
        
        Args:
            command_type: Type of command to send
            parameters: Optional parameters for the command
            
        Returns:
            Command ID for tracking
        """
        command_id = str(uuid.uuid4())
        
        if self.ipc_client:
            command_data = {
                "command_id": command_id,
                "command_type": command_type,
                "parameters": parameters or {}
            }
            
            try:
                self.ipc_client.send_message("ocr_commands", command_data)
                self.logger.info(f"Sent OCR command '{command_type}' with ID: {command_id}")
            except Exception as e:
                self.logger.error(f"Failed to send OCR command: {e}")
        else:
            self.logger.error("IPC client not available, cannot send OCR command")
            
        return command_id
        
    @property
    def is_ready(self) -> bool:
        """Check if the OCR process is ready."""
        return self._process is not None and self._process.is_alive()
        
    @property
    def ocr_process(self) -> Optional[multiprocessing.Process]:
        """Get the OCR process (for compatibility with existing code)."""
        return self._process