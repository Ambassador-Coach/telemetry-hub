# C:\TESTRADE\core\telemetry_interfaces.py

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class ITelemetryService(ABC):
    """
    Interface for a fire-and-forget telemetry service.
    Implementations of this service handle queuing and background publishing
    of non-critical observability data without blocking the caller.
    """
    @abstractmethod
    def enqueue(self,
                source_component: str,
                event_type: str,
                payload: Dict[str, Any],
                stream_override: Optional[str] = None,
                origin_timestamp_s: Optional[float] = None) -> None:
        """
        Enqueues a telemetry event for asynchronous publishing.
        This method MUST be non-blocking.
        
        Args:
            source_component: Name of the component generating the telemetry
            event_type: Type of event (e.g., 'RISK_DECISION', 'POSITION_UPDATE')
            payload: The telemetry data payload
            stream_override: Optional Redis stream name override
            origin_timestamp_s: Golden Timestamp - when the data originally occurred
        """
        pass
    
    @abstractmethod
    def publish_bootstrap_data(self, config_dict: Dict[str, Any]) -> None:
        """
        Publish bootstrap configuration data to Redis.
        
        Args:
            config_dict: Complete configuration dictionary
        """
        pass
    
    @abstractmethod
    def publish_health_data(self, health_data: Dict[str, Any]) -> None:
        """
        Publish core health monitoring data to Redis.
        
        Args:
            health_data: Health status information
        """
        pass
    
    @abstractmethod
    def publish_command_response(self, command_data: Dict[str, Any]) -> None:
        """
        Publish command response data to GUI.
        
        Args:
            command_data: Command response information
        """
        pass
    
    @abstractmethod
    def publish_raw_ocr_data(self, ocr_data: Dict[str, Any]) -> None:
        """
        Publish raw OCR data to Redis streams.
        
        Args:
            ocr_data: Raw OCR data payload
        """
        pass