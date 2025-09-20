"""
Application Core Interface - Main application controller

⚠️ CLEAN ARCHITECTURE - DO NOT MODIFY WITHOUT READING ⚠️
This is part of the central interfaces directory pattern.
All services depend on these interfaces, not implementations.
See /PROJECT_MEMORY/clean_architecture.md for details.
"""

from abc import ABC, abstractmethod
from typing import Any


class IApplicationCore(ABC):
    """
    Interface for the main application core.
    
    This is the root of the application that bootstraps
    all services and manages the application lifecycle.
    """
    
    @abstractmethod
    def start(self) -> None:
        """Start the application and all services."""
        pass
    
    @abstractmethod
    def stop(self) -> None:
        """Stop the application and all services."""
        pass
    
    @abstractmethod
    def get_service(self, service_name: str) -> Any:
        """
        Get a service by name (legacy compatibility).
        
        Args:
            service_name: Name of the service
            
        Returns:
            Service instance
        """
        pass