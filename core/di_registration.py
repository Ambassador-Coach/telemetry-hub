"""
Centralized Dependency Injection Registration

This module contains all service registrations for the TESTRADE application.
It isolates ApplicationCore from the details of service creation and 
dependency management, making the system more maintainable and testable.

By centralizing all DI registrations here, we achieve:
- Clear separation of concerns
- Easier testing and mocking
- Simplified ApplicationCore
- Better dependency visibility
"""

import logging
from typing import Any, Optional
from core.dependency_injection import DIContainer, LazyProxy
from utils.testrade_modes import requires_telemetry_service, requires_ipc_services
from interfaces.core.services import (
    IEventBus as DI_IEventBus, IConfigService, IPipelineValidator,
    IPerformanceBenchmarker, ILifecycleService, IBulkDataService
)
from interfaces.core.application import IApplicationCore as DI_ApplicationCore
from interfaces.data.services import IPriceProvider as DI_IPriceProvider
from interfaces.broker.services import IBrokerService
from interfaces.trading.services import (
    ITradeExecutor, IOrderStrategy, ITradeManagerService, IPositionManager
)
from interfaces.order_management.services import IOrderRepository
from interfaces.trading.services import ITradeLifecycleManager
from interfaces.risk.services import IRiskManagementService
from interfaces.ocr.services import (
    IOCRDataConditioningService, IOCRScalpingSignalOrchestratorService, 
    IROIService, IOCRParametersService
)
from interfaces.ocr.services import IOCRProcessManager
from interfaces.utility.services import ISymbolLoadingService, IFingerprintService
from interfaces.core.services import (
    IBulletproofBabysitterIPCClient, ICommandListener, 
    IIPCManager, IHealthMonitor, IPerformanceMetricsService, 
    IEmergencyGUIService, IServiceLifecycleManager
)
from interfaces.core.telemetry_interfaces import ITelemetryService
from interfaces.data.services import IMarketDataPublisher
from interfaces.logging.services import ICorrelationLogger
from modules.correlation_logging.loggers import NullCorrelationLogger, TelemetryCorrelationLogger
from utils.global_config import GlobalConfig

logger = logging.getLogger(__name__)


def register_all_services(container: DIContainer) -> None:
    """
    Register all services with the DI container.
    
    This function contains all service registrations that were previously
    scattered throughout ApplicationCore. The registrations are organized
    by service type for clarity.
    
    Args:
        container: The DI container to register services with
    """
    logger.info("Starting centralized service registration...")
    
    # Core Infrastructure Services
    _register_infrastructure_services(container)
    
    # Market Data and Price Services
    _register_market_data_services(container)
    
    # Broker Services - ESSENTIAL
    _register_broker_services(container)
    
    # Trade Management Services
    _register_trade_management_services(container)
    
    # OCR Services 
    _register_ocr_services(container)
    
    # Risk and Compliance Services
    _register_risk_services(container)
    
    # Utility Services
    _register_utility_services(container)
    
    logger.info("Centralized service registration completed successfully")


def _register_infrastructure_services(container: DIContainer) -> None:
    """Register core infrastructure services."""
    logger.debug("Registering infrastructure services...")
    
    # GlobalConfig factory - the raw config object
    # Only register if not already registered by ApplicationCore
    if GlobalConfig not in container._services:
        def global_config_factory(container: DIContainer):
            from utils.global_config import config as global_config_instance
            return global_config_instance
        
        container.register_factory(GlobalConfig, global_config_factory)
        logger.debug("Registered GlobalConfig factory (fallback)")
    else:
        logger.debug("GlobalConfig already registered, skipping factory registration")
    
    # ConfigService factory - wraps GlobalConfig
    # Only register if not already registered by ApplicationCore
    if IConfigService not in container._services:
        def config_service_factory(container: DIContainer):
            from core.config_service import ConfigService
            global_config_instance = container.resolve(GlobalConfig)
            return ConfigService(global_config_instance)
        
        container.register_factory(IConfigService, config_service_factory)
        logger.debug("Registered IConfigService factory (fallback)")
    else:
        logger.debug("IConfigService already registered, skipping factory registration")
    
    
    # EventBus factory
    def event_bus_factory(container: DIContainer):
        from core.event_bus import EventBus
        return EventBus()
    
    container.register_factory(DI_IEventBus, event_bus_factory)
    
    # Correlation Logger factory
    def correlation_logger_factory(container: DIContainer):
        """
        This factory determines which ICorrelationLogger implementation to use.
        It checks a config flag to decide between the live Telemetry logger
        and the no-op Null logger. This is the central switch for enabling
        or disabling detailed IntelliSense logging.
        """
        config = container.resolve(IConfigService)
        
        # Check for a master flag. If True, use the live logger. Otherwise, use the null logger.
        # This flag should be set to False for TANK MODE.
        is_intellisense_enabled = getattr(config, 'ENABLE_INTELLISENSE_LOGGING', False)

        if is_intellisense_enabled and requires_telemetry_service():
            logger.info("DI Registration: IntelliSense logging is ENABLED. Providing TelemetryCorrelationLogger.")
            # This logger needs the telemetry service to function
            try:
                telemetry_service = container.resolve(ITelemetryService)
                return TelemetryCorrelationLogger(telemetry_service=telemetry_service)
            except Exception as e:
                logger.warning(f"DI Registration: Telemetry service not available, falling back to NullCorrelationLogger: {e}")
                return NullCorrelationLogger()
        else:
            logger.info("DI Registration: IntelliSense logging is DISABLED (TANK MODE). Providing NullCorrelationLogger.")
            return NullCorrelationLogger()

    container.register_factory(ICorrelationLogger, correlation_logger_factory)
    
    # Performance Benchmarker factory
    def performance_benchmarker_factory(container: DIContainer):
        from modules.monitoring.performance_benchmarker import PerformanceBenchmarker
        config = container.resolve(IConfigService)
        return PerformanceBenchmarker(
            redis_prefix=getattr(config, 'perf_redis_prefix', "testrade_perf"),
            metric_ttl_seconds=int(getattr(config, 'perf_metric_ttl_seconds', 604800)),
            max_entries_per_metric=int(getattr(config, 'perf_max_entries_per_metric', 10000))
        )
    
    container.register_factory(IPerformanceBenchmarker, performance_benchmarker_factory)
    
    # Pipeline Validator factory
    def pipeline_validator_factory(container: DIContainer):
        from modules.monitoring.pipeline_validator import PipelineValidator
        
        # Resolve specific services instead of ApplicationCore
        ocr_conditioning_service = None
        price_provider = None
        
        try:
            ocr_conditioning_service = container.resolve(IOCRDataConditioningService)
        except Exception:
            logger.debug("OCRDataConditioningService not available for PipelineValidator")
            
        try:
            price_provider = container.resolve(DI_IPriceProvider)
        except Exception:
            logger.debug("PriceProvider not available for PipelineValidator")
            
        return PipelineValidator(
            ocr_conditioning_service=ocr_conditioning_service,
            price_provider=price_provider
        )
    
    container.register_factory(IPipelineValidator, pipeline_validator_factory)
    
    # Babysitter IPC Client factory
    # Bulletproof IPC Client factory - needed for TelemetryService
    def babysitter_ipc_factory(container: DIContainer):
        # Only create IPC client if required by mode
        if not requires_ipc_services():
            logger.info("DI Registration: IPC services not required in current mode, returning None")
            return None
            
        from core.bulletproof_ipc_client import BulletproofBabysitterIPCClient
        config = container.resolve(IConfigService)
        
        # Check if we can create a ZMQ context
        try:
            import zmq
            zmq_context = zmq.Context.instance()
        except ImportError:
            zmq_context = None
            logger.warning("DI Registration: ZMQ not available for IPC client")
            
        # For now, create with minimal config that matches constructor
        return BulletproofBabysitterIPCClient(
            zmq_context=zmq_context,
            ipc_config=config
        )
    
    # Only register if we have ZMQ available and IPC services are required
    if requires_ipc_services():
        try:
            import zmq
            container.register_factory(IBulletproofBabysitterIPCClient, babysitter_ipc_factory)
            logger.info("DI Registration: Registered IBulletproofBabysitterIPCClient factory")
        except ImportError:
            logger.warning("DI Registration: ZMQ not available, skipping IPC client registration")
    else:
        logger.info("DI Registration: IPC services not required in current mode, skipping IPC client registration")
    
    # Telemetry Service factory - MUST be in infrastructure as other services depend on it
    def telemetry_service_factory(container: DIContainer):
        # Only create telemetry service if required by mode
        if not requires_telemetry_service():
            logger.info("DI Registration: Telemetry service not required in current mode")
            return None
            
        from core.services.telemetry_streaming_service import TelemetryStreamingService
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
                logger.debug("TelemetryStreamingService: IPC client resolved successfully")
            except Exception as e:
                logger.warning(f"TelemetryStreamingService: Could not resolve IPC client: {e}")
                ipc_client = None
        else:
            logger.debug("TelemetryStreamingService: IPC client not required in current mode")
            
        config = container.resolve(IConfigService)
        
        return TelemetryStreamingService(
            ipc_client=ipc_client,
            config=config
        )
    
    # Only register telemetry service if required by mode
    if requires_telemetry_service():
        container.register_factory(ITelemetryService, telemetry_service_factory)
        logger.info("DI Registration: Registered ITelemetryService factory")
    else:
        logger.info("DI Registration: Telemetry service not required in current mode, skipping registration")
    
    # Bulk Data Service factory - for handling large binary data
    def bulk_data_service_factory(container: DIContainer):
        from core.services.bulk_data_service import BulkDataService
        
        config = container.resolve(IConfigService)
        
        # Telemetry service is conditional based on mode
        telemetry_service = None
        if requires_telemetry_service():
            try:
                telemetry_service = container.resolve(ITelemetryService)
            except Exception as e:
                logger.warning(f"BulkDataService: Telemetry service required but not available: {e}")
        
        return BulkDataService(
            config_service=config,
            telemetry_service=telemetry_service
        )
    
    container.register_factory(IBulkDataService, bulk_data_service_factory)
    logger.info("DI_REGISTRATION: IBulkDataService has been registered.")
    
    # ApplicationCore factory - the main application coordinator
    def application_core_factory(container: DIContainer):
        from core.application_core import ApplicationCore
        # Pass the external DI container to ApplicationCore for the ideal pattern
        return ApplicationCore(di_container=container)
    
    container.register_factory(DI_ApplicationCore, application_core_factory)
    
    # Service Lifecycle Manager factory - CRITICAL infrastructure service
    def service_lifecycle_manager_factory(container: DIContainer):
        from core.services.service_lifecycle_manager import ServiceLifecycleManager
        config_service = container.resolve(IConfigService)
        event_bus = container.resolve(DI_IEventBus)
        
        return ServiceLifecycleManager(
            di_container=container,
            config_service=config_service,
            event_bus=event_bus
        )
    
    container.register_factory(IServiceLifecycleManager, service_lifecycle_manager_factory)


def _register_market_data_services(container: DIContainer) -> None:
    """Register market data and price services."""
    logger.debug("Registering market data services...")
    
    # Symbol Loading Service factory - Must be first since ActiveSymbolsService depends on it
    def symbol_loading_service_factory(container: DIContainer):
        from modules.symbol_management.symbol_loading_service import SymbolLoadingService
        config_service = container.resolve(IConfigService)
        return SymbolLoadingService(config_service=config_service)
    
    container.register_factory(ISymbolLoadingService, symbol_loading_service_factory)
    
    # Filtered Market Data Publisher factory
    def filtered_market_data_publisher_factory(container: DIContainer):
        from modules.market_data_publishing.filtered_market_data_publisher import FilteredMarketDataPublisher
        from utils.global_config import GlobalConfig
        config = container.resolve(GlobalConfig)
        # CRITICAL FIX: The publisher should depend on the TelemetryService, not AppCore.
        correlation_logger = container.resolve(ICorrelationLogger)
        return FilteredMarketDataPublisher(
            config_service=config,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(IMarketDataPublisher, filtered_market_data_publisher_factory)
    
    # ActiveSymbolsService factory - This is a central component for filtering
    def active_symbols_service_factory(container: DIContainer):
        from modules.active_symbols.active_symbols_service import ActiveSymbolsService
        from utils.global_config import GlobalConfig
        from interfaces.logging.services import ICorrelationLogger
        event_bus = container.resolve(DI_IEventBus)
        config = container.resolve(GlobalConfig)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # Create with None initially - these will be wired up by the LifecycleManager (or AppCore.start)
        # This is a valid pattern to break circular dependencies when setters are used.
        service = ActiveSymbolsService(
            event_bus=event_bus,
            correlation_logger=correlation_logger,
            position_manager_ref=None,
            price_fetching_service_ref=None
        )
        
        # CRITICAL FIX: These lines incorrectly pollute the ActiveSymbolsService with all symbols.
        # They are being removed to ensure the service only tracks true active/pending positions.
        # The ActiveSymbolsService will now rely exclusively on:
        # 1. bootstrap_from_position_manager() method 
        # 2. Live trading events (PositionUpdateEvent, OpenOrderSubmittedEvent)
        # This ensures the MarketDataFilterService receives only truly active symbols.
        
        # Bootstrap with all tradeable symbols from CSV for fast trading response
        # try:
        #     symbol_loading_service = container.resolve(ISymbolLoadingService)
        #     csv_symbols = symbol_loading_service.tradeable_symbols
        #     if csv_symbols:
        #         service.bootstrap_with_initial_symbols(csv_symbols)
        #         logger.info(f"ActiveSymbolsService: Bootstrapped with {len(csv_symbols)} tradeable symbols from CSV")
        #     else:
        #         logger.warning("ActiveSymbolsService: No tradeable symbols found in CSV, starting empty")
        # except Exception as e:
        #     logger.error(f"ActiveSymbolsService: Failed to load CSV symbols, starting empty: {e}")
        #     # Fallback to config symbols if CSV fails
        #     initial_symbols = set(getattr(config, 'INITIAL_SYMBOLS_ALPACA', []))
        #     if initial_symbols:
        #         service.bootstrap_with_initial_symbols(initial_symbols)
        #         logger.info(f"ActiveSymbolsService: Fallback bootstrapped with {len(initial_symbols)} symbols from config")
        
        logger.info("ActiveSymbolsService: Starting in CLEAN state - will populate only with active positions and live trading events")
        
        return service
    
    container.register_factory('ActiveSymbolsService', active_symbols_service_factory)
    
    # Price Repository factory - JIT Price Oracle
    def price_repository_factory(container: DIContainer):
        from modules.price_fetching.price_repository import PriceRepository
        from utils.global_config import GlobalConfig
        
        # JIT Price Oracle only needs config and REST client
        config = container.resolve(GlobalConfig)
        
        # Create Alpaca REST client cleanly within the factory
        alpaca_rest_client = None
        try:
            import alpaca_trade_api as tradeapi
            
            # Access config through the IConfigService interface
            if hasattr(config, 'get_config'): # If it's our wrapper
                raw_config = config.get_config()
            else: # If it's the raw GlobalConfig object
                raw_config = config
                
            if hasattr(raw_config, 'ALPACA_API_KEY') and raw_config.ALPACA_API_KEY:
                alpaca_rest_client = tradeapi.REST(
                    key_id=raw_config.ALPACA_API_KEY,
                    secret_key=raw_config.ALPACA_API_SECRET,
                    base_url=raw_config.ALPACA_BASE_URL,
                    api_version='v2'
                )
                logger.info("DI Registration: Alpaca REST client created for JIT Price Oracle.")
        except ImportError:
            logger.warning("DI Registration: alpaca_trade_api not available, REST client disabled.")
        except Exception as e:
            logger.error(f"DI Registration: Failed to create Alpaca REST client: {e}", exc_info=True)
        
        return PriceRepository(
            config_service=config,
            rest_client=alpaca_rest_client
        )
    
    container.register_factory(DI_IPriceProvider, price_repository_factory)

    # MarketDataFilterService factory (Hybrid Bloom Filter Architecture)
    def market_data_filter_service_factory(container: DIContainer):
        from modules.market_data_filtering.market_data_filter_service import MarketDataFilterService
        
        # Resolve required dependencies for Hybrid Bloom Filter
        event_bus = container.resolve(DI_IEventBus)
        active_symbols_service = container.resolve('ActiveSymbolsService')
        
        # Get the market data publisher for filtered data
        downstream_consumers = []
        
        try:
            market_data_publisher = container.resolve(IMarketDataPublisher)
            downstream_consumers.append(market_data_publisher)
            logger.info("Added FilteredMarketDataPublisher to MarketDataFilterService downstream consumers")
        except Exception:
            logger.debug("FilteredMarketDataPublisher not available - filtered data won't be published to Redis")
            
        # Add RiskManagementService for live feed to Meltdown Detector
        try:
            risk_service = container.resolve(IRiskManagementService)
            downstream_consumers.append(risk_service)
            logger.info("Added RiskManagementService to MarketDataFilterService downstream consumers")
        except Exception:
            logger.debug("RiskManagementService not available for streaming market data")
        
        if not downstream_consumers:
            logger.warning("MarketDataFilterService has no downstream consumers - filtered data will be dropped")
        
        return MarketDataFilterService(
            event_bus=event_bus,
            active_symbols_service=active_symbols_service,
            downstream_consumers=downstream_consumers,
            bloom_capacity=1000,      # Elite performance: 1000 symbol capacity
            bloom_error_rate=0.001    # 0.1% false positive rate
        )
    
    container.register_factory('MarketDataFilterService', market_data_filter_service_factory)

    # MarketDataDisruptor factory - High-performance market data distribution
    def market_data_disruptor_factory(container: DIContainer):
        from core.services.market_data_disruptor import MarketDataDisruptor
        
        # Create list of ALL consumers that need raw market data firehose
        receivers = [
            container.resolve(DI_IPriceProvider),          # Path 1: PriceRepository caches all symbols
            container.resolve('MarketDataFilterService')   # Path 2: Filter service processes all symbols
            # Add any other services here that need the raw data stream
        ]
        
        logger.info(f"MarketDataDisruptor: Configured with {len(receivers)} consumers:")
        for receiver in receivers:
            logger.info(f"  - {receiver.__class__.__name__}")
        
        return MarketDataDisruptor(
            receivers=receivers,
            buffer_size=8192  # High-performance ring buffer (power of 2)
        )
    
    container.register_factory('MarketDataDisruptor', market_data_disruptor_factory)

    # Price Fetching Service factory (The "Firehose" from Alpaca)
    def price_fetching_service_factory(container: DIContainer):
        from modules.price_fetching.price_fetching_service import PriceFetchingService
        from utils.global_config import GlobalConfig
        global_config = container.resolve(GlobalConfig) # Get the raw config object
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # This service can be disabled
        if getattr(global_config, 'disable_price_fetching_service', False):
            logger.info("DI Registration: PriceFetchingService is disabled by config.")
            return None

        # Transform GlobalConfig to the dict format PriceFetchingService expects
        price_fetching_config = {
            'key': getattr(global_config, 'ALPACA_API_KEY', ''),
            'secret': getattr(global_config, 'ALPACA_API_SECRET', ''),
            'paper': getattr(global_config, 'ALPACA_USE_PAPER_TRADING', True),
            'sip_url': getattr(global_config, 'ALPACA_SIP_URL', None)
        }
        
        # Get initial symbols from ActiveSymbolsService AFTER it has been bootstrapped
        active_symbols_service = container.resolve('ActiveSymbolsService')
        initial_symbols = active_symbols_service.get_all_symbols() if hasattr(active_symbols_service, 'get_all_symbols') else set()
        if not initial_symbols:
             logger.warning("PriceFetchingService factory: ActiveSymbolsService returned no initial symbols. Check bootstrap logic.")
             # Fallback to config if needed, but this indicates a bootstrap issue
             initial_symbols = set(getattr(global_config, 'INITIAL_SYMBOLS_ALPACA', []))

        # Check if we have credentials to start
        if not price_fetching_config['key'] or not price_fetching_config['secret']:
             logger.warning("DI Registration: PriceFetchingService not created due to missing Alpaca credentials.")
             return None

        # --- HIGH-PERFORMANCE DISRUPTOR ARCHITECTURE ---
        # Single receiver: MarketDataDisruptor handles all distribution with lock-free ring buffer
        disruptor_receiver = container.resolve('MarketDataDisruptor')
        
        logger.info("DI Registration: PriceFetchingService configured with Disruptor pattern:")
        logger.info("  - Producer: PriceFetchingService → MarketDataDisruptor (lock-free)")
        logger.info("  - Disruptor fans out to all consumers with independent read cursors")

        return PriceFetchingService(
            config=price_fetching_config,
            receiver=disruptor_receiver,  # Single high-performance receiver
            initial_symbols=initial_symbols,
            correlation_logger=correlation_logger
            # No rest_client needed here, PriceRepository handles fallbacks
        )
    
    container.register_factory('PriceFetchingService', price_fetching_service_factory)


def _register_broker_services(container: DIContainer) -> None:
    """Register broker and trading services."""
    logger.debug("Registering broker services...")
    
    # Lightspeed Broker factory
    def lightspeed_broker_factory(container: DIContainer):
        from modules.broker_bridge.lightspeed_broker import LightspeedBroker
        event_bus = container.resolve(DI_IEventBus)
        price_provider = container.resolve(DI_IPriceProvider)
        correlation_logger = container.resolve(ICorrelationLogger)
        config_service = container.resolve(IConfigService)
        # ApplicationCore dependency removed - LightspeedBroker now uses CorrelationLogger directly
        order_repository = container.resolve(IOrderRepository)  # DEPENDENCY RESTORED - Order repo starts before broker
        
        return LightspeedBroker(
            event_bus=event_bus,
            price_provider=price_provider,
            correlation_logger=correlation_logger,
            config_service=config_service,
            order_repository=order_repository  # DEPENDENCY RESTORED - Order repo starts before broker
        )
    
    container.register_factory(IBrokerService, lightspeed_broker_factory)
    
    # Order State Service factory - the new core state management service
    def order_state_service_factory(container: DIContainer):
        from modules.order_management.order_state_service import OrderStateService
        return OrderStateService()
    
    from interfaces.order_management.state_service import IOrderStateService
    container.register_factory(IOrderStateService, order_state_service_factory)
    logger.info("Registered IOrderStateService - core order state management")
    
    # Order Linking Service factory - manages ID mappings
    def order_linking_service_factory(container: DIContainer):
        from modules.order_management.order_linking_service import OrderLinkingService
        from interfaces.order_management.services import IOrderLinkingService
        
        # Inject state service for backward compatibility methods
        state_service = container.resolve(IOrderStateService)
        
        return OrderLinkingService(state_service=state_service)
    
    from interfaces.order_management.services import IOrderLinkingService
    container.register_factory(IOrderLinkingService, order_linking_service_factory)
    logger.info("Registered IOrderLinkingService - ID mapping management")
    
    # Order Event Processor factory - handles broker events
    def order_event_processor_factory(container: DIContainer):
        from modules.order_management.order_event_processor import OrderEventProcessor
        from interfaces.order_management.services import IOrderLinkingService, IOrderQueryService
        from interfaces.trading.services import IPositionManager
        from interfaces.logging.services import ICorrelationLogger
        
        event_bus = container.resolve(DI_IEventBus)
        state_service = container.resolve(IOrderStateService)
        linking_service = container.resolve(IOrderLinkingService)
        query_service = container.resolve(IOrderQueryService)
        
        # Optional dependencies
        position_manager = None
        correlation_logger = None
        try:
            position_manager = container.resolve(IPositionManager)
        except:
            pass  # Optional dependency
        try:
            correlation_logger = container.resolve(ICorrelationLogger)
        except:
            pass  # Optional dependency
        
        return OrderEventProcessor(
            event_bus=event_bus,
            state_service=state_service,
            linking_service=linking_service,
            query_service=query_service,
            position_manager=position_manager,
            correlation_logger=correlation_logger
        )
    
    from interfaces.order_management.services import IOrderEventProcessor
    container.register_factory(IOrderEventProcessor, order_event_processor_factory)
    logger.info("Registered IOrderEventProcessor - broker event handling")
    
    # Order Query Service factory - read-only order data access
    def order_query_service_factory(container: DIContainer):
        from modules.order_management.order_query_service import OrderQueryService
        
        state_service = container.resolve(IOrderStateService)
        
        return OrderQueryService(state_service=state_service)
    
    from interfaces.order_management.services import IOrderQueryService
    container.register_factory(IOrderQueryService, order_query_service_factory)
    logger.info("Registered IOrderQueryService - read-only order queries")
    
    # Order Repository factory - uses LazyProxy to break circular dependency
    def order_repository_factory(container: DIContainer):
        from modules.order_management.order_repository import OrderRepository
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # ApplicationCore no longer needed - OrderRepository is fully decoupled
        
        # Try to resolve Trade Lifecycle Manager, but make it optional
        trade_lifecycle_manager = None
        try:
            trade_lifecycle_manager = container.resolve(ILifecycleManager)
        except Exception:
            logger.debug("Trade Lifecycle Manager not available for OrderRepository - running without trade lifecycle access")
        
        # Try to resolve Position Manager, but make it optional
        position_manager = None
        try:
            position_manager = container.resolve(IPositionManager)
        except Exception:
            logger.debug("Position Manager not available for OrderRepository - running without position management access")
        
        # Order Repository uses correlation logger for event logging
        
        # Create LazyProxy for broker service to break circular dependency
        lazy_broker = LazyProxy(container, IBrokerService)
        
        return OrderRepository(
            event_bus=event_bus,
            broker_service=lazy_broker,  # Pass LazyProxy instead of None
            config_service=config_service,
            correlation_logger=correlation_logger,
            position_manager=position_manager,  # CRITICAL: Restored missing dependency
            trade_lifecycle_manager=trade_lifecycle_manager  # Clean dependency injection
        )
    
    container.register_factory(IOrderRepository, order_repository_factory)

    # Order Event Processor factory
    def order_event_processor_factory(container: DIContainer):
        from modules.order_management.order_event_processor import OrderEventProcessor
        from interfaces.order_management.state_service import IOrderStateService
        from interfaces.order_management.services import IOrderLinkingService, IOrderQueryService
        
        # Resolve dependencies
        event_bus = container.resolve(DI_IEventBus)
        state_service = container.resolve(IOrderStateService)
        linking_service = container.resolve(IOrderLinkingService)
        query_service = container.resolve(IOrderQueryService)
        position_manager = container.resolve(IPositionManager)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        return OrderEventProcessor(
            event_bus=event_bus,
            state_service=state_service,
            linking_service=linking_service,
            query_service=query_service,
            position_manager=position_manager,
            correlation_logger=correlation_logger
        )
    
    # Import the interface
    from interfaces.order_management.services import IOrderEventProcessor
    container.register_factory(IOrderEventProcessor, order_event_processor_factory)

    # Stale Order Service factory
    def stale_order_service_factory(container: DIContainer):
        from modules.order_management.stale_order_service import StaleOrderService
        from interfaces.order_management.state_service import IOrderStateService
        from interfaces.order_management.services import IOrderQueryService
        
        # Resolve dependencies
        state_service = container.resolve(IOrderStateService)
        query_service = container.resolve(IOrderQueryService)
        broker_service = container.resolve(IBrokerService)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        return StaleOrderService(
            state_service=state_service,
            query_service=query_service,
            broker_service=broker_service,
            correlation_logger=correlation_logger
        )
    
    # Import the interface
    from interfaces.order_management.services import IStaleOrderService
    container.register_factory(IStaleOrderService, stale_order_service_factory)


def _register_trade_management_services(container: DIContainer) -> None:
    """Register trade management and execution services."""
    logger.debug("Registering trade management services...")
    
    # Trade Lifecycle Manager factory
    def trade_lifecycle_manager_factory(container: DIContainer):
        from modules.trade_management.trade_lifecycle_manager_service import TradeLifecycleManagerService
        order_repository = container.resolve(IOrderRepository)
        position_manager = container.resolve(IPositionManager)
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)
        # ApplicationCore dependency removed - TradeLifecycleManager now operates independently
        return TradeLifecycleManagerService(
            order_repository=order_repository,
            position_manager=position_manager,
            event_bus=event_bus,
            config_service=config_service,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(ITradeLifecycleManager, trade_lifecycle_manager_factory)
    
    # Position Manager factory - using PositionManagerFactory from service_factories.py
    def position_manager_factory(container: DIContainer):
        from core.service_factories import PositionManagerFactory
        factory = PositionManagerFactory()
        return factory.create_position_manager(container)
    
    container.register_factory(IPositionManager, position_manager_factory)
    
    # Trade Executor factory
    def trade_executor_factory(container: DIContainer):
        from modules.trade_management.trade_executor import TradeExecutor
        # IPositionManager already imported at top
        
        broker = container.resolve(IBrokerService)
        order_repository = container.resolve(IOrderRepository)
        config_service = container.resolve(IConfigService)
        event_bus = container.resolve(DI_IEventBus)
        position_manager = container.resolve(IPositionManager)
        correlation_logger = container.resolve(ICorrelationLogger)
        # ApplicationCore dependency removed - TradeExecutor now operates independently
        
        return TradeExecutor(
            broker=broker,
            order_repository=order_repository,
            config_service=config_service,
            event_bus=event_bus,
            position_manager=position_manager,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(ITradeExecutor, trade_executor_factory)
    
    # Order Strategy factory
    def order_strategy_factory(container: DIContainer):
        from modules.trade_management.order_strategy import OrderStrategy
        config_service = container.resolve(IConfigService)
        price_provider = container.resolve(DI_IPriceProvider)
        position_manager = container.resolve(IPositionManager)
        correlation_logger = container.resolve(ICorrelationLogger)
        return OrderStrategy(
            config_service=config_service,
            price_provider=price_provider,
            position_manager=position_manager,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(IOrderStrategy, order_strategy_factory)
    
    # Trade Manager Service factory
    def trade_manager_service_factory(container: DIContainer):
        from modules.trade_management.trade_manager_service import TradeManagerService
        event_bus = container.resolve(DI_IEventBus)
        broker_service = container.resolve(IBrokerService)
        price_provider = container.resolve(DI_IPriceProvider)
        config_service = container.resolve(IConfigService)
        order_repository = container.resolve(IOrderRepository)
        position_manager = container.resolve(IPositionManager)
        trade_executor = container.resolve(ITradeExecutor)
        lifecycle_manager = container.resolve(ITradeLifecycleManager)
        order_strategy = container.resolve(IOrderStrategy)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        return TradeManagerService(
            event_bus=event_bus,
            broker_service=broker_service,
            price_provider=price_provider,
            config_service=config_service,
            order_repository=order_repository,
            position_manager=position_manager,
            order_strategy=order_strategy,
            trade_executor=trade_executor,
            lifecycle_manager=lifecycle_manager,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(ITradeManagerService, trade_manager_service_factory)
    
    # Position Enrichment Service factory
    def position_enrichment_factory(container: DIContainer):
        from modules.trade_management.position_enrichment_service import PositionEnrichmentService
        # ApplicationCore dependency removed - PositionEnrichmentService now operates independently
        config_service = container.resolve(IConfigService)
        price_provider = container.resolve(DI_IPriceProvider)
        event_bus = container.resolve(DI_IEventBus)
        correlation_logger = container.resolve(ICorrelationLogger)
        return PositionEnrichmentService(
            config_service=config_service,
            price_provider=price_provider,
            event_bus=event_bus,
            correlation_logger=correlation_logger
        )
    
    container.register_factory('PositionEnrichmentService', position_enrichment_factory)


def _register_ocr_services(container: DIContainer) -> None:
    """Register OCR data processing services."""
    logger.debug("Registering OCR services...")
    
    # OCR Data Conditioning Service factory
    def ocr_conditioning_factory(container: DIContainer):
        from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService
        event_bus = container.resolve(DI_IEventBus)
        
        # Use LazyProxy to break circular dependency
        orchestrator = LazyProxy(container, IOCRScalpingSignalOrchestratorService)
        
        from modules.ocr.python_ocr_data_conditioner import PythonOCRDataConditioner
        price_provider = container.resolve(DI_IPriceProvider)
        active_symbols_service = container.resolve('ActiveSymbolsService')
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)
        conditioner = PythonOCRDataConditioner(
            price_provider=price_provider,
            active_symbols_service=active_symbols_service,
            config_service=config_service,
            correlation_logger=correlation_logger
        )
        correlation_logger = container.resolve(ICorrelationLogger)
        return OCRDataConditioningService(
            event_bus=event_bus,
            conditioner=conditioner,
            orchestrator_service=orchestrator,  # FIX: Properly inject the orchestrator
            observability_publisher=None,  # Will be injected later
            correlation_logger=correlation_logger
        )
    
    container.register_factory(IOCRDataConditioningService, ocr_conditioning_factory)
    
    # OCR Scalping Signal Orchestrator Service factory
    def ocr_orchestrator_factory(container: DIContainer):
        from modules.trade_management.ocr_scalping_signal_orchestrator_service import OCRScalpingSignalOrchestratorService
        
        # Resolve required dependencies
        event_bus = container.resolve(DI_IEventBus)
        price_provider = container.resolve(DI_IPriceProvider)
        position_manager = container.resolve(IPositionManager)
        config_service = container.resolve(IConfigService)
        
        # Resolve optional dependencies
        interpreter_service = None
        action_filter_service = None
        benchmarker = None
        pipeline_validator = None
        
        try:
            interpreter_service = container.resolve('ISnapshotInterpreter')
            logger.info("DI Registration: SnapshotInterpreter resolved successfully for OCRScalpingSignalOrchestratorService")
        except Exception as e:
            logger.critical(f"DI Registration: SnapshotInterpreter resolution FAILED for OCRScalpingSignalOrchestratorService: {e}")
            logger.critical("DI Registration: Trading will NOT work without SnapshotInterpreter - attempting to resolve dependencies...")
            # This is a critical failure - trading cannot work without SnapshotInterpreter
            # Let's try to resolve the dependencies to see what's missing
            try:
                logger.info("DI Registration: Checking SnapshotInterpreter dependencies...")
                lifecycle_manager = container.resolve(ITradeLifecycleManager)
                config_service = container.resolve(IConfigService)
                position_manager = container.resolve(IPositionManager) 
                order_repository = container.resolve(IOrderRepository)
                telemetry_service = container.resolve(ITelemetryService)
                logger.info("DI Registration: All SnapshotInterpreter dependencies resolved successfully")
                interpreter_service = None  # Still set to None but we've identified the issue
            except Exception as dep_e:
                logger.critical(f"DI Registration: SnapshotInterpreter dependency resolution failed: {dep_e}")
                interpreter_service = None
                
        try:
            action_filter_service = container.resolve('IMasterActionFilter')
            logger.info("DI Registration: MasterActionFilter resolved successfully for OCRScalpingSignalOrchestratorService")
        except Exception as e:
            logger.warning(f"DI Registration: MasterActionFilter resolution failed for OCRScalpingSignalOrchestratorService: {e}")
            action_filter_service = None
            
        try:
            benchmarker = container.resolve(IPerformanceBenchmarker)
        except Exception:
            logger.debug("PerformanceBenchmarker not available for OCRScalpingSignalOrchestratorService")
            
        try:
            pipeline_validator = container.resolve(IPipelineValidator)
        except Exception:
            logger.debug("PipelineValidator not available for OCRScalpingSignalOrchestratorService")
        
        correlation_logger = container.resolve(ICorrelationLogger)
        fingerprint_service = container.resolve(IFingerprintService)

        return OCRScalpingSignalOrchestratorService(
            event_bus=event_bus,
            price_provider=price_provider,
            position_manager=position_manager,
            config_service=config_service,
            fingerprint_service=fingerprint_service,
            interpreter_service=interpreter_service,
            action_filter_service=action_filter_service,
            benchmarker=benchmarker,
            pipeline_validator=pipeline_validator,
            correlation_logger=correlation_logger
        )
    
    container.register_factory(IOCRScalpingSignalOrchestratorService, ocr_orchestrator_factory)
    
    # Note: OCRService is no longer registered in DI container
    # It runs in a separate subprocess managed by OCRProcessManager
    
    # OCR Parameters Service factory
    def ocr_parameters_factory(container: DIContainer):
        from modules.ocr.subcomponents.parameters_service import OCRParametersService
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("OCRParametersService: IPC client not available")
        
        return OCRParametersService(
            config_service=config_service, 
            correlation_logger=correlation_logger,
            ipc_client=ipc_client
        )
    
    container.register_factory(IOCRParametersService, ocr_parameters_factory)

    # OCR Service Factory factory
    def ocr_service_factory_factory(container: DIContainer):
        """Creates the factory that creates the OCR service."""
        from core.service_factories import OCRServiceFactory

        # The factory itself has dependencies, which the container will provide.
        config_service = container.resolve(IConfigService)
        event_bus = container.resolve(DI_IEventBus)
        ocr_params_service = container.resolve(IOCRParametersService)

        return OCRServiceFactory(
            config_service=config_service,
            event_bus=event_bus,
            ocr_params_service=ocr_params_service
        )

    # NOTE: IOCRServiceFactory interface was removed during interface consolidation
    # The factory can still be registered directly if needed
    # container.register_factory(IOCRServiceFactory, ocr_service_factory_factory)
    container.register_factory('OCRServiceFactory', ocr_service_factory_factory)
    logger.info("DI_REGISTRATION: OCRServiceFactory has been registered.")


def _register_risk_services(container: DIContainer) -> None:
    """Register risk management services."""
    logger.debug("Registering risk management services...")
    
    # Risk Management Service factory
    def risk_service_factory(container: DIContainer):
        from modules.risk_management.risk_service import RiskManagementService
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)  # <<< NEW
        price_provider = container.resolve(DI_IPriceProvider)
        order_repository = container.resolve(IOrderRepository)
        position_manager = container.resolve(IPositionManager)
        pipeline_validator = container.resolve(IPipelineValidator)
        performance_benchmarker = container.resolve(IPerformanceBenchmarker)
        return RiskManagementService(
            event_bus=event_bus,
            config_service=config_service,
            correlation_logger=correlation_logger,  # <<< NEW
            price_provider=price_provider,
            order_repository=order_repository,  # CRITICAL: Restored missing dependency
            position_manager=position_manager,
            benchmarker=performance_benchmarker,
            pipeline_validator=pipeline_validator
            # telemetry_service removed
        )
    
    container.register_factory(IRiskManagementService, risk_service_factory)


def _register_utility_services(container: DIContainer) -> None:
    """Register utility and support services."""
    logger.debug("Registering utility services...")
    
    # ROI Service factory
    def roi_service_factory(container: DIContainer):
        from modules.ocr.subcomponents.roi_service import ROIService
        config_service = container.resolve(IConfigService)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("ROIService: IPC client not available")
        
        return ROIService(
            config_service=config_service, 
            correlation_logger=correlation_logger, 
            ipc_client=ipc_client
        )
    
    container.register_factory(IROIService, roi_service_factory)
    
    # Snapshot Interpreter Service factory
    def snapshot_interpreter_factory(container: DIContainer):
        from modules.trade_management.snapshot_interpreter_service import SnapshotInterpreterService
        lifecycle_manager = container.resolve(ITradeLifecycleManager)
        config_service = container.resolve(IConfigService)
        position_manager = container.resolve(IPositionManager)
        order_repository = container.resolve(IOrderRepository)
        correlation_logger = container.resolve(ICorrelationLogger)
        return SnapshotInterpreterService(
            lifecycle_manager=lifecycle_manager,
            config_service=config_service,
            position_manager=position_manager,
            order_repository=order_repository,
            correlation_logger=correlation_logger
        )
    
    container.register_factory('ISnapshotInterpreter', snapshot_interpreter_factory)
    
    # Master Action Filter Service factory
    def master_action_filter_factory(container: DIContainer):
        from modules.trade_management.master_action_filter_service import MasterActionFilterService
        config_service = container.resolve(IConfigService)
        price_provider = container.resolve(DI_IPriceProvider)
        correlation_logger = container.resolve(ICorrelationLogger)
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("MasterActionFilterService: IPC client not available")
        
        return MasterActionFilterService(
            config_service=config_service,
            price_provider=price_provider,
            ipc_client=ipc_client,
            correlation_logger=correlation_logger
        )
    
    container.register_factory('IMasterActionFilter', master_action_filter_factory)
    
    # GUI Command Service factory
    def gui_command_service_factory(container: DIContainer):
        from modules.gui_commands.gui_command_service import GUICommandService
        # Resolve all required dependencies for proper DI injection
        order_repository = container.resolve(IOrderRepository)
        config_service = container.resolve(IConfigService)
        broker_service = container.resolve(IBrokerService)
        event_bus = container.resolve(DI_IEventBus)
        price_provider = container.resolve(DI_IPriceProvider)
        position_manager = container.resolve(IPositionManager)
        trade_lifecycle_manager = container.resolve(ILifecycleManager)
        
        # Telemetry service is conditional based on mode
        telemetry_service = None
        if requires_telemetry_service():
            try:
                telemetry_service = container.resolve(ITelemetryService)
            except Exception as e:
                logger.warning(f"GUICommandService: Telemetry service required but not available: {e}")
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("GUICommandService: IPC client not available")
        
        return GUICommandService(
            telemetry_service=telemetry_service,
            order_repository=order_repository,
            config_service=config_service,
            broker_service=broker_service,
            event_bus=event_bus,
            price_provider=price_provider,
            position_manager=position_manager,
            trade_lifecycle_manager=trade_lifecycle_manager,
            ipc_client=ipc_client
        )
    
    container.register_factory('IGUICommandService', gui_command_service_factory)
    
    # System Health Monitoring Service factory
    def system_health_service_factory(container: DIContainer):
        from modules.system_monitoring.system_health_service import SystemHealthMonitoringService
        config_service = container.resolve(IConfigService)
        perf_benchmarker = container.resolve(IPerformanceBenchmarker)
        pipeline_validator = container.resolve(IPipelineValidator)
        
        
        # Try to resolve OCRProcessManager (optional)
        ocr_process_manager = None
        try:
            ocr_process_manager = container.resolve(IOCRProcessManager)
        except Exception:
            logger.debug("OCRProcessManager not available for SystemHealthService")
        
        # Try to resolve EventBus (optional)
        event_bus = None
        try:
            event_bus = container.resolve(DI_IEventBus)
        except Exception:
            logger.debug("EventBus not available for SystemHealthService")
        
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("SystemHealthService: IPC client not available")
        
        # Define performance metrics list
        performance_metrics = [
            "ocr_process.tesseract_time_ms",
            "event_bus.publish_latency_ms", 
            "risk.order_request_handling_time_ms",
            "market_data_processing_time",
            "broker_response_time"
        ]
        
        # Resolve correlation logger
        correlation_logger = None
        try:
            correlation_logger = container.resolve(ICorrelationLogger)
        except Exception:
            logger.debug("Correlation logger not available for SystemHealthService")
        
        return SystemHealthMonitoringService(
            config_service=config_service,
            ocr_process_manager=ocr_process_manager,
            event_bus=event_bus,
            performance_metrics=performance_metrics,
            correlation_logger=correlation_logger,
            perf_benchmarker=perf_benchmarker,
            pipeline_validator_ref=pipeline_validator,
            ipc_client=ipc_client
        )
    
    container.register_factory('ISystemHealthMonitoringService', system_health_service_factory)
    
    # Also register with string key for ApplicationCore compatibility
    container.register_factory('SystemHealthMonitoringService', system_health_service_factory)
    
    
    # Performance Metrics Service factory
    def performance_metrics_service_factory(container: DIContainer):
        from core.services.performance_metrics_service import PerformanceMetricsService
        config_service = container.resolve(IConfigService)
        
        # Try to resolve IPC manager (may not be available yet)
        ipc_manager = None
        try:
            ipc_manager = container.resolve(IIPCManager)
        except Exception:
            logger.debug("IPC manager not available for PerformanceMetricsService")
            
        # Try to resolve performance benchmarker
        performance_benchmarker = None
        try:
            performance_benchmarker = container.resolve(IPerformanceBenchmarker)
        except Exception:
            logger.debug("Performance benchmarker not available for PerformanceMetricsService")
            
        return PerformanceMetricsService(
            config_service=config_service,
            ipc_manager=ipc_manager,
            performance_benchmarker=performance_benchmarker
        )
    
    container.register_factory(IPerformanceMetricsService, performance_metrics_service_factory)
    
    # OCR Process Manager Service factory
    def ocr_process_manager_factory(container: DIContainer):
        from core.services.ocr_process_manager import OCRProcessManager
        config_service = container.resolve(IConfigService)
        ocr_data_conditioning_service = LazyProxy(container, IOCRDataConditioningService)
        
        # Telemetry service is conditional based on mode
        telemetry_service = None
        if requires_telemetry_service():
            try:
                telemetry_service = container.resolve(ITelemetryService)
            except Exception as e:
                logger.warning(f"OCRProcessManager: Telemetry service required but not available: {e}")
        
        # Try to resolve optional dependencies
        event_bus = None
        try:
            event_bus = container.resolve(DI_IEventBus)
        except Exception:
            logger.debug("Event bus not available for OCRProcessManager")
            
        # IPC client is conditional based on mode
        ipc_client = None
        if requires_ipc_services():
            try:
                ipc_client = container.resolve(IBulletproofBabysitterIPCClient)
            except Exception:
                logger.debug("OCRProcessManager: IPC client not available")
            
        return OCRProcessManager(
            config_service=config_service,
            telemetry_service=telemetry_service,
            ocr_data_conditioning_service=ocr_data_conditioning_service,
            event_bus=event_bus,
            ipc_client=ipc_client
        )
    
    container.register_factory(IOCRProcessManager, ocr_process_manager_factory)
    
    # IPC Manager Service factory
    def ipc_manager_factory(container: DIContainer):
        from core.services.ipc_manager import IPCManager
        config_service = container.resolve(IConfigService)
        
        # Try to resolve ZMQ context if available
        zmq_context = None
        try:
            import zmq
            zmq_context = zmq.Context.instance()
        except ImportError:
            logger.debug("ZMQ not available for IPCManager")
            
        # Try to resolve mission control notifier (optional)
        mission_control_notifier = None
        if requires_telemetry_service():
            try:
                from utils.core_mission_control_notifier import CoreMissionControlNotifier
                telemetry_service = container.resolve(ITelemetryService)
                mission_control_notifier = CoreMissionControlNotifier(
                    logger_instance=logging.getLogger(__name__),
                    telemetry_service=telemetry_service,
                    config_service=config_service
                )
            except Exception:
                logger.debug("Mission control notifier not available for IPCManager")
            
        return IPCManager(
            config_service=config_service,
            zmq_context=zmq_context,
            mission_control_notifier=mission_control_notifier
        )
    
    container.register_factory(IIPCManager, ipc_manager_factory)
    
    # Emergency GUI Service factory
    # IEmergencyGUIService already imported at top
    def emergency_gui_service_factory(container: DIContainer):
        from core.services.emergency_gui_service import EmergencyGUIService
        config_service = container.resolve(IConfigService)
        
        # Try to resolve ZMQ context if available
        zmq_context = None
        try:
            import zmq
            zmq_context = zmq.Context.instance()
        except ImportError:
            logger.debug("ZMQ not available for EmergencyGUIService")
        
        return EmergencyGUIService(
            config_service=config_service,
            zmq_context=zmq_context,
            application_core_ref=None  # Clean architecture - no app core reference
        )
    
    container.register_factory(IEmergencyGUIService, emergency_gui_service_factory)
    
    # Health Monitor Service factory
    # IHealthMonitor already imported at top
    def health_monitor_factory(container: DIContainer):
        from core.services.health_monitor import HealthMonitor
        config_service = container.resolve(IConfigService)
        ipc_manager = container.resolve(IIPCManager)
        
        # Try to resolve performance benchmarker (optional)
        performance_benchmarker = None
        try:
            performance_benchmarker = container.resolve(IPerformanceBenchmarker)
        except Exception:
            logger.debug("Performance benchmarker not available for HealthMonitor")
        
        return HealthMonitor(
            config_service=config_service,
            ipc_manager=ipc_manager,
            performance_benchmarker=performance_benchmarker,
            application_core_ref=None  # Clean architecture - no app core reference
        )
    
    container.register_factory(IHealthMonitor, health_monitor_factory)
    
    # Command Listener Service factory
    # ICommandListener already imported at top
    def command_listener_factory(container: DIContainer):
        from core.services.command_listener import CommandListener
        config_service = container.resolve(IConfigService)
        ipc_manager = container.resolve(IIPCManager)
        
        # Try to resolve ZMQ context if available
        zmq_context = None
        try:
            import zmq
            zmq_context = zmq.Context.instance()
        except ImportError:
            logger.debug("ZMQ not available for CommandListener")
        
        # Try to resolve GUI command service (optional)
        gui_command_service = None
        try:
            from modules.gui_commands.gui_command_service import GUICommandService
            gui_command_service = container.resolve('IGUICommandService')
        except Exception:
            logger.debug("GUI command service not available for CommandListener")
        
        return CommandListener(
            config_service=config_service,
            ipc_manager=ipc_manager,
            zmq_context=zmq_context,
            gui_command_service=gui_command_service
        )
    
    container.register_factory(ICommandListener, command_listener_factory)
    
    # Fingerprint Service factory
    def fingerprint_service_factory(container: DIContainer):
        from modules.utility.fingerprint_service import FingerprintService
        event_bus = container.resolve(DI_IEventBus)
        config_service = container.resolve(IConfigService)

        return FingerprintService(
            event_bus=event_bus,
            config_service=config_service
        )

    container.register_factory(IFingerprintService, fingerprint_service_factory)

    # Service Lifecycle Manager factory
    def service_lifecycle_manager_factory(container: DIContainer):
        from core.services.service_lifecycle_manager import ServiceLifecycleManager
        config_service = container.resolve(IConfigService)
        event_bus = container.resolve(DI_IEventBus)

        return ServiceLifecycleManager(
            di_container=container,
            config_service=config_service,
            event_bus=event_bus
        )

    # IServiceLifecycleManager already imported at top
    container.register_factory(IServiceLifecycleManager, service_lifecycle_manager_factory)