"""
IntelliSense Enhanced Components Package

This package provides enhanced versions of system components that integrate with
the IntelliSense data capture interfaces. These enhanced components extend the
original components with observation capabilities for correlation data capture.

Key Components:
- metrics_collector.py: Utilities for capture-related metrics collection
- enhanced_ocr_service.py: Enhanced OCR data conditioning service with capture observation
- enhanced_price_service.py: Enhanced price services with capture observation
- enhanced_broker_service.py: Enhanced broker services with capture observation
"""

from .metrics_collector import ComponentCaptureMetrics
from .enhanced_ocr_service import EnhancedOCRDataConditioningService
from .enhanced_price_service import EnhancedPriceRepository
from .enhanced_broker_service import EnhancedLightspeedBroker, EnhancedBrokerService
from .enhanced_price_repository import EnhancedPriceRepository as EnhancedPriceRepositoryV2
from .enhanced_broker_interface import EnhancedLightspeedBroker as EnhancedLightspeedBrokerV2, EnhancedGenericBrokerInterface

__all__ = [
    'ComponentCaptureMetrics',
    'EnhancedOCRDataConditioningService',
    'EnhancedPriceRepository',
    'EnhancedLightspeedBroker',
    'EnhancedBrokerService',
    'EnhancedPriceRepositoryV2',
    'EnhancedLightspeedBrokerV2',
    'EnhancedGenericBrokerInterface'
]
