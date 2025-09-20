"""
Factory Interfaces for IntelliSense Module

Defines abstract base classes for creating data sources and other components
within the IntelliSense system.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

# Import data source interfaces from core module
from intellisense.core.interfaces import IOCRDataSource, IPriceDataSource, IBrokerDataSource

# Use TYPE_CHECKING to avoid circular imports
if TYPE_CHECKING:
    from intellisense.core.types import IntelliSenseConfig, TestSessionConfig

# Forward declarations to avoid circular imports
IntelliSenseConfig = Any
TestSessionConfig = Any

# Placeholder for ApplicationCore
ApplicationCore = Any


class IDataSourceFactory(ABC):
    """Interface for creating data sources for IntelliSense operations."""

    @abstractmethod
    def __init__(self, app_core: 'ApplicationCore', intellisense_config: Optional['IntelliSenseConfig']):
        """
        Initialize the data source factory.

        Args:
            app_core: Application core instance for accessing live sources
            intellisense_config: IntelliSense configuration containing data source overrides
        """
        pass

    @abstractmethod
    def create_ocr_data_source(self, session_config: 'TestSessionConfig') -> Optional[IOCRDataSource]:
        """
        Create OCR data source for specific session.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            OCR data source instance or None if not configured
        """
        pass

    @abstractmethod
    def create_price_data_source(self, session_config: 'TestSessionConfig') -> Optional[IPriceDataSource]:
        """
        Create price data source for specific session.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            Price data source instance or None if not configured
        """
        pass

    @abstractmethod
    def create_broker_data_source(self, session_config: 'TestSessionConfig') -> Optional[IBrokerDataSource]:
        """
        Create broker data source for specific session.

        Args:
            session_config: Test session configuration containing data paths and settings

        Returns:
            Broker data source instance or None if not configured
        """
        pass
