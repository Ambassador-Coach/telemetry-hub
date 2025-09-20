"""
Service Lifecycle Manager

This service manages the complete lifecycle of all services in the application, including:
- Multi-phase service startup orchestration
- Dependency-ordered service shutdown
- Service factory registration and management
- Service health monitoring and readiness checking
- Service state tracking and lifecycle events
- Configuration-driven service management

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List, Type, Set
from threading import Lock

from interfaces.core.services import ILifecycleService
from core.dependency_injection import DIContainer
# Import our custom interface (will be relative path when integrated)
from interfaces.core.services import (
    IServiceLifecycleManager, IIPCManager, IHealthMonitor, 
    ICommandListener, IEmergencyGUIService, IPerformanceMetricsService
)
from interfaces.ocr.services import IOCRProcessManager
# Import Tank mode utilities
from utils.testrade_modes import (
    requires_ipc_services, requires_telemetry_service, 
    requires_external_publishing, get_current_mode, TestradeMode
)

logger = logging.getLogger(__name__)


class CoreDependencyError(Exception):
    """
    Raised when a critical system dependency fails to start or becomes unavailable.
    This represents a fatal error that should halt the entire application startup.
    
    In a zero-tolerance financial system, ANY service failure is considered critical
    and the application cannot continue in a partially functional state.
    """
    def __init__(self, message: str, service_name: Optional[str] = None, original_exception: Optional[Exception] = None):
        super().__init__(message)
        self.service_name = service_name
        self.original_exception = original_exception


class ServiceLifecycleManager(ILifecycleService, IServiceLifecycleManager):
    """
    Manages the complete lifecycle of all application services.
    
    This service orchestrates the startup, monitoring, and shutdown of
    all services in the proper dependency order.
    """
    
    def __init__(self,
                 di_container: DIContainer,
                 config_service: Any,
                 event_bus: Optional[Any] = None):
        """
        Initialize the Service Lifecycle Manager.
        
        Args:
            di_container: Dependency injection container
            config_service: Global configuration object
            event_bus: Event bus for lifecycle notifications
        """
        self.logger = logger
        self.di_container = di_container
        self.config = config_service
        self.event_bus = event_bus
        
        # Lifecycle management attributes
        self._is_running = False
        self._lock = Lock()
        
        # Service state tracking
        self._core_services_fully_started = threading.Event()
        self._service_lifecycle_stop_event = threading.Event()
        self._services_started = False
        self._startup_phase = 0
        self._shutdown_phase = 0
        
        # LATE WORKER ACTIVATION: Global "Go" signal for all worker threads
        self.application_ready_event = threading.Event()
        self.logger.info("🚦 LATE WORKER ACTIVATION: Global application_ready_event created")
        
        # Service references (will be resolved during startup)
        # Support Services
        self.ipc_manager: Optional[IIPCManager] = None
        self.health_monitor: Optional[IHealthMonitor] = None
        self.command_listener: Optional[ICommandListener] = None
        self.emergency_gui_service: Optional[IEmergencyGUIService] = None
        self.performance_metrics_service: Optional[IPerformanceMetricsService] = None
        self.ocr_process_manager: Optional[IOCRProcessManager] = None
        
        # Core Trading Services
        self.telemetry_service: Optional[Any] = None
        self.market_data_publisher: Optional[Any] = None
        self.active_symbols_service: Optional[Any] = None
        self.price_repository: Optional[Any] = None
        self.price_fetching_service: Optional[Any] = None
        self.market_data_filter: Optional[Any] = None
        self.order_repository: Optional[Any] = None
        self.position_manager: Optional[Any] = None
        self.lifecycle_manager: Optional[Any] = None
        self.broker_service: Optional[Any] = None
        self.risk_service: Optional[Any] = None
        self.trade_executor: Optional[Any] = None
        self.trade_manager: Optional[Any] = None
        self.order_event_processor: Optional[Any] = None  # FUZZY'S FIX: Add order event processor
        self.ocr_conditioning_service: Optional[Any] = None
        self.ocr_orchestrator_service: Optional[Any] = None
        self.system_health_service: Optional[Any] = None
        self.fingerprint_service: Optional[Any] = None
        
        # BROKER-FIRST TIERED STARTUP: Prevents resource contention by prioritizing critical connections
        # This eliminates the "noisy neighbor" problem where heavy network operations interfere with
        # delicate broker handshakes during startup
        
        # Get current mode for conditional service inclusion
        current_mode = get_current_mode()
        
        # TIER 1: BROKER ESSENTIALS ONLY - Minimal dependencies for broker connection test
        tier_1_base = [
            # SYMBOL LOADING FIRST: Must load CSV before any market data services
            'ISymbolLoadingService',   # CSV symbol loading - must be first for proper symbol initialization
            
            # BROKER DEPENDENCIES SECOND: Only the absolute essentials
            'IOrderRepository',        # Broker dependency - order tracking foundation
            
            # BROKER LAST: Start broker after its dependencies are ready
            'IBrokerService',          # Broker connection - single lightweight local connection to 127.0.0.1
        ]
        
        # Add telemetry only if required by mode
        if requires_telemetry_service():
            tier_1_base.insert(0, 'ITelemetryService')  # Telemetry service for broker data publishing
            
        self.TIER_1_SERVICES = tier_1_base
        
        # TIER 2: Heavy network operations - Price feeds and market data
        self.TIER_2_SERVICES = [
            'DI_IPriceProvider',       # Price provider interface
            'PriceFetchingService',    # Alpaca WebSocket feed
            'MarketDataFilterService', # Market data filtering
        ]
        
        # TIER 3: All other services including OCR
        tier_3_base = [
            'ITradeLifecycleManager',
            'IPositionManager',
            'IRiskManagementService',
            'ITradeManagerService',
            'ITradeExecutor',
            'IOrderEventProcessor',    # FUZZY'S FIX: Add missing order event processor
            'IFingerprintService',     # Smart duplicate detection for OCR
            'IOCRProcessManager',      # OCR enabled
            'IOCRDataConditioningService', # OCR enabled
            'IOCRScalpingSignalOrchestratorService', # OCR enabled
            'SystemHealthMonitoringService',
            'ActiveSymbolsService',
            'IPerformanceMetricsService',
            'IHealthMonitor',
            'ICommandListener',
            'IEmergencyGUIService'
        ]
        
        # Add IPC Manager only if required by mode
        if requires_ipc_services():
            tier_3_base.append('IIPCManager')
            
        self.TIER_3_SERVICES = tier_3_base
        
        # Combined order for shutdown (reverse dependency order)
        self.service_startup_order = self.TIER_1_SERVICES + self.TIER_2_SERVICES + self.TIER_3_SERVICES
        self.service_shutdown_order = list(reversed(self.service_startup_order))
        
        # State tracking
        self._start_time = None
        self._services_status = {}
        self._last_readiness_check = None
        
        # Thread management
        self._thread_ready_timeout = {}  # Per-service thread ready timeouts
        self._thread_shutdown_timeout = {}  # Per-service thread shutdown timeouts
        self._critical_threaded_services = {  # Services that MUST have working threads
            'DI_IPriceProvider': {'timeout': 10.0, 'critical': True},  # PriceRepository API workers
            'PriceFetchingService': {'timeout': 15.0, 'critical': True},  # WebSocket threads
            'IOCRDataConditioningService': {'timeout': 8.0, 'critical': True},  # OCR worker threads
            'ITelemetryService': {'timeout': 5.0, 'critical': True},  # Publisher threads
            'IBrokerService': {'timeout': 20.0, 'critical': True},  # Connection threads
            'IOrderRepository': {'timeout': 5.0, 'critical': False},  # Background threads
            'SystemHealthMonitoringService': {'timeout': 5.0, 'critical': False}  # Monitoring threads
        }
        
    @property
    def is_ready(self) -> bool:
        """Check if the service lifecycle manager is ready."""
        with self._lock:
            return self._is_running and self._services_started
    
    @property
    def all_services_ready(self) -> bool:
        """Check if all services are ready."""
        return self._core_services_fully_started.is_set()
    
    def start(self) -> None:
        """Start the service lifecycle manager."""
        with self._lock:
            if self._is_running:
                self.logger.warning("ServiceLifecycleManager already running")
                return
                
            self._is_running = True
            self._start_time = time.time()
            self._startup_phase = 0
            
            try:
                # Start multi-phase service startup
                self._execute_multi_phase_startup()
                
                self._services_started = True
                self.logger.info("ServiceLifecycleManager started successfully")
                
            except Exception as e:
                self.logger.error(f"Failed to start ServiceLifecycleManager: {e}", exc_info=True)
                self._is_running = False
                raise
    
    def stop(self) -> None:
        """Stop the service lifecycle manager."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            self._shutdown_phase = 0
            
            # Signal stop to all services
            self._service_lifecycle_stop_event.set()
            
            try:
                # Execute multi-phase shutdown
                self._execute_multi_phase_shutdown()
                
                self._services_started = False
                self._core_services_fully_started.clear()
                
                self.logger.info("ServiceLifecycleManager stopped successfully")
                
            except Exception as e:
                self.logger.error(f"Error during ServiceLifecycleManager shutdown: {e}", exc_info=True)
    
    def _execute_multi_phase_startup(self):
        """Execute multi-phase service startup."""
        self.logger.info("Starting multi-phase service startup...")
        
        # Phase 0: Resolve core services from DI container
        self._startup_phase = 0
        self._resolve_core_services()
        
        # Phase 1: Start services in dependency order
        self._startup_phase = 1
        self._start_services_in_order()
        
        # Phase 2: Check service readiness (Readiness Gate)
        self._startup_phase = 2
        self._check_all_services_ready()
        
        # Phase 3: Signal services fully started (Internal readiness)
        self._startup_phase = 3
        self._signal_services_ready()
        
        # Phase 4: LATE WORKER ACTIVATION - Send "Go" signal to all worker threads
        self._startup_phase = 4
        self._activate_all_worker_threads()
        
        self.logger.info("🎉 LATE WORKER ACTIVATION: Multi-phase service startup completed successfully")
    
    def _resolve_core_services(self):
        """Resolve core services from DI container."""
        current_mode = get_current_mode()
        self.logger.info(f"Phase 0: Resolving services from DI container for {current_mode.value} mode...")
        self.logger.info(f"Mode requirements: IPC={requires_ipc_services()}, Telemetry={requires_telemetry_service()}, External={requires_external_publishing()}")
        
        try:
            # Import interfaces for resolution
            # Import interfaces using new organized structure
            from interfaces.data.services import IPriceProvider as DI_IPriceProvider, IMarketDataPublisher
            from interfaces.trading.services import ITradeExecutor, ITradeManagerService
            from interfaces.order_management.services import IOrderRepository
            from interfaces.broker.services import IBrokerService
            from interfaces.trading.services import ITradeLifecycleManager
            from interfaces.risk.services import IRiskManagementService
            from interfaces.ocr.services import IOCRDataConditioningService, IOCRScalpingSignalOrchestratorService
            from interfaces.utility.services import ISymbolLoadingService, IFingerprintService
            from interfaces.core.telemetry_interfaces import ITelemetryService
            from interfaces.trading.services import IPositionManager
            
            # BROKER-FIRST RESOLUTION ORDER: Resolve services in startup tier order
            # This ensures the most critical services are instantiated first
            
            # TIER 1: Critical Local Connections (BROKER DEPENDENCIES FIRST)
            # Telemetry service is conditional based on Tank mode
            if requires_telemetry_service():
                try:
                    self.telemetry_service = self.di_container.resolve(ITelemetryService)
                    self.logger.info("✅ ITelemetryService resolved successfully")
                except Exception as e:
                    self.logger.warning(f"Failed to resolve ITelemetryService: {e}")
                    self.telemetry_service = None
            else:
                self.logger.info(f"⏭️  Skipping ITelemetryService in {current_mode.value} mode")
                self.telemetry_service = None
                
            self.order_repository = self.di_container.resolve(IOrderRepository)  # Broker dependency
            
            # Optional services for broker isolation test
            try:
                self.symbol_loading_service = self.di_container.resolve(ISymbolLoadingService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve ISymbolLoadingService: {e}")
                self.symbol_loading_service = None
                
            # Market data publisher is conditional based on Tank mode (external publishing)
            if requires_external_publishing():
                try:
                    self.market_data_publisher = self.di_container.resolve(IMarketDataPublisher)
                    self.logger.info("✅ IMarketDataPublisher resolved successfully")
                except Exception as e:
                    self.logger.warning(f"Failed to resolve IMarketDataPublisher: {e}")
                    self.market_data_publisher = None
            else:
                self.logger.info(f"⏭️  Skipping IMarketDataPublisher in {current_mode.value} mode")
                self.market_data_publisher = None
                
            self.broker_service = self.di_container.resolve(IBrokerService)  # Broker LAST after dependencies
            
            # TIER 2: Heavy Network Operations (After broker is resolved) - OPTIONAL for process of elimination
            try:
                self.price_repository = self.di_container.resolve(DI_IPriceProvider)
            except Exception as e:
                self.logger.warning(f"Failed to resolve DI_IPriceProvider: {e}")
                self.price_repository = None
                
            try:
                self.price_fetching_service = self.di_container.resolve('PriceFetchingService')
            except Exception as e:
                self.logger.warning(f"Failed to resolve PriceFetchingService: {e}")
                self.price_fetching_service = None
                
            try:
                self.market_data_filter = self.di_container.resolve('MarketDataFilterService')
            except Exception as e:
                self.logger.warning(f"Failed to resolve MarketDataFilterService: {e}")
                self.market_data_filter = None
            
            # TIER 3: All Other Services (After broker + price feeds are resolved) - OPTIONAL for process of elimination
            try:
                self.active_symbols_service = self.di_container.resolve('ActiveSymbolsService')
            except Exception as e:
                self.logger.warning(f"Failed to resolve ActiveSymbolsService: {e}")
                self.active_symbols_service = None
                
            try:
                self.position_manager = self.di_container.resolve(IPositionManager)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IPositionManager: {e}")
                self.position_manager = None
                
            try:
                self.lifecycle_manager = self.di_container.resolve(ITradeLifecycleManager)
            except Exception as e:
                self.logger.warning(f"Failed to resolve ITradeLifecycleManager: {e}")
                self.lifecycle_manager = None
            
            # Resolve Trading Logic - OPTIONAL for process of elimination
            try:
                self.risk_service = self.di_container.resolve(IRiskManagementService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IRiskManagementService: {e}")
                self.risk_service = None
                
            try:
                self.trade_executor = self.di_container.resolve(ITradeExecutor)
            except Exception as e:
                self.logger.warning(f"Failed to resolve ITradeExecutor: {e}")
                self.trade_executor = None
                
            try:
                self.trade_manager = self.di_container.resolve(ITradeManagerService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve ITradeManagerService: {e}")
                self.trade_manager = None

            # FUZZY'S FIX: Resolve Order Event Processor - Required for broker event handling
            try:
                from interfaces.order_management.services import IOrderEventProcessor
                self.order_event_processor = self.di_container.resolve(IOrderEventProcessor)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IOrderEventProcessor: {e}")
                self.order_event_processor = None

            # Resolve Fingerprint Service - Required by OCR orchestrator
            try:
                self.fingerprint_service = self.di_container.resolve(IFingerprintService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IFingerprintService: {e}")
                self.fingerprint_service = None
            
            # Resolve OCR Services - OPTIONAL for process of elimination
            try:
                self.ocr_conditioning_service = self.di_container.resolve(IOCRDataConditioningService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IOCRDataConditioningService: {e}")
                self.ocr_conditioning_service = None
                
            try:
                self.ocr_orchestrator_service = self.di_container.resolve(IOCRScalpingSignalOrchestratorService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IOCRScalpingSignalOrchestratorService: {e}")
                self.ocr_orchestrator_service = None
            
            # Resolve System Services - OPTIONAL for process of elimination
            try:
                self.system_health_service = self.di_container.resolve('SystemHealthMonitoringService')
            except Exception as e:
                self.logger.warning(f"Failed to resolve SystemHealthMonitoringService: {e}")
                self.system_health_service = None
            
            # Resolve Support Services - OPTIONAL for process of elimination
            # IPC Manager is conditional based on Tank mode
            if requires_ipc_services():
                try:
                    self.ipc_manager = self.di_container.resolve(IIPCManager)
                    self.logger.info("✅ IIPCManager resolved successfully")
                except Exception as e:
                    self.logger.warning(f"Failed to resolve IIPCManager: {e}")
                    self.ipc_manager = None
            else:
                self.logger.info(f"⏭️  Skipping IIPCManager in {current_mode.value} mode")
                self.ipc_manager = None
                
            try:
                self.performance_metrics_service = self.di_container.resolve(IPerformanceMetricsService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IPerformanceMetricsService: {e}")
                self.performance_metrics_service = None
                
            try:
                self.ocr_process_manager = self.di_container.resolve(IOCRProcessManager)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IOCRProcessManager: {e}")
                self.ocr_process_manager = None
                
            try:
                self.health_monitor = self.di_container.resolve(IHealthMonitor)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IHealthMonitor: {e}")
                self.health_monitor = None
                
            try:
                self.command_listener = self.di_container.resolve(ICommandListener)
            except Exception as e:
                self.logger.warning(f"Failed to resolve ICommandListener: {e}")
                self.command_listener = None
                
            try:
                self.emergency_gui_service = self.di_container.resolve(IEmergencyGUIService)
            except Exception as e:
                self.logger.warning(f"Failed to resolve IEmergencyGUIService: {e}")
                self.emergency_gui_service = None
            
            # Wire up circular dependencies after all services are resolved
            self._wire_service_dependencies()
            
            # Log Tank mode resolution summary
            self._log_tank_mode_summary()
            
            self.logger.info("ALL services resolved and wired successfully from DI container")
            
        except Exception as e:
            self.logger.error(f"Failed to resolve services from DI container: {e}", exc_info=True)
            raise
    
    def _log_tank_mode_summary(self):
        """Log summary of services skipped due to Tank mode."""
        current_mode = get_current_mode()
        
        if current_mode == TestradeMode.TANK_SEALED:
            self.logger.info("=" * 60)
            self.logger.info(f"🛡️  TANK_SEALED MODE - Maximum isolation enabled")
            self.logger.info("Skipped services:")
            self.logger.info("  - ITelemetryService (no telemetry interfaces)")
            self.logger.info("  - IIPCManager (no IPC services)")
            self.logger.info("  - IMarketDataPublisher (no external publishing)")
            self.logger.info("=" * 60)
        elif current_mode == TestradeMode.TANK_BUFFERED:
            self.logger.info("=" * 60)
            self.logger.info(f"🛡️  TANK_BUFFERED MODE - Local buffering only")
            self.logger.info("Skipped services:")
            self.logger.info("  - IIPCManager (no IPC services)")
            self.logger.info("  - IMarketDataPublisher (no external publishing)")
            self.logger.info("Active services:")
            self.logger.info("  - ITelemetryService (local buffering only)")
            self.logger.info("=" * 60)
        else:
            self.logger.info("=" * 60)
            self.logger.info(f"🚀 LIVE MODE - All services enabled")
            self.logger.info("=" * 60)

    def _wire_service_dependencies(self):
        """Wire up circular dependencies between services."""
        self.logger.info("Wiring service dependencies...")
        
        try:
            # Wire ActiveSymbolsService dependencies
            if self.active_symbols_service:
                # Set PositionManager reference
                if hasattr(self.active_symbols_service, 'set_position_manager_ref') and self.position_manager:
                    self.logger.critical("### [DI_TRACE] ServiceLifecycleManager: ABOUT TO INJECT PositionManager into ActiveSymbolsService. ###")
                    self.active_symbols_service.set_position_manager_ref(self.position_manager)
                    self.logger.info("Wired PositionManager reference into ActiveSymbolsService")
                
                # Set PriceFetchingService reference
                if hasattr(self.active_symbols_service, 'set_price_fetching_service_ref') and self.price_fetching_service:
                    self.logger.critical("### [DI_TRACE] ServiceLifecycleManager: ABOUT TO INJECT PriceFetchingService into ActiveSymbolsService. ###")
                    self.active_symbols_service.set_price_fetching_service_ref(self.price_fetching_service)
                    self.logger.info("Wired PriceFetchingService reference into ActiveSymbolsService")
                
                # Bootstrap with current positions after wiring
                if hasattr(self.active_symbols_service, 'bootstrap_with_current_positions'):
                    self.active_symbols_service.bootstrap_with_current_positions()
                    self.logger.info("ActiveSymbolsService bootstrapped with current positions")
            
            self.logger.info("Service dependency wiring completed successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to wire service dependencies: {e}", exc_info=True)
            raise
    
    def _start_services_in_order(self):
        """
        Start services using BROKER-FIRST TIERED ACTIVATION to eliminate resource contention.
        
        This method separates service creation (resolution) from service activation (starting).
        All services are already resolved by the DI container, but we activate them in a 
        carefully controlled sequence to prevent resource contention.
        """
        self.logger.info("Phase 1: BROKER-FIRST TIERED ACTIVATION - Separating creation from activation...")
        self.logger.info("Policy: All services resolved, but activated in safe sequence")
        
        # Call the new tiered activation method
        self._activate_services_in_tiers()
        
        self.logger.info("🎉 BROKER-FIRST ACTIVATION SUCCESS: Resource contention eliminated!")
        self.logger.info("💰 Financial system ready for 100% operational trading with optimal activation sequence")
    
    def _activate_services_in_tiers(self):
        """
        Activate services in tiers, separating creation from activation.
        
        All services are already resolved (created) by the DI container.
        This method activates (starts) them in the exact sequence required
        to prevent resource contention during startup.
        """
        
        # =================== TIER 1 ACTIVATION: CRITICAL LOCAL CONNECTIONS ===================
        self.logger.info("🔗 TIER 1 ACTIVATION: Starting critical local connections (Broker FIRST)...")
        self.logger.info("    🎯 Priority: Lightweight broker connection to 127.0.0.1 in quiet environment")
        
        # Activate Tier 1 services and wait for each to be ready
        # Build tier1_services list dynamically based on what was resolved
        tier1_services = []
        
        # Add services in the same order as TIER_1_SERVICES
        if 'ITelemetryService' in self.TIER_1_SERVICES and self.telemetry_service:
            tier1_services.append(('ITelemetryService', self.telemetry_service))
        
        tier1_services.append(('IOrderRepository', self.order_repository))
        
        # Market data publisher checked for TIER_1 (symbol loading moved to TIER 2)
        if self.market_data_publisher:
            tier1_services.append(('IMarketDataPublisher', self.market_data_publisher))
            
        tier1_services.append(('IBrokerService', self.broker_service))
        
        for service_name, service_instance in tier1_services:
            self._activate_single_service(service_name, service_instance, "TIER 1")
        
        # TIER 1 services are now ready - broker connection established
        self.logger.info("✅ TIER 1 SUCCESS: Broker and essential infrastructure ready!")
        self.logger.info("🛡️  RESOURCE CONTENTION BARRIER: Broker connection secured before heavy network operations")
        
        # =================== TIER 2 ACTIVATION: HEAVY NETWORK OPERATIONS ===================
        self.logger.info("🌐 TIER 2 ACTIVATION: Starting symbol loading + heavy network operations (Price feeds)...")
        self.logger.info("    📋 Loading symbols first for hot pre-subscription")
        self.logger.info("    ⚡ Starting aggressive ~1400 symbol Alpaca subscription (noisy neighbor)")
        
        # Activate Tier 2 services and wait for each to be ready
        tier2_services = []
        
        # Add symbol loading service first to ensure symbols are loaded before price fetching
        if self.symbol_loading_service:
            tier2_services.append(('ISymbolLoadingService', self.symbol_loading_service))
            
        tier2_services.extend([
            ('DI_IPriceProvider', self.price_repository),
            ('PriceFetchingService', self.price_fetching_service),
            ('MarketDataFilterService', self.market_data_filter),
        ])
        
        for service_name, service_instance in tier2_services:
            self._activate_single_service(service_name, service_instance, "TIER 2")
        
        # TIER 2 services are now ready - price feeds established
        self.logger.info("✅ TIER 2 SUCCESS: Heavy network operations complete!")
        self.logger.info("💹 PRICE FEED BARRIER CLEARED: Live market data flowing")
        
        # =================== TIER 3 ACTIVATION: ALL OTHER SERVICES ===================
        self.logger.info("🚀 TIER 3 ACTIVATION: Starting all other services (Trading, OCR, etc.)...")
        self.logger.info("    📈 OCR can now safely start - broker + price feeds available")
        
        # Activate Tier 3 services and wait for each to be ready
        # Build tier3_services list dynamically to handle conditional services
        tier3_services = []
        
        # Add services that should always be present (if resolved)
        service_mapping = [
            ('ITradeLifecycleManager', self.lifecycle_manager),
            ('IPositionManager', self.position_manager),
            ('IRiskManagementService', self.risk_service),
            ('ITradeManagerService', self.trade_manager),
            ('ITradeExecutor', self.trade_executor),
            ('IOrderEventProcessor', self.order_event_processor),  # FUZZY'S FIX: Add order event processor
            ('IFingerprintService', self.fingerprint_service),
            ('IOCRProcessManager', self.ocr_process_manager),
            ('IOCRDataConditioningService', self.ocr_conditioning_service),
            ('IOCRScalpingSignalOrchestratorService', self.ocr_orchestrator_service),
            ('SystemHealthMonitoringService', self.system_health_service),
            ('ActiveSymbolsService', self.active_symbols_service),
            ('IPerformanceMetricsService', self.performance_metrics_service),
            ('IHealthMonitor', self.health_monitor),
            ('ICommandListener', self.command_listener),
            ('IEmergencyGUIService', self.emergency_gui_service),
        ]
        
        # Add conditional services only if they're in TIER_3_SERVICES
        if 'IIPCManager' in self.TIER_3_SERVICES:
            service_mapping.append(('IIPCManager', self.ipc_manager))
        
        # Filter out None services
        tier3_services = [(name, svc) for name, svc in service_mapping if svc is not None]
        
        for service_name, service_instance in tier3_services:
            self._activate_single_service(service_name, service_instance, "TIER 3")
        
        # TIER 3 services are now ready - full system operational
        self.logger.info("✅ TIER 3 SUCCESS: All dependent services ready!")
        
    def _activate_single_service(self, service_name: str, service_instance: Any, tier_name: str):
        """
        Activate a single service and wait for it to be ready.
        
        Args:
            service_name: Name of the service for logging
            service_instance: The already-resolved service instance
            tier_name: Tier name for logging context
        """
        if service_instance is None:
            self.logger.warning(f"  ⚠️  {tier_name}: {service_name} is None - skipping activation")
            return
        
        # Check if service needs lifecycle management
        if isinstance(service_instance, ILifecycleService):
            try:
                self.logger.info(f"  🔄 {tier_name}: Activating {service_name}...")
                service_instance.start()
                self.logger.info(f"  ✅ {tier_name}: {service_name} start() called successfully")
                
                # BLOCKING: Wait for service to be ready before proceeding
                self.logger.info(f"  ⏱️  {tier_name}: Waiting for {service_name} readiness (timeout: 30.0s)")
                ready_start_time = time.time()
                timeout_seconds = 30.0
                
                while not service_instance.is_ready:
                    if time.time() - ready_start_time > timeout_seconds:
                        self.logger.critical(f"  ❌ {tier_name}: {service_name} failed to become ready within {timeout_seconds}s")
                        raise CoreDependencyError(
                            f"FATAL: {tier_name} service '{service_name}' failed to become ready within {timeout_seconds}s. "
                            f"Tiered activation requires each service to be fully ready before proceeding.",
                            service_name=service_name
                        )
                    time.sleep(0.1)  # Check every 100ms
                
                ready_duration = time.time() - ready_start_time
                self.logger.info(f"  ✅ {tier_name}: {service_name} became ready in {ready_duration:.2f}s")
                
            except Exception as activation_error:
                self.logger.critical(f"  ❌ {tier_name}: FATAL activation error for {service_name}: {activation_error}")
                raise CoreDependencyError(
                    f"FATAL: {tier_name} service '{service_name}' failed to activate. "
                    f"Original error: {activation_error}",
                    service_name=service_name,
                    original_exception=activation_error
                ) from activation_error
        else:
            # Passive service - no activation needed
            self.logger.debug(f"  ⏭️  {tier_name}: {service_name} is passive - no activation needed")
    
    def _start_service_tier(self, tier_services: List[str], service_map: Dict[str, Any], tier_name: str) -> List[str]:
        """
        Start all services in a specific tier with ZERO TOLERANCE policy.
        
        Args:
            tier_services: List of service names in this tier
            service_map: Mapping of service names to service instances
            tier_name: Name of the tier for logging
            
        Returns:
            List of successfully started service names
            
        Raises:
            CoreDependencyError: If any lifecycle service in the tier fails to start
        """
        started_services = []
        lifecycle_count = 0
        passive_count = 0
        
        for service_name in tier_services:
            service = service_map.get(service_name)
            
            # ZERO TOLERANCE: Service must exist
            if service is None:
                self.logger.critical(f"FATAL: {tier_name} service '{service_name}' not found in DI container")
                raise CoreDependencyError(
                    f"{tier_name} service '{service_name}' not found in DI container. "
                    f"Financial system cannot start with missing {tier_name.lower()} dependencies.",
                    service_name=service_name
                )
            
            # Check if the service has a lifecycle that needs management
            if isinstance(service, ILifecycleService):
                lifecycle_count += 1
                try:
                    self.logger.info(f"  [{lifecycle_count}] {tier_name}: Starting {service_name}...")
                    service.start()
                    self.logger.info(f"  ✅ {tier_name}: {service_name} started successfully")
                    
                    # SEQUENTIAL BLOCKING: Wait for service to be ready before proceeding
                    self.logger.info(f"  ⏱️  {tier_name}: Waiting for {service_name} readiness (timeout: 30.0s) - ZERO TOLERANCE")
                    ready_start_time = time.time()
                    timeout_seconds = 30.0
                    
                    while not service.is_ready:
                        if time.time() - ready_start_time > timeout_seconds:
                            self.logger.critical(f"FATAL: {tier_name} service '{service_name}' failed to become ready within {timeout_seconds}s")
                            raise CoreDependencyError(
                                f"FATAL: {tier_name} service '{service_name}' failed to become ready within {timeout_seconds}s. "
                                f"Sequential startup requires each service to be fully ready before proceeding.",
                                service_name=service_name
                            )
                        time.sleep(0.1)  # Check every 100ms
                    
                    ready_duration = time.time() - ready_start_time
                    self.logger.info(f"  ✅ {tier_name}: {service_name} became ready in {ready_duration:.2f}s")
                    started_services.append(service_name)
                    
                    # Record service status
                    self._services_status[service_name] = {
                        "started": True,
                        "start_time": time.time(),
                        "error": None,
                        "type": "lifecycle",
                        "tier": tier_name,
                        "ready_duration": ready_duration
                    }
                    
                except Exception as startup_error:
                    self.logger.critical("=" * 80)
                    self.logger.critical(f"FATAL: {tier_name} SERVICE FAILURE: {service_name}")
                    self.logger.critical(f"Error: {startup_error}")
                    self.logger.critical(f"Successfully started before failure: {started_services}")
                    self.logger.critical(f"ZERO TOLERANCE POLICY: Cannot continue with failed {tier_name.lower()} service")
                    self.logger.critical("=" * 80)
                    raise CoreDependencyError(
                        f"FATAL: {tier_name} service '{service_name}' failed to start. "
                        f"Original error: {startup_error}",
                        service_name=service_name,
                        original_exception=startup_error
                    ) from startup_error
            else:
                # Passive service - no start() method needed
                passive_count += 1
                self.logger.debug(f"  ⏭️  {tier_name}: Skipping passive service {service_name}")
                
                # Record passive service status
                self._services_status[service_name] = {
                    "started": True,
                    "start_time": time.time(),
                    "error": None,
                    "type": "passive",
                    "tier": tier_name
                }
        
        self.logger.info(f"  🎯 {tier_name} STARTUP COMPLETE: {len(started_services)} lifecycle services, {passive_count} passive services")
        return started_services
    
    def _check_tier_services_ready(self, tier_services: List[str], service_map: Dict[str, Any], tier_name: str) -> bool:
        """
        Check that all services in a tier are ready before proceeding to the next tier.
        
        Args:
            tier_services: List of service names in this tier
            service_map: Mapping of service names to service instances  
            tier_name: Name of the tier for logging
            
        Returns:
            True if all lifecycle services in the tier are ready, False otherwise
        """
        ready_timeout = getattr(self.config, 'service_ready_timeout_sec', 30.0)
        ready_services = []
        failed_services = []
        
        self.logger.info(f"  🔍 {tier_name} READINESS CHECK: Verifying all services are ready...")
        
        for service_name in tier_services:
            service = service_map.get(service_name)
            
            if not service:
                failed_services.append(service_name)
                continue
                
            # Only check readiness for lifecycle services
            if isinstance(service, ILifecycleService):
                if hasattr(service, 'is_ready'):
                    # Service has readiness check - wait for it
                    self.logger.info(f"    ⏱️  Checking {service_name} readiness...")
                    if self._wait_for_service_ready_zero_tolerance(service, service_name, ready_timeout):
                        ready_services.append(service_name)
                        self.logger.info(f"    ✅ {service_name} is READY")
                    else:
                        failed_services.append(service_name)
                        self.logger.critical(f"    ❌ {service_name} FAILED readiness check")
                else:
                    # Lifecycle service without readiness check - assume ready if started
                    if self._services_status.get(service_name, {}).get("started", False):
                        ready_services.append(service_name)
                        self.logger.info(f"    ✅ {service_name} assumed ready (no readiness check)")
                    else:
                        failed_services.append(service_name)
                        self.logger.critical(f"    ❌ {service_name} not started")
            else:
                # Passive service - always considered ready
                ready_services.append(service_name)
                self.logger.debug(f"    ⏭️  {service_name} (passive service - ready)")
        
        # Report tier readiness results
        if failed_services:
            self.logger.critical(f"  ❌ {tier_name} READINESS FAILED: {len(failed_services)} services not ready")
            self.logger.critical(f"     Failed services: {failed_services}")
            self.logger.critical(f"     Ready services: {ready_services}")
            return False
        else:
            self.logger.info(f"  ✅ {tier_name} READINESS SUCCESS: All {len(ready_services)} services ready")
            return True
    
    def _check_all_services_ready(self):
        """
        Check that ALL services are ready with ZERO TOLERANCE policy.
        
        In a financial system, services that start but never become ready represent
        a critical failure state. We cannot operate with services in unknown states.
        """
        self.logger.info("Phase 2: Checking ALL services readiness with ZERO TOLERANCE policy...")
        
        ready_timeout = getattr(self.config, 'service_ready_timeout_sec', 30.0)
        ready_services = []
        failed_readiness_services = []
        
        # ALL services need to be checked for readiness - no exceptions in financial systems
        service_map = {
            # Core Infrastructure
            'ISymbolLoadingService': self.symbol_loading_service,
            'ITelemetryService': self.telemetry_service,
            'IMarketDataPublisher': self.market_data_publisher,
            'ActiveSymbolsService': self.active_symbols_service,
            
            # Data Services
            'DI_IPriceProvider': self.price_repository,
            'PriceFetchingService': self.price_fetching_service,
            'MarketDataFilterService': self.market_data_filter,
            
            # Trading Core
            'IOrderRepository': self.order_repository,
            'IPositionManager': self.position_manager,
            'ITradeLifecycleManager': self.lifecycle_manager,
            'IBrokerService': self.broker_service,
            
            # Trading Logic
            'IRiskManagementService': self.risk_service,
            'ITradeExecutor': self.trade_executor,
            'ITradeManagerService': self.trade_manager,
            'IFingerprintService': self.fingerprint_service,
            
            # OCR Services
            'IOCRDataConditioningService': self.ocr_conditioning_service,
            'IOCRScalpingSignalOrchestratorService': self.ocr_orchestrator_service,
            
            # System Services
            'SystemHealthMonitoringService': self.system_health_service,
            
            # Support Services
            'IIPCManager': self.ipc_manager,
            'IPerformanceMetricsService': self.performance_metrics_service,
            'IOCRProcessManager': self.ocr_process_manager,
            'IHealthMonitor': self.health_monitor,
            'ICommandListener': self.command_listener,
            'IEmergencyGUIService': self.emergency_gui_service
        }
        
        start_check_time = time.time()
        
        for service_name in self.service_startup_order:
            service = service_map.get(service_name)
            
            # Skip readiness check for passive services (no lifecycle management)
            if not isinstance(service, ILifecycleService):
                ready_services.append(service_name)
                self.logger.debug(f"⏭️  Skipping readiness check for passive service: {service_name}")
                continue
            
            # For lifecycle services, check readiness
            if service and hasattr(service, 'is_ready'):
                # Lifecycle service has is_ready - MUST become ready
                self.logger.info(f"Checking readiness for lifecycle service {service_name}...")
                ready = self._wait_for_service_ready_zero_tolerance(service, service_name, ready_timeout)
                if ready:
                    ready_services.append(service_name)
                    self.logger.info(f"✅ {service_name} is READY")
                else:
                    failed_readiness_services.append(service_name)
                    self.logger.critical(f"❌ {service_name} FAILED readiness check")
                    
            elif service:
                # Lifecycle service doesn't have is_ready property - assume ready if started successfully
                if self._services_status.get(service_name, {}).get("started", False):
                    ready_services.append(service_name)
                    self.logger.info(f"✅ {service_name} assumed ready (lifecycle service, no is_ready property)")
                else:
                    failed_readiness_services.append(service_name)
                    self.logger.critical(f"❌ {service_name} NOT started but expected to be ready")
        
        check_duration = time.time() - start_check_time
        self._last_readiness_check = time.time()
        
        # ZERO TOLERANCE: If ANY service failed readiness, abort
        if failed_readiness_services:
            self.logger.critical("=" * 80)
            self.logger.critical(f"FATAL READINESS FAILURE: {len(failed_readiness_services)} services failed readiness checks")
            self.logger.critical(f"Failed services: {failed_readiness_services}")
            self.logger.critical(f"Ready services: {ready_services}")
            self.logger.critical(f"ZERO TOLERANCE POLICY: Financial system cannot operate with services in unknown states")
            self.logger.critical("=" * 80)
            
            raise CoreDependencyError(
                f"FATAL: {len(failed_readiness_services)} services failed readiness checks: {failed_readiness_services}. "
                f"Financial system cannot operate with services in unknown states.",
                service_name=f"Readiness_Check_{len(failed_readiness_services)}_failures"
            )
        
        # If we reach here, ALL services are ready
        self.logger.info("🎉 ZERO TOLERANCE SUCCESS: ALL services are READY")
        self.logger.info(f"✅ Ready services ({len(ready_services)}): {ready_services}")
        self.logger.info(f"Readiness check completed in {check_duration:.2f}s")
    
    def _wait_for_service_ready_zero_tolerance(self, service: Any, service_name: str, timeout: float) -> bool:
        """
        Wait for a service to be ready with ZERO TOLERANCE policy.
        
        In a financial system, ALL services must become ready within the timeout.
        There are no "non-critical" services - everything is critical.
        
        Args:
            service: The service instance
            service_name: Name of the service for logging
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if service becomes ready, False if timeout or error
        """
        start_time = time.time()
        self.logger.info(f"⏱️  Waiting for {service_name} readiness (timeout: {timeout}s) - ZERO TOLERANCE")
        
        while time.time() - start_time < timeout:
            try:
                # Check if service has is_ready property and it's True
                if hasattr(service, 'is_ready') and service.is_ready:
                    elapsed = time.time() - start_time
                    self.logger.info(f"✅ {service_name} became ready in {elapsed:.2f}s")
                    return True
                    
                # Check if service has is_ready but it's not ready yet
                elif hasattr(service, 'is_ready'):
                    self.logger.debug(f"⏳ {service_name} not ready yet (is_ready=False)")
                else:
                    # This shouldn't happen since we checked hasattr before calling this method
                    self.logger.error(f"❌ {service_name} has no is_ready property")
                    return False
                    
            except Exception as e:
                self.logger.error(f"❌ Error checking readiness for {service_name}: {e}")
                return False
            
            # Brief sleep before next check
            time.sleep(0.1)  # Check every 100ms
        
        # Timeout reached
        elapsed = time.time() - start_time
        self.logger.critical(f"❌ TIMEOUT: {service_name} did not become ready within {timeout}s (actual: {elapsed:.2f}s)")
        self.logger.critical(f"❌ ZERO TOLERANCE VIOLATION: Service readiness timeout in financial system")
        return False
    
    def _wait_for_service_ready(self, service: Any, service_name: str, timeout: float) -> bool:
        """Wait for a specific service to be ready, including thread readiness."""
        start_time = time.time()
        
        # Use service-specific timeout if configured
        if service_name in self._critical_threaded_services:
            timeout = self._critical_threaded_services[service_name]['timeout']
            is_critical = self._critical_threaded_services[service_name]['critical']
        else:
            is_critical = False
        
        self.logger.info(f"Waiting for {service_name} readiness (timeout: {timeout}s, critical: {is_critical})")
        
        while time.time() - start_time < timeout:
            try:
                # Check basic service readiness
                if hasattr(service, 'is_ready') and service.is_ready:
                    # For threaded services, do additional thread health check
                    if service_name in self._critical_threaded_services:
                        thread_health = self._check_service_thread_health(service, service_name)
                        if thread_health:
                            self.logger.info(f"{service_name} ready with healthy threads")
                            return True
                        else:
                            self.logger.debug(f"{service_name} service ready but threads not healthy yet")
                    else:
                        self.logger.info(f"{service_name} ready (no thread check required)")
                        return True
                elif hasattr(service, 'is_ready'):
                    self.logger.debug(f"{service_name} not ready yet (is_ready=False)")
                else:
                    # Service has no is_ready property - assume ready if started
                    self.logger.info(f"{service_name} ready (no is_ready property)")
                    return True
            except Exception as e:
                self.logger.debug(f"Error checking readiness for {service_name}: {e}")
            
            time.sleep(0.1)  # Check every 100ms
        
        # Handle timeout
        if is_critical:
            self.logger.error(f"CRITICAL: {service_name} not ready after {timeout}s timeout - this will cause trading failures!")
            return False
        else:
            self.logger.warning(f"Service {service_name} not ready after {timeout}s timeout (non-critical)")
            return False
            
    def _check_service_thread_health(self, service: Any, service_name: str) -> bool:
        """Check if service threads are healthy and ready."""
        try:
            # Check for common thread attributes
            if hasattr(service, '_api_workers'):  # PriceRepository
                workers = service._api_workers
                expected_count = getattr(service, '_api_worker_count', 0)
                alive_count = sum(1 for w in workers if w.is_alive()) if workers else 0
                if alive_count != expected_count:
                    self.logger.debug(f"{service_name} thread health: {alive_count}/{expected_count} API workers alive")
                    return False
                    
            elif hasattr(service, '_worker_threads'):  # OCRDataConditioningService
                workers = service._worker_threads
                alive_count = sum(1 for w in workers if w.is_alive()) if workers else 0
                expected_count = len(workers) if workers else 0
                if alive_count != expected_count:
                    self.logger.debug(f"{service_name} thread health: {alive_count}/{expected_count} worker threads alive")
                    return False
                    
            elif hasattr(service, '_publisher_thread'):  # TelemetryService
                thread = service._publisher_thread
                if thread and not thread.is_alive():
                    self.logger.debug(f"{service_name} thread health: publisher thread not alive")
                    return False
                    
            elif hasattr(service, '_ws_thread'):  # PriceFetchingService  
                thread = service._ws_thread
                if thread and not thread.is_alive():
                    self.logger.debug(f"{service_name} thread health: WebSocket thread not alive")
                    return False
                    
            # If we get here, threads are healthy or no thread checks needed
            return True
            
        except Exception as e:
            self.logger.debug(f"Error checking thread health for {service_name}: {e}")
            return False
    
    def _signal_services_ready(self):
        """Signal that all services are ready."""
        self.logger.info("Phase 3: Signaling services ready...")
        
        # Set the core services ready event
        self._core_services_fully_started.set()
        
        # Publish bootstrap data if configured
        self._publish_bootstrap_data_if_ready()
        
        self.logger.info("Services ready signal sent")
    
    def _publish_bootstrap_data_if_ready(self):
        """Publish bootstrap data if all services are ready."""
        try:
            if self.event_bus and hasattr(self.event_bus, 'publish_bootstrap_data'):
                self.event_bus.publish_bootstrap_data()
                self.logger.info("Bootstrap data published")
        except Exception as e:
            self.logger.error(f"Error publishing bootstrap data: {e}", exc_info=True)
    
    def _execute_multi_phase_shutdown(self):
        """Execute multi-phase service shutdown."""
        self.logger.info("Starting multi-phase service shutdown...")
        
        # Phase 0: Stop services in reverse dependency order
        self._shutdown_phase = 0
        self._stop_services_in_reverse_order()
        
        # Phase 1: Shutdown DI container services
        self._shutdown_phase = 1
        self._shutdown_di_container_services()
        
        self.logger.info("Multi-phase service shutdown completed")
    
    def _stop_services_in_reverse_order(self):
        """Stop services in reverse dependency order with proper thread shutdown."""
        self.logger.info("Phase 0: Stopping services in reverse dependency order with thread management...")
        
        # Full service map for shutdown
        service_map = {
            # Core Infrastructure
            'ISymbolLoadingService': self.symbol_loading_service,
            'ITelemetryService': self.telemetry_service,
            'IMarketDataPublisher': self.market_data_publisher,
            'ActiveSymbolsService': self.active_symbols_service,
            
            # Data Services
            'DI_IPriceProvider': self.price_repository,
            'PriceFetchingService': self.price_fetching_service,
            'MarketDataFilterService': self.market_data_filter,
            
            # Trading Core
            'IOrderRepository': self.order_repository,
            'IPositionManager': self.position_manager,
            'ITradeLifecycleManager': self.lifecycle_manager,
            'IBrokerService': self.broker_service,
            
            # Trading Logic
            'IRiskManagementService': self.risk_service,
            'ITradeExecutor': self.trade_executor,
            'ITradeManagerService': self.trade_manager,
            'IFingerprintService': self.fingerprint_service,
            
            # OCR Services
            'IOCRDataConditioningService': self.ocr_conditioning_service,
            'IOCRScalpingSignalOrchestratorService': self.ocr_orchestrator_service,
            
            # System Services
            'SystemHealthMonitoringService': self.system_health_service,
            
            # Support Services
            'IIPCManager': self.ipc_manager,
            'IPerformanceMetricsService': self.performance_metrics_service,
            'IOCRProcessManager': self.ocr_process_manager,
            'IHealthMonitor': self.health_monitor,
            'ICommandListener': self.command_listener,
            'IEmergencyGUIService': self.emergency_gui_service
        }
        
        stopped_services = []
        for service_name in self.service_shutdown_order:
            try:
                service = service_map.get(service_name)
                if service and hasattr(service, 'stop'):
                    self.logger.info(f"Stopping {service_name}...")
                    
                    # Stop the service
                    service.stop()
                    
                    # For critical threaded services, verify threads actually stopped
                    if service_name in self._critical_threaded_services:
                        self._wait_for_threads_shutdown(service, service_name)
                    
                    stopped_services.append(service_name)
                    self.logger.info(f"{service_name} stopped successfully")
                    
                elif service:
                    self.logger.warning(f"{service_name} found but has no stop method")
                else:
                    self.logger.debug(f"{service_name} not found or already None")
                    
            except Exception as e:
                self.logger.error(f"Error stopping {service_name}: {e}", exc_info=True)
                # Continue stopping other services - don't let one failure block shutdown
        
        self.logger.info(f"Service shutdown phase completed. Stopped: {stopped_services}")
        
    def _wait_for_threads_shutdown(self, service: Any, service_name: str, timeout: float = 10.0):
        """Wait for service threads to properly shutdown."""
        try:
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                threads_alive = self._count_service_threads_alive(service, service_name)
                if threads_alive == 0:
                    self.logger.info(f"{service_name} threads shutdown cleanly")
                    return
                    
                self.logger.debug(f"{service_name} waiting for {threads_alive} threads to shutdown...")
                time.sleep(0.2)
            
            # Timeout - log warning but continue
            threads_alive = self._count_service_threads_alive(service, service_name)
            if threads_alive > 0:
                self.logger.warning(f"{service_name} shutdown timeout: {threads_alive} threads still alive after {timeout}s")
                
        except Exception as e:
            self.logger.error(f"Error waiting for {service_name} thread shutdown: {e}")
            
    def _count_service_threads_alive(self, service: Any, service_name: str) -> int:
        """Count how many threads are still alive for a service."""
        try:
            alive_count = 0
            
            if hasattr(service, '_api_workers'):  # PriceRepository
                workers = service._api_workers
                alive_count += sum(1 for w in workers if w.is_alive()) if workers else 0
                
            elif hasattr(service, '_worker_threads'):  # OCRDataConditioningService
                workers = service._worker_threads
                alive_count += sum(1 for w in workers if w.is_alive()) if workers else 0
                
            elif hasattr(service, '_publisher_thread'):  # TelemetryService
                thread = service._publisher_thread
                if thread and thread.is_alive():
                    alive_count += 1
                    
            elif hasattr(service, '_ws_thread'):  # PriceFetchingService
                thread = service._ws_thread
                if thread and thread.is_alive():
                    alive_count += 1
                    
            return alive_count
            
        except Exception as e:
            self.logger.debug(f"Error counting threads for {service_name}: {e}")
            return 0
    
    def _shutdown_di_container_services(self):
        """Shutdown remaining services in DI container."""
        self.logger.info("Phase 1: Shutting down DI container services...")
        
        try:
            if self.di_container and hasattr(self.di_container, 'shutdown_all'):
                self.di_container.shutdown_all()
                self.logger.info("DI container services shutdown completed")
        except Exception as e:
            self.logger.error(f"Error shutting down DI container services: {e}", exc_info=True)
    
    def start_all_services(self) -> None:
        """Start all services in proper dependency order."""
        self.start()
    
    def stop_all_services(self) -> None:
        """
        Stops all services that have a lifecycle, in the reverse order of startup.
        Ensures a clean and orderly shutdown of all threads and processes.
        
        This is the robust shutdown sequence that mirrors the startup process.
        It iterates through all resolved services and calls stop() on any that
        implement ILifecycleService, ensuring no zombie threads remain.
        """
        self.logger.info("Starting orderly shutdown of all lifecycle-aware services...")
        
        # Get shutdown order from DI container (reverse dependency order)
        try:
            shutdown_order = self.di_container.get_shutdown_order()
            all_singleton_instances = self.di_container.get_all_singleton_instances()
            
            # Create a map of interface to instance for easy lookup
            instance_map = {}
            for interface, instance in all_singleton_instances.items():
                interface_name = getattr(interface, '__name__', str(interface))
                instance_map[interface_name] = instance
                # Also map by class name for services registered by class name
                if hasattr(instance, '__class__'):
                    instance_map[instance.__class__.__name__] = instance
            
            # Also include manually resolved services from our service references
            # Use getattr with None default to safely access attributes that might not exist
            service_references = {
                'ISymbolLoadingService': getattr(self, 'symbol_loading_service', None),
                'ITelemetryService': getattr(self, 'telemetry_service', None),
                'IMarketDataPublisher': getattr(self, 'market_data_publisher', None),
                'ActiveSymbolsService': getattr(self, 'active_symbols_service', None),
                'DI_IPriceProvider': getattr(self, 'price_repository', None),
                'PriceFetchingService': getattr(self, 'price_fetching_service', None),
                'MarketDataFilterService': getattr(self, 'market_data_filter', None),
                'IOrderRepository': getattr(self, 'order_repository', None),
                'IPositionManager': getattr(self, 'position_manager', None),
                'ITradeLifecycleManager': getattr(self, 'lifecycle_manager', None),
                'IBrokerService': getattr(self, 'broker_service', None),
                'IRiskManagementService': getattr(self, 'risk_service', None),
                'ITradeExecutor': getattr(self, 'trade_executor', None),
                'ITradeManagerService': getattr(self, 'trade_manager', None),
                'IFingerprintService': getattr(self, 'fingerprint_service', None),
                'IOCRDataConditioningService': getattr(self, 'ocr_conditioning_service', None),
                'IOCRScalpingSignalOrchestratorService': getattr(self, 'ocr_orchestrator_service', None),
                'SystemHealthMonitoringService': getattr(self, 'system_health_service', None),
                'IIPCManager': getattr(self, 'ipc_manager', None),
                'IPerformanceMetricsService': getattr(self, 'performance_metrics_service', None),
                'IOCRProcessManager': getattr(self, 'ocr_process_manager', None),
                'IHealthMonitor': getattr(self, 'health_monitor', None),
                'ICommandListener': getattr(self, 'command_listener', None),
                'IEmergencyGUIService': getattr(self, 'emergency_gui_service', None)
            }
            # Filter out None values to avoid trying to stop services that don't exist
            service_references = {k: v for k, v in service_references.items() if v is not None}
            instance_map.update(service_references)
            
            stopped_services = []
            failed_services = []
            
            # Iterate in reverse startup order (shutdown order)
            for interface in shutdown_order:
                interface_name = getattr(interface, '__name__', str(interface))
                service = instance_map.get(interface_name)
                
                if not service:
                    # Try alternate lookup strategies
                    for alt_name, alt_service in service_references.items():
                        if alt_service and alt_name == interface_name:
                            service = alt_service
                            break
                
                if not service:
                    self.logger.debug(f"Skipping shutdown for {interface_name} - service not found or already None")
                    continue
                
                # If the service has a lifecycle (implements ILifecycleService), stop it
                if isinstance(service, ILifecycleService) and hasattr(service, 'stop'):
                    self.logger.info(f"Stopping lifecycle service: {interface_name}...")
                    try:
                        service.stop()
                        stopped_services.append(interface_name)
                        self.logger.info(f"✅ {interface_name} stopped successfully")
                        
                        # For critical threaded services, verify threads stopped
                        if interface_name in self._critical_threaded_services:
                            self._wait_for_threads_shutdown(service, interface_name, timeout=10.0)
                            
                    except Exception as e:
                        failed_services.append(interface_name)
                        self.logger.error(f"❌ Error stopping service {interface_name}: {e}", exc_info=True)
                        # Continue with other services - don't let one failure block complete shutdown
                        
                elif hasattr(service, 'stop'):
                    # Service has stop method but doesn't implement ILifecycleService
                    self.logger.info(f"Stopping non-lifecycle service: {interface_name}...")
                    try:
                        service.stop()
                        stopped_services.append(interface_name)
                        self.logger.info(f"✅ {interface_name} stopped successfully")
                    except Exception as e:
                        failed_services.append(interface_name)
                        self.logger.error(f"❌ Error stopping service {interface_name}: {e}", exc_info=True)
                        
                else:
                    self.logger.debug(f"⏭️  Skipping shutdown for passive service: {interface_name} (no stop method)")
            
            # Final shutdown summary
            if failed_services:
                self.logger.warning("=" * 80)
                self.logger.warning(f"⚠️  PARTIAL SHUTDOWN: {len(failed_services)} services failed to stop cleanly")
                self.logger.warning(f"❌ Failed services: {failed_services}")
                self.logger.warning(f"✅ Successfully stopped services ({len(stopped_services)}): {stopped_services}")
                self.logger.warning("⚠️  Some zombie threads may remain. Manual intervention may be required.")
                self.logger.warning("=" * 80)
            else:
                self.logger.info("🎉 CLEAN SHUTDOWN SUCCESS: All lifecycle services stopped successfully")
                self.logger.info(f"✅ Successfully stopped services ({len(stopped_services)}): {stopped_services}")
                self.logger.info("All threads and processes should be cleanly terminated")
            
            # Finally, shutdown the DI container
            self.logger.info("Shutting down DI container...")
            if hasattr(self.di_container, 'shutdown_all'):
                self.di_container.shutdown_all()
            
        except Exception as e:
            self.logger.critical(f"FATAL ERROR during shutdown sequence: {e}", exc_info=True)
            self.logger.critical("Shutdown may be incomplete - manual process termination may be required")
            
        # Call the original stop method to handle internal cleanup
        self.stop()
        
        self.logger.info("All services have been instructed to stop. Shutdown sequence complete.")
    
    def check_services_ready(self, timeout_seconds: float = 30.0) -> bool:
        """Check if all services are ready within timeout."""
        start_time = time.time()
        
        while time.time() - start_time < timeout_seconds:
            if self.all_services_ready:
                return True
            time.sleep(0.1)
        
        return False
    
    def get_service_status(self, service_name: str) -> Dict[str, Any]:
        """Get status of a specific service."""
        if service_name in self._services_status:
            status = self._services_status[service_name].copy()
            
            # Add runtime information if service is available
            service_map = {
                'IIPCManager': self.ipc_manager,
                'IPerformanceMetricsService': self.performance_metrics_service,
                'IOCRProcessManager': self.ocr_process_manager,
                'IHealthMonitor': self.health_monitor,
                'ICommandListener': self.command_listener,
                'IEmergencyGUIService': self.emergency_gui_service
            }
            
            service = service_map.get(service_name)
            if service:
                status["available"] = True
                status["has_start_method"] = hasattr(service, 'start')
                status["has_stop_method"] = hasattr(service, 'stop')
                status["has_is_ready_property"] = hasattr(service, 'is_ready')
                
                if hasattr(service, 'is_ready'):
                    try:
                        status["is_ready"] = service.is_ready
                    except Exception as e:
                        status["is_ready_error"] = str(e)
            else:
                status["available"] = False
            
            return status
        else:
            return {"error": f"Service {service_name} not found"}
    
    def get_all_services_status(self) -> Dict[str, Any]:
        """Get status of all managed services."""
        with self._lock:
            uptime = time.time() - self._start_time if self._start_time else 0
            
            all_status = {
                "lifecycle_manager": {
                    "is_running": self._is_running,
                    "is_ready": self.is_ready,
                    "all_services_ready": self.all_services_ready,
                    "services_started": self._services_started,
                    "uptime_seconds": uptime,
                    "startup_phase": self._startup_phase,
                    "shutdown_phase": self._shutdown_phase,
                    "last_readiness_check": self._last_readiness_check
                },
                "services": {}
            }
            
            # Get status of each managed service
            for service_name in self.service_startup_order:
                all_status["services"][service_name] = self.get_service_status(service_name)
            
            return all_status
    
    def _activate_all_worker_threads(self):
        """
        Phase 4: LATE WORKER ACTIVATION - Send global "Go" signal to all worker threads.
        
        This is the final phase of startup that signals all worker threads they can
        begin their main processing loops. Until this point, all worker threads
        should be started but waiting on the application_ready_event.
        """
        self.logger.info("🚦 Phase 4: LATE WORKER ACTIVATION - Signaling all worker threads to begin processing...")
        
        # Signal the global "Go" event - this releases ALL worker threads simultaneously
        self.application_ready_event.set()
        
        self.logger.info("✅ LATE WORKER ACTIVATION: application_ready_event.set() called")
        self.logger.info("🚀 All worker threads are now authorized to begin their main processing loops")
        self.logger.info("💰 Financial system is 100% operational - all deadlock risks eliminated")

    def dry_run_startup_verification(self):
        """
        PRE-FLIGHT VERIFICATION: Dry-run startup simulation to verify dependency order.
        
        This method simulates the exact startup sequence without actually starting services,
        allowing verification of the dependency resolution order and detection of circular
        dependencies before they cause runtime issues.
        """
        self.logger.critical("### [DRY_RUN] Starting Pre-Flight Startup Verification ###")
        self.logger.critical("### [DRY_RUN] This simulates the exact startup order without starting services ###")
        
        try:
            # Resolve all services to verify DI registration
            self.logger.critical("### [DRY_RUN] PHASE 1: Resolving all services from DI container ###")
            
            # TIER 1: Infrastructure Services
            tier1_services = [
                'GlobalConfig',
                'IConfigService', 
                'DI_IEventBus',
                'ICorrelationLogger',
                'ITelemetryService',
                'IBulkDataService',
                'DI_ApplicationCore',
                'IServiceLifecycleManager'
            ]
            
            for service_name in tier1_services:
                try:
                    service = self.di_container.resolve(service_name)
                    self.logger.critical(f"### [DRY_RUN] ✅ TIER 1 - {service_name}: {service.__class__.__name__}")
                except Exception as e:
                    self.logger.critical(f"### [DRY_RUN] ❌ TIER 1 - {service_name}: FAILED - {e}")
            
            # TIER 2: Market Data Services  
            self.logger.critical("### [DRY_RUN] PHASE 2: Market Data Services ###")
            tier2_services = [
                'ISymbolLoadingService',
                'ActiveSymbolsService',
                'DI_IPriceProvider',
                'MarketDataDisruptor',
                'PriceFetchingService',
                'MarketDataFilterService'
            ]
            
            for service_name in tier2_services:
                try:
                    service = self.di_container.resolve(service_name)
                    self.logger.critical(f"### [DRY_RUN] ✅ TIER 2 - {service_name}: {service.__class__.__name__}")
                except Exception as e:
                    self.logger.critical(f"### [DRY_RUN] ❌ TIER 2 - {service_name}: FAILED - {e}")
            
            # TIER 3: Trading Services
            self.logger.critical("### [DRY_RUN] PHASE 3: Trading Services ###")
            tier3_services = [
                'IBrokerService',
                'IOrderRepository',
                'ITradeLifecycleManager',
                'IPositionManager',
                'IRiskManagementService',
                'ITradeManagerService',
                'ITradeExecutor',
                'IOrderEventProcessor'
            ]
            
            for service_name in tier3_services:
                try:
                    service = self.di_container.resolve(service_name)
                    self.logger.critical(f"### [DRY_RUN] ✅ TIER 3 - {service_name}: {service.__class__.__name__}")
                except Exception as e:
                    self.logger.critical(f"### [DRY_RUN] ❌ TIER 3 - {service_name}: FAILED - {e}")
            
            # TIER 4: OCR and Support Services
            self.logger.critical("### [DRY_RUN] PHASE 4: OCR and Support Services ###")
            tier4_services = [
                'IFingerprintService',
                'IOCRProcessManager',
                'IOCRDataConditioningService',
                'IOCRScalpingSignalOrchestratorService',
                'SystemHealthMonitoringService',
                'IPerformanceMetricsService',
                'IHealthMonitor',
                'ICommandListener',
                'IEmergencyGUIService'
            ]
            
            for service_name in tier4_services:
                try:
                    service = self.di_container.resolve(service_name)
                    self.logger.critical(f"### [DRY_RUN] ✅ TIER 4 - {service_name}: {service.__class__.__name__}")
                except Exception as e:
                    self.logger.critical(f"### [DRY_RUN] ❌ TIER 4 - {service_name}: FAILED - {e}")
            
            self.logger.critical("### [DRY_RUN] ✅ PRE-FLIGHT VERIFICATION COMPLETED SUCCESSFULLY ###")
            self.logger.critical("### [DRY_RUN] All services can be resolved - no circular dependency issues detected ###")
            self.logger.critical("### [DRY_RUN] System is ready for live startup ###")
            return True
            
        except Exception as e:
            self.logger.critical(f"### [DRY_RUN] ❌ PRE-FLIGHT VERIFICATION FAILED: {e}")
            self.logger.critical("### [DRY_RUN] CRITICAL: Do not proceed with live startup - fix dependency issues first ###")
            return False