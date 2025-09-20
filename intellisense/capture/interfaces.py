"""
IntelliSense Data Capture Interfaces

This module defines the protocols for observing and capturing data from live system components
during operation. These interfaces enable the IntelliSense system to record correlation logs
for later replay and analysis.
"""

from abc import ABC, abstractmethod
from typing import Protocol, Dict, Any, runtime_checkable, Optional, List, TYPE_CHECKING # Added Optional

# Assuming these types are defined and importable
# from intellisense.core.types import CleanedOCRSnapshotEventData # Actual output of conditioning
# For placeholder:
if 'CleanedOCRSnapshotEventData' not in globals():
    class CleanedOCRSnapshotEventData: snapshots: Dict[str, Dict[str,Any]] = {} # Simplified placeholder

from .feedback_isolation_types import PositionMockProfile, PositionState

# Using @runtime_checkable to allow isinstance() checks if needed,
# though primary usage will be type hinting.

class ICorrelationLogger(ABC):
    """
    Interface for the CorrelationLogger service, responsible for logging
    events with correlation tracking for IntelliSense.
    """

    @abstractmethod
    def log_event(self,
                  source_sense: str,
                  event_payload: Dict[str, Any],
                  source_component_name: Optional[str] = None,
                  perf_timestamp_ns_override: Optional[int] = None
                 ) -> None:
        """
        Logs an event with associated metadata.

        Args:
            source_sense: The 'sense' or domain of the event (e.g., "OCR", "PRICE", "BROKER", "RISK_PROCESSING").
            event_payload: A dictionary containing the actual data of the event.
                           Should include 'correlation_id' if applicable.
            source_component_name: Name of the component that originated this loggable event.
            perf_timestamp_ns_override: Optional performance counter timestamp to use instead of a new one.
        """
        pass

    @abstractmethod
    def get_session_path(self) -> Optional[str]:
        """Returns the file system path for the current logging session."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Closes the logger, ensuring all buffered entries are flushed."""
        pass

    @abstractmethod
    def is_active(self) -> bool:
        """Returns True if the logger is currently active and logging."""
        pass


@runtime_checkable
class IPriceDataListener(Protocol):
    """Protocol for price data observation during capture."""
    
    def on_trade_data(self, source_component_name: str, trade_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """
        Called when trade data is processed by an observed component.
        
        Args:
            source_component_name: Name of the component that processed the data (e.g., "PriceRepository").
            trade_data: Dictionary containing the trade event data (symbol, price, volume, etc.).
            processing_latency_ns: Latency (in ns) for the source component to process this event.
        """
        ...  # Protocol methods have '...' as their body
    
    def on_quote_data(self, source_component_name: str, quote_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """
        Called when quote data is processed by an observed component.
        
        Args:
            source_component_name: Name of the component that processed the data.
            quote_data: Dictionary containing the quote event data (symbol, bid, ask, etc.).
            processing_latency_ns: Latency (in ns) for the source component to process this event.
        """
        ...


@runtime_checkable
class IBrokerDataListener(Protocol):
    """Protocol for broker response observation during capture."""
    
    def on_broker_response(self, source_component_name: str, response_data: Dict[str, Any], processing_latency_ns: int) -> None:
        """
        Called when a broker response is processed by an observed component.
        
        Args:
            source_component_name: Name of the component that processed the data (e.g., "LightspeedBroker").
            response_data: Dictionary containing the broker response data.
            processing_latency_ns: Latency (in ns) for the source component to process this event.
        """
        ...


@runtime_checkable
class IOCRDataListener(Protocol):
    """Protocol for OCR data observation during capture."""
    
    def on_ocr_frame_processed(self, source_component_name: str, frame_number: int, ocr_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        """
        Called when an OCR frame has been fully processed (e.g., by OCRDataConditioningService).
        
        Args:
            source_component_name: Name of the component that processed the data (e.g., "OCRDataConditioningService").
            frame_number: The frame number associated with this OCR data.
            ocr_payload: Dictionary containing the processed OCR data (e.g., extracted text, positions, confidence).
                         This should include the 'expected_position_dict' and 'ocr_confidence' for correlation.
            processing_latency_ns: Latency (in ns) for the source component to process this frame/data.
        """
        ...

    # The Lead Architect's spec had `on_ocr_data` and `on_frame_processed`.
    # For clarity, let's assume `on_ocr_frame_processed` is the primary one.
    # If there are distinct "raw OCR data" vs "processed frame data" events, we can add another method.
    # For now, one method to capture the significant OCR output.


class IFeedbackIsolationManager(ABC): # Changed from metaclass=ABCMeta for modern Python
    """Manages position feedback isolation during controlled injections."""

    @abstractmethod
    def activate_symbol_ocr_mock(self, symbol: str, mock_profile: PositionMockProfile) -> None:
        """Activate OCR output mocking for a specific symbol with the given profile."""
        pass

    @abstractmethod
    def deactivate_symbol_ocr_mock(self, symbol: str) -> None:
        """Deactivate OCR output mocking for a specific symbol."""
        pass

    @abstractmethod
    def is_symbol_ocr_mocked(self, symbol: str) -> bool:
        """Checks if a symbol's OCR output is currently being mocked."""
        pass

    @abstractmethod
    def get_mocked_ocr_snapshot_data(
        self,
        symbol: str,
        actual_conditioned_snapshot_data: Dict[str, Any] # This is one symbol's snapshot from CleanedOCRSnapshotEventData.snapshots
    ) -> Dict[str, Any]:
        """
        If mocking is active for the symbol, returns the mocked snapshot data.
        Otherwise, returns the actual_conditioned_snapshot_data.
        The returned dict should match the structure of CleanedOCRSnapshotEventData.snapshots[symbol].
        """
        pass

    @abstractmethod
    def set_testable_position_manager_state(self, symbol: str, state: PositionState) -> bool:
        """
        Sets the internal state of the IntelliSenseTestablePositionManager for a symbol.
        Returns True if successful.
        """
        pass

    @abstractmethod
    def reset_all_mocking_and_states(self) -> None:
        """Clears all active OCR mocks and resets any controlled PositionManager states."""
        pass
