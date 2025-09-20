"""
Controller Interfaces for IntelliSense Module

Defines abstract base classes for intelligence engines, timeline synchronization,
and performance monitoring within the IntelliSense system.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from intellisense.core.types import TestSessionConfig, TestSession
    from intellisense.core.enums import OperationMode, TestStatus
else:
    # Runtime placeholders to avoid import issues
    TestSessionConfig = Any
    TestSession = Any
    OperationMode = Any
    TestStatus = Any


class IIntelligenceEngine(ABC):
    """Base interface for IntelliSense Intelligence Engines."""
    
    @abstractmethod
    def initialize(self, config: Any, data_source_factory: Any) -> None:
        """
        Initialize the intelligence engine with configuration and data source factory.
        
        Args:
            config: Specific part of IntelliSenseConfig relevant to this engine
            data_source_factory: Factory for creating data sources
        """
        pass

    @abstractmethod
    def start_session(self, session_id: str) -> None:
        """
        Start an IntelliSense session.
        
        Args:
            session_id: Unique identifier for the session
        """
        pass

    @abstractmethod
    def stop_session(self) -> None:
        """Stop the current IntelliSense session."""
        pass

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """
        Get current status of the intelligence engine.
        
        Returns:
            Dictionary containing status information
        """
        pass


class IOCRIntelligenceEngine(IIntelligenceEngine):
    """Interface for the OCR Intelligence Engine."""
    # Specific methods for OCR engine if any, beyond base
    pass


class IPriceIntelligenceEngine(IIntelligenceEngine):
    """Interface for the Price Intelligence Engine."""
    # Specific methods for Price engine if any
    pass


class IBrokerIntelligenceEngine(IIntelligenceEngine):
    """Interface for the Broker Intelligence Engine."""
    # Specific methods for Broker engine if any
    pass


# Placeholder for TimelineSynchronizer and PerformanceMonitor interfaces
# These might be concrete classes directly instantiated for now.

class ITimelineSynchronizer(ABC):
    """Interface for timeline synchronization across intelligence engines."""
    
    @abstractmethod
    def initialize(self, 
                   ocr_engine: IOCRIntelligenceEngine, 
                   price_engine: IPriceIntelligenceEngine, 
                   broker_engine: IBrokerIntelligenceEngine) -> None:
        """
        Initialize the timeline synchronizer with intelligence engines.
        
        Args:
            ocr_engine: OCR intelligence engine instance
            price_engine: Price intelligence engine instance
            broker_engine: Broker intelligence engine instance
        """
        pass
    
    @abstractmethod
    def synchronize_and_start_replay(self, master_speed_factor: float) -> None:
        """
        Synchronize timelines and start replay with specified speed factor.
        
        Args:
            master_speed_factor: Speed multiplier for replay (1.0 = real-time)
        """
        pass

    @abstractmethod
    def stop_replay(self) -> None:
        """Stop the timeline replay."""
        pass


class IPerformanceMonitor(ABC):
    """Interface for IntelliSense-specific performance monitoring."""
    
    @abstractmethod
    def start_monitoring_session(self, session_id: str) -> None:
        """
        Start monitoring performance for a session.
        
        Args:
            session_id: Unique identifier for the session
        """
        pass

    @abstractmethod
    def stop_monitoring_session(self) -> None:
        """Stop monitoring the current session."""
        pass

    @abstractmethod
    def get_session_metrics(self) -> Dict[str, Any]:
        """
        Get performance metrics for the current session.
        
        Returns:
            Dictionary containing performance metrics
        """
        pass


class IIntelliSenseMasterController(ABC):
    """
    Interface for the IntelliSenseMasterController, responsible for orchestrating
    test sessions, intelligence engines, and data replay/capture operations.
    """

    @abstractmethod
    def create_test_session(self, session_config: 'TestSessionConfig') -> str:
        """
        Creates a new IntelliSense test session based on the provided configuration.

        Args:
            session_config: The configuration for the test session.

        Returns:
            str: The unique session_id for the created session.

        Raises:
            IntelliSenseSessionError: If session creation fails or config is invalid.
        """
        pass

    @abstractmethod
    def load_session_data(self, session_id: str) -> bool:
        """
        Loads all necessary data for the specified test session into the
        relevant replay data sources. This typically involves instructing
        the configured intelligence engines to set up their sources.

        Args:
            session_id: The ID of the session to load data for.

        Returns:
            bool: True if data loading was successfully initiated for critical sources, False otherwise.

        Raises:
            IntelliSenseSessionError: If the session is not found or not in a valid state.
        """
        pass

    @abstractmethod
    def start_replay_session(self, session_id: str) -> bool:
        """
        Starts the replay process for a previously loaded and prepared test session.
        This typically involves starting the timeline generator and intelligence engines.

        Args:
            session_id: The ID of the session to start.

        Returns:
            bool: True if the replay session was successfully initiated, False otherwise.

        Raises:
            IntelliSenseSessionError: If session not found, not ready, or already running.
        """
        pass

    @abstractmethod
    def stop_replay_session(self, session_id: str) -> bool:
        """
        Requests to stop an active replay session gracefully.

        Args:
            session_id: The ID of the session to stop.

        Returns:
            bool: True if the stop request was successfully processed, False otherwise
                  (e.g., session not found or not running).
        """
        pass

    @abstractmethod
    def get_session_status(self, session_id: str) -> Optional['TestSession']: # Or a dedicated SessionStatusDTO
        """
        Retrieves the current status and details of a specific test session.

        Args:
            session_id: The ID of the session to query.

        Returns:
            Optional[TestSession]: The TestSession object containing status and details,
                                   or None if the session_id is not found.
        """
        pass

    @abstractmethod
    def list_sessions(self) -> List['TestSession']: # Or List[SessionSummaryDTO]
        """
        Returns a list of all registered test sessions (or summaries).
        The source of this list will be Redis once implemented, otherwise in-memory.
        """
        pass

    @abstractmethod
    def get_session_results_summary(self, session_id: str) -> Optional[Dict[str, Any]]: # Or a dedicated ResultsSummaryDTO
        """
        Retrieves a summary of the execution results for a completed or stopped test session.
        This may include overall status, number of steps, validation outcomes, and key latencies.

        Args:
            session_id: The ID of the session for which to get results.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing the results summary,
                                      or None if results are not available or session not found.
        """
        pass

    @abstractmethod
    def register_cleanup_hook(self, cleanup_func: Callable[[], None]) -> None:
        """
        Registers a function to be called during the session cleanup process,
        typically when a session is stopped or the controller is shut down.

        Args:
            cleanup_func: A callable that takes no arguments and returns None.
        """
        pass

    @abstractmethod
    def delete_session(self, session_id: str) -> bool:
        """
        Deletes a test session and its persisted data.

        Args:
            session_id: The ID of the session to delete.

        Returns:
            True if deletion was successful or session did not exist,
            False if deletion failed.
        """
        pass

    @abstractmethod
    def shutdown(self) -> None:
        """
        Performs graceful shutdown of the master controller and all managed sessions.
        This method should:
        - Stop any active sessions
        - Stop all engines (OCR, Price, Broker)
        - Execute all registered cleanup hooks
        - Close external connections (Redis, etc.)
        - Release all resources

        This method is essential for proper lifecycle management when the controller
        is managed by external components (API servers, main applications, etc.).
        """
        pass
