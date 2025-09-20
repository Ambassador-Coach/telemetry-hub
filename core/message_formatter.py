# core/message_formatter.py

import json
import time
import uuid
from typing import Dict, Any, Optional

from interfaces.core.services import IMessageFormatter, IConfigService

class MessageFormatter(IMessageFormatter):
    """
    A stateless utility that wraps raw data payloads in the standardized 
    TESTRADE message format for inter-process communication.
    """
    
    def __init__(self, config_service: IConfigService):
        self._config = config_service
    
    def create_message(self, payload: dict, event_type: str, correlation_id: str,
                       causation_id: Optional[str] = None) -> str:
        """
        Creates a standardized JSON message string for IPC streams.
        
        Args:
            payload: The raw data dictionary
            event_type: The string identifier for the event type
            correlation_id: The ID linking this event to an entire transaction
            causation_id: The ID of the direct event that caused this one
            
        Returns:
            A JSON string representing the complete message
        """
        message = {
            "metadata": {
                "event_type": event_type,
                "event_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "correlation_id": correlation_id,
                "causation_id": causation_id,
                "source_component": "TESTRADE_CORE",
                "version": "1.0"
            },
            "payload": payload
        }
        
        return json.dumps(message, default=str)