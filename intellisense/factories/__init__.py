"""
IntelliSense Factories Module

Contains factory interfaces and implementations for creating data sources,
enhanced components, and testable components within the IntelliSense system.

Key Components:
- IDataSourceFactory: Interface for data source creation
- DataSourceFactory: Implementation for data source creation
- ComponentFactory: Factory for enhanced capture and testable analysis components

The factory pattern ensures consistent component creation while maintaining
separation of concerns between component logic and initialization complexity.
"""

from .interfaces import IDataSourceFactory
from .datasource_factory import DataSourceFactory
from .component_factory import ComponentFactory

__all__ = [
    'IDataSourceFactory',
    'DataSourceFactory',
    'ComponentFactory'
]
