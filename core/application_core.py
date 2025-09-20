"""
ApplicationCore module - Central coordinator for the application's core services.

This module defines the ApplicationCore class which manages the lifecycle of core services,
including the OCR process, EventBus, and other critical components.
"""

import logging
import threading
import time
import json
import uuid # Keep for event ID generation if needed
from typing import Optional, Dict, Any, Callable

# Check psutil availability for health monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ZMQ for IPC command listener
try:
    import zmq
    ZMQ_AVAILABLE = True
except ImportError:
    ZMQ_AVAILABLE = False
    logger = logging.getLogger(__name__) # Define logger early for this warning
    logger.critical("PyZMQ library not found! Core command listener and Babysitter IPC will NOT work.")

# Core imports
from interfaces.data.services import IMarketDataPublisher
from utils.testrade_modes import requires_telemetry_service
# Broker event handling moved to ServiceLifecycleManager

from modules.ocr.data_types import OCRParsedData

from utils.global_config import GlobalConfig, load_global_config
# Circuit breaker logic moved to ServiceLifecycleManager
from core.dependency_injection import DIContainer
from interfaces.core.services import (
    IEventBus as DI_IEventBus, IConfigService, IPipelineValidator, ILifecycleService
)
from interfaces.core.application import IApplicationCore
from interfaces.data.services import IPriceProvider as DI_IPriceProvider
from interfaces.broker.services import IBrokerService
from interfaces.trading.services import ITradeExecutor, IOrderStrategy, ITradeManagerService
from interfaces.trading.services import ITradeLifecycleManager
from interfaces.risk.services import IRiskManagementService
from interfaces.ocr.services import (
    IOCRDataConditioningService, IOCRScalpingSignalOrchestratorService, IROIService
)
# Factory interfaces are no longer needed - removed after refactoring
from interfaces.core.services import IServiceLifecycleManager
from modules.price_fetching.price_fetching_service import PriceFetchingService
from core.di_registration import register_all_services

try:
    import alpaca_trade_api as tradeapi
except ImportError:
    tradeapi = None

logger = logging.getLogger(__name__)

class ServiceConflictError(Exception): pass
class CoreDependencyError(Exception): pass
class ConfigurationError(Exception): pass


class ApplicationCore:
    """
    Central coordinator for the application's core services. Manages the lifecycle
    of all major components via a Dependency Injection container.
    """

    def __init__(self,
                 config_path: Optional[str] = "utils/control.json",
                 config_object: Optional[GlobalConfig] = None,
                 di_container: Optional[DIContainer] = None):
        logger.info("ApplicationCore: Initializing (Lean __init__)...")
        self.logger = logger

        # Use external DI container if provided, otherwise create internal one
        if di_container:
            self._di_container = di_container
            logger.info("ApplicationCore: Using external DI container (ideal pattern)")
        else:
            self._di_container = DIContainer()
            logger.info("ApplicationCore: Creating internal DI container (legacy compatibility)")
            
        self._app_core_stop_event = threading.Event()
        self._core_services_fully_started = threading.Event()

        # 1. Load Config (only if not using external container)
        if not di_container:
            if config_object:
                self.config = config_object
                self.config_path = config_path
            elif config_path:
                self.config = load_global_config(config_path)
                self.config_path = config_path
            else:
                raise ValueError("Either config_path or config_object must be provided.")
            self._validate_critical_configuration()

            # 2. Register Self and Config with DI
            self._di_container.register_instance(IApplicationCore, self)
            self._di_container.register_instance(GlobalConfig, self.config)
            
            # Create and register ConfigService that wraps the GlobalConfig
            from core.config_service import ConfigService
            config_service = ConfigService(self.config)
            self._di_container.register_instance(IConfigService, config_service)

            # 3. Register all service factories via centralized registration
            register_all_services(self._di_container)
            logger.info("ApplicationCore: All services registered via centralized DI registration.")
        else:
            # When using external container, resolve config from it
            self.config = self._di_container.resolve(IConfigService)
            self.config_path = config_path
            logger.info("ApplicationCore: Config resolved from external DI container.")

        # 4. Initialize placeholders for compatibility
        self.service_lifecycle_manager: Optional[IServiceLifecycleManager] = None
        
        # Legacy service references (for compatibility)
        self.event_bus: Optional[DI_IEventBus] = None
        self.broker: Optional[IBrokerService] = None

        # Legacy service references (for compatibility)
        # Note: Metrics clearing now handled by PerformanceMetricsService
        # Note: Broker circuit breaker now handled by ServiceLifecycleManager

        logger.info("ApplicationCore: Lean __init__ complete. Service instantiation deferred to start().")

    def _validate_critical_configuration(self):
        """Validate critical configuration parameters."""
        # Basic validation - can be expanded as needed
        if not hasattr(self.config, 'REDIS_HOST'):
            raise ConfigurationError("REDIS_HOST not found in configuration")
        logger.debug("Critical configuration validation passed.")

    def start(self):
        """
        Starts the application using the ServiceLifecycleManager for orchestrated startup.
        This method now delegates complex service management to the dedicated lifecycle manager.
        """
        logger.info("ApplicationCore.start() sequence initiated (using ServiceLifecycleManager)...")
        if self._core_services_fully_started.is_set():
            logger.warning("ApplicationCore.start() called, but already started. Ignoring.")
            return

        try:
            # ====================================================================
            # PHASE 0: Initialize ServiceLifecycleManager
            # ====================================================================
            logger.info("STARTUP PHASE 0: Initializing ServiceLifecycleManager...")
            self.service_lifecycle_manager = self._di_container.resolve(IServiceLifecycleManager)
            
            # Setup minimal infrastructure for compatibility
            self.event_bus = self._di_container.resolve(DI_IEventBus)
            
            # CRITICAL: Start the EventBus processing thread
            self.event_bus.start()
            logger.info("ApplicationCore foundation services initialized and EventBus started.")

            # ====================================================================
            # PHASE 1: Delegate to ServiceLifecycleManager
            # ====================================================================
            logger.info("STARTUP PHASE 1: Starting ServiceLifecycleManager...")
            self.service_lifecycle_manager.start_all_services()
            
            # Set legacy references for compatibility
            self.broker = self.service_lifecycle_manager.broker_service
            
            # ====================================================================
            # PHASE 2: Wait for All Services Ready
            # ====================================================================
            logger.info("STARTUP PHASE 2: Waiting for all services to be ready...")
            timeout = 60  # Increased timeout for comprehensive service startup
            if not self.service_lifecycle_manager.check_services_ready(timeout):
                raise RuntimeError(f"Services did not become ready within {timeout}s!")
            
            # Broker startup is now handled by ServiceLifecycleManager

            # ====================================================================
            # PHASE 3: Application is Live
            # ====================================================================
            self._core_services_fully_started.set()
            logger.info("ApplicationCore: _core_services_fully_started event SET.")
            
            # Publish bootstrap data via telemetry service (mode-aware)
            if requires_telemetry_service():
                logger.info("Telemetry is required in this mode. Attempting to publish bootstrap data...")
                try:
                    from interfaces.core.telemetry_interfaces import ITelemetryService
                    telemetry_service = self._di_container.resolve(ITelemetryService)
                    if telemetry_service:
                        config_dict = self.config.__dict__ if hasattr(self.config, '__dict__') else {}
                        # Clean up non-serializable data
                        clean_config = {k: v for k, v in config_dict.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
                        telemetry_service.enqueue(
                            source_component="ApplicationCore.Bootstrap",
                            event_type="TESTRADE_CONFIG_BOOTSTRAP",
                            payload={"full_config_snapshot": clean_config}
                        )
                        logger.info("Successfully published bootstrap data via ITelemetryService.")
                    else:
                        logger.warning("ITelemetryService resolved but returned None - bootstrap data not published.")
                except Exception as e:
                    # This would now be a TRUE error, because the service was required but failed.
                    logger.error(f"CRITICAL CONFIG ERROR: Telemetry is required but failed to publish bootstrap data: {e}", exc_info=True)
            else:
                # This is the TANK_SEALED path where telemetry is not used.
                logger.info("Telemetry not required in this mode. Skipping bootstrap data publishing.")

            # Metrics clearing now handled by PerformanceMetricsService
            logger.critical("ApplicationCore: STARTUP SEQUENCE COMPLETE. System is operational (ServiceLifecycleManager).")

        except Exception as e_fatal:
            logger.critical(f"ApplicationCore: FATAL ERROR during startup: {e_fatal}", exc_info=True)
            self.stop()
            raise

    def stop(self):
        logger.info("ApplicationCore.stop() initiated (using ServiceLifecycleManager)...")
        if self._app_core_stop_event.is_set():
            logger.info("ApplicationCore already stopping.")
            return

        # Performance dumping now handled by PerformanceMetricsService
        
        self._app_core_stop_event.set()

        # Delegate shutdown to ServiceLifecycleManager
        if self.service_lifecycle_manager:
            logger.info("Shutting down via ServiceLifecycleManager...")
            self.service_lifecycle_manager.stop_all_services()
        else:
            # Fallback to DI container shutdown
            if self._di_container:
                logger.info("ServiceLifecycleManager not available, falling back to DI container shutdown...")
                self._di_container.shutdown_all()

        # Metrics timer now handled by PerformanceMetricsService
        
        logger.info("ApplicationCore.stop() sequence complete.")

    # All business logic has been moved to appropriate services:
    # - Broker management: ServiceLifecycleManager
    # - Metrics management: PerformanceMetricsService
    # - Telemetry: TelemetryService

    # Properties for compatibility
    @property
    def is_ready(self) -> bool:
        """Check if ApplicationCore is ready."""
        return self._core_services_fully_started.is_set()
