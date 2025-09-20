"""
Core Enums for IntelliSense Module

Defines all enumeration types used throughout the IntelliSense system for
operation modes, trade actions, lifecycle states, and status indicators.
"""

from enum import Enum


class OperationMode(Enum):
    """System operation mode - live trading or IntelliSense testing."""
    LIVE = "live"
    INTELLISENSE = "intellisense"


class TradeActionType(Enum):
    """Trade action types for position management and validation."""
    OPEN_LONG = "OPEN_LONG"
    ADD_LONG = "ADD_LONG"
    REDUCE_PARTIAL = "REDUCE_PARTIAL"
    REDUCE_FULL = "REDUCE_FULL"
    CLOSE_POSITION = "CLOSE_POSITION"
    DO_NOTHING = "DO_NOTHING"
    UNKNOWN = "UNKNOWN"
    # TODO: Add SHORT-side actions if system supports shorting (Pending Dependency)


class LifecycleState(Enum):
    """Position lifecycle states for tracking trade progression."""
    NONE = 0
    OPEN_PENDING = 1
    OPEN_ACTIVE = 2
    CLOSE_PENDING = 3
    CLOSED = 4
    ERROR = 5
    # TODO: Confirm consistency with live system or plan migration (Pending Dependency)


class DataLoadStatus(Enum):
    """Status indicators for data loading operations."""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"


class SyncAccuracyStatus(Enum):
    """Synchronization accuracy levels for timeline correlation."""
    ACCURATE = "accurate"
    WARNING = "warning"
    POOR = "poor"


class TestStatus(Enum):
    """Test execution status indicators."""
    PENDING = "pending"                   # Session created, not yet started
    LOADING_DATA = "loading_data"         # Data is being loaded for the session
    # Critical Rework: Added DATA_LOADED_PENDING_SYNC
    DATA_LOADED_PENDING_SYNC = "data_loaded_pending_sync"  # Data loaded, ready for timeline sync
    SYNCHRONIZING = "synchronizing"       # Timelines are being synchronized
    RUNNING = "running"                   # Test session is actively replaying/injecting
    COMPLETED = "completed"               # Session finished normally (deprecated if using _PENDING_REPORT)
    # Critical Rework: Added COMPLETED_PENDING_REPORT
    COMPLETED_PENDING_REPORT = "completed_pending_report"  # Session finished, report generation pending
    FAILED = "failed"                     # Session failed due to test logic or data issues
    ERROR = "error"                       # Session failed due to system/unexpected error
    STOPPED = "stopped"                   # Session was manually stopped
