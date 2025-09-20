# interfaces/utility/services.py

from abc import ABC, abstractmethod
from typing import Optional

class ISystemHealthMonitoringService(ABC):
    """Interface for system health monitoring."""
    pass

class IGUICommandService(ABC):
    """Interface for GUI command service."""
    # TODO: Define proper interface methods when refactoring GUICommandService
    pass

class IROIService(ABC):
    """Interface for ROI service.""" 
    pass

class IPositionEnrichmentService(ABC):
    """Interface for position enrichment service."""
    pass

class ISymbolLoadingService(ABC):
    """Interface for symbol loading service."""

    @abstractmethod
    def load_symbols(self) -> None:
        """Load symbols."""
        pass

    @abstractmethod
    def get_symbols(self) -> list:
        """Get loaded symbols."""
        pass

class IFingerprintService(ABC):
    """Interface for smart fingerprint-based duplicate detection service."""

    @abstractmethod
    def is_duplicate(self, fingerprint: tuple, context: Optional[dict] = None) -> bool:
        """
        Check if a fingerprint represents a duplicate signal.

        Args:
            fingerprint: Tuple representing the signal fingerprint
            context: Optional context for smart decision making (symbol, market conditions, etc.)

        Returns:
            True if this is a duplicate that should be suppressed
        """
        pass

    @abstractmethod
    def update(self, fingerprint: tuple, signal_data: dict) -> None:
        """
        Update the cache with a new fingerprint after successful signal generation.

        Args:
            fingerprint: Tuple representing the signal fingerprint
            signal_data: Associated signal data for context
        """
        pass

    @abstractmethod
    def invalidate_by_symbol(self, symbol: str) -> None:
        """
        Invalidate all cached fingerprints for a specific symbol.
        Used when market conditions change for that symbol.

        Args:
            symbol: Symbol to invalidate fingerprints for
        """
        pass

    @abstractmethod
    def get_cache_stats(self) -> dict:
        """
        Get statistics about the fingerprint cache for monitoring.

        Returns:
            Dictionary with cache statistics
        """
        pass