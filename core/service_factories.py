"""
Service Factories for Complex Service Creation

This module contains factory classes that handle the creation of complex services
with multiple dependencies. These factories encapsulate the intricate dependency
wiring logic that was previously scattered throughout ApplicationCore.

Each factory is responsible for:
1. Resolving all required dependencies from the DI container
2. Creating the service instance with proper configuration
3. Handling any special initialization requirements
"""

import logging
from typing import Any

# Factory interfaces have been removed - using concrete factories directly
from interfaces.risk.services import IRiskManagementService
from interfaces.trading.services import ITradeManagerService, ITradeExecutor, IOrderStrategy
from interfaces.order_management.services import IOrderRepository
from interfaces.ocr.services import IOCRDataConditioningService, IOCRScalpingSignalOrchestratorService
from interfaces.core.services import (
    IEventBus as DI_IEventBus, IConfigService, IPipelineValidator, IPerformanceBenchmarker
)
from interfaces.data.services import IPriceProvider as DI_IPriceProvider
from interfaces.broker.services import IBrokerService
from interfaces.trading.services import ITradeLifecycleManager
from interfaces.core.application import IApplicationCore
from interfaces.core.telemetry_interfaces import ITelemetryService

# Import PositionManager interface from trading interfaces
from interfaces.trading.services import IPositionManager
from interfaces.trading.services import ITradeLifecycleManager

# Import IMarketDataPublisher from data interfaces
from interfaces.data.services import IMarketDataPublisher

# Import concrete implementations
from modules.risk_management.risk_service import RiskManagementService
from modules.trade_management.trade_manager_service import TradeManagerService
from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService
from modules.trade_management.ocr_scalping_signal_orchestrator_service import OCRScalpingSignalOrchestratorService

logger = logging.getLogger(__name__)


class RiskServiceFactory:
    """Factory for creating RiskManagementService with all dependencies."""

    def create_risk_service(self, container) -> IRiskManagementService:
        """Create RiskManagementService with all required dependencies."""
        logger.debug("Creating RiskManagementService via factory")

        # Resolve all dependencies using container.resolve(IDependencyInterface)
        event_bus = container.resolve(DI_IEventBus)
        price_provider = container.resolve(DI_IPriceProvider)
        config_service = container.resolve(IConfigService)

        # Use canonical IPositionManager interface
        position_manager = container.resolve(IPositionManager)

        pipeline_validator = container.resolve(IPipelineValidator)
        benchmarker = container.resolve(IPerformanceBenchmarker)
        telemetry_service = container.resolve(ITelemetryService)

        # Clean architecture - no ApplicationCore dependency

        # Try to resolve CorrelationLogger from IntelliSense (optional)
        correlation_logger = None
        try:
            from intellisense.capture.interfaces import ICorrelationLogger
            correlation_logger = container.resolve(ICorrelationLogger)
            logger.debug("CorrelationLogger resolved for RiskManagementService")
        except Exception:
            logger.debug("CorrelationLogger not available for RiskManagementService")

        # GUI logger function removed - clean architecture
        gui_logger_func = None

        # Create the service
        risk_service = RiskManagementService(
            event_bus=event_bus,
            config_service=config_service,
            price_provider=price_provider,
            position_manager=position_manager,
            gui_logger_func=gui_logger_func,  # Pass GUI logger function
            benchmarker=benchmarker,
            pipeline_validator=pipeline_validator,
            correlation_logger=correlation_logger,  # Pass CorrelationLogger
            # ApplicationCore dependency removed - using telemetry service
            telemetry_service=telemetry_service
        )

        logger.info("RiskManagementService created successfully")
        return risk_service


class TradeManagerFactory:
    """Factory for creating TradeManagerService with all dependencies."""

    def create_trade_manager(self, container) -> ITradeManagerService:
        """Create TradeManagerService with all required dependencies."""
        logger.debug("Creating TradeManagerService via factory")

        # Resolve all dependencies using container.resolve(IDependencyInterface)
        event_bus = container.resolve(DI_IEventBus)
        broker_service = container.resolve(IBrokerService)
        price_provider = container.resolve(DI_IPriceProvider)
        config_service = container.resolve(IConfigService)
        order_repository = container.resolve(IOrderRepository)

        # Use canonical IPositionManager interface
        position_manager = container.resolve(IPositionManager)

        order_strategy = container.resolve(IOrderStrategy)
        trade_executor = container.resolve(ITradeExecutor)
        lifecycle_manager = container.resolve(ITradeLifecycleManager)
        pipeline_validator = container.resolve(IPipelineValidator)
        benchmarker = container.resolve(IPerformanceBenchmarker)
        telemetry_service = container.resolve(ITelemetryService)
        # ApplicationCore dependency removed

        # Create the service
        trade_manager = TradeManagerService(
            event_bus=event_bus,
            broker_service=broker_service,
            price_provider=price_provider,
            config_service=config_service,
            order_repository=order_repository,
            position_manager=position_manager,
            order_strategy=order_strategy,
            trade_executor=trade_executor,
            lifecycle_manager=lifecycle_manager,
            pipeline_validator=pipeline_validator,
            benchmarker=benchmarker,
            # ApplicationCore dependency removed,  # Pass ApplicationCore reference for Redis publishing
            telemetry_service=telemetry_service  # MIGRATED: Clean telemetry architecture
        )

        logger.info("TradeManagerService created successfully")
        return trade_manager


class OCRServiceFactory:
    """Factory for creating OCR-related services with all dependencies."""
    
    def create_ocr_conditioning_service(self, container) -> IOCRDataConditioningService:
        """Create OCRDataConditioningService with custom dependencies."""
        logger.debug("Creating OCRDataConditioningService via streamlined factory")

        # Resolve core dependencies using container.resolve(IDependencyInterface)
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        price_provider = container.resolve(DI_IPriceProvider)
        pipeline_validator = container.resolve(IPipelineValidator)
        benchmarker = container.resolve(IPerformanceBenchmarker)
        telemetry_service = container.resolve(ITelemetryService)
        # ApplicationCore dependency removed

        # Create specialized dependencies that aren't in DI container
        from modules.ocr.python_ocr_data_conditioner import PythonOCRDataConditioner
        from utils.observability_data_publisher import ObservabilityDataPublisher

        # Clean architecture: Use telemetry service for publishing
        conditioner = PythonOCRDataConditioner(
            config_service=config_service,
            price_provider=price_provider,
            telemetry_service=telemetry_service,
            # ApplicationCore dependency removed
        )
        observability_publisher = ObservabilityDataPublisher(config_service=config_service)

        # Create service with orchestrator_service=None (set later to avoid circular dependency)
        ocr_conditioning = OCRDataConditioningService(
            event_bus=event_bus,
            conditioner=conditioner,
            orchestrator_service=None,  # Set later
            observability_publisher=observability_publisher,
            config_service=config_service,
            benchmarker=benchmarker,
            pipeline_validator=pipeline_validator,
            price_provider=price_provider,
            # ApplicationCore dependency removed - using telemetry service
            telemetry_service=telemetry_service
        )

        logger.info("OCRDataConditioningService created successfully")
        return ocr_conditioning
        
    def create_ocr_orchestrator_service(self, container) -> IOCRScalpingSignalOrchestratorService:
        """Create OCRScalpingSignalOrchestratorService with specialized dependencies."""
        logger.debug("Creating OCRScalpingSignalOrchestratorService via streamlined factory")

        # Resolve core dependencies using container.resolve(IDependencyInterface)
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        price_provider = container.resolve(DI_IPriceProvider)

        # Use canonical IPositionManager interface
        position_manager = container.resolve(IPositionManager)

        pipeline_validator = container.resolve(IPipelineValidator)
        benchmarker = container.resolve(IPerformanceBenchmarker)
        telemetry_service = container.resolve(ITelemetryService)
        # ApplicationCore dependency removed

        # Create specialized services that need custom instantiation
        interpreter_service = None
        action_filter_service = None

        try:
            from modules.trade_management.snapshot_interpreter_service import SnapshotInterpreterService
            from modules.trade_management.trade_lifecycle_manager_service import TradeLifecycleManagerService
            from modules.trade_management.master_action_filter_service import MasterActionFilterService

            # Resolve IPC client for both SnapshotInterpreterService and MasterActionFilterService
            ipc_client_instance = None
            try:
                from interfaces.core.services import IBulletproofBabysitterIPCClient
                ipc_client_instance = container.resolve(IBulletproofBabysitterIPCClient)
                logger.info("IBulletproofBabysitterIPCClient resolved for SnapshotInterpreterService and MasterActionFilterService.")
            except Exception as e:
                logger.debug(f"IBulletproofBabysitterIPCClient not available: {e}")

            order_repository = container.resolve(IOrderRepository)
            lifecycle_manager = TradeLifecycleManagerService(
                order_repository=order_repository,
                position_manager=position_manager,
                event_bus=event_bus,
                # ApplicationCore dependency removed
            )

            interpreter_service = SnapshotInterpreterService(
                lifecycle_manager=lifecycle_manager,
                config_service=config_service,
                position_manager=position_manager,
                order_repository=order_repository,
                telemetry_service=telemetry_service,  # CRITICAL FIX: Add missing telemetry_service
                ipc_client=ipc_client_instance,  # Pass IPC client for signal decision publishing
                # ApplicationCore dependency removed
            )

            action_filter_service = MasterActionFilterService(
                config_service=config_service,
                price_provider=price_provider,
                ipc_client=ipc_client_instance,  # Pass the resolved IPC client
                # ApplicationCore dependency removed
            )

            logger.info("SnapshotInterpreterService and MasterActionFilterService created successfully")
        except Exception as e:
            logger.warning(f"Could not create specialized services: {e}")

        # Create the orchestrator service
        ocr_orchestrator = OCRScalpingSignalOrchestratorService(
            event_bus=event_bus,
            price_provider=price_provider,
            position_manager=position_manager,
            config_service=config_service,
            interpreter_service=interpreter_service,
            action_filter_service=action_filter_service,
            benchmarker=benchmarker,
            pipeline_validator=pipeline_validator,
            # ApplicationCore dependency removed - using telemetry service
            telemetry_service=telemetry_service  # MIGRATED: Clean telemetry architecture
        )

        logger.info("OCRScalpingSignalOrchestratorService created successfully")
        return ocr_orchestrator


class PriceProviderFactory:
    """Factory for creating PriceRepository (IPriceProvider) with all dependencies."""

    def create_price_provider(self, container) -> DI_IPriceProvider:
        """Create PriceRepository with all required dependencies."""
        logger.debug("Creating PriceRepository via factory")

        # Resolve all dependencies using container.resolve(IDependencyInterface)
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        market_data_publisher = container.resolve(IMarketDataPublisher)
        # ApplicationCore dependency removed
        telemetry_service = container.resolve(ITelemetryService)

        # --- CRITICAL FIX: Create REST client directly (clean architecture) ---
        alpaca_client_for_pr = None
        try:
            import alpaca_trade_api as tradeapi
            # Access config through ConfigService interface
            if hasattr(config_service, 'get_config'):
                raw_config = config_service.get_config()
            else:
                raw_config = config_service
                
            if hasattr(raw_config, 'ALPACA_API_KEY') and raw_config.ALPACA_API_KEY:
                alpaca_client_for_pr = tradeapi.REST(
                    key_id=raw_config.ALPACA_API_KEY,
                    secret_key=raw_config.ALPACA_API_SECRET,
                    base_url=raw_config.ALPACA_BASE_URL,
                    api_version='v2'
                )
                logger.info(f"PriceRepositoryFactory: Created Alpaca REST client for PriceRepository")
            else:
                logger.warning("PriceRepositoryFactory: Alpaca API keys not available, REST client disabled")
        except ImportError:
            logger.warning("PriceRepositoryFactory: alpaca_trade_api not available, REST client disabled")
        except Exception as e:
            logger.error(f"PriceRepositoryFactory: Failed to create Alpaca REST client: {e}")
            
        log_msg = f"PriceRepositoryFactory: Passing rest_client of type {type(alpaca_client_for_pr)} to PriceRepository."
        if alpaca_client_for_pr is None:
            logger.warning(log_msg + " Fallback will be disabled.")
        else:
            logger.info(log_msg)

        # Resolve optional dependencies safely
        pipeline_validator = None
        try:
            pipeline_validator = container.resolve(IPipelineValidator)
        except Exception:
            logger.debug("PipelineValidator not available for PriceRepository")

        benchmarker = None
        try:
            benchmarker = container.resolve(IPerformanceBenchmarker)
        except Exception:
            logger.debug("PerformanceBenchmarker not available for PriceRepository")

        # Resolve ActiveSymbolsService
        active_symbols_service = None
        try:
            active_symbols_service = container.resolve('ActiveSymbolsService')  # String key for now
        except Exception:
            logger.debug("ActiveSymbolsService not available for PriceRepository")

        # Create the PriceRepository
        from modules.price_fetching.price_repository import PriceRepository
        price_repository = PriceRepository(
            event_bus=event_bus,
            config_service=config_service,
            market_data_publisher=market_data_publisher,
            telemetry_service=telemetry_service,
            active_symbols_service=active_symbols_service,  # CRITICAL FIX: Pass ActiveSymbolsService
            risk_service=None,  # Will be set later if needed
            observability_publisher=None,  # Optional
            rest_client=alpaca_client_for_pr,  # <<< PASS THE CORRECT INSTANCE HERE
            pipeline_validator=pipeline_validator,
            benchmarker=benchmarker,
        )

        logger.info("PriceRepository created successfully")
        return price_repository


class PositionManagerFactory:
    """Factory for creating PositionManager with proper TradeLifecycleManager dependency injection."""

    def create_position_manager(self, container) -> IPositionManager:
        """
        Factory method to create and wire up the PositionManager.
        This orchestration breaks the circular dependency using LazyProxy.
        """
        logger.debug("PositionManagerFactory: Starting LazyProxy creation process.")

        # Resolve direct dependencies
        from interfaces.core.services import IEventBus, IConfigService, IBulletproofBabysitterIPCClient
        from interfaces.order_management.services import IOrderRepository
        from interfaces.trading.services import ITradeLifecycleManager
        from interfaces.data.services import IPriceProvider as DI_IPriceProvider
        from interfaces.logging.services import ICorrelationLogger
        event_bus = container.resolve(IEventBus)
        correlation_logger = container.resolve(ICorrelationLogger)
        order_repository = container.resolve(IOrderRepository)
        config_service = container.resolve(IConfigService)
        
        # Use LazyProxy for IPriceProvider to break circular dependency
        from core.dependency_injection import LazyProxy
        price_provider = LazyProxy(container, DI_IPriceProvider)
        
        # Try to resolve IPC client (optional)
        ipc_client = None
        try:
            ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
        except Exception:
            logger.debug("IPC client not available for PositionManager")
            
        # Resolve broker service for initial position loading
        broker_service = None
        try:
            from interfaces.broker.services import IBrokerService
            broker_service = container.resolve(IBrokerService)
        except Exception as e:
            logger.warning(f"PositionManagerFactory: Could not resolve IBrokerService: {e}")
        
        # Create LazyProxy for TradeLifecycleManager to break circular dependency
        from core.dependency_injection import LazyProxy
        trade_lifecycle_manager_proxy = LazyProxy(container, ITradeLifecycleManager)
        logger.debug("PositionManagerFactory: LazyProxy created for TradeLifecycleManager.")
        
        # Create PositionManager with LazyProxy - no circular dependency!
        from modules.trade_management.position_manager import PositionManager
        position_manager = PositionManager(
            event_bus=event_bus,
            correlation_logger=correlation_logger,
            trade_lifecycle_manager=trade_lifecycle_manager_proxy,  # LazyProxy instead of real instance
            order_repository=order_repository,  # CRITICAL FIX: Proper DI injection
            config_service=config_service,  # NEW: Clean DI injection
            price_provider=price_provider,  # NEW: Clean DI injection
            ipc_client=ipc_client,  # NEW: Clean DI injection
            broker_service=broker_service  # NEW: Broker service for initial position loading
        )
        
        logger.info("PositionManagerFactory: PositionManager created with LazyProxy and clean DI!")
        return position_manager


class ServiceFactoryRegistry:
    """
    Registry for all service factories.
    
    This class provides a centralized way to access all service factories
    and ensures they are properly configured with the DI container.
    """
    
    def __init__(self):
        self._risk_factory = RiskServiceFactory()
        self._trade_manager_factory = TradeManagerFactory()
        self._ocr_factory = OCRServiceFactory()
        self._price_provider_factory = PriceProviderFactory()
        self._position_manager_factory = PositionManagerFactory()
        
    # Properties removed - factory interfaces no longer exist
    # Use the factories directly instead
        
    @property
    def position_manager_factory(self) -> PositionManagerFactory:
        """Get the position manager factory."""
        return self._position_manager_factory
        
    def register_factories_with_container(self, container):
        """
        Register all factories with the DI container.
        Crucially, register methods that PRODUCE the service, not just the factory object.

        Args:
            container: DI container to register factories with
        """
        # For RiskServiceFactory
        # We register the 'create_risk_service' method of our factory instance
        # against the IRiskManagementService interface.
        container.register_factory(
            IRiskManagementService,
            self._risk_factory.create_risk_service # Pass the bound method
        )
        logger.info("ServiceFactoryRegistry: Registered factory for IRiskManagementService.")

        # For TradeManagerFactory
        container.register_factory(
            ITradeManagerService,
            self._trade_manager_factory.create_trade_manager # Pass the bound method
        )
        logger.info("ServiceFactoryRegistry: Registered factory for ITradeManagerService.")

        # For OCRServiceFactory (it has two creation methods)
        container.register_factory(
            IOCRDataConditioningService,
            self._ocr_factory.create_ocr_conditioning_service # Pass the bound method
        )
        logger.info("ServiceFactoryRegistry: Registered factory for IOCRDataConditioningService.")

        container.register_factory(
            IOCRScalpingSignalOrchestratorService,
            self._ocr_factory.create_ocr_orchestrator_service # Pass the bound method
        )
        logger.info("ServiceFactoryRegistry: Registered factory for IOCRScalpingSignalOrchestratorService.")

        # Register the factory method for DI_IPriceProvider
        container.register_factory(DI_IPriceProvider, self._price_provider_factory.create_price_provider)
        logger.info("ServiceFactoryRegistry: Registered factory for DI_IPriceProvider.")

        # Register PositionManager factory with proper circular dependency handling
        container.register_factory(
            IPositionManager,
            self._position_manager_factory.create_position_manager
        )
        logger.info("ServiceFactoryRegistry: Registered factory for IPositionManager with circular dependency resolution.")

        # Also register factory instances for any code that might need the factory objects themselves
        # Factory interfaces no longer exist - register factories as concrete instances
        container.register_instance(RiskServiceFactory, self._risk_factory)
        container.register_instance(TradeManagerFactory, self._trade_manager_factory)
        container.register_instance(OCRServiceFactory, self._ocr_factory)
        container.register_instance(PriceProviderFactory, self._price_provider_factory)

        logger.info("All service product factories (from ServiceFactoryRegistry) registered with DI container")


def create_factory_registry() -> ServiceFactoryRegistry:
    """
    Create and configure the service factory registry.
    
    Returns:
        Configured ServiceFactoryRegistry instance
    """
    registry = ServiceFactoryRegistry()
    logger.info("Service factory registry created")
    return registry
