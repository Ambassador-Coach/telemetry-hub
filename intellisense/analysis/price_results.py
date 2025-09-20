from dataclasses import dataclass, field
import time
from typing import Dict, Any, Optional, List

@dataclass
class PriceRepositoryObservedState:
    """Represents the observed state of PriceRepository after an event for validation."""
    symbol: str
    last_trade_price: Optional[float] = None
    last_trade_timestamp: Optional[float] = None
    bid_price: Optional[float] = None
    bid_timestamp: Optional[float] = None
    ask_price: Optional[float] = None
    ask_timestamp: Optional[float] = None
    # Add other relevant queryable states from PriceRepository if needed

@dataclass
class PriceValidationResult:
    """Result of validating price event processing against PriceRepository state."""
    is_valid: bool # Overall validity based on checks
    checks_performed: List[str] = field(default_factory=list) # e.g., ["last_trade_price_match", "bid_update_latency_within_threshold"]
    discrepancies: List[str] = field(default_factory=list)
    observed_state_after_event: Optional[PriceRepositoryObservedState] = None
    # expected_state_after_event: Optional[PriceRepositoryObservedState] = None # If we have clear expected states

@dataclass
class PriceAnalysisResult:
    """Result of Price intelligence analysis for a single PriceTimelineEvent."""
    price_event_type: str
    symbol: str
    epoch_timestamp_s: float
    perf_counter_timestamp: Optional[float]
    analysis_completed_timestamp: float = field(default_factory=time.time)
    original_price_event_global_sequence_id: Optional[int] = None

    captured_processing_latency_ns: Optional[int] = None
    # Latency of IntelliSenseTestablePriceRepository.on_test_price_event processing this replayed event
    replay_price_repo_injection_latency_ns: Optional[int] = None

    replayed_event_data: Optional[Dict[str, Any]] = None
    validation_result: Optional[PriceValidationResult] = None

    service_versions: Optional[Dict[str, str]] = None
    analysis_error: Optional[str] = None
    diagnostic_info: Optional[Dict[str, Any]] = None

    @property
    def is_successful(self) -> bool:
        return self.analysis_error is None
