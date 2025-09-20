"""
IntelliSense Component Factory

Factory for creating instances of EnhancedCapture and TestableAnalysis components
for the IntelliSense environment. Provides centralized component creation with
proper dependency injection and type safety.

This factory handles the complex initialization of enhanced and testable components
while maintaining compatibility with the original production component interfaces.
"""

import logging
from typing import Any, TYPE_CHECKING, Optional, Callable # Added Callable

if TYPE_CHECKING:
    from intellisense.core.interfaces import IIntelliSenseApplicationCore
    # from intellisense.capture.logger import CorrelationLogger # REMOVE if no longer passed by any factory method
    from intellisense.capture.interfaces import ICorrelationLogger # Keep for type hinting if any method still needed it conceptually

    # Actual (or Base Placeholder) Production Component Types
    from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService as BaseOCRService
    from modules.price_fetching.price_repository import PriceRepository as BasePriceRepository
    from modules.broker_bridge.lightspeed_broker import LightspeedBroker as BaseBrokerInterface
    from modules.order_management.order_repository import OrderRepository as BaseOrderRepository
    from modules.trade_management.position_manager import PositionManager as BasePositionManager
    # Actual Production services that will get the logger
    from modules.risk_management.risk_service import RiskManagementService as ActualRiskManagementService
    from modules.trade_management.trade_manager_service import TradeManagerService as ActualTradeManagerService
    # IntelliSense Enhanced/Testable Component Types
    from intellisense.capture.enhanced_components.enhanced_ocr_service import EnhancedOCRDataConditioningService
    from intellisense.capture.enhanced_components.enhanced_price_repository import EnhancedPriceRepository
    from intellisense.capture.enhanced_components.enhanced_broker_interface import EnhancedLightspeedBroker
    from intellisense.analysis.testable_components import (
        IntelliSenseTestableOrderRepository,
        IntelliSenseTestablePositionManager,
        IntelliSenseTestablePriceRepository
    )
    # Config and Core Dependency Types (actual paths)
    from utils.global_config import GlobalConfig # Assuming this is the type for 'config'
    from core.interfaces import IEventBus, IBroker # Base interfaces
    from interfaces.ocr.services import IOCRDataConditioner # For OCR Service
    from interfaces.data.services import IPriceProvider # For Broker Interface
    # For new factory methods, other dependencies of RMS and TMS
    from interfaces.order_management.services import IOrderRepository as ProdIOrderRepository
    from interfaces.trading.services import (
        IPositionManager as ProdIPositionManager,
        IOrderStrategy as ProdIOrderStrategy,
        ITradeExecutor as ProdITradeExecutor,
        ITradeLifecycleManager as ProdITradeLifecycleManager,
        ISnapshotInterpreter as ProdISnapshotInterpreter
    )
    # BaseBrokerService for TMS is IBrokerService
    from interfaces.broker.services import IBrokerService as ProdIBrokerService
    # For Alpaca Rest Client type hint in TMS
    import alpaca_trade_api as tradeapi

logger = logging.getLogger(__name__)

class ComponentFactory:
    """
    Factory for creating instances of EnhancedCapture and TestableAnalysis components
    for the IntelliSense environment.
    
    This factory provides centralized component creation with proper dependency injection,
    ensuring that enhanced and testable components are initialized with the correct
    references to the IntelliSense application core and original production dependencies.
    """

    @staticmethod
    def create_enhanced_ocr_service(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig', # Assuming this is the type of original_service.config
        original_event_bus: 'IEventBus',
        original_conditioner: Optional['IOCRDataConditioner'] # Conditioner instance from original service
        # Add other specific dependencies of BaseOCRDataConditioningService if needed for super().__init__
    ) -> Optional['EnhancedOCRDataConditioningService']:
        """
        Create an EnhancedOCRDataConditioningService instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original OCR service
            original_event_bus: Event bus from original service
            original_conditioner: OCR conditioner instance from original service

        Returns:
            EnhancedOCRDataConditioningService instance with IntelliSense capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="EnhancedOCRDataConditioningService",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus,
            original_conditioner=original_conditioner
        ):
            logger.error("Factory: Failed to create EnhancedOCRDataConditioningService - dependency validation failed")
            return None

        try:
            from intellisense.capture.enhanced_components.enhanced_ocr_service import EnhancedOCRDataConditioningService
            logger.debug(f"Factory: Creating EnhancedOCRDataConditioningService for AppCore {id(app_core_ref)}")
            return EnhancedOCRDataConditioningService(
                app_core_ref_for_intellisense=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus,
                original_conditioner=original_conditioner
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create EnhancedOCRDataConditioningService: {e}", exc_info=True)
            return None

    @staticmethod
    def create_enhanced_price_repository(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig',
        original_event_bus: 'IEventBus'
        # Add other specific dependencies of BasePriceRepository (e.g., rest_client, observability_publisher)
    ) -> Optional['EnhancedPriceRepository']:
        """
        Create an EnhancedPriceRepository instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original price repository
            original_event_bus: Event bus from original service

        Returns:
            EnhancedPriceRepository instance with IntelliSense capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="EnhancedPriceRepository",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus
        ):
            logger.error("Factory: Failed to create EnhancedPriceRepository - dependency validation failed")
            return None

        try:
            from intellisense.capture.enhanced_components.enhanced_price_repository import EnhancedPriceRepository
            logger.debug(f"Factory: Creating EnhancedPriceRepository for AppCore {id(app_core_ref)}")
            return EnhancedPriceRepository(
                app_core_ref_for_intellisense=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create EnhancedPriceRepository: {e}", exc_info=True)
            return None

    @staticmethod
    def create_enhanced_broker_interface(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig',
        original_event_bus: 'IEventBus',
        original_price_provider: 'IPriceProvider' # Base LightspeedBroker takes price_provider
        # Add other specific dependencies of BaseLightspeedBroker (host, port, credentials etc.)
    ) -> Optional['EnhancedLightspeedBroker']:
        """
        Create an EnhancedLightspeedBroker instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original broker
            original_event_bus: Event bus from original service
            original_price_provider: Price provider from original broker

        Returns:
            EnhancedLightspeedBroker instance with IntelliSense capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="EnhancedLightspeedBroker",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus,
            original_price_provider=original_price_provider
        ):
            logger.error("Factory: Failed to create EnhancedLightspeedBroker - dependency validation failed")
            return None

        try:
            from intellisense.capture.enhanced_components.enhanced_broker_interface import EnhancedLightspeedBroker
            logger.debug(f"Factory: Creating EnhancedLightspeedBroker for AppCore {id(app_core_ref)}")
            return EnhancedLightspeedBroker(
                app_core_ref_for_intellisense=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus,
                original_price_provider=original_price_provider
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create EnhancedLightspeedBroker: {e}", exc_info=True)
            return None

    @staticmethod
    def create_testable_order_repository(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig', # Assuming OR needs config
        original_event_bus: 'IEventBus',
        original_broker_service: 'IBroker', # Base OrderRepository takes broker_service
        original_position_manager: Optional['BasePositionManager'] # Base OR takes optional PM
    ) -> Optional['IntelliSenseTestableOrderRepository']:
        """
        Create an IntelliSenseTestableOrderRepository instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original order repository
            original_event_bus: Event bus from original service
            original_broker_service: Broker service from original repository
            original_position_manager: Position manager from original repository

        Returns:
            IntelliSenseTestableOrderRepository instance with test capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="IntelliSenseTestableOrderRepository",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus,
            original_broker_service=original_broker_service,
            original_position_manager=original_position_manager
        ):
            logger.error("Factory: Failed to create IntelliSenseTestableOrderRepository - dependency validation failed")
            return None

        try:
            from intellisense.analysis.testable_components import IntelliSenseTestableOrderRepository
            logger.debug(f"Factory: Creating IntelliSenseTestableOrderRepository for AppCore {id(app_core_ref)}")
            return IntelliSenseTestableOrderRepository(
                app_core_instance_for_test_mode_check=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus,
                original_broker_service=original_broker_service,
                original_position_manager=original_position_manager
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create IntelliSenseTestableOrderRepository: {e}", exc_info=True)
            return None

    @staticmethod
    def create_testable_position_manager(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig', # Assuming PM needs config
        original_event_bus: 'IEventBus'
    ) -> Optional['IntelliSenseTestablePositionManager']:
        """
        Create an IntelliSenseTestablePositionManager instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original position manager
            original_event_bus: Event bus from original service

        Returns:
            IntelliSenseTestablePositionManager instance with test capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="IntelliSenseTestablePositionManager",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus
        ):
            logger.error("Factory: Failed to create IntelliSenseTestablePositionManager - dependency validation failed")
            return None

        try:
            from intellisense.analysis.testable_components import IntelliSenseTestablePositionManager
            logger.debug(f"Factory: Creating IntelliSenseTestablePositionManager for AppCore {id(app_core_ref)}")
            return IntelliSenseTestablePositionManager(
                app_core_instance_for_test_mode_check=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create IntelliSenseTestablePositionManager: {e}", exc_info=True)
            return None

    @staticmethod
    def create_testable_price_repository(
        app_core_ref: 'IIntelliSenseApplicationCore',
        original_config: 'GlobalConfig',
        original_event_bus: 'IEventBus'
    ) -> Optional['IntelliSenseTestablePriceRepository']:
        """
        Create an IntelliSenseTestablePriceRepository instance.

        Args:
            app_core_ref: Reference to IntelliSense application core
            original_config: Configuration from original price repository
            original_event_bus: Event bus from original service

        Returns:
            IntelliSenseTestablePriceRepository instance with test capabilities, or None if validation fails
        """
        # Validate dependencies before creation
        if not ComponentFactory.validate_component_dependencies(
            component_name="IntelliSenseTestablePriceRepository",
            app_core_ref=app_core_ref,
            original_config=original_config,
            original_event_bus=original_event_bus
        ):
            logger.error("Factory: Failed to create IntelliSenseTestablePriceRepository - dependency validation failed")
            return None

        try:
            from intellisense.analysis.testable_components import IntelliSenseTestablePriceRepository
            logger.debug(f"Factory: Creating IntelliSenseTestablePriceRepository for AppCore {id(app_core_ref)}")
            return IntelliSenseTestablePriceRepository(
                app_core_ref_for_intellisense=app_core_ref,
                original_config=original_config,
                original_event_bus=original_event_bus
            )
        except Exception as e:
            logger.error(f"Factory: Failed to create IntelliSenseTestablePriceRepository: {e}", exc_info=True)
            return None

    # --- MODIFIED Factory Methods for Production Services ---
    # These methods will now instantiate the production services WITHOUT injecting
    # IntelliSense's CorrelationLogger. The production services themselves (after
    # modification by the TESTRADE team) will handle their own event publishing to Redis.
    @staticmethod
    def create_production_risk_management_service(
        app_core_ref: 'IIntelliSenseApplicationCore',
        event_bus: 'IEventBus', # Explicitly list required dependencies
        config_service: 'GlobalConfig',
        price_provider: Optional['IPriceProvider'],
        order_repository: Optional['ProdIOrderRepository'],
        position_manager: Optional['ProdIPositionManager'],
        gui_logger_func: Optional[Callable[[str, str], None]],
        benchmarker: Optional[Any],
        pipeline_validator: Optional[Any],
        # correlation_logger: Optional['ICorrelationLogger'] = None, # --- REMOVED PARAMETER ---
        original_component_instance: Optional['ActualRiskManagementService'] = None, # Still useful for getting other base args
        **kwargs
    ) -> Optional['ActualRiskManagementService']:
        try:
            # Runtime import of the actual production service
            from modules.risk_management.risk_service import RiskManagementService as ProductionRiskService

            # ProductionRiskService __init__ was modified to take an optional correlation_logger.
            # Here, we explicitly DO NOT pass IntelliSense's logger.
            # TESTRADE's instance will use its own mechanisms for publishing to Redis.
            logger.debug(f"Factory: Creating ProductionRiskManagementService (IntelliSense logger NOT injected).")
            return ProductionRiskService(
                event_bus=event_bus,
                config_service=config_service,
                price_provider=price_provider,
                order_repository=order_repository,
                position_manager=position_manager,
                gui_logger_func=gui_logger_func,
                benchmarker=benchmarker,
                pipeline_validator=pipeline_validator,
                correlation_logger=None # Explicitly pass None or omit if default is None
            )
        except ImportError:
            logger.error("Factory: Production 'modules.risk_management.risk_service.RiskManagementService' not found.")
            return None
        except Exception as e:
            logger.error(f"Factory: Failed to create ProductionRiskManagementService: {e}", exc_info=True)
            return None

    @staticmethod
    def create_production_trade_manager_service(
        app_core_ref: 'IIntelliSenseApplicationCore',
        event_bus: 'IEventBus',
        broker_service: 'ProdIBrokerService',
        price_provider: 'IPriceProvider',
        config_service: 'GlobalConfig',
        order_repository: 'ProdIOrderRepository',
        position_manager: 'ProdIPositionManager',
        order_strategy: 'ProdIOrderStrategy',
        trade_executor: 'ProdITradeExecutor',
        lifecycle_manager: 'ProdITradeLifecycleManager',
        snapshot_interpreter: Optional['ProdISnapshotInterpreter'],
        rest_client: Optional[Any], # Alpaca client type
        risk_service_ref: Optional['ActualRiskManagementService'],
        gui_logger_func: Optional[Callable[[str, str], None]],
        pipeline_validator: Optional[Any],
        benchmarker: Optional[Any],
        # correlation_logger: Optional['ICorrelationLogger'] = None, # --- REMOVED PARAMETER ---
        original_component_instance: Optional['ActualTradeManagerService'] = None, # Still useful
        **kwargs
    ) -> Optional['ActualTradeManagerService']:
        try:
            # Runtime import of the actual production service
            from modules.trade_management.trade_manager_service import TradeManagerService as ProductionTradeManagerService

            logger.debug(f"Factory: Creating ProductionTradeManagerService (IntelliSense logger NOT injected).")
            return ProductionTradeManagerService(
                event_bus=event_bus,
                broker_service=broker_service,
                price_provider=price_provider,
                config_service=config_service,
                order_repository=order_repository,
                position_manager=position_manager,
                order_strategy=order_strategy,
                trade_executor=trade_executor,
                lifecycle_manager=lifecycle_manager,
                snapshot_interpreter=snapshot_interpreter,
                rest_client=rest_client,
                risk_service=risk_service_ref,
                gui_logger_func=gui_logger_func,
                pipeline_validator=pipeline_validator,
                benchmarker=benchmarker,
                correlation_logger=None # Explicitly pass None or omit if default is None
            )
        except ImportError:
            logger.error("Factory: Production 'modules.trade_management.trade_manager_service.TradeManagerService' not found.")
            return None
        except Exception as e:
            logger.error(f"Factory: Failed to create ProductionTradeManagerService: {e}", exc_info=True)
            return None

    @staticmethod
    def validate_component_dependencies(component_name: str, app_core_ref: 'IIntelliSenseApplicationCore', **dependencies) -> bool:
        if not app_core_ref:
            logger.error(f"Factory: Cannot create {component_name} - app_core_ref is None")
            return False

        # Define genuinely optional dependencies for each component type or pass them in a list
        # For simplicity, making the check more lenient for now, relying on constructor errors
        # specific_optional_deps = {
        #     "EnhancedOCRDataConditioningService": ["original_conditioner"],
        #     "IntelliSenseTestableOrderRepository": ["original_position_manager"],
        #     "ProductionRiskManagementService": ["price_provider", "order_repository", "position_manager", "gui_logger_func", "benchmarker", "pipeline_validator", "correlation_logger"],
        #     # ... and so on for other components
        # }
        # current_optional_deps = specific_optional_deps.get(component_name, [])

        # A simpler general check, assumes most core dependencies should be present
        # Keys that are okay to be None without warning (as they are truly optional by design)
        # The 'correlation_logger' is now removed from the explicit dependencies list
        # that _extract_dependencies_for_factory passes for these production services.
        allowed_none_deps = [
            'original_conditioner', 'original_position_manager', 'snapshot_interpreter',
            'rest_client', 'risk_service_ref', # risk_service_ref for TMS can be None if RMS itself is None
            'gui_logger_func', 'benchmarker', 'pipeline_validator',
            # 'correlation_logger', # Removed from allowed_none if no longer a param to any factory method
            'original_component_instance' # This is for the factory to get base args, not for the component's direct use
        ]

        missing_deps = [name for name, dep in dependencies.items() if dep is None and name not in allowed_none_deps]

        if missing_deps:
            # Only log as warning if a critical dependency is missing
            # For now, just log all "None" dependencies that aren't explicitly optional
            logger.warning(f"Factory: Creating {component_name} with some dependencies as None: {missing_deps}. Ensure this is intended.")

        return True

# Note on Factory Dependencies:
# The exact dependencies (like original_conditioner, original_broker_service, etc.) 
# passed to factory methods and then to component constructors are critical and must 
# match the actual __init__ signatures of your base production classes. 
# The placeholders above are illustrative and should be adjusted based on actual 
# component constructor requirements.
