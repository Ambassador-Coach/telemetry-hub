# modules/ocr_processing/ocr_data_conditioning_service.py
import logging
import time
import threading
import sys
import uuid
from queue import SimpleQueue, Empty as QueueEmpty
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, TYPE_CHECKING

from interfaces.core.services import ILifecycleService, IEventBus
from interfaces.ocr.services import IOCRDataConditioner, IOCRDataConditioningService
from utils.thread_safe_uuid import get_thread_safe_uuid
from modules.ocr.data_types import OCRParsedData
from utils.global_config import GlobalConfig
from interfaces.logging.services import ICorrelationLogger

# PRODUCTION: Import real classes directly - no stubs for live trading!
from core.events import CleanedOCRSnapshotEvent, CleanedOCRSnapshotEventData

logger = logging.getLogger(__name__)

# Stub for ObservabilityDataPublisher (placeholder for future implementation)
class ObservabilityDataPublisher:
    """Stub for observability data publisher - placeholder for future implementation."""

    def __init__(self, config_service: Optional[GlobalConfig] = None):
        self.logger = logging.getLogger(__name__)
        self._enabled = False
        if config_service:
            self._enabled = getattr(config_service, 'FEATURE_FLAG_ENABLE_OBSERVABILITY_PUBLISHER', False)

    def publish_cleaned_ocr(self, cleaned_data_snapshots_dict):
        """Publish cleaned OCR data for observability (stub implementation)."""
        if self._enabled:
            self.logger.debug(f"ObservabilityDataPublisher: Would publish cleaned OCR data with {len(cleaned_data_snapshots_dict)} symbols")
        # Future implementation will send data to observability system

    def publish_raw_ocr(self, ocr_data_dict):
        """Publish raw OCR data for observability (stub implementation)."""
        if self._enabled:
            symbols_count = len(ocr_data_dict.get('snapshots', {}))
            frame_ts = ocr_data_dict.get('frame_timestamp', 'N/A')
            self.logger.debug(f"ObservabilityDataPublisher: Would publish raw OCR data with {symbols_count} symbols, Frame TS: {frame_ts}")
        # Future implementation will send data to observability system

class OCRDataConditioningService(IOCRDataConditioningService, ILifecycleService):
    """
    Lean OCR Data Conditioning Service.
    
    This service acts as a bridge between raw OCR data and the orchestrator.
    It receives raw OCR data via direct enqueue, processes it through the conditioner,
    and passes cleaned data directly to the orchestrator service (bypassing EventBus
    for high-frequency OCR events).
    """
    
    def __init__(self,
                 event_bus: IEventBus,  # Kept for compatibility but not used - direct enqueue only
                 conditioner: IOCRDataConditioner, # This is PythonOCRDataConditioner instance
                 orchestrator_service: 'OCRScalpingSignalOrchestratorService', # Forward reference
                 observability_publisher: 'ObservabilityDataPublisher', # Forward reference
                 config_service: Optional[GlobalConfig] = None,
                 benchmarker: Optional[Any] = None,
                 pipeline_validator: Optional[Any] = None,
                 price_provider: Optional[Any] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        self.logger = logger
        # self._event_bus = event_bus  # REMOVED: Using direct enqueue only, no EventBus needed
        self._conditioner = conditioner
        self._orchestrator = orchestrator_service
        self._observability_publisher = observability_publisher # Corrected name from proposal
        self._config_service = config_service
        self._benchmarker = benchmarker # From Task 1.1
        self._pipeline_validator = pipeline_validator # For end-to-end pipeline validation
        self._price_provider = price_provider # For time synchronization
        # Redis publishing handled via correlation logger
        self._correlation_logger = correlation_logger
        self._is_running = False
        self._is_ready = False
        self._workers_started = 0
        self._workers_ready = 0

        self._MAX_CONDITIONING_QUEUE_SIZE = getattr(config_service, 'ocr_conditioning_queue_max_size', 200)
        self._conditioning_queue = SimpleQueue()
        print(f"AGENT_DIAG_SERVICE_INIT: OCRDataConditioningService created, service ID: {id(self)}, queue ID: {id(self._conditioning_queue)}")
        self._num_workers = getattr(config_service, 'ocr_conditioning_workers', 1)

        # Heavy setup moved to start() method
        self._conditioning_worker_pool = None  # Created in start()
        self._stop_conditioning_event = threading.Event()

        self._events_processed = 0 # From existing code
        self._events_published = 0 # From existing code
        self._processing_errors = 0 # From existing code
        self._start_time = None # From existing code
        self._debug_mode = False # From existing code
        if config_service:
            self._debug_mode = getattr(config_service, 'debug_ocr_handler', False)

        # Direct path only - no EventBus subscription needed
        logger.info("OCRDataConditioningService: Using direct enqueue for raw OCR data. "
                    "Master correlation ID will be OCRParsedData.event_id.")

        logger.info("OCRDataConditioningService initialized.")

    @property
    def is_ready(self) -> bool:
        """Returns True if the service is fully initialized and ready to operate."""
        return (self._is_running and 
                self._is_ready and 
                self._conditioning_worker_pool is not None and
                self._workers_ready >= self._num_workers)

    def get_price_provider(self):
        """Get the price provider instance for time synchronization."""
        return self._price_provider

    def _process_conditioning_queue(self):
        """Process OCR conditioning tasks from the queue in a worker thread."""
        self.logger.info(f"Conditioning worker thread {threading.current_thread().name} starting loop.")
        
        # RACE CONDITION FIX: Brief delay to ensure dependencies are fully initialized
        time.sleep(0.5)
        
        # Mark this worker as ready
        self._workers_ready += 1
        if self._workers_ready >= self._num_workers:
            self._is_ready = True
            self.logger.info("All OCR conditioning workers are ready - service is now ready")
        
        print(f"AGENT_DIAG_WORKER_READY: Worker thread {threading.current_thread().name} ready after initialization delay")
        
        print(f"AGENT_DIAG_BEFORE_QUEUE_ACCESS: About to access self._conditioning_queue")
        try:
            print(f"AGENT_DIAG_SELF_CHECK: self object exists: {self is not None}")
            print(f"AGENT_DIAG_ATTR_CHECK: hasattr _conditioning_queue: {hasattr(self, '_conditioning_queue')}")
            # CRITICAL FIX: Ensure worker thread has correct queue reference
            print(f"AGENT_DIAG_QUEUE_REFERENCE: Worker thread queue reference ID: {id(self._conditioning_queue)}")
        except Exception as e:
            print(f"AGENT_DIAG_QUEUE_REF_ERROR: Error accessing queue reference: {e}")
            self.logger.critical(f"Worker thread queue reference error: {e}", exc_info=True)
            
            # Publish queue reference error
            queue_error_context = {
                "thread_name": threading.current_thread().name,
                "exception_type": type(e).__name__,
                "processing_stage": "QUEUE_ACCESS",
                "queue_object_exists": hasattr(self, '_conditioning_queue'),
                "self_object_exists": self is not None
            }
            
            self._publish_conditioning_error_to_redis(
                error_type="QUEUE_REFERENCE_ERROR",
                error_message=f"Worker thread queue reference error: {e}",
                error_context=queue_error_context
            )
            
            return  # Exit worker thread if can't access queue

        while not self._stop_conditioning_event.is_set():
            try:
                # Use non-blocking get. This is the key to preventing the deadlock.
                ocr_parsed_data = self._conditioning_queue.get_nowait()

                # If we get here, an item was dequeued. Process it.
                # --- All the original item processing logic goes here ---
                print(f"AGENT_DIAG_ITEM_DEQUEUED: Worker processing FrameTS: {getattr(ocr_parsed_data, 'frame_timestamp', 'N/A')}")

                try:
                    # Process the OCR data
                    self._events_processed += 1

                    # Get the master correlation ID from the stored attribute (Break #1 fix)
                    master_correlation_id = getattr(ocr_parsed_data, 'master_correlation_id', None)
                    
                    # CRITICAL: Warn if correlation ID is missing but continue processing
                    if master_correlation_id is None:
                        self.logger.warning(
                            f"CRITICAL: No master correlation ID found on OCR data for frame_timestamp: "
                            f"{getattr(ocr_parsed_data, 'frame_timestamp', 'N/A')}. "
                            f"This will break end-to-end tracing chain but continuing processing."
                        )
                        # Generate a new correlation ID for this event to continue processing
                        import uuid
                        master_correlation_id = str(uuid.uuid4())
                    
                    self.logger.info(f"[OCR_CONDITIONING] Processing OCR data with CorrID: {master_correlation_id}")

                    import asyncio
                    cleaned_data_snapshots = asyncio.run(self._conditioner.condition_ocr_data(ocr_parsed_data))
                    self.logger.info(f"[OCR_CONDITIONING] Conditioning completed for CorrID: {master_correlation_id}")

                    if cleaned_data_snapshots and cleaned_data_snapshots.conditioned_snapshots:
                        # Extract the actual snapshots dictionary from CleanedOcrResult
                        conditioned_snapshots = cleaned_data_snapshots.conditioned_snapshots
                        
                        # Generate unique Event ID for this cleaned OCR event (Break #2 fix)
                        original_ocr_event_id = getattr(ocr_parsed_data, 'event_id', get_thread_safe_uuid())
                        
                        # Generate validation ID for pipeline tracking
                        validation_id = get_thread_safe_uuid()
                        self.logger.debug(f"Generated validation_id {validation_id} for OCR frame {getattr(ocr_parsed_data, 'frame_timestamp', 0.0)}")
                        
                        # Start OCR pipeline validation tracking
                        if self._pipeline_validator:
                            try:
                                # Start validation tracking for this OCR processing pipeline
                                self._pipeline_validator.start_validation(
                                    pipeline_type='ocr_to_signal',
                                    custom_uid=validation_id
                                )
                                self.logger.debug(f"Started OCR pipeline validation for validation_id: {validation_id}")
                                
                                # Register the first stage immediately
                                self._pipeline_validator.register_stage_completion(
                                    validation_id,
                                    'ocr_conditioning_started',
                                    stage_data={
                                        'frame_timestamp': getattr(ocr_parsed_data, 'frame_timestamp', 0.0),
                                        'master_correlation_id': master_correlation_id,
                                        'symbols_count': len(conditioned_snapshots)
                                    }
                                )
                            except Exception as e:
                                self.logger.warning(f"Error starting OCR pipeline tracking: {e}")
                                
                                # Publish pipeline validation error
                                pipeline_error_context = {
                                    "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 'N/A'),
                                    "validation_id": validation_id,
                                    "pipeline_type": "ocr_to_signal",
                                    "validation_stage": "START_VALIDATION",
                                    "exception_type": type(e).__name__,
                                    "symbols_count": len(conditioned_snapshots) if conditioned_snapshots else 0
                                }
                                
                                self._publish_conditioning_error_to_redis(
                                    error_type="PIPELINE_VALIDATION_ERROR",
                                    error_message=f"Error starting OCR pipeline tracking: {e}",
                                    error_context=pipeline_error_context
                                )
                        
                        # Create the payload for the orchestrator
                        cleaned_event_data_payload = CleanedOCRSnapshotEventData(
                            frame_timestamp=getattr(ocr_parsed_data, 'frame_timestamp', 0.0),
                            snapshots=conditioned_snapshots,
                            original_ocr_event_id=original_ocr_event_id,  # Original OCR event ID for causation
                            raw_ocr_confidence=getattr(ocr_parsed_data, 'overall_confidence', None),
                            origin_correlation_id=master_correlation_id,  # Master correlation ID for end-to-end tracing
                            validation_id=validation_id,  # Pipeline validation ID for end-to-end tracking
                            origin_timestamp_ns=getattr(ocr_parsed_data, 'origin_timestamp_ns', None),  # Golden timestamp for accurate latency measurement
                            
                            # --- PASS ANALYSIS METADATA FORWARD ---
                            roi_used_for_capture=getattr(ocr_parsed_data, 'roi_used_for_capture', []),
                            preprocessing_params_used=getattr(ocr_parsed_data, 'preprocessing_params_used', {})
                        )
                        
                        self.logger.info(f"[OCR_CONDITIONING] Created cleaned event payload with CorrID: {master_correlation_id}")

                        # Register conditioning completed stage
                        if self._pipeline_validator and validation_id:
                            try:
                                self._pipeline_validator.register_stage_completion(
                                    validation_id,
                                    'ocr_conditioning_completed',
                                    stage_data={'symbols_count': len(conditioned_snapshots)}
                                )
                            except Exception as e:
                                self.logger.warning(f"Error registering ocr_conditioning_completed: {e}")
                                
                                # Publish pipeline validation error
                                pipeline_error_context = {
                                    "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 'N/A'),
                                    "validation_id": validation_id,
                                    "pipeline_type": "ocr_to_signal",
                                    "validation_stage": "CONDITIONING_COMPLETED",
                                    "exception_type": type(e).__name__,
                                    "symbols_count": len(conditioned_snapshots) if conditioned_snapshots else 0
                                }
                                
                                self._publish_conditioning_error_to_redis(
                                    error_type="PIPELINE_VALIDATION_ERROR",
                                    error_message=f"Error registering ocr_conditioning_completed: {e}",
                                    error_context=pipeline_error_context
                                )

                        # Publish cleaned OCR data to Redis stream
                        self._publish_cleaned_ocr_to_redis(cleaned_event_data_payload)

                        # Direct Path: Enqueue to OCRScalpingSignalOrchestratorService
                        if self._orchestrator:
                            # --- CAPTURE T5 ---
                            t5_handoff_to_orchestrator_ns = time.perf_counter_ns()
                            
                            # Generate unique event ID for this cleaned OCR snapshot for IntelliSense causation tracking
                            cleaned_event_id = get_thread_safe_uuid()
                            self.logger.info(f"[OCR_CONDITIONING] Enqueuing to orchestrator with CorrID: {master_correlation_id}, CleanedEventID: {cleaned_event_id}")
                            self._orchestrator.enqueue_cleaned_snapshot_data(cleaned_event_data_payload, cleaned_event_id)
                            print(f"AGENT_DIAG_HANDOFF: Handed off {len(conditioned_snapshots)} snapshots to orchestrator with event ID {cleaned_event_id}.")
                            
                            # --- Report T4 -> T5 and T0 -> T5 Latency ---
                            t0_ns = getattr(ocr_parsed_data, 'raw_image_grab_ns', None)
                            t4_ns = getattr(ocr_parsed_data, 't4_handoff_to_conditioner_ns', None)
                            
                            if self._correlation_logger and t4_ns and t0_ns:
                                conditioning_latency_ms = (t5_handoff_to_orchestrator_ns - t4_ns) / 1_000_000
                                total_pipeline_ms = (t5_handoff_to_orchestrator_ns - t0_ns) / 1_000_000
                                
                                self._correlation_logger.log_event(
                                    source_component="OCRDataConditioningService",
                                    event_name="OcrConditioningLatency",
                                    payload={
                                        "correlation_id": master_correlation_id,
                                        "t4_to_t5_ms": conditioning_latency_ms,
                                        "total_t0_to_t5_ms": total_pipeline_ms,
                                        "timestamps_ns": {"t4": t4_ns, "t5": t5_handoff_to_orchestrator_ns}
                                    },
                                    stream_override="testrade:telemetry:performance"
                                )
                                # Log C++ timestamp breakdown if available
                                cxx_ts = getattr(ocr_parsed_data, 'cxx_milestones_ns', {})
                                if cxx_ts:
                                    cxx_t0 = cxx_ts.get('t0_entry')
                                    cxx_t1 = cxx_ts.get('t1_preproc_done')
                                    cxx_t2 = cxx_ts.get('t2_contours_done')
                                    cxx_t3 = cxx_ts.get('t3_tess_start')
                                    cxx_t4 = cxx_ts.get('t4_tess_done')
                                    
                                    if cxx_t0 and cxx_t4:
                                        # Calculate C++ phase durations
                                        cxx_total_ms = (cxx_t4 - cxx_t0) / 1_000_000
                                        preproc_ms = (cxx_t1 - cxx_t0) / 1_000_000 if cxx_t1 else 0
                                        contours_ms = (cxx_t2 - cxx_t1) / 1_000_000 if cxx_t1 and cxx_t2 else 0
                                        tess_setup_ms = (cxx_t3 - cxx_t2) / 1_000_000 if cxx_t2 and cxx_t3 else 0
                                        tess_ocr_ms = (cxx_t4 - cxx_t3) / 1_000_000 if cxx_t3 else 0
                                        # CRITICAL FIX: Calculate Python overhead as time between Python T0 and C++ T0
                                        # Both timestamps should be from the same clock base (perf_counter_ns)
                                        if t0_ns and cxx_t0:
                                            # Sanity check: C++ T0 should be after Python T0 (within reasonable bounds)
                                            raw_overhead_ms = (cxx_t0 - t0_ns) / 1_000_000
                                            if 0 <= raw_overhead_ms <= 50:  # Reasonable overhead range
                                                python_overhead_ms = raw_overhead_ms
                                            else:
                                                # Fallback: timestamp epoch mismatch, estimate 2ms overhead
                                                python_overhead_ms = 2.0
                                                self.logger.debug(f"T0 timestamp mismatch detected ({raw_overhead_ms:.2f}ms), using fallback overhead")
                                        else:
                                            # No timing data available, use reasonable estimate
                                            python_overhead_ms = 2.0
                                        
                                        print(f"[PIPELINE_BREAKDOWN] CorrID {master_correlation_id}: "
                                              f"C++ Total={cxx_total_ms:.2f}ms | "
                                              f"Preproc={preproc_ms:.2f}ms | "
                                              f"Contours={contours_ms:.2f}ms | "
                                              f"Tess Setup={tess_setup_ms:.2f}ms | "
                                              f"Tess OCR={tess_ocr_ms:.2f}ms | "
                                              f"Python Overhead={python_overhead_ms:.2f}ms", flush=True)
                                
                                self.logger.info(f"[PIPELINE_METRIC] T4-T5 for CorrID {master_correlation_id}: {conditioning_latency_ms:.2f}ms")
                                self.logger.info(f"[PIPELINE_METRIC] T0-T5 Total for CorrID {master_correlation_id}: {total_pipeline_ms:.2f}ms")
                            
                            # Register enqueue stage
                            if self._pipeline_validator and validation_id:
                                try:
                                    self._pipeline_validator.register_stage_completion(
                                        validation_id,
                                        'ocr_cleaned_data_enqueued_to_orchestrator',
                                        stage_data={'cleaned_event_id': cleaned_event_id}
                                    )
                                except Exception as e:
                                    self.logger.warning(f"Error registering ocr_cleaned_data_enqueued_to_orchestrator: {e}")
                                    
                                    # Publish pipeline validation error
                                    pipeline_error_context = {
                                        "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 'N/A'),
                                        "validation_id": validation_id,
                                        "pipeline_type": "ocr_to_signal",
                                        "validation_stage": "ENQUEUE_TO_ORCHESTRATOR",
                                        "exception_type": type(e).__name__,
                                        "cleaned_event_id": cleaned_event_id
                                    }
                                    
                                    self._publish_conditioning_error_to_redis(
                                        error_type="PIPELINE_VALIDATION_ERROR",
                                        error_message=f"Error registering ocr_cleaned_data_enqueued_to_orchestrator: {e}",
                                        error_context=pipeline_error_context
                                    )
                        else:
                            self.logger.error("OCRDataConditioningService: orchestrator_service is not available for direct enqueue.")

                        self._events_published += 1
                    else:
                        # No valid trade/summary data found - send explicit "no data" message to clear GUI
                        self._publish_no_trade_data_message(ocr_parsed_data, master_correlation_id)
                        print("AGENT_DIAG_DISCARD: Conditioner returned no snapshots. Item discarded.")

                except Exception as e_item:
                    self.logger.error(f"Failed to process item: {e_item}", exc_info=True)
                    print(f"AGENT_DIAG_ITEM_ERROR: {e_item}")
                    self._processing_errors += 1
                    
                    # Publish item processing error
                    item_error_context = {
                        "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 'N/A'),
                        "ocr_event_id": getattr(ocr_parsed_data, 'event_id', 'N/A'),
                        "master_correlation_id": getattr(ocr_parsed_data, 'master_correlation_id', None),
                        "exception_type": type(e_item).__name__,
                        "processing_stage": "ITEM_PROCESSING",
                        "events_processed": self._events_processed,
                        "processing_errors": self._processing_errors
                    }
                    
                    self._publish_conditioning_error_to_redis(
                        error_type="ITEM_PROCESSING_ERROR",
                        error_message=f"Failed to process OCR item: {e_item}",
                        error_context=item_error_context
                    )
                # --- End of item processing logic ---

            except QueueEmpty:
                # This is the normal path when the queue is empty.
                # Sleep briefly to yield the GIL and prevent a tight, CPU-spinning loop.
                time.sleep(0.05) # 50ms is a reasonable poll interval
                continue # Go back to the top of the while loop
            except Exception as e_critical:
                self.logger.critical(f"Conditioning worker CRASHED: {e_critical}", exc_info=True)
                print(f"AGENT_DIAG_WORKER_CRASH: {e_critical}")
                
                # Publish worker thread crash event
                error_context = {
                    "thread_name": threading.current_thread().name,
                    "worker_crash": True,
                    "exception_type": type(e_critical).__name__,
                    "events_processed": self._events_processed,
                    "events_published": self._events_published,
                    "processing_errors": self._processing_errors
                }
                
                self._publish_conditioning_error_to_redis(
                    error_type="WORKER_THREAD_CRASH",
                    error_message=f"OCR conditioning worker thread crashed: {e_critical}",
                    error_context=error_context
                )
                
                break # Exit the thread on a fatal error

        self.logger.info(f"Conditioning worker thread {threading.current_thread().name} stopped.")



    # The method _enqueue_raw_ocr_data_with_correlation_id is no longer needed due to changes in __init__ and _process_conditioning_queue
    # def _enqueue_raw_ocr_data_with_correlation_id(self, ocr_data: OCRParsedData, correlation_id: str):
    #     # This method is no longer needed due to changes in __init__ and _process_conditioning_queue
    #     pass

    def _publish_cleaned_ocr_to_redis(self, cleaned_data: 'CleanedOCRSnapshotEventData'):
        """Publish cleaned OCR data to Redis stream via Babysitter IPC."""
        try:
            # CORRECTLY get the one true correlation ID from the payload.
            master_correlation_id = getattr(cleaned_data, 'origin_correlation_id', None)

            # TANK Mode check...
            if (hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self._config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for cleaned OCR data (CorrID: {master_correlation_id})")
                return

            import dataclasses
            cleaned_payload_dict = dataclasses.asdict(cleaned_data)
            
            # Ensure consistency by adding the 'correlation_id' key directly to the payload
            cleaned_payload_dict['correlation_id'] = master_correlation_id

            if self._correlation_logger:
                target_stream = getattr(self._config_service, 'redis_stream_cleaned_ocr', 'testrade:cleaned-ocr-snapshots')
                
                # Create the event data payload for telemetry
                event_data = {
                    # This is the crucial part: pass the correct correlation ID to the metadata wrapper
                    "__correlation_id": master_correlation_id, 
                    **cleaned_payload_dict
                }

                self._correlation_logger.log_event(
                    source_component="OCRDataConditioningService",
                    event_name="CleanedOcrSnapshot",
                    payload=event_data,
                    stream_override=target_stream
                )
                self.logger.info(f"Cleaned OCR data logged (CorrID: {master_correlation_id})")
            else:
                self.logger.warning("Correlation logger not available for cleaned OCR data - cannot publish to Redis")

        except Exception as e:
            self.logger.error(f"OCRDataConditioningService: Error in _publish_cleaned_ocr_to_redis: {e}", exc_info=True)
            
            # Publish Redis publishing error
            redis_error_context = {
                "target_stream": getattr(self._config_service, 'redis_stream_cleaned_ocr_snapshots', 'testrade:cleaned-ocr-snapshots'),
                "correlation_id": master_correlation_id,
                "exception_type": type(e).__name__,
                "publishing_stage": "CLEANED_OCR_REDIS_PUBLISH",
                "has_correlation_logger": self._correlation_logger is not None
            }
            
            self._publish_conditioning_error_to_redis(
                error_type="REDIS_PUBLISHING_ERROR",
                error_message=f"Error in _publish_cleaned_ocr_to_redis: {e}",
                error_context=redis_error_context,
                correlation_id=master_correlation_id
            )

    def _publish_no_trade_data_message(self, ocr_parsed_data: 'OCRParsedData', correlation_id: str):
        """Publish 'no trade data' message to clear GUI when no valid trade/summary lines are found."""
        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self._config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for no trade data message (CorrID: {correlation_id})")
                return

            import time

            # Create a special "no data" payload
            no_data_payload = {
                "type": "no_trade_data",
                "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 0.0),
                "reason": "No valid trade or summary lines detected",
                "correlation_id": correlation_id,
                "timestamp": time.time()
            }

            # Send via central telemetry queue if available
            target_stream = getattr(self._config_service, 'redis_stream_cleaned_ocr_snapshots', 'testrade:cleaned-ocr-snapshots')
            
            if self._correlation_logger:
                # Use correlation logger
                self._correlation_logger.log_event(
                    source_component="OCRDataConditioningService",
                    event_name="NoTradeData",
                    payload=no_data_payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"No trade data message logged (CorrID: {correlation_id})")
            else:
                # Fallback - log warning, correlation logger should be available
                self.logger.warning("Correlation logger not available - cannot publish no trade data message")

        except Exception as e:
            self.logger.error(f"OCRDataConditioningService: Error in _publish_no_trade_data_message: {e}", exc_info=True)
            
            # Publish Redis publishing error for no-trade-data message
            redis_error_context = {
                "target_stream": getattr(self._config_service, 'redis_stream_cleaned_ocr_snapshots', 'testrade:cleaned-ocr-snapshots'),
                "correlation_id": correlation_id,
                "exception_type": type(e).__name__,
                "publishing_stage": "NO_TRADE_DATA_REDIS_PUBLISH",
                "frame_timestamp": getattr(ocr_parsed_data, 'frame_timestamp', 'N/A'),
                "has_correlation_logger": self._correlation_logger is not None
            }
            
            self._publish_conditioning_error_to_redis(
                error_type="REDIS_PUBLISHING_ERROR",
                error_message=f"Error in _publish_no_trade_data_message: {e}",
                error_context=redis_error_context,
                correlation_id=correlation_id
            )

    def enqueue_raw_ocr_data(self, ocr_data: OCRParsedData):
        """Thread-safe method to add raw OCR data to the conditioning queue.
        
        Args:
            ocr_data: The OCR parsed data to enqueue (contains master_correlation_id as attribute)
        """
        # This method should be extremely simple to minimize lock time.
        try:
            # The master_correlation_id is already an attribute of the ocr_data object.
            # No need to set it here.
            correlation_id = getattr(ocr_data, 'master_correlation_id', 'unknown')
            self.logger.info(f"[OCR_CONDITIONING] Enqueuing raw OCR data with CorrID: {correlation_id}")
            
            # The queue.put() call is atomic and thread-safe.
            self._conditioning_queue.put(ocr_data)
        except Exception as e:
            # It's very rare for put() on an unbounded queue to fail, but we log if it does.
            self.logger.error(f"Failed to enqueue OCR data: {e}", exc_info=True)
            
            # Publish enqueue error
            enqueue_error_context = {
                "frame_timestamp": getattr(ocr_data, 'frame_timestamp', 'N/A'),
                "ocr_event_id": getattr(ocr_data, 'event_id', 'N/A'),
                "master_correlation_id": master_correlation_id,
                "exception_type": type(e).__name__,
                "queue_size": self.queue_size() if hasattr(self, '_conditioning_queue') else 'unknown',
                "service_running": self._is_running
            }
            
            self._publish_conditioning_error_to_redis(
                error_type="ENQUEUE_ERROR",
                error_message=f"Failed to enqueue OCR data: {e}",
                error_context=enqueue_error_context,
                correlation_id=master_correlation_id
            )

    def _debug_print(self, message: str):
        """Debug print helper that respects debug mode setting."""
        if self._debug_mode:
            self.logger.debug(f"[OCR_CONDITIONING_SERVICE_DEBUG] {message}")



    def start(self) -> None:
        """Start the OCR Data Conditioning Service."""
        print(f"AGENT_DIAG_SERVICE_START_CALLED: OCRDataConditioningService.start() called")
        if self._is_running:
            self.logger.warning("OCRDataConditioningService already running.")
            print(f"AGENT_DIAG_SERVICE_ALREADY_RUNNING: Service already running, returning")
            return

        self.logger.info("OCRDataConditioningService starting...")
        print(f"AGENT_DIAG_SERVICE_STARTING: OCRDataConditioningService starting...")

        # Create ThreadPoolExecutor (moved from __init__)
        if self._conditioning_worker_pool is None:
            self._conditioning_worker_pool = ThreadPoolExecutor(
                max_workers=self._num_workers,
                thread_name_prefix="OCRDataConditionerWorker"
            )

        # Mark as running and record start time
        self._is_running = True
        self._start_time = time.time()
        self._stop_conditioning_event.clear()

        # Reset counters
        self._events_processed = 0
        self._events_published = 0
        self._processing_errors = 0
        
        # Reset readiness tracking
        self._workers_started = 0
        self._workers_ready = 0
        self._is_ready = False

        # Start worker threads
        for i in range(self._num_workers):
            future = self._conditioning_worker_pool.submit(self._process_conditioning_queue)
            print(f"AGENT_DIAG_WORKER_SUBMITTED: Successfully submitted worker thread {i+1}/{self._num_workers} to thread pool executor")

        self.logger.info(f"OCRDataConditioningService started with {self._num_workers} worker thread(s). "
                        f"Queue-based processing enabled.")
        
        # INSTRUMENTATION: Publish service health status - STARTED
        self._publish_service_health_status(
            "STARTED",
            "OCR Data Conditioning Service started successfully",
            {
                "service_name": "OCRDataConditioningService",
                "num_workers": self._num_workers,
                "max_queue_size": self._MAX_CONDITIONING_QUEUE_SIZE,
                "conditioner_available": self._conditioner is not None,
                "orchestrator_available": self._orchestrator is not None,
                "observability_publisher_available": self._observability_publisher is not None,
                "price_provider_available": self._price_provider is not None,
                "health_status": "HEALTHY",
                "startup_timestamp": time.time()
            }
        )

    def stop(self) -> None:
        """Stop service and shutdown worker threads"""
        if not self._is_running:
            return

        self._is_running = False
        self._is_ready = False
        self._stop_conditioning_event.set()

        # Drain the conditioning queue
        drained_count = 0
        while not self._conditioning_queue.empty():
            try:
                self._conditioning_queue.get_nowait()
                drained_count += 1
            except QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"Error draining conditioning queue during stop: {e}", exc_info=True)
                
                # Publish service shutdown error
                shutdown_error_context = {
                    "exception_type": type(e).__name__,
                    "shutdown_stage": "QUEUE_DRAIN_STOP",
                    "items_drained": drained_count,
                    "service_running": self._is_running
                }
                
                self._publish_conditioning_error_to_redis(
                    error_type="SERVICE_SHUTDOWN_ERROR",
                    error_message=f"Error draining conditioning queue during stop: {e}",
                    error_context=shutdown_error_context
                )
                break

        if drained_count > 0:
            self.logger.info(f"OCRDataConditioningService: Drained {drained_count} items from conditioning queue during stop.")



        # Shutdown worker pool
        try:
            if sys.version_info >= (3, 9):
                self._conditioning_worker_pool.shutdown(wait=True, cancel_futures=True)
            else:
                self._conditioning_worker_pool.shutdown(wait=True)
        except Exception as e:
            self.logger.warning(f"Error shutting down worker pool: {e}")

        self.logger.info("OCRDataConditioningService stopped")
        
        # INSTRUMENTATION: Publish service health status - STOPPED
        self._publish_service_health_status(
            "STOPPED",
            "OCR Data Conditioning Service stopped and cleaned up resources",
            {
                "service_name": "OCRDataConditioningService",
                "items_drained": drained_count,
                "events_processed": self._events_processed,
                "events_published": self._events_published,
                "processing_errors": self._processing_errors,
                "stop_reason": "manual_stop",
                "health_status": "STOPPED",
                "shutdown_timestamp": time.time()
            }
        )

    def graceful_shutdown(self, timeout: float = 2.0) -> None:
        """
        Gracefully shutdown the OCR Data Conditioning Service.

        Args:
            timeout: Maximum time to wait for worker threads to finish
        """
        if not self._is_running:
            self.logger.info("OCRDataConditioningService already stopped.")
            return

        self.logger.info("OCRDataConditioningService gracefully stopping...")

        # Signal workers to stop
        self._is_running = False
        self._is_ready = False
        self._stop_conditioning_event.set()

        # Drain the conditioning queue
        drained_count = 0
        while not self._conditioning_queue.empty():
            try:
                self._conditioning_queue.get_nowait()
                drained_count += 1
            except QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"Error draining conditioning queue during graceful shutdown: {e}", exc_info=True)
                
                # Publish service shutdown error
                shutdown_error_context = {
                    "exception_type": type(e).__name__,
                    "shutdown_stage": "QUEUE_DRAIN_GRACEFUL",
                    "items_drained": drained_count,
                    "service_running": self._is_running
                }
                
                self._publish_conditioning_error_to_redis(
                    error_type="SERVICE_SHUTDOWN_ERROR",
                    error_message=f"Error draining conditioning queue during graceful shutdown: {e}",
                    error_context=shutdown_error_context
                )
                break

        if drained_count > 0:
            self.logger.info(f"OCRDataConditioningService: Drained {drained_count} items from conditioning queue during graceful shutdown.")



        # Shutdown worker pool with timeout
        try:
            if sys.version_info >= (3, 9):
                self._conditioning_worker_pool.shutdown(wait=True, cancel_futures=True)
            else:
                self._conditioning_worker_pool.shutdown(wait=True)
        except Exception as e:
            self.logger.warning(f"Error during graceful worker pool shutdown: {e}")

        # Log final statistics
        if self._start_time:
            uptime_seconds = time.time() - self._start_time
            self.logger.info(f"OCRDataConditioningService gracefully stopped. "
                           f"Uptime: {uptime_seconds:.1f}s, "
                           f"Events processed: {self._events_processed}, "
                           f"Events published: {self._events_published}, "
                           f"Processing errors: {self._processing_errors}")
        else:
            self.logger.info("OCRDataConditioningService gracefully stopped.")

    def get_service_metrics(self) -> dict:
        """
        Get service performance metrics.
        
        Returns:
            Dictionary containing service metrics
        """
        uptime_seconds = 0.0
        if self._start_time and self._is_running:
            uptime_seconds = time.time() - self._start_time
        
        metrics = {
            'is_running': self._is_running,
            'uptime_seconds': uptime_seconds,
            'events_processed': self._events_processed,
            'events_published': self._events_published,
            'processing_errors': self._processing_errors,
            'success_rate': 0.0
        }
        
        # Calculate success rate
        if self._events_processed > 0:
            metrics['success_rate'] = (self._events_processed - self._processing_errors) / self._events_processed
        
        # Get conditioner metrics if available
        try:
            conditioner_metrics = self._conditioner.get_conditioner_metrics()
            metrics['conditioner_metrics'] = conditioner_metrics
        except Exception as e:
            self.logger.warning(f"Could not get conditioner metrics: {e}")
            metrics['conditioner_metrics'] = {}
        
        return metrics

    def is_running(self) -> bool:
        """Check if the service is currently running."""
        return self._is_running

    def get_conditioner(self) -> IOCRDataConditioner:
        """Get the underlying conditioner instance."""
        return self._conditioner

    def queue_size(self) -> int:
        """Returns the approximate current size of the conditioning queue."""
        return self._conditioning_queue.qsize()

    def set_orchestrator_service(self, orchestrator_service: 'OCRScalpingSignalOrchestratorService'):
        """
        Set the orchestrator service reference after initialization.

        This is needed to resolve circular dependency between conditioning and orchestrator services.

        Args:
            orchestrator_service: The OCRScalpingSignalOrchestratorService instance
        """
        self._orchestrator = orchestrator_service
        self.logger.info("OCRDataConditioningService: Orchestrator service reference set successfully.")

    def _publish_conditioning_error_to_redis(self, error_type: str, error_message: str, 
                                           error_context: dict, correlation_id: str = None):
        """
        Publish OCR conditioning error events to Redis stream via Babysitter IPC.
        
        Args:
            error_type: Type of error (e.g., 'WORKER_THREAD_CRASH', 'PIPELINE_VALIDATION_ERROR')
            error_message: Human-readable error message
            error_context: Additional context about the error
            correlation_id: Optional correlation ID for tracing
        """
        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self._config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for conditioning error (Type: {error_type})")
                return

            import time
            correlation_id = correlation_id or f"conditioning_error_{int(time.time_ns())}"
            
            # Create error event payload
            error_payload = {
                "error_type": error_type,
                "error_message": error_message,
                "error_context": error_context,
                "timestamp": time.time(),
                "component": "OCRDataConditioningService",
                "severity": "error",
                "correlation_id": correlation_id
            }

            # Send via central telemetry queue if available
            target_stream = getattr(self._config_service, 'redis_stream_raw_ocr', 'testrade:raw-ocr-events')
            
            if self._correlation_logger:
                # Use correlation logger
                self._correlation_logger.log_event(
                    source_component="OCRDataConditioningService",
                    event_name="OcrConditioningError",
                    payload=error_payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"Published conditioning error via correlation logger: {error_type}")
            else:
                # Fallback - log warning, correlation logger should be available
                self.logger.warning("Correlation logger not available - cannot publish conditioning error")

        except Exception as e:
            self.logger.error(f"Error publishing conditioning error event: {e}", exc_info=True)

    # --- Health Status Publishing Methods ---

    def _publish_service_health_status(self, status: str, message: str, context: dict):
        """
        Publish health status events to Redis stream via telemetry service.
        
        Args:
            status: Service status (STARTED, STOPPED, HEALTHY, DEGRADED, etc.)
            message: Human-readable status message
            context: Additional context data for the health event
        """
        try:
            # Check if IPC data dumping is disabled (TANK mode) FIRST
            if (hasattr(self._config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self._config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping IPC send for OCR conditioning service health status - ENABLE_IPC_DATA_DUMP is False")
                return
            
            # Create service health status payload
            payload = {
                "health_status": status,
                "status_reason": message,
                "context": context,
                "status_timestamp": time.time(),
                "component": "OCRDataConditioningService"
            }
            
            # Get target stream from config
            target_stream = getattr(self._config_service, 'redis_stream_system_health', 'testrade:system-health')
            
            # Send via correlation logger if available
            if self._correlation_logger:
                # Use correlation logger
                self._correlation_logger.log_event(
                    source_component="OCRDataConditioningService",
                    event_name="ServiceHealthStatus",
                    payload=payload,
                    stream_override=target_stream
                )
                self.logger.debug(f"OCR conditioning service health status published via correlation logger: {status}")
            else:
                # Fallback - log warning, correlation logger should be available
                self.logger.warning("Correlation logger not available - cannot publish service health status")
                
        except Exception as e:
            # Never let health publishing errors affect main functionality
            self.logger.error(f"OCRDataConditioningService: Error publishing health status: {e}", exc_info=True)

    def _publish_periodic_health_status(self):
        """
        Publish periodic health status with operational metrics.
        Called during normal operation to provide health monitoring data.
        """
        try:
            # Calculate operational metrics
            current_time = time.time()
            
            # Calculate queue utilization
            queue_size = 0
            try:
                queue_size = self._conditioning_queue.qsize()
            except:
                queue_size = 0  # qsize() may not be available on all platforms
            
            queue_utilization = (queue_size / self._MAX_CONDITIONING_QUEUE_SIZE) * 100 if self._MAX_CONDITIONING_QUEUE_SIZE > 0 else 0
            
            # Calculate processing rate
            processing_rate = 0.0
            if self._start_time and current_time > self._start_time:
                uptime_seconds = current_time - self._start_time
                processing_rate = self._events_processed / uptime_seconds if uptime_seconds > 0 else 0
            
            # Calculate error rate
            error_rate = 0.0
            if self._events_processed > 0:
                error_rate = (self._processing_errors / self._events_processed) * 100
            
            # Basic health indicators
            health_context = {
                "service_name": "OCRDataConditioningService",
                "is_running": self._is_running,
                "is_ready": self._is_ready,
                "num_workers": self._num_workers,
                "workers_ready": self._workers_ready,
                "queue_size": queue_size,
                "queue_utilization_percent": queue_utilization,
                "max_queue_size": self._MAX_CONDITIONING_QUEUE_SIZE,
                "events_processed": self._events_processed,
                "events_published": self._events_published,
                "processing_errors": self._processing_errors,
                "processing_rate_per_second": processing_rate,
                "error_rate_percent": error_rate,
                "conditioner_available": self._conditioner is not None,
                "orchestrator_available": self._orchestrator is not None,
                "health_status": "HEALTHY",
                "last_health_check": current_time
            }
            
            # Check for potential health issues
            health_issues = []
            
            # Check queue capacity
            if queue_utilization > 80:
                health_issues.append("queue_near_capacity")
            
            # Check error rate
            if error_rate > 5:  # More than 5% error rate is concerning
                health_issues.append("high_error_rate")
            
            # Check if service is not ready
            if not self._is_ready:
                health_issues.append("service_not_ready")
            
            # Check if workers are not ready
            if self._workers_ready < self._num_workers:
                health_issues.append("workers_not_ready")
            
            if health_issues:
                health_context["health_status"] = "DEGRADED"
                health_context["health_issues"] = health_issues
            
            # Publish health status
            self._publish_service_health_status(
                "PERIODIC_HEALTH_CHECK",
                f"OCRDataConditioningService periodic health check - Status: {health_context['health_status']}",
                health_context
            )
            
        except Exception as e:
            # Never let health publishing errors affect main functionality
            self.logger.error(f"OCRDataConditioningService: Error in periodic health status publishing: {e}", exc_info=True)

    def check_and_publish_health_status(self):
        """
        Public method to trigger health status publishing.
        Can be called externally to force a health check.
        """
        try:
            self._publish_periodic_health_status()
        except Exception as e:
            self.logger.error(f"OCRDataConditioningService: Error in external health status check: {e}", exc_info=True)
