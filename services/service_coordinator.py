#!/usr/bin/env python3
"""
Lightweight Service Coordinator for Telemetry Hub
Designed for IntelliSense agent framework - simpler than TANK
"""
import asyncio
import logging
from typing import Dict, Optional, List, Any, Callable
from enum import Enum
from dataclasses import dataclass, field
import signal
import json
from datetime import datetime

logger = logging.getLogger(__name__)

class ServiceState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"

@dataclass
class ServiceConfig:
    """Configuration for a service"""
    name: str
    start_func: Callable
    stop_func: Optional[Callable] = None
    dependencies: List[str] = field(default_factory=list)
    restart_on_failure: bool = True
    health_check: Optional[Callable] = None

@dataclass
class ServiceStatus:
    """Runtime status of a service"""
    state: ServiceState
    task: Optional[asyncio.Task] = None
    error: Optional[str] = None
    last_health_check: Optional[datetime] = None
    restart_count: int = 0

class ServiceCoordinator:
    """
    Pragmatic service orchestration for telemetry-hub
    - Simpler than TANK but extensible for IntelliSense agents
    - Supports service dependencies
    - Health monitoring
    - Future: Agent framework integration
    """

    def __init__(self, redis_client=None):
        self.services: Dict[str, ServiceConfig] = {}
        self.status: Dict[str, ServiceStatus] = {}
        self.redis_client = redis_client
        self.running = False
        self._shutdown_event = asyncio.Event()
        self._health_check_task = None

    def register_service(self, config: ServiceConfig):
        """Register a service with its configuration"""
        self.services[config.name] = config
        self.status[config.name] = ServiceStatus(state=ServiceState.STOPPED)
        logger.info(f"Registered service: {config.name}")

    async def start_service(self, name: str) -> bool:
        """Start a single service with dependency checking"""
        if name not in self.services:
            logger.error(f"Service {name} not registered")
            return False

        service = self.services[name]
        status = self.status[name]

        # Check if already running
        if status.state == ServiceState.RUNNING:
            logger.info(f"Service {name} already running")
            return True

        # Check dependencies first
        for dep in service.dependencies:
            if dep not in self.status or self.status[dep].state != ServiceState.RUNNING:
                logger.error(f"Cannot start {name}: dependency {dep} not running")
                return False

        try:
            status.state = ServiceState.STARTING
            logger.info(f"Starting service: {name}")

            # Start the service
            status.task = asyncio.create_task(self._run_service(name))

            # Wait briefly to confirm startup
            await asyncio.sleep(0.5)

            if status.task.done() and status.task.exception():
                raise status.task.exception()

            status.state = ServiceState.RUNNING

            # Publish status to Redis if available
            if self.redis_client:
                await self._publish_status(name, "started")

            return True

        except Exception as e:
            status.state = ServiceState.ERROR
            status.error = str(e)
            logger.error(f"Failed to start {name}: {e}")
            return False

    async def _run_service(self, name: str):
        """Wrapper to run service with error handling"""
        service = self.services[name]
        status = self.status[name]

        try:
            # Run the service
            await service.start_func()
        except asyncio.CancelledError:
            logger.info(f"Service {name} cancelled")
            raise
        except Exception as e:
            logger.error(f"Service {name} crashed: {e}")
            status.state = ServiceState.ERROR
            status.error = str(e)

            # Attempt restart if configured
            if service.restart_on_failure and status.restart_count < 3:
                status.restart_count += 1
                logger.info(f"Restarting {name} (attempt {status.restart_count})")
                await asyncio.sleep(2 ** status.restart_count)  # Exponential backoff
                await self.start_service(name)

    async def stop_service(self, name: str):
        """Stop a single service"""
        if name not in self.services:
            return

        service = self.services[name]
        status = self.status[name]

        if status.state != ServiceState.RUNNING:
            return

        try:
            status.state = ServiceState.STOPPING
            logger.info(f"Stopping service: {name}")

            # Call stop function if provided
            if service.stop_func:
                await service.stop_func()

            # Cancel the task
            if status.task and not status.task.done():
                status.task.cancel()
                try:
                    await status.task
                except asyncio.CancelledError:
                    pass

            status.state = ServiceState.STOPPED
            status.task = None

            # Publish status to Redis
            if self.redis_client:
                await self._publish_status(name, "stopped")

        except Exception as e:
            logger.error(f"Error stopping {name}: {e}")

    async def start_all(self):
        """Start all services respecting dependencies"""
        self.running = True

        # Start health monitoring
        self._health_check_task = asyncio.create_task(self._health_monitor())

        # Build dependency order
        start_order = self._calculate_start_order()

        for name in start_order:
            success = await self.start_service(name)
            if not success:
                logger.warning(f"Failed to start {name}, continuing with others...")

    def _calculate_start_order(self) -> List[str]:
        """Calculate service start order based on dependencies"""
        # Simple topological sort
        visited = set()
        order = []

        def visit(name):
            if name in visited:
                return
            visited.add(name)
            if name in self.services:
                for dep in self.services[name].dependencies:
                    visit(dep)
                order.append(name)

        for name in self.services:
            visit(name)

        return order

    async def stop_all(self):
        """Stop all services in reverse order"""
        self.running = False
        self._shutdown_event.set()

        # Cancel health monitoring
        if self._health_check_task:
            self._health_check_task.cancel()

        # Stop in reverse dependency order
        stop_order = list(reversed(self._calculate_start_order()))

        for name in stop_order:
            await self.stop_service(name)

    async def _health_monitor(self):
        """Monitor service health"""
        while self.running:
            try:
                for name, service in self.services.items():
                    if service.health_check and self.status[name].state == ServiceState.RUNNING:
                        try:
                            healthy = await service.health_check()
                            self.status[name].last_health_check = datetime.now()

                            if not healthy:
                                logger.warning(f"Service {name} unhealthy")
                                # Could trigger restart here
                        except Exception as e:
                            logger.error(f"Health check failed for {name}: {e}")

                await asyncio.sleep(10)  # Check every 10 seconds

            except asyncio.CancelledError:
                break

    async def _publish_status(self, service_name: str, event: str):
        """Publish service status to Redis for monitoring"""
        if not self.redis_client:
            return

        try:
            status_data = {
                "service": service_name,
                "event": event,
                "timestamp": datetime.now().isoformat(),
                "state": self.status[service_name].state.value
            }

            await self.redis_client.xadd(
                "telemetry:service-status",
                status_data
            )
        except Exception as e:
            logger.error(f"Failed to publish status: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get current status of all services"""
        return {
            name: {
                "state": status.state.value,
                "error": status.error,
                "restart_count": status.restart_count,
                "last_health_check": status.last_health_check.isoformat() if status.last_health_check else None
            }
            for name, status in self.status.items()
        }

    async def wait_for_shutdown(self):
        """Wait for shutdown signal"""
        await self._shutdown_event.wait()

# Future: Agent Framework Extension
class AgentCoordinator(ServiceCoordinator):
    """
    Future extension for IntelliSense agent framework
    Will manage Pattern Detection, Anomaly Detection, etc.
    """

    def register_agent(self, agent_config):
        """Register an IntelliSense analysis agent"""
        # Convert agent to service config
        service_config = ServiceConfig(
            name=f"agent_{agent_config.name}",
            start_func=agent_config.run,
            health_check=agent_config.health_check,
            dependencies=["tcs"]  # Agents depend on TCS
        )
        self.register_service(service_config)