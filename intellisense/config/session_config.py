"""
IntelliSense Session Configuration

This module provides configuration classes for IntelliSense test sessions
and component-specific test configurations.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List


@dataclass
class OCRTestConfig:
    """Configuration for OCR intelligence engine testing."""
    test_param: str = "default_ocr_test"
    confidence_threshold: float = 0.7
    validation_enabled: bool = True
    performance_tracking: bool = True
    additional_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PriceTestConfig:
    """Configuration for price intelligence engine testing."""
    test_param: str = "default_price_test"
    symbols: List[str] = field(default_factory=lambda: ["AAPL", "MSFT"])
    price_validation_enabled: bool = True
    latency_tracking: bool = True
    target_stress_tps: float = 100.0  # For placeholder engine
    additional_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerTestConfig:
    """Configuration for broker intelligence engine testing."""
    test_param: str = "default_broker_test"
    order_validation_enabled: bool = True
    fill_tracking: bool = True
    response_latency_tracking: bool = True
    target_stress_tps: float = 50.0  # For placeholder engine
    delay_profile: str = "normal"  # For placeholder engine
    additional_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestSessionConfig:
    """Configuration for a complete IntelliSense test session."""
    session_name: str = "default_test_session"
    description: str = "IntelliSense test session"
    test_date: str = "2024-01-01"  # YYYY-MM-DD

    # --- DEPRECATED / To be removed once Redis is fully primary ---
    controlled_trade_log_path: Optional[str] = None # For OCR events
    historical_price_data_path_sync: Optional[str] = None
    historical_price_data_path_stress: Optional[str] = None
    broker_response_log_path: Optional[str] = None
    # --- END DEPRECATED ---

    duration_minutes: int = 60
    speed_factor: float = 1.0 # For replay from files, may be less relevant for Redis live consumption

    ocr_config: OCRTestConfig = field(default_factory=OCRTestConfig)
    price_config: PriceTestConfig = field(default_factory=PriceTestConfig)
    broker_config: BrokerTestConfig = field(default_factory=BrokerTestConfig)

    injection_plan_path: Optional[str] = None # For MOC

    # --- NEW: Redis Configuration ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None

    # Specific stream names, with defaults matching BA12's spec
    # These allow overriding if TESTRADE's stream names change or for specific test setups
    redis_stream_raw_ocr: str = "testrade:raw-ocr-events"
    redis_stream_cleaned_ocr: str = "testrade:cleaned-ocr-snapshots"
    redis_stream_order_requests: str = "testrade:order-requests"
    redis_stream_validated_orders: str = "testrade:validated-orders"
    redis_stream_fills: str = "testrade:order-fills"
    redis_stream_status: str = "testrade:order-status"
    redis_stream_rejections: str = "testrade:order-rejections"
    redis_stream_market_trades: str = "testrade:market-data:trades"
    redis_stream_market_quotes: str = "testrade:market-data:quotes"

    # Additional streams (optional - high memory usage)
    redis_stream_image_grabs: str = "testrade:image-grabs"
    redis_stream_processed_roi_images: str = "testrade:processed-roi-image-frames"  # NEW: Processed ROI images
    redis_stream_core_status: str = "testrade:core-status-events"
    redis_stream_position_summary: str = "testrade:position-summary"

    # New system event streams for TimelineEvent types
    redis_stream_config_changes: str = "testrade:config-changes"
    redis_stream_trading_state_changes: str = "testrade:trading-state-changes"
    redis_stream_trade_lifecycle_events: str = "testrade:trade-lifecycle-events"
    redis_stream_system_alerts: str = "testrade:system-alerts"

    redis_consumer_group_name: str = "intellisense-group" # Default group name
    # Consumer name prefix will be used by PDS to generate unique consumer names like "pds_consumer_xyz123"
    redis_consumer_name_prefix: str = "pds_consumer"
    # --- END NEW Redis Configuration ---

    # Existing config fields
    monitoring_enabled: bool = True
    dashboard_update_interval_sec: float = 5.0
    metrics_collection_interval_sec: float = 1.0
    validation_enabled: bool = True
    correlation_tolerance_ms: float = 10.0 # May be less relevant if using source timestamps
    accuracy_threshold_percent: float = 95.0
    output_directory: Optional[str] = None # For logs, reports

    session_timeout_seconds: Optional[int] = None
    additional_params: Dict[str, Any] = field(default_factory=dict)
