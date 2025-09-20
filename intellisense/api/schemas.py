"""
Pydantic Models for IntelliSense API Data

This module defines Pydantic schemas for API request/response contracts,
providing clean separation between API layer and internal dataclasses.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
import time

# REMOVED: Problematic imports that cause circular dependencies
# These will be handled lazily when needed

# Define local enums to avoid import dependencies
class TestSessionStatus:
    """Local enum for session status to avoid import dependencies."""
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPED = "stopped"
    ERROR = "error"

# --- Request Schemas ---

class TestSessionCreateRequest(BaseModel):
    """Schema for creating a new test session, mirrors TestSessionConfig."""
    session_name: str
    description: str
    test_date: str # YYYY-MM-DD

    controlled_trade_log_path: str
    historical_price_data_path_sync: str
    historical_price_data_path_stress: Optional[str] = None
    broker_response_log_path: str

    duration_minutes: int = 60
    speed_factor: float = 1.0

    # For sub-configs, we can either flatten or nest. Nesting is cleaner.
    # These would be Pydantic models mirroring OCRTestConfig, PriceTestConfig, BrokerTestConfig
    # For brevity, keeping them as dicts for now, but ideally, they'd be Pydantic models too.
    ocr_config: Dict[str, Any] = Field(default_factory=dict) # Placeholder for OCRTestConfigSchema
    price_config: Dict[str, Any] = Field(default_factory=dict) # Placeholder for PriceTestConfigSchema
    broker_config: Dict[str, Any] = Field(default_factory=dict) # Placeholder for BrokerTestConfigSchema
    
    injection_plan_path: Optional[str] = None

    monitoring_enabled: bool = True
    dashboard_update_interval_sec: float = 5.0
    metrics_collection_interval_sec: float = 1.0
    validation_enabled: bool = True
    correlation_tolerance_ms: float = 10.0
    accuracy_threshold_percent: float = 95.0
    output_directory: Optional[str] = None

    # Helper to convert to internal TestSessionConfig (if needed by controller)
    def to_internal_config(self) -> Optional[Dict[str, Any]]:
        """Convert to internal config format - returns dict to avoid import dependencies."""
        # Return as dict - let the controller handle conversion to internal types
        # This avoids circular import issues during development
        return self.dict()


class InjectionStepRequest(BaseModel):
    """Schema for individual injection step in controlled testing."""
    step_id: str
    step_description: Optional[str] = None
    stimulus: Dict[str, Any]
    validation_points: List[Dict[str, Any]] = Field(default_factory=list)
    wait_after_injection_s: float = 0.5
    ocr_mocks_to_activate: Optional[Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    position_manager_states_to_set: Optional[Dict[str, Dict[str, Any]]] = Field(default_factory=dict)


class InjectionPlanRequest(BaseModel):
    """Schema for complete injection plan."""
    plan_name: str
    description: Optional[str] = None
    steps: List[InjectionStepRequest]
    global_settings: Dict[str, Any] = Field(default_factory=dict)


# --- Response Schemas ---

class SessionCreationResponse(BaseModel):
    """Response for session creation."""
    session_id: str
    status: str # e.g., "created", "failed"
    message: str
    created_at: float = Field(default_factory=time.time)


class SessionSummaryResponse(BaseModel):
    """Summary information for a test session."""
    session_id: str
    session_name: str
    status: str # Value from TestSessionStatus enum
    test_date: str
    duration_minutes: int
    created_at: float
    description: Optional[str] = None


class SessionStatusDetailResponse(SessionSummaryResponse):
    """Detailed status information extending summary."""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    progress_percent: float = 0.0
    current_phase: Optional[str] = None
    error_count: int = 0
    warning_count: int = 0
    validation_failures: int = 0
    # Add more details if needed, e.g., config snapshot
    config_snapshot: Optional[Dict[str, Any]] = None
    performance_metrics: Optional[Dict[str, Any]] = None


class ActionResponse(BaseModel):
    """Response for session actions (start, stop, pause, etc.)."""
    session_id: str
    action: str
    status: str # e.g., "initiated", "failed", "already_in_state"
    message: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)


# For results summary (needs actual ValidationOutcome and StepExecutionResult schemas)
class ValidationOutcomeSchema(BaseModel):
    """Schema mirroring ValidationOutcome dataclass."""
    validation_id: str
    passed: bool
    validation_type: str # Literal ValidationPointType
    description: Optional[str] = None
    target_identifier: Optional[str] = None
    field_checked: Optional[str] = None
    operator_used: str = "==" # Literal ComparisonOperator
    expected_value_detail: Any = None
    actual_value_detail: Any = None
    details: Optional[str] = None


class StepExecutionResultSchema(BaseModel):
    """Schema mirroring StepExecutionResult dataclass."""
    step_id: str
    correlation_id: str
    pipeline_data: Dict[str, Any] # Summary of pipeline_data
    step_description: Optional[str] = None
    stimulus_type: Optional[str] = None
    validation_outcomes: List[ValidationOutcomeSchema] = Field(default_factory=list)
    step_execution_error: Optional[str] = None
    all_validations_passed: bool = True

    @property
    def validation_summary(self) -> Dict[str, int]:
        """Summary of validation results."""
        passed = sum(1 for vo in self.validation_outcomes if vo.passed)
        failed = len(self.validation_outcomes) - passed
        return {"passed": passed, "failed": failed, "total": len(self.validation_outcomes)}


class SessionResultsSummaryResponse(BaseModel):
    """Comprehensive session results summary."""
    session_id: str
    session_name: str
    overall_status: str # TestSessionStatus
    execution_summary: Dict[str, Any] = Field(default_factory=dict)
    
    # Step execution summary
    total_steps: Optional[int] = None
    successful_steps: Optional[int] = None
    failed_steps: Optional[int] = None
    steps_with_validation_failures: Optional[int] = None
    
    # Performance summary
    key_latencies_summary: Optional[Dict[str, float]] = None # e.g. avg total_pipeline_ms
    performance_metrics: Optional[Dict[str, Any]] = None
    
    # Validation summary
    total_validations: Optional[int] = None
    passed_validations: Optional[int] = None
    failed_validations: Optional[int] = None
    
    # Results preview
    step_results_preview: Optional[List[Dict[str, Any]]] = None # e.g. list of step_id and all_validations_passed
    
    # Timing information
    session_duration_seconds: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class SessionListResponse(BaseModel):
    """Response for listing sessions."""
    sessions: List[SessionSummaryResponse]
    total_count: int
    page: int = 1
    page_size: int = 50
    has_more: bool = False


class ErrorResponse(BaseModel):
    """Standard error response schema."""
    error: str
    message: str
    details: Optional[Dict[str, Any]] = None
    timestamp: float = Field(default_factory=time.time)
    request_id: Optional[str] = None


# --- Utility Schemas ---

class HealthCheckResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    timestamp: float = Field(default_factory=time.time)
    version: Optional[str] = None
    components: Optional[Dict[str, str]] = None # component_name -> status


class MetricsResponse(BaseModel):
    """Real-time metrics response."""
    session_id: str
    timestamp: float = Field(default_factory=time.time)
    metrics: Dict[str, Any]
    performance_data: Optional[Dict[str, Any]] = None
    validation_status: Optional[Dict[str, Any]] = None


# --- Detailed Configuration Schemas ---

class OCRTestConfigSchema(BaseModel):
    """Schema for OCR test configuration."""
    enabled: bool = True
    confidence_threshold: float = 0.8
    processing_timeout_ms: int = 5000
    mock_data_enabled: bool = False
    mock_data_path: Optional[str] = None
    validation_rules: Dict[str, Any] = Field(default_factory=dict)


class PriceTestConfigSchema(BaseModel):
    """Schema for price test configuration."""
    enabled: bool = True
    update_frequency_ms: int = 100
    price_tolerance: float = 0.01
    staleness_threshold_ms: int = 1000
    mock_price_enabled: bool = False
    price_volatility_factor: float = 1.0


class BrokerTestConfigSchema(BaseModel):
    """Schema for broker test configuration."""
    enabled: bool = True
    response_delay_ms: int = 50
    fill_probability: float = 0.95
    partial_fill_enabled: bool = True
    error_injection_rate: float = 0.0
    mock_responses_enabled: bool = False


class EnhancedTestSessionCreateRequest(TestSessionCreateRequest):
    """Enhanced test session creation with detailed sub-configs."""
    ocr_config: OCRTestConfigSchema = Field(default_factory=OCRTestConfigSchema)
    price_config: PriceTestConfigSchema = Field(default_factory=PriceTestConfigSchema)
    broker_config: BrokerTestConfigSchema = Field(default_factory=BrokerTestConfigSchema)


# --- Validation and Pipeline Schemas ---

class ValidationPointSchema(BaseModel):
    """Schema for validation point definition."""
    validation_id: str
    type: str # ValidationPointType
    description: Optional[str] = None
    target_identifier: Optional[str] = None
    field_to_check: Optional[str] = None
    expected_value: Any = None
    operator: str = "=="
    tolerance: Optional[float] = None
    event_criteria: Optional[Dict[str, Any]] = None
    expected_event_count: Optional[int] = None
    custom_query_payload: Optional[Dict[str, Any]] = None


class PipelineLatencySchema(BaseModel):
    """Schema for pipeline latency measurements."""
    stage_name: str
    start_timestamp_ns: int
    end_timestamp_ns: int
    duration_ns: int
    duration_ms: float
    correlation_id: str


class CorrelationLogEntrySchema(BaseModel):
    """Schema for correlation log entries."""
    timestamp: float
    correlation_id: str
    source_sense: str
    event_type: str
    payload: Dict[str, Any]
    sequence_id: Optional[int] = None


# --- Advanced Response Schemas ---

class DetailedSessionResultsResponse(BaseModel):
    """Detailed session results with complete step data."""
    session_id: str
    session_name: str
    overall_status: str

    # Complete step execution results
    step_results: List[StepExecutionResultSchema]

    # Detailed performance metrics
    pipeline_latencies: List[PipelineLatencySchema] = Field(default_factory=list)
    correlation_log_entries: List[CorrelationLogEntrySchema] = Field(default_factory=list)

    # Comprehensive statistics
    execution_statistics: Dict[str, Any] = Field(default_factory=dict)
    validation_statistics: Dict[str, Any] = Field(default_factory=dict)
    performance_statistics: Dict[str, Any] = Field(default_factory=dict)

    # Session metadata
    session_config: Optional[Dict[str, Any]] = None
    execution_environment: Optional[Dict[str, Any]] = None

    # Timing information
    session_duration_seconds: float
    started_at: float
    completed_at: float


class SessionProgressResponse(BaseModel):
    """Real-time session progress information."""
    session_id: str
    current_step: Optional[str] = None
    current_step_index: Optional[int] = None
    total_steps: Optional[int] = None
    progress_percent: float = 0.0
    estimated_completion_time: Optional[float] = None
    current_phase: str = "initializing"

    # Real-time metrics
    steps_completed: int = 0
    steps_failed: int = 0
    validations_passed: int = 0
    validations_failed: int = 0

    # Performance metrics
    average_step_duration_ms: Optional[float] = None
    current_step_duration_ms: Optional[float] = None

    # Status information
    last_update: float = Field(default_factory=time.time)
    is_running: bool = False
    has_errors: bool = False


# --- Batch Operation Schemas ---

class BatchSessionCreateRequest(BaseModel):
    """Schema for creating multiple test sessions."""
    sessions: List[TestSessionCreateRequest]
    execution_mode: str = "sequential" # "sequential" or "parallel"
    max_concurrent_sessions: int = 3
    stop_on_first_failure: bool = False


class BatchSessionResponse(BaseModel):
    """Response for batch session operations."""
    batch_id: str
    total_sessions: int
    successful_sessions: int
    failed_sessions: int
    session_results: List[SessionCreationResponse]
    overall_status: str
    started_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None
