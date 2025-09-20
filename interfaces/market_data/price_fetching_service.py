"""
Price Fetching Service Interface

WebSocket data feed connection and management.
"""

from abc import ABC, abstractmethod
from typing import Set

from ..core.lifecycle import ILifecycleService


class IPriceFetchingService(ILifecycleService):
    """
    Interface for the price fetching service that manages WebSocket connections
    to market data feeds.
    """
    
    @abstractmethod
    def update_subscriptions(self, symbols: Set[str]) -> None:
        """
        Update the active symbol subscriptions.
        
        Args:
            symbols: Set of symbols to subscribe to
        """
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the WebSocket connection is active."""
        pass