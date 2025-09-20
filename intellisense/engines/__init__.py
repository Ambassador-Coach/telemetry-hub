"""
IntelliSense Engines Module

Contains intelligence engines and data source implementations for the IntelliSense system.
"""

# Import from legacy datasources file (renamed to avoid conflict)
from .legacy_datasources import (
    LiveOCRDataSource,
    IntelliSenseOCRReplaySource,
    LivePriceDataSource,
    IntelliSensePriceReplaySource,
    LiveBrokerDataSource,
    IntelliSenseBrokerReplaySource
)

# Import from new datasources directory
from .datasources.redis_ocr_raw_datasource import RedisRawOCRDataSource

from .placeholders import (
    PlaceholderOCRIntelligenceEngine,
    PlaceholderPriceIntelligenceEngine,
    PlaceholderBrokerIntelligenceEngine,
    PlaceholderTimelineSynchronizer,
    PlaceholderPerformanceMonitor
)

__all__ = [
    # Legacy Data Sources
    'LiveOCRDataSource',
    'IntelliSenseOCRReplaySource',
    'LivePriceDataSource',
    'IntelliSensePriceReplaySource',
    'LiveBrokerDataSource',
    'IntelliSenseBrokerReplaySource',

    # Redis Data Sources
    'RedisRawOCRDataSource',

    # Placeholder Engines
    'PlaceholderOCRIntelligenceEngine',
    'PlaceholderPriceIntelligenceEngine',
    'PlaceholderBrokerIntelligenceEngine',
    'PlaceholderTimelineSynchronizer',
    'PlaceholderPerformanceMonitor'
]
