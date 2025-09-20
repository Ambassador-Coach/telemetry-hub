import logging
import time
import os
import json
import threading
import uuid  # For unique consumer names in data sources & thread names
import dataclasses  # For asdict conversion
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from intellisense.core.interfaces import IIntelliSenseApplicationCore, IDataSource  # Added IDataSource
    from .interfaces import IFeedbackIsolationManager, ICorrelationLogger
    from .optimization_capture import MillisecondOptimizationCapture
    from .bootstrap import PrecisionBootstrap
    from intellisense.core.types import TimelineEvent  # Base for typing

# Import the new Redis-based DataSources
# Lazy imports to avoid circular import
# from intellisense.engines.datasources.redis_ocr_raw_datasource import RedisRawOCRDataSource
# from intellisense.engines.datasources.redis_market_data_source import RedisMarketDataDataSource
# from intellisense.engines.datasources.redis_broker_datasource import RedisBrokerDataSource
# Conceptual import for Cleaned OCR if it's a separate class/stream
# from intellisense.engines.datasources.redis_ocr_cleaned_datasource import RedisCleanedOCRDataSource

from .logger import CorrelationLogger
from .utils import get_next_global_sequence_id, reset_global_sequence_id_generator
from intellisense.core.internal_event_bus import InternalIntelliSenseEventBus
from .scenario_types import InjectionStep, ValidationPoint

logger = logging.getLogger(__name__)


class ProductionDataCaptureSession:  # No longer implements IPriceDataListener etc.
    DEFAULT_CAPTURE_BASE_DIR = "./intellisense_capture_logs"

    def __init__(self,
                 testrade_instance: 'IIntelliSenseApplicationCore',
                 capture_session_config: Dict[str, Any]):
        self.testrade = testrade_instance
        self.session_config_dict = capture_session_config  # Store the dict for data sources
        self.capture_mode = capture_session_config.get("mode", "OBSERVATIONAL").upper()
        self.custom_session_prefix = capture_session_config.get("session_name_prefix", "capture")
        self.injection_plan_path = capture_session_config.get("injection_plan_path")
        self.target_symbols = capture_session_config.get("target_symbols", [])

        self._capture_active = False
        self._correlation_logger: Optional[ICorrelationLogger] = None
        self._session_name: Optional[str] = None
        self._session_path: Optional[str] = None

        self._bootstrap_ocr_monitors: Dict[str, threading.Event] = {}
        self._bootstrap_expected_shares: Dict[str, float] = {}
        self._bootstrap_monitor_lock = threading.Lock()

        self._internal_event_bus = InternalIntelliSenseEventBus()

        self.feedback_isolator: Optional[IFeedbackIsolationManager] = None
        self.precision_bootstrap: Optional['PrecisionBootstrap'] = None
        self.millisecond_capture_orchestrator: Optional['MillisecondOptimizationCapture'] = None

        self._data_sources: List['IDataSource'] = []  # Store IDataSource instances
        self._data_source_threads: List[threading.Thread] = []
        self._stop_all_ds_threads_event = threading.Event()

        if self.capture_mode == "CONTROLLED_INJECTION":
            # ... (FIM, PrecisionBootstrap, MOC instantiation as before) ...
            from .feedback_isolation_manager import FeedbackIsolationManager
            testable_pm = None  # Placeholder for brevity
            if hasattr(testrade_instance, 'position_manager') and testrade_instance.position_manager.__class__.__name__ == 'IntelliSenseTestablePositionManager':
                testable_pm = testrade_instance.position_manager
            self.feedback_isolator = FeedbackIsolationManager(testable_position_manager=testable_pm)
            from .bootstrap import PrecisionBootstrap
            self.precision_bootstrap = PrecisionBootstrap(app_core=self.testrade, feedback_isolator=self.feedback_isolator, capture_session_ref=self)
            if self.injection_plan_path:
                from .optimization_capture import MillisecondOptimizationCapture
                self.millisecond_capture_orchestrator = MillisecondOptimizationCapture(
                    app_core=self.testrade, feedback_isolator=self.feedback_isolator,
                    correlation_logger=None, internal_event_bus=self._internal_event_bus
                )
        logger.info(f"ProductionDataCaptureSession initialized for {self.capture_mode} mode (Redis consumer strategy).")

    def _get_next_gsi_for_datasource(self) -> int:
        """Provides global sequence IDs to data sources. Thread-safe due to GIL on get_next_global_sequence_id."""
        return get_next_global_sequence_id()

    def get_data_source_status(self) -> Dict[str, Any]:
        """Get current status of all configured Redis data sources."""
        status = {
            "capture_active": self._capture_active,
            "session_name": self._session_name,
            "data_sources_count": len(self._data_sources),
            "logging_threads_count": len(self._data_source_threads),
            "data_sources": []
        }

        for ds in self._data_sources:
            ds_info = {
                "name": ds.__class__.__name__,
                "active": getattr(ds, 'is_active', lambda: False)(),
                "type": "Redis-based"
            }
            status["data_sources"].append(ds_info)

        return status

    def _log_event_from_datasource_thread(self, timeline_event: 'TimelineEvent'):
        """
        Central method for threads to log events via CorrelationLogger.
        Also handles dispatching OCR events to bootstrap checker.
        """
        if not self._capture_active or not self._correlation_logger:
            return

        try:
            # Log the event using IntelliSense's CorrelationLogger
            # The TimelineEvent already has all necessary fields (GSI, timestamps, correlation_id, etc.)
            # CorrelationLogger.log_event expects event_payload as a dict.
            # TimelineEvent.data is already a dataclass (e.g. OCRTimelineEventData).
            # We need to ensure the payload sent to log_event is structured correctly.
            # Typically, it's the event_payload from the original Redis message which is
            # already in timeline_event.data.raw_payload (if we stored it), or we reconstruct.
            # For now, let's assume timeline_event.data (which is e.g. OCRTimelineEventData) can be asdict'd
            # and contains all necessary fields including original correlation IDs from metadata.
            # The main fields of TimelineEvent (GSI, timestamps, source_sense, event_type, correlation_id)
            # are used by CorrelationLogger's _write_log_entry_to_file to form the log structure.
            # The event_payload for the log will be asdict(timeline_event.data).

            payload_for_log = {}
            if timeline_event.data:
                if hasattr(timeline_event.data, 'to_dict') and callable(timeline_event.data.to_dict):
                    payload_for_log = timeline_event.data.to_dict()
                elif dataclasses.is_dataclass(timeline_event.data):  # Check if it's a dataclass
                    payload_for_log = dataclasses.asdict(timeline_event.data)
                elif isinstance(timeline_event.data, dict):
                    payload_for_log = timeline_event.data  # If it's already a dict
                else:
                    logger.warning(f"PDS: timeline_event.data for GSI {timeline_event.global_sequence_id} is not a dict or dataclass. Logging limited info.")
                    payload_for_log = {"info": f"Data for {timeline_event.event_type} not fully serializable"}

            # Add key trace IDs from metadata if they were stored on TimelineEvent directly and not in .data
            # (Based on current TimelineEvent, correlation_id is top-level)
            if timeline_event.correlation_id and 'correlation_id' not in payload_for_log:
                payload_for_log['correlation_id_from_metadata'] = timeline_event.correlation_id
            if timeline_event.source_info and timeline_event.source_info.get('redis_stream_event_id') and \
               'redis_stream_event_id' not in payload_for_log:
                payload_for_log['redis_stream_event_id'] = timeline_event.source_info['redis_stream_event_id']

            # Import the new event types at the top of session.py
            from intellisense.core.types import ProcessedImageFrameTimelineEvent, VisualTimelineEvent

            if isinstance(timeline_event, ProcessedImageFrameTimelineEvent):
                # Specific handling for processed images to save image bytes and log path
                if self._correlation_logger and hasattr(self._correlation_logger, 'log_processed_image_event'):
                    # The ProcessedImageFrameTimelineEvent.data.image_bytes_b64 should be populated
                    # by its from_redis_message method.
                    self._correlation_logger.log_processed_image_event(timeline_event) # type: ignore
                else:
                    logger.warning("PDS: CorrelationLogger not available or no log_processed_image_event method for ProcessedImageFrameTimelineEvent.")
                return  # Skip the regular logging path
            elif isinstance(timeline_event, VisualTimelineEvent): # Generic VisualTimelineEvent from old image_datasource
                 # This branch is for the old `_on_visual_sense_event` which might still be wired to `RedisImageDataSource`
                 # It uses `log_visual_event`. This needs to be reconciled if `RedisImageDataSource` is replaced or refactored.
                 # For now, assume `RedisProcessedImageDataSource` is the one used for `testrade:processed-roi-image-frames`.
                 if self._correlation_logger and hasattr(self._correlation_logger, 'log_visual_event'):
                    self._correlation_logger.log_visual_event(timeline_event) # type: ignore
                 else:
                    logger.warning("PDS: CorrelationLogger not available or no log_visual_event method for VisualTimelineEvent.")
                 return  # Skip the regular logging path

            # Prepare additional fields for BrokerTimelineEvent
            extra_broker_fields = {}
            if timeline_event.source_sense == "BROKER":
                # Import BrokerTimelineEvent for type checking
                from intellisense.core.types import BrokerTimelineEvent
                if isinstance(timeline_event, BrokerTimelineEvent):
                    # Add top-level broker fields for easier access in from_correlation_log_entry
                    if timeline_event.original_order_id:
                        extra_broker_fields["original_order_id_top_level"] = timeline_event.original_order_id
                    if timeline_event.broker_order_id_on_event:
                        extra_broker_fields["broker_order_id_on_event_top_level"] = timeline_event.broker_order_id_on_event
                    # Add event_type and correlation_id for proper reconstruction
                    extra_broker_fields["event_type"] = timeline_event.event_type
                    extra_broker_fields["correlation_id"] = timeline_event.correlation_id
                    extra_broker_fields["source_info"] = timeline_event.source_info
                    extra_broker_fields["component_processing_latency_ns"] = timeline_event.processing_latency_ns

            # Add common fields for all event types
            if not extra_broker_fields.get("event_type"):
                extra_broker_fields["event_type"] = timeline_event.event_type
            if not extra_broker_fields.get("correlation_id"):
                extra_broker_fields["correlation_id"] = timeline_event.correlation_id
            if not extra_broker_fields.get("source_info"):
                extra_broker_fields["source_info"] = timeline_event.source_info
            if not extra_broker_fields.get("component_processing_latency_ns"):
                extra_broker_fields["component_processing_latency_ns"] = timeline_event.processing_latency_ns

            self._correlation_logger.log_event(
                source_sense=timeline_event.source_sense,
                event_payload=payload_for_log,
                source_component_name=timeline_event.source_info.get("component", "UnknownRedisSource"),
                perf_timestamp_ns_override=timeline_event.perf_counter_timestamp,  # Use timestamp from event
                **extra_broker_fields  # Include additional fields for proper reconstruction
            )

            # If it's an OCR event, check bootstrap monitors
            if timeline_event.source_sense == "OCR" and self._bootstrap_ocr_monitors:
                # The payload_for_log here is effectively timeline_event.data
                # _check_bootstrap_ocr_monitors expects the 'data' part of an OCR event
                # (which contains 'snapshots')
                self._check_bootstrap_ocr_monitors(payload_for_log)

        except Exception as e:
            logger.error(f"PDS: Error logging event GSI {timeline_event.global_sequence_id} from data source: {e}", exc_info=True)

    def _datasource_consumption_thread_target(self, data_source: 'IDataSource', stream_name_for_log: str):
        """Target function for threads consuming from individual data sources."""
        logger.info(f"PDS: Starting consumption thread for data source: {stream_name_for_log}")
        try:
            stream_iterator: Optional[Iterator[TimelineEvent]] = None
            if hasattr(data_source, 'get_data_stream'):
                stream_iterator = data_source.get_data_stream()
            elif hasattr(data_source, 'get_price_stream'):
                stream_iterator = data_source.get_price_stream()  # type: ignore
            elif hasattr(data_source, 'get_response_stream'):
                stream_iterator = data_source.get_response_stream()  # type: ignore
            elif hasattr(data_source, 'get_visual_stream'):
                stream_iterator = data_source.get_visual_stream()  # type: ignore

            if stream_iterator:
                for timeline_event in stream_iterator:
                    if self._stop_all_ds_threads_event.is_set() or not self._capture_active:
                        break
                    if timeline_event:
                        self._log_event_from_datasource_thread(timeline_event)
            else:
                logger.error(f"PDS: Data source for {stream_name_for_log} does not have a known get stream method.")

        except Exception as e:
            logger.error(f"PDS: Unhandled exception in consumption thread for {stream_name_for_log}: {e}", exc_info=True)
        finally:
            logger.info(f"PDS: Consumption thread for data source {stream_name_for_log} finished.")

    def _check_bootstrap_ocr_monitors(self, ocr_event_data_payload: Dict[str, Any]):
        # ... (Logic as defined in E.1 Iteration 1, ensure it parses ocr_event_data_payload correctly) ...
        # This payload is now the 'data' part of the OCRTimelineEvent,
        # which itself was populated from the 'payload' of the Redis message.
        # ocr_event_data_payload should directly contain 'snapshots', 'frame_number'.
        with self._bootstrap_monitor_lock:
            if not self._bootstrap_ocr_monitors: return
            snapshots = ocr_event_data_payload.get('snapshots', {})
            frame_number = ocr_event_data_payload.get('frame_number', -1)
            for symbol_monitored_upper, detection_event in list(self._bootstrap_ocr_monitors.items()):
                # ... (rest of detection logic as before) ...
                if detection_event.is_set(): continue
                symbol_snapshot = snapshots.get(symbol_monitored_upper)
                if symbol_snapshot and isinstance(symbol_snapshot, dict):
                    ocr_shares_val = symbol_snapshot.get('total_shares', symbol_snapshot.get('shares')); ocr_strategy_val = symbol_snapshot.get('strategy_hint', '')
                    expected_shares_for_symbol = self._bootstrap_expected_shares.get(symbol_monitored_upper)
                    if ocr_shares_val is not None and expected_shares_for_symbol is not None:
                        try:
                            ocr_shares_str = str(ocr_shares_val).replace('+', '').strip(); ocr_shares = float(ocr_shares_str) if ocr_shares_str else 0.0
                            is_shares_match = abs(ocr_shares - expected_shares_for_symbol) < 0.1; is_strategy_match = "long" in ocr_strategy_val.lower()
                            if is_shares_match and is_strategy_match:
                                logger.info(f"PDS BOOTSTRAP for {symbol_monitored_upper} (Frame {frame_number}): Detected. Setting event."); detection_event.set()
                        except ValueError: logger.warning(f"PDS BOOTSTRAP {symbol_monitored_upper}: ValueError OCR shares '{ocr_shares_val}'.")
                        except Exception as e_check: logger.error(f"PDS BOOTSTRAP {symbol_monitored_upper}: Exception: {e_check}", exc_info=True)

    # register_bootstrap_ocr_monitor, unregister_bootstrap_ocr_monitor, _load_injection_plan remain.

    def register_bootstrap_ocr_monitor(self, symbol: str, expected_shares: float, detection_event: threading.Event):
        """Called by PrecisionBootstrap to monitor for OCR detection of a bootstrap trade."""
        with self._bootstrap_monitor_lock:
            logger.info(f"[{self._session_name}] Registering bootstrap OCR monitor for {symbol}, expected shares: {expected_shares}")
            self._bootstrap_ocr_monitors[symbol.upper()] = detection_event
            self._bootstrap_expected_shares[symbol.upper()] = expected_shares

    def unregister_bootstrap_ocr_monitor(self, symbol: str):
        """Called by PrecisionBootstrap when monitoring for a symbol is done or times out."""
        with self._bootstrap_monitor_lock:
            removed_event = self._bootstrap_ocr_monitors.pop(symbol.upper(), None)
            removed_shares = self._bootstrap_expected_shares.pop(symbol.upper(), None)
            if removed_event:
                logger.info(f"[{self._session_name}] Unregistered bootstrap OCR monitor for {symbol.upper()}.")

    def _load_injection_plan(self) -> Optional[List[InjectionStep]]:
        # ... (file existence checks as in Chunk 17) ...
        if not self.injection_plan_path or not os.path.exists(self.injection_plan_path):
             logger.error(f"Injection plan file not found or path not set: {self.injection_plan_path}")
             return None
        try:
            with open(self.injection_plan_path, 'r') as f:
                plan_json_data = json.load(f)
            if not isinstance(plan_json_data, list):
                logger.error(f"Injection plan {self.injection_plan_path} is not a list.")
                return None

            injection_plan: List[InjectionStep] = []
            for i, step_dict in enumerate(plan_json_data):
                try:
                    # Use InjectionStep.from_dict if defined for robust deserialization of nested ValidationPoints
                    if hasattr(InjectionStep, 'from_dict') and callable(InjectionStep.from_dict):
                         step_obj = InjectionStep.from_dict(step_dict)
                    else: # Fallback to direct instantiation (less robust for nested ValidationPoints)
                         vp_list_data = step_dict.pop('validation_points', [])
                         validation_points_objs = [ValidationPoint(**vp_data) for vp_data in vp_list_data]
                         step_obj = InjectionStep(validation_points=validation_points_objs, **step_dict)
                    injection_plan.append(step_obj)
                except Exception as e_step: # Catch more broadly
                    logger.error(f"Error processing injection step {i} from {self.injection_plan_path}: {e_step}. Data: {step_dict}", exc_info=True)

            logger.info(f"Loaded {len(injection_plan)} steps from injection plan: {self.injection_plan_path}")
            return injection_plan if injection_plan else None
        except Exception as e: # ... (error handling)
            logger.error(f"Failed to load/parse injection plan {self.injection_plan_path}: {e}", exc_info=True)
            return None

    def start_capture_session(self, session_name_prefix: Optional[str] = None) -> bool:
        if self._capture_active:
            logger.warning("PDS: Capture session already active."); return False
        self._session_name = f"{session_name_prefix or self.custom_session_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._session_path = os.path.join(self.DEFAULT_CAPTURE_BASE_DIR, self._session_name)
        logger.info(f"PDS: Attempting to start capture session '{self._session_name}'. Logging to: {self._session_path}")

        try:
            if hasattr(self.testrade, 'activate_capture_mode') and callable(self.testrade.activate_capture_mode):
                if not self.testrade.activate_capture_mode():  # type: ignore
                    logger.error("PDS: Failed to activate AppCore capture mode."); return False
            # else: PDS might operate without explicit ISAC.activate_capture_mode if ISAC is already configured.

            os.makedirs(self._session_path, exist_ok=True)
            reset_global_sequence_id_generator()

            self._correlation_logger = CorrelationLogger(
                session_data_path=self._session_path,
                internal_event_bus=self._internal_event_bus
            )
            logger.info(f"PDS: CorrelationLogger initialized for session '{self._session_name}'.")

            if self.millisecond_capture_orchestrator:
                self.millisecond_capture_orchestrator.correlation_logger = self._correlation_logger

            if hasattr(self.testrade, 'set_active_intellisense_correlation_logger'):  # Conceptual ISAC method
                 self.testrade.set_active_intellisense_correlation_logger(self._correlation_logger)  # type: ignore

            # --- Initialize and Start Redis DataSources and their consumer threads ---
            self._data_sources = []
            self._data_source_threads = []
            self._stop_all_ds_threads_event.clear()

            # For type hinting session_config from the dict
            from intellisense.config.session_config import TestSessionConfig as TestSessionConfigType

            # Filter out PDS-specific config keys that aren't part of TestSessionConfig
            pds_specific_keys = {'mode', 'session_name_prefix', 'injection_plan_path', 'target_symbols'}
            filtered_config = {k: v for k, v in self.session_config_dict.items() if k not in pds_specific_keys}

            try:
                typed_session_config = TestSessionConfigType(**filtered_config)
            except TypeError as e:
                logger.error(f"PDS: Error creating TestSessionConfig: {e}")
                logger.info(f"PDS: Available config keys: {list(filtered_config.keys())}")
                # Create minimal config for Redis streams
                typed_session_config = TestSessionConfigType(
                    redis_host=filtered_config.get('redis_host', 'localhost'),
                    redis_port=filtered_config.get('redis_port', 6379),
                    redis_db=filtered_config.get('redis_db', 0),
                    redis_password=filtered_config.get('redis_password'),
                    redis_stream_raw_ocr=filtered_config.get('redis_stream_raw_ocr', ''),
                    redis_stream_cleaned_ocr=filtered_config.get('redis_stream_cleaned_ocr', ''),
                    redis_stream_market_trades=filtered_config.get('redis_stream_market_trades', ''),
                    redis_stream_market_quotes=filtered_config.get('redis_stream_market_quotes', ''),
                    redis_stream_order_requests=filtered_config.get('redis_stream_order_requests', ''),
                    redis_stream_validated_orders=filtered_config.get('redis_stream_validated_orders', ''),
                    redis_stream_fills=filtered_config.get('redis_stream_fills', ''),
                    redis_stream_status=filtered_config.get('redis_stream_status', ''),
                    redis_stream_rejections=filtered_config.get('redis_stream_rejections', ''),
                    redis_stream_image_grabs=filtered_config.get('redis_stream_image_grabs', 'testrade:image-grabs'),
                    redis_stream_processed_roi_images=filtered_config.get('redis_stream_processed_roi_images', 'testrade:processed-roi-image-frames'),
                    redis_stream_config_changes=filtered_config.get('redis_stream_config_changes', 'testrade:config-changes'),
                    redis_stream_trading_state_changes=filtered_config.get('redis_stream_trading_state_changes', 'testrade:trading-state-changes'),
                    redis_stream_trade_lifecycle_events=filtered_config.get('redis_stream_trade_lifecycle_events', 'testrade:trade-lifecycle-events'),
                    redis_stream_system_alerts=filtered_config.get('redis_stream_system_alerts', 'testrade:system-alerts'),
                    redis_consumer_group_name=filtered_config.get('redis_consumer_group_name', 'intellisense-group'),
                    redis_consumer_name_prefix=filtered_config.get('redis_consumer_name_prefix', 'pds_consumer')
                )

            # Raw OCR Data Source
            if typed_session_config.redis_stream_raw_ocr:
                from intellisense.engines.datasources.redis_ocr_raw_datasource import RedisRawOCRDataSource
                raw_ocr_ds = RedisRawOCRDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(raw_ocr_ds)
                logger.info(f"PDS: Raw OCR data source configured for stream: {typed_session_config.redis_stream_raw_ocr}")

            # Cleaned OCR Data Source
            if typed_session_config.redis_stream_cleaned_ocr:
                from intellisense.engines.datasources.redis_ocr_cleaned_datasource import RedisCleanedOCRDataSource
                cleaned_ocr_ds = RedisCleanedOCRDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(cleaned_ocr_ds)
                logger.info(f"PDS: Cleaned OCR data source configured for stream: {typed_session_config.redis_stream_cleaned_ocr}")

            # Market Data Source
            if typed_session_config.redis_stream_market_trades or typed_session_config.redis_stream_market_quotes:
                from intellisense.engines.datasources.redis_market_data_source import RedisMarketDataDataSource
                market_data_ds = RedisMarketDataDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(market_data_ds)
                streams_configured = []
                if typed_session_config.redis_stream_market_trades:
                    streams_configured.append(f"trades:{typed_session_config.redis_stream_market_trades}")
                if typed_session_config.redis_stream_market_quotes:
                    streams_configured.append(f"quotes:{typed_session_config.redis_stream_market_quotes}")
                logger.info(f"PDS: Market data source configured for streams: {', '.join(streams_configured)}")

            # Broker Data Source - Complete stream configuration
            broker_streams_available = [
                typed_session_config.redis_stream_order_requests,
                typed_session_config.redis_stream_validated_orders,
                typed_session_config.redis_stream_fills,
                typed_session_config.redis_stream_status,
                typed_session_config.redis_stream_rejections
            ]
            if any(broker_streams_available):
                from intellisense.engines.datasources.redis_broker_datasource import RedisBrokerDataSource
                broker_ds = RedisBrokerDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(broker_ds)
                configured_streams = []
                if typed_session_config.redis_stream_order_requests:
                    configured_streams.append(f"requests:{typed_session_config.redis_stream_order_requests}")
                if typed_session_config.redis_stream_validated_orders:
                    configured_streams.append(f"validated:{typed_session_config.redis_stream_validated_orders}")
                if typed_session_config.redis_stream_fills:
                    configured_streams.append(f"fills:{typed_session_config.redis_stream_fills}")
                if typed_session_config.redis_stream_status:
                    configured_streams.append(f"status:{typed_session_config.redis_stream_status}")
                if typed_session_config.redis_stream_rejections:
                    configured_streams.append(f"rejections:{typed_session_config.redis_stream_rejections}")
                logger.info(f"PDS: Broker data source configured for streams: {', '.join(configured_streams)}")

            # Image Data Source - Visual Sense for Three Senses Replay
            if typed_session_config.redis_stream_image_grabs:
                from intellisense.engines.datasources.redis_image_datasource import RedisImageDataSource
                image_ds = RedisImageDataSource(typed_session_config, self._on_visual_sense_event)
                self._data_sources.append(image_ds)
                logger.info(f"👁️ PDS: Visual Sense (Image) data source configured for stream: {typed_session_config.redis_stream_image_grabs}")

            # Processed ROI Image Data Source (NEW)
            if typed_session_config.redis_stream_processed_roi_images:
                from intellisense.engines.datasources.redis_processed_image_datasource import RedisProcessedImageDataSource
                processed_image_ds = RedisProcessedImageDataSource(
                    redis_host=getattr(typed_session_config, 'redis_host', '127.0.0.1'),
                    redis_port=getattr(typed_session_config, 'redis_port', 6379),
                    redis_db=getattr(typed_session_config, 'redis_db', 0),
                    stream_name=typed_session_config.redis_stream_processed_roi_images,
                    consumer_group=getattr(typed_session_config, 'redis_consumer_group_name', 'intellisense_processed_image_group'),
                    consumer_name=f"{getattr(typed_session_config, 'redis_consumer_name_prefix', 'pds_consumer')}_procimg",
                    max_queue_size=1000
                )
                self._data_sources.append(processed_image_ds)
                logger.info(f"🖼️ PDS: Processed ROI Image data source configured for stream: {typed_session_config.redis_stream_processed_roi_images}")

            # Config Change Data Source
            if hasattr(typed_session_config, 'redis_stream_config_changes') and typed_session_config.redis_stream_config_changes:
                from intellisense.engines.datasources.redis_config_change_datasource import RedisConfigChangeDataSource
                config_change_ds = RedisConfigChangeDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(config_change_ds)
                logger.info(f"⚙️ PDS: Config Change data source configured for stream: {typed_session_config.redis_stream_config_changes}")

            # Trading State Change Data Source
            if hasattr(typed_session_config, 'redis_stream_trading_state_changes') and typed_session_config.redis_stream_trading_state_changes:
                from intellisense.engines.datasources.redis_trading_state_datasource import RedisTradingStateDataSource
                trading_state_ds = RedisTradingStateDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(trading_state_ds)
                logger.info(f"🔄 PDS: Trading State Change data source configured for stream: {typed_session_config.redis_stream_trading_state_changes}")

            # Trade Lifecycle Data Source
            if hasattr(typed_session_config, 'redis_stream_trade_lifecycle_events') and typed_session_config.redis_stream_trade_lifecycle_events:
                from intellisense.engines.datasources.redis_trade_lifecycle_datasource import RedisTradeLifecycleDataSource
                trade_lifecycle_ds = RedisTradeLifecycleDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(trade_lifecycle_ds)
                logger.info(f"📈 PDS: Trade Lifecycle data source configured for stream: {typed_session_config.redis_stream_trade_lifecycle_events}")

            # System Alert Data Source
            if hasattr(typed_session_config, 'redis_stream_system_alerts') and typed_session_config.redis_stream_system_alerts:
                from intellisense.engines.datasources.redis_system_alert_datasource import RedisSystemAlertDataSource
                system_alert_ds = RedisSystemAlertDataSource(typed_session_config, self._get_next_gsi_for_datasource)
                self._data_sources.append(system_alert_ds)
                logger.info(f"🚨 PDS: System Alert data source configured for stream: {typed_session_config.redis_stream_system_alerts}")


            # Initialize and start all configured Redis data sources
            logger.info(f"PDS: Starting {len(self._data_sources)} Redis data sources...")
            for ds in self._data_sources:
                ds_name = ds.__class__.__name__
                logger.info(f"PDS: Initializing data source: {ds_name}")

                # Load timeline data (prepare for consumption)
                if hasattr(ds, 'load_timeline_data'):
                    if ds.load_timeline_data():
                        logger.debug(f"PDS: {ds_name} timeline data loaded successfully")
                    else:
                        logger.warning(f"PDS: {ds_name} timeline data loading returned False")

                # Start the data source (begin Redis stream consumption)
                if hasattr(ds, 'start'):
                    ds.start()
                    logger.debug(f"PDS: {ds_name} Redis stream consumption started")

                # Create dedicated thread for this data source's TimelineEvent processing
                thread = threading.Thread(
                    target=self._datasource_consumption_thread_target,
                    args=(ds, ds_name),
                    daemon=True,
                    name=f"PDS-Logger-{ds_name}"
                )
                self._data_source_threads.append(thread)
                thread.start()
                logger.debug(f"PDS: {ds_name} logging thread started: {thread.name}")

            logger.info(f"PDS: All {len(self._data_source_threads)} Redis data source logging threads started successfully.")
            logger.info(f"PDS: Redis-based data ingestion fully operational for session '{self._session_name}'")
            # --- End Complete Redis DataSource Setup ---

            if self.capture_mode == "CONTROLLED_INJECTION":
                # ... (FIM and PrecisionBootstrap setup as before) ...
                if self.feedback_isolator and hasattr(self.testrade, 'set_active_feedback_isolator'):
                    self.testrade.set_active_feedback_isolator(self.feedback_isolator) # type: ignore
                if self.precision_bootstrap:
                    self.precision_bootstrap.start_bootstrap_mode()
                    if self.target_symbols:
                        # PrecisionBootstrap now needs to use Redis for price feed checks
                        # or have its PriceRepository be a Redis consumer. This needs careful thought.
                        # For now, assume bootstrap_symbol_feeds might use a different mechanism
                        # or is adapted to know that price_repository now sources from Redis.
                        self.precision_bootstrap.bootstrap_symbol_feeds(self.target_symbols)

            self._capture_active = True
            logger.info(f"PDS: Capture session '{self._session_name}' started. Consuming from Redis. Logging to: {self._session_path}")

            if self.capture_mode == "CONTROLLED_INJECTION" and self.millisecond_capture_orchestrator and self.injection_plan_path:
                # ... (execute plan as before) ...
                injection_plan = self._load_injection_plan()
                if injection_plan:
                    logger.info(f"PDS [{self._session_name}] Starting MOC injection plan...")
                    threading.Thread(target=self.millisecond_capture_orchestrator.execute_plan, args=(injection_plan,), daemon=True).start()
                    # MOC plan execution might be long, run in thread if PDS start is expected to be non-blocking.
                    # Or, if PDS start IS the main loop for capture, then call it directly.
                    # For now, let's assume PDS start_capture_session can return after starting things.
                    # logger.info(f"PDS [{self._session_name}] MOC injection plan execution finished.") # This log might be premature if threaded
            return True
        except Exception as e:
            logger.error(f"PDS: Failed to start capture session '{self.custom_session_prefix}': {e}", exc_info=True)
            self.stop_capture_session()  # Attempt full cleanup
            return False

    def stop_capture_session(self) -> None:
        session_name_at_stop = self._session_name if self._session_name else "UnnamedSession"
        logger.info(f"PDS: Stopping data capture session '{session_name_at_stop}'...")

        if not self._capture_active and not self._data_sources and not self._data_source_threads:
            logger.info("PDS: No active capture or components to stop.")
            return

        try:
            self._capture_active = False  # Signal to threads and other logic
            self._stop_all_ds_threads_event.set()  # Signal consumption threads to stop

            # Stop all Redis DataSources (this stops their internal RedisStreamConsumerBase threads)
            logger.info(f"PDS: Stopping {len(self._data_sources)} Redis data sources...")
            for ds in self._data_sources:
                ds_name = ds.__class__.__name__
                if hasattr(ds, 'stop'):
                    logger.debug(f"PDS: Stopping Redis data source: {ds_name}")
                    try:
                        ds.stop()
                        logger.debug(f"PDS: {ds_name} stopped successfully")
                    except Exception as e:
                        logger.error(f"PDS: Error stopping {ds_name}: {e}", exc_info=True)
                else:
                    logger.warning(f"PDS: {ds_name} does not have stop() method")

            # Wait for all PDS-managed logging threads to finish
            logger.info(f"PDS: Waiting for {len(self._data_source_threads)} logging threads to finish...")
            for thread in self._data_source_threads:
                logger.debug(f"PDS: Joining logging thread: {thread.name}")
                try:
                    thread.join(timeout=2.0)  # 2.0 second timeout for each thread join
                    if thread.is_alive():
                        logger.warning(f"PDS: Logging thread {thread.name} did not join cleanly within timeout")
                    else:
                        logger.debug(f"PDS: Logging thread {thread.name} joined successfully")
                except Exception as e:
                    logger.error(f"PDS: Error joining thread {thread.name}: {e}", exc_info=True)

            # Clean up data source collections
            self._data_sources.clear()
            self._data_source_threads.clear()
            logger.info(f"PDS: All Redis data sources and logging threads stopped for session '{session_name_at_stop}'")

            # ... (cleanup MOC, PrecisionBootstrap, FIM as before) ...
            if self.millisecond_capture_orchestrator and hasattr(self.millisecond_capture_orchestrator, 'cleanup'):
                self.millisecond_capture_orchestrator.cleanup()
            if self.precision_bootstrap:
                self.precision_bootstrap.cleanup()
            if self.feedback_isolator:
                self.feedback_isolator.reset_all_mocking_and_states()
            if hasattr(self.testrade, 'set_active_feedback_isolator'):
                self.testrade.set_active_feedback_isolator(None)  # type: ignore

            if self._correlation_logger:
                self._correlation_logger.close()
                logger.info(f"PDS: CorrelationLogger for session '{session_name_at_stop}' closed.")

            if hasattr(self.testrade, 'deactivate_capture_mode') and callable(self.testrade.deactivate_capture_mode):
                self.testrade.deactivate_capture_mode()  # type: ignore

            if hasattr(self.testrade, 'set_active_intellisense_correlation_logger'):
                 self.testrade.set_active_intellisense_correlation_logger(None)  # type: ignore

            logger.info(f"PDS: Data capture session '{session_name_at_stop}' stopped successfully.")
        except Exception as e:
            logger.error(f"PDS: Error stopping capture session '{session_name_at_stop}': {e}", exc_info=True)
        finally:
            self._capture_active = False; self._session_name = None; self._session_path = None
            self._correlation_logger = None; self._data_sources.clear(); self._data_source_threads.clear()
            self._stop_all_ds_threads_event.clear()  # Reset for next session

    # register_bootstrap_ocr_monitor, unregister_bootstrap_ocr_monitor, _load_injection_plan remain as is.
    # OLD Listener methods (on_trade_data, on_quote_data, on_broker_response, on_ocr_frame_processed)
    # are REMOVED as PDS no longer directly implements these interfaces.
    # The functionality of on_ocr_frame_processed related to bootstrap is now in _check_bootstrap_ocr_monitors,
    # called from _log_event_from_datasource_thread.

    def register_bootstrap_ocr_monitor(self, symbol: str, expected_shares: float, detection_event: threading.Event):
        """Called by PrecisionBootstrap to monitor for OCR detection of a bootstrap trade."""
        with self._bootstrap_monitor_lock:
            logger.info(f"[{self._session_name}] Registering bootstrap OCR monitor for {symbol}, expected shares: {expected_shares}")
            self._bootstrap_ocr_monitors[symbol.upper()] = detection_event
            self._bootstrap_expected_shares[symbol.upper()] = expected_shares

    def unregister_bootstrap_ocr_monitor(self, symbol: str):
        """Called by PrecisionBootstrap when monitoring for a symbol is done or times out."""
        with self._bootstrap_monitor_lock:
            removed_event = self._bootstrap_ocr_monitors.pop(symbol.upper(), None)
            removed_shares = self._bootstrap_expected_shares.pop(symbol.upper(), None)
            if removed_event:
                logger.info(f"[{self._session_name}] Unregistered bootstrap OCR monitor for {symbol.upper()}.")

    def _on_visual_sense_event(self, visual_event: Dict[str, Any]):
        """
        Handle visual sense events from image data source.

        This processes the "visual sense" - what the trader actually SAW on screen.
        Essential for three senses replay: Visual + Price + Broker feedback.

        Args:
            visual_event: Visual event data containing image and metadata
        """
        try:
            # Create visual timeline event for correlation logging
            from intellisense.core.types import VisualTimelineEvent, VisualTimelineEventData
            import time

            # Create visual data payload
            visual_data = VisualTimelineEventData(
                image_type=visual_event.get("image_type", "unknown"),
                image_data_base64=visual_event.get("image_data", {}).get("base64_data"),
                image_size_bytes=visual_event.get("image_data", {}).get("size_bytes", 0),
                roi_coordinates=visual_event.get("roi_coordinates"),
                capture_timestamp=visual_event.get("capture_timestamp"),
                frame_number=visual_event.get("frame_number"),
                processing_info=visual_event.get("processing_info", {}),
                correlation_id_propagated=visual_event.get("correlation_id")
            )

            # Create visual timeline event
            visual_timeline_event = VisualTimelineEvent(
                global_sequence_id=self._get_next_gsi_for_datasource(),
                epoch_timestamp_s=time.time(),
                perf_counter_timestamp=visual_event.get("timestamp_ns", 0),
                source_sense="VISUAL",
                correlation_id=visual_event.get("correlation_id"),
                event_id=visual_event.get("event_id"),
                source_info={"component": visual_event.get("source_component", "ImageDataSource")},
                data=visual_data
            )

            # Log to correlation logger for replay analysis
            if self._correlation_logger:
                self._correlation_logger.log_visual_event(visual_timeline_event)

            # Forward to internal event bus for real-time processing
            if self._internal_event_bus:
                self._internal_event_bus.publish_visual_event(visual_timeline_event)

            logger.debug(f"👁️ Visual Sense: Processed image event {visual_event.get('event_id')} - {visual_event.get('image_type')} ({visual_event.get('image_data', {}).get('size_bytes', 0)} bytes)")

        except Exception as e:
            logger.error(f"👁️ Visual Sense: Error processing visual event: {e}", exc_info=True)

    def _load_injection_plan(self) -> Optional[List[InjectionStep]]:
        # ... (file existence checks as in Chunk 17) ...
        if not self.injection_plan_path or not os.path.exists(self.injection_plan_path):
             logger.error(f"Injection plan file not found or path not set: {self.injection_plan_path}")
             return None
        try:
            with open(self.injection_plan_path, 'r') as f:
                plan_json_data = json.load(f)
            if not isinstance(plan_json_data, list):
                logger.error(f"Injection plan {self.injection_plan_path} is not a list.")
                return None

            injection_plan: List[InjectionStep] = []
            for i, step_dict in enumerate(plan_json_data):
                try:
                    # Use InjectionStep.from_dict if defined for robust deserialization of nested ValidationPoints
                    if hasattr(InjectionStep, 'from_dict') and callable(InjectionStep.from_dict):
                         step_obj = InjectionStep.from_dict(step_dict)
                    else: # Fallback to direct instantiation (less robust for nested ValidationPoints)
                         vp_list_data = step_dict.pop('validation_points', [])
                         validation_points_objs = [ValidationPoint(**vp_data) for vp_data in vp_list_data]
                         step_obj = InjectionStep(validation_points=validation_points_objs, **step_dict)
                    injection_plan.append(step_obj)
                except Exception as e_step: # Catch more broadly
                    logger.error(f"Error processing injection step {i} from {self.injection_plan_path}: {e_step}. Data: {step_dict}", exc_info=True)

            logger.info(f"Loaded {len(injection_plan)} steps from injection plan: {self.injection_plan_path}")
            return injection_plan if injection_plan else None
        except Exception as e: # ... (error handling)
            logger.error(f"Failed to load/parse injection plan {self.injection_plan_path}: {e}", exc_info=True)
            return None
