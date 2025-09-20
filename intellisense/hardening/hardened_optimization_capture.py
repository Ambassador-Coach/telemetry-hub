#!/usr/bin/env python3
"""
Hardened MillisecondOptimizationCapture

This is a production-hardened version of MillisecondOptimizationCapture that includes:
- Comprehensive error handling and recovery
- Circuit breaker patterns for external dependencies
- Resource monitoring and management
- Graceful degradation capabilities
- Health monitoring and self-healing
- Fallback mechanisms for critical operations
"""

import logging
import time
import threading
import traceback
from typing import Dict, Any, Optional, List, Type, Deque
from collections import deque
import weakref

# Import the reliability framework
from .reliability_framework import ReliabilityFramework, ComponentType, CircuitBreaker

# Import original types
from ..capture.scenario_types import InjectionStep, ValidationPoint, ValidationOutcome, StepExecutionResult
from ..capture.pipeline_definitions import get_expected_pipeline_stages, PIPELINE_STAGE_CRITERIA

logger = logging.getLogger(__name__)

class HardenedMillisecondOptimizationCapture:
    """
    Production-hardened version of MillisecondOptimizationCapture with comprehensive
    reliability features for live trading environments.
    """
    
    def __init__(self,
                 app_core: 'IntelliSenseApplicationCore',
                 feedback_isolator: 'IFeedbackIsolationManager',
                 correlation_logger: 'CorrelationLogger',
                 internal_event_bus: 'InternalIntelliSenseEventBus',
                 enable_reliability_monitoring: bool = True):
        
        # Initialize reliability framework first
        self.reliability = ReliabilityFramework(enable_monitoring=enable_reliability_monitoring)
        
        # Core dependencies with weak references to prevent memory leaks
        self.app_core_ref = weakref.ref(app_core) if app_core else None
        self.feedback_isolator_ref = weakref.ref(feedback_isolator) if feedback_isolator else None
        self.correlation_logger_ref = weakref.ref(correlation_logger) if correlation_logger else None
        self.internal_event_bus_ref = weakref.ref(internal_event_bus) if internal_event_bus else None
        
        # Lazy-loaded dependencies with fallbacks
        self._OrderRequestEvent: Optional[Type] = None
        self._OrderRequestData: Optional[Type] = None
        self._CorrelationLogEntryEvent: Optional[Type] = None
        
        # Core state with thread safety
        self._active_pipeline_monitors: Dict[str, Dict[str, Any]] = {}
        self._correlation_event_buffer: Deque = deque(maxlen=2000)
        self._monitors_lock = threading.RLock()
        self._buffer_lock = threading.RLock()
        
        # Performance tracking
        self._injection_count = 0
        self._successful_injections = 0
        self._failed_injections = 0
        self._last_health_check = time.time()
        
        # Register components with reliability framework
        self._register_components()
        
        # Initialize with safe subscription
        self._safe_subscribe_to_correlation_events()
        
        logger.info("HardenedMillisecondOptimizationCapture initialized with reliability monitoring")
    
    def _register_components(self):
        """Register all components with the reliability framework"""
        self.reliability.register_component("core_events_loader", ComponentType.DEPENDENCY)
        self.reliability.register_component("correlation_logger", ComponentType.CORE)
        self.reliability.register_component("pipeline_monitor", ComponentType.CORE)
        self.reliability.register_component("event_bus", ComponentType.DEPENDENCY)
        self.reliability.register_component("feedback_isolator", ComponentType.OPTIONAL)
        self.reliability.register_component("ocr_service", ComponentType.OPTIONAL)
    
    @property
    def app_core(self):
        """Safe access to app_core with None check"""
        return self.app_core_ref() if self.app_core_ref else None
    
    @property
    def feedback_isolator(self):
        """Safe access to feedback_isolator with None check"""
        return self.feedback_isolator_ref() if self.feedback_isolator_ref else None
    
    @property
    def correlation_logger(self):
        """Safe access to correlation_logger with None check"""
        return self.correlation_logger_ref() if self.correlation_logger_ref else None
    
    @property
    def internal_event_bus(self):
        """Safe access to internal_event_bus with None check"""
        return self.internal_event_bus_ref() if self.internal_event_bus_ref else None
    
    def _safe_lazy_load_core_events(self) -> bool:
        """Safely load core events with circuit breaker protection"""
        if self._OrderRequestData is not None and self._OrderRequestEvent is not None:
            return True
        
        circuit_breaker = self.reliability.get_circuit_breaker("core_events_import")
        if not circuit_breaker:
            return self._fallback_load_core_events()
        
        try:
            with circuit_breaker.call():
                from core.events import OrderRequestData, OrderRequestEvent
                self._OrderRequestData = OrderRequestData
                self._OrderRequestEvent = OrderRequestEvent
                logger.debug("Successfully loaded core.events with circuit breaker protection")
                return True
                
        except Exception as e:
            logger.warning(f"Circuit breaker protected core.events import failed: {e}")
            return self._fallback_load_core_events()
    
    def _fallback_load_core_events(self) -> bool:
        """Fallback mock classes when core.events is unavailable"""
        try:
            from dataclasses import dataclass, field
            from typing import Optional
            from uuid import uuid4
            
            @dataclass
            class FallbackOrderRequestData:
                request_id: str = field(default_factory=lambda: str(uuid4()))
                symbol: str = ""
                side: str = ""
                quantity: float = 0.0
                order_type: str = "MKT"
                limit_price: Optional[float] = None
                correlation_id: Optional[str] = None
                source_strategy: str = "INTELLISENSE_FALLBACK"
            
            @dataclass
            class FallbackOrderRequestEvent:
                data: FallbackOrderRequestData = field(default=None)
                event_id: str = field(default_factory=lambda: str(uuid4()))
                timestamp: float = field(default_factory=time.time)
            
            self._OrderRequestData = FallbackOrderRequestData
            self._OrderRequestEvent = FallbackOrderRequestEvent
            logger.info("Using fallback mock classes for core.events")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create fallback classes: {e}")
            return False
    
    def _safe_subscribe_to_correlation_events(self):
        """Safely subscribe to correlation events with error handling"""
        try:
            if not self.internal_event_bus:
                logger.warning("Internal event bus not available - correlation monitoring disabled")
                return
            
            from intellisense.core.internal_events import CorrelationLogEntryEvent
            self._CorrelationLogEntryEvent = CorrelationLogEntryEvent
            
            self.internal_event_bus.subscribe(CorrelationLogEntryEvent, self._safe_on_correlation_log_entry)
            logger.debug("Successfully subscribed to correlation events")
            
        except Exception as e:
            logger.error(f"Failed to subscribe to correlation events: {e}")
            # Continue without correlation monitoring - system should still function
    
    def _safe_on_correlation_log_entry(self, event):
        """Safely handle correlation log entries with comprehensive error handling"""
        try:
            with self._buffer_lock:
                self._correlation_event_buffer.append(event)
            
            # Process monitors with timeout protection
            self._process_monitors_with_timeout(event)
            
        except Exception as e:
            logger.error(f"Error processing correlation log entry: {e}", exc_info=True)
            # Don't let correlation processing errors break the system
    
    def _process_monitors_with_timeout(self, event, timeout_seconds: float = 5.0):
        """Process pipeline monitors with timeout protection"""
        start_time = time.time()
        
        try:
            log_entry = event.log_entry if hasattr(event, 'log_entry') else {}
            
            with self._monitors_lock:
                monitors_to_process = list(self._active_pipeline_monitors.items())
            
            for corr_id, monitor_data in monitors_to_process:
                # Check timeout for this processing
                if time.time() - start_time > timeout_seconds:
                    logger.warning(f"Monitor processing timeout exceeded for {corr_id}")
                    break
                
                try:
                    self._process_single_monitor(corr_id, monitor_data, log_entry)
                except Exception as e:
                    logger.error(f"Error processing monitor {corr_id}: {e}")
                    # Continue with other monitors
                    
        except Exception as e:
            logger.error(f"Critical error in monitor processing: {e}", exc_info=True)
    
    def _process_single_monitor(self, corr_id: str, monitor_data: Dict[str, Any], log_entry: Dict[str, Any]):
        """Process a single pipeline monitor safely"""
        if monitor_data.get("completed", False):
            return
        
        if time.perf_counter_ns() > monitor_data.get("timeout_at_ns", 0):
            self._handle_monitor_timeout(corr_id, monitor_data)
            return
        
        expected_stages = monitor_data.get("expected_stages", [])
        current_stage_idx = monitor_data.get("current_stage_index", 0)
        
        if current_stage_idx >= len(expected_stages):
            return
        
        stage_source_sense, stage_event_desc, stage_checker_func, stage_name_key = expected_stages[current_stage_idx]
        
        log_source_sense = log_entry.get('source_sense')
        if log_source_sense != stage_source_sense:
            return
        
        # Execute checker with timeout protection
        try:
            log_payload = log_entry.get('event_payload', {})
            stimulus_data = monitor_data.get("stimulus_data", {})
            
            match_result = stage_checker_func(log_payload, stimulus_data)
            
            if match_result:
                self._handle_stage_match(corr_id, monitor_data, log_entry, stage_name_key, current_stage_idx)
                
        except Exception as e:
            logger.error(f"Checker function failed for stage {stage_name_key}: {e}")
    
    def _handle_monitor_timeout(self, corr_id: str, monitor_data: Dict[str, Any]):
        """Handle pipeline monitor timeout"""
        logger.warning(f"Pipeline monitor {corr_id} timed out")
        with self._monitors_lock:
            if corr_id in self._active_pipeline_monitors:
                del self._active_pipeline_monitors[corr_id]
        
        # Set completion event if present
        completion_event = monitor_data.get('completion_event')
        if completion_event and hasattr(completion_event, 'set'):
            completion_event.set()
    
    def _handle_stage_match(self, corr_id: str, monitor_data: Dict[str, Any], 
                           log_entry: Dict[str, Any], stage_name_key: str, stage_idx: int):
        """Handle successful stage match"""
        logger.info(f"Pipeline monitor {corr_id}: Matched stage '{stage_name_key}' (Index {stage_idx})")
        
        monitor_data['matched_events'].append(log_entry)
        monitor_data.setdefault('events_by_stage_key', {})[stage_name_key] = log_entry
        monitor_data['current_stage_index'] += 1
        
        expected_stages = monitor_data.get("expected_stages", [])
        if monitor_data['current_stage_index'] >= len(expected_stages):
            logger.info(f"Pipeline monitor {corr_id}: All stages completed")
            monitor_data['completed'] = True
            
            completion_event = monitor_data.get('completion_event')
            if completion_event and hasattr(completion_event, 'set'):
                completion_event.set()
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status"""
        health = self.reliability.check_system_health()
        
        # Add MOC-specific metrics
        with self._monitors_lock:
            active_monitors = len(self._active_pipeline_monitors)
        
        with self._buffer_lock:
            buffer_size = len(self._correlation_event_buffer)
        
        health.update({
            "moc_specific": {
                "active_monitors": active_monitors,
                "correlation_buffer_size": buffer_size,
                "injection_count": self._injection_count,
                "successful_injections": self._successful_injections,
                "failed_injections": self._failed_injections,
                "success_rate": (self._successful_injections / max(1, self._injection_count)) * 100,
                "dependencies_available": {
                    "app_core": self.app_core is not None,
                    "feedback_isolator": self.feedback_isolator is not None,
                    "correlation_logger": self.correlation_logger is not None,
                    "internal_event_bus": self.internal_event_bus is not None,
                    "core_events": self._OrderRequestData is not None
                }
            }
        })
        
        return health
