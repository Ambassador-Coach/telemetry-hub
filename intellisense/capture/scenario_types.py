"""
Scenario Types for IntelliSense Controlled Injection

This module defines data structures for controlled injection scenarios,
including injection steps, validation points, and stimulus definitions.
"""

from dataclasses import dataclass, field, asdict  # Added asdict for potential use in TestSession.to_dict
from typing import Dict, Any, List, Optional, Union, Literal, TYPE_CHECKING
from typing_extensions import TypedDict

# LAZY IMPORT SOLUTION: Avoid circular dependencies with feedback isolation types
if TYPE_CHECKING:
    from .feedback_isolation_types import PositionMockProfile, PositionState
else:
    # Import or define PositionMockProfile and PositionState
    try:
        from .feedback_isolation_types import PositionMockProfile, PositionState
    except ImportError:
        # Placeholder definitions if feedback_isolation_types.py is not available
        class PositionMockProfile(TypedDict):
            """Placeholder for PositionMockProfile from feedback_isolation_types."""
            strategy_hint: str
            cost_basis: float
            pnl_per_share: float
            realized_pnl: float
            unrealized_pnl: float
            total_shares: float
            current_value: float

        class PositionState(TypedDict):
            """Placeholder for PositionState from feedback_isolation_types."""
            symbol: str
            open: bool
            strategy: str
            total_shares: float
            total_avg_price: float
            overall_realized_pnl: float
            unrealized_pnl: Optional[float]
            market_value: Optional[float]

ValidationPointType = Literal[
    "order_repository_state",       # Was "order_status" - more generic now
    "position_manager_state",       # Was "position_state"
    "correlation_log_event_count",
    "pipeline_latency",             # Was "pipeline_latency_max" - more generic now
    "specific_log_event_field",     # Check specific field in pipeline event
    "pipeline_timeout_status",      # Check if pipeline timed out
    "pipeline_completion_status",   # Check if pipeline completed successfully
    "stimulus_field_validation",    # Validate fields directly from stimulus data
    "custom_query"                  # Retained for future
]

ComparisonOperator = Literal[
    "==", "!=", ">", "<", ">=", "<=",
    "in", "not_in",
    "exists", "not_exists",
    "is_empty", "is_not_empty",
    "contains_all", "contains_any",
    "starts_with", "ends_with", "matches_regex"  # Added more string/pattern ops
]

@dataclass
class ValidationPoint:
    validation_id: str
    type: ValidationPointType
    description: Optional[str] = None

    # Common parameters
    # For state checks: symbol or order_id (resolved from stimulus if ref)
    # For log checks: criteria to find relevant log entries
    # For latency checks: name of the latency metric (key in latencies_ns dict)
    target_identifier: Optional[str] = None

    # For state/value checks from components or specific log entry payloads
    field_to_check: Optional[str] = None # Dot-separated path for nested dicts
    expected_value: Any = None
    operator: ComparisonOperator = "=="
    tolerance: Optional[float] = None # For float comparisons

    # For correlation_log_event_count & pipeline_latency (to identify specific log events)
    # Criteria to filter log entries (e.g., {"source_sense": "BROKER", "event_payload.broker_response_type": "FILL"})
    event_criteria: Optional[Dict[str, Any]] = None

    # For correlation_log_event_count
    expected_event_count: Optional[int] = None # Operator applies to this count

    # For pipeline_latency
    # field_to_check will be the key in the latencies_ns dict (e.g., "T0_CmdInject_to_T3_BrokerAck_ns")
    # expected_value will be the max allowed latency in ms (operator will be '<=')

    # For custom_query (advanced)
    custom_query_payload: Optional[Dict[str, Any]] = None

    # Helper for deserializing from JSON where Any might be complex
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ValidationPoint':
        """
        Create ValidationPoint from dictionary for JSON compatibility.

        Handles proper type conversion for expected_value: Any field
        and ensures all fields are properly deserialized.
        """
        # Basic field copy, assumes types in JSON match or are convertible
        # More complex deserialization for 'expected_value' might be needed if it can be nested structures.
        # For now, direct instantiation handles most cases correctly.
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert ValidationPoint to dictionary for JSON serialization."""
        return asdict(self)

@dataclass
class ValidationOutcome:
    validation_id: str
    passed: bool
    validation_type: ValidationPointType
    description: Optional[str] = None
    target_identifier: Optional[str] = None
    field_checked: Optional[str] = None
    operator_used: ComparisonOperator = "=="
    expected_value_detail: Any = None # Store what was expected
    actual_value_detail: Any = None   # Store what was found
    details: Optional[str] = None     # Further context or discrepancy message

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ValidationOutcome':
        """Create ValidationOutcome from dictionary for JSON compatibility."""
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert ValidationOutcome to dictionary for JSON serialization."""
        return asdict(self)

@dataclass
class InjectionStep:
    step_id: str
    stimulus: Dict[str, Any]
    step_description: Optional[str] = None
    ocr_mocks_to_activate: Optional[Dict[str, PositionMockProfile]] = field(default_factory=dict)
    position_manager_states_to_set: Optional[Dict[str, PositionState]] = field(default_factory=dict)
    validation_points: List[ValidationPoint] = field(default_factory=list)
    wait_after_injection_s: float = 0.5

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InjectionStep':
        """
        Create InjectionStep from dictionary with proper nested ValidationPoint handling.

        Handles deserialization of nested ValidationPoint list from JSON data.
        """
        # Handle deserialization of nested ValidationPoint list
        vp_list_data = data.get('validation_points', [])
        validation_points_objs = [ValidationPoint.from_dict(vp_data) for vp_data in vp_list_data]

        # Create a new dict for InjectionStep constructor, replacing validation_points
        step_constructor_data = {k: v for k, v in data.items() if k != 'validation_points'}
        step_constructor_data['validation_points'] = validation_points_objs

        return cls(**step_constructor_data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert InjectionStep to dictionary for JSON serialization."""
        data = asdict(self)
        # Ensure validation_points are properly serialized
        data['validation_points'] = [vp.to_dict() for vp in self.validation_points]
        return data

@dataclass
class StepExecutionResult:
    """Stores the outcome of executing a single InjectionStep."""
    step_id: str
    correlation_id: str # The correlation_id assigned to this step's stimulus
    pipeline_data: Dict[str, Any] # Result from _wait_for_pipeline_completion
    step_description: Optional[str] = None
    stimulus_type: Optional[str] = None
    validation_outcomes: List[ValidationOutcome] = field(default_factory=list)
    step_execution_error: Optional[str] = None

    @property
    def all_validations_passed(self) -> bool:
        if not self.validation_outcomes: return True # No validations means trivially passed
        return all(vo.passed for vo in self.validation_outcomes)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StepExecutionResult':
        """Create StepExecutionResult from dictionary with proper nested ValidationOutcome handling."""
        # Handle deserialization of nested ValidationOutcome list
        vo_list_data = data.get('validation_outcomes', [])
        validation_outcomes_objs = [ValidationOutcome.from_dict(vo_data) for vo_data in vo_list_data]

        # Create a new dict for StepExecutionResult constructor, replacing validation_outcomes
        result_constructor_data = {k: v for k, v in data.items() if k != 'validation_outcomes'}
        result_constructor_data['validation_outcomes'] = validation_outcomes_objs

        return cls(**result_constructor_data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert StepExecutionResult to dictionary for JSON serialization."""
        data = asdict(self)
        # Ensure validation_outcomes are properly serialized
        data['validation_outcomes'] = [vo.to_dict() for vo in self.validation_outcomes]
        return data


@dataclass
class StimulusDefinition:
    """
    Defines different types of stimuli that can be injected during controlled testing.
    """
    
    # Stimulus type identifier
    stimulus_type: str  # 'trade_command', 'ocr_event', 'price_update', 'broker_response'
    
    # Stimulus-specific parameters
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    # Optional timing information
    injection_timestamp: Optional[float] = None
    delay_before_injection_ms: Optional[float] = None
    
    # Optional metadata
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class InjectionPlan:
    """
    Defines a complete controlled injection plan consisting of multiple steps.
    """
    
    # Plan metadata
    plan_name: str
    description: Optional[str] = None
    version: str = "1.0"
    
    # Plan configuration
    target_symbols: List[str] = field(default_factory=list)
    total_duration_minutes: Optional[float] = None
    
    # Injection steps
    steps: List[InjectionStep] = field(default_factory=list)
    
    # Global validation settings
    global_validation_settings: Dict[str, Any] = field(default_factory=dict)
    
    # Plan execution settings
    execution_settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InjectionResult:
    """
    Captures the result of executing an injection step or plan.
    """
    
    # Execution metadata
    step_index: Optional[int] = None
    execution_timestamp: Optional[float] = None
    execution_duration_ms: Optional[float] = None
    
    # Execution status
    success: bool = False
    error_message: Optional[str] = None
    
    # Validation results
    validation_results: List[Dict[str, Any]] = field(default_factory=list)
    
    # Captured data during execution
    captured_data: Dict[str, Any] = field(default_factory=dict)
    
    # Performance metrics
    performance_metrics: Dict[str, Any] = field(default_factory=dict)

@dataclass
class InjectionPlanResult:
    """
    Comprehensive result of executing an injection plan.

    Contains step-by-step results, validation outcomes, timing data,
    and overall plan execution status.
    """
    plan_id: str
    plan_description: Optional[str] = None
    execution_start_time: Optional[float] = None
    execution_end_time: Optional[float] = None
    total_steps: int = 0
    successful_steps: int = 0
    failed_steps: int = 0

    # Step-by-step results
    step_results: List[Dict[str, Any]] = field(default_factory=list)

    # Validation outcomes
    all_validation_outcomes: List[ValidationOutcome] = field(default_factory=list)
    passed_validations: int = 0
    failed_validations: int = 0

    # Overall status
    plan_passed: bool = False
    error_message: Optional[str] = None

@dataclass
class ValidationContext:
    """
    Context information available during validation execution.

    Provides access to system components, correlation logs, and
    pipeline data for comprehensive validation checks.
    """
    step_correlation_id: str
    pipeline_data: Dict[str, Any]
    correlation_events: List[Dict[str, Any]]
    app_core: Any  # Reference to application core for component access
    validation_timestamp: float


# Stimulus type constants for consistency
class StimulusTypes:
    """Constants for stimulus types."""
    TRADE_COMMAND = "trade_command"
    OCR_EVENT = "ocr_event"
    PRICE_UPDATE = "price_update"
    BROKER_RESPONSE = "broker_response"
    POSITION_UPDATE = "position_update"
    SYSTEM_EVENT = "system_event"


# Validation type constants for consistency
class ValidationTypes:
    """Constants for validation types."""
    POSITION_STATE = "position_state"
    TRADE_EXECUTION = "trade_execution"
    OCR_DETECTION = "ocr_detection"
    SYSTEM_STATE = "system_state"
    PERFORMANCE_METRIC = "performance_metric"
    CORRELATION_CHECK = "correlation_check"


# Helper functions for creating common injection scenarios
def create_trade_stimulus(symbol: str, side: str, quantity: int, order_type: str = "MKT", 
                         limit_price: Optional[float] = None) -> Dict[str, Any]:
    """Create a trade command stimulus."""
    stimulus = {
        'type': StimulusTypes.TRADE_COMMAND,
        'symbol': symbol,
        'side': side,
        'quantity': quantity,
        'order_type': order_type
    }
    if limit_price is not None:
        stimulus['limit_price'] = limit_price
    return stimulus


def create_ocr_stimulus(symbol: str, position_data: Dict[str, Any]) -> Dict[str, Any]:
    """Create an OCR event stimulus."""
    return {
        'type': StimulusTypes.OCR_EVENT,
        'symbol': symbol,
        'position_data': position_data,
        'frame_timestamp': None,  # Will be set during injection
        'confidence': 0.95
    }


def create_position_validation(symbol: str, expected_shares: float, 
                             expected_strategy: str) -> Dict[str, Any]:
    """Create a position state validation point."""
    return {
        'type': ValidationTypes.POSITION_STATE,
        'symbol': symbol,
        'expected_shares': expected_shares,
        'expected_strategy': expected_strategy
    }


def create_trade_execution_validation(expected_order_count: int, 
                                    expected_fill_count: int = 0) -> Dict[str, Any]:
    """Create a trade execution validation point."""
    return {
        'type': ValidationTypes.TRADE_EXECUTION,
        'expected_order_count': expected_order_count,
        'expected_fill_count': expected_fill_count
    }
