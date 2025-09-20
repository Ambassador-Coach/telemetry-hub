# modules/ocr/interfaces.py
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Set, Tuple
from dataclasses import dataclass
import numpy as np

# --- Configuration Structure ---
# Define a dataclass for OCR-specific config
# This replaces reliance on the generic GlobalConfig directly inside OCR logic
@dataclass
class OcrConfigData:
    """
    Configuration data for OCR processing.
    Contains all parameters needed for OCR service initialization and preprocessing.
    """
    # Core configuration
    tesseract_cmd: str  # Path to Tesseract executable
    initial_roi: List[int]  # Region of interest coordinates [x1, y1, x2, y2]
    include_all_symbols: bool = False  # Whether to include all symbols in OCR processing
    flicker_params: Optional[Dict[str, Any]] = None  # Flicker filter settings as dict
    debug_ocr_handler: bool = False  # Enable debug logging for OCR handler

    # Video input configuration
    video_file_path: Optional[str] = None  # Path to video file for replay mode (None = live screen capture)
    video_loop_enabled: bool = False  # Whether to loop video when it ends (False for long videos)

    # Capture timing configuration
    ocr_capture_interval_seconds: float = 0.2  # Default to 0.2s (5 FPS) if not in config
    
    # ZMQ Command Listener configuration
    ocr_ipc_command_pull_address: str = 'tcp://127.0.0.1:5559'  # Default ZMQ address for OCR commands

    # Basic preprocessing parameters (7)
    # These names MUST match how they are accessed in OCRService.__init__
    upscale_factor: float = 2.5  # Image upscaling factor before OCR
    force_black_text_on_white: bool = True  # Invert colors for black text on white background
    unsharp_strength: float = 1.8  # Unsharp mask strength for sharpening
    threshold_block_size: int = 25  # Block size for adaptive thresholding (must be odd)
    threshold_c: int = -3  # C value for adaptive thresholding
    red_boost: float = 1.8  # Red channel boost factor
    green_boost: float = 1.8  # Green channel boost factor

    # Text mask cleaning parameters (4)
    apply_text_mask_cleaning: bool = True  # Whether to apply text mask cleaning
    text_mask_min_contour_area: int = 10  # Minimum contour area for text mask cleaning
    text_mask_min_width: int = 2  # Minimum contour width for text mask cleaning
    text_mask_min_height: int = 2  # Minimum contour height for text mask cleaning

    # Symbol enhancement parameters (6)
    enhance_small_symbols: bool = True  # Whether to enhance small symbols
    symbol_max_height_for_enhancement_upscaled: int = 20  # Maximum height for symbol enhancement
    period_comma_aspect_ratio_range_upscaled: Tuple[float, float] = (0.5, 1.8)  # Aspect ratio range for period/comma detection
    period_comma_draw_radius_upscaled: int = 3  # Draw radius for period/comma enhancement
    hyphen_like_min_aspect_ratio_upscaled: float = 1.8  # Minimum aspect ratio for hyphen detection
    hyphen_like_draw_min_height_upscaled: int = 2  # Minimum draw height for hyphen enhancement

    def __post_init__(self):
        # Ensure initial_roi is a mutable list
        self.initial_roi = list(self.initial_roi)
        # Initialize flicker_params_dict for backward compatibility
        self.flicker_params_dict = self.flicker_params or {}

# --- Service Interface ---
class IOcrService(ABC):
    """Interface for the main OCR service controlling capture and processing."""

    @abstractmethod
    def start_ocr(self): pass

    @abstractmethod
    def stop_ocr(self): pass

    @abstractmethod
    def start_recording(self, output_dir: Optional[str] = None, fps: float = 15.0): pass

    @abstractmethod
    def stop_recording(self): pass

    @abstractmethod
    def set_roi(self, x1: int, y1: int, x2: int, y2: int): pass

    @abstractmethod
    def get_roi(self) -> List[int]: pass

    @abstractmethod
    def set_confidence_mode(self, frequent: bool): pass

    @abstractmethod
    def is_confidence_mode_frequent(self) -> bool: pass

    @abstractmethod
    def get_status(self) -> Dict[str, Any]: pass

    @abstractmethod
    def shutdown(self): pass

    @abstractmethod
    def get_latest_frame_for_consumer(self, timeout: float = 0.1) -> Optional[np.ndarray]: pass

    @abstractmethod
    def update_configuration(self, config: OcrConfigData):
        """Updates the service's configuration."""
        pass

    @abstractmethod
    def set_preprocessing_params(self, upscale_factor: float, force_black_text_on_white: bool,
                               unsharp_strength: float = None, threshold_block_size: int = None,
                               threshold_c: int = None, red_boost: float = None, green_boost: float = None,
                               apply_text_mask_cleaning: bool = None, text_mask_min_contour_area: int = None,
                               text_mask_min_width: int = None, text_mask_min_height: int = None,
                               enhance_small_symbols: bool = None, symbol_max_height_for_enhancement_upscaled: int = None,
                               period_comma_aspect_ratio_range_upscaled: tuple = None, period_comma_draw_radius_upscaled: int = None,
                               hyphen_like_min_aspect_ratio_upscaled: float = None, hyphen_like_draw_min_height_upscaled: int = None):
        """Updates the preprocessing parameters."""
        pass

    @abstractmethod
    def get_preprocessing_params(self) -> tuple:
        """
        Returns the current preprocessing parameters as a tuple:
        (upscale_factor, force_black_text_on_white, unsharp_strength,
         threshold_block_size, threshold_c, red_boost, green_boost,
         apply_text_mask_cleaning, text_mask_min_contour_area,
         text_mask_min_width, text_mask_min_height, enhance_small_symbols,
         symbol_max_height_for_enhancement_upscaled,
         period_comma_aspect_ratio_range_upscaled,
         period_comma_draw_radius_upscaled,
         hyphen_like_min_aspect_ratio_upscaled,
         hyphen_like_draw_min_height_upscaled)
        """
        pass

    # Maybe add frame grabbing for video service if needed externally?
    # @abstractmethod
    # def get_latest_frame_for_consumer(self, timeout: float = 0.1) -> Optional[Any]: pass # Return type depends on format (np.ndarray?)

# --- Handler Interface ---
class IProcessedOCRHandler(ABC):
    """Interface for the logic that parses OCR text into snapshots/strings."""

    @abstractmethod
    def process_text_to_snapshots(self,
                                 processed_ocr_text: str,
                                 frame_timestamp: Optional[float] = None,
                                 raw_finish_timestamp: Optional[float] = None,
                                 confidence: float = 0.0,
                                 raw_ocr_text: str = "",
                                 perf_timestamps: Dict[str, float] = None
                                 ) -> Dict[str, Dict[str, Any]]:
        pass

    @abstractmethod
    def process_text_to_gui_string(self,
                                   processed_ocr_text: str,
                                   perf_timestamps: Dict[str, float] = None
                                  ) -> str:
        pass

    @abstractmethod
    def set_symbol_filtering(self, include_all: bool):
        pass

    @abstractmethod
    def update_configuration(self, config: OcrConfigData): # Use the specific config dataclass
        """Updates the handler's configuration (e.g., flicker params, debug)."""
        pass

    @abstractmethod
    def reset_state(self):
        """Resets internal state like flicker filter history."""
        pass

    @abstractmethod
    def clear_stable_cost(self, symbol: str):
        """Clears the stable cost basis and strategy for a symbol."""
        pass

    @abstractmethod
    def get_last_known_strategy(self, symbol: str) -> Optional[str]:
        """Returns the last known strategy for a symbol, or None if not found."""
        pass

# --- Dependency Interfaces (Implementations will be outside OCR module) ---
class ISymbolProvider(ABC):
    @abstractmethod
    def is_tradeable(self, symbol: str) -> bool: pass
    @abstractmethod
    def is_non_tradeable(self, symbol: str) -> bool: pass

    @abstractmethod
    def get_all_symbols(self) -> Set[str]:
        """Gets the set of all known symbols (tradeable and non-tradeable)."""
        pass

class IPipelineLogger(ABC):
    """Interface for logging structured pipeline events."""
    @abstractmethod
    def log_event(self, event_type: str, data: Any):
        """Logs a specific event with associated data."""
        pass

class IAppObserver(ABC):
    """Interface for the App/GUI that receives callbacks."""
    @abstractmethod
    def on_ocr_result(self, data: Dict[str, Any]): pass
    @abstractmethod
    def on_status_update(self, message: str): pass

# --- NEW: Consolidated Interfaces from core/service_interfaces.py ---

class IROIService(ABC):
    """Interface for ROI (Region of Interest) management and persistence."""
    @abstractmethod
    def process_roi_update_from_ocr(self, roi_update_data: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def get_current_roi(self) -> List[int]:
        pass

class IOCRParametersService(ABC):
    """Interface for OCR parameters service."""
    @abstractmethod
    def process_ocr_parameters_update_from_ocr(self, parameters_update_data: Dict[str, Any]):
        pass
    
    @abstractmethod
    def get_current_parameters(self) -> Dict[str, Any]:
        pass


class IOCRProcessManager(ABC):
    """Interface for OCR Process Management service."""
    
    @abstractmethod
    def start(self) -> None:
        """Start the OCR process manager."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the OCR process manager."""
        pass
    
    @abstractmethod
    def send_ocr_command(self, command_type: str, parameters: Dict[str, Any]) -> str:
        """Send a command to the OCR service."""
        pass
    
    @abstractmethod
    def check_ocr_health(self) -> Dict[str, Any]:
        """Check OCR process health status."""
        pass
    
    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the OCR process manager is ready."""
        pass

# --- Import data types needed for the new interfaces ---
from modules.ocr.data_types import OCRParsedData, CleanedOcrResult
from interfaces.core.services import ILifecycleService

# --- Data Conditioning Interfaces ---

class IOCRDataConditioner(ABC):
    """
    Interface for the core OCR data conditioning logic.
    
    This processes raw OCR data into clean, validated
    trading signals with flicker filtering and decimal correction.
    """
    
    @abstractmethod
    async def condition_ocr_data(self, raw_ocr_event_data: OCRParsedData) -> CleanedOcrResult:
        """
        Process raw OCR data into clean trading data with world-class metadata architecture.
        
        Args:
            raw_ocr_event_data: Raw OCR parsed data object.
            
        Returns:
            CleanedOcrResult with metadata/payload separation and T4-T6 timing.
        """
        pass
    
    @abstractmethod
    def get_conditioner_metrics(self) -> Dict[str, float]:
        """
        Get performance and operational metrics from the conditioner.
        
        Returns:
            A dictionary with metrics like flicker_rejections, corrections, etc.
        """
        pass

class IOCRDataConditioningService(ILifecycleService):
    """
    Service wrapper for the OCR data conditioning pipeline.
    This service listens for raw OCR events and orchestrates their conditioning.
    """
    
    # This interface inherits start(), stop(), and is_ready from ILifecycleService.
    # Add any other public methods specific to the service wrapper here.
    pass

class IOCRScalpingSignalOrchestratorService(ILifecycleService):
    """
    Service for orchestrating the generation of trading signals from cleaned OCR data.
    """

    # This interface inherits start(), stop(), and is_ready from ILifecycleService.
    # Add any other public methods specific to the orchestrator here.
    @abstractmethod
    def enqueue_cleaned_snapshot_data(self, cleaned_event_data: Any, cleaned_event_id: Optional[str] = None):
        """Directly enqueues cleaned OCR data for processing, bypassing the main event bus."""
        pass