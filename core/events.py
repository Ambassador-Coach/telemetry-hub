import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4 # Import UUID for type hinting and uuid4 for ID generation
from typing import Optional, Any, List, Dict, Union, Protocol, runtime_checkable # Added Protocol support
from utils.thread_safe_uuid import get_thread_safe_uuid

# Import enums for standardized event payloads
# CRITICAL FIX: Always import from canonical source to avoid type mismatches
# NOTE: Import moved to function level to avoid circular import with order_repository

# Protocol-based decoupling: Define protocols for IntelliSense dependencies
@runtime_checkable
class CorrelationLoggerProtocol(Protocol):
    """Protocol for correlation logging to decouple core events from IntelliSense."""
    def log_event(self, source_sense: str, event_payload: Dict[str, Any], source_component_name: Optional[str] = None) -> bool:
        """Log an event with correlation tracking."""
        ...

@dataclass
class BaseEvent:
    # Fields without default values must come before fields with default values
    # So we'll make these fields have default_factory
    event_id: str = field(default_factory=get_thread_safe_uuid)
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None  # Master correlation ID for end-to-end tracing
    # No event_type string field here; dispatch will use type(event)

# --- Example Specific Event Classes (more will be added later) ---
# These serve as a template for future event definitions.

@dataclass(slots=True)
class BrokerConnectionEventData: # New data class for the payload
    connected: bool = False
    message: str = ""
    latency_ms: Optional[float] = None

@dataclass
class BrokerConnectionEvent(BaseEvent): # Inherits BaseEvent
    data: BrokerConnectionEventData = field(default=None) # Now has a 'data' attribute of the new type

@dataclass(slots=True)
class OrderFilledEventData: # Using a separate data class for the payload
    order_id: str = "" # Broker's order ID
    local_order_id: Optional[str] = None # Internal system's order ID
    symbol: str = ""
    side: str = "UNKNOWN" # <<< MODIFIED: Use string to avoid circular import
    fill_quantity: float = 0.0
    fill_price: float = 0.0
    fill_timestamp: float = field(default_factory=time.time) # Epoch timestamp of the fill
    execution_id: Optional[str] = None  # Broker's execution ID for the fill
    exchange: Optional[str] = None  # Exchange where the fill occurred
    # Optional fields based on common needs
    # total_filled_for_order: Optional[float] = None
    # remaining_for_order: Optional[float] = None

@dataclass
class OrderFilledEvent(BaseEvent): # This event wraps the data
    data: OrderFilledEventData = field(default=None)


@dataclass(slots=True)
class OrderStatusUpdateData:
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    ephemeral_corr: Optional[str] = None  # <<< ADD THIS NEW FIELD
    symbol: str = ""
    status: str = "UNKNOWN"  # <<< MODIFIED: Use string to avoid circular import
    filled_quantity: float = 0.0
    average_fill_price: Optional[float] = None
    remaining_quantity: float = 0.0
    message: Optional[str] = None # Optional reason text, e.g., for rejection
    timestamp: float = field(default_factory=time.time) # Timestamp of this status update from broker if available, else event creation time
    correlation_id: Optional[str] = None  # Master correlation ID for end-to-end tracing

@dataclass
class OrderStatusUpdateEvent(BaseEvent):
    data: OrderStatusUpdateData = field(default=None)

# Note: OrderFilledEvent is a more specific event for fills. OrderStatusUpdateEvent covers all statuses.
# Depending on broker capabilities, one might imply the other or both might be published.

@dataclass(slots=True)
class OrderRejectedData: # More specific than a generic OrderStatusUpdateEvent for rejection
    local_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None # May not exist if rejected early
    symbol: str = ""
    reason: str = "" # Specific reason for rejection
    timestamp: float = field(default_factory=time.time)

@dataclass
class OrderRejectedEvent(BaseEvent):
    data: OrderRejectedData = field(default=None)

@dataclass(slots=True)
class OpenOrderSubmittedEventData:
    symbol: str = ""
    local_order_id: str = "" # The OrderRepository's local ID for this OPEN order
    # Add other fields if MDFS needs them for context, e.g.:
    # requested_quantity: Optional[float] = None
    # side: Optional[str] = None # Should be "BUY" or "SELL" (for OPEN_LONG/OPEN_SHORT)

@dataclass
class OpenOrderSubmittedEvent(BaseEvent):
    data: OpenOrderSubmittedEventData = field(default_factory=OpenOrderSubmittedEventData)

@dataclass(slots=True)
class PositionData:
    """Simple position data for AllPositionsData - used by broker bridge."""
    symbol: str = ""
    quantity: float = 0.0
    average_price: float = 0.0
    unrealized_pnl: Optional[float] = None
    last_update_timestamp: float = field(default_factory=time.time)

@dataclass(slots=True)
class BrokerPositionQuantityOnlyPushData:
    """Data for lean, real-time share quantity updates from broker."""
    symbol: str
    quantity: float  # The new total shares for the symbol from broker
    broker_timestamp: Optional[float] = None  # Timestamp FROM the broker for this specific update

@dataclass
class BrokerPositionQuantityOnlyPushEvent(BaseEvent):
    """Event for real-time quantity-only position updates from broker."""
    data: BrokerPositionQuantityOnlyPushData = field(default_factory=BrokerPositionQuantityOnlyPushData)

@dataclass(slots=True)
class BrokerFullPositionPushData:
    """Data for complete position information from broker."""
    symbol: str
    quantity: float
    average_price: float
    realized_pnl_day: Optional[float] = None
    unrealized_pnl_day: Optional[float] = None  # From broker's "unrealized_pnl" or "open_pl"
    total_pnl_day: Optional[float] = None       # From broker's "total_pnl" (or calculated if not directly provided)
    broker_timestamp: Optional[float] = None

@dataclass
class BrokerFullPositionPushEvent(BaseEvent):
    """Event for full position data updates from broker."""
    data: BrokerFullPositionPushData = field(default_factory=BrokerFullPositionPushData)

@dataclass(slots=True)
class BrokerSnapshotPositionData:
    """
    Position data structure for broker snapshots from IBroker.get_all_positions().
    Contains only what the broker provides in bulk position snapshots.
    Used by: AllPositionsData, broker bridge implementations.
    """
    symbol: str = ""
    quantity: float = 0.0
    average_price: float = 0.0
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    total_pnl: Optional[float] = None

@dataclass(slots=True)
class AllPositionsData: # For response to get_all_positions
    positions: List[BrokerSnapshotPositionData] = field(default_factory=list) # List of broker snapshot positions

@dataclass
class AllPositionsEvent(BaseEvent):
    data: AllPositionsData = field(default=None)

@dataclass(slots=True)
class AccountSummaryData:
    account_id: str = ""
    equity: float = 0.0
    buying_power: float = 0.0
    cash: float = 0.0
    realized_pnl_day: Optional[float] = None
    unrealized_pnl_day: Optional[float] = None
    # ... other relevant account fields ...
    timestamp: float = field(default_factory=time.time)

@dataclass
class AccountUpdateEvent(BaseEvent): # For unsolicited account updates or response to request
    data: AccountSummaryData = field(default=None)

@dataclass(slots=True)
class MarketDataTickData:
    symbol: str = ""
    price: float = 0.0
    volume: Optional[float] = None # Volume for this tick (trade) or size (quote)
    timestamp: float = field(default_factory=time.time) # Timestamp of the tick from the source if available, else arrival time
    tick_type: str = "" # e.g., "TRADE", "BID", "ASK"
    # Optional: bid_price: Optional[float] = None (if tick_type is BID)
    # Optional: ask_price: Optional[float] = None (if tick_type is ASK)
    # Optional: bid_size: Optional[float] = None
    # Optional: ask_size: Optional[float] = None
    # Optional: exchange: Optional[str] = None

@dataclass
class MarketDataTickEvent(BaseEvent): # As per SME feedback
    data: MarketDataTickData = field(default=None)

@dataclass(slots=True)
class MarketDataQuoteData:
    symbol: str = ""
    bid_price: float = 0.0
    ask_price: float = 0.0
    bid_size: int = 0  # Or float if fractional sizes are possible
    ask_size: int = 0  # Or float
    timestamp: float = field(default_factory=time.time)  # Epoch timestamp of the quote from the source
    # Optional: exchange: Optional[str] = None
    # Optional: conditions: Optional[List[str]] = None

@dataclass
class MarketDataQuoteEvent(BaseEvent):
    data: MarketDataQuoteData = field(default=None)

@dataclass(slots=True)
class BrokerErrorData:
    message: str = ""
    code: Optional[str] = None # Broker-specific error code
    details: Optional[Any] = None # Any additional structured error information
    is_critical: bool = False # Indicates if this error might require intervention or disconnect

@dataclass
class BrokerErrorEvent(BaseEvent):
    data: BrokerErrorData = field(default=None)

@dataclass(slots=True)
class BrokerRawMessageRelayEventData:
    """
    Data payload for raw broker messages that need to be relayed to OrderRepository
    for legacy-style processing. Used for SME Hybrid Approach.
    """
    raw_message_dict: Dict[str, Any] = field(default_factory=dict)  # Full JSON dict from broker
    original_local_copy_id: Optional[str] = None  # Extracted local_copy_id from the message
    source: Optional[str] = field(default="LightspeedBroker", compare=False)  # Source of the raw message
    message: Optional[str] = None  # Raw message as string
    timestamp: Optional[float] = None  # Timestamp when message was received

@dataclass
class BrokerRawMessageRelayEvent(BaseEvent):
    """
    Event for relaying raw broker messages to OrderRepository for legacy-style processing.
    Used when broker messages don't fit standard typed events but contain order-related data.
    """
    data: BrokerRawMessageRelayEventData = field(default=None)

# Import OCRParsedData from the modules.ocr.data_types module
from modules.ocr.data_types import OCRParsedData

@dataclass
class OCRParsedEvent(BaseEvent):
    """
    Event published when new parsed OCR data is available from the OCR process.
    The data attribute contains an OCRParsedData object.
    """
    # The 'data' attribute holds the OCRParsedData payload
    # This follows the same pattern as other events like OrderFilledEvent
    data: OCRParsedData = field(default=None)

    def __post_init__(self):
        """Post-initialization hook for optional correlation logging."""
        # --- START REWORK ---
        # REMOVE: self.data.event_id = self.event_id
        # (Ensures OCRParsedData.event_id, our master correlation_id, is preserved)
        # --- END REWORK ---

        # Keep the correlation logging part if it exists and is separate
        if hasattr(self, '_correlation_logger') and self._correlation_logger is not None:
            try:
                event_payload = {
                    'event_type': 'OCRParsedEvent',
                    'frame_number': getattr(self.data, 'frame_number', 'unknown') if self.data else 'unknown',
                    'timestamp': getattr(self.data, 'timestamp', 0.0) if self.data else 0.0
                }
                self._correlation_logger.log_event("CORE_EVENT", event_payload, "OCRParsedEvent")
            except Exception:
                pass  # Silently ignore logging errors


@dataclass(slots=True)
class CleanedOCRSnapshotEventData:
    frame_timestamp: float = 0.0
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict) # Dict[symbol, {'strategy_hint':str, 'cost_basis':float,...}]
    original_ocr_event_id: Optional[str] = None
    raw_ocr_confidence: Optional[float] = None
    origin_correlation_id: Optional[str] = None # Master correlation ID from source OCRParsedEvent for end-to-end tracing
    validation_id: Optional[str] = None # Pipeline validation ID for end-to-end tracking
    origin_timestamp_ns: Optional[int] = None # Golden timestamp captured at image grab (perf_counter_ns)
    
    # --- ANALYSIS METADATA FIELDS ---
    roi_used_for_capture: Optional[List[int]] = field(default_factory=list) # Exact ROI coordinates used for this frame
    preprocessing_params_used: Optional[Dict[str, Any]] = field(default_factory=dict) # Snapshot of preprocessing parameters

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization."""
        return {
            'frame_timestamp': self.frame_timestamp,
            'snapshots': self.snapshots,
            'original_ocr_event_id': self.original_ocr_event_id,
            'raw_ocr_confidence': self.raw_ocr_confidence,
            'origin_correlation_id': self.origin_correlation_id,
            'validation_id': self.validation_id,
            'origin_timestamp_ns': self.origin_timestamp_ns,
            'roi_used_for_capture': self.roi_used_for_capture,
            'preprocessing_params_used': self.preprocessing_params_used
        }

@dataclass
class CleanedOCRSnapshotEvent(BaseEvent):
    data: CleanedOCRSnapshotEventData = field(default_factory=CleanedOCRSnapshotEventData)

# --- Configuration Change Events ---
# These events are published when configuration is modified during runtime (hot reload)

@dataclass(slots=True)
class ConfigChangeEventData:
    """Configuration change event data for IntelliSense analysis audit trail."""
    change_timestamp: float = field(default_factory=time.time)  # Epoch timestamp of change
    change_source: str = "unknown"  # e.g., "file_watcher", "gui_command", "manual_edit"
    changed_parameters: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # {param: {old_value, new_value}}
    full_config_snapshot: Dict[str, Any] = field(default_factory=dict)  # Complete new configuration
    correlation_id: Optional[str] = None  # For linking to specific operations
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization."""
        return {
            'change_timestamp': self.change_timestamp,
            'change_source': self.change_source,
            'changed_parameters': self.changed_parameters,
            'full_config_snapshot': self.full_config_snapshot,
            'correlation_id': self.correlation_id
        }

@dataclass
class ConfigChangeEvent(BaseEvent):
    data: ConfigChangeEventData = field(default_factory=ConfigChangeEventData)

# --- Order Request Events ---
# These events are used for the order request workflow:
# OCRScalpingSignalGenerator -> OrderRequestEvent -> RiskManagementService -> ValidatedOrderRequestEvent/OrderRejectedByRiskEvent

@dataclass(slots=True)
class OrderRequestData:
    """
    Data payload for order requests generated by signal generators.
    Contains all necessary details for an order to be considered by risk management and then executed.
    """
    # Requester can generate a unique ID for this request for tracking, if desired
    request_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    side: str = ""  # For now, use string "BUY" or "SELL". Can be OrderSide enum later if preferred.
    quantity: float = 0.0
    order_type: str = "MKT"  # e.g., "MKT", "LMT"
    limit_price: Optional[float] = None  # Required if order_type is "LMT"
    stop_price: Optional[float] = None   # Required if order_type is "STP" or "STP LMT"
    time_in_force: str = "DAY"  # Default time in force
    source_strategy: str = ""  # Name of the strategy generating this request
    # Optional fields for additional context
    related_ocr_event_id: Optional[str] = None  # To link back to the OCRParsedEvent if needed
    client_order_id: Optional[str] = None  # If a specific client ID needs to be passed through
    # Additional metadata that might be useful for risk assessment or execution
    current_price: Optional[float] = None  # Current market price at time of signal generation
    current_position: Optional[float] = None  # Current position size for the symbol
    ocr_frame_timestamp: Optional[float] = None  # Timestamp from the OCR frame that generated this signal
    triggering_cost_basis: Optional[float] = None  # Cost basis from OCR that triggered this signal (for fingerprinting)
    validation_id: Optional[str] = None  # Pipeline validation ID for end-to-end tracking
    # Ensure a correlation_id for tracing this specific request through the pipeline
    correlation_id: str = field(default_factory=lambda: f"corr_{get_thread_safe_uuid()[:8]}")
    # Metadata for additional context and debugging information
    meta: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class OrderRequestEvent(BaseEvent):
    """
    Event published by signal generators (like OCRScalpingSignalGenerator) when they identify a trading opportunity.
    This event will be consumed by RiskManagementService for validation.
    """
    data: OrderRequestData = field(default=None)

    def __post_init__(self):
        """Post-initialization hook for optional correlation logging."""
        # This allows for optional correlation logging without breaking existing code
        # The logger can be injected via a class-level attribute or passed during creation
        if hasattr(self, '_correlation_logger') and self._correlation_logger is not None:
            try:
                event_payload = {
                    'event_type': 'OrderRequestEvent',
                    'request_id': self.data.request_id if self.data else 'unknown',
                    'symbol': self.data.symbol if self.data else 'unknown',
                    'side': self.data.side if self.data else 'unknown',
                    'quantity': self.data.quantity if self.data else 0.0,
                    'correlation_id': self.data.correlation_id if self.data else 'unknown'
                }
                self._correlation_logger.log_event("CORE_EVENT", event_payload, "OrderRequestEvent")
            except Exception:
                pass  # Silently ignore logging errors to avoid breaking core functionality

@dataclass
class ValidatedOrderRequestEvent(BaseEvent):
    """
    Event published by RiskManagementService when an OrderRequestEvent passes all risk checks.
    This event will be consumed by TradeExecutor for actual order placement.
    """
    # For simplicity, ValidatedOrderRequestData reuses OrderRequestData structure
    # If distinct fields are needed later, create a separate ValidatedOrderRequestData dataclass.
    # For now, OrderRequestData is sufficient.
    data: OrderRequestData = field(default=None)

@dataclass(slots=True)
class OrderRejectedByRiskData:
    """
    Data payload for orders rejected by risk management.
    Contains the original request details and the reason for rejection.
    """
    original_request_id: str = ""  # From the OrderRequestData that was rejected
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    order_type: str = ""
    reason: str = ""  # e.g., "Max order size exceeded", "Symbol not allowed"
    timestamp: float = field(default_factory=time.time)
    # Optional fields for more detailed context
    risk_check_details: Optional[Dict[str, Any]] = None  # For more detailed risk assessment context
    original_request: Optional[OrderRequestData] = None  # Full original request for reference
    __validation_id: Optional[str] = None  # Pipeline validation ID for end-to-end tracking

@dataclass
class OrderRejectedByRiskEvent(BaseEvent):
    """
    Event published by RiskManagementService when an OrderRequestEvent fails risk checks.
    This provides feedback to signal generators and logging systems about rejected orders.
    """
    data: OrderRejectedByRiskData = field(default=None)

# --- Risk Assessment Event for Pre-Trade Risk Validation Chain ---

@dataclass(slots=True)
class RiskAssessmentEventData:
    """
    Data payload for risk assessment decisions made by the RiskManagementService.
    Contains detailed risk validation results and timing information for T13-T15 milestone analysis.
    """
    # Core order identification
    original_request_id: str = ""  # From the OrderRequestData that was assessed
    symbol: str = ""
    side: str = ""
    quantity: float = 0.0
    order_type: str = ""
    
    # Risk assessment decision
    decision: str = ""  # "VALIDATED" or "REJECTED"
    rejection_reason: Optional[str] = None  # Populated if decision is "REJECTED"
    
    # Timing milestones for T13-T15 analysis
    T13_RiskCheckStart_ns: Optional[int] = None  # Risk validation pipeline start
    T14_RiskCheckEnd_ns: Optional[int] = None    # Risk validation pipeline end
    T15_RiskEventPublished_ns: Optional[int] = None  # Risk assessment event published
    
    # Risk check details and context
    risk_check_details: Optional[Dict[str, Any]] = None  # Detailed risk assessment context
    risk_scores: Optional[Dict[str, float]] = None  # Individual risk component scores
    
    # Causation and correlation tracking
    source_event_id: Optional[str] = None  # OrderRequestEvent.event_id that triggered this assessment
    correlation_id: Optional[str] = None   # End-to-end correlation ID
    validation_id: Optional[str] = None    # Pipeline validation ID
    
    # Assessment metadata
    timestamp: float = field(default_factory=time.time)
    assessment_duration_ns: Optional[int] = None  # Calculated T14 - T13
    
    # Optional context for comprehensive analysis
    current_price: Optional[float] = None  # Price at time of assessment
    risk_limits_applied: Optional[Dict[str, Any]] = None  # Risk limits that were checked
    portfolio_context: Optional[Dict[str, Any]] = None  # Portfolio state during assessment
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class RiskAssessmentEvent(BaseEvent):
    """
    Event published by RiskManagementService after completing risk assessment of an OrderRequestEvent.
    This event provides detailed risk validation results, timing analysis, and decision context
    for both VALIDATED and REJECTED scenarios. Essential for T13-T15 milestone timing analysis.
    """
    data: RiskAssessmentEventData = field(default=None)

# --- Task 1.3: New Event Types for Price Repository ---

@dataclass(slots=True)
class SignificantPriceMoveEventData: # Or a more generic name if it can cover quote changes too
    symbol: str = ""
    price: float = 0.0
    timestamp: float = field(default_factory=time.time)
    volume: Optional[float] = None # For trades
    spread: Optional[float] = None # For quotes if a quote change is "significant"
    reason: str = "" # e.g., "Crossed 200MA", "1% move in 1s"
    # Add other relevant context

@dataclass
class SignificantPriceMoveEvent(BaseEvent):
    data: SignificantPriceMoveEventData = field(default_factory=SignificantPriceMoveEventData)

@dataclass(slots=True)
class SymbolRiskLevelChangedEventData:
    symbol: str = ""
    previous_level: Optional[str] = None # Name of RiskLevel enum
    new_level: str = "" # Name of RiskLevel enum
    risk_score: Optional[float] = None
    details: Optional[Dict[str, Any]] = None # From PMD or risk assessment
    timestamp: float = field(default_factory=time.time)

@dataclass
class SymbolRiskLevelChangedEvent(BaseEvent):
    data: SymbolRiskLevelChangedEventData = field(default_factory=SymbolRiskLevelChangedEventData)

# --- Position Update Events for Redis Publishing ---

@dataclass(slots=True)
class PositionUpdateEventData:
    """Data for position updates to be published to Redis streams."""
    symbol: str = ""
    quantity: float = 0.0  # Net shares
    average_price: float = 0.0
    realized_pnl_session: Optional[float] = None  # PnL realized during this session for this symbol
    is_open: bool = False
    strategy: Optional[str] = None  # e.g., "long", "closed"
    last_update_timestamp: float = field(default_factory=time.time)
    # Add master_correlation_id IF the position update was due to an order fill
    # that can be traced back. This is tricky for general position updates.
    # For now, let's assume correlation_id on metadata will be for the PositionUpdateEvent itself.
    # A specific causing_correlation_id (master_ocr_id) could be added if an order fill caused this.
    causing_correlation_id: Optional[str] = None
    update_source_trigger: Optional[str] = None  # e.g., "FILL", "SYNC", "MANUAL_ADJUST"

    # Enhanced metadata fields
    position_opened_timestamp: Optional[float] = None  # When position was first opened
    last_fill_timestamp: Optional[float] = None  # Timestamp of most recent fill
    total_fills_count: int = 0  # Number of fills for this position
    session_buy_quantity: float = 0.0  # Total shares bought this session
    session_sell_quantity: float = 0.0  # Total shares sold this session
    session_buy_value: float = 0.0  # Total dollar value bought this session
    session_sell_value: float = 0.0  # Total dollar value sold this session

    # IntelliSense tracking fields
    position_uuid: Optional[str] = None  # Unique identifier for this position instance
    opening_signal_correlation_id: Optional[str] = None  # OCR event that triggered position open
    opening_order_correlation_id: Optional[str] = None  # Order that opened the position
    last_signal_correlation_id: Optional[str] = None  # Most recent OCR signal affecting position
    last_order_correlation_id: Optional[str] = None  # Most recent order affecting position
    position_opened_timestamp_ns: Optional[int] = None  # Nanosecond precision open time
    last_fill_timestamp_ns: Optional[int] = None  # Nanosecond precision last fill time
    signal_to_fill_latency_ms: Optional[float] = None  # Latency from signal to most recent fill
    order_to_fill_latency_ms: Optional[float] = None  # Latency from order to most recent fill
    
    # --- THE FIX: Add missing fields for PositionManager compatibility ---
    causation_id: Optional[str] = None  # Specific causation ID for this position update
    update_source: Optional[str] = None  # Source of the update (may differ from update_source_trigger)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class PositionUpdateEvent(BaseEvent):
    data: PositionUpdateEventData = field(default=None)

# --- Risk Trading Stopped Events for Redis Publishing ---

@dataclass(slots=True)
class RiskTradingStoppedData:
    """Data for risk trading stopped events to be published to Redis streams."""
    symbol: Optional[str] = None  # Symbol if stop is symbol-specific, else None for global
    reason_code: str = ""  # E.g., "MARKET_VOLATILITY_HALT", "MELTDOWN_DETECTED"
    description: str = ""  # Human-readable details
    risk_level_at_stoppage: str = ""  # Value of RiskLevel enum, e.g., "CRITICAL"
    timestamp: float = field(default_factory=time.time)
    details: Optional[Dict[str, Any]] = None  # e.g., metrics that triggered
    # If stop was related to a specific order flow, include its correlation_id
    triggering_correlation_id: Optional[str] = None

@dataclass
class RiskTradingStoppedEvent(BaseEvent):  # For internal bus if needed
    data: RiskTradingStoppedData = field(default=None)

# --- Symbol Publishing State Events ---

@dataclass(slots=True)
class SymbolPublishingStateChangedData:
    """Data for symbol publishing state change events."""
    symbol: str = ""
    should_publish: bool = False
    reason: str = ""  # e.g., "POSITION_OPENED", "POSITION_CLOSED", "ORDER_PENDING"
    
@dataclass
class SymbolPublishingStateChangedEvent(BaseEvent):
    """Event published when a symbol's publishing state should change."""
    data: SymbolPublishingStateChangedData = field(default_factory=SymbolPublishingStateChangedData)

# --- Active Symbol Set Events ---

@dataclass(slots=True)
class ActiveSymbolSetChangedData:
    """
    Data for active symbol set change events.
    
    MEMORY OPTIMIZATION: Uses frozen set for immutability and memory efficiency.
    The frozenset can be safely shared between event and MarketDataFilterService
    without copying, reducing memory overhead significantly.
    """
    active_symbols: frozenset = field(default_factory=frozenset)  # Immutable set - memory efficient
    change_type: str = ""  # "SYMBOL_ADDED", "SYMBOL_REMOVED", "FULL_UPDATE"
    changed_symbol: str = ""  # The specific symbol that changed (if applicable)
    reason: str = ""  # e.g., "POSITION_OPENED", "POSITION_CLOSED", "ORDER_FILLED"
    
@dataclass
class ActiveSymbolSetChangedEvent(BaseEvent):
    """Event published when the set of active symbols changes."""
    data: ActiveSymbolSetChangedData = field(default_factory=ActiveSymbolSetChangedData)

# --- API Fallback Events for Observability ---

@dataclass(slots=True)
class ApiFallbackEventData:
    """Data for API fallback events when PriceRepository uses API for stale/missing cache data."""
    symbol: str = ""
    data_type: str = ""  # e.g., "LATEST_PRICE", "QUOTE_BID", "QUOTE_ASK"
    reason: str = ""     # e.g., "CacheStale", "CacheEmpty", "CacheZero"
    api_source: str = "" # e.g., "AlpacaREST_get_latest_trade", "AlpacaREST_get_latest_quote"
    timestamp: float = field(default_factory=time.time)

@dataclass
class ApiFallbackEvent(BaseEvent):
    """Event published when PriceRepository successfully uses API fallback for market data."""
    data: ApiFallbackEventData = field(default_factory=ApiFallbackEventData)

# --- Signal Decision Events for OCR Scalping Signal Orchestrator ---

@dataclass(slots=True)
class SignalDecisionEventData:
    """
    Data payload for signal decision events with metadata/payload separation.
    Contains detailed information about signal generation decisions and their context.
    """
    # Core decision metadata
    decision_type: str = ""  # "SIGNAL_GENERATED", "SIGNAL_SUPPRESSED", "VALIDATION_FAILURE", "GENERATION_FAILURE"
    symbol: str = ""
    timestamp: float = field(default_factory=time.time)
    
    # Causation chain metadata
    master_correlation_id: Optional[str] = None  # End-to-end correlation from original OCR event
    causation_id: Optional[str] = None  # ID of the cleaned OCR event that caused this decision
    origin_ocr_event_id: Optional[str] = None  # Original raw OCR event ID
    
    # Signal processing context
    signal_action: Optional[str] = None  # TradeActionType.name if signal generated
    signal_quantity: Optional[float] = None  # Calculated quantity
    signal_reason: Optional[str] = None  # Reason for generation/suppression
    
    # Timing and latency data (T7-T10 milestones)
    t7_orchestration_start_ns: Optional[int] = None  # Orchestration start timing
    t8_interpretation_end_ns: Optional[int] = None   # Interpretation completion timing
    t9_filtering_end_ns: Optional[int] = None        # Filtering completion timing
    t10_decision_ready_ns: Optional[int] = None      # Order request ready/suppression decision timing
    
    # OCR processing context
    ocr_frame_timestamp: Optional[float] = None  # Original OCR frame timestamp
    ocr_confidence: Optional[float] = None       # OCR confidence score
    
    # Market context at decision time
    current_price: Optional[float] = None        # Market price when decision made
    current_position: Optional[float] = None     # Current position shares
    triggering_cost_basis: Optional[float] = None  # Cost basis that triggered the signal
    
    # Validation and pipeline tracking
    validation_id: Optional[str] = None          # Pipeline validation ID
    fingerprint_data: Optional[str] = None       # Fingerprint for duplicate detection
    
    # Additional context for failures
    failure_context: Optional[Dict[str, Any]] = None  # Additional failure details
    
    # Order request details (if signal generated)
    order_request_id: Optional[str] = None       # Generated order request ID
    order_type: Optional[str] = None             # MKT, LMT, etc.
    order_side: Optional[str] = None             # BUY, SELL
    limit_price: Optional[float] = None          # Limit price if applicable
    
    # Source component identification
    source_component: str = "OCRScalpingSignalOrchestratorService"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class SignalDecisionEvent(BaseEvent):
    """
    Event for signal generation decisions with comprehensive metadata tracking.
    Supports both signal generation and suppression scenarios with full causation chains.
    """
    data: SignalDecisionEventData = field(default_factory=SignalDecisionEventData)

# --- Broker Order Submission Events ---

@dataclass(slots=True)
class BrokerOrderSubmittedEventData:
    """
    Data payload for broker order submission events with comprehensive timing and context.
    Captures the final submission step in the pre-trade pipeline with T16-T17 milestone timing.
    """
    # Core order identification
    symbol: str = ""
    local_order_id: Optional[str] = None  # Internal system's order ID
    order_side: str = ""  # BUY, SELL
    order_quantity: float = 0.0
    order_type: str = "MKT"  # MKT, LMT, etc.
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    
    # Broker submission details
    broker_payload: Optional[Dict[str, Any]] = None  # Complete payload sent to broker
    submission_timestamp: float = field(default_factory=time.time)
    submission_successful: bool = False
    
    # T16-T17 milestone timing for trade execution pipeline
    T16_ExecutionStart_ns: Optional[int] = None  # Trade execution pipeline start
    T17_BrokerSubmission_ns: Optional[int] = None  # Broker submission completion
    execution_duration_ns: Optional[int] = None  # Calculated T17 - T16
    
    # Causation and correlation tracking
    source_event_id: Optional[str] = None  # Event ID that triggered this execution
    correlation_id: Optional[str] = None   # End-to-end correlation ID
    validation_id: Optional[str] = None    # Pipeline validation ID
    
    # Risk assessment causation (linking to T13-T15 events)
    risk_assessment_event_id: Optional[str] = None  # RiskAssessmentEvent that validated this order
    order_request_id: Optional[str] = None  # Original OrderRequestEvent ID
    
    # Market context at submission
    current_price: Optional[float] = None  # Market price at submission time
    current_position: Optional[float] = None  # Position shares before this order
    
    # Order parameters context
    action_type: Optional[str] = None  # TradeActionType name
    is_manual_trigger: bool = False
    aggressive_chase: bool = False
    
    # Pipeline context for end-to-end analysis
    ocr_correlation_id: Optional[str] = None  # Original OCR event correlation ID
    signal_correlation_id: Optional[str] = None  # Signal generation correlation ID
    
    # Additional submission metadata
    extended_hours_enabled: bool = False
    fingerprint_data: Optional[str] = None  # Fingerprint used for duplicate detection
    
    # Error context (if submission failed)
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    
    # Source component identification
    source_component: str = "TradeExecutor"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class BrokerOrderSubmittedEvent(BaseEvent):
    """
    Event published when an order is submitted to the broker, completing the T16-T17 execution pipeline.
    This represents the final step in the pre-trade instrumentation chain from T0 (image capture) 
    to T17 (broker submission). Contains comprehensive timing, causation, and submission context.
    """
    data: BrokerOrderSubmittedEventData = field(default_factory=BrokerOrderSubmittedEventData)

# --- Broker Gateway Communication Events ---

@dataclass(slots=True)
class BrokerOrderSentEventData:
    """
    Data payload for broker order sent events with T18 milestone timing.
    Captures the exact moment when an order is sent to the external broker gateway,
    bridging internal system and external market communication.
    """
    # Core order identification  
    symbol: str = ""
    local_order_id: Optional[str] = None  # Internal system's order ID
    order_side: str = ""  # BUY, SELL
    order_quantity: float = 0.0
    order_type: str = "MKT"  # MKT, LMT, etc.
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    
    # Broker gateway submission details
    broker_request_id: Optional[str] = None  # Broker's request ID for tracking
    broker_payload: Optional[Dict[str, Any]] = None  # Complete payload sent to broker
    broker_endpoint: Optional[str] = None  # Broker endpoint/connection info
    
    # T18 milestone timing for broker send
    T18_BrokerSend_ns: Optional[int] = None  # Precise broker send timing
    submission_timestamp: float = field(default_factory=time.time)
    submission_successful: bool = False
    
    # Causation and correlation tracking
    correlation_id: Optional[str] = None   # End-to-end correlation ID for tracking
    source_event_id: Optional[str] = None  # Event ID that triggered this send
    validation_id: Optional[str] = None    # Pipeline validation ID
    
    # Error handling context
    error_message: Optional[str] = None  # Error details if submission failed
    retry_count: int = 0  # Number of retry attempts
    
    # Source component identification
    source_component: str = "BrokerBridge"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class BrokerOrderSentEvent(BaseEvent):
    """
    Event published when an order is sent to the broker gateway, capturing T18 milestone timing.
    This event bridges the gap between internal order execution (T17) and external broker 
    communication, enabling precise tracking of the order lifecycle through the broker gateway.
    """
    data: BrokerOrderSentEventData = field(default_factory=BrokerOrderSentEventData)

# --- Master Action Filter Duty Cycle Events ---

@dataclass(slots=True)
class MAFDutyCycleEventData:
    """
    Data payload for Master Action Filter duty cycle events with metadata/payload separation.
    Contains detailed information about MAF filtering decisions and their context.
    """
    # Core decision metadata
    decision_type: str = ""  # "SIGNAL_PASSED", "SIGNAL_SUPPRESSED", "SETTLING_OVERRIDE", "PASSTHROUGH", "ERROR"
    symbol: str = ""
    action: str = ""  # "ADD", "REDUCE", "RESET", etc.
    timestamp: float = field(default_factory=time.time)
    
    # Causation chain metadata - maintain flow from incoming signal
    master_correlation_id: Optional[str] = None  # End-to-end correlation from original trade signal
    causation_id: Optional[str] = None  # ID of the trade signal that caused this filtering decision
    origin_signal_event_id: Optional[str] = None  # Original trade signal event ID
    
    # MAF filtering decision context
    filtering_reason: Optional[str] = None  # Detailed reason for the decision
    decision_details: Optional[Dict[str, Any]] = None  # Additional decision context
    
    # Timing and latency data (T9-T10 milestones)
    t9_filtering_start_ns: Optional[int] = None  # MAF filtering start timing
    t10_filtering_end_ns: Optional[int] = None   # MAF filtering completion timing
    
    # Signal processing context
    signal_action: Optional[str] = None  # TradeActionType from incoming signal
    signal_quantity: Optional[float] = None  # Signal quantity if available
    signal_triggering_snapshot: Optional[Dict[str, Any]] = None  # Triggering OCR snapshot
    
    # MAF state context at filtering time
    current_ocr_cost: Optional[float] = None     # Current cost basis from OCR
    current_ocr_rpnl: Optional[float] = None     # Current realized P&L from OCR
    current_market_price: Optional[float] = None # Market price at filtering time
    
    # Settling state information
    is_cost_settling: Optional[bool] = None      # Whether in cost ADD settling period
    is_rpnl_settling: Optional[bool] = None      # Whether in rPnL REDUCE settling period
    settling_end_timestamp: Optional[float] = None  # When settling period ends
    time_remaining_in_settling: Optional[float] = None  # Seconds remaining in settling
    
    # Baseline values for override calculations
    baseline_cost: Optional[float] = None        # Baseline cost from last system action
    baseline_rpnl: Optional[float] = None        # Baseline rPnL from last system action
    baseline_price: Optional[float] = None       # Baseline price from last system action
    
    # Delta calculations for override logic
    abs_delta_cost: Optional[float] = None       # Absolute cost change since baseline
    delta_rpnl: Optional[float] = None           # rPnL change since baseline
    price_delta_abs: Optional[float] = None      # Absolute price change
    price_delta_pct: Optional[float] = None      # Percentage price change
    
    # Configuration values at decision time
    override_thresholds: Optional[Dict[str, float]] = None  # Relevant threshold values
    settling_durations: Optional[Dict[str, float]] = None   # Current settling durations
    
    # Output signal information
    output_signal_id: Optional[str] = None       # ID of output signal if passed through
    signal_modified: Optional[bool] = None       # Whether signal was modified during filtering
    attached_metadata: Optional[Dict[str, Any]] = None  # Metadata attached to output signal
    
    # Source component identification
    source_component: str = "MasterActionFilterService"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for Redis serialization using dataclasses.asdict()."""
        from dataclasses import asdict
        return asdict(self)

@dataclass
class MAFDutyCycleEvent(BaseEvent):
    """
    Event for Master Action Filter duty cycle tracking with comprehensive metadata.
    Supports signal filtering, suppression, override, and passthrough scenarios with full causation chains.
    """
    data: MAFDutyCycleEventData = field(default_factory=MAFDutyCycleEventData)

# --- SYMBOL ACTIVATION/DEACTIVATION EVENTS FOR BLOOM FILTER ---

@dataclass(slots=True)
class SymbolActivatedEventData:
    """Data payload for symbol activation event."""
    symbol: str = ""
    reason: str = ""  # e.g., "POSITION_OPENED", "ORDER_SUBMITTED"
    timestamp: float = field(default_factory=time.time)

@dataclass
class SymbolActivatedEvent(BaseEvent):
    """Event published when a symbol becomes active (position opened, order submitted)."""
    data: SymbolActivatedEventData = field(default=None)

@dataclass(slots=True)  
class SymbolDeactivatedEventData:
    """Data payload for symbol deactivation event."""
    symbol: str = ""
    reason: str = ""  # e.g., "POSITION_CLOSED", "ORDER_FILLED"
    timestamp: float = field(default_factory=time.time)

@dataclass
class SymbolDeactivatedEvent(BaseEvent):
    """Event published when a symbol becomes inactive (position closed, no pending orders)."""
    data: SymbolDeactivatedEventData = field(default=None)