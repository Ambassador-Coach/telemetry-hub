"""
OCR Scalping Signal Orchestrator Service module.

This module provides the OCRScalpingSignalOrchestratorService class that receives cleaned OCR data
via direct enqueue from the OCRDataConditioningService, interprets trading signals using 
SnapshotInterpreterService, applies filtering using MasterActionFilterService, and publishes 
OrderRequestEvents when trading opportunities are identified. Uses ThreadPoolExecutor pattern
for processing cleaned OCR data in dedicated worker threads.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty as QueueEmpty, Full as QueueFull
from typing import Any, Optional, Dict, Tuple, Union
from datetime import datetime
import pytz
import queue

# Import core interfaces
from interfaces.core.services import IEventBus
from interfaces.data.services import IPriceProvider
from interfaces.trading.services import IPositionManager, ISnapshotInterpreter, IMasterActionFilter, TradeSignal
from interfaces.utility.services import IFingerprintService

# Import price exceptions for JIT Price Oracle
from data_models.pricing import StalePriceError
from modules.price_fetching.price_repository import PriceUnavailableError

# Import event classes and data types
from core.events import CleanedOCRSnapshotEvent, CleanedOCRSnapshotEventData, OrderRequestEvent, OrderRequestData
from modules.ocr.data_types import CleanedOcrResult
from data_models import OrderSide, TradeActionType
from utils.thread_safe_uuid import get_thread_safe_uuid

# Import configuration
from utils.global_config import GlobalConfig

# Import logging interfaces
from interfaces.logging.services import ICorrelationLogger

# Import performance tracking
from utils.performance_tracker import is_performance_tracking_enabled, create_timestamp_dict, add_timestamp

logger = logging.getLogger(__name__)

class OCRScalpingSignalOrchestratorService:
    """
    OCR Scalping Signal Orchestrator Service that receives cleaned OCR data via direct enqueue,
    interprets trading signals, applies filtering using MasterActionFilterService,
    and publishes OrderRequestEvents when trading opportunities are identified.
    Uses ThreadPoolExecutor pattern with bounded queue for processing cleaned OCR data in worker threads.
    """

    def __init__(self,
                 event_bus: IEventBus,
                 price_provider: IPriceProvider,
                 position_manager: IPositionManager,
                 config_service: GlobalConfig,
                 fingerprint_service: IFingerprintService,
                 interpreter_service: Optional['ISnapshotInterpreter'] = None,
                 action_filter_service: Optional['IMasterActionFilter'] = None,
                 benchmarker: Optional[Any] = None,
                 pipeline_validator: Optional[Any] = None,
                 correlation_logger: Optional[ICorrelationLogger] = None):
        """
        Initialize the OCRScalpingSignalOrchestratorService with dependencies.

        Args:
            event_bus: The event bus for publishing and subscribing to events
            price_provider: Price provider service for market data (still needed for strategy context)
            position_manager: Position manager service for position state
            config_service: Global configuration service
            interpreter_service: Optional snapshot interpreter service for signal generation
            action_filter_service: Optional master action filter service for signal filtering
        """
        # Store dependencies
        self.event_bus = event_bus
        self.price_provider = price_provider
        self.position_manager = position_manager
        self.config_service = config_service
        self.fingerprint_service = fingerprint_service  # Smart fingerprint service
        self.interpreter_service = interpreter_service
        self.action_filter_service = action_filter_service
        self._benchmarker = benchmarker
        self._pipeline_validator = pipeline_validator  # For end-to-end pipeline validation
        # ApplicationCore dependency removed for clean architecture
        self._correlation_logger = correlation_logger
        self.logger = logging.getLogger(__name__)

        # CRITICAL LOGGING: Verify dependency injection status
        self.logger.critical(f"ORCHESTRATOR_DI_STATUS: action_filter_service={'SUCCESS' if self.action_filter_service else 'FAILED/NULL'}")
        self.logger.critical(f"ORCHESTRATOR_DI_STATUS: interpreter_service={'SUCCESS' if self.interpreter_service else 'FAILED/NULL'}")
        if self.action_filter_service:
            self.logger.critical(f"ORCHESTRATOR_DI_STATUS: action_filter_service type={type(self.action_filter_service).__name__}")
        if self.interpreter_service:
            self.logger.critical(f"ORCHESTRATOR_DI_STATUS: interpreter_service type={type(self.interpreter_service).__name__}")

        # Initialize previous OCR state tracking for strategy logic
        self._previous_ocr_states: Dict[str, Dict[str, Any]] = {}  # Now stores CLEANED snapshots
        self._state_lock = threading.RLock()

        # FUZZY'S SMART FINGERPRINT SERVICE: No more dumb cache management!
        # The smart FingerprintService handles all duplicate detection with context awareness
        self.logger.info("OCRScalpingSignalOrchestratorService using smart FingerprintService")

        # Initialize legacy event handler reference to None (no longer used with direct enqueue)
        self._cleaned_ocr_event_handler_for_event_bus = None
        
        # Initialize async IPC publishing infrastructure
        self._ipc_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=1000)
        self._ipc_worker_thread: Optional[threading.Thread] = None
        self._ipc_shutdown_event = threading.Event()
        # Thread creation deferred to start() method

        self.logger.info("OCRScalpingSignalOrchestratorService (Service-Based) initialized. Thread creation deferred to start() method.")
        self.logger.info("Using smart FingerprintService for context-aware duplicate detection.")

        # Initialize OCR task queue and worker pool
        # 🍒 CHERRY-PICK: Bounded queue to prevent memory leaks from unbounded SimpleQueue
        ocr_queue_max_size = getattr(config_service, 'OCR_ORCHESTRATOR_QUEUE_MAX_SIZE', 300)
        self._ocr_tasks_queue = Queue(maxsize=ocr_queue_max_size)  # For processing CleanedOCRSnapshotEvents
        self.logger.info(f"OCRScalpingSignalOrchestratorService: Initialized bounded queue with maxsize={ocr_queue_max_size}")

        # Get max_workers from config or use default
        max_workers = getattr(config_service, 'ocr_signal_generator_workers', 2)
        self._ocr_worker_pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="OCRScalperSignalWorker"
        )
        self._internal_stop_event = threading.Event()

        # Subscribe to events
        self._subscribe_to_events()  # Will now subscribe to CleanedOCRSnapshotEvent

        # Start OCR processing workers
        self._start_ocr_processing_workers()  # Workers will process CleanedOCRSnapshotEvents

        logger.info(f"OCRScalpingSignalOrchestratorService initialized with {max_workers} worker(s).")

    def _extract_snapshots(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> Dict[str, Dict[str, Any]]:
        """Extract snapshots from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'conditioned_snapshots'):
            # CleanedOcrResult
            snapshots = cleaned_data.conditioned_snapshots
            # Validate that conditioned_snapshots is actually a dictionary
            if isinstance(snapshots, dict):
                return snapshots
            else:
                self.logger.error(f"conditioned_snapshots is not a dictionary (type: {type(snapshots)}). Using empty dict fallback.")
                return {}
        elif hasattr(cleaned_data, 'snapshots'):
            # CleanedOCRSnapshotEventData
            snapshots = cleaned_data.snapshots
            # Validate that snapshots is actually a dictionary
            if isinstance(snapshots, dict):
                return snapshots
            else:
                self.logger.error(f"snapshots is not a dictionary (type: {type(snapshots)}). Using empty dict fallback.")
                return {}
        else:
            self.logger.error(f"Unable to extract snapshots from data type: {type(cleaned_data)}")
            return {}

    def _extract_frame_timestamp(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> float:
        """Extract frame timestamp from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'metadata') and isinstance(cleaned_data.metadata, dict):
            # CleanedOcrResult
            return cleaned_data.metadata.get('frame_timestamp', 0.0)
        elif hasattr(cleaned_data, 'frame_timestamp'):
            # CleanedOCRSnapshotEventData
            return cleaned_data.frame_timestamp
        else:
            self.logger.error(f"Unable to extract frame_timestamp from data type: {type(cleaned_data)}")
            return 0.0

    def _extract_validation_id(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> Optional[str]:
        """Extract validation_id from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'metadata') and isinstance(cleaned_data.metadata, dict):
            # CleanedOcrResult
            return cleaned_data.metadata.get('validation_id', None)
        elif hasattr(cleaned_data, 'validation_id'):
            # CleanedOCRSnapshotEventData
            return cleaned_data.validation_id
        else:
            return None

    def _extract_origin_correlation_id(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> Optional[str]:
        """Extract origin_correlation_id from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'metadata') and isinstance(cleaned_data.metadata, dict):
            # CleanedOcrResult
            return cleaned_data.metadata.get('origin_correlation_id', None)
        elif hasattr(cleaned_data, 'origin_correlation_id'):
            # CleanedOCRSnapshotEventData
            return cleaned_data.origin_correlation_id
        else:
            return None

    def _extract_original_ocr_event_id(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> Optional[str]:
        """Extract original_ocr_event_id from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'metadata') and isinstance(cleaned_data.metadata, dict):
            # CleanedOcrResult  
            return cleaned_data.metadata.get('original_ocr_event_id', None)
        elif hasattr(cleaned_data, 'original_ocr_event_id'):
            # CleanedOCRSnapshotEventData
            return cleaned_data.original_ocr_event_id
        else:
            return None

    def _extract_raw_ocr_confidence(self, cleaned_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData]) -> Optional[float]:
        """Extract raw_ocr_confidence from either CleanedOcrResult or CleanedOCRSnapshotEventData."""
        if hasattr(cleaned_data, 'metadata') and isinstance(cleaned_data.metadata, dict):
            # CleanedOcrResult
            return cleaned_data.metadata.get('raw_ocr_confidence', None)
        elif hasattr(cleaned_data, 'raw_ocr_confidence'):
            # CleanedOCRSnapshotEventData
            return cleaned_data.raw_ocr_confidence
        else:
            return None

    def start(self):
        """Start the OCRScalpingSignalOrchestratorService and its worker threads."""
        logger.info("Starting OCRScalpingSignalOrchestratorService...")
        
        # Start IPC worker thread
        self._start_ipc_worker()
        
        logger.info("OCRScalpingSignalOrchestratorService started successfully.")

    def _is_regular_trading_hours(self) -> bool:
        """
        Check if current time is within Regular Trading Hours (RTH).
        RTH is 9:30 AM - 4:00 PM ET, Monday-Friday.

        Returns:
            bool: True if currently in RTH, False otherwise
        """
        try:
            # Get current time in ET
            et_tz = pytz.timezone('US/Eastern')
            now_et = datetime.now(et_tz)

            # Check if it's a weekday (Monday=0, Sunday=6)
            if now_et.weekday() >= 5:  # Saturday or Sunday
                return False

            # Check if time is between 9:30 AM and 4:00 PM ET
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)

            is_rth = market_open <= now_et <= market_close
            self.logger.debug(f"RTH Check: Current ET time: {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}, RTH: {is_rth}")
            return is_rth

        except Exception as e:
            self.logger.error(f"Error checking RTH: {e}", exc_info=True)
            # Default to False (extended hours) for safety
            return False

    def _subscribe_to_events(self):
        """Subscribe to relevant events from the event_bus."""
        # Direct enqueue path only - no EventBus subscription for OCR data
        logger.info("OCRScalpingSignalOrchestratorService: Using direct enqueue for cleaned OCR data. No EventBus subscriptions.")

    def enqueue_cleaned_snapshot_data(self, cleaned_event_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData], cleaned_event_id: Optional[str] = None):
        """Direct input method for cleaned OCR data, bypassing event bus."""
        print(f"DEBUG_DOWNSTREAM: Orchestrator received cleaned data for enqueue")
        
        if not self._internal_stop_event.is_set(): # Check if service is running
            snapshots = self._extract_snapshots(cleaned_event_data)
            frame_timestamp = self._extract_frame_timestamp(cleaned_event_data)
            self.logger.info(f"SIGNAL_DEBUG: Enqueuing cleaned OCR data directly. Frame TS: {frame_timestamp}. Snapshots: {list(snapshots.keys())}")

            # CAUSATION ID REFINEMENT: Package both the data and the event's unique ID for precise causation tracking
            task_item = {
                'event_data': cleaned_event_data,
                'cleaned_event_id': cleaned_event_id  # May be None for direct enqueue, will generate fallback
            }

            # 🍒 CHERRY-PICK: Bounded queue with timeout and QueueFull handling
            try:
                self._ocr_tasks_queue.put(task_item, block=True, timeout=0.1)
                q_size = self._ocr_tasks_queue.qsize()
                self.logger.info(f"ORCH_QUEUE_ADD: Enqueued item for FrameTS {frame_timestamp}. New qsize: {q_size}.")

                # Use configurable threshold for warnings
                orch_q_warn_thresh = getattr(self.config_service, 'ORCH_QUEUE_WARN_THRESHOLD', 50)
                if q_size > orch_q_warn_thresh:
                    self.logger.warning(f"ORCH_QUEUE_HIGH: Orchestrator queue size is {q_size} (Threshold: {orch_q_warn_thresh}).")
            except QueueFull:
                # Critical error - workers are stuck/dead or too slow
                max_size = getattr(self.config_service, 'OCR_ORCHESTRATOR_QUEUE_MAX_SIZE', 300)
                self.logger.critical(f"ORCH_QUEUE_BOUNDED_AND_FULL: Workers STUCK/DEAD or too slow. Producer (Conditioner) now blocking/dropping. MaxSize: {max_size}. Event FrameTS: {frame_timestamp} DROPPED.")
            except Exception as e:
                self.logger.error(f"ORCH_QUEUE_PUT_ERROR: Unexpected error enqueueing task: {e}", exc_info=True)
        else:
            self.logger.warning("OCRScalpingSignalOrchestratorService: Attempted to enqueue cleaned OCR data while stopping. Discarding.")

    def _start_ocr_processing_workers(self):
        """Start the OCR processing worker threads."""
        for _ in range(self._ocr_worker_pool._max_workers):  # Get actual number of workers
            self._ocr_worker_pool.submit(self._process_ocr_task_queue)
        logger.info(f"OCRScalpingSignalOrchestratorService started {self._ocr_worker_pool._max_workers} OCR processing worker(s).")

    def _process_ocr_task_queue(self):
        """Process OCR tasks from the queue in a worker thread."""
        logger.info(f"OCR Signal Worker Thread ({threading.current_thread().name}) started for CLEANED data.")
        while not self._internal_stop_event.is_set():
            try:
                # Periodic queue size logging
                current_q_time = time.perf_counter_ns() / 1_000_000_000
                if not hasattr(self, '_last_q_log_time_orch') or \
                   (current_q_time - getattr(self, '_last_q_log_time_orch', 0) > 5.0):  # Log every 5 seconds
                    if hasattr(self, '_ocr_tasks_queue'):  # Check if queue exists
                        q_size = self._ocr_tasks_queue.qsize()
                        self.logger.info(f"OCRScalpingSignalOrchestratorService: OCR tasks queue size = {q_size}")
                        # 🍒 CHERRY-PICK: Now using bounded Queue with configurable maxsize
                        # Use configurable warning threshold for queue monitoring
                        WARN_THRESHOLD_ORCH_QUEUE = getattr(self.config_service, 'ORCH_QUEUE_WARN_THRESHOLD', 50)
                        if q_size > WARN_THRESHOLD_ORCH_QUEUE:
                             self.logger.warning(f"OCRScalpingSignalOrchestratorService: OCR tasks queue size HIGH: {q_size} (Threshold: {WARN_THRESHOLD_ORCH_QUEUE})")
                    setattr(self, '_last_q_log_time_orch', current_q_time)

                task_item = self._ocr_tasks_queue.get(timeout=0.2)

                # CAUSATION ID REFINEMENT: Handle both old and new task formats for backward compatibility
                if isinstance(task_item, dict) and 'event_data' in task_item:
                    # New format with causation ID tracking
                    cleaned_event_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData] = task_item['event_data']
                    cleaned_event_id: Optional[str] = task_item.get('cleaned_event_id')
                    frame_timestamp = self._extract_frame_timestamp(cleaned_event_data)
                    logger.info(f"SIGNAL_DEBUG: Worker dequeued enhanced task item. Frame TS: {frame_timestamp}, Event ID: {cleaned_event_id}")
                else:
                    # Legacy format - direct CleanedOcrResult or CleanedOCRSnapshotEventData
                    cleaned_event_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData] = task_item
                    cleaned_event_id: Optional[str] = None
                    frame_timestamp = self._extract_frame_timestamp(cleaned_event_data)
                    logger.info(f"SIGNAL_DEBUG: Worker dequeued legacy cleaned data. Frame TS: {frame_timestamp}")

                snapshots = self._extract_snapshots(cleaned_event_data)
                logger.info(f"SIGNAL_DEBUG: Worker processing snapshots: {list(snapshots.keys())}")

                # Check for validation_id and register orchestrator signal logic start
                validation_id = self._extract_validation_id(cleaned_event_data)
                self.logger.debug(f"SIGNAL_DEBUG: Worker received validation_id: {validation_id} for frame {frame_timestamp}")
                if validation_id and self._pipeline_validator:
                    try:
                        self._pipeline_validator.register_stage_completion(
                            validation_id,
                            'orchestrator_signal_logic_started',
                            stage_data={'frame_timestamp': frame_timestamp, 'symbols_count': len(snapshots)}
                        )
                    except Exception as e:
                        self.logger.warning(f"Error registering orchestrator_signal_logic_started for validation_id {validation_id}: {e}")

                # Pass the CleanedOCRSnapshotEventData and cleaned_event_id to the main interpretation logic
                # 🍒 CHERRY-PICK: Enhanced exception handling for worker thread stability
                try:
                    import asyncio
                    # Create a new event loop for this worker thread to avoid conflicts
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(self._interpret_and_generate_signal(cleaned_event_data, cleaned_event_id))
                    finally:
                        loop.close()
                except AttributeError as attr_err:
                    # Specific handling for obsolete code path issues
                    worker_name = threading.current_thread().name
                    self.logger.error(f"ORCH_WORKER_INNER_EXCEPTION: {worker_name} - AttributeError in interpretation (FrameTS: {frame_timestamp}): {attr_err}", exc_info=True)
                except Exception as inner_e:
                    # Generic exception handling for any interpretation failures
                    worker_name = threading.current_thread().name
                    self.logger.error(f"ORCH_WORKER_INNER_EXCEPTION: {worker_name} - Error in interpretation (FrameTS: {frame_timestamp}): {inner_e}", exc_info=True)
                finally:
                    # Ensure proper queue lifecycle management for bounded Queue
                    if hasattr(self._ocr_tasks_queue, 'task_done'):
                        try:
                            self._ocr_tasks_queue.task_done()
                        except Exception as task_done_err:
                            self.logger.warning(f"Error calling task_done(): {task_done_err}")

            except QueueEmpty:
                continue
            except Exception as e:
                # 🍒 CHERRY-PICK: Enhanced outer exception logging
                worker_name = threading.current_thread().name
                self.logger.error(f"ORCH_WORKER_OUTER_EXCEPTION: {worker_name} - Error in queue processing or overall loop: {e}", exc_info=True)
        logger.info(f"OCR Signal Worker Thread ({threading.current_thread().name}) stopped.")

    async def _interpret_and_generate_signal(self, cleaned_event_data: Union[CleanedOcrResult, CleanedOCRSnapshotEventData], cleaned_event_id: Optional[str] = None):
        """
        EFFICIENT ORDER: Check duplicates FIRST before any expensive processing.
        This follows the correct data flow as specified:
        1. Create fingerprint from incoming data (fast)
        2. Check if duplicate (fast lookup)
        3. If duplicate, return immediately
        4. Only if NOT duplicate, do expensive processing
        5. Update fingerprint cache after successful publish
        """
        # T7 MILESTONE: Orchestration Start
        t7_orchestration_start_ns = time.perf_counter_ns()
        
        # Extract minimal metadata needed for fingerprinting
        frame_timestamp = self._extract_frame_timestamp(cleaned_event_data)
        snapshots = self._extract_snapshots(cleaned_event_data)
        
        if not snapshots:
            logger.info("SIGNAL_DEBUG: No snapshots in CleanedOCRSnapshotEvent. Nothing to process.")
            return

        # STEP 1: Create potential fingerprints for ALL symbols FIRST (very fast)
        # This is done BEFORE any expensive processing
        potential_fingerprints = {}
        for symbol, snapshot in snapshots.items():
            # Extract minimal data needed for fingerprint
            action_str = snapshot.get('strategy_hint', '')
            quantity_str = snapshot.get('quantity', '0')
            cost_str = snapshot.get('cost_basis', '0')
            realized_pnl_str = snapshot.get('realized_pnl', '0')
            
            # Create fingerprint tuple from snapshot data
            try:
                quantity_float = float(quantity_str) if quantity_str else 0.0
                # Ensure cost_str is a string before calling replace
                if isinstance(cost_str, (int, float)):
                    cost_float = float(cost_str)
                else:
                    cost_float = float(str(cost_str).replace(',', '')) if cost_str else 0.0
                # Parse realized PnL
                if isinstance(realized_pnl_str, (int, float)):
                    realized_pnl_float = float(realized_pnl_str)
                else:
                    realized_pnl_float = float(str(realized_pnl_str).replace(',', '')) if realized_pnl_str else 0.0
            except (ValueError, TypeError):
                quantity_float = 0.0
                cost_float = 0.0
                realized_pnl_float = 0.0
                
            fingerprint_tuple = (
                symbol.upper(),
                action_str,
                round(quantity_float, 2),  # Round to avoid float precision issues
                round(cost_float, 2),
                round(realized_pnl_float, 2)  # Include realized PnL for REDUCE detection
            )
            potential_fingerprints[symbol] = fingerprint_tuple
            
        # STEP 2: Check for duplicates FIRST (before any expensive work)
        symbols_to_process = []
        for symbol, fingerprint in potential_fingerprints.items():
            context = {
                'symbol': symbol,
                'market_volatility': 'NORMAL',  # TODO: Add real market volatility
                'market_hours': 'REGULAR'       # TODO: Add real market hours
            }
            
            if self.fingerprint_service.is_duplicate(fingerprint, context):
                # It's a duplicate - skip it entirely
                self.logger.info(f"[{symbol}] DUPLICATE DETECTED (fingerprint: {fingerprint}). Skipping ALL processing.")
                # Could publish a duplicate suppression event here if needed
            else:
                # Not a duplicate - add to processing list
                symbols_to_process.append(symbol)
                
        # STEP 3: If ALL symbols are duplicates, exit early
        if not symbols_to_process:
            logger.info("SIGNAL_DEBUG: All symbols were duplicates. No processing needed.")
            return
            
        # STEP 4: Only NOW do expensive processing for non-duplicate symbols
        logger.info(f"SIGNAL_DEBUG: Processing {len(symbols_to_process)} non-duplicate symbols out of {len(snapshots)} total")
        
        # Extract remaining metadata needed for processing
        origin_correlation_id = self._extract_origin_correlation_id(cleaned_event_data)
        original_ocr_event_id = self._extract_original_ocr_event_id(cleaned_event_data)
        raw_ocr_confidence = self._extract_raw_ocr_confidence(cleaned_event_data)
        validation_id = self._extract_validation_id(cleaned_event_data)
        
        # Performance tracking
        perf_timestamps = create_timestamp_dict() if is_performance_tracking_enabled() else None
        if perf_timestamps:
            add_timestamp(perf_timestamps, 'ocr_signal_generator.interpret_cleaned_start')
            
        cleaned_event_data_arrival_time = time.perf_counter_ns() / 1_000_000_000
        
        try:
            with self._state_lock:
                # Process only non-duplicate symbols
                for symbol in symbols_to_process:
                    current_clean_snapshot = snapshots[symbol]
                    fingerprint = potential_fingerprints[symbol]
                    
                    try:
                        logger.info(f"SIGNAL_DEBUG: Processing non-duplicate symbol {symbol}")
                        
                        # Use cleaned_event_id for causation tracking
                        causation_id_for_order_request = cleaned_event_id or original_ocr_event_id
                        
                        # Process the symbol - this does the expensive work
                        signal_published = await self._process_symbol_state_change(
                            symbol,
                            current_clean_snapshot,
                            frame_timestamp,
                            raw_ocr_confidence,
                            cleaned_event_data_arrival_time,
                            validation_id,
                            origin_correlation_id,
                            causation_id_for_order_request
                        )
                        
                        # STEP 5: Update fingerprint cache ONLY if signal was published
                        if signal_published:
                            signal_data = {
                                'symbol': symbol,
                                'action': current_clean_snapshot.get('strategy_hint', ''),
                                'quantity': current_clean_snapshot.get('quantity', '0'),
                                'cost_basis': current_clean_snapshot.get('cost_basis', '0'),
                                'realized_pnl': current_clean_snapshot.get('realized_pnl', '0'),
                                'timestamp': frame_timestamp,
                                'correlation_id': origin_correlation_id
                            }
                            self.fingerprint_service.update(fingerprint, signal_data)
                            logger.info(f"[{symbol}] Updated fingerprint cache after successful signal")
                            
                    except Exception as e:
                        logger.error(f"Error processing symbol {symbol}: {e}", exc_info=True)
                
                # Update state ONLY for processed symbols
                for symbol in symbols_to_process:
                    if symbol in snapshots:
                        old_state = self._previous_ocr_states.get(symbol, {})
                        self._previous_ocr_states[symbol] = snapshots[symbol].copy()
                        self.logger.debug(f"[{symbol}] Updated state: OLD={old_state} -> NEW={snapshots[symbol]}")
                        
            if perf_timestamps:
                add_timestamp(perf_timestamps, 'ocr_signal_generator.strategy_end')
                
        except Exception as e:
            logger.error(f"Error in _interpret_and_generate_signal: {e}", exc_info=True)
        finally:
            if perf_timestamps:
                add_timestamp(perf_timestamps, 'ocr_signal_generator.interpret_cleaned_end')

    # --- REMOVED: _parse_ocr_text_to_snapshots ---
    # This method has been moved to PythonOCRDataConditioner
    # The signal generator now receives pre-processed clean snapshots

    # --- REMOVED: _process_single_line ---
    # This method has been moved to PythonOCRDataConditioner
    # The signal generator now receives pre-processed clean snapshots

    async def _process_symbol_state_change(self,
                                     symbol: str,
                                     current_clean_snapshot: Dict[str, Any],
                                     ocr_frame_timestamp: float,
                                     raw_ocr_confidence: Optional[float] = None,
                                     cleaned_event_data_arrival_time: Optional[float] = None,
                                     validation_id: Optional[str] = None,
                                     origin_correlation_id_from_cleaned_data: Optional[str] = None,
                                     cleaned_snapshot_event_id_for_causation: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        Process state changes for a symbol using SnapshotInterpreterService and MasterActionFilterService.
        This method now orchestrates calls to dedicated services for interpretation and filtering.

        Args:
            symbol: The stock symbol
            current_clean_snapshot: Current cleaned OCR snapshot for the symbol
            ocr_frame_timestamp: Original frame timestamp from OCR
            raw_ocr_confidence: Optional OCR confidence score
            cleaned_event_data_arrival_time: Optional arrival time for benchmarking
            validation_id: Optional validation ID for pipeline tracking
            origin_correlation_id_from_cleaned_data: Master correlation ID
            cleaned_snapshot_event_id_for_causation: Causation ID for order tracking
            
        Returns:
            Tuple[bool, Optional[str]]: (signal_published, order_request_id)
        """
        symbol_upper = symbol.upper()
        
        # T7 MILESTONE: Orchestration Start (captured inside method)
        t7_orchestration_start_ns = time.perf_counter_ns()
        t8_interpretation_end_ns = None
        t10_decision_ready_ns = None
        t11_order_request_ready_ns = None
        t12_order_request_published_ns = None
        
        # MAF metadata will be extracted from filtered signal
        maf_decision_metadata: Optional[Dict[str, Any]] = {}
        
        # Track whether a signal was published
        signal_published = False

        # Log all input parameters at the start
        logger.info(f"SIGNAL_DEBUG: _process_symbol_state_change ENTRY for {symbol_upper}")
        logger.info(f"SIGNAL_DEBUG: Input parameters - symbol: {symbol_upper}, ocr_frame_timestamp: {ocr_frame_timestamp}, raw_ocr_confidence: {raw_ocr_confidence}")
        logger.info(f"SIGNAL_DEBUG: current_clean_snapshot: {current_clean_snapshot}")
        logger.info(f"SIGNAL_DEBUG: T7 orchestration start: {t7_orchestration_start_ns}")
        
        # Helper function to log signal decision via ICorrelationLogger
        def log_signal_decision(decision_type: str, signal_action: Optional[str] = None, 
                               signal_quantity: Optional[float] = None, signal_reason: Optional[str] = None,
                               failure_context: Optional[Dict[str, Any]] = None, 
                               order_request_data: Optional[OrderRequestData] = None):
            """Log signal decision via ICorrelationLogger with comprehensive metadata."""
            try:
                # Build comprehensive signal decision data
                signal_data = {
                    "decision_type": decision_type,
                    "symbol": symbol_upper,
                    "timestamp": time.time(),
                    
                    # Causation chain metadata
                    "master_correlation_id": origin_correlation_id_from_cleaned_data,
                    "causation_id": cleaned_snapshot_event_id_for_causation,
                    "origin_ocr_event_id": cleaned_snapshot_event_id_for_causation,
                    
                    # Signal processing context
                    "signal_action": signal_action,
                    "signal_quantity": signal_quantity,
                    "signal_reason": signal_reason,
                    
                    # Timing and latency data (T7-T12 milestones)
                    "t7_orchestration_start_ns": t7_orchestration_start_ns,
                    "t8_interpretation_end_ns": t8_interpretation_end_ns,
                    "t9_filtering_end_ns": maf_decision_metadata.get('t9_maf_filtering_start_ns') if maf_decision_metadata else None,
                    "t10_decision_ready_ns": maf_decision_metadata.get('t10_maf_decision_ready_ns') if maf_decision_metadata else None,
                    
                    # OCR processing context
                    "ocr_frame_timestamp": ocr_frame_timestamp,
                    "ocr_confidence": raw_ocr_confidence,
                    
                    # Validation and pipeline tracking
                    "validation_id": validation_id,
                    "fingerprint_data": str(getattr(self, '_last_fingerprint', None)) if hasattr(self, '_last_fingerprint') else None,
                    
                    # Additional context for failures
                    "failure_context": failure_context,
                    
                    # Order request details (if signal generated)
                    "order_request_id": order_request_data.request_id if order_request_data else None,
                    "order_type": str(order_request_data.order_type) if order_request_data else None,
                    "order_side": str(order_request_data.side) if order_request_data else None,
                    "limit_price": order_request_data.limit_price if order_request_data else None,
                    
                    # Market context (filled in when available)
                    "current_price": order_request_data.current_price if order_request_data else None,
                    "current_position": order_request_data.current_position if order_request_data else None,
                    "triggering_cost_basis": current_clean_snapshot.get('cost_basis', 0.0)
                }
                
                # Log via ICorrelationLogger instead of publishing to event bus
                if self._correlation_logger:
                    self._correlation_logger.log_event(
                        source_component="OCRScalpingSignalOrchestrator",
                        event_name=f"SignalDecision_{decision_type}",
                        payload=signal_data,
                        stream_override=None
                    )
                
                logger.info(f"SIGNAL_DECISION: Logged {decision_type} for {symbol_upper} "
                          f"(CorrID: {origin_correlation_id_from_cleaned_data}, Action: {signal_action}, "
                          f"T7-T10: {t7_orchestration_start_ns}-{t10_decision_ready_ns})")
                          
            except Exception as e:
                logger.error(f"Error logging signal decision for {symbol_upper}: {e}", exc_info=True)

        # Get previous CLEAN state for interpreter service
        with self._state_lock:
            previous_state = self._previous_ocr_states.get(symbol_upper, {})
            logger.info(f"SIGNAL_DEBUG: Retrieved previous_state for {symbol_upper}: {previous_state}")

        # Early exit if no change detected - avoid unnecessary processing
        if previous_state == current_clean_snapshot:
            # T10 MILESTONE: Decision ready (suppression decision)
            t10_decision_ready_ns = time.perf_counter_ns()
            
            logger.info(f"[{symbol_upper}] No change detected in OCR data - skipping signal generation")
            
            # Log signal decision for suppression
            log_signal_decision(
                decision_type="SIGNAL_SUPPRESSED",
                signal_reason="NO_CHANGE_DETECTED"
            )
            
            # Publish signal suppression for IntelliSense visibility (legacy method)
            self._publish_signal_suppression_to_redis(
                symbol_upper, 
                "NO_CHANGE_DETECTED", 
                origin_correlation_id_from_cleaned_data,
                cleaned_snapshot_event_id_for_causation
            )
            return  # Exit before SnapshotInterpreterService, filtering, and event bus publishing

        # Extract values needed for service calls
        current_cost = current_clean_snapshot.get('cost_basis', 0.0)
        current_realized_pnl = current_clean_snapshot.get('realized_pnl', 0.0)
        previous_cost = previous_state.get('cost_basis', 0.0)
        previous_realized_pnl = previous_state.get('realized_pnl', 0.0)

        # Get current position from PositionManager
        pos_data = self.position_manager.get_position(symbol_upper) or {}
        current_shares = pos_data.get('shares', 0.0)

        # --- AGENT: START ADDED LOGGING ---
        self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: Querying PositionManager for current state.")
        self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: PositionManager instance: {type(self.position_manager).__name__} at {id(self.position_manager)}")
        self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: PositionManager.get_position() returned: {pos_data}")
        self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: Extracted current_PM_shares = {current_shares:.1f}")

        # Debug: Show all positions in PositionManager
        if hasattr(self.position_manager, 'debug_all_positions'):
            self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: Calling PositionManager.debug_all_positions()...")
            self.position_manager.debug_all_positions()
        else:
            self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: PositionManager does not have debug_all_positions method")

        # Debug: Show all positions via get_all_positions_snapshot
        try:
            all_positions = self.position_manager.get_all_positions_snapshot()
            self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: All positions in PM: {all_positions}")
        except Exception as e:
            self.logger.critical(f"OCRSOS_PM_CHECK [{symbol_upper}]: Error getting all positions: {e}")
        # --- AGENT: END ADDED LOGGING ---

        self.logger.info(f"[{symbol_upper}] SERVICE-BASED PROCESSING: "
                        f"prev_cost={previous_cost:.4f}, curr_cost={current_cost:.4f}, "
                        f"prev_rPnL={previous_realized_pnl:.2f}, curr_rPnL={current_realized_pnl:.2f}, "
                        f"current_PM_shares={current_shares:.1f}")

        order_request_data: Optional[OrderRequestData] = None
        raw_signal: Optional[TradeSignal] = None
        filtered_signal: Optional[TradeSignal] = None

        try:
            # --- STEP 1: Use SnapshotInterpreterService for Signal Interpretation ---
            if self.interpreter_service:
                logger.info(f"SIGNAL_DEBUG: Calling SnapshotInterpreterService for {symbol_upper}")
                raw_signal = self.interpreter_service.interpret_single_snapshot(
                    symbol=symbol_upper,
                    new_state=current_clean_snapshot,
                    pos_data=pos_data,
                    old_ocr_cost=previous_cost,
                    old_ocr_realized=previous_realized_pnl,
                    correlation_id=origin_correlation_id_from_cleaned_data
                )
                
                # T8 MILESTONE: Interpretation End
                t8_interpretation_end_ns = time.perf_counter_ns()
                logger.info(f"SIGNAL_DEBUG: SnapshotInterpreterService returned: {raw_signal.action.name if raw_signal else None}")
                logger.info(f"SIGNAL_DEBUG: T8 interpretation end: {t8_interpretation_end_ns}")
            else:
                # T8 MILESTONE: Interpretation End (failure case)
                t8_interpretation_end_ns = time.perf_counter_ns()
                # T10 MILESTONE: Decision ready (validation failure)
                t10_decision_ready_ns = time.perf_counter_ns()
                
                self.logger.warning(f"[{symbol_upper}] No SnapshotInterpreterService available - skipping interpretation")
                
                # Create and publish SignalDecisionEvent for validation failure
                log_signal_decision(
                    decision_type="VALIDATION_FAILURE",
                    signal_reason="NO_SNAPSHOT_INTERPRETER_SERVICE",
                    failure_context={"validation_stage": "INTERPRETER_SERVICE_CHECK"}
                )
                
                # Publish validation failure event to Redis (legacy method)
                self._publish_signal_validation_failure_to_redis(
                    symbol=symbol_upper,
                    failure_reason="NO_SNAPSHOT_INTERPRETER_SERVICE",
                    master_correlation_id=origin_correlation_id_from_cleaned_data,
                    causation_id=cleaned_snapshot_event_id_for_causation
                )
                return

            # --- STEP 2: Use MasterActionFilterService for Signal Filtering ---
            # CRITICAL LOGGING: Debug the exact conditional logic
            self.logger.critical(f"SIGNAL_FILTER_DECISION: symbol={symbol_upper}, raw_signal={'EXISTS' if raw_signal else 'NULL'}, action_filter_service={'EXISTS' if self.action_filter_service else 'NULL'}")
            if raw_signal and self.action_filter_service:
                self.logger.critical(f"SIGNAL_FILTER_PATH: Using MasterActionFilterService for {symbol_upper}")
                logger.info(f"SIGNAL_DEBUG: Calling MasterActionFilterService for {symbol_upper}")
                filtered_signal = self.action_filter_service.filter_signal(
                    signal=raw_signal,
                    current_ocr_cost=current_cost,
                    current_ocr_rPnL=current_realized_pnl,
                    correlation_id=origin_correlation_id_from_cleaned_data,
                    causation_id=cleaned_snapshot_event_id_for_causation
                )
                
                # Extract MAF decision metadata from filtered signal (T9-T10 timing captured by MAF service)
                if filtered_signal and hasattr(filtered_signal, 'metadata') and filtered_signal.metadata:
                    maf_decision_metadata = filtered_signal.metadata
                    logger.info(f"SIGNAL_DEBUG: Extracted MAF metadata: {maf_decision_metadata}")
                else:
                    maf_decision_metadata = {}
                    logger.info(f"SIGNAL_DEBUG: No MAF metadata available")
                
                logger.info(f"SIGNAL_DEBUG: MasterActionFilterService returned: {filtered_signal.action.name if filtered_signal else 'SUPPRESSED'}")
            elif raw_signal:
                # No action filter service - pass through raw signal
                self.logger.critical(f"SIGNAL_FILTER_PATH: BYPASSING FILTER for {symbol_upper} - action_filter_service is NULL!")
                filtered_signal = raw_signal
                maf_decision_metadata = {}  # No MAF metadata in pass-through case
                
                self.logger.warning(f"[{symbol_upper}] No MasterActionFilterService available - using raw signal")
                
                # Create and publish SignalDecisionEvent for validation failure
                log_signal_decision(
                    decision_type="VALIDATION_FAILURE",
                    signal_reason="NO_MASTER_ACTION_FILTER_SERVICE",
                    signal_action=raw_signal.action.name if raw_signal else None,
                    failure_context={"validation_stage": "FILTER_SERVICE_CHECK"}
                )
                
                # Publish validation failure event to Redis (legacy method)
                self._publish_signal_validation_failure_to_redis(
                    symbol=symbol_upper,
                    failure_reason="NO_MASTER_ACTION_FILTER_SERVICE",
                    master_correlation_id=origin_correlation_id_from_cleaned_data,
                    causation_id=cleaned_snapshot_event_id_for_causation
                )
            else:
                # No raw signal to filter
                self.logger.critical(f"SIGNAL_FILTER_PATH: NO RAW SIGNAL generated for {symbol_upper} - SnapshotInterpreterService returned None")
                filtered_signal = None
                maf_decision_metadata = {}  # No MAF metadata when no signal
                logger.info(f"SIGNAL_DEBUG: No raw signal to filter for {symbol_upper}")

            # --- STEP 3: Quantity Calculation and Order Generation ---
            if filtered_signal:
                logger.info(f"SIGNAL_DEBUG: Processing filtered signal: {filtered_signal.action.name} for {symbol_upper}")
                self.logger.info(f"[{symbol_upper}] Processing filtered signal: {filtered_signal.action.name}")

                # Calculate quantity based on signal type
                quantity = self._calculate_quantity_for_signal(filtered_signal, pos_data, current_cost)

                if quantity > 0:
                    # Determine side based on action
                    side = self._get_side_for_action(filtered_signal.action)

                    # Intelligent order type and TIF determination based on RTH
                    is_rth = self._is_regular_trading_hours()
                    final_order_type = "MKT"
                    final_tif = "DAY"
                    calculated_limit_price = None

                    if not is_rth:  # Premarket or AfterHours
                        final_order_type = "LMT"
                        self.logger.info(f"[{symbol_upper}] Extended hours detected - using LMT order type")

                        # Calculate an initial aggressive limit price for extended hours
                        side_for_price = "BUY" if filtered_signal.action in [TradeActionType.OPEN_LONG, TradeActionType.ADD_LONG] else "SELL"
                        
                        # NEW: Use JIT Price Oracle blocking method for critical signal generation
                        try:
                            price_data = self.price_provider.get_price_blocking(symbol_upper, timeout_sec=2.0)
                            ref_price = price_data.price
                        except (StalePriceError, PriceUnavailableError) as e:
                            self.logger.critical(f"Could not generate signal for {symbol_upper}. No reliable price available: {e}")
                            return # Skip this snapshot - cannot generate signal without valid price

                        if ref_price and ref_price > 0.01:
                            # Use config-based offset logic for extended hours
                            offset_config_key_dollars = 'default_lmt_offset_dollars_ext_hours'
                            offset_config_key_cents = 'default_lmt_offset_cents_ext_hours'
                            default_offset_dollars = float(getattr(self.config_service, offset_config_key_dollars, 0.10))
                            default_offset_cents = float(getattr(self.config_service, offset_config_key_cents, 0.03))

                            offset = default_offset_dollars if ref_price >= 2.0 else default_offset_cents
                            if side_for_price == "BUY":
                                calculated_limit_price = round(ref_price + offset, 4)
                            else:  # SELL
                                calculated_limit_price = round(max(0.01, ref_price - offset), 4)
                            self.logger.info(f"[{symbol_upper}] Extended hours LMT price calculated: {calculated_limit_price} from ref {ref_price}")
                        else:
                            # T10 MILESTONE: Decision ready (generation failure - extended hours price calculation)
                            t10_decision_ready_ns = time.perf_counter_ns()
                            
                            self.logger.error(f"[{symbol_upper}] Cannot calculate LMT price for extended hours due to invalid ref_price: {ref_price}. Signal for {filtered_signal.action.name} will be skipped.")
                            
                            # Create and publish SignalDecisionEvent for generation failure
                            failure_context = {
                                "signal_action": filtered_signal.action.name,
                                "trading_session": "EXTENDED_HOURS",
                                "ref_price": ref_price,
                                "price_calculation_failure": "Cannot calculate limit price for extended hours",
                                "ocr_frame_timestamp": ocr_frame_timestamp,
                                "validation_stage": "EXTENDED_HOURS_PRICE_CALCULATION"
                            }
                            log_signal_decision(
                                decision_type="GENERATION_FAILURE",
                                signal_action=filtered_signal.action.name,
                                signal_reason="EXTENDED_HOURS_PRICE_CALCULATION_FAILED",
                                failure_context=failure_context
                            )
                            
                            # Publish signal generation failure event for extended hours limit price calculation failure (legacy method)
                            self._publish_signal_generation_failure_to_redis(
                                symbol=symbol_upper,
                                failure_reason="EXTENDED_HOURS_PRICE_CALCULATION_FAILED",
                                failure_context=failure_context,
                                master_correlation_id=origin_correlation_id_from_cleaned_data,
                                causation_id=cleaned_snapshot_event_id_for_causation
                            )
                            
                            return False  # Skip publishing OrderRequestEvent
                    else:
                        self.logger.info(f"[{symbol_upper}] Regular trading hours - using MKT order type")

                    # Create OrderRequestData with intelligent order type/TIF
                    # Construct the specific event type string for OCR Scalping
                    specific_event_type_str = f"OCR_SCALPING_{filtered_signal.action.name}"

                    # Validate that the generated string is a valid OrderEventType value
                    try:
                        from data_models import OrderEventType
                        OrderEventType(specific_event_type_str)  # This will raise ValueError if not valid
                        self.logger.debug(f"[{symbol_upper}] Generated valid specific_event_type_str: '{specific_event_type_str}'")
                    except ValueError:
                        self.logger.error(f"[{symbol_upper}] Generated specific_event_type_str '{specific_event_type_str}' is NOT a valid OrderEventType! Check enum definition.")
                        # Continue anyway - OrderRepository will handle the invalid enum and default to UNKNOWN

                    # --- START MODIFICATION ---
                    if origin_correlation_id_from_cleaned_data is None:
                        self.logger.error(f"[{symbol_upper}] CRITICAL: origin_correlation_id_from_cleaned_data is None when trying to create OrderRequestData. End-to-end tracing will be broken. Generating a new fallback correlation_id.")
                        # Fallback to default factory in OrderRequestData if master is somehow lost
                        order_request_data = OrderRequestData(
                            symbol=symbol_upper,
                            side=side,
                            quantity=quantity,
                            order_type=final_order_type,
                            limit_price=calculated_limit_price,  # Will be None if MKT
                            time_in_force=final_tif,
                            source_strategy=specific_event_type_str,
                            related_ocr_event_id=cleaned_snapshot_event_id_for_causation,  # Link to original OCR event (causation fix)
                            ocr_frame_timestamp=ocr_frame_timestamp,
                            triggering_cost_basis=current_cost,  # Include triggering cost basis for fingerprinting
                            # client_order_id will be set to request_id below for order tracking
                            # correlation_id will use its default_factory since origin_correlation_id is None
                        )
                    else:
                        order_request_data = OrderRequestData(
                            symbol=symbol_upper,
                            side=side,
                            quantity=quantity,
                            order_type=final_order_type,
                            limit_price=calculated_limit_price,  # Will be None if MKT
                            time_in_force=final_tif,
                            source_strategy=specific_event_type_str,
                            related_ocr_event_id=cleaned_snapshot_event_id_for_causation,  # Link to original OCR event (causation fix)
                            ocr_frame_timestamp=ocr_frame_timestamp,
                            triggering_cost_basis=current_cost,  # Include triggering cost basis for fingerprinting
                            correlation_id=origin_correlation_id_from_cleaned_data  # Pass the master correlation ID directly
                            # client_order_id will be set to request_id below for order tracking
                        )
                        
                        # Log ALL IDs for complete tracing
                        self.logger.info(f"[ORCHESTRATOR] Created OrderRequest with ALL IDs - "
                                       f"CorrID: {order_request_data.correlation_id}, "
                                       f"RequestID: {order_request_data.request_id}, "
                                       f"ValidationID: {validation_id}, "
                                       f"RelatedOCREventID: {order_request_data.related_ocr_event_id}, "
                                       f"Symbol: {order_request_data.symbol}, "
                                       f"Side: {order_request_data.side}, "
                                       f"Qty: {order_request_data.quantity}")
                    # --- END MODIFICATION ---

                    # CRITICAL: Set validation_id using proper attribute name for RiskManagementService compatibility
                    order_request_data.validation_id = validation_id

                    # T11 MILESTONE: Order Request Ready (after order construction)
                    t11_order_request_ready_ns = time.perf_counter_ns()

                    logger.info(f"SIGNAL_DEBUG: Created OrderRequestData: {order_request_data}")
                    logger.info(f"SIGNAL_DEBUG: T11 order request ready: {t11_order_request_ready_ns}")
                else:
                    # T10 MILESTONE: Decision ready (generation failure - invalid quantity)
                    t10_decision_ready_ns = time.perf_counter_ns()
                    
                    self.logger.warning(f"[{symbol_upper}] Signal {filtered_signal.action.name} determined 0 quantity. No action.")
                    
                    # Create and publish SignalDecisionEvent for generation failure
                    failure_context = {
                        "signal_action": filtered_signal.action.name,
                        "calculated_quantity": quantity,
                        "position_data": pos_data,
                        "current_cost": current_cost,
                        "ocr_frame_timestamp": ocr_frame_timestamp,
                        "validation_stage": "QUANTITY_CALCULATION"
                    }
                    log_signal_decision(
                        decision_type="GENERATION_FAILURE",
                        signal_action=filtered_signal.action.name,
                        signal_reason="INVALID_QUANTITY_CALCULATED",
                        failure_context=failure_context
                    )
                    
                    # Publish signal generation failure event for invalid quantity (legacy method)
                    self._publish_signal_generation_failure_to_redis(
                        symbol=symbol_upper,
                        failure_reason="INVALID_QUANTITY_CALCULATED",
                        failure_context=failure_context,
                        master_correlation_id=origin_correlation_id_from_cleaned_data,
                        causation_id=cleaned_snapshot_event_id_for_causation
                    )
                    
                    filtered_signal = None  # Nullify signal

            # --- STEP 4: Order Publishing (Fingerprinting already done FIRST) ---
            if order_request_data and filtered_signal:
                logger.info(f"SIGNAL_DEBUG: OrderRequestData created, proceeding to publish for {symbol_upper}")

                # Populate OrderRequestData with all necessary fields, including original timestamps for latency tracking
                # Ensure ocr_frame_timestamp from CleanedOCRSnapshotEventData is passed for latency, NOT for fingerprint
                order_request_data.ocr_frame_timestamp = ocr_frame_timestamp
                order_request_data.correlation_id = origin_correlation_id_from_cleaned_data  # Master correlation ID
                order_request_data.request_id = f"{filtered_signal.action.name}_{symbol_upper}_{int(time.perf_counter_ns() / 1_000_000_000)}"  # Ensure unique request_id
                order_request_data.client_order_id = order_request_data.request_id  # Set client_order_id same as request_id for order tracking

                # Add other context if needed
                # Ensure meta is properly initialized as a dict
                if not hasattr(order_request_data, 'meta') or order_request_data.meta is None:
                    order_request_data.meta = {}
                order_request_data.meta["ocr_confidence"] = raw_ocr_confidence
                order_request_data.meta["cleaned_snapshot_event_id"] = cleaned_snapshot_event_id_for_causation

                # Populate context for OrderRequestData (current_price, current_position)
                # STRICT PRICE VALIDATION: Orders must have fresh price data to proceed
                try:
                    current_price = self.price_provider.get_reference_price(symbol_upper)
                    if current_price is None or current_price <= 0.0:
                            # T10 MILESTONE: Decision ready (generation failure - price validation)
                            t10_decision_ready_ns = time.perf_counter_ns()
                            
                            self.logger.error(f"[{symbol_upper}] LIVE_TRADING: Order REJECTED - No valid price available. Price provider returned: {current_price}")
                            
                            # Create and publish SignalDecisionEvent for generation failure
                            failure_context = {
                                "signal_action": filtered_signal.action.name if filtered_signal else "UNKNOWN",
                                "returned_price": current_price,
                                "price_provider_response": "None or zero price",
                                "ocr_frame_timestamp": ocr_frame_timestamp,
                                "validation_stage": "PRICE_VALIDATION"
                            }
                            log_signal_decision(
                                decision_type="GENERATION_FAILURE",
                                signal_action=filtered_signal.action.name if filtered_signal else None,
                                signal_reason="PRICE_VALIDATION_FAILED",
                                failure_context=failure_context
                            )
                            
                            # Publish signal generation failure event for price validation failure (legacy method)
                            self._publish_signal_generation_failure_to_redis(
                                symbol=symbol_upper,
                                failure_reason="PRICE_VALIDATION_FAILED",
                                failure_context=failure_context,
                                master_correlation_id=origin_correlation_id_from_cleaned_data,
                                causation_id=cleaned_snapshot_event_id_for_causation
                            )
                            
                            return False  # Exit without publishing order - no fresh price data available

                    order_request_data.current_price = current_price
                    self.logger.info(f"[{symbol_upper}] LIVE_TRADING: Fresh price obtained: ${current_price:.4f}")
                except Exception as e:
                    # T10 MILESTONE: Decision ready (generation failure - price provider error)
                    t10_decision_ready_ns = time.perf_counter_ns()
                    
                    self.logger.error(f"[{symbol_upper}] LIVE_TRADING: Order REJECTED - Price provider error: {e}")
                    
                    # Create and publish SignalDecisionEvent for generation failure
                    failure_context = {
                        "signal_action": filtered_signal.action.name if filtered_signal else "UNKNOWN",
                        "price_provider_error": str(e),
                        "exception_type": type(e).__name__,
                        "ocr_frame_timestamp": ocr_frame_timestamp,
                        "validation_stage": "PRICE_PROVIDER_ACCESS"
                    }
                    log_signal_decision(
                        decision_type="GENERATION_FAILURE",
                        signal_action=filtered_signal.action.name if filtered_signal else None,
                        signal_reason="PRICE_PROVIDER_ERROR",
                        failure_context=failure_context
                    )
                    
                    # Publish signal generation failure event for price provider exception (legacy method)
                    self._publish_signal_generation_failure_to_redis(
                        symbol=symbol_upper,
                        failure_reason="PRICE_PROVIDER_ERROR",
                        failure_context=failure_context,
                        master_correlation_id=origin_correlation_id_from_cleaned_data,
                        causation_id=cleaned_snapshot_event_id_for_causation
                    )
                    
                    return False  # Exit without publishing order - price provider failed

                pos_data_for_ord_req = self.position_manager.get_position(symbol_upper) or {}
                order_request_data.current_position = pos_data_for_ord_req.get('shares', 0.0)

                logger.info(f"SIGNAL_DEBUG: Attempting to publish OrderRequestEvent for {symbol_upper}")
                logger.info(f"SIGNAL_DEBUG: Full OrderRequestData: {order_request_data}")
                self.logger.info(f"[{symbol_upper}] OrderRequestData created successfully: {order_request_data}")
                
                # Original critical log
                self.logger.critical(f"[{symbol_upper}] Publishing OrderRequestEvent: {order_request_data}")
                

                # Check for validation_id and register orchestrator signal logic completion
                if validation_id and self._pipeline_validator:
                    try:
                        self._pipeline_validator.register_stage_completion(
                            validation_id,
                            'orchestrator_signal_logic_completed',
                            stage_data={'symbol': symbol_upper, 'action': filtered_signal.action.name, 'quantity': order_request_data.quantity}
                        )
                    except Exception as e:
                        self.logger.warning(f"Error registering orchestrator_signal_logic_completed: {e}")

                # validation_id is now set during OrderRequestData creation

                # Performance benchmarking: Capture latency from cleaned OCR data to OrderRequest
                
                if self._benchmarker and order_request_data and cleaned_event_data_arrival_time:
                    latency_ms = (time.perf_counter_ns() / 1_000_000_000 - cleaned_event_data_arrival_time) * 1000
                    context = {
                        'symbol': order_request_data.symbol,
                        'source_strategy': order_request_data.source_strategy,
                        'ocr_frame_timestamp': ocr_frame_timestamp
                    }
                    self._benchmarker.capture_metric("signal_gen.ocr_cleaned_to_order_request_latency_ms", latency_ms, context=context)

                # CRUCIAL LINK: Before publishing OrderRequestEvent, inject order execution pipeline tracking
                if validation_id and self._pipeline_validator and order_request_data:
                    try:
                        # Ensure validation_id is on order_request_data first (link back to original OCR pipeline)
                        if order_request_data.validation_id is None:
                            order_request_data.validation_id = validation_id

                        # Create a new linked pipeline trace for order execution
                        order_execution_uid = f"{validation_id}_order"

                        # Create payload dict from OrderRequestData for injection
                        order_request_dict = {
                            'symbol': symbol_upper,
                            'request_id': order_request_data.request_id,
                            'side': order_request_data.side,
                            'quantity': order_request_data.quantity,
                            'order_type': order_request_data.order_type,
                            'source_strategy': order_request_data.source_strategy,
                            '__validation_id': validation_id  # Link back to original OCR pipeline
                        }

                        # Inject the order execution pipeline tracking
                        self._pipeline_validator.inject_test_data(
                            pipeline_type='order_execution_path',
                            payload=order_request_dict,
                            custom_uid=order_execution_uid
                        )

                        # IMPORTANT: Register the first stage 'order_request_published' for the NEW UID
                        self._pipeline_validator.register_stage_completion(
                            order_execution_uid,  # Use the NEW UID
                            'order_request_published',
                            stage_data={'original_ocr_validation_id': validation_id, 'request_id': order_request_data.request_id}
                        )

                        # Make sure the validation_id on the OrderRequestData being published is the NEW order_execution_uid
                        order_request_data.validation_id = order_execution_uid

                        self.logger.debug(f"[{symbol_upper}] Injected order execution pipeline tracking with UID: {order_execution_uid}")

                    except Exception as e:
                        self.logger.warning(f"Error setting up order execution pipeline tracking: {e}")

                # Publish OrderRequestEvent with correlation context
                # The correlation_id is already in order_request_data.correlation_id
                # The causation (cleaned snapshot event ID) is in order_request_data.related_ocr_event_id
                # For additional tracking, store the causation chain in metadata
                if not order_request_data.meta:
                    order_request_data.meta = {}
                order_request_data.meta['causation_id'] = order_request_data.related_ocr_event_id
                order_request_data.meta['origin_correlation_id'] = origin_correlation_id_from_cleaned_data
                
                # Add MAF decision metadata for causation chain: MAF Decision → Order Request
                if maf_decision_metadata:
                    order_request_data.meta['maf_decision_metadata'] = maf_decision_metadata
                    # Extract MAF decision ID for causation chain
                    maf_decision_id = maf_decision_metadata.get('decision_id')
                    if maf_decision_id:
                        order_request_data.meta['maf_decision_id'] = maf_decision_id
                        # Set proper causation: Order is caused by MAF decision, not just OCR snapshot
                        order_request_data.meta['immediate_causation_type'] = 'maf_decision'
                        order_request_data.meta['immediate_causation_id'] = maf_decision_id
                
                # T10 MILESTONE: Decision ready (order request ready)
                t10_decision_ready_ns = time.perf_counter_ns()
                
                # Add current market context to signal decision data
                order_request_data.current_price = order_request_data.current_price
                order_request_data.current_position = order_request_data.current_position
                
                order_event = OrderRequestEvent(
                    data=order_request_data
                )
                self.event_bus.publish(order_event)
                
                # T12 MILESTONE: Order Request Published (after order publishing)
                t12_order_request_published_ns = time.perf_counter_ns()
                
                # Signal was successfully published
                signal_published = True
                
                self.logger.critical(f"[{symbol_upper}] Published OrderRequestEvent with MasterCorrelationID: {order_request_data.correlation_id}, "
                                     f"ValidationID: {getattr(order_request_data, 'validation_id', 'N/A')}, "
                                     f"CausationID: {order_request_data.related_ocr_event_id}, "
                                     f"OriginCorrelationID: {origin_correlation_id_from_cleaned_data}")
                logger.info(f"SIGNAL_DEBUG: T12 order request published: {t12_order_request_published_ns}")

                # Log signal decision for successful generation
                log_signal_decision(
                    decision_type="SIGNAL_GENERATED",
                    signal_action=filtered_signal.action.name,
                    signal_quantity=order_request_data.quantity,
                    signal_reason="ORDER_REQUEST_GENERATED",
                    order_request_data=order_request_data
                )
                
                # Publish comprehensive summary event with full T7-T12 timing chain
                await self._publish_orchestration_summary_event(
                    symbol=symbol_upper,
                    order_request_data=order_request_data,
                    filtered_signal=filtered_signal,
                    maf_decision_metadata=maf_decision_metadata,
                    timing_data={
                        't7_orchestration_start_ns': t7_orchestration_start_ns,
                        't8_interpretation_end_ns': t8_interpretation_end_ns,
                        't11_order_request_ready_ns': t11_order_request_ready_ns,
                        't12_order_request_published_ns': t12_order_request_published_ns
                    },
                    correlation_id=origin_correlation_id_from_cleaned_data,
                    causation_id=cleaned_snapshot_event_id_for_causation,
                    ocr_frame_timestamp=ocr_frame_timestamp,
                    raw_ocr_confidence=raw_ocr_confidence
                )

                # NEW: Publish signal decision to Redis via bulletproof IPC for IntelliSense visibility (legacy method)
                self._publish_signal_decision_to_redis(
                    order_request_data, 
                    symbol_upper, 
                    "ORDER_REQUEST_GENERATED",
                    origin_correlation_id_from_cleaned_data,
                    cleaned_snapshot_event_id_for_causation
                )

                # 🍒 CHERRY-PICK: Remove obsolete Redis publishing path
                # OBSOLETE_CODE_PATH_SKIPPED: Direct Redis publishing from orchestrator is obsolete
                # Order requests now properly route via EventBus -> ApplicationCore -> Babysitter -> Redis
                self.logger.debug(f"[{symbol_upper}] OBSOLETE_CODE_PATH_SKIPPED: Direct Redis publishing bypassed - routing via AppCore->Babysitter")
                

                # Register order request published stage for the ORIGINAL OCR pipeline
                if validation_id and self._pipeline_validator:
                    try:
                        self._pipeline_validator.register_stage_completion(
                            validation_id,
                            'order_request_published',
                            stage_data={'symbol': symbol_upper, 'request_id': order_request_data.request_id}
                        )
                    except Exception as e:
                        self.logger.warning(f"Error registering order_request_published for OCR pipeline: {e}")

                # Record system action using MasterActionFilterService
                if self.action_filter_service:
                    if filtered_signal.action == TradeActionType.ADD_LONG:
                        logger.info(f"SIGNAL_DEBUG: Recording system ADD action for {symbol_upper}")
                        self.action_filter_service.record_system_add_action(symbol_upper, current_cost)
                    elif filtered_signal.action == TradeActionType.REDUCE_PARTIAL:
                        # Get market price for action recording
                        market_price_at_action = order_request_data.current_price or \
                                               (self.price_provider.get_bid_price(symbol_upper) if order_request_data.side == "SELL" else self.price_provider.get_ask_price(symbol_upper)) \
                                               or current_cost  # Fallback chain
                        logger.info(f"SIGNAL_DEBUG: Recording system REDUCE action for {symbol_upper}")
                        self.action_filter_service.record_system_reduce_action(symbol_upper, current_realized_pnl, market_price_at_action)
                
                # Return True - signal was published
                return signal_published
                
            else:
                # T10 MILESTONE: Decision ready (no signal generated)
                if t10_decision_ready_ns is None:  # Only set if not already set
                    t10_decision_ready_ns = time.perf_counter_ns()
                
                # If no OrderRequestData is created by the end of the method, log this fact clearly
                logger.info(f"SIGNAL_DEBUG: No OrderRequestData created for {symbol_upper} - order_request_data: {order_request_data}, filtered_signal: {filtered_signal}")
                self.logger.info(f"[{symbol_upper}] No OrderRequestData created - no trading signal generated for this OCR update")
                
                # Create and publish SignalDecisionEvent for no signal case
                signal_reason = "NO_SIGNAL_GENERATED"
                if filtered_signal is None:
                    signal_reason = "NO_FILTERED_SIGNAL"
                elif order_request_data is None:
                    signal_reason = "NO_ORDER_REQUEST_DATA"
                    
                log_signal_decision(
                    decision_type="SIGNAL_SUPPRESSED",
                    signal_action=filtered_signal.action.name if filtered_signal else None,
                    signal_reason=signal_reason
                )
                
                # Return False - no signal was published
                return False

        except Exception as e:
            self.logger.error(f"[{symbol_upper}] Error in _process_symbol_state_change: {e}", exc_info=True)
            # Return False on error
            return False

    def _calculate_quantity_for_signal(self, signal: TradeSignal, pos_data: Dict[str, Any], current_cost: float) -> float:
        """
        Calculate the quantity for a given trade signal.

        Args:
            signal: The trade signal
            pos_data: Current position data from PositionManager
            current_cost: Current cost basis from OCR

        Returns:
            Calculated quantity (0 if invalid)
        """
        try:
            # Always read from the global config singleton for hot reload support
            from utils.global_config import config
            
            if signal.action == TradeActionType.OPEN_LONG:
                # OPEN quantity from config
                quantity = float(getattr(config, 'initial_share_size', 1000))
                self.logger.info(f"[{signal.symbol}] OPEN quantity: {quantity}")
                return quantity

            elif signal.action == TradeActionType.ADD_LONG:
                # ADD quantity calculation
                add_type = getattr(config, 'add_type', "Equal").lower()

                if add_type == "equal":
                    quantity = float(getattr(config, 'manual_shares', 0))
                    self.logger.info(f"[{signal.symbol}] ADD Equal quantity: {quantity}")
                    return quantity

                elif add_type == "costbasis":
                    # CRITICAL: CostBasis quantity calculation (ported from legacy OrderStrategy)
                    return self._calculate_costbasis_add_quantity(signal.symbol, pos_data, current_cost)

                else:
                    self.logger.warning(f"[{signal.symbol}] Unknown add_type: {add_type}. Using manual_shares.")
                    return float(getattr(config, 'manual_shares', 0))

            elif signal.action == TradeActionType.REDUCE_PARTIAL:
                # REDUCE quantity calculation
                current_shares = pos_data.get('shares', 0.0)
                if abs(current_shares) <= 1e-9:
                    self.logger.warning(f"[{signal.symbol}] REDUCE signal but no position in PM.")
                    return 0.0

                reduce_percentage = float(getattr(config, 'reduce_percentage', 50.0))
                quantity = abs(round(current_shares * (reduce_percentage / 100.0)))

                # Ensure quantity is valid
                if quantity < 1.0:
                    quantity = 0.0
                elif quantity > abs(current_shares):
                    quantity = abs(current_shares)

                self.logger.info(f"[{signal.symbol}] REDUCE quantity: {quantity} ({reduce_percentage}% of {current_shares})")
                return quantity

            elif signal.action == TradeActionType.CLOSE_POSITION:
                # CLOSE quantity - entire position
                current_shares = pos_data.get('shares', 0.0)
                quantity = abs(current_shares)
                self.logger.info(f"[{signal.symbol}] CLOSE quantity: {quantity}")
                return quantity

            else:
                self.logger.warning(f"[{signal.symbol}] Unknown signal action: {signal.action}")
                return 0.0

        except Exception as e:
            self.logger.error(f"[{signal.symbol}] Error calculating quantity: {e}", exc_info=True)
            return 0.0

    def _calculate_costbasis_add_quantity(self, symbol: str, pos_data: Dict[str, Any], new_ocr_cost: float) -> float:
        """
        Calculate ADD quantity using CostBasis method (ported from legacy OrderStrategy).

        EXACT LEGACY LOGIC: Ported from legacy OrderStrategy._calculate_add_params

        Args:
            symbol: Trading symbol
            pos_data: Current position data from PositionManager
            new_ocr_cost: New cost basis from OCR

        Returns:
            Calculated quantity for CostBasis ADD
        """
        try:
            # Get current position data (inputs needed by OCRScalpingSignalGenerator)
            current_pm_shares = pos_data.get('shares', 0.0)
            current_pm_avg_price = pos_data.get('avg_price', 0.0)

            # Get reference price (Ask price for BUY side ADD)
            # NEW: Use JIT Price Oracle blocking method for critical cost basis calculation
            try:
                price_data = self.price_provider.get_price_blocking(symbol, timeout_sec=2.0)
                ref_price = price_data.price
            except (StalePriceError, PriceUnavailableError) as e:
                self.logger.warning(f"[{symbol}] CostBasis ADD: Could not get blocking price, trying fallback: {e}")
                # Fallback to direct ask price
                ref_price = self.price_provider.get_ask_price(symbol)
                if ref_price is None or ref_price <= 0.0:
                    self.logger.error(f"[{symbol}] CostBasis ADD: No valid reference price available")
                    return 0.0

            self.logger.info(f"[{symbol}] CostBasis ADD calculation: "
                           f"current_shares={current_pm_shares:.2f}, "
                           f"current_avg_price={current_pm_avg_price:.4f}, "
                           f"new_ocr_cost={new_ocr_cost:.4f}, "
                           f"ref_price={ref_price:.4f}")

            # EXACT LEGACY LOGIC from OrderStrategy._calculate_add_params:
            denom = (ref_price - new_ocr_cost)
            if abs(denom) < 1e-9:
                self.logger.warning(f"[{symbol}] Denominator near zero for ADD (CostBasis) calculation in OCRSignalGenerator. Skipping.")
                shares_to_add = 0.0
            else:
                num = current_pm_shares * (new_ocr_cost - current_pm_avg_price)
                shares_to_add = num / denom

            quantity_for_event = float(round(shares_to_add))
            if quantity_for_event < 1.0:
                self.logger.warning(f"[{symbol}] Calculated shares to ADD (CostBasis) < 1 ({shares_to_add:.2f}) in OCRSignalGenerator. Setting to 0.")
                quantity_for_event = 0.0

            self.logger.info(f"[{symbol}] CostBasis ADD result: "
                           f"num={num:.2f}, denom={denom:.4f}, "
                           f"raw_shares={shares_to_add:.2f}, "
                           f"final_quantity={quantity_for_event}")

            return quantity_for_event

        except Exception as e:
            self.logger.error(f"[{symbol}] Error in CostBasis ADD calculation: {e}", exc_info=True)
            return 0.0

    def _get_side_for_action(self, action: TradeActionType) -> str:
        """
        Determine the order side (BUY/SELL) for a given action.

        Args:
            action: The trade action type

        Returns:
            Order side string ("BUY" or "SELL")
        """
        if action in [TradeActionType.OPEN_LONG, TradeActionType.ADD_LONG]:
            return "BUY"
        elif action in [TradeActionType.CLOSE_POSITION, TradeActionType.REDUCE_PARTIAL]:
            return "SELL"
        else:
            self.logger.warning(f"Unknown action type for side determination: {action}")
            return "BUY"  # Default to BUY

    # --- REMOVED: _parse_simple_ocr_format ---
    # This method has been moved to PythonOCRDataConditioner
    # The signal generator now receives pre-processed clean snapshots

    # --- REMOVED: All OCR parsing methods ---
    # The following methods have been moved to PythonOCRDataConditioner:
    # - _parse_simple_ocr_format
    # - _get_robust_price_for_filter
    # - _correct_decimal_ocr
    # - _fix_price_with_live
    # - _parse_cost_basis_token
    # - _parse_pnl_per_share_token
    # - _parse_realized_pnl_token
    # - _debug_print (for parsing)
    # - _log_pipeline_event
    #
    # This class now focuses purely on strategy logic using cleaned data.

    # --- REMOVED: All embedded action filter methods ---
    # The following methods have been replaced by MasterActionFilterService:
    # - _record_system_add_action
    # - _record_system_reduce_action
    # - _reset_action_filter_state
    # - _filter_cost_add_signal
    # - _filter_rPnL_reduce_signal
    #
    # This class now uses the injected MasterActionFilterService for all filtering logic.

    def _make_fingerprint(self,
                          symbol: str,
                          event_type: str,  # This is from TradeActionType.name
                          shares_float: float
                          # Optional: Add other STABLE parameters if they define a distinct signal,
                          # e.g., a specific rounded trigger price if the signal is "buy at X.XX"
                         ) -> tuple:
        """
        Creates a stable tuple fingerprint based on core signal characteristics.
        Excludes volatile per-instance data like timestamps.
        """
        try:
            symbol_upper = symbol.upper()
            event_type_upper = event_type.upper()
            # Round shares to an integer or a fixed precision if float issues are a concern.
            # Using int here for simplicity, assuming whole shares for scalping signals.
            shares_repr = str(int(round(shares_float)))

            fingerprint_tuple = (
                event_type_upper,
                symbol_upper,
                shares_repr
                # Add other stable, defining parameters of the signal here if needed.
                # e.g., if a signal is "OPEN_LONG_XYZ_AT_10.50", then 10.50 (rounded) could be part of it.
            )

            # Log what goes into the fingerprint tuple
            self.logger.info(f"[{symbol_upper}] Fingerprint TUPLE created: {fingerprint_tuple} "
                             f"(Inputs: type={event_type_upper}, symbol={symbol_upper}, shares={shares_repr})")
            return fingerprint_tuple
        except Exception as e:
            self.logger.error(f"OCRScalpingSignalOrchestratorService [{symbol if symbol else 'NOSYMBOL'}]: "
                              f"Error creating fingerprint tuple: {e}", exc_info=True)
            # Return a unique tuple on error to prevent accidental duplicate suppression
            # and ensure the error is surfaced rather than silently failing to trade.
            import uuid
            return (str(uuid.uuid4()),)  # Ensure it's a tuple

    # FUZZY'S CLEANUP: Old dumb fingerprint methods removed!
    # Smart fingerprint logic is now handled by the injected FingerprintService

    # --- REMOVED: _debug_print and all remaining OCR parsing methods ---
    # These have been moved to PythonOCRDataConditioner

    def _debug_print_REMOVED(self, msg: str):
        """REMOVED: Simple debug helper - moved to PythonOCRDataConditioner."""
        pass

    # --- ALL OCR PARSING METHODS REMOVED ---
    # The following methods have been completely moved to PythonOCRDataConditioner:
    # - _get_robust_price_for_filter
    # - _correct_decimal_ocr
    # - _fix_price_with_live
    # - _log_pipeline_event
    # - _parse_cost_basis_token
    # - _parse_pnl_per_share_token
    # - _parse_realized_pnl_token
    #
    # This class now focuses purely on strategy logic using cleaned data.

    # 🍒 CHERRY-PICK: REMOVED _publish_order_request_to_redis() method
    # RATIONALE: Direct Redis publishing from orchestrator is obsolete and problematic
    # Order requests now properly route via EventBus -> ApplicationCore -> Babysitter -> Redis
    # Clean architecture - no direct ApplicationCore dependencies
    # and maintains proper architectural separation between components

    def _start_ipc_worker(self):
        """Start the async IPC publishing worker thread."""
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            return
            
        self._ipc_worker_thread = threading.Thread(
            target=self._ipc_worker_loop,
            name="SignalOrchestrator-IPC-Worker",
            daemon=True
        )
        self._ipc_worker_thread.start()
        self.logger.info("Started async IPC publishing worker thread")
    
    def _ipc_worker_loop(self):
        """Worker loop that processes IPC messages asynchronously."""
        while not self._ipc_shutdown_event.is_set():
            try:
                # Get message from queue with timeout
                try:
                    target_stream, redis_message = self._ipc_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Legacy IPC code - now using telemetry service
                try:
                    self.logger.debug(f"Legacy IPC queue processing - this message was queued but telemetry service should be used instead")
                except Exception as e:
                    self.logger.error(f"Async IPC: Error publishing: {e}")
                    
            except Exception as e:
                self.logger.error(f"Error in IPC worker loop: {e}", exc_info=True)
                
        self.logger.info("IPC worker thread shutting down")
    
    def _queue_ipc_publish(self, target_stream: str, redis_message: str):
        """Queue an IPC message for async publishing."""
        try:
            self._ipc_queue.put_nowait((target_stream, redis_message))
        except queue.Full:
            self.logger.warning(f"IPC queue full, dropping message for stream '{target_stream}'")

    def _publish_signal_decision_to_redis(self, order_request_data: OrderRequestData, symbol: str, decision_type: str, 
                                         master_correlation_id: Optional[str] = None, causation_id: Optional[str] = None):
        """Publish signal generation decision to Redis stream via TelemetryService for IntelliSense visibility.
        
        Args:
            order_request_data: The order request data
            symbol: Trading symbol
            decision_type: Type of decision
            master_correlation_id: Master correlation ID for end-to-end tracing
            causation_id: ID of the event that caused this decision
        """
        if not self._correlation_logger:
            self.logger.warning("Telemetry service not available to publish signal decision")
            return

        try:
            # Use passed parameters or fallback to OrderRequestData attributes
            correlation_id = master_correlation_id or getattr(order_request_data, 'correlation_id', None)
            causation = causation_id or getattr(order_request_data, 'related_ocr_event_id', None)

            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self.config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping telemetry send for signal decision (Symbol: {symbol}, CorrID: {correlation_id})")
                return

            # 1. Convert OrderRequestData to dict for signal decision payload
            import dataclasses
            if hasattr(order_request_data, 'to_dict'):
                order_data_dict = order_request_data.to_dict()
            elif dataclasses.is_dataclass(order_request_data):
                order_data_dict = dataclasses.asdict(order_request_data)
            else:
                self.logger.error(f"OrderRequestData for {symbol} (CorrID: {correlation_id}) is not serializable. Cannot publish signal decision to Redis.")
                order_data_dict = {"error": "serialization_failed", "correlation_id": correlation_id}

            # 2. Create signal decision payload
            import time
            signal_decision_payload = {
                "decision_type": decision_type,
                "symbol": symbol,
                "order_request_data": order_data_dict,
                "correlation_id": correlation_id,
                "timestamp": time.perf_counter_ns() / 1_000_000_000,
                "source_component": "OCRScalpingSignalOrchestratorService"
            }

            # 3. Use CorrelationLogger for clean async publishing
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OCRScalpingSignalOrchestratorService",
                    event_name="SignalDecision",
                    payload=signal_decision_payload,
                    stream_override="testrade:signal-decisions"
                )
                self.logger.info(f"Signal decision for {symbol} logged (CorrID: {correlation_id}, Decision: {decision_type})")
            else:
                self.logger.error(f"Correlation logger not available - cannot publish signal decision for {symbol} (CorrID: {correlation_id})")

        except Exception as e:
            self.logger.error(f"Error publishing signal decision for {symbol}: {e}", exc_info=True)

    def _publish_signal_suppression_to_redis(self, symbol: str, suppression_reason: str, master_correlation_id: str, 
                                            causation_id: Optional[str] = None, fingerprint_data=None):
        """Publish signal suppression/rejection to Redis stream via TelemetryService for IntelliSense visibility.
        
        Args:
            symbol: Trading symbol
            suppression_reason: Reason for suppression
            master_correlation_id: Master correlation ID for end-to-end tracing
            causation_id: ID of the event that caused this suppression
            fingerprint_data: Optional fingerprint data for duplicate detection
        """
        if not self._correlation_logger:
            self.logger.warning("Telemetry service not available to publish signal suppression")
            return

        try:
            # TANK Mode: Check if IPC data dumping is disabled
            if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
                not self.config_service.ENABLE_IPC_DATA_DUMP):
                self.logger.debug(f"TANK_MODE: Skipping telemetry send for signal suppression (Symbol: {symbol}, CorrID: {master_correlation_id})")
                return

            # Create signal suppression payload
            import time
            signal_suppression_payload = {
                "decision_type": "SIGNAL_SUPPRESSED",
                "symbol": symbol,
                "suppression_reason": suppression_reason,
                "correlation_id": master_correlation_id,
                "fingerprint_data": str(fingerprint_data) if fingerprint_data else None,
                "timestamp": time.perf_counter_ns() / 1_000_000_000,
                "source_component": "OCRScalpingSignalOrchestratorService"
            }

            # Use CorrelationLogger for clean async publishing
            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OCRScalpingSignalOrchestratorService",
                    event_name="SignalSuppression",
                    payload=signal_suppression_payload,
                    stream_override="testrade:signal-decisions"
                )
                self.logger.info(f"Signal suppression for {symbol} logged (CorrID: {master_correlation_id}, Reason: {suppression_reason})")
            else:
                self.logger.error(f"Correlation logger not available - cannot publish signal suppression for {symbol} (CorrID: {master_correlation_id})")

        except Exception as e:
            self.logger.error(f"Error publishing signal suppression for {symbol}: {e}", exc_info=True)

    def _publish_signal_validation_failure_to_redis(self, symbol: str, failure_reason: str, 
                                                   master_correlation_id: Optional[str] = None,
                                                   causation_id: Optional[str] = None):
        """
        Publish signal validation failure events to Redis for IntelliSense visibility.
        
        Args:
            symbol: Trading symbol
            failure_reason: Reason for validation failure
            master_correlation_id: Master correlation ID for end-to-end tracing
            causation_id: ID of the event that caused this validation failure
        """
        if not self._correlation_logger:
            self.logger.warning("Telemetry service not available to publish signal validation failure")
            return

        # TANK Mode: Check if IPC data dumping is disabled
        if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
            not self.config_service.ENABLE_IPC_DATA_DUMP):
            self.logger.debug(f"TANK_MODE: Skipping telemetry send for signal validation failure (Symbol: {symbol})")
            return

        try:
            correlation_id = master_correlation_id or f"signal_validation_failure_{int(time.time_ns())}"
            
            redis_payload = {
                "symbol": symbol,
                "decision_type": "VALIDATION_FAILURE",
                "failure_reason": failure_reason,
                "timestamp": time.perf_counter_ns() / 1_000_000_000,
                "correlation_id": correlation_id
            }

            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OCRScalpingSignalOrchestratorService",
                    event_name="SignalValidationFailure",
                    payload=redis_payload,
                    stream_override="testrade:signal-decisions"
                )
                self.logger.debug(f"Signal validation failure logged for {symbol}: {failure_reason}")
            else:
                self.logger.error(f"Correlation logger not available - cannot publish signal validation failure for {symbol}")

        except Exception as e:
            self.logger.error(f"Error publishing signal validation failure for {symbol}: {e}", exc_info=True)

    def _publish_signal_generation_failure_to_redis(self, symbol: str, failure_reason: str, 
                                                   failure_context: Dict[str, Any], 
                                                   master_correlation_id: Optional[str] = None,
                                                   causation_id: Optional[str] = None):
        """
        Publish signal generation failure events to Redis for IntelliSense visibility.
        
        Args:
            symbol: Trading symbol
            failure_reason: Reason for generation failure
            failure_context: Additional context about the failure
            master_correlation_id: Master correlation ID for end-to-end tracing
            causation_id: ID of the event that caused this generation failure
        """
        if not self._correlation_logger:
            self.logger.warning("Telemetry service not available to publish signal generation failure")
            return

        # TANK Mode: Check if IPC data dumping is disabled
        if (hasattr(self.config_service, 'ENABLE_IPC_DATA_DUMP') and 
            not self.config_service.ENABLE_IPC_DATA_DUMP):
            self.logger.debug(f"TANK_MODE: Skipping telemetry send for signal generation failure (Symbol: {symbol})")
            return

        try:
            correlation_id = master_correlation_id or f"signal_generation_failure_{int(time.time_ns())}"
            
            redis_payload = {
                "symbol": symbol,
                "decision_type": "GENERATION_FAILURE",
                "failure_reason": failure_reason,
                "failure_context": failure_context,
                "timestamp": time.perf_counter_ns() / 1_000_000_000,
                "correlation_id": correlation_id
            }

            if self._correlation_logger:
                self._correlation_logger.log_event(
                    source_component="OCRScalpingSignalOrchestratorService",
                    event_name="SignalGenerationFailure",
                    payload=redis_payload,
                    stream_override="testrade:signal-decisions"
                )
                self.logger.debug(f"Signal generation failure logged for {symbol}: {failure_reason}")
            else:
                self.logger.error(f"Correlation logger not available - cannot publish signal generation failure for {symbol}")

        except Exception as e:
            self.logger.error(f"Error publishing signal generation failure for {symbol}: {e}", exc_info=True)

    async def _publish_orchestration_summary_event(self, 
                                                  symbol: str,
                                                  order_request_data: OrderRequestData,
                                                  filtered_signal: Optional[TradeSignal],
                                                  maf_decision_metadata: Optional[Dict[str, Any]],
                                                  timing_data: Dict[str, int],
                                                  correlation_id: str,
                                                  causation_id: str,
                                                  ocr_frame_timestamp: float,
                                                  raw_ocr_confidence: Optional[float]):
        """
        Publish comprehensive orchestration summary event with full T7-T12 timing chain.
        
        This event provides end-to-end instrumentation of the signal processing pipeline
        from orchestration start through order publishing, including MAF decision metadata.
        """
        try:
            # Extract T9-T10 timing from MAF metadata
            t9_maf_filtering_start_ns = maf_decision_metadata.get('t9_maf_filtering_start_ns') if maf_decision_metadata else None
            t10_maf_decision_ready_ns = maf_decision_metadata.get('t10_maf_decision_ready_ns') if maf_decision_metadata else None
            
            # Calculate latencies
            orchestration_latency_ms = None
            if timing_data.get('t12_order_request_published_ns') and timing_data.get('t7_orchestration_start_ns'):
                orchestration_latency_ms = (timing_data['t12_order_request_published_ns'] - timing_data['t7_orchestration_start_ns']) / 1_000_000
                
            interpretation_latency_ms = None
            if timing_data.get('t8_interpretation_end_ns') and timing_data.get('t7_orchestration_start_ns'):
                interpretation_latency_ms = (timing_data['t8_interpretation_end_ns'] - timing_data['t7_orchestration_start_ns']) / 1_000_000
                
            maf_latency_ms = None
            if t10_maf_decision_ready_ns and t9_maf_filtering_start_ns:
                maf_latency_ms = (t10_maf_decision_ready_ns - t9_maf_filtering_start_ns) / 1_000_000
                
            order_construction_latency_ms = None
            if timing_data.get('t11_order_request_ready_ns') and timing_data.get('t8_interpretation_end_ns'):
                order_construction_latency_ms = (timing_data['t11_order_request_ready_ns'] - timing_data['t8_interpretation_end_ns']) / 1_000_000
                
            # Create comprehensive summary payload
            summary_payload = {
                "event_type": "OrchestrationSummary",
                "symbol": symbol,
                "timestamp": time.time(),
                
                # Complete timing chain T7-T12
                "timing_milestones": {
                    "t7_orchestration_start_ns": timing_data.get('t7_orchestration_start_ns'),
                    "t8_interpretation_end_ns": timing_data.get('t8_interpretation_end_ns'),
                    "t9_maf_filtering_start_ns": t9_maf_filtering_start_ns,
                    "t10_maf_decision_ready_ns": t10_maf_decision_ready_ns,
                    "t11_order_request_ready_ns": timing_data.get('t11_order_request_ready_ns'),
                    "t12_order_request_published_ns": timing_data.get('t12_order_request_published_ns')
                },
                
                # Calculated latencies
                "latency_analysis": {
                    "total_orchestration_latency_ms": orchestration_latency_ms,
                    "interpretation_latency_ms": interpretation_latency_ms,
                    "maf_filtering_latency_ms": maf_latency_ms,
                    "order_construction_latency_ms": order_construction_latency_ms
                },
                
                # Causation chain
                "causation_chain": {
                    "master_correlation_id": correlation_id,
                    "ocr_causation_id": causation_id,
                    "maf_decision_id": maf_decision_metadata.get('decision_id') if maf_decision_metadata else None,
                    "order_request_id": order_request_data.request_id
                },
                
                # Signal processing results
                "signal_processing": {
                    "signal_action": filtered_signal.action.name if filtered_signal else None,
                    "signal_quantity": order_request_data.quantity,
                    "order_type": order_request_data.order_type,
                    "order_side": order_request_data.side,
                    "limit_price": order_request_data.limit_price
                },
                
                # MAF decision context
                "maf_decision_context": maf_decision_metadata,
                
                # OCR context
                "ocr_context": {
                    "frame_timestamp": ocr_frame_timestamp,
                    "confidence": raw_ocr_confidence,
                    "triggering_cost_basis": order_request_data.triggering_cost_basis
                }
            }
            
            # WORLD-CLASS INSTRUMENTATION: Fire-and-forget T7-T12 milestone telemetry
            if self._correlation_logger:
                try:
                    # Enhanced payload for world-class instrumentation
                    instrumentation_payload = {
                        # Core timing milestones
                        **summary_payload["timing_milestones"],
                        
                        # Enhanced latency calculations
                        "T7_to_T8_ms": interpretation_latency_ms,
                        "T8_to_T11_ms": order_construction_latency_ms,
                        "T11_to_T12_ms": (timing_data.get('t12_order_request_published_ns', 0) - timing_data.get('t11_order_request_ready_ns', 0)) / 1_000_000 if timing_data.get('t11_order_request_ready_ns') and timing_data.get('t12_order_request_published_ns') else None,
                        "T7_to_T12_total_ms": orchestration_latency_ms,
                        
                        # Include MAF timing if available
                        "T9_to_T10_ms": maf_latency_ms,
                        
                        # Complete payload
                        **summary_payload
                    }
                    
                    # Add origin_timestamp to payload
                    instrumentation_payload["origin_timestamp_s"] = timing_data.get('t7_orchestration_start_ns', 0) / 1_000_000_000
                    # Add correlation_id to payload for correlation logger handling
                    instrumentation_payload["correlation_id"] = correlation_id
                    
                    # Fire-and-forget correlation logging
                    self._correlation_logger.log_event(
                        source_component="OCRScalpingSignalOrchestrator",
                        event_name="OrchestrationMilestones",
                        payload=instrumentation_payload,
                        stream_override="testrade:instrumentation:orchestration"
                    )
                    
                    self.logger.debug(f"[{symbol}] Published world-class orchestration instrumentation "
                                    f"(T7-T12 total: {orchestration_latency_ms:.2f}ms)")
                    
                except Exception as telemetry_error:
                    # Non-critical error - don't affect order processing
                    self.logger.debug(f"Correlation logging error (non-critical): {telemetry_error}")
            else:
                self.logger.warning(f"[{symbol}] No correlation logger available for orchestration summary")
                
        except Exception as e:
            self.logger.error(f"Error publishing orchestration summary for {symbol}: {e}", exc_info=True)

    def stop(self):
        """Stop the OCRScalpingSignalOrchestratorService and its worker threads."""
        logger.info("OCRScalpingSignalOrchestratorService stopping...")
        self._internal_stop_event.set()
        
        # Stop IPC worker thread
        self._ipc_shutdown_event.set()
        if self._ipc_worker_thread and self._ipc_worker_thread.is_alive():
            self._ipc_worker_thread.join(timeout=2.0)
            if self._ipc_worker_thread.is_alive():
                logger.warning("IPC worker thread did not stop gracefully")

        # Unsubscribe from events if we were subscribed (legacy path)
        if self._cleaned_ocr_event_handler_for_event_bus:
            try:
                self.event_bus.unsubscribe(CleanedOCRSnapshotEvent, self._cleaned_ocr_event_handler_for_event_bus)
                self._cleaned_ocr_event_handler_for_event_bus = None
                logger.debug("Unsubscribed from CleanedOCRSnapshotEvent")
            except Exception as e:
                logger.warning(f"Error unsubscribing from CleanedOCRSnapshotEvent: {e}")

        # Drain the queue to prevent processing stale items on next start
        drained_count = 0
        while not self._ocr_tasks_queue.empty():
            try:
                self._ocr_tasks_queue.get_nowait()
                drained_count += 1
            except QueueEmpty:
                break

        if drained_count > 0:
            logger.info(f"OCRScalpingSignalOrchestratorService: Drained {drained_count} pending OCR tasks from queue")

        try:
            # Python 3.9+ for cancel_futures
            self._ocr_worker_pool.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # For older Python versions
            self._ocr_worker_pool.shutdown(wait=True)

        logger.info("OCRScalpingSignalOrchestratorService stopped.")
