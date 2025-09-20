"""
Core Interfaces for IntelliSense Module

Defines abstract base classes for data sources and other core components
used throughout the IntelliSense system.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Iterator, Any, Optional, Dict, List, TYPE_CHECKING

# Use forward references instead of direct imports to avoid circular dependency
# Timeline event types are referenced as strings: 'OCRTimelineEvent', 'PriceTimelineEvent', 'BrokerTimelineEvent'

# Import specific types needed for refined interfaces
if TYPE_CHECKING:
    from intellisense.config.session_config import OCRTestConfig, PriceTestConfig, BrokerTestConfig, TestSessionConfig
    from intellisense.factories.interfaces import IDataSourceFactory
    from intellisense.core.types import OCRTimelineEvent, PriceTimelineEvent, BrokerTimelineEvent, VisualTimelineEvent
    from intellisense.analysis.ocr_results import OCRAnalysisResult
    from intellisense.analysis.price_results import PriceAnalysisResult
    from intellisense.analysis.broker_results import BrokerAnalysisResult


# --- Core Service Interfaces ---
class IEventBus(ABC):
    """Interface for event bus service."""

    @abstractmethod
    def publish(self, event: Any) -> None:
        """Publish an event to the event bus."""
        pass


# --- Data Source Interfaces (Refinement 3) ---
class IDataSource(ABC):
    """Base interface for all data sources in IntelliSense."""

    @abstractmethod
    def is_active(self) -> bool:
        """Check if data source is currently active."""
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the data source (e.g., begin replay, connect to live feed)."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the data source (e.g., halt replay, disconnect from feed)."""
        pass

    @abstractmethod
    def load_timeline_data(self) -> bool:
        """Loads data for replay. Returns True on success. No-op or True for live sources."""
        pass

    @abstractmethod
    def get_replay_progress(self) -> Dict[str, Any]:
        """
        Get standardized replay progress information for this source.
        Returns dict with keys: 'source_type', 'total_events', 'events_loaded',
                               'progress_percentage_loaded'.
        Live sources might report 0 total/loaded or reflect buffer status.
        """
        pass

    def cleanup(self) -> None:
        """Perform any final cleanup of resources (e.g., close file handles)."""
        pass


class IOCRDataSource(IDataSource):
    """Interface for OCR data sources (live or replay)."""

    @abstractmethod
    def get_data_stream(self) -> Iterator['OCRTimelineEvent']:
        """
        Get a stream of OCR timeline events.
        This should be an iterator that yields events according to their timestamps
        when in replay mode, or yields live events.
        """
        pass

    # load_timeline_data and get_replay_progress are inherited from IDataSource


class IPriceDataSource(IDataSource):
    """Interface for price data sources (live or replay)."""

    @abstractmethod
    def get_price_stream(self) -> Iterator['PriceTimelineEvent']:
        """
        Get a stream of price timeline events.
        This should be an iterator that yields events according to their timestamps
        when in replay mode, or yields live events.
        """
        pass

    # load_timeline_data and get_replay_progress are inherited from IDataSource


class IBrokerDataSource(IDataSource):
    """Interface for broker data sources (live or replay)."""

    @abstractmethod
    def get_response_stream(self) -> Iterator['BrokerTimelineEvent']:
        """Get stream of broker response timeline events."""
        pass

    @abstractmethod
    def set_delay_profile(self, profile_name: str) -> bool:
        """Set response delay profile for realistic simulation."""
        pass

    # load_timeline_data and get_replay_progress are inherited from IDataSource


class IVisualDataSource(IDataSource):
    """Interface for visual data sources (live or replay)."""

    @abstractmethod
    def get_visual_stream(self) -> Iterator['VisualTimelineEvent']:
        """
        Get a stream of visual timeline events.
        This should be an iterator that yields events according to their timestamps
        when in replay mode, or yields live events.
        """
        pass

    # load_timeline_data and get_replay_progress are inherited from IDataSource


# --- Intelligence Engine Interfaces (Enhanced for Chunk 3) ---
class IIntelligenceEngine(ABC):
    """Base interface for all intelligence engines."""

    @abstractmethod
    def initialize(self, config: Any, data_source_factory: 'IDataSourceFactory') -> bool:
        """Initialize the engine with configuration and data source factory."""
        pass

    @abstractmethod
    def setup_for_session(self, session_config: Any) -> bool:
        """Sets up the engine for a specific test session, including its data source."""
        pass

    @abstractmethod
    def start(self) -> None:
        """Start the intelligence engine."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop the intelligence engine."""
        pass

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        """Get current status of the intelligence engine."""
        pass


class IOCRIntelligenceEngine(IIntelligenceEngine):
    """Interface for OCR intelligence engines with analysis capabilities."""

    @abstractmethod
    def initialize(self,
                   config: 'OCRTestConfig',
                   data_source_factory: 'IDataSourceFactory',
                   snapshot_interpreter_instance: Any,  # Actual SnapshotInterpreterService
                   position_manager_instance: Any       # Actual PositionManager
                   ) -> bool:
        """
        Initialize the OCR intelligence engine with specific dependencies.

        Args:
            config: OCR test configuration
            data_source_factory: Factory for creating data sources
            snapshot_interpreter_instance: SnapshotInterpreter for position analysis
            position_manager_instance: PositionManager for expected position data

        Returns:
            True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    def setup_for_session(self, session_config: 'TestSessionConfig') -> bool:
        """
        Set up the engine for a specific test session.

        Args:
            session_config: Test session configuration

        Returns:
            True if setup successful, False otherwise
        """
        pass

    @abstractmethod
    def process_event(self, event: 'OCRTimelineEvent') -> 'OCRAnalysisResult':
        """
        Process a single OCRTimelineEvent and return analysis results.

        Args:
            event: OCR timeline event to process

        Returns:
            OCRAnalysisResult containing validation and performance analysis
        """
        pass

    @abstractmethod
    def get_analysis_results(self) -> List['OCRAnalysisResult']:
        """
        Get all analysis results collected during the session.

        Returns:
            List of OCRAnalysisResult objects from processed events
        """
        pass


class IPriceIntelligenceEngine(IIntelligenceEngine):
    """Interface for price intelligence engines with analysis capabilities."""

    @abstractmethod
    def initialize(self,
                   config: 'PriceTestConfig',
                   data_source_factory: 'IDataSourceFactory',
                   real_services: Dict[str, Any]  # Expects e.g. {'price_repository': RealPriceRepository}
                   ) -> bool:
        """
        Initialize the price intelligence engine with specific dependencies.

        Args:
            config: Price test configuration
            data_source_factory: Factory for creating data sources
            real_services: Dictionary of real services for price analysis

        Returns:
            True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    def setup_for_session(self, session_config: 'TestSessionConfig') -> bool:
        """
        Set up the engine for a specific test session.

        Args:
            session_config: Test session configuration

        Returns:
            True if setup successful, False otherwise
        """
        pass

    @abstractmethod
    def process_event(self, event: 'PriceTimelineEvent') -> 'PriceAnalysisResult':
        """
        Process a single PriceTimelineEvent and return analysis results.

        Args:
            event: Price timeline event to process

        Returns:
            PriceAnalysisResult containing validation and performance analysis
        """
        pass

    @abstractmethod
    def get_analysis_results(self) -> List['PriceAnalysisResult']:
        """
        Get all analysis results collected during the session.

        Returns:
            List of PriceAnalysisResult objects from processed events
        """
        pass


class IBrokerIntelligenceEngine(IIntelligenceEngine):
    """Interface for broker intelligence engines with analysis capabilities."""

    @abstractmethod
    def initialize(self,
                   config: 'BrokerTestConfig',
                   data_source_factory: 'IDataSourceFactory',
                   real_services: Dict[str, Any]  # Expects {'event_bus': IEventBus,
                                                  # 'order_repository': TestableOrderRepository,
                                                  # 'position_manager': TestablePositionManager}
                   ) -> bool:
        """
        Initialize the broker intelligence engine with specific dependencies.

        Args:
            config: Broker test configuration
            data_source_factory: Factory for creating data sources
            real_services: Dictionary of real services for broker analysis

        Returns:
            True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    def setup_for_session(self, session_config: 'TestSessionConfig') -> bool:
        """
        Set up the engine for a specific test session.

        Args:
            session_config: Test session configuration

        Returns:
            True if setup successful, False otherwise
        """
        pass

    @abstractmethod
    def process_event(self, event: 'BrokerTimelineEvent') -> 'BrokerAnalysisResult':
        """
        Process a single BrokerTimelineEvent and return analysis results.

        Args:
            event: Broker timeline event to process

        Returns:
            BrokerAnalysisResult containing validation and performance analysis
        """
        pass

    @abstractmethod
    def get_analysis_results(self) -> List['BrokerAnalysisResult']:
        """
        Get all analysis results collected during the session.

        Returns:
            List of BrokerAnalysisResult objects from processed events
        """
        pass


# --- New Clean Architecture Interfaces ---

class IIntelliSenseApplicationCore(ABC):
    """
    Interface defining the essential contract provided by IntelliSenseApplicationCore
    to other IntelliSense components and potentially to TESTRADE services it manages.
    """

    @abstractmethod
    def activate_capture_mode(self) -> bool:
        """Activates data capture mode, swapping in EnhancedCapture components."""
        pass

    @abstractmethod
    def deactivate_capture_mode(self) -> None:
        """Deactivates data capture mode, restoring original components."""
        pass

    @abstractmethod
    def activate_testable_analysis_mode(self) -> bool:
        """Activates testable analysis mode, swapping in TestableAnalysis components."""
        pass

    @abstractmethod
    def deactivate_testable_analysis_mode(self) -> None:
        """Deactivates testable analysis mode, restoring original components."""
        pass

    @abstractmethod
    def get_real_services_for_intellisense(self) -> Dict[str, Any]:
        """
        Provides access to currently installed service instances (original, enhanced, or testable)
        needed by IntelliSense engines or other components.
        """
        pass

    @abstractmethod
    def create_capture_session(self, capture_session_config: Dict[str, Any]) -> Any:
        """Creates a new ProductionDataCaptureSession instance."""
        pass

    @abstractmethod
    def set_active_correlation_logger(self, logger: Optional[Any]) -> None:
        """Sets or clears the active CorrelationLogger for the current capture session."""
        pass

    @abstractmethod
    def _configure_correlation_logging_for_production_services(self, enable: bool) -> None:
        """
        (Internal but exposed for ProductionDataCaptureSession)
        Reconfigures core production services (like RiskManagementService, TradeManagerService)
        to use/stop using the active correlation logger.
        """
        pass

    @abstractmethod
    def set_active_feedback_isolator(self, isolator: Optional[Any]) -> None:
        """Sets or clears the active FeedbackIsolationManager for the current capture session."""
        pass

    @abstractmethod
    def get_active_feedback_isolator(self) -> Optional[Any]:
        """Gets the currently active FeedbackIsolationManager."""
        pass

    @property
    @abstractmethod
    def intellisense_test_mode_active(self) -> bool:
        """Flag indicating if IntelliSense test/analysis mode is active."""
        pass

    @property
    @abstractmethod
    def config(self) -> Any:
        """Provides access to the main application configuration."""
        pass

    @property
    @abstractmethod
    def event_bus(self) -> 'IEventBus':
        """Provides access to the main TESTRADE event bus."""
        pass


class IIntelliSenseMasterController(ABC):
    """
    Interface for the IntelliSenseMasterController, orchestrating test sessions,
    engines, and replay.
    """

    @abstractmethod
    def create_test_session(self, session_config: Any) -> str:
        """
        Creates a new test session based on the provided configuration.
        Returns the session_id.
        """
        pass

    @abstractmethod
    def load_session_data(self, session_id: str) -> bool:
        """
        Loads data for the specified session into replay sources.
        Returns True on success.
        """
        pass

    @abstractmethod
    def start_replay_session(self, session_id: str) -> bool:
        """
        Starts the replay for a loaded and prepared test session.
        Returns True if start initiated successfully.
        """
        pass

    @abstractmethod
    def stop_replay_session(self, session_id: str) -> bool:
        """
        Requests to stop an active replay session.
        Returns True if stop initiated successfully.
        """
        pass

    @abstractmethod
    def get_session_status(self, session_id: str) -> Optional[Any]:
        """
        Retrieves the status and details of a specific test session.
        Returns the TestSession object or a DTO, None if not found.
        """
        pass

    @abstractmethod
    def list_sessions(self) -> List[Any]:
        """Returns a list of all registered test sessions or their summaries."""
        pass

    @abstractmethod
    def get_session_results_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a summary of execution results for a session.
        """
        pass


class IPositionManager(ABC):
    """Interface for position management operations."""

    @abstractmethod
    def get_cached_position_state(self, symbol: str) -> Dict[str, Any]:
        """Get the current position state for a symbol."""
        pass

    @abstractmethod
    def update_position(self, symbol: str, shares: float, price: float) -> None:
        """Update position with new shares and price."""
        pass


class IOrderRepository(ABC):
    """Interface for order repository operations."""

    @abstractmethod
    def get_cached_order_state(self, order_id: str) -> Dict[str, Any]:
        """Get the current order state."""
        pass

    @abstractmethod
    def add_order(self, order_data: Dict[str, Any]) -> str:
        """Add a new order and return order ID."""
        pass


class IValidationEngine(ABC):
    """Interface for validation operations."""

    @abstractmethod
    def validate_stimulus_field(self, field_value: Any, expected_value: Any, operator: str) -> bool:
        """Validate a stimulus field using the specified operator."""
        pass

    @abstractmethod
    def validate_position_state(self, symbol: str, field_path: str, expected_value: Any, operator: str) -> bool:
        """Validate a position manager state field."""
        pass

    @abstractmethod
    def validate_pipeline_status(self, pipeline_data: Dict[str, Any], status_type: str, expected_value: Any) -> bool:
        """Validate pipeline status (timeout, completion, etc.)."""
        pass
