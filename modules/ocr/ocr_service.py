# modules/ocr/ocr_service.py

import threading
import time
import re
import logging
import os
import copy # Added for deep copying args/kwargs in _notify_observer
import uuid
import base64
from typing import List, Optional, Set, Dict, Any, Callable, Tuple
from queue import Queue, Empty as QueueEmpty, Full as QueueFull
from datetime import datetime
import weakref # Added to hold observer weakly

import numpy as np
from PIL import ImageGrab  # For screen capture only

# C++ OCR Accelerator import (Windows only)
import sys
import platform
import os

if platform.system() == "Windows":
    try:
        # FUZZY'S SIMPLE, RELIABLE OCR ACCELERATOR LOADER
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        build_dir = os.path.join(project_root, "ocr_accelerator", "x64", "Release")
        build_dir = os.path.abspath(build_dir)

        if not os.path.exists(build_dir):
            raise ImportError(f"Build directory not found: {build_dir}")

        # Check critical files
        pyd_file = os.path.join(build_dir, "ocr_accelerator.pyd")
        if not os.path.exists(pyd_file):
            raise ImportError(f"ocr_accelerator.pyd not found in {build_dir}")

        # Add to DLL search path
        if hasattr(os, 'add_dll_directory'):
            os.add_dll_directory(build_dir)

        # Add to PATH
        current_path = os.environ.get('PATH', '')
        if build_dir not in current_path:
            os.environ['PATH'] = build_dir + os.pathsep + current_path

        # Add to sys.path
        if build_dir not in sys.path:
            sys.path.insert(0, build_dir)

        # Change to build directory and import
        original_cwd = os.getcwd()
        try:
            os.chdir(build_dir)
            import ocr_accelerator
        finally:
            os.chdir(original_cwd)

        CPP_ACCELERATOR_AVAILABLE = True
        CPP_ACCELERATOR_LOAD_STATUS = f"C++ accelerator loaded successfully from {build_dir}"
    except ImportError as e:
        CPP_ACCELERATOR_AVAILABLE = False
        CPP_ACCELERATOR_LOAD_STATUS = f"CRITICAL: C++ accelerator failed to load on Windows: {e}"
    except Exception as e:
        CPP_ACCELERATOR_AVAILABLE = False
        CPP_ACCELERATOR_LOAD_STATUS = f"CRITICAL: Error setting up C++ accelerator path: {e}"
else:
    CPP_ACCELERATOR_AVAILABLE = False
    CPP_ACCELERATOR_LOAD_STATUS = f"C++ accelerator not available on {platform.system()} (Windows required)"

from data_models import OCRParsedData  # Import from centralized data models

# Local and project root imports
try:
    from interfaces.ocr.services import IOcrService, IAppObserver, OcrConfigData
    # Removed image_utils import - C++ accelerator handles all preprocessing
    from utils.function_tracer import trace_function
    from utils.performance_tracker import create_timestamp_dict, add_timestamp, calculate_durations, add_to_stats, is_performance_tracking_enabled, capture_metric_with_benchmarker, finalize_performance_scope
    from utils.decorators import log_function_call
    from utils.thread_safe_uuid import get_thread_safe_uuid
    from config import VIDEO_DIR
    from interfaces.logging.services import ICorrelationLogger
except ImportError as e:
    logging.error(f"Failed to import utility modules: {e}")
    # Provide dummy implementations if needed for testing structure
    def trace_function(module): return lambda func: func
    def is_performance_tracking_enabled(): return False

# Stub for apply_preprocessing - no longer needed with C++ accelerator
def apply_preprocessing(frame): return frame

# Additional stubs if imports fail
if 'create_timestamp_dict' not in globals():
    def create_timestamp_dict(): return None
    def add_timestamp(*args, **kwargs): return args[0] if args else None
    def calculate_durations(*args, **kwargs): return {}
    def add_to_stats(*args, **kwargs): pass

logger = logging.getLogger(__name__)

# Log C++ accelerator status after logger is available
if CPP_ACCELERATOR_AVAILABLE:
    logger.info(f"C++ OCR accelerator {CPP_ACCELERATOR_LOAD_STATUS}")
else:
    logger.warning(f"C++ OCR accelerator {CPP_ACCELERATOR_LOAD_STATUS}")

# Helper functions have been moved to the ProcessedOCRHandler

# --- OCR Service Class ---

class OCRService(IOcrService):
    """
    Manages OCR operations, screen capture, and video recording in separate threads.
    Communicates with the application via an observer pattern.
    """

    # Confidence check frequency constants
    ROI_OPTIMIZATION_CONF_FREQ = 5
    NORMAL_OPERATION_CONF_FREQ = 1000

    @log_function_call('ocr_service')
    def __init__(self,
                 observer: IAppObserver,
                 config: OcrConfigData,
                 pipe_to_parent: Optional[Any] = None,
                 correlation_logger: Optional['ICorrelationLogger'] = None):
        """
        Initializes the OCR Service with a clean, decoupled signature.

        Args:
            observer: An object to send results to (e.g., the pipe observer).
            config: A dataclass containing all necessary OCR and preprocessing parameters.
            pipe_to_parent: Pipe connection to send status updates back to ApplicationCore.
            correlation_logger: (Optional) Logger for correlation events.
        
        Note: Symbol validation is handled by PythonOCRDataConditioner, not OCRService.
        This service is responsible ONLY for OCR capture and processing, not trading logic.
        """
        self.logger = logging.getLogger(__name__)
        self._observer = weakref.ref(observer) if observer else None
        self._config = config
        self._pipe_to_parent = pipe_to_parent
        self._correlation_logger = correlation_logger
        
        # Log clean initialization
        if hasattr(self._config, 'ocr_capture_interval_seconds'):
            self.logger.info(f"OCR_SERVICE_INIT: Received config with ocr_capture_interval_seconds = {self._config.ocr_capture_interval_seconds:.3f}")
        else:
            self.logger.warning("OCR_SERVICE_INIT: OcrConfigData received does NOT have ocr_capture_interval_seconds field.")

        # REMOVED - These dependencies do not belong in the isolated OCR process:
        # self._tkinter_root = tkinter_root_for_callbacks (GUI dependency in headless service)
        # self._initialization_complete_event (trading dependency)  
        # self._trade_manager_service (trading dependency)
        
        self.logger.info("OCRService initialized with a clean, decoupled signature.")

        # Use values from config object
        self._tesseract_cmd = self._config.tesseract_cmd # Get from config
        self._roi_coordinates = list(self._config.initial_roi) # Ensure mutable list copy

        # Video input removed - C++ accelerator only uses live screen capture

        # REMOVED - Symbol validation is handled by PythonOCRDataConditioner, not OCRService
        # This service is responsible ONLY for OCR capture and processing, not symbol validation

        # Single informative log about initialization
        self.logger.info(f"OCRService initialized. Initial upscale: {self._config.upscale_factor}, unsharp: {self._config.unsharp_strength}, block_size: {self._config.threshold_block_size}")
        # Detailed logs at debug level
        self.logger.debug(f"OCRService.__init__: Initializing all 17 preprocessing parameters from OcrConfigData object ID: {id(self._config)}")
        self.logger.debug(f"OCRService.__init__: OcrConfigData received: unsharp={self._config.unsharp_strength}, block={self._config.threshold_block_size}, C={self._config.threshold_c}, red_boost={self._config.red_boost}, green_boost={self._config.green_boost}")
        self.logger.debug(f"OCRService.__init__: OcrConfigData received (symbol enhance): enhance_symbols={self._config.enhance_small_symbols}, symbol_max_h={self._config.symbol_max_height_for_enhancement_upscaled}, period_ratio={self._config.period_comma_aspect_ratio_range_upscaled}")

        # === START MODIFICATION / VERIFICATION AREA ===
        # Explicitly set all 17 internal attributes from the 'config' (OcrConfigData) object
        self._upscale_factor = self._config.upscale_factor
        self._force_black_text_on_white = self._config.force_black_text_on_white
        self._unsharp_strength = self._config.unsharp_strength
        self._threshold_block_size = self._config.threshold_block_size
        self._threshold_c = self._config.threshold_c
        self._red_boost = self._config.red_boost
        self._green_boost = self._config.green_boost

        self._apply_text_mask_cleaning = self._config.apply_text_mask_cleaning
        self._text_mask_min_contour_area = self._config.text_mask_min_contour_area
        self._text_mask_min_width = self._config.text_mask_min_width
        self._text_mask_min_height = self._config.text_mask_min_height

        self._enhance_small_symbols = self._config.enhance_small_symbols
        # Ensure attribute names on 'config' (e.g., config.symbol_max_height_for_enhancement_upscaled)
        # match EXACTLY what is defined in the OcrConfigData dataclass in interfaces.py
        self._symbol_max_height = self._config.symbol_max_height_for_enhancement_upscaled
        self._period_comma_ratio = self._config.period_comma_aspect_ratio_range_upscaled
        self._period_comma_radius = self._config.period_comma_draw_radius_upscaled
        self._hyphen_min_ratio = self._config.hyphen_like_min_aspect_ratio_upscaled
        self._hyphen_min_height = self._config.hyphen_like_draw_min_height_upscaled
        # === END MODIFICATION / VERIFICATION AREA ===

        # Detailed logs at debug level
        self.logger.debug(f"OCRService.__init__: AFTER ASSIGNMENT to internal vars: self._unsharp_strength = {self._unsharp_strength}, self._threshold_block_size = {self._threshold_block_size}, self._threshold_c = {self._threshold_c}, self._red_boost = {self._red_boost}, self._green_boost = {self._green_boost}")
        self.logger.debug(f"OCRService.__init__: Symbol enhancement params AFTER ASSIGNMENT: self._enhance_small_symbols = {self._enhance_small_symbols}, self._symbol_max_height = {self._symbol_max_height}, self._period_comma_ratio = {self._period_comma_ratio}, self._hyphen_min_ratio = {self._hyphen_min_ratio}")

        # --- Internal State ---
        self._ocr_active = False
        self._recording_active = False
        self._capture_active = False # Separate flag for capture thread
        self._confidence_check_frequency = self.ROI_OPTIMIZATION_CONF_FREQ # Default
        self._confidence_mode_is_frequent = True

        # --- Threading ---
        self._capture_thread: Optional[threading.Thread] = None
        self._ocr_thread: Optional[threading.Thread] = None
        self._recording_thread: Optional[threading.Thread] = None
        # NEW: Lock-protected "mailbox" for the latest frame, replacing the queue.
        self._latest_frame_package: Optional[Dict[str, Any]] = None
        self._frame_lock = threading.Lock()
        self._new_frame_condition = threading.Condition(self._frame_lock)
        self._preview_output_queue = Queue(maxsize=2) # NEW: For OCR-ready frames: OCR_Loop -> App Preview
        self._stop_capture_event = threading.Event()
        self._stop_ocr_event = threading.Event()
        self._stop_recording_event = threading.Event()

        # --- Performance Degradation Thresholds ---
        self._confidence_drop_threshold = 70.0  # Alert if confidence drops below 70%
        self._processing_timeout_threshold_ms = 500.0  # Alert if total processing takes > 500ms
        self._ocr_timeout_threshold_ms = 300.0  # Alert if OCR step takes > 300ms
        self._preprocessing_timeout_threshold_ms = 100.0  # Alert if preprocessing takes > 100ms
        self._queue_timeout_threshold_ms = 50.0  # Alert if queue operations take > 50ms
        
        # Performance tracking state
        self._last_confidence = 100.0  # Track previous confidence for drop detection
        self._confidence_drop_count = 0  # Track consecutive confidence drops
        self._performance_alert_cooldown = {}  # Prevent spam alerts

        # --- Resources ---
        self._video_writer = None  # Video recording removed
        self._lock = threading.Lock() # Lock for sensitive state changes if needed

        # Golden timestamp tracking for status updates
        self._current_frame_context: Optional[Dict[str, Any]] = None
        self._frame_context_lock = threading.Lock()

        # REMOVED - Trading dependencies do not belong in isolated OCR process:
        # self._initialization_complete_event (trading coordination)
        # self._trade_manager_service (trading logic)

        # Regex patterns have been moved to the ProcessedOCRHandler

        # C++ OCR accelerator is now mandatory - no pytesseract configuration needed
        # Initialize the C++ OCR engine ONCE during startup
        if CPP_ACCELERATOR_AVAILABLE:
            try:
                print("[C++ ENGINE] Initializing persistent C++ OCR engine...", flush=True)
                self._cpp_engine = ocr_accelerator.OcrEngine("eng")  # Initialize with English
                print("[C++ ENGINE] C++ OCR engine initialized successfully!", flush=True)
            except Exception as e:
                logger.error(f"Failed to initialize C++ OCR engine: {e}")
                raise RuntimeError(f"C++ OCR engine initialization failed: {e}")
        else:
            raise RuntimeError(f"C++ OCR accelerator is required but not available: {CPP_ACCELERATOR_LOAD_STATUS}")
        
        # --- DEFINITIVE INJECTION VALIDATION ---
        # This block runs once at startup to prove what was injected.
        if self._correlation_logger is not None:
            self.logger.critical(
                f"### [INJECTION PROOF] OCRService INITIALIZED WITH a CorrelationLogger. Type: {type(self._correlation_logger).__name__} ###"
            )
        else:
            self.logger.critical(
                f"### [INJECTION PROOF] OCRService INITIALIZED WITHOUT a CorrelationLogger. The object is None. ###"
            )
        # --- END OF VALIDATION ---

        self._start_capture_thread() # Start capture thread immediately

    # Removed _configure_pytesseract - C++ accelerator handles OCR directly
    # Removed _initialize_video_input - live screen capture only

    # Removed _initialize_video_input - live screen capture only

    @log_function_call('ocr_service')
    def _notify_observer(self, method_name: str, *args, **kwargs):
        """
        Safely calls a method on the observer if it exists.
        For GUI observers, ensures the callback runs on the main thread using root.after().
        """
        observer = self._observer() if self._observer else None # Dereference weakref

        if observer and hasattr(observer, method_name):
            method_to_call = getattr(observer, method_name)

            # Create deep copies of args and kwargs to prevent issues if they are modified
            # by the OCRThread after scheduling the call but before it executes on the GUI thread.
            try:
                args_copy = copy.deepcopy(args)
                kwargs_copy = copy.deepcopy(kwargs)
            except Exception as e_copy:
                self.logger.error(f"OCRService: Could not deepcopy args/kwargs for observer method {method_name}: {e_copy}. Using original (RISKY).")
                args_copy = args # Fallback
                kwargs_copy = kwargs # Fallback

            # CLEAN ARCHITECTURE: Direct observer call without GUI thread dependencies
            # The OCR service should not have GUI-specific logic
            try:
                self.logger.debug(f"OCRService: Calling observer.{method_name} directly.")
                method_to_call(*args_copy, **kwargs_copy)
            except Exception as e_direct:
                self.logger.error(f"OCRService: Error in observer call for {method_name}: {e_direct}", exc_info=True)
        elif observer and not hasattr(observer, method_name):
            self.logger.warning(f"OCRService: Observer {observer} does not have method {method_name}.")
        elif not observer:
            self.logger.debug(f"OCRService: No observer to notify for method {method_name} (observer is None or weakref expired).")

    @log_function_call('ocr_service')
    def _notify_status(self, message: str):
        """Convenience method to notify observer of status updates."""
        logger.info(f"Status Update: {message}")
        self._notify_observer("on_status_update", message)


    @log_function_call('ocr_service')
    def _debug_print(self, message: str):
        """Prints debug messages if debug mode is enabled in config."""
        if hasattr(self._config, 'debug_ocr_service') and self._config.debug_ocr_service:
            self.logger.debug(f"[OCR_SERVICE_DEBUG] {message}")
    
    def _get_preprocessing_kwargs(self) -> Dict[str, Any]:
        """Helper to gather all preprocessing parameters into a dict for analysis metadata."""
        return {
            "upscale_factor": self._upscale_factor,
            "force_black_text_on_white": self._force_black_text_on_white,
            "unsharp_strength": self._unsharp_strength,
            "threshold_block_size": self._threshold_block_size,
            "threshold_c": self._threshold_c,
            "red_boost": self._red_boost,
            "green_boost": self._green_boost,
            "apply_text_mask_cleaning": getattr(self, '_apply_text_mask_cleaning', False),
            "text_mask_min_contour_area": getattr(self, '_text_mask_min_contour_area', 100),
            "text_mask_min_width": getattr(self, '_text_mask_min_width', 5),
            "text_mask_min_height": getattr(self, '_text_mask_min_height', 5),
            "enhance_small_symbols": getattr(self, '_enhance_small_symbols', False),
            "symbol_max_height_for_enhancement_upscaled": getattr(self, '_symbol_max_height', 20),
            "period_comma_aspect_ratio_range_upscaled": getattr(self, '_period_comma_ratio', (0.5, 1.5)),
            "period_comma_draw_radius_upscaled": getattr(self, '_period_comma_radius', 3),
            "hyphen_like_min_aspect_ratio_upscaled": getattr(self, '_hyphen_min_ratio', 2.0),
            "hyphen_like_draw_min_height_upscaled": getattr(self, '_hyphen_min_height', 3)
        }

    # --- Screen Capture Thread ---

    @log_function_call('ocr_service')
    def _get_screen_frame(self) -> Optional[np.ndarray]:
        """Captures the current frame from live screen using PIL only."""
        try:
            # Use a thread-safe copy of ROI
            with self._lock:
                roi = tuple(self._roi_coordinates)

            # Capture ONLY the ROI area for efficiency using PIL
            from PIL import ImageGrab
            
            if self._capture_loop_iteration <= 2:  # Only log first couple attempts
                self.logger.debug(f"About to call ImageGrab.grab with ROI: {roi}")
            
            screen = ImageGrab.grab(bbox=roi)
            
            if self._capture_loop_iteration <= 2:
                self.logger.debug(f"ImageGrab returned: {type(screen)} with size: {screen.size if screen else 'None'}")
            
            # Convert PIL image directly to numpy array (RGB format for C++ accelerator)
            frame = np.array(screen)
            
            if self._capture_loop_iteration == 1:
                self.logger.debug(f"Frame shape after PIL conversion: {frame.shape}")
            
            return frame

        except Exception as e:
            # Log less frequently to avoid spamming if ROI is constantly invalid
            logger.error(f"Error in _get_screen_frame: {e}")
            self.logger.debug(f"Exception type: {type(e).__name__}")
            
            # Publish screen capture error
            error_context = {
                "roi_coordinates": getattr(self, '_roi_coordinates', 'unknown'),
                "capture_method": "PIL_ImageGrab",
                "frame_operation": "screen_capture"
            }
            self._publish_ocr_error_via_pipe(
                error_type="SCREEN_CAPTURE_ERROR",
                error_message=f"Error in screen frame capture: {e}",
                error_context=error_context,
                exception_obj=e
            )
            
            # Return a minimal valid frame on error to prevent downstream None errors
            return np.zeros((100, 100, 3), dtype=np.uint8)

    @log_function_call('ocr_service')
    def _capture_loop(self):
        """Target function for the screen capture thread."""
        logger.info("Screen capture loop started with PIL ImageGrab.")
        self._capture_active = True
        self._capture_loop_iteration = 0

        while not self._stop_capture_event.is_set():
            try:
                self._capture_loop_iteration += 1
                if self._capture_loop_iteration == 1:
                    self.logger.debug(f"First iteration of capture loop in process {os.getpid()}")
                
                # === ID GENERATION & T0 MILESTONE ===
                # MILESTONE T0: Image Ingress. This is the true start of the event chain.
                T0_ImageIngress_ns = time.perf_counter_ns()
                
                # Generate all core IDs according to the guide.
                event_id = get_thread_safe_uuid()
                correlation_id = event_id  # For the first event, correlationId is the same as eventId.
                causation_id = None      # The first event has no cause.

                # Create the standard metadata block. The official timestamp is T0.
                event_metadata = {
                    "eventId": event_id,
                    "correlationId": correlation_id,
                    "causationId": causation_id,
                    "timestamp_ns": T0_ImageIngress_ns, # Official event time is T0 - golden timestamp
                    "eventType": "TESTRADE_RAW_OCR_EVENT",
                    "sourceComponent": "OCRService"
                }
                
                self.logger.debug(f"[ID_GENESIS] New event chain started. CorrID: {correlation_id}")
                
                # Keep legacy fields for compatibility during transition
                master_correlation_id = correlation_id
                raw_image_grab_ns = T0_ImageIngress_ns
                capture_timestamp_ns = T0_ImageIngress_ns
                
                # Get a frame using ImageGrab
                frame = self._get_screen_frame()
                
                # Capture completed timestamp
                capture_completed_ns = time.perf_counter_ns()
                capture_duration_ms = (capture_completed_ns - raw_image_grab_ns) / 1_000_000
                
                if capture_duration_ms > 50:  # Log if capture took more than 50ms
                    self.logger.warning(f"[OCR_ORIGIN] Slow image capture: {capture_duration_ms:.1f}ms for CorrID: {master_correlation_id}")
                    
                    # Log slow capture degradation via CorrelationLogger
                    if self._correlation_logger:
                        try:
                            timestamp_ns = time.perf_counter_ns()
                            log_payload = {
                                "performance_degradation_type": "SLOW_IMAGE_CAPTURE",
                                "capture_duration_ms": capture_duration_ms,
                                "threshold_ms": 50.0,
                                "degradation_factor": capture_duration_ms / 50.0,
                                "correlation_id": master_correlation_id,
                                "roi_coordinates": list(self._roi_coordinates),
                                "timestamp_ns": timestamp_ns
                            }
                            self._correlation_logger.log_event(
                                source_component="OCRService",
                                event_name="OcrPerformanceDegradation",
                                payload=log_payload
                            )
                        except Exception as log_error:
                            self.logger.error(f"Failed to log OCR capture degradation: {log_error}")

                # Generate raw image grab metadata for IntelliSense
                if frame is not None:
                    raw_img_roi_used = list(self._roi_coordinates)

                    # Encode raw frame to base64 for streaming using PIL
                    try:
                        from PIL import Image
                        import io
                        pil_image = Image.fromarray(frame)
                        buffer = io.BytesIO()
                        pil_image.save(buffer, format='PNG')
                        raw_frame_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    except Exception as e:
                        logger.warning(f"Failed to encode raw frame to base64: {e}")
                        raw_frame_b64 = None

                    # Create raw image data package
                    raw_image_data = {
                        "event_id": master_correlation_id,  # Use master correlation ID
                        "grab_timestamp_ns": capture_timestamp_ns,  # When capture started
                        "capture_completed_ns": capture_completed_ns,  # When capture finished
                        "capture_duration_ms": capture_duration_ms,
                        "roi_used": raw_img_roi_used,
                        "frame_base64": raw_frame_b64
                    }

                    # Create frame package with all timing and correlation data
                    frame_package = {
                        "frame_for_ocr": frame.copy(),
                        "metadata": event_metadata,
                        "capture_completed_ns": capture_completed_ns, # Pass this for analysis
                        # Keep legacy fields for compatibility during transition
                        "master_correlation_id": master_correlation_id,
                        "capture_timestamp_ns": capture_timestamp_ns,
                        "raw_image_grab_ns": raw_image_grab_ns,  # T0: Precise image grab timestamp
                        "raw_image_data": raw_image_data
                    }

                    # NEW: Overwrite the latest frame in the mailbox and notify the worker.
                    with self._new_frame_condition:
                        self._latest_frame_package = frame_package
                        self._new_frame_condition.notify() # Ring the doorbell for the OCR worker.

                # --- THIS IS THE NEW, FAST "NO SLEEP" WAY ---
                # No sleep is used. The loop will now "spin" and capture frames
                # as fast as possible, flooding the queue with the most recent
                # screen data. This minimizes latency.
                # A tiny, optional sleep can yield the CPU if needed, but for
                # pure speed, we omit it.
                time.sleep(0.001) # Optional 1ms yield

            except Exception as e:
                logger.error(f"Error in capture loop: {e}", exc_info=True)
                
                # Publish capture loop failure event
                error_context = {
                    "capture_method": "live_screen",
                    "loop_iteration": getattr(self, '_capture_loop_iteration', 0),
                    "capture_active": self._capture_active,
                    "roi_coordinates": self._roi_coordinates,
                    "exception_type": type(e).__name__,
                    "thread_name": threading.current_thread().name
                }
                
                self._publish_ocr_error_via_pipe(
                    error_type="CAPTURE_LOOP_ERROR",
                    error_message=f"Screen capture loop encountered error: {e}",
                    error_context=error_context
                )
                
                # Sleep a bit longer after an error
                self._stop_capture_event.wait(timeout=0.5)

        self._capture_active = False
        logger.info("Screen capture loop finished.")

    @log_function_call('ocr_service')
    def _start_capture_thread(self):
        """Starts the screen capture thread if not already running."""
        if self._capture_thread and self._capture_thread.is_alive():
            logger.warning("Capture thread already running.")
            return
        
        try:
            self._stop_capture_event.clear()
            self._capture_thread = threading.Thread(target=self._capture_loop, name="CaptureThread", daemon=True)
            self._capture_thread.start()
            
            # Brief wait to check if thread started successfully
            import time
            time.sleep(0.1)
            if not self._capture_thread.is_alive():
                raise RuntimeError("Capture thread failed to start")
                
            logger.info("Screen capture thread initiated successfully.")
            
            # Publish OCR capture startup event
            # New correlation logger code
            if self._correlation_logger:
                try:
                    timestamp_ns = time.perf_counter_ns()
                    log_payload = {
                        "thread_name": "CaptureThread",
                        "capture_method": "live_screen",
                        "roi_coordinates": self._roi_coordinates,
                        "interval_seconds": getattr(self._config, 'ocr_capture_interval_seconds', 0.25),
                        "timestamp_ns": timestamp_ns,
                        "correlation_id": str(uuid.uuid4())
                    }
                    self._correlation_logger.log_event(
                        source_component="OCRService",
                        event_name="OcrCaptureStarted",
                        payload=log_payload
                    )
                except Exception as log_error:
                    logger.debug(f"Correlation logger error (non-critical): {log_error}")
            
        except Exception as e:
            logger.error(f"Failed to start capture thread: {e}", exc_info=True)
            
            # Publish thread startup failure event
            error_context = {
                "thread_name": "CaptureThread",
                "startup_failure": True,
                "capture_method": "live_screen",
                "roi_coordinates": self._roi_coordinates,
                "exception_type": type(e).__name__
            }
            
            # Publish capture failure event
            # New correlation logger code
            if self._correlation_logger:
                try:
                    timestamp_ns = time.perf_counter_ns()
                    log_payload = {
                        **error_context,
                        "error_message": str(e),
                        "timestamp_ns": timestamp_ns,
                        "correlation_id": str(uuid.uuid4())
                    }
                    self._correlation_logger.log_event(
                        source_component="OCRService",
                        event_name="OcrCaptureFailure",
                        payload=log_payload
                    )
                except Exception as log_error:
                    self.logger.error(f"Failed to log OCR capture failure: {log_error}")
            
            self._publish_ocr_error_via_pipe(
                error_type="CAPTURE_THREAD_STARTUP_FAILURE",
                error_message=f"Failed to start screen capture thread: {e}",
                error_context=error_context
            )
            
            # Clean up failed thread reference
            self._capture_thread = None

    @log_function_call('ocr_service')
    def _stop_capture_thread(self):
        """Stops the screen capture thread."""
        if not self._capture_thread or not self._capture_thread.is_alive():
            logger.info("Capture thread not running.")
            return
            
        try:
            logger.info("Stopping screen capture thread...")
            self._stop_capture_event.set()
            self._capture_thread.join(timeout=1.5) # Wait a bit longer
            
            if self._capture_thread.is_alive():
                logger.warning("Capture thread did not stop gracefully.")
                
                # Publish thread shutdown failure event
                error_context = {
                    "thread_name": "CaptureThread", 
                    "shutdown_failure": True,
                    "timeout_seconds": 1.5,
                    "thread_still_alive": True,
                    "capture_method": "live_screen"
                }
                
                self._publish_ocr_error_via_pipe(
                    error_type="CAPTURE_THREAD_SHUTDOWN_FAILURE",
                    error_message="Screen capture thread did not stop gracefully within timeout",
                    error_context=error_context
                )
            else:
                logger.info("Screen capture thread stopped successfully.")
                
        except Exception as e:
            logger.error(f"Error stopping capture thread: {e}", exc_info=True)
            
            # Publish thread shutdown error event
            error_context = {
                "thread_name": "CaptureThread",
                "shutdown_error": True,
                "exception_type": type(e).__name__,
                "capture_method": "live_screen"
            }
            
            self._publish_ocr_error_via_pipe(
                error_type="CAPTURE_THREAD_SHUTDOWN_ERROR", 
                error_message=f"Error during screen capture thread shutdown: {e}",
                error_context=error_context
            )
        finally:
            # Always clean up regardless of errors
            self._capture_thread = None
            self._capture_active = False
        logger.info("Screen capture thread stopped.")

    # --- OCR Processing ---

    # Text processing has been moved to the ProcessedOCRHandler

    @log_function_call('ocr_service')
    def _ocr_loop(self):
        """Target function for the OCR processing thread."""
        logger.info("OCR processing loop started.")

        # CLEAN ARCHITECTURE: OCR service starts immediately without trading dependencies
        # The isolated OCR process should not wait for trading services to initialize
        logger.info("OCRThread starting immediately - no trading dependencies in isolated OCR process.")

        frame_counter = 0
        last_confidence = 0.0
        last_status_log_time = time.perf_counter()  # For periodic status logging - use golden timestamp

        while not self._stop_ocr_event.is_set():
            # CRITICAL MEMORY LEAK FIX: Disable fine-grained performance tracking in main loop
            # Only enable for periodic sampling to prevent memory exhaustion
            perf_tracking_enabled = False  # Disabled to prevent memory leak
            perf_timestamps = None

            # Enable performance tracking only every 100 frames for sampling
            if frame_counter % 100 == 0:
                perf_tracking_enabled = is_performance_tracking_enabled()
                if perf_tracking_enabled:
                    perf_timestamps = create_timestamp_dict()

            frame = None
            raw_image_grab_details = None
            try:
                # CRITICAL FIX: Check stop event before blocking operations
                if self._stop_ocr_event.is_set():
                    break

                # Get frame package from queue with timeout - measure queue performance
                queue_start_time = time.time()
                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.frame_queue_get_start')
                frame_package = None # Ensure it's defined outside the lock
                with self._new_frame_condition:
                    # Wait efficiently until a new frame is produced.
                    self._new_frame_condition.wait() 
                    # Once woken up, grab the latest frame and clear the mailbox.
                    if self._latest_frame_package:
                        frame_package = self._latest_frame_package
                        self._latest_frame_package = None

                # If we didn't get a frame (e.g., spurious wakeup), just continue.
                if not frame_package:
                    continue
                queue_end_time = time.time()
                queue_duration_ms = (queue_end_time - queue_start_time) * 1000

                # Check for queue timeout degradation
                if queue_duration_ms > self._queue_timeout_threshold_ms:
                    current_time = time.time()
                    if self._should_alert("queue_timeout", current_time):
                        performance_context = {
                            "frame_counter": frame_counter,
                            "queue_operation": "frame_queue_get",
                            "queue_duration_ms": queue_duration_ms,
                            "queue_threshold_ms": self._queue_timeout_threshold_ms,
                            "queue_timeout_factor": queue_duration_ms / self._queue_timeout_threshold_ms,
                            "queue_size": 1 if self._latest_frame_package else 0
                        }
                        
                        self._publish_ocr_performance_degradation_via_pipe(
                            degradation_type="QUEUE_TIMEOUT",
                            current_value=queue_duration_ms,
                            threshold_value=self._queue_timeout_threshold_ms,
                            performance_context=performance_context
                        )

                # Unpack the frame and pristine metadata from the capture thread
                if isinstance(frame_package, dict):
                    frame = frame_package.get("frame_for_ocr")
                    event_metadata = frame_package.get("metadata")
                    capture_completed_ns = frame_package.get("capture_completed_ns")
                    
                    if not event_metadata:
                        self.logger.error("FATAL: Frame package missing metadata. Skipping.")
                        
                        # Log critical metadata failure via CorrelationLogger
                        if self._correlation_logger:
                            try:
                                timestamp_ns = time.perf_counter_ns()
                                log_payload = {
                                    "critical_failure_type": "MISSING_METADATA",
                                    "frame_package_keys": list(frame_package.keys()) if isinstance(frame_package, dict) else [],
                                    "frame_counter": frame_counter,
                                    "timestamp_ns": timestamp_ns
                                }
                                self._correlation_logger.log_event(
                                    source_component="OCRService",
                                    event_name="OcrCriticalFailure",
                                    payload=log_payload
                                )
                            except Exception as log_error:
                                self.logger.error(f"Failed to log OCR metadata failure: {log_error}")
                        continue
                    
                    # Correctly retrieve the golden timestamp and correlation ID from metadata
                    correlation_id = event_metadata.get("correlationId")
                    # --- PRESERVATION OF GOLDEN TIMESTAMP ---
                    # This is the one, true T0, captured in the Python capture loop.
                    PYTHON_T0_ImageIngress_ns = event_metadata.get("timestamp_ns")

                else: # Legacy fallback
                    self.logger.warning("[LEGACY_FORMAT] Received raw frame. Instrumentation incomplete.")
                    continue

                # Set current frame context for golden timestamp tracking in status updates
                self._set_current_frame_context(PYTHON_T0_ImageIngress_ns, correlation_id)

                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.frame_queue_get_end')
                
                # Ancillary metric: Queue delay
                ocr_start_ns = time.perf_counter_ns()
                queue_delay_ms = (ocr_start_ns - capture_completed_ns) / 1_000_000
                if queue_delay_ms > 100:
                    self.logger.warning(f"[PERF] High queue delay: {queue_delay_ms:.1f}ms for CorrID: {correlation_id}")
                    
                    # Log high queue delay degradation via CorrelationLogger
                    if self._correlation_logger:
                        try:
                            timestamp_ns = time.perf_counter_ns()
                            log_payload = {
                                "performance_degradation_type": "HIGH_QUEUE_DELAY",
                                "queue_delay_ms": queue_delay_ms,
                                "threshold_ms": 100.0,
                                "degradation_factor": queue_delay_ms / 100.0,
                                "correlation_id": correlation_id,
                                "frame_counter": frame_counter,
                                "timestamp_ns": timestamp_ns
                            }
                            self._correlation_logger.log_event(
                                source_component="OCRService",
                                event_name="OcrPerformanceDegradation",
                                payload=log_payload
                            )
                        except Exception as log_error:
                            self.logger.error(f"Failed to log OCR queue delay degradation: {log_error}")
                

                # Debug timestamp info using logger instead of print
                if frame_counter % 100 == 0:  # Only log occasionally
                    logger.debug(f"Golden timestamp (T0): {PYTHON_T0_ImageIngress_ns}ns, CorrID: {correlation_id}")

                # --- ADD THIS CHECK ---
                if self._stop_ocr_event.is_set():
                    break # Exit loop immediately if stopped
                
                # CAPTURE CURRENT SETTINGS FOR ANALYSIS METADATA
                # Get thread-safe snapshot of current ROI and preprocessing parameters
                with self._lock:
                    current_roi_snapshot = list(self._roi_coordinates)
                    current_preprocessing_params = self._get_preprocessing_kwargs()
                # --- END ADDED CHECK ---

                # --- C++ OCR ACCELERATOR OR PYTHON FALLBACK ---
                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.preprocessing_start')
                
                # Initialize variables
                ocr_success = False
                raw_ocr_text = ""
                frame_preprocessed = None
                used_cpp_accelerator = False
                cpp_result = None  # Initialize C++ result for confidence access
                
                try:
                    # C++ OCR ACCELERATOR (MANDATORY)
                    if not CPP_ACCELERATOR_AVAILABLE:
                        raise RuntimeError(f"C++ OCR accelerator is required but not available: {CPP_ACCELERATOR_LOAD_STATUS}")
                    
                    print(f"[C++ ACCELERATOR] Using persistent C++ OCR engine for frame processing", flush=True)
                    
                    # --- C++ CALL ISOLATION TEST ---
                    cpp_call_start_ns = time.perf_counter_ns()

                    # FAST C++ OCR ACCELERATOR - Use persistent engine
                    cpp_result = self._cpp_engine.process_image(
                        frame,
                        self._upscale_factor,
                        self._force_black_text_on_white,
                        self._unsharp_strength,
                        self._threshold_block_size,
                        self._threshold_c,
                        self._red_boost,
                        self._green_boost,
                        getattr(self, '_apply_text_mask_cleaning', False),
                        getattr(self, '_text_mask_min_contour_area', 100),
                        getattr(self, '_text_mask_min_width', 5),
                        getattr(self, '_text_mask_min_height', 5),
                        getattr(self, '_enhance_small_symbols', False),
                        getattr(self, '_symbol_max_height', 20),
                        getattr(self, '_period_comma_ratio', (0.5, 1.5)),
                        getattr(self, '_period_comma_radius', 3),
                        getattr(self, '_hyphen_min_ratio', 2.0),
                        getattr(self, '_hyphen_min_height', 3)
                    )

                    cpp_call_end_ns = time.perf_counter_ns()
                    cpp_call_duration_ms = (cpp_call_end_ns - cpp_call_start_ns) / 1_000_000
                    # --- END OF ISOLATION TEST ---

                    # --- AGGRESSIVE LOGGING OF THE RESULT ---
                    self.logger.critical(
                        f"[CPP_ISOLATION_TEST] C++ 'process_image' call took: {cpp_call_duration_ms:.2f}ms. "
                        f"CorrID: {correlation_id[:8]}"
                    )
                    
                    # --- Print C++ timestamps immediately after receiving them ---
                    print(
                        f"[C++ TIMESTAMPS] "
                        f"t0_entry={cpp_result.get('t0_entry_ns')} | "
                        f"t1_preproc={cpp_result.get('t1_preproc_done_ns')} | "
                        f"t2_contours={cpp_result.get('t2_contours_done_ns')} | "
                        f"t3_tess_start={cpp_result.get('t3_tess_start_ns')} | "
                        f"t4_tess_done={cpp_result.get('t4_tess_done_ns')}",
                        flush=True
                    )
                    
                    # Extract results from C++ accelerator
                    raw_ocr_text = cpp_result["text"]
                    
                    # Create dictionary of C++ timestamps for pipeline
                    cxx_timestamps = {
                        't0_entry': cpp_result.get('t0_entry_ns'),
                        't1_preproc_done': cpp_result.get('t1_preproc_done_ns'),
                        't2_contours_done': cpp_result.get('t2_contours_done_ns'),
                        't3_tess_start': cpp_result.get('t3_tess_start_ns'),
                        't4_tess_done': cpp_result.get('t4_tess_done_ns'),
                    }
                    
                    # --- RECONCILE C++ and PYTHON TIMESTAMPS ---
                    # The C++ module gives us the intermediate milestones.
                    T1_ImageProcessing_ns = cpp_result.get("t1_preproc_done_ns")
                    T2_TesseractOutput_ns = cpp_result.get("t4_tess_done_ns")
                    
                    # Keep legacy fields for compatibility
                    image_processing_finish_ns = T1_ImageProcessing_ns
                    tesseract_finish_ns = T2_TesseractOutput_ns
                    
                    # 🔬 SCIENTIFIC TIMING MEASUREMENT - Log every frame for precise analysis
                    try:
                        t0_ns = cpp_result["t0_entry_ns"]
                        t1_ns = cpp_result["t1_preproc_done_ns"] 
                        t2_ns = cpp_result["t2_contours_done_ns"]
                        t3_ns = cpp_result["t3_tess_start_ns"]
                        t4_ns = cpp_result["t4_tess_done_ns"]
                        
                        # Calculate individual phase durations (in milliseconds)
                        opencv_preprocessing_ms = (t1_ns - t0_ns) / 1_000_000
                        opencv_contours_ms = (t2_ns - t1_ns) / 1_000_000
                        tesseract_setup_ms = (t3_ns - t2_ns) / 1_000_000
                        tesseract_ocr_ms = (t4_ns - t3_ns) / 1_000_000
                        total_cpp_ms = (t4_ns - t0_ns) / 1_000_000
                        
                        # 📊 SCIENTIFIC LOG: Every frame timing breakdown
                        print(f"[OCR_TIMING] CorrID:{correlation_id[:8]} | "
                              f"OpenCV_Preproc:{opencv_preprocessing_ms:.2f}ms | "
                              f"OpenCV_Contours:{opencv_contours_ms:.2f}ms | "
                              f"Tesseract_Setup:{tesseract_setup_ms:.2f}ms | "
                              f"Tesseract_OCR:{tesseract_ocr_ms:.2f}ms | "
                              f"Total_C++:{total_cpp_ms:.2f}ms")
                    except KeyError as e:
                        self.logger.error(f"[OCR_TIMING] Missing timing data from C++ module: {e}")
                        self.logger.error(f"[OCR_TIMING] Available keys in cpp_result: {list(cpp_result.keys())}")
                        
                        # Log C++ module timing failure via CorrelationLogger
                        if self._correlation_logger:
                            try:
                                timestamp_ns = time.perf_counter_ns()
                                log_payload = {
                                    "critical_failure_type": "CPP_TIMING_DATA_MISSING",
                                    "missing_key": str(e),
                                    "available_keys": list(cpp_result.keys()) if cpp_result else [],
                                    "frame_counter": frame_counter,
                                    "correlation_id": correlation_id,
                                    "timestamp_ns": timestamp_ns
                                }
                                self._correlation_logger.log_event(
                                    source_component="OCRService",
                                    event_name="OcrCriticalFailure",
                                    payload=log_payload
                                )
                            except Exception as log_error:
                                self.logger.error(f"Failed to log OCR C++ timing failure: {log_error}")
                    except Exception as e:
                        self.logger.error(f"[OCR_TIMING] Error calculating timing metrics: {e}")
                        
                        # Log C++ module calculation failure via CorrelationLogger
                        if self._correlation_logger:
                            try:
                                timestamp_ns = time.perf_counter_ns()
                                log_payload = {
                                    "critical_failure_type": "CPP_TIMING_CALCULATION_ERROR",
                                    "error_message": str(e),
                                    "exception_type": type(e).__name__,
                                    "frame_counter": frame_counter,
                                    "correlation_id": correlation_id,
                                    "timestamp_ns": timestamp_ns
                                }
                                self._correlation_logger.log_event(
                                    source_component="OCRService",
                                    event_name="OcrCriticalFailure",
                                    payload=log_payload
                                )
                            except Exception as log_error:
                                self.logger.error(f"Failed to log OCR C++ calculation failure: {log_error}")
                    
                    ocr_success = True
                    used_cpp_accelerator = True
                    
                    # Use original frame for preview (C++ already processed internally)
                    frame_preprocessed = frame.copy()

                    if perf_tracking_enabled and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, 'ocr_service.preprocessing_end')

                    if self._stop_ocr_event.is_set(): break # Check stop event again

                    # --- Put processed frame onto preview queue ---
                    if frame_preprocessed is not None:
                        try:
                            frame_for_preview = frame_preprocessed.copy()

                            if self._preview_output_queue.full():
                                try:
                                    self._preview_output_queue.get_nowait() # Make space if full
                                except QueueEmpty:
                                    pass
                            self._preview_output_queue.put(frame_for_preview, block=False, timeout=0.05)
                            self.logger.debug("OCRService: Put processed frame onto _preview_output_queue.")
                        except QueueFull:
                            self.logger.debug("OCRService: _preview_output_queue was full, frame for preview dropped.")
                        except Exception as e_prev_q:
                            self.logger.error(f"OCRService: Error putting frame on _preview_output_queue: {e_prev_q}")
                    
                    # Log processing details occasionally
                    if frame_counter % 50 == 0:
                        accelerator_type = "C++" if used_cpp_accelerator else "Python"
                        if hasattr(self, '_enhance_small_symbols'):
                            logger.debug(f"Applied {accelerator_type} OCR processing (upscale={self._upscale_factor}, "
                                        f"invert={self._force_black_text_on_white}, sharp={self._unsharp_strength}, "
                                        f"block={self._threshold_block_size}, C={self._threshold_c}, "
                                        f"red_boost={self._red_boost}, green_boost={self._green_boost}, "
                                        f"text_mask_cleaning={getattr(self, '_apply_text_mask_cleaning', False)}, "
                                        f"enhance_symbols={getattr(self, '_enhance_small_symbols', False)})")
                        else:
                            logger.debug(f"Applied {accelerator_type} OCR processing (upscale={self._upscale_factor}, "
                                        f"invert={self._force_black_text_on_white}, sharp={self._unsharp_strength}, "
                                        f"block={self._threshold_block_size}, C={self._threshold_c})")

                except Exception as processing_err:
                    logger.error(f"Error in OCR processing: {processing_err}", exc_info=True)
                    if perf_tracking_enabled and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, 'ocr_service.preprocessing_error')
                    # Set fallback timestamps
                    T1_ImageProcessing_ns = time.perf_counter_ns()
                    T2_TesseractOutput_ns = time.perf_counter_ns()
                    image_processing_finish_ns = T1_ImageProcessing_ns
                    tesseract_finish_ns = T2_TesseractOutput_ns
                    if perf_tracking_enabled and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, 'ocr_service.tesseract_ocr_error')
                    
                    # Publish OCR processing error
                    error_context = {
                        "frame_counter": frame_counter,
                        "frame_processing_time_ms": (time.time() - ocr_start_ns / 1_000_000_000) * 1000 if 'ocr_start_ns' in locals() else None,
                        "preprocessing_applied": True,
                        "accelerator_used": "C++" if used_cpp_accelerator else "Python"
                    }
                    if hasattr(self, '_publish_ocr_error_via_pipe'):
                        self._publish_ocr_error_via_pipe(
                            error_type="OCR_PROCESSING_ERROR",
                            error_message=f"OCR processing failed: {processing_err}",
                            error_context=error_context,
                            exception_obj=processing_err
                        )

                # --- Calculate Critical Timing (Safely) ---
                if perf_tracking_enabled and perf_timestamps is not None and 'ocr_service.tesseract_ocr_end' in perf_timestamps and 'ocr_service.frame_queue_get_start' in perf_timestamps:
                    try:
                        grab_to_ocr_time = perf_timestamps['ocr_service.tesseract_ocr_end'] - perf_timestamps['ocr_service.frame_queue_get_start']
                        if frame_counter % 50 == 0: # Log less frequently
                            logger.debug(f"Timing: FrameQueue Get to OCR End: {grab_to_ocr_time*1000:.1f}ms")
                    except KeyError: # Should not happen with the check above, but for extra safety
                         logger.warning("KeyError calculating grab_to_ocr_time despite checks.")
                    except Exception as calc_err:
                         logger.warning(f"Error calculating grab_to_ocr_time: {calc_err}")
                elif perf_tracking_enabled and perf_timestamps is not None and 'ocr_service.tesseract_ocr_error' in perf_timestamps:
                     logger.warning("Cannot calculate grab_to_ocr_time due to OCR error.")


                # --- No Text Processing in OCR Service ---
                # Text processing is now handled by ProcessedOCRHandler
                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.text_processing_skipped')

                # --- ADD THIS CHECK (Optional but good practice) ---
                if self._stop_ocr_event.is_set():
                    break # Exit loop immediately if stopped
                # --- END ADDED CHECK ---

                # --- Calculate Confidence (Conditional, only if OCR succeeded) ---
                frame_counter += 1
                avg_conf = last_confidence
                
                # Periodic status log (every 5 seconds at 20fps = ~100 frames)
                current_time = time.perf_counter()
                if current_time - last_status_log_time >= 5.0:  # Every 5 seconds
                    self.logger.info(f"OCR processing active: frame {frame_counter}, confidence: {avg_conf:.1f}%")
                    last_status_log_time = current_time
                if ocr_success and frame_counter % self._confidence_check_frequency == 0 :
                    try:
                        if perf_tracking_enabled and perf_timestamps is not None:
                            add_timestamp(perf_timestamps, 'ocr_service.confidence_calc_start')
                        # Use confidence from C++ accelerator (already calculated)
                        if ocr_success and used_cpp_accelerator and cpp_result and 'confidence' in cpp_result:
                            avg_conf = cpp_result['confidence']
                        else:
                            avg_conf = last_confidence  # Use last known confidence
                        last_confidence = avg_conf
                        if perf_tracking_enabled and perf_timestamps is not None:
                            add_timestamp(perf_timestamps, 'ocr_service.confidence_calc_end')
                        if frame_counter % 50 == 0: logger.debug(f"Confidence from C++ accelerator: {avg_conf:.2f}% (Frame {frame_counter})")
                        
                        # Check for confidence degradation
                        self._check_confidence_degradation(avg_conf, frame_counter)
                    except Exception as conf_err:
                         logging.error(f"Error calculating confidence: {conf_err}")
                         if perf_tracking_enabled and perf_timestamps is not None:
                             add_timestamp(perf_timestamps, 'ocr_service.confidence_calc_error')
                         
                         # Publish confidence calculation error
                         error_context = {
                             "frame_counter": frame_counter,
                             "last_confidence": getattr(self, 'last_confidence', 'unknown'),
                             "confidence_calculation_method": "cpp_accelerator"
                         }
                         self._publish_ocr_error_via_pipe(
                             error_type="CONFIDENCE_CALCULATION_ERROR",
                             error_message=f"Error calculating confidence: {conf_err}",
                             error_context=error_context,
                             exception_obj=conf_err
                         )
                else:
                     # Reuse last confidence if not calculated this frame
                     avg_conf = last_confidence


                # --- Prepare data package for observer ---
                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.notify_start')

                # MILESTONE T3: Formatted Output Ready
                T3_FormattedOutput_ns = time.perf_counter_ns()
                
                # Calculate total OCR processing time using the clean T0-T3 model
                ocr_processing_time_ms = (T3_FormattedOutput_ns - T2_TesseractOutput_ns) / 1_000_000
                total_time_since_capture_ms = (T3_FormattedOutput_ns - PYTHON_T0_ImageIngress_ns) / 1_000_000
                
                # Log OCR processing complete with timing (DEBUG level to reduce spam)
                self.logger.debug(f"[OCR_ORIGIN] OCR processing complete for CorrID: {correlation_id}, "
                               f"T2->T3 time: {ocr_processing_time_ms:.1f}ms, "
                               f"T0->T3 total: {total_time_since_capture_ms:.1f}ms")

                # Encode processed frame to base64 for streaming using PIL
                processed_frame_b64 = None
                if frame is not None:
                    try:
                        from PIL import Image
                        import io
                        pil_image = Image.fromarray(frame)
                        buffer = io.BytesIO()
                        pil_image.save(buffer, format='PNG')
                        processed_frame_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                    except Exception as e:
                        logger.warning(f"Failed to encode processed frame to base64: {e}")

                # Create the final OCRParsedData object using world-class metadata/payload separation
                # DEAD CODE REMOVED: frame_timestamp and raw_ocr_processing_finish_timestamp are gone.
                ocr_data_package_for_app = OCRParsedData(
                    # --- METADATA BLOCK: All tracing, routing, and correlation data ---
                    metadata=event_metadata,  # Pass the original, pristine metadata block forward
                    
                    # --- PAYLOAD: Business Logic Data ---
                    
                    # Core OCR Results
                    overall_confidence=avg_conf,
                    snapshots={},
                    full_raw_text=raw_ocr_text,
                    
                    # --- CRITICAL FIX: USE THE PRESERVED PYTHON T0 ---
                    T0_ImageIngress_ns=PYTHON_T0_ImageIngress_ns,  # Use the preserved golden timestamp
                    T1_ImageProcessing_ns=T1_ImageProcessing_ns,
                    T2_TesseractOutput_ns=T2_TesseractOutput_ns,
                    T3_FormattedOutput_ns=T3_FormattedOutput_ns,
                    
                    # C++ Accelerator timestamps
                    cxx_milestones_ns=cxx_timestamps,
                    
                    # Performance and Debug Data
                    ocr_process_perf_timestamps=perf_timestamps,
                    processed_frame_base64=processed_frame_b64,
                    
                    # Analysis Metadata
                    roi_used_for_capture=current_roi_snapshot,
                    preprocessing_params_used=current_preprocessing_params,
                    current_roi_for_ocr=list(self._roi_coordinates)
                )

                # Debug print for OCR service output
                if hasattr(self._config, 'debug_ocr_service') and self._config.debug_ocr_service:
                    self.logger.debug("\n--- [PIPELINE STEP 1: OCRService Output] ---")
                    self.logger.debug(f"Type: {type(ocr_data_package_for_app)}")
                    # Print keys and types for structure check
                    self.logger.debug(f"Keys: {str({k: type(v).__name__ for k, v in ocr_data_package_for_app.items()})}")
                    # Print partial text for verification
                    self.logger.debug(f"Raw Text (Start): {repr(ocr_data_package_for_app.get('raw_text', '')[:100])}...")
                    self.logger.debug(f"Success: {ocr_data_package_for_app.get('ocr_success')}, Confidence: {ocr_data_package_for_app.get('confidence'):.2f}")
                    # Print preprocessing info
                    preproc_time = 0
                    if perf_tracking_enabled and perf_timestamps is not None and 'durations' in locals():
                        preproc_time = durations.get('ocr_service.preprocessing_start_to_ocr_service.preprocessing_end', 0)*1000
                    self.logger.debug(f"Preprocessing: Applied (took {preproc_time:.1f}ms)")
                    self.logger.debug("-" * 40)

                # This is the ONLY way OCRService should output its results:
                observer_instance = self._observer() if self._observer else None
                if observer_instance and hasattr(observer_instance, 'on_ocr_result'):
                    try:
                        # Log correlation ID when passing to observer (DEBUG level to reduce spam)
                        self.logger.debug(f"[OCR_ORIGIN] Notifying observer with CorrID: {correlation_id}")
                        observer_instance.on_ocr_result(ocr_data_package_for_app)
                        self.logger.debug("Notified observer with ocr_data_package_for_app.")
                        
                        # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T0-T3 milestone telemetry
                        # New correlation logger code
                        if self._correlation_logger:
                            try:
                                # Re-calculate latencies using the CORRECT T0
                                t0_to_t1_ms = (T1_ImageProcessing_ns - PYTHON_T0_ImageIngress_ns) / 1_000_000 if T1_ImageProcessing_ns and PYTHON_T0_ImageIngress_ns else None
                                t1_to_t2_ms = (T2_TesseractOutput_ns - T1_ImageProcessing_ns) / 1_000_000 if T1_ImageProcessing_ns and T2_TesseractOutput_ns else None
                                t2_to_t3_ms = (T3_FormattedOutput_ns - T2_TesseractOutput_ns) / 1_000_000 if T2_TesseractOutput_ns else None
                                t0_to_t3_total_ms = (T3_FormattedOutput_ns - PYTHON_T0_ImageIngress_ns) / 1_000_000 if PYTHON_T0_ImageIngress_ns else None

                                if t0_to_t3_total_ms is not None:
                                    self.logger.critical(f"[PID:{os.getpid()}] T0->T3 Corrected Total Latency: {t0_to_t3_total_ms:.2f}ms")
                                
                                log_payload = {
                                    # --- CRITICAL FIX: USE THE PRESERVED PYTHON T0 IN THE LOG ---
                                    "T0_ImageIngress_ns": PYTHON_T0_ImageIngress_ns,
                                    "T1_ImageProcessing_ns": T1_ImageProcessing_ns,
                                    "T2_TesseractOutput_ns": T2_TesseractOutput_ns,
                                    "T3_FormattedOutput_ns": T3_FormattedOutput_ns,
                                    
                                    # Correctly calculated latencies
                                    "T0_to_T1_ms": t0_to_t1_ms,
                                    "T1_to_T2_ms": t1_to_t2_ms,
                                    "T2_to_T3_ms": t2_to_t3_ms,
                                    "T0_to_T3_total_ms": t0_to_t3_total_ms,
                                    
                                    # OCR result summary (not the full data)
                                    "ocr_text_length": len(ocr_data_package_for_app.get("ocr_text", "")),
                                    "overall_confidence": ocr_data_package_for_app.get("overall_confidence", 0.0),
                                    
                                    # Additional context
                                    "roi_coordinates": self._roi_coordinates,
                                    "confidence_check_count": getattr(self, '_confidence_check_count', 0),
                                    "processing_params": {
                                        "confidence_check_enabled": self._confidence_check_enabled,
                                        "roi_optimization_mode": self._roi_optimization_mode,
                                        "preprocessing_enabled": self._config.preprocessing_config.apply_preprocessing
                                    },
                                    "origin_timestamp_ns": PYTHON_T0_ImageIngress_ns,
                                    "correlation_id": correlation_id  # Pass for debugging
                                }
                                
                                self._correlation_logger.log_event(
                                    source_component="OCRService",
                                    event_name="OcrCaptureMilestones",
                                    payload=log_payload,
                                    stream_override="testrade:instrumentation:ocr-capture"
                                )
                            except Exception as log_error:
                                # Non-critical error - don't affect OCR processing
                                self.logger.debug(f"Correlation logger error (non-critical): {log_error}")
                        
                        # Clear frame context after successful processing
                        self._clear_current_frame_context()
                    except Exception as e_obs_call:
                        self.logger.error(f"Error calling observer.on_ocr_result: {e_obs_call}", exc_info=True)
                        
                        # Log observer callback failure via CorrelationLogger
                        if self._correlation_logger:
                            try:
                                timestamp_ns = time.perf_counter_ns()
                                log_payload = {
                                    "critical_failure_type": "OBSERVER_CALLBACK_FAILURE",
                                    "error_message": str(e_obs_call),
                                    "exception_type": type(e_obs_call).__name__,
                                    "frame_counter": frame_counter,
                                    "correlation_id": correlation_id,
                                    "timestamp_ns": timestamp_ns
                                }
                                self._correlation_logger.log_event(
                                    source_component="OCRService",
                                    event_name="OcrCriticalFailure",
                                    payload=log_payload
                                )
                            except Exception as log_error:
                                self.logger.error(f"Failed to log OCR observer failure: {log_error}")
                        
                        # Clear frame context even on observer error
                        self._clear_current_frame_context()
                # ... (ensure no other result submission paths exist) ...

                if perf_tracking_enabled and perf_timestamps is not None:
                    add_timestamp(perf_timestamps, 'ocr_service.notified_observer')

                # --- Performance Stats & Finalization ---
                try:
                    if perf_tracking_enabled and perf_timestamps is not None:
                        add_timestamp(perf_timestamps, 'ocr_service.ocr_loop_end')
                        # Calculate durations safely using .get() with default
                        durations = calculate_durations(perf_timestamps)
                        add_to_stats(perf_timestamps)

                        # Send aggregated metrics to main PerformanceBenchmarker via finalize_performance_scope
                        try:
                            from utils.performance_tracker import finalize_performance_scope, capture_metric_with_benchmarker

                            # CRITICAL FIX: Send key metrics to main PerformanceBenchmarker (every 100 frames to prevent memory leak)
                            if frame_counter % 100 == 0:
                                # Use finalize_performance_scope with the full performance dictionary
                                finalize_performance_scope(perf_timestamps, 'ocr_process')

                                # Also send individual metrics via capture_metric_with_benchmarker
                                preproc_duration_ms = durations.get('ocr_service.preprocessing_start_to_ocr_service.preprocessing_end', 0) * 1000
                                ocr_duration_ms = durations.get('ocr_service.tesseract_ocr_start_to_ocr_service.tesseract_ocr_end', 0) * 1000
                                total_duration_ms = (perf_timestamps.get('ocr_service.ocr_loop_end', 0) - perf_timestamps.get('ocr_service.frame_queue_get_start', 0)) * 1000

                                capture_metric_with_benchmarker('ocr_process.opencv_time_ms', preproc_duration_ms)
                                capture_metric_with_benchmarker('ocr_process.tesseract_time_ms', ocr_duration_ms)
                                capture_metric_with_benchmarker('ocr_process.total_processing_time_ms', total_duration_ms)
                                
                                # Check for performance degradation
                                self._check_processing_performance_degradation(
                                    total_duration_ms, preproc_duration_ms, ocr_duration_ms, frame_counter
                                )

                        except Exception as e_finalize:
                            logger.debug(f"Error finalizing performance scope and capturing metrics: {e_finalize}")

                        # Debug logging (less frequent)
                        if frame_counter % 50 == 0:
                            total_duration = (perf_timestamps.get('ocr_service.ocr_loop_end', 0) - perf_timestamps.get('ocr_service.frame_queue_get_start', 0))*1000
                            preproc_duration = durations.get('ocr_service.preprocessing_start_to_ocr_service.preprocessing_end', 0)*1000
                            ocr_duration = durations.get('ocr_service.tesseract_ocr_start_to_ocr_service.tesseract_ocr_end', 0)*1000
                            notify_duration = durations.get('ocr_service.notify_start_to_ocr_service.notified_observer', 0)*1000

                            logger.debug(f"OCR Loop Perf Summary: Total={total_duration:.1f}ms, "
                                         f"Preproc={preproc_duration:.1f}ms, "
                                         f"OCR={ocr_duration:.1f}ms, "
                                         f"Notify={notify_duration:.1f}ms")
                except Exception as e: logger.warning(f"Error calculating performance: {e}")

            except QueueEmpty:
                time.sleep(0.05)
                continue
            except Exception as e:
                # --- THIS IS THE BLOCK THAT CATCHES ERRORS LIKE THE KEYERROR ---
                # Log the exception that occurred within the main try block
                logging.exception(f"Error in OCR processing loop: {e}")
                # Clear frame context on any OCR processing error
                self._clear_current_frame_context()
                # Ensure loop end timestamp exists even on error for total duration calculation attempt
                if perf_tracking_enabled and perf_timestamps is not None and 'ocr_service.ocr_loop_end' not in perf_timestamps:
                     add_timestamp(perf_timestamps, 'ocr_service.ocr_loop_error_exit')
                
                # Publish general OCR loop error
                error_context = {
                    "frame_counter": frame_counter if 'frame_counter' in locals() else 'unknown',
                    "loop_stage": "ocr_processing_main_loop",
                    "performance_tracking_enabled": perf_tracking_enabled,
                    "queue_size": 1 if hasattr(self, '_latest_frame_package') and self._latest_frame_package else 0
                }
                self._publish_ocr_error_via_pipe(
                    error_type="OCR_LOOP_ERROR",
                    error_message=f"Error in OCR processing loop: {e}",
                    error_context=error_context,
                    exception_obj=e
                )
                
                time.sleep(0.2) # Sleep longer after an error

        logger.info("OCR processing loop finished.")

    @log_function_call('ocr_service')
    def start_ocr(self):
        """Starts the OCR processing thread."""
        self.logger.debug(f"start_ocr called, _capture_active = {self._capture_active}")
        if not self._capture_active:
             logger.warning("Capture thread must be running to start OCR.")
             self._notify_status("Error: Cannot start OCR, capture inactive.")
             self.logger.warning("Cannot start OCR - capture not active!")
             return
        if self._ocr_active:
            logger.warning("OCR thread already running.")
            return

        logger.info("Starting OCR processing...")
        self._stop_ocr_event.clear()
        self._ocr_thread = threading.Thread(target=self._ocr_loop, name="OCRThread", daemon=True)
        self._ocr_active = True # Set flag before starting thread
        self._ocr_thread.start()
        self._notify_status("OCR Started")
        
        # Publish OCR started status event
        status_context = {
            "capture_interval_seconds": getattr(self._config, 'ocr_capture_interval_seconds', 'unknown'),
            "tesseract_cmd": getattr(self, '_tesseract_cmd', 'unknown'),
            "roi_coordinates": getattr(self, '_roi_coordinates', 'unknown'),
            "video_mode": False,
            "video_file": None
        }
        self._publish_ocr_status_via_pipe(
            status_event="OCR_STARTED",
            status_message="OCR processing started successfully",
            status_context=status_context
        )
        

    @log_function_call('ocr_service')
    def stop_ocr(self):
        """Stops the OCR processing thread with enhanced resource cleanup."""
        if not self._ocr_active:
            logger.info("OCR not running.")
            return

        logger.info("Stopping OCR processing...")
        self._stop_ocr_event.set()

        # CRITICAL FIX: Non-blocking shutdown to prevent hanging
        if self._ocr_thread and self._ocr_thread.is_alive():
            logger.info("Waiting for OCR thread to stop gracefully...")
            self._ocr_thread.join(timeout=1.0)  # Reduced timeout to 1 second
            if self._ocr_thread.is_alive():
                logger.warning("OCR thread did not stop gracefully within 1 second - forcing shutdown")
                # Don't wait indefinitely - mark as inactive and continue
            else:
                logger.info("OCR thread stopped gracefully")

        self._ocr_thread = None
        self._ocr_active = False

        # CRITICAL MEMORY LEAK FIX: Enhanced resource cleanup
        try:
            # NEW: Ensure the OCR thread wakes up to see the shutdown signal.
            with self._new_frame_condition:
                self._new_frame_condition.notify()

            # Clear preview output queue
            while not self._preview_output_queue.empty():
                try:
                    self._preview_output_queue.get_nowait()
                except QueueEmpty:
                    break

            logger.debug("OCR Service: Cleared frame queues during stop")

        except Exception as e:
            logger.warning(f"Error during OCR resource cleanup: {e}")

        logger.info("OCR processing stopped.")
        self._notify_status("OCR Stopped")
        
        # Publish OCR stopped status event
        status_context = {
            "stop_reason": "manual_stop",
            "final_roi_coordinates": getattr(self, '_roi_coordinates', 'unknown'),
            "video_mode": False
        }
        self._publish_ocr_status_via_pipe(
            status_event="OCR_STOPPED",
            status_message="OCR processing stopped",
            status_context=status_context
        )
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        self._publish_service_health_status(
            "STOPPED",
            "OCR Service stopped and cleaned up resources",
            {
                "service_name": "OCRService",
                "ocr_active": False,
                "capture_active": self._capture_active,
                "frame_queues_cleared": True,
                "stop_reason": "manual_stop",
                "health_status": "STOPPED",
                "shutdown_timestamp": time.time()
            }
        )

    # --- Video Recording ---

    @log_function_call('ocr_service')
    def _recording_loop(self, video_path: str, width: int, height: int, fps: float):
        """Target function for the video recording thread."""
        logger.info(f"Recording loop started. Writing to {video_path}")
        frames_written = 0
        writer = None # Initialize writer inside the thread's scope

        try:
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            writer = cv2.VideoWriter(video_path, fourcc, fps, (width, height))
            if not writer.isOpened():
                logger.error(f"Failed to open VideoWriter for: {video_path}")
                self._notify_status(f"Error: Failed recording to {os.path.basename(video_path)}")
                self._recording_active = False # Ensure state is correct
                return # Exit thread if writer failed

            # Assign to instance variable ONLY if successfully opened
            self._video_writer = writer

            while not self._stop_recording_event.is_set():
                frame = None
                try:
                    # Get frame from mailbox
                    frame = None
                    with self._new_frame_condition:
                        self._new_frame_condition.wait(timeout=0.2)
                        if self._latest_frame_package:
                            frame = self._latest_frame_package
                except QueueEmpty:
                    continue # No frame, check stop event

                if frame is not None:
                    try:
                        writer.write(frame)
                        frames_written += 1
                    except Exception as e:
                        logger.error(f"Error writing frame during recording: {e}")
                        self._notify_status("Error: Recording failed during write.")
                        self._stop_recording_event.set() # Stop recording on error
                        break # Exit loop

        except Exception as e:
             logger.exception(f"Error initializing or during recording loop: {e}")
             self._notify_status("Error: Recording initialization failed.")
        finally:
            if writer is not None:
                try:
                    writer.release()
                    logger.info(f"VideoWriter released. Total frames written: {frames_written}")
                except Exception as e:
                    logger.error(f"Error releasing VideoWriter: {e}")
            self._video_writer = None # Clear instance variable
            self._recording_active = False # Ensure flag is false on exit
            logger.info("Recording loop finished.")
            # Notify observer AFTER loop finishes and active flag is false
            if self._stop_recording_event.is_set(): # Check if stopped intentionally or by error
                 if frames_written > 0 : # Avoid "stopped" message if it never really started
                    self._notify_status("Recording Stopped")
                    
                    # Publish recording stopped status event
                    status_context = {
                        "frames_written": frames_written,
                        "stop_reason": "manual_stop",
                        "recording_duration": "unknown"  # Could be calculated if start time was tracked
                    }
                    self._publish_ocr_status_via_pipe(
                        status_event="RECORDING_STOPPED",
                        status_message=f"Video recording stopped. {frames_written} frames written.",
                        status_context=status_context
                    )


    @log_function_call('ocr_service')
    def start_recording(self, output_dir: str = None, fps: float = 15.0):
        """Starts video recording."""
        if not self._capture_active:
             logger.warning("Capture thread must be running to start recording.")
             self._notify_status("Error: Cannot record, capture inactive.")
             return
        if self._recording_active:
            logger.warning("Recording already active.")
            return

        logger.info("Starting recording...")
        try:
            with self._lock:
                 x1, y1, x2, y2 = self._roi_coordinates
            width, height = x2 - x1, y2 - y1
            if width <= 0 or height <= 0:
                logger.error(f"Invalid ROI dimensions for recording: W={width}, H={height}")
                self._notify_status("Error: Invalid ROI for recording.")
                return

            log_directory = output_dir or VIDEO_DIR # Default directory from root config
            os.makedirs(log_directory, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            video_filename = os.path.join(log_directory, f'recording_{timestamp}.avi')

            self._stop_recording_event.clear()
            # Pass necessary info to the thread
            self._recording_thread = threading.Thread(
                target=self._recording_loop,
                args=(video_filename, width, height, fps),
                name="RecordingThread",
                daemon=True
            )
            self._recording_active = True # Set flag before starting
            self._recording_thread.start()
            self._notify_status(f"Recording Started: {os.path.basename(video_filename)}")
            
            # Publish recording started status event
            status_context = {
                "video_filename": os.path.basename(video_filename),
                "full_path": video_filename,
                "fps": fps,
                "roi_dimensions": {"width": width, "height": height},
                "roi_coordinates": list(self._roi_coordinates),
                "output_directory": output_dir or VIDEO_DIR
            }
            self._publish_ocr_status_via_pipe(
                status_event="RECORDING_STARTED",
                status_message=f"Video recording started: {os.path.basename(video_filename)}",
                status_context=status_context
            )

        except Exception as e_rec:
            logger.exception(f"Error initiating recording start: {e_rec}")
            self._recording_active = False # Reset flag on error
            self._notify_status("Error: Recording failed to start.")


    @log_function_call('ocr_service')
    def stop_recording(self):
        """Stops video recording."""
        if not self._recording_active:
            logger.info("Recording not active.")
            return

        logger.info("Stopping recording...")
        self._stop_recording_event.set() # Signal the thread

        if self._recording_thread and self._recording_thread.is_alive():
            self._recording_thread.join(timeout=2.5) # Wait longer for file write/release
            if self._recording_thread.is_alive():
                logger.warning("Recording thread did not stop gracefully.")
        self._recording_thread = None
        # self._recording_active is set to False inside the loop's finally block
        # self._notify_status("Recording Stopped") # Also notified from loop finish

    # --- Control Methods ---

    @log_function_call('ocr_service')
    def set_roi(self, x1: int, y1: int, x2: int, y2: int):
        """Updates the Region of Interest."""
        # Basic validation
        min_size = 10

        # Ensure coordinates are within basic screen/positive bounds
        x1 = max(0, x1)
        y1 = max(0, y1)

        # Final check to ensure x1 < x2 and y1 < y2
        x2 = max(x1 + min_size, x2)
        y2 = max(y1 + min_size, y2)

        with self._lock:
            self._roi_coordinates = [x1, y1, x2, y2]
        logger.info(f"ROI updated to: {self._roi_coordinates}")
        self._notify_status(f"ROI Updated: {x1},{y1},{x2},{y2}")

        # Publish ROI updated status event
        status_context = {
            "new_roi_coordinates": [x1, y1, x2, y2],
            "roi_dimensions": {"width": x2 - x1, "height": y2 - y1},
            "update_source": "manual_roi_update"
        }
        self._publish_ocr_status_via_pipe(
            status_event="ROI_UPDATED",
            status_message=f"ROI updated to: {x1},{y1},{x2},{y2}",
            status_context=status_context
        )

        # Send ROI state update via pipe to ApplicationCore
        self._send_roi_state_update()

    def _send_roi_state_update(self):
        """Send ROI state update via pipe to ApplicationCore."""
        if not self._pipe_to_parent:
            logger.debug("No pipe connection available for ROI state update")
            return

        try:
            import time
            new_roi_coords = self.get_roi()

            roi_update_message = {
                "type": "roi_state_update",
                "data": {
                    "roi": new_roi_coords,
                    "source": "OCRProcessInternalUpdate",
                    "timestamp": time.time()
                }
            }

            self._pipe_to_parent.send(roi_update_message)
            logger.info(f"OCR_PROCESS: ROI successfully updated to {new_roi_coords}. Sending update via pipe.")

        except Exception as e:
            logger.error(f"Error sending ROI state update via pipe: {e}", exc_info=True)

    def _publish_ocr_error_via_pipe(self, error_type: str, error_message: str, 
                                   error_context: Dict[str, Any], 
                                   exception_obj: Optional[Exception] = None):
        """
        Publish OCR error events via pipe to parent process for Redis publishing.
        
        Args:
            error_type: Type of OCR error (e.g., 'TESSERACT_CONFIG_ERROR', 'OCR_PROCESSING_ERROR')
            error_message: Human-readable error message
            error_context: Additional context about the error
            exception_obj: The original exception object for stack trace
        """
        if not self._pipe_to_parent:
            return  # No pipe available
            
        try:
            import traceback
            
            # Create error event payload
            error_payload = {
                "error_type": error_type,
                "error_message": error_message,
                "error_context": error_context,
                "timestamp": time.time(),
                "component": "OCRService",
                "severity": "error"
            }
            
            # Add stack trace for debugging if exception provided
            if exception_obj:
                error_payload["exception_type"] = type(exception_obj).__name__
                error_payload["stack_trace"] = traceback.format_exc()
            
            # Create pipe message
            pipe_message = {
                "type": "ocr_error_event",
                "data": error_payload
            }
            
            # CRITICAL FIX: Don't let error reporting block the OCR process
            # Skip sending errors if it would block
            try:
                # Check if we can send without blocking
                if self._pipe_to_parent.poll(0):
                    # Pipe has data waiting (shouldn't happen on child side)
                    pass
                
                # Try to send, but don't block if pipe is full
                self._pipe_to_parent.send(pipe_message)
                logger.debug(f"OCR_PROCESS: Published {error_type} error via pipe")
            except Exception as send_error:
                # If we can't send the error, just log it locally and continue
                logger.warning(f"Could not send {error_type} error via pipe: {send_error}")
            
        except Exception as e:
            logger.error(f"Error sending OCR error event via pipe: {e}", exc_info=True)

    def _publish_ocr_status_via_pipe(self, status_event: str, status_message: str, 
                                    status_context: Dict[str, Any] = None,
                                    origin_timestamp_ns: Optional[int] = None):
        """
        Publish OCR status events via pipe to parent process for Redis publishing.
        
        Args:
            status_event: Type of status event (e.g., 'OCR_STARTED', 'OCR_STOPPED', 'CONFIG_UPDATED')
            status_message: Human-readable status message
            status_context: Additional context about the status change
            origin_timestamp_ns: Optional golden timestamp in nanoseconds indicating when the event originally occurred
        """
        if not self._pipe_to_parent:
            return  # No pipe available
            
        try:
            # Use golden timestamp if provided, otherwise check current frame context, finally fall back to current time
            event_timestamp_ns = origin_timestamp_ns
            correlation_id = None
            
            if event_timestamp_ns is None:
                # Try to get golden timestamp from current frame context
                with self._frame_context_lock:
                    if self._current_frame_context:
                        capture_timestamp_ns = self._current_frame_context.get('capture_timestamp_ns')
                        if capture_timestamp_ns:
                            event_timestamp_ns = capture_timestamp_ns  # Keep in nanoseconds
                        correlation_id = self._current_frame_context.get('master_correlation_id')
            
            # Final fallback to current time
            if event_timestamp_ns is None:
                event_timestamp_ns = time.perf_counter_ns()
            
            # Create status event payload compatible with existing handler
            status_payload = {
                "status_event": status_event,
                "message": status_message,  # Use 'message' field for compatibility
                "status_context": status_context or {},
                "timestamp_ns": event_timestamp_ns,  # Golden timestamp in nanoseconds
                "component": "OCRService"
            }
            
            # Add correlation ID if available
            if correlation_id:
                status_payload["correlation_id"] = correlation_id
            
            # Create pipe message
            pipe_message = {
                "type": "ocr_status_update",
                "data": status_payload
            }
            
            self._pipe_to_parent.send(pipe_message)
            
            # Log with indication of timestamp source
            timestamp_source = "golden" if origin_timestamp_ns or (self._current_frame_context and 'capture_timestamp_ns' in self._current_frame_context) else "current"
            logger.debug(f"OCR_PROCESS: Published {status_event} status via pipe (timestamp source: {timestamp_source})")
            
        except Exception as e:
            logger.error(f"Error sending OCR status event via pipe: {e}", exc_info=True)

    def _set_current_frame_context(self, capture_timestamp_ns: Optional[int], 
                                  master_correlation_id: Optional[str]):
        """
        Set the current frame context for golden timestamp tracking.
        
        Args:
            capture_timestamp_ns: Golden timestamp in nanoseconds when frame was captured
            master_correlation_id: Master correlation ID for the frame
        """
        with self._frame_context_lock:
            if capture_timestamp_ns and master_correlation_id:
                self._current_frame_context = {
                    'capture_timestamp_ns': capture_timestamp_ns,
                    'master_correlation_id': master_correlation_id
                }
                logger.debug(f"OCR_PROCESS: Set frame context with golden timestamp {capture_timestamp_ns}")
            else:
                self._current_frame_context = None
                logger.debug("OCR_PROCESS: Cleared frame context")

    def _clear_current_frame_context(self):
        """Clear the current frame context when frame processing is complete."""
        with self._frame_context_lock:
            self._current_frame_context = None

    def _publish_ocr_performance_degradation_via_pipe(self, degradation_type: str, 
                                                     current_value: float, 
                                                     threshold_value: float,
                                                     performance_context: Dict[str, Any]):
        """
        Publish OCR performance degradation events via pipe to parent process for Redis publishing.
        
        Args:
            degradation_type: Type of degradation (e.g., 'CONFIDENCE_DROP', 'PROCESSING_TIMEOUT', 'QUEUE_TIMEOUT')
            current_value: Current performance value 
            threshold_value: Expected threshold value
            performance_context: Additional performance context and metrics
        """
        if not self._pipe_to_parent:
            return  # No pipe available
            
        try:
            # Create performance degradation event payload
            degradation_payload = {
                "degradation_type": degradation_type,
                "current_value": current_value,
                "threshold_value": threshold_value,
                "performance_impact": (current_value - threshold_value) / threshold_value * 100,  # Percentage impact
                "performance_context": performance_context,
                "timestamp": time.time(),
                "component": "OCRService",
                "severity": "warning" if abs(current_value - threshold_value) < threshold_value * 0.5 else "error"
            }
            
            # Create pipe message
            pipe_message = {
                "type": "ocr_error_event",  # Use existing error pipeline for performance issues
                "data": {
                    "error_type": "PERFORMANCE_DEGRADATION",
                    "error_message": f"OCR performance degradation detected: {degradation_type}",
                    "error_context": degradation_payload,
                    "timestamp": time.time(),
                    "component": "OCRService",
                    "severity": degradation_payload["severity"]
                }
            }
            
            self._pipe_to_parent.send(pipe_message)
            logger.warning(f"OCR_PROCESS: Published {degradation_type} performance degradation via pipe")
            
        except Exception as e:
            logger.error(f"Error sending OCR performance degradation event via pipe: {e}", exc_info=True)

    def _check_confidence_degradation(self, current_confidence: float, frame_counter: int):
        """
        Check for OCR confidence degradation and publish alerts.
        
        Args:
            current_confidence: Current OCR confidence percentage
            frame_counter: Current frame number for context
        """
        try:
            # Check if confidence dropped below threshold
            if current_confidence < self._confidence_drop_threshold:
                self._confidence_drop_count += 1
                
                # Only alert on first drop or every 10th consecutive drop to avoid spam
                if self._confidence_drop_count == 1 or self._confidence_drop_count % 10 == 0:
                    performance_context = {
                        "frame_counter": frame_counter,
                        "previous_confidence": self._last_confidence,
                        "confidence_drop_count": self._confidence_drop_count,
                        "confidence_threshold": self._confidence_drop_threshold,
                        "confidence_drop_percentage": ((self._last_confidence - current_confidence) / self._last_confidence) * 100
                    }
                    
                    self._publish_ocr_performance_degradation_via_pipe(
                        degradation_type="CONFIDENCE_DROP",
                        current_value=current_confidence,
                        threshold_value=self._confidence_drop_threshold,
                        performance_context=performance_context
                    )
            else:
                # Reset counter if confidence recovers
                if self._confidence_drop_count > 0:
                    logger.info(f"OCR confidence recovered: {current_confidence:.2f}% (was dropping for {self._confidence_drop_count} frames)")
                self._confidence_drop_count = 0
            
            # Update last confidence for next comparison
            self._last_confidence = current_confidence
            
        except Exception as e:
            logger.error(f"Error checking confidence degradation: {e}", exc_info=True)

    def _check_processing_performance_degradation(self, total_duration_ms: float, 
                                                preproc_duration_ms: float, 
                                                ocr_duration_ms: float, 
                                                frame_counter: int):
        """
        Check for OCR processing performance degradation and publish alerts.
        
        Args:
            total_duration_ms: Total processing time in milliseconds
            preproc_duration_ms: Preprocessing time in milliseconds  
            ocr_duration_ms: OCR processing time in milliseconds
            frame_counter: Current frame number for context
        """
        try:
            current_time = time.time()
            
            # Check total processing timeout with cooldown
            if (total_duration_ms > self._processing_timeout_threshold_ms and 
                self._should_alert("processing_timeout", current_time)):
                
                performance_context = {
                    "frame_counter": frame_counter,
                    "breakdown": {
                        "total_ms": total_duration_ms,
                        "preprocessing_ms": preproc_duration_ms,
                        "ocr_ms": ocr_duration_ms
                    },
                    "threshold_ms": self._processing_timeout_threshold_ms,
                    "timeout_factor": total_duration_ms / self._processing_timeout_threshold_ms
                }
                
                self._publish_ocr_performance_degradation_via_pipe(
                    degradation_type="PROCESSING_TIMEOUT",
                    current_value=total_duration_ms,
                    threshold_value=self._processing_timeout_threshold_ms,
                    performance_context=performance_context
                )
            
            # Check OCR-specific timeout with cooldown
            if (ocr_duration_ms > self._ocr_timeout_threshold_ms and 
                self._should_alert("ocr_timeout", current_time)):
                
                performance_context = {
                    "frame_counter": frame_counter,
                    "ocr_duration_ms": ocr_duration_ms,
                    "ocr_threshold_ms": self._ocr_timeout_threshold_ms,
                    "ocr_timeout_factor": ocr_duration_ms / self._ocr_timeout_threshold_ms,
                    "total_processing_ms": total_duration_ms
                }
                
                self._publish_ocr_performance_degradation_via_pipe(
                    degradation_type="OCR_TIMEOUT",
                    current_value=ocr_duration_ms,
                    threshold_value=self._ocr_timeout_threshold_ms,
                    performance_context=performance_context
                )
            
            # Check preprocessing timeout with cooldown
            if (preproc_duration_ms > self._preprocessing_timeout_threshold_ms and 
                self._should_alert("preprocessing_timeout", current_time)):
                
                performance_context = {
                    "frame_counter": frame_counter,
                    "preprocessing_duration_ms": preproc_duration_ms,
                    "preprocessing_threshold_ms": self._preprocessing_timeout_threshold_ms,
                    "preprocessing_timeout_factor": preproc_duration_ms / self._preprocessing_timeout_threshold_ms,
                    "total_processing_ms": total_duration_ms
                }
                
                self._publish_ocr_performance_degradation_via_pipe(
                    degradation_type="PREPROCESSING_TIMEOUT",
                    current_value=preproc_duration_ms,
                    threshold_value=self._preprocessing_timeout_threshold_ms,
                    performance_context=performance_context
                )
                
        except Exception as e:
            logger.error(f"Error checking processing performance degradation: {e}", exc_info=True)

    def _should_alert(self, alert_type: str, current_time: float) -> bool:
        """
        Check if we should send an alert based on cooldown period.
        
        Args:
            alert_type: Type of alert for cooldown tracking
            current_time: Current timestamp
            
        Returns:
            True if alert should be sent, False if in cooldown
        """
        cooldown_period = 30.0  # 30 second cooldown between similar alerts
        last_alert_time = self._performance_alert_cooldown.get(alert_type, 0)
        
        if current_time - last_alert_time > cooldown_period:
            self._performance_alert_cooldown[alert_type] = current_time
            return True
        return False

    @log_function_call('ocr_service')
    def get_roi(self) -> List[int]:
        """Gets the current Region of Interest."""
        with self._lock:
            return list(self._roi_coordinates) # Return a copy

    @log_function_call('ocr_service')
    def get_latest_frame_for_consumer(self, timeout: float = 0.1) -> Optional[np.ndarray]:
        """
        Allows external consumers (like video recorder) to get the latest frame
        from the internal queue without disrupting OCR processing flow significantly.
        Returns None if no frame is available within the timeout or on error.
        """
        # Ensure necessary imports are present at the top of the file:
        # from queue import Empty as QueueEmpty
        # import numpy as np
        # import logging
        # logger = logging.getLogger(__name__) # Ensure logger exists

        if not hasattr(self, '_latest_frame_package'):
            logger.error("Frame mailbox not initialized in OCRService.")
            return None

        try:
            # Try to get item without blocking indefinitely
            frame_package = None
            with self._new_frame_condition:
                self._new_frame_condition.wait(timeout=timeout)
                if self._latest_frame_package:
                    frame_package = self._latest_frame_package
            return frame_package
        except QueueEmpty:
            # Expected if queue is empty within timeout
            return None
        except Exception as e:
            logger.error(f"Error getting frame from queue for consumer: {e}")
            return None

    @log_function_call('ocr_service')
    def set_confidence_mode(self, frequent: bool):
         """Sets the confidence calculation frequency mode."""
         if frequent:
             self._confidence_check_frequency = self.ROI_OPTIMIZATION_CONF_FREQ
             self._confidence_mode_is_frequent = True
             logger.info(f"Confidence mode set to Frequent (every {self._confidence_check_frequency} frames)")
             self._notify_status("Status: Confidence check set to Frequent")
         else:
             self._confidence_check_frequency = self.NORMAL_OPERATION_CONF_FREQ
             self._confidence_mode_is_frequent = False
             logger.info(f"Confidence mode set to Infrequent (every {self._confidence_check_frequency} frames)")
             self._notify_status("Status: Confidence check set to Infrequent")

    @log_function_call('ocr_service')
    def is_confidence_mode_frequent(self) -> bool:
        """Returns True if confidence checks are currently frequent."""
        return self._confidence_mode_is_frequent

    @log_function_call('ocr_service')
    def get_preview_output_queue(self) -> Queue:
        """Returns the queue for processed frames intended for GUI preview."""
        return self._preview_output_queue

    @log_function_call('ocr_service')
    def update_configuration(self, config: OcrConfigData):
        """Updates the service's configuration."""
        self.logger.info("Updating OCRService configuration...")
        with self._lock: # Protect updates if needed
            self._config = config
            # Update internal state derived from config if necessary
            if self._tesseract_cmd != config.tesseract_cmd:
                 self._tesseract_cmd = config.tesseract_cmd
                 # C++ accelerator doesn't need pytesseract configuration

            # Update preprocessing parameters from the new config
            self._upscale_factor = config.upscale_factor
            self._force_black_text_on_white = config.force_black_text_on_white
            self._unsharp_strength = config.unsharp_strength
            self._threshold_block_size = config.threshold_block_size
            self._threshold_c = config.threshold_c
            self._red_boost = config.red_boost
            self._green_boost = config.green_boost

            # Update enhanced preprocessing parameters if they exist in the config
            if hasattr(config, 'apply_text_mask_cleaning'):
                self._apply_text_mask_cleaning = config.apply_text_mask_cleaning
                self._text_mask_min_contour_area = config.text_mask_min_contour_area
                self._text_mask_min_width = config.text_mask_min_width
                self._text_mask_min_height = config.text_mask_min_height
                self._enhance_small_symbols = config.enhance_small_symbols
                self._symbol_max_height = config.symbol_max_height_for_enhancement_upscaled
                self._period_comma_ratio = config.period_comma_aspect_ratio_range_upscaled
                self._period_comma_radius = config.period_comma_draw_radius_upscaled
                self._hyphen_min_ratio = config.hyphen_like_min_aspect_ratio_upscaled
                self._hyphen_min_height = config.hyphen_like_draw_min_height_upscaled

            # ROI is updated via set_roi, not here
            # self._roi_coordinates = list(config.initial_roi)
        self.logger.info("OCRService configuration updated.")
        self._notify_status("OCR Service Config Updated") # Optional status update
        
        # Publish configuration updated status event
        status_context = {
            "config_update_type": "ocr_service_config",
            "updated_fields": [
                "tesseract_cmd", "initial_roi", "upscale_factor", "unsharp_strength", 
                "threshold_block_size", "threshold_c"
            ],
            "new_tesseract_cmd": config.tesseract_cmd,
            "new_roi": list(config.initial_roi),
            "new_video_mode": False
        }
        self._publish_ocr_status_via_pipe(
            status_event="CONFIG_UPDATED",
            status_message="OCR Service configuration updated",
            status_context=status_context
        )

    @log_function_call('ocr_service')
    def set_preprocessing_params(self,
                             upscale_factor: float,
                             force_black_text_on_white: bool,
                             unsharp_strength: float,
                             threshold_block_size: int,
                             threshold_c: int,
                             red_boost: float, # Parameter at index 5
                             green_boost: float, # Parameter at index 6
                             apply_text_mask_cleaning: bool, # Parameter at index 7
                             text_mask_min_contour_area: int,
                             text_mask_min_width: int,
                             text_mask_min_height: int,
                             enhance_small_symbols: bool,
                             symbol_max_height_for_enhancement_upscaled: int,
                             period_comma_aspect_ratio_range_upscaled: tuple,
                             period_comma_draw_radius_upscaled: int,
                             hyphen_like_min_aspect_ratio_upscaled: float,
                             hyphen_like_draw_min_height_upscaled: int # Parameter at index 16
                             ):
        """Updates the preprocessing parameters."""
        self.logger.debug("OCRService.set_preprocessing_params called")  # Debug level for verbose logging

        with self._lock:
            # Update all internal parameters
            self._upscale_factor = upscale_factor
            self._force_black_text_on_white = force_black_text_on_white
            self._unsharp_strength = unsharp_strength
            self._threshold_block_size = threshold_block_size
            self._threshold_c = threshold_c
            self._red_boost = red_boost
            self._green_boost = green_boost

            self._apply_text_mask_cleaning = apply_text_mask_cleaning
            self._text_mask_min_contour_area = text_mask_min_contour_area
            self._text_mask_min_width = text_mask_min_width
            self._text_mask_min_height = text_mask_min_height

            self._enhance_small_symbols = enhance_small_symbols
            self._symbol_max_height = symbol_max_height_for_enhancement_upscaled
            self._period_comma_ratio = period_comma_aspect_ratio_range_upscaled
            self._period_comma_radius = period_comma_draw_radius_upscaled
            self._hyphen_min_ratio = hyphen_like_min_aspect_ratio_upscaled
            self._hyphen_min_height = hyphen_like_draw_min_height_upscaled

            # Also update in config for consistency
            self._config.upscale_factor = upscale_factor
            self._config.force_black_text_on_white = force_black_text_on_white
            self._config.unsharp_strength = unsharp_strength
            self._config.threshold_block_size = threshold_block_size
            self._config.threshold_c = threshold_c
            self._config.red_boost = red_boost
            self._config.green_boost = green_boost

            self._config.apply_text_mask_cleaning = apply_text_mask_cleaning
            self._config.text_mask_min_contour_area = text_mask_min_contour_area
            self._config.text_mask_min_width = text_mask_min_width
            self._config.text_mask_min_height = text_mask_min_height

            self._config.enhance_small_symbols = enhance_small_symbols
            self._config.symbol_max_height_for_enhancement_upscaled = symbol_max_height_for_enhancement_upscaled
            self._config.period_comma_aspect_ratio_range_upscaled = period_comma_aspect_ratio_range_upscaled
            self._config.period_comma_draw_radius_upscaled = period_comma_draw_radius_upscaled
            self._config.hyphen_like_min_aspect_ratio_upscaled = hyphen_like_min_aspect_ratio_upscaled
            self._config.hyphen_like_draw_min_height_upscaled = hyphen_like_draw_min_height_upscaled

        # Log the updated parameters - keep at debug level for detailed info
        if hasattr(self, '_enhance_small_symbols'):
            self.logger.debug(f"Preprocessing parameters updated in OCRService: upscale={self._upscale_factor}, invert={self._force_black_text_on_white}, "
                            f"sharp={self._unsharp_strength}, block={self._threshold_block_size}, "
                            f"C={self._threshold_c}, red_boost={self._red_boost}, green_boost={self._green_boost}, "
                            f"text_mask_cleaning={self._apply_text_mask_cleaning}, contour_area={self._text_mask_min_contour_area}, "
                            f"min_width={self._text_mask_min_width}, min_height={self._text_mask_min_height}, "
                            f"enhance_symbols={self._enhance_small_symbols}, symbol_max_h={self._symbol_max_height}, "
                            f"period_ratio={self._period_comma_ratio}, period_radius={self._period_comma_radius}, "
                            f"hyphen_ratio={self._hyphen_min_ratio}, hyphen_height={self._hyphen_min_height}")
        else:
            self.logger.debug(f"Preprocessing parameters updated in OCRService: upscale={self._upscale_factor}, invert={self._force_black_text_on_white}, "
                            f"sharp={self._unsharp_strength}, block={self._threshold_block_size}, "
                            f"C={self._threshold_c}, "
                            f"red_boost={self._red_boost}, green_boost={self._green_boost}")

        # Keep a shorter info log for significant state changes
        self.logger.info(f"OCR preprocessing parameters updated successfully")
        self._notify_status(f"Preprocessing updated: upscale={self._upscale_factor}, invert={self._force_black_text_on_white}")

    @log_function_call('ocr_service')
    def get_preprocessing_params(self) -> Tuple:
        """
        Returns the current preprocessing parameters.

        Returns:
            tuple: A tuple containing all preprocessing parameters in the following order:
                (upscale_factor, force_black_text_on_white, unsharp_strength,
                threshold_block_size, threshold_c, red_boost, green_boost,
                apply_text_mask_cleaning, text_mask_min_contour_area, text_mask_min_width,
                text_mask_min_height, enhance_small_symbols, symbol_max_height_for_enhancement_upscaled,
                period_comma_aspect_ratio_range_upscaled, period_comma_draw_radius_upscaled,
                hyphen_like_min_aspect_ratio_upscaled, hyphen_like_draw_min_height_upscaled)
        """
        self.logger.debug("OCRService.get_preprocessing_params called")  # Debug level for verbose logging

        with self._lock:
            params = (
                self._upscale_factor,
                self._force_black_text_on_white,
                self._unsharp_strength,
                self._threshold_block_size,
                self._threshold_c,
                self._red_boost,
                self._green_boost,
                self._apply_text_mask_cleaning,
                self._text_mask_min_contour_area,
                self._text_mask_min_width,
                self._text_mask_min_height,
                self._enhance_small_symbols,
                self._symbol_max_height,
                self._period_comma_ratio,
                self._period_comma_radius,
                self._hyphen_min_ratio,
                self._hyphen_min_height
            )
            self.logger.debug(f"OCRService.get_preprocessing_params: Returning: {params}")
            return params

    @log_function_call('ocr_service')
    def get_latest_frame_for_consumer(self, timeout: float = 0.1) -> Optional[np.ndarray]:
        """
        Allows external consumers (like video recorder) to get the latest frame
        from the internal queue without disrupting OCR processing flow significantly.
        Returns None if no frame is available within the timeout or on error.

        Args:
            timeout: Maximum time to wait for a frame, in seconds.

        Returns:
            The latest frame as a numpy array, or None if no frame is available.
        """
        if not hasattr(self, '_latest_frame_package'):
            logger.error("Frame mailbox not initialized in OCRService.")
            return None

        try:
            # Try to get item without blocking indefinitely
            frame_package = None
            with self._new_frame_condition:
                self._new_frame_condition.wait(timeout=timeout)
                if self._latest_frame_package:
                    frame_package = self._latest_frame_package
            return frame_package
        except QueueEmpty:
            # Expected if queue is empty within timeout
            return None
        except Exception as e:
            logger.error(f"Error getting frame from queue for consumer: {e}")
            return None

    @log_function_call('ocr_service')
    def get_status(self) -> Dict[str, Any]:
        """Returns the current status of the service."""
        # Consider adding thread is_alive() checks
        return {
            "ocr_active": self._ocr_active,
            "recording_active": self._recording_active,
            "capture_active": self._capture_active,
            "confidence_mode_frequent": self._confidence_mode_is_frequent,
            "roi": self.get_roi(),
        }

    @log_function_call('ocr_service')
    def shutdown(self):
        """Gracefully stops all threads and releases resources."""
        logger.info("Shutting down OCR Service...")
        # Order: Stop consumers first, then the producer (capture)
        self.stop_ocr()
        self.stop_recording()
        self._stop_capture_thread() # Stop the frame source last

        # Release video capture if it exists
        if self.video_capture is not None:
            try:
                self.video_capture.release()
                logger.info("Video capture released")
            except Exception as e:
                logger.error(f"Error releasing video capture: {e}")
            finally:
                self.video_capture = None

        # Wait for all threads to ensure they have exited
        if self._ocr_thread and self._ocr_thread.is_alive(): self._ocr_thread.join(0.5)
        if self._recording_thread and self._recording_thread.is_alive(): self._recording_thread.join(0.5)
        if self._capture_thread and self._capture_thread.is_alive(): self._capture_thread.join(0.5)

        logger.info("OCR Service shutdown complete.")
        self._notify_status("OCR Service Shutdown")
        
        # Publish service shutdown status event
        status_context = {
            "shutdown_reason": "normal_shutdown",
            "final_roi_coordinates": getattr(self, '_roi_coordinates', 'unknown'),
            "ocr_was_active": getattr(self, '_ocr_active', False),
            "capture_was_active": getattr(self, '_capture_active', False),
            "recording_was_active": getattr(self, '_recording_active', False)
        }
        self._publish_ocr_status_via_pipe(
            status_event="SERVICE_SHUTDOWN",
            status_message="OCR Service shutdown complete",
            status_context=status_context
        )

    @log_function_call('ocr_service')
    def get_performance_metrics(self) -> Dict[str, float]:
        """
        Get aggregated performance metrics from fine-grained timing data.

        Returns:
            Dictionary containing aggregated performance metrics:
            - cpp_processing_time_ms: Average C++ accelerator processing time
            - tesseract_processing_time_ms: Average Tesseract OCR time (legacy field)
            - output_events_per_minute: Rate of OCR events processed
        """
        try:
            # Import performance tracker functions
            from utils.performance_tracker import get_performance_stats, is_performance_tracking_enabled

            if not is_performance_tracking_enabled():
                # Return default values when tracking is disabled
                return {
                    'cpp_processing_time_ms': 0.0,
                    'tesseract_processing_time_ms': 0.0,
                    'output_events_per_minute': 0.0
                }

            # Get performance stats from the fine-grained tracker
            stats = get_performance_stats()

            # Extract relevant timing metrics
            cpp_time_ms = 0.0
            tesseract_time_ms = 0.0

            # Calculate average preprocessing time (C++ accelerator operations)
            preproc_key = 'ocr_service.preprocessing_start_to_ocr_service.preprocessing_end'
            if preproc_key in stats and stats[preproc_key]:
                # Filter out non-numeric values to prevent type errors
                numeric_values = [v for v in stats[preproc_key] if isinstance(v, (int, float))]
                if numeric_values:
                    cpp_time_ms = sum(numeric_values) / len(numeric_values) * 1000  # Convert to ms

            # Calculate average Tesseract OCR time
            tesseract_key = 'ocr_service.tesseract_ocr_start_to_ocr_service.tesseract_ocr_end'
            if tesseract_key in stats and stats[tesseract_key]:
                # Filter out non-numeric values to prevent type errors
                numeric_values = [v for v in stats[tesseract_key] if isinstance(v, (int, float))]
                if numeric_values:
                    tesseract_time_ms = sum(numeric_values) / len(numeric_values) * 1000  # Convert to ms

            # Calculate output rate based on recent activity
            # Use the observer's frame count if available
            output_rate = 0.0
            if hasattr(self, '_observer') and hasattr(self._observer, 'frame_count'):
                # Estimate events per minute based on recent frame processing
                # This is a rough estimate - could be enhanced with time-based tracking
                output_rate = min(self._observer.frame_count * 6, 600)  # Cap at 600/min for safety

            return {
                'cpp_processing_time_ms': cpp_time_ms,
                'tesseract_processing_time_ms': tesseract_time_ms,
                'output_events_per_minute': output_rate
            }
        except Exception as e:
            self.logger.error(f"Error calculating performance metrics: {e}")
            return {
                'cpp_processing_time_ms': 0.0,
                'tesseract_processing_time_ms': 0.0,
                'output_events_per_minute': 0.0
            }

    # --- Health Status Publishing Methods ---

    def _publish_service_health_status(self, status: str, message: str, context: Dict[str, Any]):
        """
        Publish health status events to Redis stream via telemetry service.
        
        Args:
            status: Service status (STARTED, STOPPED, HEALTHY, DEGRADED, etc.)
            message: Human-readable status message
            context: Additional context data for the health event
        """
        if not self._correlation_logger:
            logger.debug("Cannot publish service health status: correlation logger missing")
            return
        
        try:
            # Check if IPC data dumping is disabled (TANK mode) FIRST
            # Note: TANK mode check would need config service access
            # For now, proceed with publishing
            
            # Create service health status payload
            payload = {
                "health_status": status,
                "status_reason": message,
                "context": context,
                "status_timestamp": time.time(),
                "component": "OCRService"
            }
            
            # Define target stream for health status
            target_stream = "testrade:system-health"
            
            # Use correlation logger for publishing
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OCRService",
                    event_name="ServiceHealthStatus",
                    payload=payload,
                    stream_override=target_stream
                )
                logger.debug(f"OCR service health status logged: {status}")
                
        except Exception as e:
            # Never let health publishing errors affect main functionality
            logger.error(f"OCRService: Error publishing health status: {e}", exc_info=True)

    def _publish_periodic_health_status(self):
        """
        Publish periodic health status with operational metrics.
        Called during normal operation to provide health monitoring data.
        """
        try:
            # Calculate operational metrics
            current_time = time.time()
            
            # Basic health indicators
            health_context = {
                "service_name": "OCRService",
                "ocr_active": getattr(self, '_ocr_active', False),
                "capture_active": getattr(self, '_capture_active', False),
                "observer_available": self._observer is not None and self._observer() is not None,
                "tesseract_available": getattr(self, '_tesseract_cmd', None) is not None,
                "roi_configured": hasattr(self, '_roi_coordinates') and self._roi_coordinates is not None,
                "video_mode": False,
                "health_status": "HEALTHY",
                "last_health_check": current_time
            }
            
            # Add performance metrics if available
            try:
                perf_metrics = self.get_performance_metrics()
                health_context.update({
                    "cpp_processing_time_ms": perf_metrics.get('cpp_processing_time_ms', 0.0),
                    "tesseract_processing_time_ms": perf_metrics.get('tesseract_processing_time_ms', 0.0),
                    "output_events_per_minute": perf_metrics.get('output_events_per_minute', 0.0)
                })
            except Exception as perf_err:
                logger.warning(f"OCRService: Error getting performance metrics for health check: {perf_err}")
                health_context["performance_metrics_error"] = str(perf_err)
            
            # Check for potential health issues
            health_issues = []
            
            # Check if OCR processing is stuck or degraded
            if hasattr(self, '_last_confidence') and hasattr(self, '_confidence_degradation_alerts'):
                if len(self._confidence_degradation_alerts) > 0:
                    health_issues.append("confidence_degradation_detected")
            
            # Check if processing performance is degraded
            if hasattr(self, '_performance_alert_cooldown') and len(self._performance_alert_cooldown) > 0:
                health_issues.append("performance_degradation_detected")
            
            if health_issues:
                health_context["health_status"] = "DEGRADED"
                health_context["health_issues"] = health_issues
            
            # Publish health status
            self._publish_service_health_status(
                "PERIODIC_HEALTH_CHECK",
                f"OCRService periodic health check - Status: {health_context['health_status']}",
                health_context
            )
            
        except Exception as e:
            # Never let health publishing errors affect main functionality
            logger.error(f"OCRService: Error in periodic health status publishing: {e}", exc_info=True)

    def check_and_publish_health_status(self):
        """
        Public method to trigger health status publishing.
        Can be called externally to force a health check.
        """
        try:
            self._publish_periodic_health_status()
        except Exception as e:
            logger.error(f"OCRService: Error in external health status check: {e}", exc_info=True)