"""
IntelliSense Master Controller with EpochTimelineGenerator Integration

Enhanced master controller for IntelliSense optimization system with
session lifecycle management, intelligence engine coordination, and
EpochTimelineGenerator for precise multi-source timeline replay.
"""

import uuid
import time
import logging
import os
import shutil  # Add for deleting session directory
import signal  # NEW for signal handling
import threading
from typing import Dict, Any, Optional, List, Callable, TYPE_CHECKING  # Added TYPE_CHECKING

# Safe imports - core types and enums (less likely to cause cycles)
from intellisense.core.enums import OperationMode, TestStatus
from intellisense.core.types import (
    IntelliSenseConfig, TestSession, TestSessionConfig, TimelineEvent,
    OCRTimelineEvent, PriceTimelineEvent, BrokerTimelineEvent  # For dispatching
)
from intellisense.core.exceptions import IntelliSenseInitializationError, IntelliSenseSessionError, IntelliSenseDataError

# Safe imports - interfaces (abstract base classes)
from intellisense.core.interfaces import IOCRIntelligenceEngine, IPriceIntelligenceEngine, IBrokerIntelligenceEngine
from intellisense.factories.interfaces import IDataSourceFactory  # CORRECTED IMPORT LOCATION
from intellisense.controllers.interfaces import IIntelliSenseMasterController

# Safe imports - analysis result DTOs (data classes, less likely to cause cycles)
from intellisense.analysis.ocr_results import OCRAnalysisResult  # For type hint
from intellisense.analysis.price_results import PriceAnalysisResult # For type hint
from intellisense.analysis.broker_results import BrokerAnalysisResult # For type hint

# Safe imports - timeline components (used by controller)
from intellisense.timeline.timeline_generator import EpochTimelineGenerator
from intellisense.timeline.generator_config import TimelineGeneratorConfig

if TYPE_CHECKING:
    # Type-only imports for complex components that might cause circular dependencies
    from intellisense.core.application_core_intellisense import IntelliSenseApplicationCore as ApplicationCore
    from intellisense.factories.datasource_factory import DataSourceFactory
    from intellisense.engines.ocr_engine import OCRIntelligenceEngine
    from intellisense.engines.price_engine import PriceIntelligenceEngine
    from intellisense.engines.broker_engine import BrokerIntelligenceEngine
    from intellisense.analysis.testable_components import IntelliSenseTestableOrderRepository, IntelliSenseTestablePositionManager
else:
    # Runtime fallback - use Any for ApplicationCore to avoid import issues
    ApplicationCore = Any

# Runtime imports for concrete implementations needed for instantiation
from intellisense.factories.datasource_factory import DataSourceFactory
from intellisense.engines.ocr_engine import OCRIntelligenceEngine  # Import concrete engine
from intellisense.engines.price_engine import PriceIntelligenceEngine # Import concrete engine
from intellisense.engines.broker_engine import BrokerIntelligenceEngine # Import concrete engine

# Placeholder services for OCR engine initialization
class SnapshotInterpreterServicePlaceholder:
    """Placeholder for SnapshotInterpreterService."""
    def interpret_single_snapshot(self, symbol: str, new_state: dict, pos_data: dict, old_ocr_cost: float, old_ocr_realized: float):
        return {symbol: {"qty": 100, "avg_price": 155.75}}  # Mock interpretation


class PositionManagerPlaceholder:
    """Placeholder for PositionManager."""
    def get_position(self, symbol: str):
        return {"symbol": symbol, "shares": 0, "avg_price": 0, "open": False, "strategy": "closed"}


logger = logging.getLogger(__name__)

class IntelliSenseMasterController(IIntelliSenseMasterController):
    """
    Master controller for IntelliSense operations with Redis-based session persistence.
    Redis is the primary and sole persistence mechanism for session data.
    """

    def __init__(self,
                 application_core: Optional['ApplicationCore'] = None,  # Make optional for lazy loading
                 operation_mode: OperationMode = OperationMode.LIVE,
                 intellisense_config: Optional[IntelliSenseConfig] = None):

        self._app_core = application_core  # Store privately for lazy loading
        self._app_core_config = None  # Store config for lazy initialization
        self._data_source_factory = None  # Lazy-loaded factory
        self._engines = {}  # Component registry for engines
        self._redis_manager = None  # Lazy-loaded Redis persistence

        # Session management
        self.current_session_id: Optional[str] = None
        self.session_registry: Dict[str, 'TestSession'] = {}  # In-memory cache of active/recent sessions
        self.operation_mode = operation_mode
        self.intellisense_config: Optional[IntelliSenseConfig] = intellisense_config
        logger.info(f"IntelliSenseMasterController initializing in {self.operation_mode.name} mode.")

        # Placeholder services from app_core if not using real ones
        self._placeholder_snapshot_interpreter = SnapshotInterpreterServicePlaceholder()
        self._placeholder_position_manager = PositionManagerPlaceholder()

        self.current_session_id: Optional[str] = None
        self.session_registry: Dict[str, TestSession] = {}
        # Redis is now the primary persistence mechanism - no file-based fallback

        # Type hints use interfaces primarily - concrete classes assigned at runtime
        self.ocr_engine: Optional[IOCRIntelligenceEngine] = None
        self.price_engine: Optional[IPriceIntelligenceEngine] = None
        self.broker_engine: Optional[IBrokerIntelligenceEngine] = None

        self.timeline_generator: Optional[EpochTimelineGenerator] = None  # Concrete type, instantiated here
        # self.performance_monitor: Optional[PlaceholderPerformanceMonitor] = None # From Chunk 2

        self._data_source_factory: Optional[IDataSourceFactory] = None  # Interface type
        self._is_replay_active = False # To control the replay loop

        # SME Refinements: Cleanup hooks and signal handling
        self._cleanup_hooks: List[Callable[[], None]] = []  # SME Point 2d
        self._shutdown_requested = False  # SME Point 3
        self._signal_handlers_registered = False  # SME Point 3

        if self.operation_mode == OperationMode.INTELLISENSE:
            if not self.intellisense_config:
                raise ValueError("IntelliSenseConfig required for INTELLISENSE mode.")
            try:
                self._initialize_intellisense_mode()
            except Exception as e:
                raise IntelliSenseInitializationError(f"IntelliSense mode init failed: {e}")
        
        # Initialize Redis persistence
        self._load_sessions_from_persistence()

        logger.info("IntelliSenseMasterController initialized successfully.")

    def _load_sessions_from_persistence(self):
        """Loads session summaries or all session data from Redis into in-memory registry."""
        redis_mgr = self.redis_manager
        if not redis_mgr or not redis_mgr.health_check():
            logger.warning("Cannot load sessions: Redis not available.")
            return

        logger.info("Loading existing sessions from Redis persistence...")
        session_ids = redis_mgr.get_all_session_ids_from_index()
        loaded_count = 0
        for session_id in session_ids:
            session_data_dict = redis_mgr.load_session_data(session_id)
            if session_data_dict:
                try:
                    # Import TestSession here to avoid circular imports
                    from intellisense.core.types import TestSession
                    session_obj = TestSession.from_dict(session_data_dict)
                    self.session_registry[session_id] = session_obj
                    loaded_count += 1
                except Exception as e:
                    logger.error(f"Failed to deserialize session {session_id} from Redis: {e}", exc_info=True)
            else:
                logger.warning(f"Session ID {session_id} found in index but no data loaded from Redis key.")

        if loaded_count > 0:
            logger.info(f"Loaded {loaded_count} sessions from Redis into registry.")
        else:
            logger.info("No existing sessions found or loaded from Redis.")

    def _persist_session(self, session: 'TestSession', expiry_seconds: Optional[int] = None) -> bool:
        """Persists a single session state to Redis."""
        redis_mgr = self.redis_manager
        if not redis_mgr or not redis_mgr.health_check():
            logger.error(f"Cannot persist session {session.session_id}: Redis not available.")
            return False

        logger.debug(f"Persisting session {session.session_id} to Redis...")
        try:
            session_dict = session.to_dict()
            # Default expiry: 7 days for sessions
            default_session_ttl_days = getattr(self.intellisense_config, 'redis_session_ttl_days', 7) if self.intellisense_config else 7
            final_expiry_seconds = expiry_seconds if expiry_seconds is not None else default_session_ttl_days * 24 * 60 * 60

            if redis_mgr.save_session_data(session.session_id, session_dict, final_expiry_seconds):
                logger.info(f"Session {session.session_id} (status: {session.status.name}) persisted to Redis. TTL: {final_expiry_seconds/3600:.1f}h")
                return True
            else:
                logger.error(f"Failed to persist session {session.session_id} to Redis via RedisManager.")
                return False
        except Exception as e:
            logger.error(f"Exception persisting session {session.session_id} to Redis: {e}", exc_info=True)
            return False

    def _load_component(self, component_name):
        """Dynamically load components only when needed"""
        component_map = {
            'app_core': ('intellisense.core.application_core_intellisense', 'IntelliSenseApplicationCore'),
            'data_source': ('intellisense.factories.datasource_factory', 'DataSourceFactory'),
            'ocr_engine': ('intellisense.engines.ocr_engine', 'OCRIntelligenceEngine'),
            'price_engine': ('intellisense.engines.price_engine', 'PriceIntelligenceEngine'),
            'broker_engine': ('intellisense.engines.broker_engine', 'BrokerIntelligenceEngine'),
            'redis_manager': ('intellisense.persistence.redis_manager', 'RedisManager')
        }

        if component_name not in component_map:
            raise ValueError(f"Unknown component: {component_name}")

        mod_name, cls_name = component_map[component_name]
        try:
            module = __import__(mod_name, fromlist=[cls_name])
            component_class = getattr(module, cls_name)
            logger.debug(f"Dynamic-loaded component: {component_name} -> {cls_name}")
            return component_class
        except (ImportError, AttributeError) as e:
            logger.error(f"Critical import failure for {component_name}: {e}")
            return None

    @property
    def app_core(self) -> 'ApplicationCore':
        """Lazy-loaded application core to break import cycles"""
        if self._app_core is None:
            # Use dynamic component loading
            app_core_class = self._load_component('app_core')
            if app_core_class:
                # Use stored config or create minimal config
                config = self._app_core_config or getattr(self, 'intellisense_config', None)
                if config is None:
                    # Create minimal config for testing
                    class MinimalConfig:
                        pass
                    config = MinimalConfig()

                self._app_core = app_core_class(
                    config=config,
                    intellisense_overall_mode_active=(self.operation_mode == OperationMode.INTELLISENSE)
                )
            else:
                print("🚨 Failed to load IntelliSenseApplicationCore - using None")
                self._app_core = None
        return self._app_core

    @property
    def redis_manager(self):
        """Lazy-loaded Redis manager for session persistence"""
        if self._redis_manager is None:
            redis_manager_class = self._load_component('redis_manager')
            if redis_manager_class:
                try:
                    # Try to get default instance first (singleton pattern)
                    self._redis_manager = redis_manager_class.get_default_instance()
                except Exception as e:
                    print(f"⚠️ Failed to get default Redis instance, creating new: {e}")
                    # Fallback to creating new instance
                    self._redis_manager = redis_manager_class()
            else:
                print("🚨 Failed to load RedisManager - persistence disabled")
                self._redis_manager = None
        return self._redis_manager

    def register_cleanup_hook(self, cleanup_func: Callable[[], None]):
        """Register a function to be called during session cleanup."""
        if cleanup_func not in self._cleanup_hooks:
            self._cleanup_hooks.append(cleanup_func)
            logger.debug(f"Registered cleanup hook: {getattr(cleanup_func, '__name__', str(cleanup_func))}")

    def _register_signal_handlers(self):
        """Register signal handlers for graceful shutdown. Idempotent."""
        if self._signal_handlers_registered:
            return

        def signal_handler(signum, frame):
            signal_name = signal.Signals(signum).name
            logger.warning(f"Received signal {signal_name} ({signum}), initiating graceful shutdown...")
            self._shutdown_requested = True  # Signal the replay loop
            if self.current_session_id:
                # Call the internal stop, which handles status updates and cleanup
                self.stop_replay_session_internal(self.current_session_id, TestStatus.STOPPED)
            else:  # No active session, just log and prepare to exit if this controller runs a main loop
                logger.info("No active session to stop during signal handling.")
            # If the controller itself has a main loop, it should check _shutdown_requested to exit.
            # For now, this primarily affects the _replay_loop.

        try:
            signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
            signal.signal(signal.SIGTERM, signal_handler)  # Termination request
            if os.name == 'nt' and hasattr(signal, 'SIGBREAK'):  # Windows specific
                signal.signal(signal.SIGBREAK, signal_handler)
            self._signal_handlers_registered = True
            logger.info("Signal handlers registered for graceful shutdown.")
        except ValueError as e:  # Happens if not in main thread
            logger.warning(f"Could not register signal handlers (likely not in main thread): {e}")
        except Exception as e:
            logger.error(f"Error registering signal handlers: {e}", exc_info=True)

    def _release_data_sources(self, session_id: str):
        """Calls stop() and cleanup() on all data sources of registered engines."""
        logger.info(f"[{session_id}] Releasing data sources for all engines...")
        engines = [
            ("OCR", self.ocr_engine),
            ("Price", self.price_engine),
            ("Broker", self.broker_engine)
        ]

        for engine_name, engine in engines:
            if engine and hasattr(engine, 'data_source') and engine.data_source:
                source = engine.data_source
                try:
                    if source.is_active():
                        source.stop()
                    if hasattr(source, 'cleanup'):
                        source.cleanup()
                    logger.debug(f"Data source for {engine_name} engine released.")
                except Exception as e:
                    logger.error(f"Error releasing data source for {engine_name} engine: {e}", exc_info=True)

    def _flush_performance_metrics(self, session_id: str):
        """Flush performance metrics for the session."""
        logger.info(f"[{session_id}] Flushing performance metrics (placeholder)...")
        # Future implementation: if self.performance_monitor and hasattr(self.performance_monitor, 'flush_metrics'):
        #     try:
        #         self.performance_monitor.flush_metrics()
        #         logger.debug(f"Performance metrics flushed for session {session_id}.")
        #     except Exception as e:
        #         logger.error(f"Error flushing performance metrics: {e}", exc_info=True)
        pass

    def _initialize_intellisense_mode(self):
        logger.info("Initializing IntelliSense mode...")

        # ... (data_source_factory and OCRIntelligenceEngine setup as before) ...
        # ... (real_services_dict population as before, ensuring it now includes 'price_repository') ...
        if not self.intellisense_config: raise IntelliSenseInitializationError("IntelliSenseConfig missing.")
        if not self._data_source_factory: self._data_source_factory = DataSourceFactory(self.app_core, self.intellisense_config)

        # ... (data_source_factory, OCR engine, Price engine setup as before) ...
        # ... (real_services_dict population as before) ...
        # It's critical that real_services_dict['order_repository'] and ['position_manager']
        # are instances of IntelliSenseTestableOrderRepository and IntelliSenseTestablePositionManager
        # if activate_testable_analysis_mode() was called on app_core.

        real_services_dict = {} # Populate this from app_core
        if hasattr(self.app_core, 'get_real_services_for_intellisense'):
            real_services_dict = self.app_core.get_real_services_for_intellisense()
        else: # Fallback
            # Try both attribute names for snapshot interpreter
            snapshot_service = getattr(self.app_core, 'snapshot_interpreter_service', None) or getattr(self.app_core, 'real_snapshot_interpreter', None)
            real_services_dict['snapshot_interpreter_service'] = snapshot_service
            real_services_dict['position_manager'] = getattr(self.app_core, 'position_manager', None)
            real_services_dict['price_repository'] = getattr(self.app_core, 'price_repository', None)
            # For BrokerEngine, ensure event_bus, order_repository, position_manager are available
            # These might need to be the "Testable" versions if direct injection is used.
            real_services_dict['event_bus'] = getattr(self.app_core, 'event_bus', None) # Assuming app_core has event_bus
            # The following should be instances of IntelliSenseTestable... if using the hybrid approach
            real_services_dict['order_repository'] = getattr(self.app_core, 'order_repository', None) # This should be Testable OR
            real_services_dict['position_manager'] = real_services_dict.get('position_manager') # This should be Testable PM


        # Instantiate concrete engines but assign to interface-typed attributes
        ocr_engine_instance = OCRIntelligenceEngine()
        ocr_init_ok = ocr_engine_instance.initialize(
            config=self.intellisense_config.test_session_config.ocr_config,
            data_source_factory=self._data_source_factory,
            snapshot_interpreter_instance=(real_services_dict.get('snapshot_interpreter_service') or self._placeholder_snapshot_interpreter),
            position_manager_instance=(real_services_dict.get('position_manager') or self._placeholder_position_manager)
        )
        if not ocr_init_ok:
            raise IntelliSenseInitializationError("OCR Engine init failed.")
        self.ocr_engine = ocr_engine_instance  # Assign concrete to interface type attribute

        # Price Intelligence Engine
        price_engine_instance = PriceIntelligenceEngine()
        price_engine_init_ok = price_engine_instance.initialize(
            config=self.intellisense_config.test_session_config.price_config,
            data_source_factory=self._data_source_factory,
            real_services=real_services_dict # This dict should provide TestablePriceRepository via 'price_repository' key
        )
        if not price_engine_init_ok:
            raise IntelliSenseInitializationError("PriceIntelligenceEngine initialization failed.")
        self.price_engine = price_engine_instance

        # Broker Intelligence Engine
        broker_engine_instance = BrokerIntelligenceEngine()
        broker_engine_init_ok = broker_engine_instance.initialize(
            config=self.intellisense_config.test_session_config.broker_config,
            data_source_factory=self._data_source_factory,
            real_services=real_services_dict # Passes event_bus, AND (testable) OR & PM
        )
        if not broker_engine_init_ok:
            raise IntelliSenseInitializationError("BrokerIntelligenceEngine initialization failed.")
        self.broker_engine = broker_engine_instance

        tg_config = TimelineGeneratorConfig(speed_factor=self.intellisense_config.test_session_config.speed_factor)
        self.timeline_generator = EpochTimelineGenerator(config=tg_config)
        logger.info("INTELLISENSE mode components initialized (incl. BrokerEngine).")

    def create_test_session(self, session_config: TestSessionConfig) -> str:
        # ... (implementation from Chunk 2 - no changes needed here for Chunk 5 core)
        if self.operation_mode != OperationMode.INTELLISENSE:
            raise IntelliSenseSessionError("Cannot create test session: Not in INTELLISENSE mode.")
        logger.info(f"Attempting to create test session: {session_config.session_name}")
        validation_errors = self._validate_session_config(session_config)
        if validation_errors:
            error_msg = f"Session configuration validation failed: {'; '.join(validation_errors)}"
            logger.error(error_msg)
            raise IntelliSenseSessionError(error_msg)
        session_id = f"is_session_{uuid.uuid4().hex[:12]}"
        try:
            test_session = TestSession(session_id=session_id, config=session_config, status=TestStatus.PENDING)
            self.session_registry[session_id] = test_session
            self._persist_session(test_session)
            logger.info(f"Test session '{test_session.config.session_name}' (ID: {session_id}) created.")
            return session_id
        except Exception as e:
            logger.error(f"Failed to create TestSession object or persist: {e}", exc_info=True)
            raise IntelliSenseSessionError(f"Error during session object creation/persistence: {e}")

    def _validate_session_config(self, config: TestSessionConfig) -> List[str]:
        # ... (implementation from Chunk 2)
        errors = []
        if not config.session_name: errors.append("Session name empty.")
        # ... add more checks
        return errors

    def load_session_data(self, session_id: str) -> bool:
        """
        Loads data for the specified session into the relevant replay sources
        by instructing engines to setup for the session.
        Engines create data sources via factory, then sources load their data.
        Loaded sources are registered with the timeline_generator.
        """
        # Initial checks for mode, session, components
        if self.operation_mode != OperationMode.INTELLISENSE:  # Guard
            logger.error(f"[{session_id}] Cannot load session data: Not in INTELLISENSE mode.")
            return False

        session = self.session_registry.get(session_id)
        if not session:
            logger.error(f"[{session_id}] Session not found for data loading.")
            return False

        if not all([self.timeline_generator, self.ocr_engine, self.price_engine, self.broker_engine]):
            msg = f"[{session_id}] Cannot load data: Core IntelliSense components (generator/engines) not initialized."
            logger.error(msg)
            session.update_status(TestStatus.ERROR, msg)
            self._persist_session(session)
            return False

        session.update_status(TestStatus.DATA_LOADING)
        logger.info(f"[{session_id}] Loading data via Intelligence Engines...")

        all_sources_loaded_successfully = True  # Track overall success

        # Setup OCR Engine and its data source
        if self.ocr_engine:
            if not self.ocr_engine.setup_for_session(session.config):  # Engine creates & loads its source
                logger.error(f"[{session_id}] Failed to setup OCR engine (or load its data).")
                all_sources_loaded_successfully = False
            elif self.ocr_engine.data_source:
                self.timeline_generator.register_replay_source("ocr", self.ocr_engine.data_source)
            else:  # Setup_for_session succeeded but no data_source somehow (shouldn't happen if setup_for_session is robust)
                logger.error(f"[{session_id}] OCR engine setup reported success but no data source was available for registration.")
                all_sources_loaded_successfully = False
        else:  # Should not happen if _initialize_intellisense_mode worked
            logger.error(f"[{session_id}] OCR Engine is None during load_session_data.")
            all_sources_loaded_successfully = False

        # Price Engine setup
        if self.price_engine:
            if not self.price_engine.setup_for_session(session.config):
                logger.warning(f"[{session_id}] Failed to setup Price engine for session.")
                # Decide if this is fatal or can continue with other sources
            elif self.price_engine.data_source:
                self.timeline_generator.register_replay_source("price_sync", self.price_engine.data_source) # Be specific if it's sync feed
            # TODO: Add registration for stress price feed source if PriceEngine manages a second one

        # Broker Engine setup
        if self.broker_engine:
            if not self.broker_engine.setup_for_session(session.config):
                logger.warning(f"[{session_id}] Failed to setup Broker engine.")
            elif self.broker_engine.data_source:
                self.timeline_generator.register_replay_source("broker", self.broker_engine.data_source)
        # ... (rest of method) ...

        # ... (Return True/False based on success of loading *critical* sources like OCR)
        all_critical_loaded = self.ocr_engine and self.ocr_engine.data_source and self.ocr_engine.data_source.timeline_data
        if all_critical_loaded:
            session.update_status(TestStatus.READY, "Data loaded, ready for replay."); self._persist_session(session); return True
        else:
            session.update_status(TestStatus.FAILED, "Failed to load critical data."); self._persist_session(session); return False

    def start_replay_session(self, session_id: str) -> bool: # Renamed from start_test_session for clarity
        """
        Starts the replay for a loaded and prepared test session.
        """
        if self.operation_mode != OperationMode.INTELLISENSE:
            logger.error(f"[{session_id}] Cannot start replay: Not in INTELLISENSE mode.")
            return False
        if self.current_session_id is not None and self.current_session_id != session_id : # Check if another session is active
            logger.error(f"[{session_id}] Cannot start replay: Another session ({self.current_session_id}) is already active.")
            return False

        session = self.session_registry.get(session_id)
        if not session:
            logger.error(f"[{session_id}] Session not found.")
            return False
        if session.status != TestStatus.READY:
            logger.error(f"[{session_id}] Session not ready for replay. Status: {session.status.name}")
            return False
        if not self.timeline_generator or not self.ocr_engine or not self.price_engine or not self.broker_engine: # Ensure components are initialized
            logger.error(f"[{session_id}] Cannot start replay: Core IntelliSense components not initialized.")
            session.update_status(TestStatus.ERROR, "Core components missing")
            self._persist_session(session)
            return False

        # Register signal handlers when first replay session starts
        if not self._signal_handlers_registered and self.operation_mode == OperationMode.INTELLISENSE:
            self._register_signal_handlers()

        self.current_session_id = session_id
        session.update_status(TestStatus.RUNNING)
        session.start_time = time.time()
        self._is_replay_active = True # Controller's replay flag
        self._shutdown_requested = False  # Reset for new session
        logger.info(f"[{session_id}] Starting replay. Speed: {session.config.speed_factor}x")
        self._persist_session(session)

        # Start engines (they might start their internal data source streaming if designed that way)
        self.ocr_engine.start()
        self.price_engine.start()
        self.broker_engine.start()
        # if self.performance_monitor: self.performance_monitor.start_monitoring_session(session_id, session.config)

        # Create and run replay loop in a new thread to not block controller
        replay_thread = threading.Thread(target=self._replay_loop, args=(session,), daemon=True, name=f"ReplayThread-{session_id}")
        replay_thread.start()

        return True

    def _replay_loop(self, session: TestSession):
        """Internal method to run the replay event loop."""
        if not self.timeline_generator: return # Should not happen if initialized correctly

        timeline_iterator = self.timeline_generator.create_master_timeline()

        previous_perf_counter_ts: Optional[float] = None
        events_processed = 0
        total_events = self.timeline_generator.get_timeline_stats().get('total_events', 0)

        # Handle start_time_offset
        if self.timeline_generator.config.start_time_offset_s > 0:
            logger.info(f"[{session.session_id}] Applying start time offset of {self.timeline_generator.config.start_time_offset_s}s.")
            time.sleep(self.timeline_generator.config.start_time_offset_s / session.config.speed_factor)


        for event in timeline_iterator:
            if not self._is_replay_active or self._shutdown_requested or self.current_session_id != session.session_id:
                logger.info(f"[{session.session_id}] Replay loop interrupted (active: {self._is_replay_active}, shutdown_req: {self._shutdown_requested}, session_changed: {self.current_session_id != session.session_id}).")
                break

            if event.perf_counter_timestamp is not None:
                if previous_perf_counter_ts is not None:
                    delay_s = event.perf_counter_timestamp - previous_perf_counter_ts
                    if delay_s < 0: delay_s = 0 # Should not happen if sorted correctly

                    actual_sleep_s = delay_s / session.config.speed_factor
                    if actual_sleep_s > 0.0001: # Only sleep if delay is meaningful
                        time.sleep(actual_sleep_s)

                previous_perf_counter_ts = event.perf_counter_timestamp
            else: # No perf_counter_timestamp, yield immediately relative to last event
                previous_perf_counter_ts = None # Reset so next event doesn't calculate delay from this one

            # Dispatch event to appropriate engine/handler
            self._dispatch_event(session, event)
            events_processed += 1
            if total_events > 0:
                session.progress_percent = (events_processed / total_events) * 100

        # Replay finished or stopped
        if self._is_replay_active and self.current_session_id == session.session_id : # If it wasn't stopped externally
            session.update_status(TestStatus.COMPLETED)
            session.end_time = time.time()
            logger.info(f"[{session.session_id}] Replay completed. Processed {events_processed} events.")

        # Ensure cleanup actions occur
        self.stop_replay_session_internal(session.session_id, final_status_if_not_set=session.status)


    def _dispatch_event(self, session: TestSession, event: TimelineEvent):
        # ... (OCR and Price dispatch) ...
        try:
            # ...
            if isinstance(event, OCRTimelineEvent):
                if self.ocr_engine:
                    analysis_result = self.ocr_engine.process_event(event)
                    self._handle_ocr_analysis_result(session, analysis_result)
            elif isinstance(event, PriceTimelineEvent):
                if self.price_engine:
                    analysis_result = self.price_engine.process_event(event)
                    self._handle_price_analysis_result(session, analysis_result)
                else:
                    logger.warning(f"[{session.session_id}] PriceIntelligenceEngine not available for event.")
            elif isinstance(event, BrokerTimelineEvent):
                if self.broker_engine:
                    analysis_result = self.broker_engine.process_event(event)
                    self._handle_broker_analysis_result(session, analysis_result)
                else:
                    logger.warning(f"[{session.session_id}] BrokerIntelligenceEngine not available for event.")
            # ...
        except Exception as e_dispatch: # ...
            pass

    def _handle_ocr_analysis_result(self, session: TestSession, result: OCRAnalysisResult):
        # ... (Implementation from Chunk 10) ...
        if not result.is_successful: logger.error(f"[{session.session_id}] OCR analysis FAILED for frame {result.frame_number}: {result.analysis_error}"); session.add_error(f"OCR Frame {result.frame_number} Analysis: {result.analysis_error}")
        elif result.validation_result and not result.validation_result.is_valid: logger.warning(f"[{session.session_id}] OCR Validation FAILED: Frame {result.frame_number}, Acc: {result.validation_result.accuracy_score:.2%}, Disc: {result.validation_result.discrepancies[:1]}")
        else: logger.info(f"[{session.session_id}] OCR Analysis OK: Frame {result.frame_number}, Interpreter Lat: {result.interpreter_latency_ns / 1e6 if result.interpreter_latency_ns else 'N/A'}ms, Val Acc: {result.validation_result.accuracy_score:.2% if result.validation_result else 'N/A'}")
        self._persist_session(session)

    def _handle_price_analysis_result(self, session: TestSession, result: PriceAnalysisResult):
        """Handles the result from PriceIntelligenceEngine.process_event."""
        # Placeholder for logging, dashboard updates, etc.
        if not result.is_successful:
            logger.error(f"[{session.session_id}] Price analysis FAILED for {result.symbol} ({result.price_event_type}): {result.analysis_error}")
            session.add_error(f"Price Analysis {result.symbol} ({result.price_event_type}): {result.analysis_error}")
        else:
            logger.info(
                f"[{session.session_id}] Price Analysis OK: {result.symbol} ({result.price_event_type}), "
                f"Repo Injection Latency: {result.replay_price_repo_injection_latency_ns / 1e6 if result.replay_price_repo_injection_latency_ns else 'N/A'}ms"
            )
        self._persist_session(session) # Persist session if errors or progress updated

    def _handle_broker_analysis_result(self, session: TestSession, result: BrokerAnalysisResult):
        """Handles the result from BrokerIntelligenceEngine.process_event."""
        if not result.is_successful:
            logger.error(f"[{session.session_id}] Broker analysis FAILED for order {result.original_order_id} ({result.broker_event_type}): {result.analysis_error}")
            session.add_error(f"Broker Analysis {result.original_order_id} ({result.broker_event_type}): {result.analysis_error}")
        else:
            latency_info = []
            if result.event_bus_publish_latency_ns is not None: latency_info.append(f"EBPubLat: {result.event_bus_publish_latency_ns / 1e6:.2f}ms")
            if result.order_repo_direct_call_latency_ns is not None: latency_info.append(f"ORDirectLat: {result.order_repo_direct_call_latency_ns / 1e6:.2f}ms")
            if result.position_manager_direct_call_latency_ns is not None: latency_info.append(f"PMDirectLat: {result.position_manager_direct_call_latency_ns / 1e6:.2f}ms")

            logger.info(
                f"[{session.session_id}] Broker Analysis OK: Order {result.original_order_id} ({result.broker_event_type}). "
                f"{'; '.join(latency_info)}. "
                f"Valid: {result.validation_result.is_overall_valid if result.validation_result else 'N/A'}"
            )
        self._persist_session(session)

    def stop_replay_session(self, session_id: str) -> bool: # Public method to request stop
        """Requests to stop an active replay session."""
        logger.info(f"[{session_id}] Request received to stop replay session.")
        if self.current_session_id != session_id or not self._is_replay_active:
            logger.warning(f"[{session_id}] Cannot stop: Not active or not the current session.")
            return False

        self.stop_replay_session_internal(session_id, TestStatus.STOPPED)
        return True

    def stop_replay_session_internal(self, session_id: str, final_status_if_not_set: TestStatus):
        """Internal method to handle the mechanics of stopping a session."""
        self._is_replay_active = False # Signal replay loop to stop

        session = self.session_registry.get(session_id)
        if session:
            if session.status == TestStatus.RUNNING : # If it was running and now stopping
                session.update_status(final_status_if_not_set)
                if not session.end_time: session.end_time = time.time()

            logger.info(f"[{session_id}] Stopping associated engines and monitors...")
            if self.ocr_engine: self.ocr_engine.stop()
            if self.price_engine: self.price_engine.stop()
            if self.broker_engine: self.broker_engine.stop()
            # if self.performance_monitor: self.performance_monitor.stop_monitoring_session()

            # SME Refinements: Release data sources and flush metrics
            self._release_data_sources(session_id)
            self._flush_performance_metrics(session_id)

            # Execute registered cleanup hooks (LIFO order)
            logger.info(f"[{session_id}] Executing {len(self._cleanup_hooks)} general cleanup hooks...")
            for cleanup_func in reversed(self._cleanup_hooks):
                try:
                    cleanup_func()
                except Exception as e_hook:
                    logger.error(f"Error in cleanup hook {getattr(cleanup_func, '__name__', 'unknown')}: {e_hook}", exc_info=True)

            # Generate and save report for completed/stopped sessions
            if session.status in [TestStatus.COMPLETED, TestStatus.STOPPED, TestStatus.FAILED, TestStatus.ERROR]:
                try:
                    from intellisense.analysis.report_generator import IntelliSenseReportGenerator  # Lazy/local import
                    report_generator = IntelliSenseReportGenerator(session)
                    # Generate structured data first (populates self.report_data in generator)
                    report_generator.generate_report_data()

                    # Save reports to session's output directory (if configured)
                    if session.config and session.config.output_directory:
                        saved_paths = report_generator.save_reports()
                        if saved_paths.get('text_summary'):
                            session.detailed_report_path = saved_paths['text_summary']  # Store path to main summary
                        logger.info(f"[{session_id}] Report generated and saved: {saved_paths}")
                    else:
                        # If no output directory, perhaps just log the text report
                        logger.info(f"[{session_id}] Report Data Generated (no output_directory to save files):\n{report_generator.get_text_report()}")

                except Exception as e_report:
                    logger.error(f"[{session_id}] Failed to generate or save report: {e_report}", exc_info=True)
                    if hasattr(session, 'add_error'):
                        session.add_error(f"Report generation failed: {e_report}")

            self._persist_session(session)  # Persist final state including report path
            logger.info(f"[{session_id}] Replay session internal stop processed. Final status: {session.status.name}")

        if self.current_session_id == session_id:
            self.current_session_id = None

    # Session management methods
    def get_session_status(self, session_id: str) -> Optional[TestSession]:
        """Retrieves the specified test session from the registry."""
        session = self.session_registry.get(session_id)
        if not session:
            logger.warning(f"Session ID {session_id} not found in registry.")
            return None
        return session  # Returns the full TestSession object

    def list_sessions(self) -> List[TestSession]:
        """Returns a list of all registered test sessions."""
        return list(self.session_registry.values())

    def delete_session(self, session_id: str) -> bool:
        """
        Deletes a test session and its persisted data (e.g., from Redis)
        and any associated on-disk artifacts (like log files if applicable).

        Args:
            session_id: The ID of the session to delete.

        Returns:
            True if deletion was successful or session did not exist.
            False if deletion failed for an existing session (e.g., active session,
                  persistence error).
        """
        logger.info(f"[{session_id}] Attempting to delete session.")

        session_to_delete = self.session_registry.get(session_id)

        # 1. Check if session is active (cannot delete an active replay session)
        if self.current_session_id == session_id and self._is_replay_active:
            error_msg = f"Session '{session_id}' is currently active/replaying and cannot be deleted."
            logger.warning(f"[{session_id}] Cannot delete: {error_msg}")
            if session_to_delete:  # Update status if it was found in registry
                session_to_delete.add_error(f"Deletion failed: {error_msg}")
                # self._persist_session(session_to_delete)  # Optional: persist error state
            # Consider raising a specific exception for API to catch and return 409 Conflict
            # raise IntelliSenseSessionError(error_msg)
            return False  # Indicate failure to API

        # 2. Attempt to delete from Redis persistence
        redis_mgr = self.redis_manager
        redis_delete_successful = False
        if not redis_mgr or not redis_mgr.health_check():
            logger.error(f"[{session_id}] Cannot delete session from Redis: RedisManager not available or unhealthy.")
            # Do not proceed if Redis is down, as we can't guarantee index removal.
            if session_to_delete:
                session_to_delete.add_error("Deletion failed: Redis unavailable.")
            return False

        try:
            # RedisManager.delete_session_data should handle removing from index AND deleting data key
            if redis_mgr.delete_session_data(session_id):
                logger.info(f"[{session_id}] Successfully deleted session data from Redis.")
                redis_delete_successful = True
            else:
                # This means RedisManager reported no keys were deleted, implying it wasn't there
                # or an issue occurred which RedisManager should log.
                logger.info(f"[{session_id}] Session data not found in Redis or already deleted by RedisManager.")
                redis_delete_successful = True  # Treat as success if it's gone from Redis
        except Exception as e:
            logger.error(f"[{session_id}] Exception during Redis deletion for session: {e}", exc_info=True)
            if session_to_delete:
                session_to_delete.add_error(f"Redis deletion error: {e}")
            # If Redis deletion fails, we might choose to not remove from memory or disk yet.
            # For now, we'll proceed to remove from memory if it was there.
            # Critical failure might leave inconsistencies.
            # For V1, if Redis fails, we still try to clean up memory/disk if the user expects it gone.

        # 3. Remove from in-memory registry
        session_popped_from_registry = self.session_registry.pop(session_id, None)
        if session_popped_from_registry:
            logger.info(f"[{session_id}] Removed session from in-memory registry.")
            # If this was the current_session_id (but not active replay), clear it
            if self.current_session_id == session_id:
                self.current_session_id = None
        else:
            logger.info(f"[{session_id}] Session not found in in-memory registry (may have already been deleted or never loaded).")

        # 4. Delete on-disk session directory (logs, images)
        # The session_path is usually derived when the session is created or loaded.
        # If we popped it from registry, use its config. If not, we need a way to find its path.
        # Assuming PDS sets session.config.output_directory or we use a standard base.
        # For Redis-only loaded sessions, the config might have come from Redis.

        session_disk_path_to_delete = None
        if session_popped_from_registry and session_popped_from_registry.config and session_popped_from_registry.config.output_directory:
            # Path structure is usually DEFAULT_CAPTURE_BASE_DIR / session_name (which is session_id)
            # If output_directory in config IS the full session path:
            session_disk_path_to_delete = session_popped_from_registry.config.output_directory
        elif session_popped_from_registry and session_popped_from_registry.config and \
             hasattr(session_popped_from_registry.config, 'session_name') and \
             isinstance(getattr(session_popped_from_registry, 'DEFAULT_CAPTURE_BASE_DIR', None), str):  # Check PDS variable
            # This is less ideal as TestSession shouldn't know about PDS internals
            # Better: Controller knows the base path.
            # For now, let's assume output_directory in TestSessionConfig is the full path to the session's data.
            pass  # Already covered by above

        # Fallback: Construct path if possible (needs standard base dir for controller)
        # This part is tricky if TestSession object isn't available or doesn't have output_dir.
        # Best if RedisManager stored a "disk_path" attribute when session was created by PDS,
        # or PDS sets config.output_directory to the full session_path.
        # Let's assume config.output_directory IS the session_path for logged data.

        if session_disk_path_to_delete:
            if os.path.exists(session_disk_path_to_delete):
                try:
                    shutil.rmtree(session_disk_path_to_delete)
                    logger.info(f"[{session_id}] Successfully deleted on-disk session directory: {session_disk_path_to_delete}")
                except Exception as e_disk:
                    logger.error(f"[{session_id}] Failed to delete on-disk session directory {session_disk_path_to_delete}: {e_disk}", exc_info=True)
                    # Non-fatal for the overall delete operation's success status if Redis was cleared,
                    # but should be logged as an issue.
                    # We could return False here if disk cleanup is mandatory for "success".
                    # For V1, let's say if Redis is clear, it's mostly a success.
            else:
                logger.info(f"[{session_id}] No on-disk session directory found at {session_disk_path_to_delete} to delete.")
        else:
            logger.warning(f"[{session_id}] Could not determine on-disk session directory path for deletion. Disk cleanup skipped.")

        # Overall success if Redis was cleared successfully, regardless of whether it was in memory or on disk previously.
        return redis_delete_successful

    def get_session_results_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a summary of execution results for a session.
        """
        session = self.session_registry.get(session_id)
        if not session:
            logger.warning(f"Session ID {session_id} not found for results summary.")
            return None

        # Build results summary
        summary = {
            "session_id": session_id,
            "session_name": session.config.session_name if session.config else "Unknown",
            "status": session.status.name,
            "start_time": session.start_time,
            "end_time": session.end_time,
            "duration_seconds": (session.end_time - session.start_time) if (session.start_time and session.end_time) else None,
            "error_message": session.error_message,
            "events_processed": len(session.timeline_events) if hasattr(session, 'timeline_events') else 0,
            "analysis_results": {
                "ocr_results": len(session.ocr_analysis_results) if hasattr(session, 'ocr_analysis_results') else 0,
                "price_results": len(session.price_analysis_results) if hasattr(session, 'price_analysis_results') else 0,
                "broker_results": len(session.broker_analysis_results) if hasattr(session, 'broker_analysis_results') else 0
            }
        }

        return summary

    def shutdown(self) -> None:
        """
        Performs graceful shutdown of the master controller and all managed sessions.
        """
        logger.info("IntelliSenseMasterController shutdown initiated...")
        
        # First, shutdown the ApplicationCore instance as per Work Package 3
        logger.info(f"MasterController: Initiating shutdown for ApplicationCore instance.")
        if hasattr(self, '_app_core') and self._app_core:
            if hasattr(self._app_core, 'stop'):
                # The stop() method in application_core.py handles all the complex cleanup.
                self._app_core.stop()
                logger.info("MasterController: ApplicationCore stop() method has been called.")
            else:
                logger.error("MasterController: 'application_core' has no 'stop' method!")
        else:
            logger.warning("MasterController: No 'application_core' instance found to shut down.")

        # Stop any active sessions
        if self.current_session_id:
            logger.info(f"Stopping active session: {self.current_session_id}")
            self.stop_replay_session(self.current_session_id)

        # Stop all engines
        if self.ocr_engine:
            try:
                self.ocr_engine.stop()
                logger.debug("OCR engine stopped.")
            except Exception as e:
                logger.error(f"Error stopping OCR engine: {e}")

        if self.price_engine:
            try:
                self.price_engine.stop()
                logger.debug("Price engine stopped.")
            except Exception as e:
                logger.error(f"Error stopping Price engine: {e}")

        if self.broker_engine:
            try:
                self.broker_engine.stop()
                logger.debug("Broker engine stopped.")
            except Exception as e:
                logger.error(f"Error stopping Broker engine: {e}")

        # Execute all cleanup hooks
        logger.info(f"Executing {len(self._cleanup_hooks)} cleanup hooks...")
        for cleanup_func in reversed(self._cleanup_hooks):
            try:
                cleanup_func()
            except Exception as e:
                logger.error(f"Error in cleanup hook: {e}")

        # Close Redis connection if available
        if self._redis_manager:
            try:
                # Assuming RedisManager has a close/shutdown method
                if hasattr(self._redis_manager, 'close'):
                    self._redis_manager.close()
                logger.debug("Redis manager closed.")
            except Exception as e:
                logger.error(f"Error closing Redis manager: {e}")

        logger.info("IntelliSenseMasterController shutdown completed.")

    # File-based persistence methods removed - Redis is now the primary persistence mechanism
