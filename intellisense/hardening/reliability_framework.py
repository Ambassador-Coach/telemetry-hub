#!/usr/bin/env python3
"""
IntelliSense Reliability Framework

This module provides comprehensive reliability and fault tolerance mechanisms
for the IntelliSense system to ensure it can operate robustly in production
trading environments.

Key Features:
- Graceful degradation when dependencies fail
- Circuit breaker patterns for external dependencies
- Health monitoring and self-healing
- Resource usage monitoring
- Fallback mechanisms for critical operations
"""

import logging
import time
import threading
import traceback
import psutil
import gc
from typing import Dict, Any, Optional, Callable, List, Union
from dataclasses import dataclass, field
from enum import Enum
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class HealthStatus(Enum):
    """System health status levels"""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    FAILED = "FAILED"

class ComponentType(Enum):
    """Types of IntelliSense components"""
    CORE = "CORE"
    DEPENDENCY = "DEPENDENCY"
    OPTIONAL = "OPTIONAL"
    EXTERNAL = "EXTERNAL"

@dataclass
class ComponentHealth:
    """Health information for a system component"""
    name: str
    component_type: ComponentType
    status: HealthStatus
    last_check: float
    error_count: int = 0
    last_error: Optional[str] = None
    recovery_attempts: int = 0
    max_recovery_attempts: int = 3
    
    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY
    
    def can_recover(self) -> bool:
        return self.recovery_attempts < self.max_recovery_attempts

@dataclass
class SystemMetrics:
    """System performance and resource metrics"""
    timestamp: float
    cpu_percent: float
    memory_percent: float
    memory_mb: float
    thread_count: int
    active_monitors: int
    correlation_buffer_size: int
    error_rate: float
    
class CircuitBreaker:
    """Circuit breaker pattern for external dependencies"""
    
    def __init__(self, name: str, failure_threshold: int = 5, 
                 recovery_timeout: float = 60.0, call_timeout: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.call_timeout = call_timeout
        
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._lock = threading.Lock()
    
    @contextmanager
    def call(self):
        """Context manager for protected calls"""
        if not self._can_call():
            raise Exception(f"Circuit breaker {self.name} is OPEN")
        
        start_time = time.time()
        try:
            yield
            self._on_success()
        except Exception as e:
            self._on_failure(e)
            raise
        finally:
            duration = time.time() - start_time
            if duration > self.call_timeout:
                logger.warning(f"Circuit breaker {self.name}: Call took {duration:.2f}s (timeout: {self.call_timeout}s)")
    
    def _can_call(self) -> bool:
        with self._lock:
            if self.state == "CLOSED":
                return True
            elif self.state == "OPEN":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "HALF_OPEN"
                    logger.info(f"Circuit breaker {self.name}: Transitioning to HALF_OPEN")
                    return True
                return False
            else:  # HALF_OPEN
                return True
    
    def _on_success(self):
        with self._lock:
            self.failure_count = 0
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                logger.info(f"Circuit breaker {self.name}: Recovered, transitioning to CLOSED")
    
    def _on_failure(self, error: Exception):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
                logger.error(f"Circuit breaker {self.name}: OPENED after {self.failure_count} failures. Error: {error}")

class ReliabilityFramework:
    """Main reliability framework for IntelliSense"""
    
    def __init__(self, enable_monitoring: bool = True):
        self.enable_monitoring = enable_monitoring
        self.components: Dict[str, ComponentHealth] = {}
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self.metrics_history: List[SystemMetrics] = []
        self.max_metrics_history = 1000
        
        self._monitoring_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._lock = threading.Lock()
        
        # Initialize core circuit breakers
        self._init_circuit_breakers()
        
        if enable_monitoring:
            self.start_monitoring()
    
    def _init_circuit_breakers(self):
        """Initialize circuit breakers for critical dependencies"""
        self.circuit_breakers.update({
            "core_events_import": CircuitBreaker("core_events_import", failure_threshold=3, recovery_timeout=30.0),
            "correlation_logging": CircuitBreaker("correlation_logging", failure_threshold=5, recovery_timeout=60.0),
            "pipeline_monitoring": CircuitBreaker("pipeline_monitoring", failure_threshold=10, recovery_timeout=120.0),
            "event_bus_publish": CircuitBreaker("event_bus_publish", failure_threshold=5, recovery_timeout=60.0),
            "ocr_injection": CircuitBreaker("ocr_injection", failure_threshold=3, recovery_timeout=30.0),
        })
    
    def register_component(self, name: str, component_type: ComponentType, 
                          health_check: Optional[Callable[[], bool]] = None):
        """Register a component for health monitoring"""
        with self._lock:
            self.components[name] = ComponentHealth(
                name=name,
                component_type=component_type,
                status=HealthStatus.HEALTHY,
                last_check=time.time()
            )
        logger.info(f"Registered component '{name}' of type {component_type.value}")
    
    def get_circuit_breaker(self, name: str) -> Optional[CircuitBreaker]:
        """Get a circuit breaker by name"""
        return self.circuit_breakers.get(name)
    
    def check_system_health(self) -> Dict[str, Any]:
        """Comprehensive system health check"""
        with self._lock:
            healthy_components = sum(1 for c in self.components.values() if c.is_healthy())
            total_components = len(self.components)
            
            # Get latest metrics
            latest_metrics = self.metrics_history[-1] if self.metrics_history else None
            
            # Determine overall health
            if total_components == 0:
                overall_status = HealthStatus.HEALTHY
            elif healthy_components == total_components:
                overall_status = HealthStatus.HEALTHY
            elif healthy_components >= total_components * 0.8:
                overall_status = HealthStatus.DEGRADED
            elif healthy_components >= total_components * 0.5:
                overall_status = HealthStatus.CRITICAL
            else:
                overall_status = HealthStatus.FAILED
            
            return {
                "overall_status": overall_status.value,
                "healthy_components": healthy_components,
                "total_components": total_components,
                "health_percentage": (healthy_components / total_components * 100) if total_components > 0 else 100,
                "components": {name: comp.status.value for name, comp in self.components.items()},
                "circuit_breakers": {name: cb.state for name, cb in self.circuit_breakers.items()},
                "latest_metrics": latest_metrics.__dict__ if latest_metrics else None,
                "timestamp": time.time()
            }

    def collect_metrics(self) -> SystemMetrics:
        """Collect current system metrics"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()

            return SystemMetrics(
                timestamp=time.time(),
                cpu_percent=process.cpu_percent(),
                memory_percent=process.memory_percent(),
                memory_mb=memory_info.rss / 1024 / 1024,
                thread_count=process.num_threads(),
                active_monitors=0,  # Will be updated by MOC
                correlation_buffer_size=0,  # Will be updated by MOC
                error_rate=self._calculate_error_rate()
            )
        except Exception as e:
            logger.error(f"Failed to collect metrics: {e}")
            return SystemMetrics(
                timestamp=time.time(),
                cpu_percent=0, memory_percent=0, memory_mb=0,
                thread_count=0, active_monitors=0, correlation_buffer_size=0,
                error_rate=0
            )

    def _calculate_error_rate(self) -> float:
        """Calculate current error rate across components"""
        with self._lock:
            if not self.components:
                return 0.0

            total_errors = sum(comp.error_count for comp in self.components.values())
            return total_errors / len(self.components)

    def start_monitoring(self):
        """Start background monitoring thread"""
        if self._monitoring_thread and self._monitoring_thread.is_alive():
            return

        self._shutdown_event.clear()
        self._monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._monitoring_thread.start()
        logger.info("Reliability monitoring started")

    def stop_monitoring(self):
        """Stop background monitoring"""
        self._shutdown_event.set()
        if self._monitoring_thread:
            self._monitoring_thread.join(timeout=5.0)
        logger.info("Reliability monitoring stopped")

    def _monitoring_loop(self):
        """Background monitoring loop"""
        while not self._shutdown_event.wait(10.0):  # Check every 10 seconds
            try:
                # Collect metrics
                metrics = self.collect_metrics()
                with self._lock:
                    self.metrics_history.append(metrics)
                    if len(self.metrics_history) > self.max_metrics_history:
                        self.metrics_history.pop(0)

                # Check for resource issues
                if metrics.memory_percent > 80:
                    logger.warning(f"High memory usage: {metrics.memory_percent:.1f}%")
                    gc.collect()  # Force garbage collection

                if metrics.cpu_percent > 90:
                    logger.warning(f"High CPU usage: {metrics.cpu_percent:.1f}%")

                # Health check components
                self._check_component_health()

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)

    def _check_component_health(self):
        """Check health of all registered components"""
        current_time = time.time()
        with self._lock:
            for component in self.components.values():
                # Simple health check based on error count and time
                if component.error_count > 10:
                    component.status = HealthStatus.CRITICAL
                elif component.error_count > 5:
                    component.status = HealthStatus.DEGRADED
                elif current_time - component.last_check > 300:  # 5 minutes
                    component.status = HealthStatus.DEGRADED
                else:
                    component.status = HealthStatus.HEALTHY

                component.last_check = current_time
