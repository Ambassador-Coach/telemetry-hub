import logging
import time
from typing import List, Dict, Any, Optional

from intellisense.core.interfaces import IPriceIntelligenceEngine, IPriceDataSource
from intellisense.core.types import PriceTimelineEvent
from intellisense.config.session_config import PriceTestConfig, TestSessionConfig
from intellisense.factories.interfaces import IDataSourceFactory
from intellisense.analysis.price_results import PriceAnalysisResult, PriceValidationResult, PriceRepositoryObservedState

# Import the Testable version for type hinting and instanceof checks
from intellisense.analysis.testable_components import IntelliSenseTestablePriceRepository
# Placeholder for base class if needed for type checking against non-testable version
# from modules.price_fetching.price_repository import PriceRepository as BasePriceRepository
class BasePriceRepository: __version__ = "unknown_base_pr" # Placeholder

logger = logging.getLogger(__name__)

class PriceIntelligenceEngine(IPriceIntelligenceEngine):
    def __init__(self): # No args here, passed in initialize
        self.test_config: Optional[PriceTestConfig] = None
        self.data_source_factory: Optional[IDataSourceFactory] = None
        self.data_source: Optional[IPriceDataSource] = None

        # This will hold an instance of IntelliSenseTestablePriceRepository
        self.real_price_repository: Optional[IntelliSenseTestablePriceRepository] = None

        self.analysis_results: List[PriceAnalysisResult] = []
        self._engine_active = False
        self._current_session_id: Optional[str] = None
        self._compatibility_mode = False  # Track if using regular PriceRepository instead of testable version
        logger.info("PriceIntelligenceEngine instance created.")

    def initialize(self,
                   config: PriceTestConfig,
                   data_source_factory: IDataSourceFactory,
                   real_services: Dict[str, Any]
                  ) -> bool:
        self.test_config = config
        self.data_source_factory = data_source_factory

        price_repo_instance = real_services.get('price_repository')
        if not isinstance(price_repo_instance, IntelliSenseTestablePriceRepository):
            logger.warning("PriceIntelligenceEngine: Provided PriceRepository is not an IntelliSenseTestablePriceRepository instance. "
                         "Using compatibility mode - direct injection and state validation will not be possible.")
            # Use compatibility mode - accept regular PriceRepository but with limited functionality
            self.real_price_repository = price_repo_instance
            self._compatibility_mode = True
        else:
            self.real_price_repository = price_repo_instance
            self._compatibility_mode = False

        if not self._validate_service_interfaces(): # Validate the testable interface
            return False

        self.analysis_results = []
        logger.info("Price Intelligence Engine initialized with TestablePriceRepository.")
        return True

    def _validate_service_interfaces(self) -> bool:
        """Validates that the real_price_repository has test hook methods."""
        if not self.real_price_repository: return False

        # Skip validation in compatibility mode (regular PriceRepository)
        if self._compatibility_mode:
            logger.info("Skipping interface validation in compatibility mode (regular PriceRepository)")
            return True

        required_methods = ['on_test_price_event', 'get_cached_state']
        for method in required_methods:
            if not hasattr(self.real_price_repository, method) or not callable(getattr(self.real_price_repository, method)):
                logger.error(f"TestablePriceRepository missing required method for analysis: {method}")
                return False
        logger.debug("TestablePriceRepository interfaces validated for PriceIntelligenceEngine.")
        return True

    # ... (setup_for_session, start, stop, get_status as in Chunk 12) ...
    def setup_for_session(self, sc: TestSessionConfig): # sc is session_config
        if not self.data_source_factory or not self.test_config: return False
        try:
            self.data_source = self.data_source_factory.create_price_data_source(sc)
            if self.data_source and self.data_source.load_timeline_data():
                self._current_session_id = sc.session_name; self.analysis_results=[]; return True
            return False
        except Exception as e: logger.error(f"PriceEngine setup error: {e}", exc_info=True); return False

    def _get_service_versions(self) -> Dict[str, str]: # From Chunk 12
        versions = {'analysis_engine_version': 'IntelliSensePrice-1.0'}
        if self.real_price_repository and hasattr(self.real_price_repository, '__version__'): # Check base version
            versions['price_repository_version'] = self.real_price_repository.__version__
        elif self.real_price_repository: versions['price_repository_version'] = 'unknown_pr_version'
        return versions
            
    def start(self) -> None:
        if not self.data_source or not self.real_price_repository:
            logger.error("Price Engine cannot start: not properly initialized or session not set up.")
            self._engine_active = False; return
        if self.data_source: self.data_source.start()
        self._engine_active = True
        logger.info(f"Price Intelligence Engine started for session {self._current_session_id}.")

    def stop(self) -> None:
        self._engine_active = False
        if self.data_source: self.data_source.stop()
        logger.info(f"Price Intelligence Engine stopped for session {self._current_session_id}.")
        self._current_session_id = None

    def get_status(self) -> Dict[str, Any]:
        progress = 0.0
        if self.data_source and self.data_source.is_active():
             progress_info = self.data_source.get_replay_progress()
             progress = progress_info.get('progress_percentage_loaded',0.0) # Using loaded for source progress
        return {
            "active": self._engine_active, "session_id": self._current_session_id,
            "data_source_active": self.data_source.is_active() if self.data_source else False,
            "replay_progress_percent": progress, "analysis_results_count": len(self.analysis_results)
        }

    def _get_service_versions(self) -> Dict[str, str]:
        versions = {'analysis_engine_version': 'IntelliSensePrice-1.0'}
        if self.real_price_repository and hasattr(self.real_price_repository, '__version__'):
            versions['price_repository_version'] = self.real_price_repository.__version__
        elif self.real_price_repository: versions['price_repository_version'] = 'unknown_pr_version'
        return versions

    def process_event(self, event: PriceTimelineEvent) -> PriceAnalysisResult:
        if not self._engine_active or not self.real_price_repository:
            # ... (return error PriceAnalysisResult as in Chunk 12) ...
            return PriceAnalysisResult(price_event_type=str(event.price_event_type), symbol=str(event.symbol), epoch_timestamp_s=event.epoch_timestamp_s,
                                      perf_counter_timestamp=event.perf_counter_timestamp, analysis_error="Price Engine not active/repo missing")

        replay_pr_proc_lat_ns: Optional[int] = None; validation_res: Optional[PriceValidationResult] = None
        analysis_err_str: Optional[str] = None; diag_info: Optional[Dict[str,Any]] = None

        event_payload = event.data if isinstance(event.data, dict) else {} # This is the payload from correlation log

        try:
            # 1. Inject event into TestablePriceRepository
            if hasattr(self.real_price_repository, 'on_test_price_event'):
                pr_call_start_ns = time.perf_counter_ns()
                # event.event_type is like "TRADE", "QUOTE_BOTH"
                # event.data (payload) contains symbol, price, volume, bid, ask etc.
                self.real_price_repository.on_test_price_event(event.event_type, event_payload)
                replay_pr_proc_lat_ns = time.perf_counter_ns() - pr_call_start_ns
            else:
                analysis_err_str = "PriceRepository has no on_test_price_event method."
                logger.warning(analysis_err_str)

            # 2. Get observed state AFTER injection
            observed_state_after: Optional[PriceRepositoryObservedState] = None
            if hasattr(self.real_price_repository, 'get_cached_state') and event.symbol:
                raw_state = self.real_price_repository.get_cached_state(event.symbol)
                observed_state_after = PriceRepositoryObservedState(
                    symbol=event.symbol,
                    last_trade_price=raw_state.get('last_trade_price'),
                    last_trade_timestamp=raw_state.get('last_trade_timestamp'),
                    bid_price=raw_state.get('bid_price'),
                    bid_timestamp=raw_state.get('bid_timestamp'),
                    ask_price=raw_state.get('ask_price'),
                    ask_timestamp=raw_state.get('ask_timestamp')
                )

            # 3. Perform Validation (conceptual for now)
            if observed_state_after:
                # Example validation: if event was a trade, check if last_trade_price matches
                discrepancies = []
                checks_performed = []
                is_overall_valid = True # Assume valid unless a check fails

                if event.price_event_type == "TRADE" and event.price is not None:
                    checks_performed.append("last_trade_price_match")
                    if observed_state_after.last_trade_price is None or \
                       abs(observed_state_after.last_trade_price - event.price) > 0.0001: # Tolerance
                        discrepancies.append(f"Last trade price mismatch: Expected ~{event.price}, Got {observed_state_after.last_trade_price}")
                        is_overall_valid = False

                # Add more checks for bid/ask if it was a quote event, etc.
                validation_res = PriceValidationResult(
                    is_valid=is_overall_valid,
                    checks_performed=checks_performed,
                    discrepancies=discrepancies,
                    observed_state_after_event=observed_state_after
                )
            else:
                validation_res = PriceValidationResult(is_valid=False, discrepancies=["Could not get observed state from PriceRepository."])

        except Exception as e:
            analysis_err_str = str(e)
            diag_info = {"error_type": type(e).__name__, "event_symbol": str(event.symbol)}
            logger.error(f"Error processing PriceTimelineEvent ({event.symbol}, {event.price_event_type}): {e}", exc_info=True)

        analysis_result = PriceAnalysisResult(
            price_event_type=str(event.price_event_type), symbol=str(event.symbol),
            epoch_timestamp_s=event.epoch_timestamp_s, perf_counter_timestamp=event.perf_counter_timestamp,
            original_price_event_global_sequence_id=event.global_sequence_id,
            captured_processing_latency_ns=event.processing_latency_ns,
            replay_price_repo_injection_latency_ns=replay_pr_proc_lat_ns,
            replayed_event_data=event_payload,
            validation_result=validation_res,
            service_versions=self._get_service_versions(),
            analysis_error=analysis_err_str, diagnostic_info=diag_info
        )
        self.analysis_results.append(analysis_result)
        return analysis_result

    def get_analysis_results(self) -> List[PriceAnalysisResult]:
        return list(self.analysis_results)
