"""
Health Monitor Service

This service manages system health monitoring, including:
- Core health monitoring loop
- Health metrics collection  
- Health data publishing to Redis streams
- System performance monitoring
- Thread and service status tracking

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, Callable
from threading import Lock

from interfaces.core.services import ILifecycleService
# Import our custom interface (will be relative path when integrated)
from interfaces.core.services import IHealthMonitor, IIPCManager

logger = logging.getLogger(__name__)


class HealthMonitor(ILifecycleService, IHealthMonitor):
    """
    Manages system health monitoring and reporting.
    
    This service encapsulates all health monitoring logic that was
    previously scattered throughout ApplicationCore.
    """
    
    def __init__(self,
                 config_service: Any,
                 ipc_manager: IIPCManager,
                 performance_benchmarker: Optional[Any] = None,
                 application_core_ref: Optional[Any] = None):
        """
        Initialize the Health Monitor.
        
        Args:
            config_service: Global configuration object
            ipc_manager: IPC manager for publishing health data
            performance_benchmarker: Performance benchmarker instance
            application_core_ref: Reference to ApplicationCore for health checks
        """
        self.logger = logger
        self.config = config_service
        self.ipc_manager = ipc_manager
        self.performance_benchmarker = performance_benchmarker
        self.application_core_ref = application_core_ref
        
        # Health monitoring attributes
        self._health_monitor_thread: Optional[threading.Thread] = None
        self._health_monitor_stop_event = threading.Event()
        self._is_running = False
        self._lock = Lock()
        
        # Configuration
        self.health_interval = float(getattr(self.config, 'core_health_monitor_interval_sec', 8.0))
        self.target_health_stream = getattr(self.config, 'redis_stream_core_health', 'testrade:health:core')
        
        # State tracking
        self._iteration_count = 0
        self._start_time = None
        self._last_health_check = None
        
        # Health check registry
        self._health_checks: Dict[str, Callable[[], Dict[str, Any]]] = {}
        self._register_default_health_checks()
        
    @property
    def is_ready(self) -> bool:
        """Check if the health monitor is ready."""
        with self._lock:
            return self._is_running and self._health_monitor_thread is not None
    
    def start(self) -> None:
        """Start the health monitor."""
        with self._lock:
            if self._is_running:
                self.logger.warning("HealthMonitor already running")
                return
                
            self._is_running = True
            self._start_time = time.time()
            self._iteration_count = 0
            
            # Start health monitoring thread
            self._start_core_health_monitor()
            
            self.logger.info("HealthMonitor started successfully")
    
    def stop(self) -> None:
        """Stop the health monitor."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            
            # Stop health monitor thread
            self._health_monitor_stop_event.set()
            if self._health_monitor_thread and self._health_monitor_thread.is_alive():
                self.logger.info("Stopping health monitor thread...")
                self._health_monitor_thread.join(timeout=2.0)
                
            self._health_monitor_thread = None
            self.logger.info("HealthMonitor stopped")
    
    def _start_core_health_monitor(self):
        """Start the core health monitoring loop."""
        try:
            self._health_monitor_stop_event.clear()
            self._health_monitor_thread = threading.Thread(
                target=self._core_health_monitor_loop,
                daemon=True,
                name="HealthMonitorThread"
            )
            self._health_monitor_thread.start()
            self.logger.info("Health monitor thread started successfully")
        except Exception as e:
            self.logger.error(f"Failed to start health monitor thread: {e}", exc_info=True)
    
    def _core_health_monitor_loop(self):
        """Core health monitoring loop with comprehensive health checks."""
        self.logger.info("Health monitor loop started")
        
        while not self._health_monitor_stop_event.is_set():
            try:
                self._iteration_count += 1
                
                # Collect comprehensive health status
                health_status = self._collect_comprehensive_health_metrics()
                
                # Update last check time
                self._last_health_check = time.time()
                
                # Publish health data
                self._publish_health_data(health_status)
                
            except Exception as e:
                self.logger.error(f"Health monitor loop error: {e}", exc_info=True)
                
            # Wait for next iteration
            self._health_monitor_stop_event.wait(self.health_interval)
            
        self.logger.info("Health monitor loop stopped")
    
    def _collect_comprehensive_health_metrics(self) -> Dict[str, Any]:
        """Collect comprehensive health metrics from all registered checks."""
        current_time = time.time()
        
        health_status = {
            "health_check_id": f"core_health_{int(current_time)}_{self._iteration_count}",
            "status": "healthy",  # Will be updated based on checks
            "timestamp": current_time,
            "component": "ApplicationCore",
            "metrics": {
                "iteration": self._iteration_count,
                "monitor_thread_alive": True,
                "uptime_seconds": current_time - self._start_time if self._start_time else 0,
                "health_interval_sec": self.health_interval
            },
            "checks": {}
        }
        
        # Run all registered health checks
        overall_healthy = True
        for check_name, check_func in self._health_checks.items():
            try:
                check_result = check_func()
                health_status["checks"][check_name] = check_result
                
                # Update overall status
                if check_result.get("status") != "healthy":
                    overall_healthy = False
                    
            except Exception as e:
                self.logger.error(f"Health check '{check_name}' failed: {e}", exc_info=True)
                health_status["checks"][check_name] = {
                    "status": "error",
                    "error": str(e)
                }
                overall_healthy = False
        
        # Set overall status
        health_status["status"] = "healthy" if overall_healthy else "unhealthy"
        
        return health_status
    
    def _register_default_health_checks(self):
        """Register default health checks."""
        self._health_checks.update({
            "ipc_manager": self._check_ipc_manager_health,
            "memory_usage": self._check_memory_usage,
            "thread_count": self._check_thread_count,
            "application_core": self._check_application_core_health,
            "performance_benchmarker": self._check_performance_benchmarker_health
        })
    
    def register_health_check(self, name: str, check_func: Callable[[], Dict[str, Any]]):
        """
        Register a custom health check.
        
        Args:
            name: Name of the health check
            check_func: Function that returns health status dict
        """
        self._health_checks[name] = check_func
        self.logger.info(f"Registered health check: {name}")
    
    def unregister_health_check(self, name: str):
        """
        Unregister a health check.
        
        Args:
            name: Name of the health check to remove
        """
        if name in self._health_checks:
            del self._health_checks[name]
            self.logger.info(f"Unregistered health check: {name}")
    
    def _check_ipc_manager_health(self) -> Dict[str, Any]:
        """Check IPC Manager health."""
        try:
            if self.ipc_manager and hasattr(self.ipc_manager, 'is_ready'):
                is_ready = self.ipc_manager.is_ready
                return {
                    "status": "healthy" if is_ready else "unhealthy",
                    "is_ready": is_ready,
                    "details": "IPC Manager operational" if is_ready else "IPC Manager not ready"
                }
            else:
                return {
                    "status": "unhealthy",
                    "error": "IPC Manager not available"
                }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _check_memory_usage(self) -> Dict[str, Any]:
        """Check memory usage if psutil is available."""
        try:
            import psutil
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_percent = process.memory_percent()
            
            # Consider unhealthy if using more than 80% of available memory
            status = "healthy" if memory_percent < 80.0 else "warning"
            
            return {
                "status": status,
                "memory_rss_mb": memory_info.rss / (1024 * 1024),
                "memory_vms_mb": memory_info.vms / (1024 * 1024),
                "memory_percent": memory_percent
            }
        except ImportError:
            return {
                "status": "info",
                "message": "psutil not available for memory monitoring"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _check_thread_count(self) -> Dict[str, Any]:
        """Check thread count."""
        try:
            thread_count = threading.active_count()
            
            # Consider warning if more than 50 threads
            status = "healthy" if thread_count < 50 else "warning"
            
            return {
                "status": status,
                "active_threads": thread_count,
                "details": f"{thread_count} active threads"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _check_application_core_health(self) -> Dict[str, Any]:
        """Check ApplicationCore health if reference is available."""
        try:
            if not self.application_core_ref:
                return {
                    "status": "info",
                    "message": "ApplicationCore reference not available"
                }
            
            # Check if ApplicationCore is still running
            is_running = getattr(self.application_core_ref, '_is_running', None)
            services_started = getattr(self.application_core_ref, '_core_services_fully_started', None)
            
            if services_started and hasattr(services_started, 'is_set'):
                services_ready = services_started.is_set()
            else:
                services_ready = None
            
            status = "healthy"
            details = []
            
            if is_running is False:
                status = "unhealthy"
                details.append("ApplicationCore not running")
            
            if services_ready is False:
                status = "warning"
                details.append("Services not fully started")
            
            return {
                "status": status,
                "is_running": is_running,
                "services_ready": services_ready,
                "details": "; ".join(details) if details else "ApplicationCore operational"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _check_performance_benchmarker_health(self) -> Dict[str, Any]:
        """Check performance benchmarker health."""
        try:
            if not self.performance_benchmarker:
                return {
                    "status": "info",
                    "message": "Performance benchmarker not available"
                }
            
            # Check if benchmarker has a health check method
            if hasattr(self.performance_benchmarker, 'is_healthy'):
                is_healthy = self.performance_benchmarker.is_healthy()
                return {
                    "status": "healthy" if is_healthy else "unhealthy",
                    "is_healthy": is_healthy
                }
            else:
                return {
                    "status": "healthy",
                    "message": "Performance benchmarker available"
                }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    def _publish_health_data(self, health_data: Dict[str, Any]):
        """Publish health data via IPC Manager."""
        try:
            if not self.ipc_manager:
                self.logger.error("IPC Manager not available for health publishing")
                return
            
            # Use IPC Manager to publish health data
            success = self.ipc_manager.publish_to_redis(
                stream_name=self.target_health_stream,
                payload=health_data,
                event_type="TESTRADE_CORE_HEALTH_STATUS",
                correlation_id=health_data.get("health_check_id", "health_unknown"),
                source_component="HealthMonitor"
            )
            
            if not success:
                self.logger.warning("Health data publishing returned False - may be buffered or in TANK mode")
                
        except Exception as e:
            self.logger.error(f"Error publishing health data: {e}", exc_info=True)
    
    def get_health_status(self) -> Dict[str, Any]:
        """Get current health status on demand."""
        if not self._is_running:
            return {
                "status": "stopped",
                "message": "Health monitor not running"
            }
        
        return self._collect_comprehensive_health_metrics()
    
    def get_health_summary(self) -> Dict[str, Any]:
        """Get a summary of health monitor status."""
        with self._lock:
            return {
                "is_running": self._is_running,
                "is_ready": self.is_ready,
                "iteration_count": self._iteration_count,
                "health_interval_sec": self.health_interval,
                "last_check": self._last_health_check,
                "uptime_seconds": time.time() - self._start_time if self._start_time else 0,
                "registered_checks": list(self._health_checks.keys())
            }