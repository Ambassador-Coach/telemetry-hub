# TESTRADE/utils/pipeline_validator.py
import time
import logging
import statistics
from collections import defaultdict
from uuid import uuid4
from typing import Dict, Any, Optional, List

# Forward reference for ApplicationCore if needed for type hinting
# from typing import TYPE_CHECKING
# if TYPE_CHECKING:
#    from application_core import ApplicationCore # Adjust import path as needed

logger = logging.getLogger(__name__)

class PipelineValidator:
    def __init__(self, ocr_conditioning_service: Optional[Any] = None, price_provider: Optional[Any] = None): # Direct service injection
        """
        Initializes the PipelineValidator.

        Args:
            ocr_conditioning_service: OCR conditioning service for data injection
            price_provider: Price provider service for data injection
        """
        self.ocr_conditioning_service = ocr_conditioning_service
        self.price_provider = price_provider
        self._test_data_tracking: Dict[str, Dict[str, Any]] = {} # Renamed from _test_data
        
        # Define expected stages for different data types/pipelines
        # This can be expanded or made more dynamic.
        self._expected_stages: Dict[str, List[str]] = {
            'ocr_to_signal': [
                'ocr_conditioning_started',         # Stage registered by OCRDataConditioningService worker
                'ocr_conditioning_completed',
                'ocr_cleaned_data_enqueued_to_orchestrator',
                'orchestrator_signal_logic_started',# Stage registered by OCRScalpingSignalOrchestratorService worker
                'orchestrator_signal_logic_completed',
                'order_request_published'           # Stage registered just before publishing OrderRequestEvent
            ],
            'price_to_pmd': [
                'price_data_received_by_pricerepo', # Stage registered by PriceRepository on_trade/on_quote
                'price_data_enqueued_to_risk',
                'risk_pmd_processing_started',      # Stage registered by RiskManagementService PMD worker
                'risk_pmd_processing_completed'
            ],
            'order_execution_path': [
                'order_request_published',          # From OCRScalpingSignalOrchestratorService (linking point)
                'order_request_received_by_risk',   # RiskManagementService receives OrderRequestEvent
                'risk_assessment_completed',        # RiskManagementService completes validation
                'validated_order_received_by_executor', # TradeExecutor receives ValidatedOrderRequestEvent
                'order_sent_to_broker_interface',   # TradeExecutor sends order to broker
                'broker_ack_received',              # Broker acknowledges order receipt
                'order_fill_received'               # Broker reports order fill (terminal success state)
                # Alternative terminal states: 'order_rejected_by_broker'
            ],
            # Add more pipelines as needed
        }
        logger.info("PipelineValidator initialized.")

    def start_validation(self, pipeline_type: str, custom_uid: Optional[str] = None) -> Optional[str]:
        """
        Start validation for a pipeline and return a validation ID.
        This is a convenience method that creates a validation entry without injecting test data.

        Args:
            pipeline_type: The type of pipeline to validate (e.g., "ocr_to_signal")
            custom_uid: Optional custom UID, otherwise generates one

        Returns:
            validation_id if successful, None if failed
        """
        try:
            uid = custom_uid or f"val_{uuid4().hex[:8]}"

            if pipeline_type not in self._expected_stages:
                logger.error(f"PipelineValidator: Unknown pipeline type '{pipeline_type}' for validation start")
                return None

            # Create validation entry
            self._test_data_tracking[uid] = {
                "pipeline_type": pipeline_type,
                "start_time": time.time(),
                "expected_stages": self._expected_stages[pipeline_type].copy(),
                "stages": {},  # FIX: Standardize to 'stages' key everywhere
                "status": "STARTED",
                "expected_outcome": None,  # No specific outcome expected for general validation
                "actual_outcome": None,
                "end_to_end_latency_sec": None
            }

            logger.debug(f"PipelineValidator: Started validation for pipeline '{pipeline_type}' with UID {uid}")
            return uid

        except Exception as e:
            logger.error(f"PipelineValidator: Error starting validation for pipeline '{pipeline_type}': {e}", exc_info=True)
            return None

    def inject_test_data(self,
                         pipeline_type: str, 
                         payload: Dict, 
                         expected_outcome: Optional[str] = None,
                         custom_uid: Optional[str] = None) -> Optional[str]:
        """
        Injects test data into a specified pipeline with a unique tracking ID.

        Args:
            pipeline_type: Key for the pipeline (e.g., "ocr_to_signal", "price_to_pmd").
            payload: The data payload to inject.
            expected_outcome: Optional description of what's expected.
            custom_uid: Optional custom UID to use for this test data.

        Returns:
            The unique ID (UID) for this test data injection, or None if injection failed.
        """
        if pipeline_type == "ocr_to_signal" and not self.ocr_conditioning_service:
            logger.error(f"PipelineValidator: OCR conditioning service not provided. Cannot inject data for pipeline '{pipeline_type}'.")
            return None
        elif pipeline_type == "price_to_pmd" and not self.price_provider:
            logger.error(f"PipelineValidator: Price provider service not provided. Cannot inject data for pipeline '{pipeline_type}'.")
            return None

        uid = custom_uid or str(uuid4())
        # Add the validation ID to the payload so services can pick it up
        if isinstance(payload, dict):
            payload["validation_id"] = uid
        elif hasattr(payload, '__dict__'): # For dataclasses or objects
            try:
                setattr(payload, "validation_id", uid)
            except: # Handle frozen dataclasses etc.
                logger.warning(f"PipelineValidator: Could not set validation_id on payload of type {type(payload)}. It might be immutable.")
                # Consider wrapping it or requiring mutable dicts for injection. For now, log and proceed.
        else:
            logger.error(f"PipelineValidator: Payload for injection must be a dict or object with __dict__. Type: {type(payload)}")
            return None

        self._test_data_tracking[uid] = {
            "injected_at": time.time(),
            "pipeline_type": pipeline_type,
            "payload_preview": str(payload)[:200], # Store a preview
            "expected_outcome": expected_outcome,
            "stages": {}, # Stores {stage_name: {'completed_at': ts, 'result': ...}}
            "completed_fully": False,
            "final_status": "PENDING" # PENDING, COMPLETED, FAILED_TIMEOUT, FAILED_STAGES
        }
        logger.info(f"PipelineValidator: Injected test data for pipeline '{pipeline_type}' with UID: {uid}")

        # Actual injection logic based on pipeline_type
        # This requires ApplicationCore or services to have methods to accept this initial data.
        try:
            if pipeline_type == "ocr_to_signal":
                # Direct service access without get_service
                if self.ocr_conditioning_service and hasattr(self.ocr_conditioning_service, 'enqueue_raw_ocr_data'):
                    # We are injecting OCRParsedData. 'payload' should be an OCRParsedData instance.
                    # The validation_id was added to payload above.
                    self.ocr_conditioning_service.enqueue_raw_ocr_data(payload) 
                else:
                    raise RuntimeError("OCRDataConditioningService or enqueue_raw_ocr_data method not found.")
            elif pipeline_type == "price_to_pmd":
                # Direct service access without get_service
                if self.price_provider and hasattr(self.price_provider, 'on_trade'): # Assuming payload is a trade
                    # 'payload' should be structured like a trade object/dict PriceRepo expects
                    # e.g., payload = {'symbol': 'AAPL', 'price': 150.0, 'size': 100, 'timestamp': time.time(), ...}
                    # The validation_id was added to payload above.
                    self.price_provider.on_trade(**payload) # Unpack if on_trade takes kwargs
                else:
                    raise RuntimeError("PriceRepository or on_trade method not found.")
            elif pipeline_type == "order_execution_path":
                # This is a logical pipeline that tracks order execution stages manually
                # No actual injection needed - just register the tracking entry
                logger.info(f"PipelineValidator: Registered order execution pipeline tracking for UID: {uid}")
                # The tracking entry is already created above, no service injection needed
            else:
                logger.error(f"PipelineValidator: Unknown pipeline_type for injection: {pipeline_type}")
                del self._test_data_tracking[uid] # Clean up if injection point unknown
                return None
            return uid
        except Exception as e_inject:
            logger.error(f"PipelineValidator: Error injecting data for UID {uid}, pipeline '{pipeline_type}': {e_inject}", exc_info=True)
            if uid in self._test_data_tracking: del self._test_data_tracking[uid]
            return None
        
    def register_stage_completion(self, 
                                  validation_id: str, 
                                  stage_name: str, 
                                  stage_data: Optional[Any] = None,
                                  status: str = "COMPLETED",
                                  error_info: Optional[str] = None):
        """
        Called by services within the pipeline when a specific stage is completed for data
        associated with a validation_id.

        Args:
            validation_id: The unique ID of the test data.
            stage_name: A string identifying the pipeline stage (e.g., "ocr_conditioning_completed").
            stage_data: Optional data/result from this stage (e.g., a snippet of cleaned data).
            status: "COMPLETED", "ERROR"
            error_info: Details if status is "ERROR".
        """
        if validation_id in self._test_data_tracking:
            entry = self._test_data_tracking[validation_id]
            # FIX: Use 'stages' key instead of 'completed_stages' to match the dictionary structure
            if "stages" not in entry:
                entry["stages"] = {}
            entry["stages"][stage_name] = {
                'completed_at': time.time(),
                'data_preview': str(stage_data)[:200] if stage_data else None,
                'status': status,
                'error_info': error_info
            }
            logger.info(f"PipelineValidator: UID {validation_id} completed stage '{stage_name}' with status {status}.")
            
            # Check if all expected stages for this pipeline_type are now complete
            pipeline_type = entry.get("pipeline_type")
            if pipeline_type:
                expected_stages_for_pipeline = self._expected_stages.get(pipeline_type, [])
                completed_stages_for_pipeline = {s for s, d in entry["stages"].items() if d['status'] == 'COMPLETED'}
                
                if set(expected_stages_for_pipeline).issubset(completed_stages_for_pipeline):
                    entry["completed_fully"] = True
                    entry["final_status"] = "COMPLETED_ALL_STAGES"
                    # Calculate end-to-end latency if all stages completed
                    if entry["stages"]: # Check if there are any stages recorded
                        first_stage_time = entry["injected_at"]
                        # Find the latest completion time among all successfully completed expected stages
                        latest_completion_time = 0
                        for s_name in expected_stages_for_pipeline:
                            if s_name in entry["stages"] and entry["stages"][s_name]['status'] == 'COMPLETED':
                                latest_completion_time = max(latest_completion_time, entry["stages"][s_name]['completed_at'])
                        
                        if latest_completion_time > 0: # Ensure at least one stage completed to get a valid time
                            entry["end_to_end_latency_sec"] = latest_completion_time - first_stage_time
                            logger.info(f"PipelineValidator: UID {validation_id} completed all expected stages for '{pipeline_type}'. E2E Latency: {entry['end_to_end_latency_sec']:.4f}s.")
        else:
            logger.warning(f"PipelineValidator: Received stage completion for unknown validation_id: {validation_id}, stage: {stage_name}")

    def get_validation_results(self, validation_id: Optional[str] = None) -> Any:
        """Returns results for a specific validation_id or all tracked data."""
        if validation_id:
            return self._test_data_tracking.get(validation_id)
        return dict(self._test_data_tracking) # Return a copy

    def analyze_pipeline_run(self, pipeline_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Analyzes all test data (optionally filtered by pipeline_type)
        and returns summary statistics (completed, failed, latencies).
        """
        results = {
            'total_injected': 0,
            'completed_all_stages': 0,
            'failed_timeout_or_stages': 0,
            'latencies_sec': [],
            'stage_completion_counts': defaultdict(int),
            'failed_stages_summary': defaultdict(list) # {stage_name: [uid1, uid2]}
        }
        
        data_to_analyze = {}
        if pipeline_type:
            for uid, data in self._test_data_tracking.items():
                if data.get("pipeline_type") == pipeline_type:
                    data_to_analyze[uid] = data
        else:
            data_to_analyze = self._test_data_tracking

        results['total_injected'] = len(data_to_analyze)

        for uid, test_data in data_to_analyze.items():
            is_fully_completed = True # Assume success
            
            # Check if all expected stages are present and marked COMPLETED
            current_pipeline_type = test_data.get("pipeline_type")
            expected_stages_for_this = self._expected_stages.get(current_pipeline_type, [])
            
            for stage in expected_stages_for_this:
                stage_info = test_data["stages"].get(stage)
                if not stage_info or stage_info.get('status') != 'COMPLETED':
                    is_fully_completed = False
                    results['failed_stages_summary'][stage].append(uid)
                else:
                    results['stage_completion_counts'][stage] += 1
            
            if is_fully_completed:
                results['completed_all_stages'] += 1
                if "end_to_end_latency_sec" in test_data:
                    results['latencies_sec'].append(test_data["end_to_end_latency_sec"])
            else:
                results['failed_timeout_or_stages'] += 1
        
        if results['latencies_sec']:
            lat = results['latencies_sec']
            lat.sort()
            count = len(lat)
            results['latency_stats_sec'] = {
                'mean': statistics.mean(lat) if lat else 0,
                'median': statistics.median(lat) if lat else 0,
                'p95': lat[int(count * 0.95) -1 if count > 0 else 0] if count > 0 else 0,
                'min': lat[0] if lat else 0,
                'max': lat[-1] if lat else 0,
                'count': count
            }
        else:
             results['latency_stats_sec'] = {}
             
        return results

    def clear_results(self):
        self._test_data_tracking.clear()
        logger.info("PipelineValidator: All test data results cleared.")