# gui/services/base_service.py - Base Service Class

"""
Base Service Class
==================
Provides common functionality for all services following SOLID principles.

Features:
- Dependency injection support
- Lifecycle management (initialize, shutdown)
- Health checking
- Metrics collection
- Error handling patterns
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class ServiceState(Enum):
    """Service lifecycle states"""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"
    ERROR = "error"


class ServiceHealth:
    """Service health information"""
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.state = ServiceState.UNINITIALIZED
        self.last_health_check = datetime.now()
        self.error_count = 0
        self.last_error: Optional[str] = None
        self.metadata: Dict[str, Any] = {}
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.service_name,
            "state": self.state.value,
            "healthy": self.state == ServiceState.READY,
            "last_check": self.last_health_check.isoformat(),
            "error_count": self.error_count,
            "last_error": self.last_error,
            "metadata": self.metadata
        }


class BaseService(ABC):
    """
    Abstract base class for all services.
    
    Provides:
    - Lifecycle management
    - Health monitoring
    - Dependency injection
    - Error handling
    - Metrics collection
    """
    
    def __init__(self, name: str, config: Dict[str, Any], dependencies: Optional[Dict[str, Any]] = None):
        self.name = name
        self.config = config
        self.dependencies = dependencies or {}
        self.health = ServiceHealth(name)
        self._metrics: Dict[str, Any] = {}
        self._start_time = time.time()
        self._background_tasks: List[asyncio.Task] = []
        
        # Initialize logger with service name
        self.logger = logging.getLogger(f"{__name__}.{name}")
        
    @abstractmethod
    async def _do_initialize(self) -> None:
        """
        Service-specific initialization logic.
        Must be implemented by subclasses.
        """
        pass
        
    @abstractmethod
    async def _do_shutdown(self) -> None:
        """
        Service-specific shutdown logic.
        Must be implemented by subclasses.
        """
        pass
        
    @abstractmethod
    async def _do_health_check(self) -> bool:
        """
        Service-specific health check logic.
        Must be implemented by subclasses.
        
        Returns:
            bool: True if healthy, False otherwise
        """
        pass
        
    async def initialize(self) -> None:
        """Initialize the service with proper error handling"""
        if self.health.state != ServiceState.UNINITIALIZED:
            self.logger.warning(f"Service {self.name} already initialized")
            return
            
        self.health.state = ServiceState.INITIALIZING
        self.logger.info(f"🔧 Initializing {self.name} service...")
        
        try:
            # Validate dependencies
            self._validate_dependencies()
            
            # Service-specific initialization
            await self._do_initialize()
            
            # Start background tasks if any
            await self._start_background_tasks()
            
            self.health.state = ServiceState.READY
            self.logger.info(f"✅ {self.name} service initialized successfully")
            
        except Exception as e:
            self.health.state = ServiceState.ERROR
            self.health.last_error = str(e)
            self.health.error_count += 1
            self.logger.error(f"❌ Failed to initialize {self.name}: {e}", exc_info=True)
            raise
            
    async def shutdown(self) -> None:
        """Shutdown the service gracefully"""
        if self.health.state == ServiceState.SHUTDOWN:
            return
            
        self.health.state = ServiceState.SHUTTING_DOWN
        self.logger.info(f"🔄 Shutting down {self.name} service...")
        
        try:
            # Cancel background tasks
            await self._stop_background_tasks()
            
            # Service-specific shutdown
            await self._do_shutdown()
            
            self.health.state = ServiceState.SHUTDOWN
            self.logger.info(f"✅ {self.name} service shutdown complete")
            
        except Exception as e:
            self.logger.error(f"❌ Error during {self.name} shutdown: {e}", exc_info=True)
            # Still mark as shutdown even with errors
            self.health.state = ServiceState.SHUTDOWN
            
    async def health_check(self) -> ServiceHealth:
        """Perform health check and return status"""
        self.health.last_health_check = datetime.now()
        
        try:
            if self.health.state not in (ServiceState.READY, ServiceState.DEGRADED):
                return self.health
                
            # Service-specific health check
            is_healthy = await self._do_health_check()
            
            if is_healthy:
                self.health.state = ServiceState.READY
                self.health.error_count = 0
                self.health.last_error = None
            else:
                self.health.state = ServiceState.DEGRADED
                
        except Exception as e:
            self.health.state = ServiceState.DEGRADED
            self.health.last_error = str(e)
            self.health.error_count += 1
            self.logger.warning(f"Health check failed for {self.name}: {e}")
            
        return self.health
        
    def _validate_dependencies(self) -> None:
        """Validate that all required dependencies are present"""
        # Override in subclasses to add specific validation
        pass
        
    async def _start_background_tasks(self) -> None:
        """Start any background tasks needed by the service"""
        # Override in subclasses to start background tasks
        pass
        
    async def _stop_background_tasks(self) -> None:
        """Stop all background tasks"""
        if not self._background_tasks:
            return
            
        self.logger.info(f"Stopping {len(self._background_tasks)} background tasks...")
        
        # Cancel all tasks
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
                
        # Wait for cancellation
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        
    def record_metric(self, name: str, value: Any, labels: Optional[Dict[str, str]] = None) -> None:
        """Record a metric value"""
        metric_key = f"{self.name}.{name}"
        if labels:
            metric_key += f"[{','.join(f'{k}={v}' for k, v in labels.items())}]"
            
        self._metrics[metric_key] = {
            "value": value,
            "timestamp": time.time(),
            "labels": labels or {}
        }
        
    def get_metrics(self) -> Dict[str, Any]:
        """Get all recorded metrics"""
        return {
            "service": self.name,
            "uptime_seconds": time.time() - self._start_time,
            "metrics": self._metrics
        }
        
    def get_info(self) -> Dict[str, Any]:
        """Get service information"""
        return {
            "name": self.name,
            "state": self.health.state.value,
            "uptime_seconds": time.time() - self._start_time,
            "error_count": self.health.error_count,
            "last_error": self.health.last_error,
            "dependencies": list(self.dependencies.keys()),
            "config_keys": list(self.config.keys())
        }


class ServiceRegistry:
    """
    Central registry for all services.
    Manages service lifecycle and dependencies.
    """
    
    def __init__(self):
        self.services: Dict[str, BaseService] = {}
        self._initialization_order: List[str] = []
        
    def register(self, service: BaseService) -> None:
        """Register a service"""
        if service.name in self.services:
            raise ValueError(f"Service {service.name} already registered")
            
        self.services[service.name] = service
        self._initialization_order.append(service.name)
        logger.info(f"Registered service: {service.name}")
        
    async def initialize_all(self) -> None:
        """Initialize all services in registration order"""
        logger.info(f"Initializing {len(self.services)} services...")
        
        for service_name in self._initialization_order:
            service = self.services[service_name]
            await service.initialize()
            
    async def shutdown_all(self) -> None:
        """Shutdown all services in reverse order"""
        logger.info(f"Shutting down {len(self.services)} services...")
        
        # Shutdown in reverse order
        for service_name in reversed(self._initialization_order):
            service = self.services[service_name]
            await service.shutdown()
            
    async def health_check_all(self) -> Dict[str, ServiceHealth]:
        """Perform health checks on all services"""
        results = {}
        
        for service_name, service in self.services.items():
            results[service_name] = await service.health_check()
            
        return results
        
    def get_service(self, name: str) -> Optional[BaseService]:
        """Get a service by name"""
        return self.services.get(name)
        
    def get_all_info(self) -> Dict[str, Any]:
        """Get information about all services"""
        return {
            "services": {
                name: service.get_info() 
                for name, service in self.services.items()
            },
            "total_services": len(self.services),
            "initialization_order": self._initialization_order
        }