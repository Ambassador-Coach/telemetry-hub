"""
IntelliSense Controllers Module

Contains controller interfaces and implementations for orchestrating
IntelliSense test sessions and intelligence engines.
"""

from .interfaces import (
    IIntelligenceEngine,
    IOCRIntelligenceEngine,
    IPriceIntelligenceEngine,
    IBrokerIntelligenceEngine,
    ITimelineSynchronizer,
    IPerformanceMonitor,
    IIntelliSenseMasterController
)

from .master_controller import IntelliSenseMasterController

__all__ = [
    'IIntelligenceEngine',
    'IOCRIntelligenceEngine',
    'IPriceIntelligenceEngine',
    'IBrokerIntelligenceEngine',
    'ITimelineSynchronizer',
    'IPerformanceMonitor',
    'IIntelliSenseMasterController',
    'IntelliSenseMasterController'
]
