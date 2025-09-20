import logging
import time
import threading
import copy
import dataclasses # For dataclasses.replace and is_dataclass
from typing import List, Dict, Any, Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore
    from intellisense.capture.interfaces import IFeedbackIsolationManager
    from utils.global_config import GlobalConfig # Assuming this is config type
    from core.interfaces import IEventBus
    from interfaces.ocr.services import IOCRDataConditioner
    # Actual data types from your system
    from modules.ocr.data_types import OCRParsedData, CleanedOCRSnapshotEventData, OCRSnapshot
    from intellisense.core.types import Position # For Position.from_dict
    # Import actual base class if it exists
    # from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService as ActualBaseOCRService
else:
    # Runtime placeholders for missing types
    class OCRParsedData: pass
    class CleanedOCRSnapshotEventData: pass
    class IntelliSenseApplicationCore: pass # Minimal placeholder for type hint
    class GlobalConfig: pass
    class IEventBus: pass
    class IOCRDataConditioner: pass
    class IFeedbackIsolationManager: pass
    class Position: pass

# Placeholder Base Class (as used in previous chunks for generation)
class BaseOCRDataConditioningService:
    def __init__(self, *args, config: Any = None, event_bus: Any = None, conditioner: Any = None, app_core_ref_for_intellisense: Any = None, **kwargs):
        self.config=config; self.event_bus=event_bus; self.conditioner=conditioner
        self._app_core_ref = app_core_ref_for_intellisense
        logger.info("Base OCRDataConditioningService placeholder init.")
    def _process_item(self, item: 'OCRParsedData') -> Optional['CleanedOCRSnapshotEventData']:
        logger.debug(f"BaseOCR _process_item for frame {getattr(item,'frame_number','N/A')}")
        # Simulate minimal successful processing
        # This needs to be the actual CleanedOCRSnapshotEventData type used by the system
        # from intellisense.core.types import CleanedOCRSnapshotEventData # Not good to import here
        # For placeholder purposes:
        class PlaceholderCleanedSnapshot:
            def __init__(self, **kwargs): self.__dict__.update(kwargs)
            def to_dict(self): return self.__dict__

        if getattr(item,'frame_number',0) % 10 == 0: return None
        return PlaceholderCleanedSnapshot(
            frame_timestamp=getattr(item,'frame_timestamp',time.time()),
            snapshots={'SYMBOL': {'strategy_hint':'Long', 'cost_basis':100.0}},
            original_ocr_event_id=None, raw_ocr_confidence=0.9,
            frame_number=getattr(item, 'frame_number', -1), # Make sure frame_number is part of returned obj
            expected_position_dict={'SYMBOL': {'qty': getattr(item,'frame_number',0), 'avg_price': 100.0}}, # For payload
            validation_status='VALID' # For payload
        )


from intellisense.capture.interfaces import IOCRDataListener
from .metrics_collector import ComponentCaptureMetrics

logger = logging.getLogger(__name__)

class EnhancedOCRDataConditioningService(BaseOCRDataConditioningService):
    def __init__(self,
                 app_core_ref_for_intellisense: 'IntelliSenseApplicationCore',
                 original_config: 'GlobalConfig', # From original service
                 original_event_bus: 'IEventBus', # From original service
                 original_conditioner: Optional['IOCRDataConditioner'], # From original service
                 *args, **kwargs): # To catch any other args for super()

        # Call super with arguments the base class expects
        # This requires knowing BaseOCRDataConditioningService's __init__ signature
        super().__init__(config=original_config,
                         event_bus=original_event_bus,
                         conditioner=original_conditioner,
                         # Pass app_core_ref here IF base class accepts it and stores it as self._app_core_ref
                         # Otherwise, Enhanced class stores it directly.
                         *args, **kwargs)

        self._app_core_ref: 'IntelliSenseApplicationCore' = app_core_ref_for_intellisense # Store with string hint
        self._ocr_data_listeners: List[IOCRDataListener] = []
        self._listeners_lock = threading.RLock()
        self._capture_metrics = ComponentCaptureMetrics()
        self._injected_frame_counter = -1
        logger.info(f"EnhancedOCRDataConditioningService constructed via factory. AppCoreRef: {type(self._app_core_ref).__name__}")

    # --- Temporary Listener Methods REMOVED ---
    # def add_temporary_raw_output_listener(...): REMOVED
    # def remove_temporary_raw_output_listener(...): REMOVED
    # def clear_all_temporary_listeners(...): REMOVED
    # --- End REMOVED ---

    # ... (add_ocr_data_listener, remove_ocr_data_listener, _notify_ocr_listeners, get_capture_metrics,
    #      _generate_next_injected_frame_id, inject_simulated_raw_ocr_input as defined in Chunk 17/9) ...
    def add_ocr_data_listener(self, listener: IOCRDataListener) -> None: # Keep standard listeners
        with self._listeners_lock:
            if listener not in self._ocr_data_listeners: self._ocr_data_listeners.append(listener)
    def remove_ocr_data_listener(self, listener: IOCRDataListener) -> None:
        with self._listeners_lock:
            try: self._ocr_data_listeners.remove(listener)
            except ValueError: pass
    def _notify_ocr_listeners(self, source_component_name: str, frame_number: int, ocr_payload: Dict[str, Any], processing_latency_ns: int) -> None:
        if not self._ocr_data_listeners: return
        notification_start_ns = time.perf_counter_ns()
        with self._listeners_lock: listeners_snapshot = list(self._ocr_data_listeners)
        for listener in listeners_snapshot:
            try: listener.on_ocr_frame_processed(source_component_name, frame_number, ocr_payload, processing_latency_ns)
            except Exception as e: self._capture_metrics.record_notification_attempt(0, success=False); logger.error(f"Error notifying OCR listener: {e}", exc_info=True)
            else: self._capture_metrics.record_notification_attempt(time.perf_counter_ns() - notification_start_ns, success=True)
    def get_capture_metrics(self): metrics = self._capture_metrics.get_metrics(); metrics['listener_count'] = len(self._ocr_data_listeners); return metrics

    def _process_item(self, item: 'OCRParsedData') -> Optional['CleanedOCRSnapshotEventData']:
        """
        Enhanced to:
        1. Call super for actual conditioning.
        2. Extract correlation_id/tracking_dict from item if it's an injected stimulus.
        3. Include these IDs in the ocr_payload_for_logging sent to listeners.
        4. Apply feedback isolation mock if active to the result for the live pipeline.
        5. Return (potentially) mocked result to live pipeline.
        """
        conditioning_start_ns = time.perf_counter_ns()
        actual_conditioned_result: Optional['CleanedOCRSnapshotEventData'] = None
        processing_error: Optional[Exception] = None

        # Attempt to extract tracing identifiers from the input 'item'
        # MOC's inject_simulated_raw_ocr_input puts them in item.raw_ocr_data
        stimulus_correlation_id: Optional[str] = None
        stimulus_tracking_dict: Optional[Dict[str, Any]] = None

        # The 'item' is OCRParsedData. inject_simulated_raw_ocr_input creates a placeholder
        # that assigns the raw_ocr_data_payload (containing tracking_dict and correlation_id)
        # to an attribute like item.raw_ocr_data or directly to item.snapshots if it's structured that way.
        # Let's assume 'item' has an attribute 'raw_ocr_data' which was the payload from MOC.
        raw_ocr_data_from_stimulus = getattr(item, 'raw_ocr_data', None)
        if isinstance(raw_ocr_data_from_stimulus, dict):
            stimulus_correlation_id = raw_ocr_data_from_stimulus.get('correlation_id')
            stimulus_tracking_dict = raw_ocr_data_from_stimulus.get('tracking_dict')
            if stimulus_tracking_dict:
                 logger.debug(f"EnhancedOCR: Found tracking_dict in incoming item: {stimulus_tracking_dict.get('master_tracking_id')}")
            elif stimulus_correlation_id:
                 logger.debug(f"EnhancedOCR: Found correlation_id in incoming item: {stimulus_correlation_id}")

        try:
            actual_conditioned_result = super()._process_item(item)
        except Exception as e:
            processing_error = e
            logger.error(f"Error in super()._process_item for frame {getattr(item,'frame_number','N/A')}: {e}", exc_info=True)
        finally:
            conditioning_latency_ns = time.perf_counter_ns() - conditioning_start_ns
            self._capture_metrics.record_item_processed(
                conditioning_latency_ns,
                success=(processing_error is None and actual_conditioned_result is not None)
            )

            ocr_payload_for_logging: Dict[str, Any]
            frame_num_from_item = getattr(item, 'frame_number', -1)

            if processing_error:
                ocr_payload_for_logging = {
                    'frame_number': frame_num_from_item,
                    'conditioning_error': str(processing_error),
                    'raw_ocr_input_summary': str(raw_ocr_data_from_stimulus)[:200], # Log summary of input
                    'validation_status': 'ERROR_IN_SUPER_PROCESS_ITEM',
                    'conditioning_timestamp': time.time(),
                }
                # Add tracing info to error payload too
                if stimulus_correlation_id:
                    ocr_payload_for_logging['correlation_id'] = stimulus_correlation_id
                if stimulus_tracking_dict:
                    ocr_payload_for_logging['tracking_dict'] = stimulus_tracking_dict
            elif actual_conditioned_result is not None:
                # This payload must contain all data needed by:
                # 1. OCRTimelineEvent.from_correlation_log_entry to reconstruct the event.
                # 2. ProductionDataCaptureSession.on_ocr_frame_processed to verify bootstrap trades.

                # Extract symbol-specific snapshots if actual_conditioned_result has them
                # The structure of actual_conditioned_result.snapshots is Dict[str, Dict[str, Any]]
                snapshots_for_payload = {}
                if hasattr(actual_conditioned_result, 'snapshots') and isinstance(actual_conditioned_result.snapshots, dict):
                    for sym, snap_data_dict in actual_conditioned_result.snapshots.items():
                        # Ensure snap_data_dict is a dict and contains expected fields for bootstrap check
                        if isinstance(snap_data_dict, dict):
                            snapshots_for_payload[sym] = {
                                'symbol': sym, # Ensure symbol is in the snapshot data itself
                                'strategy_hint': snap_data_dict.get('strategy_hint'),
                                'cost_basis': snap_data_dict.get('cost_basis'),
                                'pnl_per_share': snap_data_dict.get('pnl_per_share'),
                                'realized_pnl': snap_data_dict.get('realized_pnl'),
                                'total_shares': snap_data_dict.get('total_shares'), # Key for bootstrap check
                                # Add other fields from PythonOCRDataConditioner's output if needed
                            }

                # This 'ocr_data' dictionary becomes the OCRTimelineEvent.data attribute
                # and is the primary payload for correlation logging.
                ocr_event_data_content = {
                    'ocr_confidence': getattr(actual_conditioned_result, 'raw_ocr_confidence', getattr(actual_conditioned_result, 'ocr_confidence', 0.0)),
                    'snapshots': snapshots_for_payload,
                    'raw_ocr_input_text_preview': getattr(item, 'full_raw_text', '')[:100] # From OCRParsedData
                }

                # --- NEW: Add correlation_id and tracking_dict to ocr_event_data_content ---
                if stimulus_correlation_id:
                    ocr_event_data_content['correlation_id'] = stimulus_correlation_id
                if stimulus_tracking_dict:
                    ocr_event_data_content['tracking_dict'] = stimulus_tracking_dict.copy() # Add a copy
                # --- END NEW ---

                ocr_payload_for_logging = {
                    'frame_number': getattr(actual_conditioned_result, 'frame_number', frame_num_from_item),
                    'ocr_data': ocr_event_data_content, # This structure becomes OCRTimelineEvent.data
                    'validation_status': getattr(actual_conditioned_result, 'validation_status', 'VALID'),
                    'original_ocr_event_id': getattr(actual_conditioned_result, 'original_ocr_event_id', None),
                    'conditioning_timestamp': time.time(),
                }
            else:
                ocr_payload_for_logging = {
                    'frame_number': frame_num_from_item,
                    'conditioning_failed': True,
                    'raw_ocr_input_summary': str(raw_ocr_data_from_stimulus)[:200],
                    'validation_status': 'CONDITIONING_RETURNED_NONE',
                    'conditioning_timestamp': time.time(),
                }
                if stimulus_correlation_id:
                    ocr_payload_for_logging['correlation_id'] = stimulus_correlation_id
                if stimulus_tracking_dict:
                    ocr_payload_for_logging['tracking_dict'] = stimulus_tracking_dict

            # Notify standard listeners (e.g., ProductionDataCaptureSession) with ACTUAL data
            self._notify_ocr_listeners(
                source_component_name='EnhancedOCRDataConditioningService',
                frame_number=frame_num_from_item, # Use frame_number from item
                ocr_payload=ocr_payload_for_logging,
                processing_latency_ns=conditioning_latency_ns # Pass component's own processing time
            )

        if processing_error:
            raise processing_error

        output_for_live_pipeline = actual_conditioned_result
        # ... (FeedbackIsolationManager logic as before - this should operate on actual_conditioned_result
        #      and its output_for_live_pipeline would then be what's returned to TESTRADE's live flow) ...
        # --- Feedback Isolation Step: Determine output for the live TESTRADE pipeline ---
        feedback_isolator: Optional['IFeedbackIsolationManager'] = None
        if self._app_core_ref and hasattr(self._app_core_ref, 'get_active_feedback_isolator'):
            feedback_isolator = self._app_core_ref.get_active_feedback_isolator()

        if feedback_isolator and actual_conditioned_result is not None:
            try:
                if dataclasses.is_dataclass(actual_conditioned_result):
                    current_snapshots = getattr(actual_conditioned_result, 'snapshots', {})
                    if isinstance(current_snapshots, dict):
                        modified_snapshots = copy.deepcopy(current_snapshots)
                        any_mock_applied = False
                        for symbol, single_symbol_snapshot_data in modified_snapshots.items():
                            if feedback_isolator.is_symbol_ocr_mocked(symbol):
                                mocked_symbol_data = feedback_isolator.get_mocked_ocr_snapshot_data(symbol, single_symbol_snapshot_data)
                                modified_snapshots[symbol] = mocked_symbol_data
                                any_mock_applied = True
                        if any_mock_applied:
                            output_for_live_pipeline = dataclasses.replace(actual_conditioned_result, snapshots=modified_snapshots)
                            logger.info(f"FeedbackIsolation applied to OCR output for frame {getattr(actual_conditioned_result, 'frame_number', frame_num_from_item)} for live pipeline.")
                    else: logger.warning("Snapshots attribute of actual_conditioned_result not a dict. OCR mock skipped.")
                else: logger.warning("Cannot apply OCR mock: actual_conditioned_result is not a dataclass.")
            except Exception as e_mock_apply:
                logger.error(f"Error applying OCR mock: {e_mock_apply}. Returning actual.", exc_info=True)
                output_for_live_pipeline = actual_conditioned_result

        return output_for_live_pipeline

    # inject_simulated_raw_ocr_input and _generate_next_injected_frame_id as per Chunk 17
    def _generate_next_injected_frame_id(self) -> int: self._injected_frame_counter -=1; return self._injected_frame_counter
    def inject_simulated_raw_ocr_input(self, raw_ocr_data_payload: Dict[str, Any], frame_number_override: Optional[int]=None, frame_timestamp_override: Optional[float]=None) -> None:
        if not (self._app_core_ref and hasattr(self._app_core_ref, 'intellisense_test_mode_active') and self._app_core_ref.intellisense_test_mode_active):
            logger.error("OCR injection blocked - test mode inactive"); return
        frame_ts = frame_timestamp_override or time.time(); frame_num = frame_number_override or self._generate_next_injected_frame_id()

        # Create placeholder OCRParsedData for injection with correlation tracking
        class OCRParsedDataPlaceholder:
            def __init__(self, frame_number, frame_timestamp, full_raw_text, overall_confidence, snapshots, raw_ocr_data):
                self.frame_number = frame_number
                self.frame_timestamp = frame_timestamp
                self.full_raw_text = full_raw_text
                self.overall_confidence = overall_confidence
                self.snapshots = snapshots
                # CRITICAL: Store the complete raw_ocr_data_payload here so _process_item can extract correlation data
                self.raw_ocr_data = raw_ocr_data

        item = OCRParsedDataPlaceholder(
            frame_number=frame_num,
            frame_timestamp=frame_ts,
            full_raw_text=raw_ocr_data_payload.get('full_raw_text',''), # Ensure these keys align
            overall_confidence=raw_ocr_data_payload.get('confidence',0.0), # with OCRParsedData
            snapshots=raw_ocr_data_payload.get('snapshots_raw_data',{}), # and its snapshots field
            raw_ocr_data=raw_ocr_data_payload  # Pass the complete payload for correlation extraction
        )

        logger.info(f"Injecting simulated OCR input frame {item.frame_number}")
        if raw_ocr_data_payload.get('correlation_id'):
            logger.debug(f"OCR injection includes correlation_id: {raw_ocr_data_payload.get('correlation_id')}")
        if raw_ocr_data_payload.get('tracking_dict'):
            logger.debug(f"OCR injection includes tracking_dict: {raw_ocr_data_payload.get('tracking_dict', {}).get('master_tracking_id', 'N/A')}")

        self._process_item(item)
