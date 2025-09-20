"""
IntelliSense Master Controller with Redis-based Isolation

This version maintains complete isolation from TESTRADE by using
Redis commands for all interactions.
"""

import uuid
import time
import logging
import threading
from typing import Dict, Any, Optional, List
import redis

from intellisense.core.enums import OperationMode, TestStatus
from intellisense.core.types import IntelliSenseConfig, TestSession, TestSessionConfig
from intellisense.core.exceptions import IntelliSenseInitializationError, IntelliSenseSessionError
from intellisense.controllers.interfaces import IIntelliSenseMasterController
from intellisense.factories.datasource_factory import DataSourceFactory
from intellisense.timeline.timeline_generator import EpochTimelineGenerator
from intellisense.timeline.generator_config import TimelineGeneratorConfig

# Use Redis-based engines
from intellisense.engines.ocr_engine_redis import OCRIntelligenceEngineRedis

logger = logging.getLogger(__name__)


class IntelliSenseMasterControllerRedis(IIntelliSenseMasterController):
    """
    Master controller that uses Redis for all TESTRADE interactions.
    No direct component dependencies - complete isolation.
    """
    
    def __init__(self,
                 operation_mode: OperationMode = OperationMode.INTELLISENSE,
                 intellisense_config: Optional[IntelliSenseConfig] = None,
                 redis_config: Optional[Dict[str, Any]] = None):
        """
        Initialize the Redis-isolated master controller.
        
        Args:
            operation_mode: Operation mode (should be INTELLISENSE)
            intellisense_config: IntelliSense configuration
            redis_config: Redis connection configuration
        """
        self.operation_mode = operation_mode
        self.intellisense_config = intellisense_config
        self.redis_config = redis_config or {
            "host": "172.22.202.120",
            "port": 6379,
            "db": 0
        }
        
        # Redis client
        self.redis_client: Optional[redis.Redis] = None
        
        # Session management
        self.current_session_id: Optional[str] = None
        self.session_registry: Dict[str, TestSession] = {}
        
        # Components - all Redis-based
        self.ocr_engine: Optional[OCRIntelligenceEngineRedis] = None
        self.data_source_factory: Optional[DataSourceFactory] = None
        self.timeline_generator: Optional[EpochTimelineGenerator] = None
        
        self._is_replay_active = False
        self._shutdown_requested = False
        
        # Cleanup hooks
        self._cleanup_hooks: List[Any] = []
        
        logger.info(f"IntelliSenseMasterControllerRedis initializing in {self.operation_mode.name} mode")
        
        if self.operation_mode == OperationMode.INTELLISENSE:
            if not self.intellisense_config:
                raise ValueError("IntelliSenseConfig required for INTELLISENSE mode")
            try:
                self._initialize_redis_mode()
            except Exception as e:
                raise IntelliSenseInitializationError(f"Redis mode init failed: {e}")
        
        logger.info("IntelliSenseMasterControllerRedis initialized successfully")
    
    def _initialize_redis_mode(self):
        """Initialize components in Redis-isolated mode."""
        logger.info("Initializing Redis-isolated IntelliSense mode...")
        
        # Connect to Redis
        try:
            self.redis_client = redis.Redis(**self.redis_config, decode_responses=True)
            self.redis_client.ping()
            logger.info(f"Connected to Redis at {self.redis_config['host']}:{self.redis_config['port']}")
        except Exception as e:
            raise IntelliSenseInitializationError(f"Failed to connect to Redis: {e}")
        
        # Create data source factory (still needed for timeline data)
        self.data_source_factory = DataSourceFactory(None, self.intellisense_config)
        
        # Initialize Redis-based OCR Engine
        self.ocr_engine = OCRIntelligenceEngineRedis()
        ocr_init_ok = self.ocr_engine.initialize(
            config=self.intellisense_config.test_session_config.ocr_config,
            data_source_factory=self.data_source_factory,
            redis_client=self.redis_client
        )
        if not ocr_init_ok:
            raise IntelliSenseInitializationError("OCR Engine (Redis mode) init failed")
        
        # Timeline generator for replay orchestration
        tg_config = TimelineGeneratorConfig(
            speed_factor=self.intellisense_config.test_session_config.speed_factor
        )
        self.timeline_generator = EpochTimelineGenerator(config=tg_config)
        
        logger.info("Redis-isolated IntelliSense mode initialized successfully")
    
    def register_cleanup_hook(self, cleanup_func):
        """
        Register a function to be called during shutdown.
        
        Args:
            cleanup_func: Callable to be executed during cleanup
        """
        if cleanup_func not in self._cleanup_hooks:
            self._cleanup_hooks.append(cleanup_func)
            logger.debug(f"Registered cleanup hook: {getattr(cleanup_func, '__name__', str(cleanup_func))}")
    
    def create_test_session(self, session_config: TestSessionConfig) -> str:
        """Create a new test session."""
        if self.operation_mode != OperationMode.INTELLISENSE:
            raise IntelliSenseSessionError("Cannot create test session: Not in INTELLISENSE mode")
            
        logger.info(f"Creating test session: {session_config.session_name}")
        
        session_id = f"is_session_{uuid.uuid4().hex[:12]}"
        test_session = TestSession(
            session_id=session_id,
            config=session_config,
            status=TestStatus.PENDING
        )
        
        self.session_registry[session_id] = test_session
        logger.info(f"Test session '{test_session.config.session_name}' (ID: {session_id}) created")
        
        return session_id
    
    def load_session_data(self, session_id: str) -> bool:
        """Load data for the session."""
        session = self.session_registry.get(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return False
            
        session.update_status(TestStatus.LOADING_DATA)
        logger.info(f"Loading data for session {session_id}...")
        
        # Setup OCR Engine data source
        if self.ocr_engine and self.ocr_engine.setup_for_session(session.config):
            if self.ocr_engine.data_source:
                self.timeline_generator.register_replay_source("ocr", self.ocr_engine.data_source)
                session.update_status(TestStatus.DATA_LOADED_PENDING_SYNC)
                return True
        
        session.update_status(TestStatus.FAILED, "Failed to load data")
        return False
    
    def start_replay_session(self, session_id: str) -> bool:
        """Start replay for the session."""
        if self.current_session_id:
            logger.error(f"Another session {self.current_session_id} is already active")
            return False
            
        session = self.session_registry.get(session_id)
        if not session or session.status != TestStatus.DATA_LOADED_PENDING_SYNC:
            logger.error(f"Session {session_id} not ready for replay")
            return False
            
        self.current_session_id = session_id
        session.update_status(TestStatus.RUNNING)
        session.start_time = time.time()
        self._is_replay_active = True
        self._shutdown_requested = False
        
        logger.info(f"Starting replay for session {session_id}")
        
        # Start engines
        if self.ocr_engine:
            self.ocr_engine.start()
        
        # Start replay thread
        replay_thread = threading.Thread(
            target=self._replay_loop,
            args=(session,),
            daemon=True,
            name=f"ReplayThread-{session_id}"
        )
        replay_thread.start()
        
        return True
    
    def _replay_loop(self, session: TestSession):
        """Run the replay loop."""
        if not self.timeline_generator:
            return
            
        timeline_iterator = self.timeline_generator.create_master_timeline()
        previous_timestamp = None
        events_processed = 0
        
        for event in timeline_iterator:
            if not self._is_replay_active or self._shutdown_requested:
                break
                
            # Handle timing
            if event.perf_counter_timestamp and previous_timestamp:
                delay_s = event.perf_counter_timestamp - previous_timestamp
                if delay_s > 0:
                    actual_sleep_s = delay_s / session.config.speed_factor
                    if actual_sleep_s > 0.0001:
                        time.sleep(actual_sleep_s)
            
            previous_timestamp = event.perf_counter_timestamp
            
            # Process event through appropriate engine
            if hasattr(event, 'frame_number') and self.ocr_engine:  # OCR event
                self.ocr_engine.process_event(event)
            
            events_processed += 1
        
        # Replay finished
        if self._is_replay_active:
            session.update_status(TestStatus.COMPLETED)
            session.end_time = time.time()
            logger.info(f"Replay completed for session {session.session_id}. Processed {events_processed} events")
        
        self.stop_replay_session_internal(session.session_id)
    
    def stop_replay_session(self, session_id: str) -> bool:
        """Stop replay session."""
        if self.current_session_id != session_id:
            return False
            
        self.stop_replay_session_internal(session_id)
        return True
    
    def stop_replay_session_internal(self, session_id: str):
        """Internal method to stop replay."""
        self._is_replay_active = False
        
        session = self.session_registry.get(session_id)
        if session and session.status == TestStatus.RUNNING:
            session.update_status(TestStatus.STOPPED)
            if not session.end_time:
                session.end_time = time.time()
        
        # Stop engines
        if self.ocr_engine:
            self.ocr_engine.stop()
        
        if self.current_session_id == session_id:
            self.current_session_id = None
            
        logger.info(f"Replay session {session_id} stopped")
    
    def get_session_status(self, session_id: str) -> Optional[TestSession]:
        """Get session status."""
        return self.session_registry.get(session_id)
    
    def list_sessions(self) -> List[TestSession]:
        """List all sessions."""
        return list(self.session_registry.values())
    
    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        if self.current_session_id == session_id and self._is_replay_active:
            logger.warning(f"Cannot delete active session {session_id}")
            return False
            
        if session_id in self.session_registry:
            del self.session_registry[session_id]
            logger.info(f"Session {session_id} deleted")
            return True
            
        return True  # Already gone
    
    def get_session_results_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session results summary."""
        session = self.session_registry.get(session_id)
        if not session:
            return None
            
        ocr_results = []
        if self.ocr_engine:
            ocr_results = self.ocr_engine.get_analysis_results()
            
        return {
            "session_id": session_id,
            "session_name": session.config.session_name,
            "status": session.status.name,
            "ocr_injections": len(ocr_results),
            "injection_mode": "redis",
            "redis_connected": self.redis_client is not None
        }
    
    def shutdown(self):
        """Shutdown the controller."""
        logger.info("IntelliSenseMasterControllerRedis shutting down...")
        
        # Stop active session
        if self.current_session_id:
            self.stop_replay_session(self.current_session_id)
        
        # Execute cleanup hooks
        logger.info(f"Executing {len(self._cleanup_hooks)} cleanup hooks...")
        for cleanup_func in reversed(self._cleanup_hooks):
            try:
                cleanup_func()
            except Exception as e:
                logger.error(f"Error in cleanup hook {getattr(cleanup_func, '__name__', 'unknown')}: {e}")
        
        # Close Redis connection
        if self.redis_client:
            try:
                self.redis_client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis: {e}")
        
        logger.info("IntelliSenseMasterControllerRedis shutdown complete")