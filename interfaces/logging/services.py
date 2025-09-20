# interfaces/logging/services.py
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class ICorrelationLogger(ABC):
    """
    An abstract interface for a high-performance, fire-and-forget logger.

    This logger is designed for capturing granular, structured events from
    within the core 'TESTRADE' engine without coupling the engine to any
    specific transport mechanism (like telemetry, disk, or console).
    """

    @abstractmethod
    def log_event(
        self,
        source_component: str,
        event_name: str,
        payload: Dict[str, Any],
        stream_override: Optional[str] = None
    ) -> None:
        """
        Logs a structured event.

        Implementations of this method MUST be non-blocking and thread-safe.

        Args:
            source_component: The name of the component logging the event (e.g., 'RiskManagementService').
            event_name: The name of the specific event or milestone (e.g., 'T13_RiskCheckStart').
            payload: A dictionary containing the structured data for the event.
                     This payload must be JSON-serializable.
            stream_override: Optional. The specific Redis stream to publish to.
                           If None, the implementation will use a default stream.
        """
        pass