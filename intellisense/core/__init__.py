"""
IntelliSense Core Module

Contains fundamental types, enums, and interfaces for the IntelliSense system.
Uses lazy import pattern to avoid circular dependencies.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

__version__ = "1.0.0"
__author__ = "TESTRADE Development Team"

# Only import safe, lightweight components immediately
from .enums import (
    OperationMode,
    TradeActionType,
    LifecycleState,
    DataLoadStatus,
    SyncAccuracyStatus,
    TestStatus
)

from .exceptions import (
    IntelliSenseError,
    IntelliSenseInitializationError,
    IntelliSenseSessionError,
    IntelliSenseDataError
)

# REMOVED: Problematic import that causes circular dependency chain
# from .application_core_intellisense import (
#     IntelliSenseApplicationCore,
#     CoreAppConfig,
#     ApplicationCore
# )
# Users should import directly: from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore

# TYPE_CHECKING guard for heavy imports - only loaded during type checking
if TYPE_CHECKING:
    from .types import (
        Position,
        TimelineEvent,
        OCRTimelineEvent,
        PriceTimelineEvent,
        BrokerAckData,
        BrokerFillData,
        BrokerTimelineEvent,
        OCRTestConfig,
        PriceTestConfig,
        DelayScenario,
        BrokerTestConfig,
        TestSessionConfig,
        IntelliSenseConfig,
        DataLoadResult,
        SynchronizationResult,
        TestExecutionResult,
        ComprehensiveReport,
        CreateSessionResponse,
        ExecutionResponse,
        TestSession,
        SessionCreationResult
    )

    from .interfaces import (
        IDataSource,
        IOCRDataSource,
        IPriceDataSource,
        IBrokerDataSource,
        IIntelligenceEngine,
        IOCRIntelligenceEngine,
        IPriceIntelligenceEngine,
        IBrokerIntelligenceEngine
    )

# Only export immediately available components to avoid import issues
__all__ = [
    # Enums (immediately available)
    'OperationMode',
    'TradeActionType',
    'LifecycleState',
    'DataLoadStatus',
    'SyncAccuracyStatus',
    'TestStatus',

    # Exceptions (immediately available)
    'IntelliSenseError',
    'IntelliSenseInitializationError',
    'IntelliSenseSessionError',
    'IntelliSenseDataError',

    # Note: Application Core, Types and Interfaces are available via direct import:
    # from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    # from intellisense.core.types import Position
    # from intellisense.core.interfaces import IDataSource
]
