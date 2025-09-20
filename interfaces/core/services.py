# interfaces/core/services.py

"""
Core Service Interfaces - Foundational Contracts for TESTRADE

This module defines the abstract base classes (interfaces) for the most
fundamental, cross-cutting services in the application. These services
form the bedrock of the infrastructure layer.

By depending on these contracts, services remain decoupled from concrete
implementations, adhering to the Dependency Inversion Principle.
"""

from abc import ABC, abstractmethod
from typing import Any, Type, Callable, Optional, Dict


# --- Lifecycle Management ---

class ILifecycleService(ABC):
    """
    A standard contract for services with a manageable lifecycle (start/stop).
    The ServiceLifecycleManager discovers and orchestrates all services that
    implement this interface, ensuring a deterministic startup and shutdown.
    """

    @abstractmethod
    def start(self) -> None:
        """
        Starts the service's background activity. This can include subscribing
        to events, starting threads, or opening connections.
        This method must be idempotent.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        Stops the service's background activity gracefully. This includes
        unsubscribing from events, joining threads, and releasing resources.
        This method must be idempotent.
        """
        pass

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """
        Returns True only when the service is fully initialized, connected,
        and ready to perform its primary function.
        """
        pass


# --- Communication & Messaging ---

class IEventBus(ABC):
    """
    An interface for the central, in-memory publish-subscribe event system.
    This enables extremely loose coupling between components within the application.
    """

    @abstractmethod
    def subscribe(self, event_type: Type[Any], handler: Callable[[Any], None]) -> None:
        """Registers a handler for a specific event type."""
        pass

    @abstractmethod
    def unsubscribe(self, event_type: Type[Any], handler: Callable[[Any], None]) -> None:
        """Unregisters a handler for a specific event type."""
        pass

    @abstractmethod
    def publish(self, event: Any) -> None:
        """Publishes an event instance to all subscribed handlers."""
        pass


class IBulletproofBabysitterIPCClient(ABC):
    """
    An interface for the client that communicates with the external 'Babysitter'
    process. This client handles robust, non-blocking message sending to Redis
    via the Babysitter.
    """
    @abstractmethod
    def send_data(self, target_redis_stream: str, data_json_string: str) -> bool:
        """
        Sends a JSON string to the Babysitter for publishing to a Redis stream.
        This method is non-blocking and handles connection/buffering internally.
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Closes the connection to the Babysitter process."""
        pass


class IMessageFormatter(ABC):
    """
    An interface for a stateless utility that wraps raw data payloads in the
    standardized TESTRADE message format (metadata + payload).
    """
    @abstractmethod
    def create_message(self, payload: dict, event_type: str, correlation_id: str,
                       causation_id: Optional[str] = None) -> str:
        """
        Creates a standardized JSON message string.

        Args:
            payload: The raw data dictionary.
            event_type: The string identifier for the event type.
            correlation_id: The ID linking this event to an entire transaction.
            causation_id: The ID of the direct event that caused this one.

        Returns:
            A JSON string representing the complete message.
        """
        pass


# --- Configuration & Utilities ---

class IConfigService(ABC):
    """
    An interface for providing application-wide configuration. This abstracts
    away the source of the configuration (e.g., file, env vars).
    """
    # This interface is often implemented directly by the GlobalConfig dataclass.
    # It exists to formalize the dependency contract.
    pass


class IPerformanceBenchmarker(ABC):
    """An interface for a high-performance metrics and timing utility."""

    @abstractmethod
    def capture_metric(self, metric_name: str, value: float, context: Optional[Dict[str, Any]] = None) -> None:
        """Captures a single metric value (e.g., a processing time in ms)."""
        pass

    @abstractmethod
    def get_stats(self, metric_name: str) -> Optional[Dict[str, Any]]:
        """Retrieves aggregated statistics for a named metric."""
        pass


class IPipelineValidator(ABC):
    """An interface for a diagnostic utility to trace data flow through a pipeline."""
    # This is primarily a debugging and testing tool.
    pass




class ICommandListener(ABC):
    """Interface for command listener."""
    pass


class IIPCManager(ABC):
    """Interface for IPC manager."""
    pass


class IHealthMonitor(ABC):
    """Interface for health monitor."""
    pass


class IPerformanceMetricsService(ABC):
    """Interface for performance metrics service."""
    pass


class IBulkDataService(ABC):
    """Interface for handling large binary data separately from telemetry."""
    
    @abstractmethod
    def store_image(self, image_data: str, image_type: str, correlation_id: str) -> str:
        """Store base64 image data and return a reference ID."""
        pass
    
    @abstractmethod
    def retrieve_image(self, reference_id: str) -> Optional[str]:
        """Retrieve image data by reference ID."""
        pass
    
    @abstractmethod
    def stop(self):
        """Stop the bulk data service."""
        pass


class IServiceLifecycleManager(ABC):
    """Interface for service lifecycle manager."""
    pass


class IEmergencyGUIService(ABC):
    """Interface for emergency GUI service."""
    pass


# Re-export for convenience
__all__ = [
    'ILifecycleService',
    'IEventBus',
    'IConfigService',
    'IMessageFormatter',
    'IBulletproofBabysitterIPCClient',
    'IPerformanceBenchmarker',
    'IPipelineValidator',
    'ICommandListener',
    'IIPCManager',
    'IHealthMonitor',
    'IPerformanceMetricsService',
    'IBulkDataService',
    'IServiceLifecycleManager',
    'IEmergencyGUIService'
]