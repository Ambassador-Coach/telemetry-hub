import logging
import time
from typing import List, Dict, Any, Optional, Tuple, TYPE_CHECKING # Added TYPE_CHECKING

from intellisense.core.interfaces import IOCRIntelligenceEngine, IOCRDataSource
from intellisense.core.types import OCRTimelineEvent, Position
from intellisense.config.session_config import OCRTestConfig, TestSessionConfig
from intellisense.factories.interfaces import IDataSourceFactory # Keep IDataSourceFactory
from intellisense.analysis.ocr_results import OCRAnalysisResult, OCRValidationResult

if TYPE_CHECKING:
    # These would be your actual application's service classes.
    # If these modules are complex and could potentially import this engine for type hints (though unlikely for these):
    from modules.trade_management.snapshot_interpreter_service import SnapshotInterpreterService
    from modules.trade_management.position_manager import PositionManager
else:
    # Runtime placeholders if actual modules aren't available or to simplify dependencies for now
    class SnapshotInterpreterService:
        def interpret_snapshot(self, ocr_snapshot_data: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
            # Actual method signature from LA: interpret_snapshot(ocr_snapshot, context=position_context)
            # ocr_snapshot_data is the prepared data for interpreter.
            # context is position_context from real PositionManager.
            # Returns Dict[symbol, {'qty': float, 'avg_price': float, ...}]
            logger.debug(f"Real SnapshotInterpreter (Placeholder): Processing snapshot for symbol {ocr_snapshot_data.get('symbol','UNKNOWN')}")
            # Simulate interpretation based on some logic, e.g., confidence
            if ocr_snapshot_data.get('ocr_confidence', 0.0) > 0.5:
                sym = ocr_snapshot_data.get('symbol', 'AAPL')
                epd = ocr_snapshot_data.get('expected_position_dict', {}).get(sym, {})
                return {sym: {'qty': float(epd.get('shares',0)) * 0.95, 'avg_price': float(epd.get('avg_price',0)) * 1.005}}
            return None
        def get_current_positions(self) -> Dict[str, Any]:
            return {} # Example method from LA spec
        __version__ = "1.0_placeholder_runtime"

    class PositionManager:
        def get_position_data(self) -> Dict[str, Any]: # Example method from LA spec for context
            logger.debug("Real PositionManager (Placeholder): get_position_data called.")
            return {"AAPL": {"shares": 100, "avg_price": 150.0, "open": True, "strategy": "long"}}
        def get_symbol_positions(self) -> Dict[str, Any]:
            return {} # Example method
        def get_account_summary(self) -> Dict[str, Any]:
            return {} # Example method
        __version__ = "1.0_placeholder_runtime"

logger = logging.getLogger(__name__)

class OCRIntelligenceEngine(IOCRIntelligenceEngine):
    def __init__(self): # No args here, passed in initialize
        self.test_config: Optional[OCRTestConfig] = None
        self.data_source_factory: Optional[IDataSourceFactory] = None # From initialize
        self.data_source: Optional[IOCRDataSource] = None # From setup_for_session

        # Type hints for real service instances - use string hints to avoid runtime import
        self.snapshot_interpreter: Optional['SnapshotInterpreterService'] = None
        self.position_manager: Optional['PositionManager'] = None

        self.analysis_results: List[OCRAnalysisResult] = []
        self._engine_active = False
        self._current_session_id: Optional[str] = None
        self._previous_ocr_interpreted_states_for_engine: Dict[str, Dict[str, Any]] = {}

        logger.info("OCRIntelligenceEngine instance created.")

    def initialize(self,
                   config: OCRTestConfig,
                   data_source_factory: IDataSourceFactory,
                   # Real services passed from IntelliSenseMasterController - use string hints
                   snapshot_interpreter_instance: 'SnapshotInterpreterService',
                   position_manager_instance: 'PositionManager'
                  ) -> bool:
        self.test_config = config
        self.data_source_factory = data_source_factory
        self.snapshot_interpreter = snapshot_interpreter_instance
        self.position_manager = position_manager_instance

        if not all([self.snapshot_interpreter, self.position_manager]):
            logger.error("OCRIntelligenceEngine: Missing critical real service instances for initialization.")
            return False

        if not self._validate_service_interfaces():
            return False

        self.analysis_results = []
        logger.info("OCR Intelligence Engine initialized with real services.")
        return True

    def _validate_service_interfaces(self) -> bool:
        """Validate that real services have expected interfaces (methods)."""
        if not self.snapshot_interpreter or not self.position_manager: return False # Should be caught by init

        # Check SnapshotInterpreterService interface (as per LA spec)
        # LA spec used interpret_snapshot and get_current_positions.
        # My previous _create_input_for_snapshot_interpreter assumed interpret_single_snapshot.
        # Aligning with LA spec's interpret_snapshot(ocr_snapshot, context=position_context)
        required_si_methods = ['interpret_snapshot'] # Add more if needed
        for method in required_si_methods:
            if not hasattr(self.snapshot_interpreter, method) or not callable(getattr(self.snapshot_interpreter, method)):
                logger.error(f"Real SnapshotInterpreterService missing required method: {method}")
                return False

        # Check PositionManager interface (as per LA spec)
        required_pm_methods = ['get_position_data', 'get_symbol_positions'] # LA example context methods
        if hasattr(self.position_manager, 'get_account_summary'): # Optional method
             required_pm_methods.append('get_account_summary')
        for method in required_pm_methods:
            if not hasattr(self.position_manager, method) or not callable(getattr(self.position_manager, method)):
                logger.error(f"Real PositionManager missing required method: {method}")
                return False

        logger.debug("Real service interfaces validated successfully for OCRIntelligenceEngine.")
        return True

    def setup_for_session(self, session_config: TestSessionConfig) -> bool:
        # ... (Implementation from Chunk 10 - uses self.data_source_factory) ...
        if not self.data_source_factory or not self.test_config: # test_config is OCRTestConfig
            logger.error("OCR Engine not fully initialized before setup_for_session call."); return False
        try:
            self.data_source = self.data_source_factory.create_ocr_data_source(session_config)
            if self.data_source:
                if self.data_source.load_timeline_data():
                    logger.info(f"OCR Engine setup data source for session: {session_config.session_name}")
                    self._current_session_id = session_config.session_name # Using name as ID for now
                    self._previous_ocr_interpreted_states_for_engine.clear()
                    self.analysis_results = []
                    return True
                else: logger.error(f"Failed to load OCR timeline for session: {session_config.session_name}"); return False
            else: logger.error("Failed to create OCR data source."); return False
        except Exception as e: logger.error(f"Failed setup OCR Engine for session {session_config.session_name}: {e}", exc_info=True); return False
            
    def start(self) -> None: # Return type changed to None
        if not self.data_source or not self.snapshot_interpreter or not self.position_manager:
            logger.error("OCR Engine cannot start: not properly initialized or session not set up.")
            self._engine_active = False; return
        if self.data_source: self.data_source.start()
        self._engine_active = True
        logger.info(f"OCR Intelligence Engine started for session {self._current_session_id}.")

    def stop(self) -> None:
        self._engine_active = False
        if self.data_source: self.data_source.stop()
        logger.info(f"OCR Intelligence Engine stopped for session {self._current_session_id}.")
        # Persist analysis_results if needed, or done by controller
        self._current_session_id = None

    def get_status(self) -> Dict[str, Any]:
        # ... (as in Chunk 10)
        progress = 0.0
        if self.data_source and self.data_source.is_active():
             progress_info = self.data_source.get_replay_progress()
             progress = progress_info.get('progress_percentage_loaded',0.0)
        return {"active": self._engine_active, "session_id": self._current_session_id,
                "data_source_active": self.data_source.is_active() if self.data_source else False,
                "replay_progress_percent": progress, "analysis_results_count": len(self.analysis_results)}

    def _get_position_context(self) -> Dict[str, Any]:
        """Get current position context from the real PositionManager."""
        if not self.position_manager: return {'error': 'PositionManager not available', 'context_timestamp': time.time()}
        try:
            # These method names are based on LA spec for _get_position_context
            pos_data = {}
            if hasattr(self.position_manager, 'get_position_data'): pos_data['current_positions'] = self.position_manager.get_position_data()
            if hasattr(self.position_manager, 'get_symbol_positions'): pos_data['symbol_positions'] = self.position_manager.get_symbol_positions()
            if hasattr(self.position_manager, 'get_account_summary'): pos_data['account_summary'] = self.position_manager.get_account_summary()
            pos_data['context_timestamp'] = time.time()
            return pos_data
        except Exception as e:
            logger.error(f"Error getting position context from real PositionManager: {e}", exc_info=True)
            return {'error': str(e), 'context_timestamp': time.time()}

    def _prepare_ocr_snapshot_for_interpreter(self, event: OCRTimelineEvent) -> Dict[str, Any]:
        """Converts OCRTimelineEvent.data (which is the ocr_payload from log) to format expected by real SnapshotInterpreterService."""
        # event.data is the dict logged as 'ocr_data' inside 'event_payload' by CorrelationLogger,
        # which itself came from EnhancedOCRDataConditioningService's ocr_payload.
        # That payload contained: frame_number, expected_position_dict, ocr_confidence, raw_ocr_data etc.
        # The real SnapshotInterpreterService.interpret_snapshot(ocr_snapshot_data, context)
        # needs 'ocr_snapshot_data' to be Dict[str,Any] representing the conditioned output.

        ocr_data_from_event = event.data if isinstance(event.data, dict) else {}

        # This should be the 'conditioned_data' part of the logged ocr_payload
        # Or, if the entire ocr_payload is considered the "snapshot" for the interpreter:
        return {
            'frame_number': event.frame_number, # Already direct attribute of OCRTimelineEvent
            'raw_ocr_data': ocr_data_from_event.get('raw_ocr_data', {}),
            'ocr_confidence': ocr_data_from_event.get('ocr_confidence', 0.0),
            'timestamp': event.epoch_timestamp_s, # Use epoch from event
            # The crucial part: what the live interpreter uses for its 'cost_basis', 'realized_pnl', 'strategy_hint'
            # These should be in ocr_data_from_event if they came from conditioned_result.to_dict()
            'cost_basis': ocr_data_from_event.get('cost_basis'), # If present
            'realized_pnl': ocr_data_from_event.get('realized_pnl'), # If present
            'strategy_hint': ocr_data_from_event.get('strategy_hint'), # If present
            'symbol': event.expected_position.symbol if event.expected_position else "UNKNOWN", # If interpreter needs symbol in snapshot
            # Include the full conditioned data if that's what interpret_snapshot expects
            'conditioned_data_snapshot': ocr_data_from_event.get('conditioned_data', ocr_data_from_event)
        }

    def _get_service_versions(self) -> Dict[str, str]:
        versions = {'analysis_engine_version': 'IntelliSenseOCR-1.0'} # This engine's version
        if self.snapshot_interpreter and hasattr(self.snapshot_interpreter, '__version__'):
            versions['snapshot_interpreter_version'] = self.snapshot_interpreter.__version__
        elif self.snapshot_interpreter: versions['snapshot_interpreter_version'] = 'unknown_si_version'
        if self.position_manager and hasattr(self.position_manager, '__version__'):
            versions['position_manager_version'] = self.position_manager.__version__
        elif self.position_manager: versions['position_manager_version'] = 'unknown_pm_version'
        return versions

    def _create_diagnostic_info(self, event: OCRTimelineEvent, error: Exception) -> Dict[str, Any]:
        return {
            "error_message": str(error),
            "error_type": type(error).__name__,
            "event_gsi": event.global_sequence_id,
            "event_frame_number": event.frame_number,
            "engine_active_status": self._engine_active
        }

    def process_event(self, event: OCRTimelineEvent) -> OCRAnalysisResult:
        # ... (Full implementation as per Lead Architect's spec from previous turn) ...
        # Key steps:
        # 1. Check if engine active
        # 2. Record analysis_start time
        # 3. Get position_context using self._get_position_context() -> measures context_latency_ns
        # 4. Prepare ocr_snapshot for interpreter using self._prepare_ocr_snapshot_for_interpreter(event)
        # 5. Call self.snapshot_interpreter.interpret_snapshot(ocr_snapshot, position_context) -> measures interpreter_latency_ns
        # 6. Validate interpreted_result against event.expected_position using OCRValidationResult.compare_positions()
        # 7. Create OCRAnalysisResult with all data and latencies.
        # 8. Store result, log, handle errors.
        if not self._engine_active: # Guard
            return OCRAnalysisResult(frame_number=event.frame_number, epoch_timestamp_s=event.epoch_timestamp_s, perf_counter_timestamp=event.perf_counter_timestamp,
                                     original_ocr_event_global_sequence_id=event.global_sequence_id, analysis_error="OCR Engine not active.")

        analysis_start_ns = time.perf_counter_ns()
        interpreted_pos_dict: Optional[Dict[str, Dict[str, Any]]] = None
        interpreter_lat_ns: Optional[int] = None; context_lat_ns: Optional[int] = None
        validation_res: Optional[OCRValidationResult] = None; pos_ctx: Optional[Dict[str,Any]] = None
        analysis_err_str: Optional[str] = None; diag_info: Optional[Dict[str,Any]] = None

        try:
            ctx_start_ns = time.perf_counter_ns()
            pos_ctx = self._get_position_context()
            context_lat_ns = time.perf_counter_ns() - ctx_start_ns

            ocr_snapshot_for_si = self._prepare_ocr_snapshot_for_interpreter(event)

            interp_start_ns = time.perf_counter_ns()
            interpreted_pos_dict = self.snapshot_interpreter.interpret_snapshot(ocr_snapshot_for_si, pos_ctx)
            interpreter_lat_ns = time.perf_counter_ns() - interp_start_ns

            # Ground truth from event's data (which is the ocr_payload from log)
            expected_pos_dict_from_event_data = {}
            if event.expected_position: # Use the property which parses from event.data
                expected_pos_dict_from_event_data = {event.expected_position.symbol: event.expected_position.to_dict()}

            validation_res = OCRValidationResult.compare_positions(interpreted_pos_dict, expected_pos_dict_from_event_data)

            if not validation_res.is_valid:
                logger.warning(f"OCR Validation Mismatch: Frame {event.frame_number}. Discrepancies: {validation_res.discrepancies[:2]}")

        except Exception as e:
            analysis_err_str = str(e)
            diag_info = self._create_diagnostic_info(event, e)
            logger.error(f"Error processing OCRTimelineEvent (Frame {event.frame_number}): {e}", exc_info=True)
        
        analysis_res_obj = OCRAnalysisResult(
            frame_number=event.frame_number, epoch_timestamp_s=event.epoch_timestamp_s, perf_counter_timestamp=event.perf_counter_timestamp,
            original_ocr_event_global_sequence_id=event.global_sequence_id,
            captured_processing_latency_ns=event.processing_latency_ns,
            context_retrieval_latency_ns=context_lat_ns, interpreter_latency_ns=interpreter_lat_ns,
            interpreted_positions_dict=interpreted_pos_dict,
            expected_positions_dict=expected_pos_dict_from_event_data if 'expected_pos_dict_from_event_data' in locals() else {},
            position_context=pos_ctx, validation_result=validation_res,
            service_versions=self._get_service_versions(),
            analysis_error=analysis_err_str, diagnostic_info=diag_info
        )
        self.analysis_results.append(analysis_res_obj)
        total_analysis_latency_ns = time.perf_counter_ns() - analysis_start_ns
        logger.debug(f"OCR analysis for frame {event.frame_number} completed in {total_analysis_latency_ns / 1_000_000:.2f}ms")
        return analysis_res_obj

    def get_analysis_results(self) -> List[OCRAnalysisResult]:
        return list(self.analysis_results)