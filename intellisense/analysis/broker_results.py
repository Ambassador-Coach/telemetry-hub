from dataclasses import dataclass, field
import time
from typing import Dict, Any, Optional, List

@dataclass
class OrderRepositoryValidation:
    is_valid: bool
    order_id: str # Local order ID
    discrepancies: List[str] = field(default_factory=list)
    expected_state: Optional[Dict[str, Any]] = field(default_factory=dict) # What we thought it should be
    actual_state: Optional[Dict[str, Any]] = field(default_factory=dict)   # What it actually was

@dataclass
class PositionManagerValidation:
    is_valid: bool
    symbol: str
    discrepancies: List[str] = field(default_factory=list)
    expected_state: Optional[Dict[str, Any]] = field(default_factory=dict)
    actual_state: Optional[Dict[str, Any]] = field(default_factory=dict)

# ... (BrokerValidationResult and BrokerAnalysisResult as before, ensure they use these updated types)
@dataclass
class BrokerValidationResult:
    is_overall_valid: bool
    order_repository_validation: Optional[OrderRepositoryValidation] = None
    position_manager_validation: Optional[PositionManagerValidation] = None
    discrepancies: List[str] = field(default_factory=list) # Consolidated discrepancies

@dataclass
class BrokerAnalysisResult: # No changes here from Chunk 13, just ensure it uses updated validation types
    broker_event_type: str
    original_order_id: str
    epoch_timestamp_s: float
    broker_order_id_on_event: Optional[str] = None
    perf_counter_timestamp: Optional[float] = None
    analysis_completed_timestamp: float = field(default_factory=time.time)
    original_broker_event_global_sequence_id: Optional[int] = None
    captured_processing_latency_ns: Optional[int] = None
    event_bus_publish_latency_ns: Optional[int] = None
    order_repo_direct_call_latency_ns: Optional[int] = None
    position_manager_direct_call_latency_ns: Optional[int] = None
    replayed_event_data: Optional[Any] = None
    derived_domain_event_type: Optional[str] = None
    validation_result: Optional[BrokerValidationResult] = None # This will use the refined types
    service_versions: Optional[Dict[str, str]] = None
    analysis_error: Optional[str] = None
    diagnostic_info: Optional[Dict[str, Any]] = None

    @property
    def is_successful(self) -> bool:
        return self.analysis_error is None
