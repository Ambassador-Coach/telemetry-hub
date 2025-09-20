"""
ROI Service - Handles ROI updates from OCR process

This service processes ROI state updates received from the OCR process via pipe,
persists them to control.json, and publishes them to Redis for GUI consumption.
"""

import logging
import json
import os
import time
from typing import Dict, Any, Optional, List, TYPE_CHECKING
from interfaces.logging.services import ICorrelationLogger
from interfaces.core.services import IConfigService, IBulletproofBabysitterIPCClient
from interfaces.ocr.services import IROIService

if TYPE_CHECKING:
    from core.application_core import ApplicationCore

logger = logging.getLogger(__name__)


class ROIService(IROIService):
    """
    Service for handling ROI (Region of Interest) updates from the OCR process.
    
    Responsibilities:
    - Process ROI updates received via pipe from OCR process
    - Persist ROI coordinates to control.json configuration
    - Publish ROI updates to Redis stream for GUI consumption
    """
    
    def __init__(self, config_service: IConfigService, correlation_logger: Optional[ICorrelationLogger] = None, ipc_client: IBulletproofBabysitterIPCClient = None):
        """
        Initialize the ROI service.
        
        Args:
            config_service: Configuration service for accessing config data
            correlation_logger: Correlation logger for publishing events
            ipc_client: IPC client for legacy compatibility (optional)
        """
        self._config_service = config_service
        self.config = config_service.get_config()  # Keep backward compatibility
        self.current_roi: List[int] = config_service.get_roi_coordinates()
        self._correlation_logger = correlation_logger
        self._ipc_client = ipc_client
        logger.info(f"ROIService initialized with clean DI. Initial ROI: {self.current_roi}")
    
    def process_roi_update_from_ocr(self, roi_update_data: Dict[str, Any]):
        """
        Process ROI update received from OCR process via pipe.
        
        Args:
            roi_update_data: Dictionary containing ROI update data with keys:
                - roi: List of 4 integers [x1, y1, x2, y2]
                - source: Source of the update (e.g., "OCRProcessInternalUpdate")
                - timestamp: Timestamp of the update
        """
        new_roi = roi_update_data.get("roi")
        source = roi_update_data.get("source", "OCRProcess")
        timestamp = roi_update_data.get("timestamp", time.time())
        
        if not (new_roi and isinstance(new_roi, list) and len(new_roi) == 4):
            logger.warning(f"ROIService: Received invalid ROI data: {new_roi}")
            return
        
        logger.info(f"ROIService: Processing ROI update from {source}: {new_roi}")
        self.current_roi = [int(c) for c in new_roi]  # Ensure integers
        
        # 1. Publish to Redis stream via Babysitter
        self._publish_roi_to_redis(source, timestamp)
        
        # 2. Persist to control.json
        self._save_roi_to_config_file()
    
    def _publish_roi_to_redis(self, source: str, timestamp: float):
        """
        Publish ROI update to Redis stream via Babysitter IPC client.
        
        Args:
            source: Source of the ROI update
            timestamp: Timestamp of the update
        """
        if not self._correlation_logger:
            logger.error("ROIService: CorrelationLogger not available. Cannot publish ROI update to Redis.")
            return
        
        try:
            redis_payload = {
                "roi": self.current_roi,
                "source": f"ROIService_from_{source}",
                "update_timestamp": timestamp
            }
            
            redis_event_id = f"roi_update_{int(time.time_ns())}"
            
            # Check if IPC data dumping is disabled
            if not self._config_service.is_feature_enabled('ENABLE_IPC_DATA_DUMP'):
                logger.debug(f"TANK_MODE: Skipping IPC send for ROI update - ENABLE_IPC_DATA_DUMP is False")
                return
            
            target_stream = self._config_service.get_redis_stream_name('roi_updates', 'testrade:roi-updates')
            
            # Use correlation logger if available
            if self._correlation_logger:
                telemetry_data = {
                    "payload": redis_payload,
                    "correlation_id": redis_event_id,
                    "causation_id": None
                }
                
                self._correlation_logger.log_event(
                    source_component="ROIService",
                    event_name="ROIUpdate",
                    payload=telemetry_data
                )
                
                logger.info(f"ROIService: ROI update for {self.current_roi} logged.")
            else:
                logger.warning("ROIService: Correlation logger not available for ROI update.")
                
        except Exception as e:
            logger.error(f"ROIService: Error publishing ROI update to Redis: {e}", exc_info=True)
    
    def _save_roi_to_config_file(self):
        """
        Save ROI coordinates to control.json configuration file.
        """
        try:
            from utils.global_config import save_global_config, CONFIG_FILE_PATH
            
            # Update the in-memory config object first
            self.config.ROI_COORDINATES = self.current_roi
            
            # Use injected IPC client for config change publishing
            ipc_client = self._ipc_client

            # Save the entire config object
            save_global_config(self.config, custom_path=CONFIG_FILE_PATH, ipc_client=ipc_client, change_source_override="roi_service_update")
            logger.info(f"ROIService: Successfully saved ROI {self.current_roi} to {CONFIG_FILE_PATH}.")
            
        except Exception as e:
            logger.error(f"ROIService: Failed to save ROI to config file: {e}", exc_info=True)
    
    def get_current_roi(self) -> List[int]:
        """
        Get the current ROI coordinates.
        
        Returns:
            List of 4 integers representing ROI coordinates [x1, y1, x2, y2]
        """
        return self.current_roi.copy()
