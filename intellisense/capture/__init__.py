"""
IntelliSense Data Capture Package

This package provides interfaces and utilities for capturing data from live system components
during operation. The captured data is used to create correlation logs for later replay
and analysis in the IntelliSense system.

Key Components:
- interfaces.py: Protocol definitions for data listeners
- utils.py: Utilities including global sequence generation
"""

from .interfaces import IPriceDataListener, IBrokerDataListener, IOCRDataListener
from .utils import (
    GlobalSequenceGenerator,
    get_next_global_sequence_id,
    reset_global_sequence_id_generator,
    get_current_global_sequence_count
)
from .logger import CorrelationLogger
from .enhanced_components import (
    ComponentCaptureMetrics,
    EnhancedOCRDataConditioningService,
    EnhancedPriceRepository,
    EnhancedLightspeedBroker,
    EnhancedBrokerService,
    EnhancedPriceRepositoryV2,
    EnhancedLightspeedBrokerV2,
    EnhancedGenericBrokerInterface
)
from .session import ProductionDataCaptureSession


__all__ = [
    # Interfaces
    'IPriceDataListener',
    'IBrokerDataListener',
    'IOCRDataListener',

    # Utilities
    'GlobalSequenceGenerator',
    'get_next_global_sequence_id',
    'reset_global_sequence_id_generator',
    'get_current_global_sequence_count',

    # Logger
    'CorrelationLogger',

    # Enhanced Components
    'ComponentCaptureMetrics',
    'EnhancedOCRDataConditioningService',
    'EnhancedPriceRepository',
    'EnhancedLightspeedBroker',
    'EnhancedBrokerService',
    'EnhancedPriceRepositoryV2',
    'EnhancedLightspeedBrokerV2',
    'EnhancedGenericBrokerInterface',

    # Session Management
    'ProductionDataCaptureSession'
]
