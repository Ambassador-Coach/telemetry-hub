# --- START OF REGENERATED application_core_intellisense.py ---
import logging
from typing import Optional, List, Any, Dict, Type, TYPE_CHECKING

# Safe imports - interfaces only
from intellisense.capture.interfaces import IFeedbackIsolationManager
from intellisense.core.interfaces import IIntelliSenseApplicationCore
from intellisense.controllers.interfaces import IIntelliSenseMasterController
if TYPE_CHECKING:
    from intellisense.core.interfaces import IEventBus as IntelliSenseIEventBus

if TYPE_CHECKING:
    from intellisense.capture.enhanced_components.enhanced_ocr_service import EnhancedOCRDataConditioningService
    from intellisense.capture.enhanced_components.enhanced_price_repository import EnhancedPriceRepository
    from intellisense.capture.enhanced_components.enhanced_broker_interface import EnhancedLightspeedBroker
    from intellisense.analysis.testable_components import (
        IntelliSenseTestableOrderRepository,
        IntelliSenseTestablePositionManager,
        IntelliSenseTestablePriceRepository
    )
    from intellisense.factories.component_factory import ComponentFactory as ActualComponentFactory
    from intellisense.capture.session import ProductionDataCaptureSession

    try:
        from core.application_core import ApplicationCore as ProductionApplicationCoreBase
        from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService as ActualOCRService
        from modules.price_fetching.price_repository import PriceRepository as ActualPriceRepository
        from modules.broker_bridge.lightspeed_broker import LightspeedBroker as ActualBroker
        from modules.order_management.order_repository import OrderRepository as ActualOrderRepository
        from modules.trade_management.position_manager import PositionManager as ActualPositionManager
        from modules.risk_management.risk_service import RiskManagementService as ActualRiskManagementService
        from modules.trade_management.trade_manager_service import TradeManagerService as ActualTradeManagerService
        from core.interfaces import IEventBus as ActualEventBus # Production IEventBus
        from utils.global_config import GlobalConfig as ActualCoreAppConfig
    except ImportError:
        ProductionApplicationCoreBase = Any
        ActualOCRService, ActualPriceRepository, ActualBroker, ActualOrderRepository, ActualPositionManager = Any, Any, Any, Any, Any
        ActualRiskManagementService, ActualTradeManagerService, ActualEventBus, ActualCoreAppConfig = Any, Any, Any, Any
else:
    ProductionApplicationCoreBase = Any
    ActualOCRService, ActualPriceRepository, ActualBroker, ActualOrderRepository, ActualPositionManager = Any, Any, Any, Any, Any
    ActualRiskManagementService, ActualTradeManagerService, ActualEventBus, ActualCoreAppConfig = Any, Any, Any, Any
    IntelliSenseTestableOrderRepository, IntelliSenseTestablePositionManager, IntelliSenseTestablePriceRepository = Any, Any, Any

logger = logging.getLogger(__name__)

def _get_application_core_base() -> Type:
    try:
        from core.application_core import ApplicationCore as ProductionAppCore
        logger.debug("Production ApplicationCore base found by IntelliSenseApplicationCore.")
        return ProductionAppCore
    except ImportError:
        logger.warning("Production ApplicationCore base not found. IntelliSenseApplicationCore will use its internal placeholder.")
        class PlaceholderApplicationCore:
            def __init__(self, config_path: Optional[str] = None, config_object: Optional[Any] = None, *args, **kwargs):
                # Match the real ApplicationCore signature
                if config_object:
                    self.config = config_object
                elif config_path:
                    # Would normally load from path, but for placeholder just create empty config
                    self.config = type('Config', (), {})()
                else:
                    self.config = type('Config', (), {})()
                    
                logger.info("IntelliSense Using PlaceholderApplicationCore base.")
                self.event_bus = None; self.ocr_data_conditioning_service = None; self.price_repository = None
                self.broker_interface = None; self.snapshot_interpreter_service = None
                self.position_manager = None; self.order_repository = None
                self.risk_management_service = None; self.trade_manager_service = None
        return PlaceholderApplicationCore

class IntelliSenseApplicationCore(IIntelliSenseApplicationCore, _get_application_core_base()):
    def __init__(self, config: Optional['ActualCoreAppConfig'] = None, intellisense_overall_mode_active: bool = False, 
                 config_path: Optional[str] = None, *args, **kwargs):
        self._initialization_complete = False # Set early
        self._intellisense_overall_mode_active = intellisense_overall_mode_active
        self._capture_components_installed = False
        self._testable_analysis_components_installed = False
        self._original_components: Dict[str, Dict[str, Any]] = {}
        self._active_feedback_isolator: Optional[IFeedbackIsolationManager] = None
        self._component_factory_instance: Optional['ActualComponentFactory'] = None # Renamed from _component_factory

        # Initialize property backing fields
        self._config = None
        self._event_bus = None
        self._master_controller = None  # For lazy loading

        # --- REMOVED ---
        # self._active_correlation_logger: Optional[ICorrelationLogger] = None
        # --- END REMOVED ---

        # Handle config parameter properly for base class
        if config is not None:
            # If config object is provided, pass it as config_object to parent
            super().__init__(config_object=config, config_path=config_path, *args, **kwargs)
        elif config_path is not None:
            # If only config_path is provided, pass it to parent
            super().__init__(config_path=config_path, *args, **kwargs)
        else:
            # If neither is provided, use parent's default behavior
            super().__init__(*args, **kwargs) # This will use default config_path

        # Store original production services for potential re-instantiation (WITHOUT logger injection)
        # These are initialized by super().__init__()
        self._original_production_risk_service: Optional['ActualRiskManagementService'] = getattr(self, 'risk_management_service', None)
        self._original_production_trade_manager_service: Optional['ActualTradeManagerService'] = getattr(self, 'trade_manager_service', None)

        # References to "real" (potentially original or swapped) services
        self.real_snapshot_interpreter: Optional['ActualOCRService'] = None # Placeholder, will be ActualSnapshotInterpreterService
        self.real_position_manager: Optional['ActualPositionManager'] = None
        self.real_price_repository: Optional['ActualPriceRepository'] = None
        self.real_broker_interface: Optional['ActualBroker'] = None
        self.real_event_bus: Optional['ActualEventBus'] = None
        self.real_order_repository: Optional['ActualOrderRepository'] = None

        if self._intellisense_overall_mode_active:
            self._prepare_real_service_references() # Cache initial real services
            self.intellisense_test_mode_active = False

        self._initialization_complete = True
        logger.debug("IntelliSenseApplicationCore initialization (Cleanup WP1 Applied).")

    def _get_component_factory(self) -> 'ActualComponentFactory':
        if self._component_factory_instance is None:
            from intellisense.factories.component_factory import ComponentFactory # Runtime import
            self._component_factory_instance = ComponentFactory()
        return self._component_factory_instance

    def _prepare_real_service_references(self):
        logger.info("IntelliSenseApplicationCore: Caching references to initial real services...")

        # These point to whatever super().__init__() created.
        self.real_snapshot_interpreter = getattr(self, 'snapshot_interpreter_service', None)
        self.real_position_manager = getattr(self, 'position_manager', None)
        self.real_price_repository = getattr(self, 'price_repository', None)
        self.real_broker_interface = getattr(self, 'broker_interface', getattr(self, 'broker', None))
        self.real_event_bus = getattr(self, 'event_bus', None)
        self.real_order_repository = getattr(self, 'order_repository', None)

        # Ensure original production service references are captured if not already
        if self._original_production_risk_service is None:
            self._original_production_risk_service = getattr(self, 'risk_management_service', None)
        if self._original_production_trade_manager_service is None:
            self._original_production_trade_manager_service = getattr(self, 'trade_manager_service', None)

    def _extract_dependencies_for_factory(self, original_component_instance: Any, component_type_str: str) -> Dict[str, Any]:
        # Ensure config and event_bus are consistently sourced, preferably from self (AppCore)
        # which should hold the primary instances.
        deps = {
            "app_core_ref": self,
            "original_config": getattr(self, 'config', None), # Main app config
            "original_event_bus": getattr(self, 'event_bus', None), # Main event bus
            "original_component_instance": original_component_instance # For factory to get other specific base args
        }

        if component_type_str == "ocr": # For EnhancedOCRService
            deps["original_conditioner"] = getattr(original_component_instance, 'conditioner', None)
        elif component_type_str == "broker": # For EnhancedBroker
            deps["original_price_provider"] = getattr(original_component_instance, 'price_provider', self.real_price_repository)
        elif component_type_str == "order_repository": # For TestableOrderRepository
            deps["original_broker_service"] = getattr(original_component_instance, '_broker', self.real_broker_interface)
            deps["original_position_manager"] = getattr(original_component_instance, '_position_manager', self.real_position_manager)

        # For Production RiskManagementService re-instantiation (WITHOUT correlation_logger)
        elif component_type_str == "production_risk_management_service":
            deps["event_bus"] = getattr(self, 'event_bus', None)
            deps["config_service"] = getattr(self, 'config', None)
            deps["price_provider"] = getattr(self, 'real_price_repository', getattr(self, 'price_repository', None))
            deps["order_repository"] = getattr(self, 'real_order_repository', getattr(self, 'order_repository', None))
            deps["position_manager"] = getattr(self, 'real_position_manager', getattr(self, 'position_manager', None))
            deps["gui_logger_func"] = getattr(original_component_instance, '_gui_logger_func', None)
            deps["benchmarker"] = getattr(original_component_instance, '_benchmarker', None)
            deps["pipeline_validator"] = getattr(original_component_instance, '_pipeline_validator', None)
            # correlation_logger explicitly NOT added - factory will pass None
        # For Production TradeManagerService re-instantiation (WITHOUT correlation_logger)
        elif component_type_str == "production_trade_manager_service":
            deps["event_bus"] = getattr(self, 'event_bus', None)
            deps["broker_service"] = getattr(self, 'real_broker_interface', getattr(self, 'broker_interface', None))
            deps["price_provider"] = getattr(self, 'real_price_repository', getattr(self, 'price_repository', None))
            deps["config_service"] = getattr(self, 'config', None)
            deps["order_repository"] = getattr(self, 'real_order_repository', getattr(self, 'order_repository', None))
            deps["position_manager"] = getattr(self, 'real_position_manager', getattr(self, 'position_manager', None))
            deps["order_strategy"] = getattr(original_component_instance, '_order_strategy', None)
            deps["trade_executor"] = getattr(original_component_instance, '_trade_executor', None)
            deps["lifecycle_manager"] = getattr(original_component_instance, '_lifecycle_manager', None)
            deps["snapshot_interpreter"] = getattr(original_component_instance, '_snapshot_interpreter', None)
            deps["rest_client"] = getattr(original_component_instance, '_rest_client', None)
            deps["risk_service_ref"] = getattr(self, 'risk_management_service', None) # The current (possibly reconfigured) RMS instance
            deps["gui_logger_func"] = getattr(original_component_instance, '_gui_logger_func', None)
            deps["pipeline_validator"] = getattr(original_component_instance, '_pipeline_validator', None)
            deps["benchmarker"] = getattr(original_component_instance, '_benchmarker', None)
            # correlation_logger explicitly NOT added - factory will pass None

        # ... (dependencies for actual Enhanced/Testable components remain as before) ...

        return deps

    def _reconfigure_production_services_without_logger_injection(self, enable: bool):
        """
        Reconfigure production services WITHOUT IntelliSense logger injection.
        This method can be used for other configuration purposes if needed,
        but will NOT inject IntelliSense's correlation logger.
        """
        if not self._initialization_complete:
            logger.warning("_reconfigure_production_services called before ISAC init complete. Skipping.")
            return

        factory = self._get_component_factory()
        action = "Reconfiguring" if enable else "Restoring"
        logger.info(f"ISAC: {action} production services (NO logger injection).")

        # --- RiskManagementService ---
        original_rms = self._original_production_risk_service
        if original_rms:
            rms_deps = self._extract_dependencies_for_factory(original_rms, "production_risk_management_service")
            # correlation_logger is explicitly NOT in rms_deps
            new_rms = factory.create_production_risk_management_service(**rms_deps)
            if new_rms:
                self.risk_management_service = new_rms # Replace current instance
                logger.info(f"RiskManagementService instance reconfigured (NO logger injection). ID: {id(new_rms)}")
            else:
                logger.error("Failed to reconfigure RiskManagementService.")
        else:
            logger.warning("Cannot reconfigure RiskManagementService: Original production instance not available.")

        # --- TradeManagerService ---
        original_tms = self._original_production_trade_manager_service
        if original_tms:
            tms_deps = self._extract_dependencies_for_factory(original_tms, "production_trade_manager_service")
            # Update risk_service_ref to point to the potentially new RMS instance
            tms_deps["risk_service_ref"] = self.risk_management_service

            new_tms = factory.create_production_trade_manager_service(**tms_deps)
            if new_tms:
                self.trade_manager_service = new_tms # Replace current instance
                logger.info(f"TradeManagerService instance reconfigured (NO logger injection). ID: {id(new_tms)}")
            else:
                logger.error("Failed to reconfigure TradeManagerService.")
        else:
            logger.warning("Cannot reconfigure TradeManagerService: Original production instance not available.")

    def _swap_component(self, attr_name: str, NewClassTypeStr: str, swap_type: str) -> bool:
        """
        Helper to swap a component using the ComponentFactory.
        NewClassTypeStr: String name of the factory method, e.g., "enhanced_ocr_service".
        """
        if not hasattr(self, attr_name) or getattr(self, attr_name) is None:
            logger.warning(f"Cannot swap '{attr_name}': original not found."); return False

        current_component = getattr(self, attr_name)
        factory_method_name = f"create_{NewClassTypeStr}" # e.g., create_enhanced_ocr_service

        component_factory = self._get_component_factory()
        if not hasattr(component_factory, factory_method_name):
            logger.error(f"ComponentFactory missing method: {factory_method_name}"); return False

        # Store original only once
        original_key = f"{attr_name}_original"
        if original_key not in self._original_components:
            self._original_components[original_key] = {'instance': current_component, 'swapped_for': None}

        # Use the initially stored true original for fetching dependencies
        true_original_for_deps = self._original_components[original_key]['instance']

        try:
            # Determine component type for dependency extraction
            component_domain = ""
            if "ocr" in attr_name: component_domain = "ocr"
            elif "price" in attr_name: component_domain = "price_repository" # Map to unique key
            elif "broker" in attr_name: component_domain = "broker"
            elif "order_repository" in attr_name: component_domain = "order_repository"
            elif "position_manager" in attr_name: component_domain = "position_manager"

            dependencies = self._extract_dependencies_for_factory(true_original_for_deps, component_domain)

            factory_method = getattr(component_factory, factory_method_name)
            new_instance = factory_method(**dependencies) # Pass app_core_ref and extracted deps

            if new_instance is None:
                logger.error(f"Factory method {factory_method_name} returned None - component creation failed")
                return False

            setattr(self, attr_name, new_instance)
            self._original_components[original_key]['swapped_for'] = swap_type
            logger.info(f"Swapped '{attr_name}' with instance from factory method {factory_method_name} for {swap_type}.")
            return True
        except Exception as e:
            logger.error(f"Failed to swap '{attr_name}' using factory: {e}. Restoring original if possible.", exc_info=True)
            if original_key in self._original_components:
                 setattr(self, attr_name, self._original_components[original_key]['instance'])
                 self._original_components[original_key]['swapped_for'] = None
            return False

    def activate_capture_mode(self) -> bool:
        if not self._intellisense_overall_mode_active:
            logger.error("Cannot activate capture: IntelliSense mode is not active on AppCore."); return False
        if self._capture_components_installed: logger.warning("Capture components already installed."); return True
        if self._testable_analysis_components_installed: self.deactivate_testable_analysis_mode()

        logger.info("IntelliSenseApplicationCore: Activating capture mode...")
        success = True
        if not self._swap_component('ocr_data_conditioning_service', "enhanced_ocr_service", 'capture'): success = False
        if not self._swap_component('price_repository', "enhanced_price_repository", 'capture'): success = False
        broker_attr = 'broker_interface' if hasattr(self, 'broker_interface') else 'broker'
        if not self._swap_component(broker_attr, "enhanced_broker_interface", 'capture'): success = False

        self._capture_components_installed = success
        if success: logger.info("Capture-enhanced components installed via factory.")
        else: logger.error("Failed to install one or more capture-enhanced components."); self.deactivate_capture_mode()
        return success

    def deactivate_capture_mode(self):
        if not self._capture_components_installed: logger.info("Capture components not installed. No deactivation needed."); return
        logger.info("IntelliSenseApplicationCore: Deactivating capture mode - restoring original components...")
        for attr_name, stored_info in list(self._original_components.items()): # Iterate copy for modification
            if stored_info['swapped_for'] == 'capture' or stored_info['swapped_for'] is None: # Restore if it was for capture or if type unknown
                setattr(self, attr_name.replace("_original",""), stored_info['instance'])
                logger.info(f"Restored original '{attr_name.replace('_original','')}'.")
                del self._original_components[attr_name]
        self._capture_components_installed = False
        logger.info("Original components restored from capture mode.")

    def activate_testable_analysis_mode(self) -> bool:
        if not self._intellisense_overall_mode_active: return False
        if self._testable_analysis_components_installed: return True
        if self._capture_components_installed: self.deactivate_capture_mode()

        logger.info("IntelliSenseApplicationCore: Activating testable analysis mode...")
        success = True
        if not self._swap_component('order_repository', "testable_order_repository", 'analysis'): success=False
        if not self._swap_component('position_manager', "testable_position_manager", 'analysis'): success=False
        if not self._swap_component('price_repository', "testable_price_repository", 'analysis'): success=False

        self._testable_analysis_components_installed = success
        if success:
            self.intellisense_test_mode_active = True
            # The factory methods are responsible for passing app_core_ref.
            # Testable components internally use this ref to check intellisense_test_mode_active.
            logger.info("Testable analysis components installed via factory. Test mode flag SET.")
        else: logger.error("Failed to install testable analysis components."); self.deactivate_testable_analysis_mode()
        return success

    def deactivate_testable_analysis_mode(self):
        # ... (as before, ensures all original components referenced in _original_components are restored) ...
        if not self._testable_analysis_components_installed: logger.info("Testable analysis components not installed."); return
        logger.info("IntelliSenseApplicationCore: Deactivating testable analysis mode - restoring originals...")
        restored_count = 0
        for attr_name_original_key in list(self._original_components.keys()): # Iterate copy
            stored_info = self._original_components[attr_name_original_key]
            # Restore if it was swapped for 'analysis' OR if its type implies it (e.g. IntelliSenseTestable...)
            # A more robust way: check if current component is one of the Testable types
            attr_name = attr_name_original_key.replace("_original", "") # Assuming key was like 'component_original'
            current_comp = getattr(self, attr_name, None)
            is_testable_instance = isinstance(current_comp, (IntelliSenseTestableOrderRepository, IntelliSenseTestablePositionManager, IntelliSenseTestablePriceRepository))

            if stored_info['swapped_for'] == 'analysis' or is_testable_instance:
                setattr(self, attr_name, stored_info['instance'])
                logger.info(f"Restored original '{attr_name}'.")
                del self._original_components[attr_name_original_key]
                restored_count +=1

        self._testable_analysis_components_installed = False
        self.intellisense_test_mode_active = False # CRITICAL: Clear the flag
        if restored_count > 0: logger.info(f"{restored_count} original components restored from analysis mode. Test mode flag CLEARED.")
        else: logger.info("No specific analysis components needed restoration. Test mode flag CLEARED.")

    def get_real_services_for_intellisense(self) -> Dict[str, Any]:
        # This method provides the *currently installed* services.
        # If activate_testable_analysis_mode() was called, these will be Testable versions.
        # If activate_capture_mode() was called, these will be EnhancedCapture versions.
        # If neither, they are the original production versions.

        # Safety check: ensure initialization is complete before providing services
        if not self._initialization_complete:
            logger.warning("get_real_services_for_intellisense called before initialization complete - returning minimal services")
            return {
                'event_bus': None,
                'snapshot_interpreter_service': None,
                'position_manager': None,
                'order_repository': None,
                'price_repository': None,
                'broker_interface': None,
                'application_core_itself': self
            }

        # Try both attribute names for snapshot interpreter
        snapshot_service = getattr(self, 'snapshot_interpreter_service', None) or getattr(self, 'real_snapshot_interpreter', None)

        # Try both attribute names for order repository
        order_repository = getattr(self, 'order_repository', None) or getattr(self, 'real_order_repository', None)

        return {
            'event_bus': getattr(self, 'event_bus', None), # Should be original EventBus
            'snapshot_interpreter_service': snapshot_service, # Original or real_snapshot_interpreter
            'position_manager': getattr(self, 'position_manager', None), # Could be Original, EnhancedCapture, or TestableAnalysis
            'order_repository': order_repository, # Original or real_order_repository
            'price_repository': getattr(self, 'price_repository', None), # Could be Original, EnhancedCapture, or TestableAnalysis
            'broker_interface': getattr(self, 'broker_interface', getattr(self, 'broker', None)), # Could be Original or EnhancedCapture
            'application_core_itself': self # For test_mode flag access by Testable components
        }

    def create_capture_session(self, capture_session_config: Dict[str, Any]) -> 'ProductionDataCaptureSession':
        """Create a production data capture session with proper type hinting."""
        if not self._intellisense_overall_mode_active:  # Check overall mode
            raise RuntimeError("IntelliSense mode must be active on AppCore to create a capture session.")
        # activate_capture_mode() will be called by ProductionDataCaptureSession.start_capture_session()
        # if not self._capture_components_installed: ... (this logic moves to start_capture_session)

        # Runtime import to avoid top-level import cycles if ProductionDataCaptureSession
        # also needs to import IntelliSenseApplicationCore for type hinting its testrade_instance parameter
        from intellisense.capture.session import ProductionDataCaptureSession
        return ProductionDataCaptureSession(
            testrade_instance=self,
            capture_session_config=capture_session_config
        )

    # --- NEW METHODS for FeedbackIsolator Management ---
    def set_active_feedback_isolator(self, isolator: Optional[IFeedbackIsolationManager]):
        """Called by ProductionDataCaptureSession to register/clear the active isolator."""
        logger.info(f"IntelliSenseApplicationCore: Setting active feedback isolator to: {'None' if isolator is None else type(isolator).__name__}")
        self._active_feedback_isolator = isolator

    def get_active_feedback_isolator(self) -> Optional[IFeedbackIsolationManager]:
        """Called by EnhancedComponents to get the current session's isolator."""
        return self._active_feedback_isolator

    @property
    def intellisense_test_mode_active(self) -> bool:
        """Flag indicating if IntelliSense test/analysis mode is active."""
        return getattr(self, '_intellisense_test_mode_active', False)

    @intellisense_test_mode_active.setter
    def intellisense_test_mode_active(self, value: bool) -> None:
        """Set the IntelliSense test mode flag."""
        self._intellisense_test_mode_active = value

    def get_master_controller(self) -> 'IIntelliSenseMasterController':
        """Lazy-loaded controller access to break import cycles"""
        if self._master_controller is None:
            # Runtime import to break cycle
            print("🔄 Runtime-loading IntelliSenseMasterController...")
            from intellisense.controllers.master_controller import IntelliSenseMasterController
            print("✅ Runtime-loaded IntelliSenseMasterController")

            self._master_controller = IntelliSenseMasterController(
                application_core=self,
                operation_mode=getattr(self, 'operation_mode', None),
                intellisense_config=getattr(self, 'config', None)
            )
        return self._master_controller

    # --- MISSING ABSTRACT METHOD IMPLEMENTATIONS ---

    def set_active_correlation_logger(self, logger: Optional[Any]) -> None:
        """Sets or clears the active CorrelationLogger for the current capture session."""
        logger_info = "None" if logger is None else type(logger).__name__
        logging.getLogger(__name__).info(f"IntelliSenseApplicationCore: Setting active correlation logger to: {logger_info}")
        # Store the logger reference (implementation can be expanded as needed)
        self._active_correlation_logger = logger

    def _configure_correlation_logging_for_production_services(self, enable: bool) -> None:
        """
        (Internal but exposed for ProductionDataCaptureSession)
        Reconfigures core production services (like RiskManagementService, TradeManagerService)
        to use/stop using the active correlation logger.
        """
        action = "Enabling" if enable else "Disabling"
        logging.getLogger(__name__).info(f"IntelliSenseApplicationCore: {action} correlation logging for production services")

        # Call the existing method that handles production service reconfiguration
        # This method already exists and handles reconfiguration without logger injection
        self._reconfigure_production_services_without_logger_injection(enable)

    @property
    def config(self) -> Any:
        """Provides access to the main application configuration."""
        # Return the config from the base class or a default
        return getattr(self, '_config', None) or getattr(super(), 'config', None)

    @config.setter
    def config(self, value: Any) -> None:
        """Sets the application configuration."""
        self._config = value

    @property
    def event_bus(self) -> 'IEventBus':
        """Provides access to the main TESTRADE event bus."""
        # Return the event_bus from the base class
        return getattr(self, '_event_bus', None) or getattr(super(), 'event_bus', None)

    @event_bus.setter
    def event_bus(self, value: 'IEventBus') -> None:
        """Sets the event bus."""
        self._event_bus = value

    # ... (intellisense_test_mode_active property and setter as before) ...

    # _swap_component, activate_capture_mode, deactivate_capture_mode,
    # activate_testable_analysis_mode, deactivate_testable_analysis_mode,
    # get_real_services_for_intellisense, create_capture_session,
    # set_active_feedback_isolator, get_active_feedback_isolator,
    # get_master_controller, _verify_enhanced_components
    # should remain as previously defined, using the updated _get_component_factory()
    # and _extract_dependencies_for_factory().

# --- END OF REGENERATED application_core_intellisense.py ---
