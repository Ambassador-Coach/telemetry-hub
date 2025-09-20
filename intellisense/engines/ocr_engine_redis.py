"""
OCR Intelligence Engine with Redis-based Injection

This version uses Redis commands to inject OCR events into TESTRADE,
maintaining complete isolation between systems.
"""

import logging
import time
from typing import List, Dict, Any, Optional
import redis

from intellisense.core.interfaces import IOCRIntelligenceEngine, IOCRDataSource
from intellisense.core.types import OCRTimelineEvent
from intellisense.config.session_config import OCRTestConfig, TestSessionConfig
from intellisense.factories.interfaces import IDataSourceFactory
from intellisense.analysis.ocr_results import OCRAnalysisResult, OCRValidationResult
from intellisense.injection import IntelliSenseRedisInjector

logger = logging.getLogger(__name__)


class OCRIntelligenceEngineRedis(IOCRIntelligenceEngine):
    """
    OCR Intelligence Engine that uses Redis-based injection.
    No direct TESTRADE component dependencies.
    """
    
    def __init__(self):
        self.test_config: Optional[OCRTestConfig] = None
        self.data_source_factory: Optional[IDataSourceFactory] = None
        self.data_source: Optional[IOCRDataSource] = None
        self.redis_injector: Optional[IntelliSenseRedisInjector] = None
        self.redis_client: Optional[redis.Redis] = None
        
        self.analysis_results: List[OCRAnalysisResult] = []
        self._engine_active = False
        self._current_session_id: Optional[str] = None
        
        # Redis streams to monitor for results
        self.ocr_results_stream = "testrade:ocr-interpreted-positions"
        self.position_updates_stream = "testrade:position-updates"
        
        logger.info("OCRIntelligenceEngineRedis instance created (Redis-isolated mode)")
    
    def initialize(self,
                   config: OCRTestConfig,
                   data_source_factory: IDataSourceFactory,
                   redis_client: redis.Redis,
                   **kwargs) -> bool:
        """
        Initialize the engine with Redis client instead of TESTRADE components.
        
        Args:
            config: OCR test configuration
            data_source_factory: Factory for creating data sources
            redis_client: Redis client for injection and monitoring
            **kwargs: Ignored - for compatibility
            
        Returns:
            True if initialization successful
        """
        self.test_config = config
        self.data_source_factory = data_source_factory
        self.redis_client = redis_client
        
        # Create Redis injector
        try:
            self.redis_injector = IntelliSenseRedisInjector(
                redis_client=redis_client,
                command_stream="intellisense:injection-commands"
            )
            logger.info("OCR Intelligence Engine initialized with Redis injection")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Redis injector: {e}")
            return False
    
    def setup_for_session(self, session_config: TestSessionConfig) -> bool:
        """Setup the engine for a specific test session."""
        if not self.data_source_factory or not self.test_config:
            logger.error("OCR Engine not initialized before setup_for_session")
            return False
            
        try:
            # Create data source for timeline events
            self.data_source = self.data_source_factory.create_ocr_data_source(session_config)
            if self.data_source and self.data_source.load_timeline_data():
                self._current_session_id = session_config.session_name
                self.redis_injector.set_session_id(self._current_session_id)
                self.analysis_results = []
                logger.info(f"OCR Engine setup for session: {session_config.session_name}")
                return True
            else:
                logger.error(f"Failed to load OCR timeline for session: {session_config.session_name}")
                return False
        except Exception as e:
            logger.error(f"Failed to setup OCR Engine: {e}", exc_info=True)
            return False
    
    def start(self) -> None:
        """Start the engine."""
        if not self.data_source or not self.redis_injector:
            logger.error("OCR Engine cannot start: not properly initialized")
            self._engine_active = False
            return
            
        if self.data_source:
            self.data_source.start()
        self._engine_active = True
        logger.info(f"OCR Intelligence Engine started for session {self._current_session_id}")
    
    def stop(self) -> None:
        """Stop the engine."""
        self._engine_active = False
        if self.data_source:
            self.data_source.stop()
        logger.info(f"OCR Intelligence Engine stopped for session {self._current_session_id}")
    
    def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        progress = 0.0
        if self.data_source and self.data_source.is_active():
            progress_info = self.data_source.get_replay_progress()
            progress = progress_info.get('progress_percentage_loaded', 0.0)
            
        return {
            "active": self._engine_active,
            "session_id": self._current_session_id,
            "data_source_active": self.data_source.is_active() if self.data_source else False,
            "replay_progress_percent": progress,
            "analysis_results_count": len(self.analysis_results),
            "injection_mode": "redis",
            "redis_connected": self.redis_client is not None
        }
    
    def process_event(self, event: OCRTimelineEvent) -> OCRAnalysisResult:
        """
        Process an OCR timeline event by injecting it via Redis.
        
        Instead of calling TESTRADE components directly, we:
        1. Inject the OCR snapshot via Redis
        2. Monitor Redis for TESTRADE's processing results
        3. Measure end-to-end latencies
        """
        if not self._engine_active:
            return OCRAnalysisResult(
                frame_number=event.frame_number,
                epoch_timestamp_s=event.epoch_timestamp_s,
                perf_counter_timestamp=event.perf_counter_timestamp,
                original_ocr_event_global_sequence_id=event.global_sequence_id,
                analysis_error="OCR Engine not active"
            )
        
        analysis_start_ns = time.perf_counter_ns()
        
        try:
            # Extract position data from event
            # First try to get from event.data.snapshots (from our test data)
            symbol = "UNKNOWN"
            position_data = {}
            
            if hasattr(event.data, 'snapshots') and event.data.snapshots:
                # Get first snapshot (there should only be one per event in our test data)
                first_symbol = list(event.data.snapshots.keys())[0]
                symbol = first_symbol
                snapshot_data = event.data.snapshots[first_symbol]
                if isinstance(snapshot_data, dict):
                    position_data = snapshot_data
            elif event.expected_position:
                # Fallback to expected_position if available
                symbol = event.expected_position.symbol
                position_data = {
                    "shares": event.expected_position.shares,
                    "avg_price": event.expected_position.avg_price,
                    "open": event.expected_position.open,
                    "strategy": event.expected_position.strategy
                }
            
            # Inject OCR snapshot via Redis
            injection_start_ns = time.perf_counter_ns()
            command_id = self.redis_injector.inject_ocr_snapshot(
                frame_number=event.frame_number,
                symbol=symbol,
                position_data=position_data,
                confidence=event.data.ocr_confidence if hasattr(event.data, 'ocr_confidence') and event.data.ocr_confidence else 0.95,
                raw_ocr_data=event.data.snapshots if hasattr(event.data, 'snapshots') else {},
                metadata={
                    "global_sequence_id": event.global_sequence_id,
                    "epoch_timestamp_s": event.epoch_timestamp_s,
                    "perf_counter_timestamp": event.perf_counter_timestamp,
                    "intellisense_session_id": self._current_session_id
                }
            )
            injection_latency_ns = time.perf_counter_ns() - injection_start_ns
            
            # For now, we just record the injection
            # In a full implementation, we would monitor Redis for TESTRADE's response
            # and correlate it back to measure end-to-end latency
            
            result = OCRAnalysisResult(
                frame_number=event.frame_number,
                epoch_timestamp_s=event.epoch_timestamp_s,
                perf_counter_timestamp=event.perf_counter_timestamp,
                original_ocr_event_global_sequence_id=event.global_sequence_id,
                captured_processing_latency_ns=event.processing_latency_ns,
                # Instead of interpreter_latency, we have injection_latency
                interpreter_latency_ns=injection_latency_ns,
                # We'll get these from monitoring TESTRADE's output
                interpreted_positions_dict=None,
                expected_positions_dict={event.expected_position.symbol: event.expected_position.to_dict()} if event.expected_position else {},
                validation_result=None,
                service_versions={"engine": "OCRIntelligenceEngineRedis-1.0", "injection": "Redis"},
                diagnostic_info={"command_id": command_id, "injection_mode": "redis"}
            )
            
            self.analysis_results.append(result)
            
            total_latency_ns = time.perf_counter_ns() - analysis_start_ns
            logger.debug(f"OCR injection for frame {event.frame_number} completed in {total_latency_ns / 1_000_000:.2f}ms")
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing OCR event: {e}", exc_info=True)
            return OCRAnalysisResult(
                frame_number=event.frame_number,
                epoch_timestamp_s=event.epoch_timestamp_s,
                perf_counter_timestamp=event.perf_counter_timestamp,
                original_ocr_event_global_sequence_id=event.global_sequence_id,
                analysis_error=str(e),
                diagnostic_info={"error_type": type(e).__name__}
            )
    
    def get_analysis_results(self) -> List[OCRAnalysisResult]:
        """Get all analysis results."""
        return list(self.analysis_results)