"""
Diagnostics Interfaces - Performance monitoring and validation

⚠️ CLEAN ARCHITECTURE - DO NOT MODIFY WITHOUT READING ⚠️
This is part of the central interfaces directory pattern.
All services depend on these interfaces, not implementations.
See /PROJECT_MEMORY/clean_architecture.md for details.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class IPipelineValidator(ABC):
    """
    Interface for pipeline validation and tracking.
    
    This allows injecting test data and tracking its flow
    through the system for validation purposes.
    """
    
    @abstractmethod
    def inject_test_data(self, pipeline_type: str, payload: Dict[str, Any], 
                        custom_uid: Optional[str] = None) -> Optional[str]:
        """Inject test data into a pipeline."""
        pass
    
    @abstractmethod
    def register_stage_completion(self, validation_id: str, stage_name: str,
                                 stage_data: Optional[Dict[str, Any]] = None) -> None:
        """Register completion of a pipeline stage."""
        pass
    
    @abstractmethod
    def get_validation_results(self) -> Dict[str, Any]:
        """Get validation results for all tracked pipelines."""
        pass


class IPerformanceBenchmarker(ABC):
    """
    Interface for performance metrics collection.
    
    This provides consistent performance monitoring
    across all services.
    """
    
    @abstractmethod
    def capture_metric(self, metric_name: str, value: float,
                      context: Optional[Dict[str, Any]] = None) -> None:
        """Capture a performance metric."""
        pass
    
    @abstractmethod
    def get_stats(self, metric_name: str) -> Optional[Dict[str, Any]]:
        """Get statistics for a specific metric."""
        pass