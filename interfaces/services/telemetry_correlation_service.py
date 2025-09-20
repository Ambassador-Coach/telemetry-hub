"""
PROJECT UNFUZZIFY: Telemetry Correlation Service Interface

Interface definition for the TCS service that provides unified timeline correlation.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


@dataclass
class TCSStats:
    """TCS performance statistics"""
    running: bool
    events_processed: int
    events_published: int
    events_buffered: int
    correlations_tracked: int
    monitored_streams: int
    buffer_window_ms: float
    global_sequence: int


class ITelemetryCorrelationService(ABC):
    """
    Interface for the Telemetry Correlation Service
    
    The TCS provides unified timeline correlation by:
    1. Consuming all Redis streams as pure observer
    2. Sorting events by nanosecond precision timestamps
    3. Publishing unified timeline to testrade:unified-timeline
    """
    
    @abstractmethod
    async def start(self) -> None:
        """Start the TCS service"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the TCS service"""
        pass
    
    @abstractmethod
    def get_stats(self) -> TCSStats:
        """Get TCS performance statistics"""
        pass
    
    @abstractmethod
    def is_running(self) -> bool:
        """Check if TCS is currently running"""
        pass
    
    @abstractmethod
    def get_monitored_streams(self) -> List[str]:
        """Get list of monitored Redis streams"""
        pass
    
    @abstractmethod
    def get_correlation_chain(self, correlation_id: str) -> List[Dict[str, Any]]:
        """Get complete event chain for a correlation ID"""
        pass
