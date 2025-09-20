"""
OCR Parameters Service

This service handles OCR parameter updates from the OCR process,
persists them to control.json, and publishes updates to Redis.
"""

import logging
import time
from typing import Dict, Any, TYPE_CHECKING, Optional

from interfaces.ocr.services import IOCRParametersService
from interfaces.logging.services import ICorrelationLogger
from interfaces.core.services import IConfigService, IBulletproofBabysitterIPCClient

if TYPE_CHECKING:
    from core.application_core import ApplicationCore

logger = logging.getLogger(__name__)


class OCRParametersService(IOCRParametersService):
    """
    Service for handling OCR parameter updates from the OCR process.
    
    Responsibilities:
    - Process OCR parameter updates received via pipe from OCR process
    - Persist OCR parameters to control.json configuration
    - Publish OCR parameter updates to Redis stream for GUI consumption
    """
    
    def __init__(self, config_service: IConfigService, correlation_logger: Optional[ICorrelationLogger] = None, ipc_client: IBulletproofBabysitterIPCClient = None):
        """
        Initialize the OCR parameters service.
        
        Args:
            config_service: Configuration service for accessing config data
            correlation_logger: Correlation logger for publishing events
            ipc_client: IPC client for legacy compatibility (optional)
        """
        self._config_service = config_service
        self._correlation_logger = correlation_logger
        self._ipc_client = ipc_client
        self.current_parameters: Dict[str, Any] = {}
        logger.info("OCRParametersService initialized with clean DI")
    
    def process_ocr_parameters_update_from_ocr(self, parameters_update_data: Dict[str, Any]):
        """
        Process OCR parameters update received from OCR process via pipe.
        
        Args:
            parameters_update_data: Dictionary containing OCR parameters update data with keys:
                - parameters: Dict of OCR parameters to update
                - source: Source of the update (e.g., "OCRZMQCommandListener")
                - timestamp: Timestamp of the update
        """
        parameters = parameters_update_data.get("parameters", {})
        source = parameters_update_data.get("source", "OCRProcess")
        timestamp = parameters_update_data.get("timestamp", time.time())
        
        if not parameters:
            logger.warning("OCRParametersService: Received empty parameters data")
            return
        
        logger.info(f"OCRParametersService: Processing OCR parameters update from {source}: {list(parameters.keys())}")
        self.current_parameters.update(parameters)
        
        # 1. Publish to Redis stream via Babysitter
        self._publish_parameters_to_redis(source, timestamp)
        
        # 2. Persist to control.json
        self._save_parameters_to_config_file(parameters)
        
        logger.info(f"OCRParametersService: Successfully processed {len(parameters)} OCR parameters from {source}")
    
    def get_current_parameters(self) -> Dict[str, Any]:
        """
        Get the current OCR parameters.
        
        Returns:
            Dictionary of current OCR parameters
        """
        return self.current_parameters.copy()
    
    def _publish_parameters_to_redis(self, source: str, timestamp: float):
        """
        Publish OCR parameters update to Redis stream via Babysitter.
        
        Args:
            source: Source of the update
            timestamp: Timestamp of the update
        """
        try:
            # Clean architecture - check correlation logger availability
            if not self._correlation_logger:
                logger.error("OCRParametersService: CorrelationLogger not available. Cannot publish OCR parameters update.")
                return
            
            # Create Redis message payload
            payload = {
                "parameters": self.current_parameters,
                "source": source,
                "timestamp": timestamp,
                "component": "OCRParametersService"
            }
            
            # TANK Mode: Check if IPC data dumping is disabled
            if hasattr(self._config_service, 'enable_ipc_data_dump') and not self._config_service.enable_ipc_data_dump:
                logger.debug(f"TANK_MODE: Skipping IPC send for OCR parameters update")
                return  # Skip IPC send but continue processing

            target_stream = getattr(self._config_service, 'redis_stream_ocr_parameters_updates', 'testrade:ocr-parameters-updates')
            
            # Use correlation logger to publish
            self._correlation_logger.log_event(
                source_component="OCRParametersService",
                event_name="OCRParametersUpdate",
                payload=payload
            )
            
            logger.info(f"OCRParametersService: OCR parameters update logged.")
                
        except Exception as e:
            logger.error(f"OCRParametersService: Error publishing OCR parameters update to Redis: {e}", exc_info=True)
    
    def _save_parameters_to_config_file(self, parameters: Dict[str, Any]):
        """
        Save OCR parameters to control.json configuration file.
        
        Args:
            parameters: Dictionary of OCR parameters to save
        """
        try:
            from utils.global_config import update_config_key
            
            saved_count = 0
            for key, value in parameters.items():
                if key.startswith('ocr_'):
                    success = update_config_key(key, value)
                    if success:
                        saved_count += 1
                        logger.debug(f"OCRParametersService: Saved parameter {key} = {value} to control.json")
                    else:
                        logger.warning(f"OCRParametersService: Failed to save parameter {key} to control.json")
            
            logger.info(f"OCRParametersService: Successfully saved {saved_count}/{len(parameters)} OCR parameters to control.json")
            
        except Exception as e:
            logger.error(f"OCRParametersService: Failed to save OCR parameters to config file: {e}", exc_info=True)
