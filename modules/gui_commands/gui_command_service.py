"""
GUI Command Service

This service handles all GUI commands sent to ApplicationCore, providing a clean
separation of concerns and easy extensibility for new commands.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, TYPE_CHECKING

from .command_result import CommandResult, CommandStatus
from interfaces.logging.services import ICorrelationLogger
from interfaces.core.services import IConfigService, IEventBus
from interfaces.order_management.services import IOrderRepository
from interfaces.broker.services import IBrokerService
from interfaces.data.services import IPriceProvider
from interfaces.trading.services import IPositionManager, ITradeLifecycleManager
from interfaces.data.services import IMarketDataPublisher
from interfaces.core.services import IBulletproofBabysitterIPCClient

if TYPE_CHECKING:
    from core.application_core import ApplicationCore

logger = logging.getLogger(__name__)


class GUICommandService:
    """
    Service for handling GUI commands sent to ApplicationCore.
    
    This service provides a clean, extensible way to handle GUI commands
    without cluttering ApplicationCore with command-specific logic.
    """
    
    def __init__(self, correlation_logger: Optional[ICorrelationLogger] = None, 
                 order_repository: IOrderRepository = None, config_service: IConfigService = None,
                 broker_service: IBrokerService = None, event_bus: IEventBus = None,
                 price_provider: IPriceProvider = None, position_manager: IPositionManager = None,
                 trade_lifecycle_manager: ITradeLifecycleManager = None, 
                 ipc_client: IBulletproofBabysitterIPCClient = None):
        """
        Initialize the GUI command service.
        
        Args:
            correlation_logger: Correlation logger for publishing events
            order_repository: OrderRepository for direct access (preferred DI approach)
            config_service: Configuration service for accessing config data
            broker_service: Broker service for trading operations
            event_bus: Event bus for event publishing
            price_provider: Price provider for market data
            position_manager: Position manager for position operations
            trade_lifecycle_manager: Trade lifecycle manager for trade operations
            ipc_client: IPC client for communication with external processes
        """
        self._order_repository = order_repository
        self._config_service = config_service
        self._broker_service = broker_service
        self._event_bus = event_bus
        self._price_provider = price_provider
        self._position_manager = position_manager
        self._trade_lifecycle_manager = trade_lifecycle_manager
        self._ipc_client = ipc_client
        self.logger = logger
        self._correlation_logger = correlation_logger
        
        # Register available commands
        self._command_handlers = {
            "START_APPLICATION_CORE": self._handle_start_application_core,
            "STOP_APPLICATION_CORE": self._handle_stop_application_core,
            "SET_ROI_ABSOLUTE": self._handle_set_roi_absolute,
            "ROI_ADJUST": self._handle_roi_adjust,
            "START_OCR": self._handle_start_ocr,
            "STOP_OCR": self._handle_stop_ocr,
            "TOGGLE_CONFIDENCE_MODE": self._handle_toggle_confidence_mode,
            "UPDATE_OCR_PREPROCESSING_FULL": self._handle_update_ocr_preprocessing,
            "UPDATE_TRADING_PARAMS": self._handle_update_trading_params,
            "SAVE_CONFIGURATION": self._handle_save_configuration,
            "UPDATE_RECORDING_SETTINGS": self._handle_update_recording_settings,
            "UPDATE_LOGGING_SETTINGS": self._handle_update_logging_settings,
            "UPDATE_DEVELOPMENT_MODE": self._handle_update_development_mode,
            "DUMP_DATA": self._handle_dump_data,
            "GET_GLOBAL_CONFIGURATION": self._handle_get_global_configuration,
            "RESET_TO_DEFAULT_CONFIG": self._handle_reset_to_default_config,
            "DEBUG_POSITIONS": self._handle_debug_positions,
            "TEST_FUNCTION": self._handle_test_function,
            # Trading Commands
            "MANUAL_TRADE": self._handle_manual_trade,
            "FORCE_CLOSE_ALL": self._handle_force_close_all,
            "EMERGENCY_STOP": self._handle_emergency_stop,
            # === BOOTSTRAP COMMANDS ===
            "REQUEST_INITIAL_STATE": self._handle_request_initial_state,
            # NEW: Individual Bootstrap API commands for GUI redesign
            "GET_ALL_POSITIONS_BOOTSTRAP": self._handle_get_all_positions_bootstrap,
            "GET_ACCOUNT_SUMMARY_BOOTSTRAP": self._handle_get_account_summary_bootstrap,
            "GET_ORDERS_TODAY_BOOTSTRAP": self._handle_get_orders_today_bootstrap,
            "GET_MARKET_STATUS_BOOTSTRAP": self._handle_get_market_status_bootstrap,
            # === DEBUG COMMANDS FOR PIPELINE TESTING ===
            "DEBUG_TRIGGER_ON_QUOTE": self._handle_debug_trigger_on_quote,
            "DEBUG_TRIGGER_ON_TRADE": self._handle_debug_trigger_on_trade,
            "DEBUG_SIMULATE_POSITION_UPDATE": self._handle_debug_simulate_position_update,
            # === IPC MONITORING COMMANDS ===
            "GET_IPC_CLIENT_STATUS": self._handle_get_ipc_client_status,
            # === OBSOLETE DATA REQUESTS (now point to an error handler) ===
            "GET_ALL_POSITIONS": self._handle_obsolete_data_request,
            "GET_ACCOUNT_SUMMARY": self._handle_obsolete_data_request,
            "GET_ALL_ORDERS": self._handle_obsolete_data_request,
            "GET_POSITION_SUMMARY": self._handle_obsolete_data_request,
            "GET_TRADE_HISTORY": self._handle_get_trade_history_obsolete,
            "get_trade_history": self._handle_get_trade_history_obsolete,  # Handle lowercase version
        }
        
        self.logger.info(f"GUICommandService initialized with {len(self._command_handlers)} command handlers")
    
    def _send_ocr_command_via_ipc(self, command_type: str, parameters: Dict[str, Any]) -> Optional[str]:
        """Send OCR command via IPC client (replacement for app_core.send_ocr_command_via_babysitter)."""
        if not self._ipc_client:
            self.logger.error(f"Cannot send OCR command {command_type}: IPC client not available")
            return None
        
        try:
            # Generate command ID
            import uuid
            command_id = str(uuid.uuid4())
            
            # Send command via IPC client
            success = self._ipc_client.send_command_to_ocr_process(command_type, parameters, command_id)
            return command_id if success else None
        except Exception as e:
            self.logger.error(f"Error sending OCR command {command_type}: {e}")
            return None
    
    def handle_command(self, command_type: str, parameters: Dict[str, Any], command_id: str = None) -> CommandResult:
        """
        Handle a GUI command.
        
        Args:
            command_type: The type of command to execute
            parameters: Command parameters
            command_id: Optional command ID for correlation
            
        Returns:
            CommandResult with execution status and details
        """
        self.logger.info(f"Processing GUI command '{command_type}' (ID: {command_id}) with params: {parameters}")
        self.logger.debug(f"Available command handlers: {list(self._command_handlers.keys())}")

        handler = self._command_handlers.get(command_type)
        if not handler:
            available_commands = list(self._command_handlers.keys())
            error_msg = f"Unknown command type: {command_type}. Available: {available_commands}"
            self.logger.error(error_msg)
            return CommandResult.error(error_msg, command_id=command_id)
        
        try:
            result = handler(parameters)
            result.command_id = command_id  # Ensure command ID is set
            self.logger.info(f"Command '{command_type}' (ID: {command_id}) completed with status: {result.status.value}")
            return result
            
        except Exception as e:
            error_msg = f"Error executing command '{command_type}': {e}"
            self.logger.error(error_msg, exc_info=True)
            return CommandResult.error(error_msg, command_id=command_id)
    
    def get_available_commands(self) -> list[str]:
        """Get list of available command types."""
        return list(self._command_handlers.keys())
    
    # Command Handlers
    
    def _handle_start_application_core(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle START_APPLICATION_CORE command."""
        try:
            self.app_core.start()
            return CommandResult.success("ApplicationCore started successfully")
        except Exception as e:
            return CommandResult.error(f"Failed to start ApplicationCore: {e}")
    
    def _handle_stop_application_core(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle STOP_APPLICATION_CORE command."""
        try:
            timeout = parameters.get('timeout', 10.0)

            # Use graceful shutdown if available, fallback to regular stop
            if hasattr(self.app_core, 'graceful_shutdown'):
                success = self.app_core.graceful_shutdown(timeout=timeout)
                if success:
                    return CommandResult.success(f"ApplicationCore stopped gracefully within {timeout}s")
                else:
                    return CommandResult.error(f"ApplicationCore shutdown timed out after {timeout}s")
            else:
                self.app_core.stop()
                return CommandResult.success("ApplicationCore stopped successfully")
        except Exception as e:
            return CommandResult.error(f"Failed to stop ApplicationCore: {e}")
    
    def _handle_set_roi_absolute(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle SET_ROI_ABSOLUTE command - Phase 5: Forward to OCR via Babysitter."""
        try:
            # Extract ROI coordinates for logging and response
            x1 = parameters.get('x1', 0)
            y1 = parameters.get('y1', 0)
            x2 = parameters.get('x2', 100)
            y2 = parameters.get('y2', 100)

            self.logger.info(f"Phase 5: Processing SET_ROI_ABSOLUTE: ({x1}, {y1}, {x2}, {y2})")

            # Phase 5: Forward ROI command to OCR process via Babysitter → ZMQ
            ocr_command_id = self._send_ocr_command_via_ipc("SET_ROI_ABSOLUTE", parameters)
            if ocr_command_id:
                return CommandResult.info(
                    f"SET_ROI_ABSOLUTE command sent to OCR process via Babysitter. Awaiting response.",
                    data={"roi": [x1, y1, x2, y2], "ocr_command_id": ocr_command_id}
                )
            else:
                return CommandResult.error("Failed to send ROI command to OCR process via Babysitter")
        except Exception as e:
            return CommandResult.error(f"Error processing ROI command: {e}")

    def _handle_roi_adjust(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle ROI_ADJUST command for incremental ROI adjustments - Phase 5: Forward to OCR via Babysitter."""
        try:
            direction = parameters.get('direction', '')
            shift = parameters.get('shift', False)

            # Determine step size (larger if shift is held)
            step_size = 10 if shift else 1

            self.logger.info(f"Phase 5: ROI adjust: direction={direction}, shift={shift}, step_size={step_size}")

            # Phase 5: Forward to OCR process via Babysitter → ZMQ
            ocr_command_id = self._send_ocr_command_via_ipc("ROI_ADJUST", {
                "direction": direction,
                "step_size": step_size,
                "shift": shift
            })

            if ocr_command_id:
                return CommandResult.info(
                    f"ROI_ADJUST command sent to OCR process via Babysitter. Awaiting response.",
                    data={"ocr_command_id": ocr_command_id, "direction": direction, "step_size": step_size}
                )
            else:
                return CommandResult.error("Failed to send ROI adjust command to OCR process via Babysitter")

        except Exception as e:
            return CommandResult.error(f"Error processing ROI adjust command: {e}")
    
    def _handle_start_ocr(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle START_OCR command - Send command via Babysitter IPC system."""
        try:
            self.logger.info("Processing START_OCR command")

            # Send START_OCR command to OCR process via Babysitter → ZMQ
            ocr_command_id = self._send_ocr_command_via_ipc("START_OCR", {})
            if ocr_command_id:
                # Send status update to GUI
                self._send_ocr_status_update("OCR_START_REQUESTED")
                return CommandResult.success(
                    "START_OCR command sent to OCR process via Babysitter.",
                    data={"ocr_command_id": ocr_command_id}
                )
            else:
                self._send_ocr_status_update("OCR_START_FAILED")
                return CommandResult.error("Failed to send START_OCR command to OCR process via Babysitter")

        except Exception as e:
            # Send error status to GUI
            self._send_ocr_status_update("OCR_START_FAILED")
            return CommandResult.error(f"Failed to start OCR process: {e}")
    
    def _handle_stop_ocr(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle STOP_OCR command - Send command via Babysitter IPC system."""
        try:
            self.logger.info("Processing STOP_OCR command")

            # Send STOP_OCR command to OCR process via Babysitter → ZMQ
            ocr_command_id = self._send_ocr_command_via_ipc("STOP_OCR", {})
            if ocr_command_id:
                # Send status update to GUI
                self._send_ocr_status_update("OCR_STOP_REQUESTED")
                return CommandResult.success(
                    "STOP_OCR command sent to OCR process via Babysitter.",
                    data={"ocr_command_id": ocr_command_id}
                )
            else:
                self._send_ocr_status_update("OCR_STOP_FAILED")
                return CommandResult.error("Failed to send STOP_OCR command to OCR process via Babysitter")

        except Exception as e:
            # Send error status to GUI
            self._send_ocr_status_update("OCR_STOP_FAILED")
            return CommandResult.error(f"Failed to stop OCR process: {e}")
    
    def _handle_toggle_confidence_mode(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle TOGGLE_CONFIDENCE_MODE command."""
        # This might be a GUI-only setting, so just acknowledge for now
        return CommandResult.success("Confidence mode toggle acknowledged")
    
    def _handle_update_ocr_preprocessing(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle UPDATE_OCR_PREPROCESSING_FULL command - Forward to OCR process via Babysitter."""
        try:
            self.logger.info(f"Processing UPDATE_OCR_PREPROCESSING_FULL with parameters: {list(parameters.keys())}")

            # Validate that we have OCR parameters
            if not parameters:
                return CommandResult.error("No OCR parameters provided")

            # Forward OCR preprocessing command to OCR process via Babysitter → ZMQ
            ocr_command_id = self._send_ocr_command_via_ipc("UPDATE_OCR_PREPROCESSING_FULL", parameters)
            if ocr_command_id:
                return CommandResult.success(
                    f"UPDATE_OCR_PREPROCESSING_FULL command sent to OCR process via Babysitter.",
                    data={"ocr_command_id": ocr_command_id, "parameters_count": len(parameters)}
                )
            else:
                return CommandResult.error("Failed to send OCR preprocessing command to OCR process via Babysitter")

        except Exception as e:
            return CommandResult.error(f"Error processing OCR preprocessing command: {e}")
    
    def _handle_update_trading_params(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle UPDATE_TRADING_PARAMS command - updates trading configuration parameters."""
        try:
            from utils.global_config import config, update_config_key
            
            # Legacy: IPC client for config change publishing - should use telemetry service
            ipc_client = None
            if self.app_core and hasattr(self.app_core, 'babysitter_ipc_client'):
                ipc_client = self._ipc_client
            
            # Track what was updated
            updated_params = []
            failed_params = []
            
            # Define allowed trading parameters and their types
            allowed_params = {
                'initial_share_size': int,
                'add_type': str,
                'reduce_percentage': float,
                'manual_shares': int
            }
            
            # Update each parameter
            for param_name, param_value in parameters.items():
                if param_name in allowed_params:
                    try:
                        # Type conversion
                        typed_value = allowed_params[param_name](param_value)
                        
                        # Update config
                        success = update_config_key(
                            param_name, 
                            typed_value, 
                            ipc_client=ipc_client,
                            change_source_override="gui_update_trading_params"
                        )
                        
                        if success:
                            updated_params.append(f"{param_name}={typed_value}")
                            self.logger.info(f"Updated trading parameter: {param_name} = {typed_value}")
                        else:
                            failed_params.append(param_name)
                            self.logger.error(f"Failed to update {param_name}")
                            
                    except (ValueError, TypeError) as e:
                        failed_params.append(param_name)
                        self.logger.error(f"Invalid value for {param_name}: {param_value} - {e}")
                else:
                    self.logger.warning(f"Ignoring non-trading parameter: {param_name}")
            
            # Reload global config to ensure all services see the updates
            if updated_params:
                from utils.global_config import reload_global_config
                reload_global_config()
                self.logger.info("Global config reloaded after trading parameter updates")
                
                # Config saving is handled by the config service
                self.logger.debug("Configuration saved via ConfigService")
            
            # Build response
            if updated_params and not failed_params:
                return CommandResult.success(
                    f"Updated trading parameters: {', '.join(updated_params)}",
                    data={
                        "updated": updated_params,
                        "count": len(updated_params)
                    }
                )
            elif updated_params and failed_params:
                return CommandResult.warning(
                    f"Partially updated. Success: {', '.join(updated_params)}. Failed: {', '.join(failed_params)}",
                    data={
                        "updated": updated_params,
                        "failed": failed_params
                    }
                )
            elif failed_params:
                return CommandResult.error(
                    f"Failed to update parameters: {', '.join(failed_params)}"
                )
            else:
                return CommandResult.info("No valid trading parameters provided to update")
                
        except Exception as e:
            return CommandResult.error(f"Error updating trading parameters: {e}")
    
    def _handle_save_configuration(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle SAVE_CONFIGURATION command. This now triggers a "hot reload"
        of control.json from disk. The reload function itself handles diffing and publishing.
        """
        try:
            from utils.global_config import reload_global_config

            # 1. Get the IPC client to pass to the intelligent reload function
            ipc_client = None
            if self.app_core and hasattr(self.app_core, 'babysitter_ipc_client'):
                ipc_client = self._ipc_client
            
            # 2. Call the new, smarter reload function. It does all the work.
            reload_global_config(
                ipc_client=ipc_client,
                change_source_override="gui_reload_button_click"
            )
            
            # 3. Return success. The function no longer needs to know about the result of the diff.
            return CommandResult.success("Configuration hot-reload process initiated successfully.")
                
        except Exception as e:
            self.logger.error(f"Failed to trigger hot-reload of configuration: {e}", exc_info=True)
            return CommandResult.error(f"Failed to trigger hot-reload of configuration: {e}")

    def save_roi_to_config(self, roi: list) -> bool:
        """
        Save ROI coordinates to control.json configuration.
        This method is called when ROI updates are received from the OCR process.

        Args:
            roi: List of 4 integers [x1, y1, x2, y2]

        Returns:
            bool: True if saved successfully, False otherwise
        """
        try:
            if not roi or not isinstance(roi, list) or len(roi) != 4:
                self.logger.warning(f"Invalid ROI format for config save: {roi}")
                return False

            from utils.global_config import config as global_config_instance, save_global_config
            global_config_instance.ROI_COORDINATES = roi

            # Get IPC client from ApplicationCore for config change publishing
            ipc_client = None
            if self._ipc_client:
                ipc_client = self._ipc_client

            save_global_config(global_config_instance, ipc_client=ipc_client, change_source_override="gui_roi_update")
            self.logger.info(f"ROI saved to control.json: {roi}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to save ROI to control.json: {e}", exc_info=True)
            return False
    
    def _handle_dump_data(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle DUMP_DATA command."""
        # TODO: Implement data dump logic
        return CommandResult.info("DUMP_DATA command received - implementation pending")
    
    def _handle_get_global_configuration(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle GET_GLOBAL_CONFIGURATION command."""
        try:
            from utils.global_config import config
            # Convert config to dict (simplified - you might want to be more selective)
            config_data = {
                "initial_share_size": config.initial_share_size,
                "add_type": config.add_type,
                "reduce_percentage": config.reduce_percentage,
                # Add more config fields as needed
            }
            return CommandResult.success("Configuration retrieved", data=config_data)
        except Exception as e:
            return CommandResult.error(f"Failed to get configuration: {e}")
    
    def _handle_reset_to_default_config(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle RESET_TO_DEFAULT_CONFIG command."""
        # TODO: Implement config reset logic
        return CommandResult.info("RESET_TO_DEFAULT_CONFIG command received - implementation pending")

    def _handle_get_ipc_client_status(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle GET_IPC_CLIENT_STATUS command - returns comprehensive IPC buffer statistics.

        This provides detailed visibility into BulletproofIPCClient operations including:
        - Enhanced statistics with graduated defense tracking
        - Session-level aggregated counters
        - Channel health and clogged status
        - Mmap buffer utilization and performance metrics

        Parameters can include:
        - 'format': 'simple' for raw stats only, 'comprehensive' (default) for full data
        - 'include_notifier_stats': boolean to include/exclude notifier statistics
        """
        try:
            # Check if BulletproofIPCClient is available
            if not self._ipc_client:
                self.logger.warning("GET_IPC_CLIENT_STATUS: BulletproofIPCClient not available via DI.")
                return CommandResult.error("IPC client (BulletproofIPCClient) is not available or not initialized.")

            # Get format preference from parameters
            format_type = parameters.get('format', 'comprehensive')
            include_notifier_stats = parameters.get('include_notifier_stats', True)

            # Get core IPC buffer statistics
            ipc_stats = self._ipc_client.get_ipc_buffer_stats()

            # For simple format, return just the raw statistics
            if format_type == 'simple':
                self.logger.debug(f"GET_IPC_CLIENT_STATUS: Returning simple format with {len(ipc_stats)} metrics")
                return CommandResult.success("IPC client status retrieved successfully.", data=ipc_stats)

            # For comprehensive format, include additional context
            is_healthy = self._ipc_client.is_healthy()

            # Get mission control notifier stats if requested and available
            # TODO: Inject mission control notifier service via DI
            notifier_stats = {}
            # if include_notifier_stats and self._mission_control_notifier:
            #     if hasattr(self._mission_control_notifier, 'get_notifier_stats'):
            #         notifier_stats = self._mission_control_notifier.get_notifier_stats()

            # Compile comprehensive status data
            status_data = {
                "ipc_buffer_stats": ipc_stats,
                "is_healthy": is_healthy,
                "mission_control_notifier_stats": notifier_stats,
                "timestamp": time.time(),
                "format": format_type
            }

            # Determine overall status for user-friendly response
            overall_status = "healthy"
            if not is_healthy:
                overall_status = "unhealthy"
            elif ipc_stats.get("overall_publisher_health") == "DEGRADED":
                overall_status = "degraded"

            self.logger.info(f"GET_IPC_CLIENT_STATUS: Retrieved comprehensive status - {overall_status}")
            return CommandResult.success(
                f"IPC Client Status: {overall_status}",
                data=status_data
            )

        except Exception as e:
            self.logger.error(f"Error getting IPC client stats: {e}", exc_info=True)
            return CommandResult.error(f"Error retrieving IPC client stats: {e}")

    def _handle_debug_positions(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle DEBUG_POSITIONS command - debug Position Manager state."""
        try:
            if self._position_manager:
                self._position_manager.debug_all_positions()
                return CommandResult.success("Position Manager debug logged - check TESTRADE logs")
            else:
                return CommandResult.error("Position Manager not available")
        except Exception as e:
            return CommandResult.error(f"Failed to debug positions: {e}")

    def _handle_test_function(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle TEST_FUNCTION command - run test function with Position Manager debug."""
        try:
            from utils.test_function import test_function
            test_function(app_core=self.app_core)
            return CommandResult.success("Test function executed - check TESTRADE logs for Position Manager debug")
        except Exception as e:
            return CommandResult.error(f"Failed to execute test function: {e}")

    def _handle_update_recording_settings(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle UPDATE_RECORDING_SETTINGS command."""
        try:
            enable_image_recording = parameters.get('enable_image_recording', False)
            enable_raw_ocr_recording = parameters.get('enable_raw_ocr_recording', True)
            enable_intellisense_logging = parameters.get('enable_intellisense_logging', False)
            enable_observability_logging = parameters.get('enable_observability_logging', False)

            # Update global config recording flags
            from utils.global_config import config
            config.enable_image_recording = enable_image_recording
            config.enable_raw_ocr_recording = enable_raw_ocr_recording
            config.enable_intellisense_logging = enable_intellisense_logging
            config.enable_observability_logging = enable_observability_logging

            self.logger.info(f"Recording settings updated: Images={enable_image_recording}, "
                           f"RawOCR={enable_raw_ocr_recording}, IntelliSense={enable_intellisense_logging}")

            # Calculate estimated memory impact
            memory_impact = "Low"
            estimated_size = "~1-5KB per event"

            if enable_image_recording:
                memory_impact = "High"
                estimated_size = "~50-200KB per frame (Base64 PNG images)"
            elif enable_raw_ocr_recording and enable_intellisense_logging:
                memory_impact = "Medium"
                estimated_size = "~5-20KB per event"

            return CommandResult.success(
                f"Recording settings updated. Memory impact: {memory_impact}",
                data={
                    "enable_image_recording": enable_image_recording,
                    "enable_raw_ocr_recording": enable_raw_ocr_recording,
                    "enable_intellisense_logging": enable_intellisense_logging,
                    "memory_impact": memory_impact,
                    "estimated_size": estimated_size
                }
            )
        except Exception as e:
            return CommandResult.error(f"Error updating recording settings: {e}")

    def _handle_update_logging_settings(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle UPDATE_LOGGING_SETTINGS command."""
        try:
            log_level = parameters.get('log_level', 'INFO')
            enable_ocr_debug_logging = parameters.get('enable_ocr_debug_logging', False)

            # Update global config logging settings
            from utils.global_config import config, update_config_key
            config.log_level = log_level
            config.enable_ocr_debug_logging = enable_ocr_debug_logging

            # Also update babysitter log level to match main log level
            if hasattr(config, 'babysitter_log_level'):
                config.babysitter_log_level = log_level

            # Get IPC client from ApplicationCore for config change publishing
            ipc_client = None
            if self._ipc_client:
                ipc_client = self._ipc_client

            # Persist both log levels to control.json
            update_config_key('log_level', log_level, ipc_client=ipc_client, change_source_override="gui_logging_settings")
            update_config_key('enable_ocr_debug_logging', enable_ocr_debug_logging, ipc_client=ipc_client, change_source_override="gui_logging_settings")
            update_config_key('babysitter_log_level', log_level, ipc_client=ipc_client, change_source_override="gui_logging_settings")

            # Update Python logging level
            import logging
            numeric_level = getattr(logging, log_level.upper(), logging.INFO)

            # Update root logger
            root_logger = logging.getLogger()
            root_logger.setLevel(numeric_level)

            # Update specific loggers that generate high volume
            high_volume_loggers = [
                'modules.ocr.ocr_service',
                'modules.ocr.ocr_process_main',
                'modules.trade_management.ocr_scalping_signal_orchestrator_service',
                'core.event_bus',
                'modules.ocr.ocr_data_conditioning_service'
            ]

            for logger_name in high_volume_loggers:
                logger = logging.getLogger(logger_name)
                if enable_ocr_debug_logging:
                    logger.setLevel(logging.DEBUG)
                else:
                    # Set to WARNING to reduce spam, unless user wants DEBUG globally
                    logger.setLevel(logging.WARNING if log_level != 'DEBUG' else logging.DEBUG)

            # Update system-wide loggers (Core, GUI Backend, Babysitter)
            system_loggers = [
                'RedisBabysitterService',  # Babysitter service logger
                'gui.gui_backend',  # GUI Backend logger (full module path)
                'gui_backend',  # GUI Backend logger (alternative name)
                '__main__',  # Main application logger
                'HeadlessCoreRunner',  # Headless core runner
                'core.application_core'  # Application core logger
            ]

            for logger_name in system_loggers:
                system_logger = logging.getLogger(logger_name)
                system_logger.setLevel(numeric_level)
                self.logger.info(f"Updated {logger_name} logger to {log_level} level")

            # Also update the root logger to ensure all loggers inherit the new level
            root_logger = logging.getLogger()
            root_logger.setLevel(numeric_level)
            self.logger.info(f"Updated root logger to {log_level} level")

            # Update GUI backend logger via direct API call
            try:
                import requests
                gui_backend_url = "http://localhost:8001/logging/update"
                response = requests.post(gui_backend_url, json={'log_level': log_level}, timeout=5)
                if response.status_code == 200:
                    self.logger.info(f"Successfully updated GUI backend logger to {log_level}")
                else:
                    self.logger.warning(f"Failed to update GUI backend logger: {response.status_code}")
            except Exception as e:
                self.logger.warning(f"Could not update GUI backend logger: {e}")

            self.logger.info(f"Logging settings updated: Level={log_level}, OCR Debug={enable_ocr_debug_logging}")

            # Calculate log file impact
            impact_level = "Low"
            if log_level == 'DEBUG' or enable_ocr_debug_logging:
                impact_level = "High (100MB+ log files possible)"
            elif log_level == 'INFO':
                impact_level = "Medium (10-50MB log files)"
            else:
                impact_level = "Low (<10MB log files)"

            return CommandResult.success(
                f"Logging settings updated. Log file impact: {impact_level}",
                data={
                    "log_level": log_level,
                    "enable_ocr_debug_logging": enable_ocr_debug_logging,
                    "impact_level": impact_level
                }
            )
        except Exception as e:
            return CommandResult.error(f"Error updating logging settings: {e}")

    def _handle_update_development_mode(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle UPDATE_DEVELOPMENT_MODE command."""
        try:
            development_mode = parameters.get('development_mode', True)

            # Update global config
            from utils.global_config import config
            config.development_mode = development_mode

            if development_mode:
                # Development mode: Reduce volume but keep functionality

                # 1. Reduce observability sampling (don't disable completely)
                config.observability_sample_rate = 0.1  # 10% of events

                # 2. Set shorter Redis TTL
                config.redis_stream_ttl_seconds = 1800  # 30 minutes

                # 3. Reduce log level for high-volume loggers
                import logging
                high_volume_loggers = [
                    'modules.ocr.ocr_service',
                    'modules.ocr.ocr_process_main',
                    'core.event_bus'
                ]
                for logger_name in high_volume_loggers:
                    logging.getLogger(logger_name).setLevel(logging.WARNING)

                # 4. Enable file rotation for observability
                config.observability_max_file_size_mb = 50  # Rotate at 50MB

                storage_impact = "Reduced (90% less storage)"

            else:
                # Full mode: Everything enabled
                config.observability_sample_rate = 1.0  # 100% of events
                config.redis_stream_ttl_seconds = 3600  # 1 hour
                config.observability_max_file_size_mb = 1000  # 1GB before rotation

                # Reset log levels to INFO
                import logging
                logging.getLogger().setLevel(logging.INFO)

                storage_impact = "Full (Large files possible)"

            self.logger.info(f"Development mode updated: {development_mode}, Storage impact: {storage_impact}")

            return CommandResult.success(
                f"Development mode {'enabled' if development_mode else 'disabled'}. Storage impact: {storage_impact}",
                data={
                    "development_mode": development_mode,
                    "storage_impact": storage_impact,
                    "observability_sample_rate": config.observability_sample_rate,
                    "redis_ttl_seconds": config.redis_stream_ttl_seconds
                }
            )
        except Exception as e:
            return CommandResult.error(f"Error updating development mode: {e}")

    # Trading Command Handlers

    def _handle_manual_trade(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle MANUAL_TRADE command - Execute manual trading actions."""
        try:
            action = parameters.get('action', 'add')
            shares = parameters.get('shares', None)
            symbol = parameters.get('symbol', None)

            self.logger.info(f"Processing manual trade: action={action}, shares={shares}, symbol={symbol}")

            # Get TradeLifecycleManager from DI
            if not self._trade_lifecycle_manager:
                return CommandResult.error("TradeLifecycleManager not available - check DI configuration")

            trade_manager = self._trade_lifecycle_manager

            if action.lower() == 'add':
                # Use global config for manual shares if not specified
                if shares is None:
                    from utils.global_config import config
                    shares = config.manual_shares
                
                # Call manual_add_shares method with proper parameters
                success = trade_manager.manual_add_shares(symbol=symbol, shares=shares)
                if success:
                    return CommandResult.success(f"Manual ADD executed: {shares} shares" + (f" for {symbol}" if symbol else ""))
                else:
                    return CommandResult.error("Manual ADD failed - check trade manager logs")
            
            elif action.lower() == 'reduce':
                # Call manual reduce method if available
                if hasattr(trade_manager, 'manual_reduce_shares'):
                    success = trade_manager.manual_reduce_shares(symbol=symbol, shares=shares)
                    if success:
                        return CommandResult.success(f"Manual REDUCE executed" + (f": {shares} shares" if shares else "") + (f" for {symbol}" if symbol else ""))
                    else:
                        return CommandResult.error("Manual REDUCE failed - check trade manager logs")
                else:
                    return CommandResult.error("Manual REDUCE not supported by current trade manager")
            
            else:
                return CommandResult.error(f"Unsupported manual trade action: {action}. Supported: 'add', 'reduce'")

        except Exception as e:
            self.logger.error(f"Exception in manual trade: {e}", exc_info=True)
            return CommandResult.error(f"Error executing manual trade: {e}")

    def _handle_force_close_all(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle FORCE_CLOSE_ALL command - Force close all open positions."""
        try:
            reason = parameters.get('reason', 'Manual GUI Force Close')
            halt_system = parameters.get('halt_system', True)  # Option to halt system after close

            self.logger.warning(f"Processing FORCE CLOSE ALL command: {reason}, halt_system={halt_system}")

            # Get TradeLifecycleManager from DI
            if not self._trade_lifecycle_manager:
                return CommandResult.error("TradeLifecycleManager not available - check DI configuration")

            trade_manager = self._trade_lifecycle_manager

            if halt_system:
                # Use the comprehensive emergency shutdown method
                success = trade_manager.force_close_all_and_halt_system(reason=reason)
                action_desc = "Force close all positions and halt system"
            else:
                # Just force close positions without halting
                if hasattr(trade_manager, 'force_close_all_positions'):
                    success = trade_manager.force_close_all_positions(reason=reason)
                    action_desc = "Force close all positions"
                else:
                    # Fallback to halt system method
                    success = trade_manager.force_close_all_and_halt_system(reason=reason)
                    action_desc = "Force close all positions and halt system (no separate close method)"

            if success:
                return CommandResult.success(f"{action_desc} executed successfully: {reason}")
            else:
                return CommandResult.error(f"{action_desc} failed - check trade manager logs")

        except Exception as e:
            self.logger.error(f"Exception in force close all: {e}", exc_info=True)
            return CommandResult.error(f"Error executing force close all: {e}")

    def _handle_emergency_stop(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle EMERGENCY_STOP command - Complete system emergency shutdown."""
        try:
            reason = parameters.get('reason', 'Emergency Stop Button')
            force_immediate = parameters.get('force_immediate', True)

            self.logger.critical(f"🚨 EMERGENCY STOP INITIATED: {reason} (force_immediate={force_immediate})")

            emergency_results = []

            # 1. First try to stop trading via TradeManager
            trade_stop_success = False
            if self._trade_lifecycle_manager:
                try:
                    trade_manager = self._trade_lifecycle_manager
                    trade_stop_success = trade_manager.force_close_all_and_halt_system(reason=f"EMERGENCY STOP: {reason}")
                    result_msg = f"Trade manager emergency stop: {'✅ SUCCESS' if trade_stop_success else '❌ FAILED'}"
                    emergency_results.append(result_msg)
                    self.logger.critical(result_msg)
                except Exception as e:
                    error_msg = f"❌ Trade manager emergency stop failed: {e}"
                    emergency_results.append(error_msg)
                    self.logger.error(error_msg)
            else:
                warning_msg = "⚠️ Trade manager not available for emergency stop"
                emergency_results.append(warning_msg)
                self.logger.warning(warning_msg)

            # 2. Stop OCR process via Babysitter
            ocr_stop_success = False
            try:
                ocr_command_id = self._send_ocr_command_via_ipc("EMERGENCY_STOP", {"reason": reason})
                if ocr_command_id:
                    ocr_stop_success = True
                    result_msg = f"✅ OCR emergency stop command sent (ID: {ocr_command_id})"
                else:
                    result_msg = "❌ Failed to send OCR emergency stop command"
                emergency_results.append(result_msg)
                self.logger.critical(result_msg)
            except Exception as e:
                error_msg = f"❌ OCR emergency stop failed: {e}"
                emergency_results.append(error_msg)
                self.logger.error(error_msg)

            # 3. Stop ApplicationCore
            core_stop_success = False
            if hasattr(self.app_core, 'emergency_stop'):
                try:
                    self.app_core.emergency_stop()
                    core_stop_success = True
                    result_msg = "✅ ApplicationCore emergency stop completed"
                    emergency_results.append(result_msg)
                    self.logger.critical(result_msg)
                except Exception as e:
                    error_msg = f"❌ ApplicationCore emergency stop failed: {e}"
                    emergency_results.append(error_msg)
                    self.logger.error(error_msg)
            else:
                # Fallback to regular stop
                try:
                    self.app_core.stop()
                    core_stop_success = True
                    result_msg = "✅ ApplicationCore regular stop completed (emergency_stop not available)"
                    emergency_results.append(result_msg)
                    self.logger.critical(result_msg)
                except Exception as e:
                    error_msg = f"❌ ApplicationCore regular stop failed: {e}"
                    emergency_results.append(error_msg)
                    self.logger.error(error_msg)

            # 4. Determine overall success and return detailed results
            success_count = sum([trade_stop_success, ocr_stop_success, core_stop_success])
            total_operations = 3

            if success_count == total_operations:
                return CommandResult.success(f"🚨 Emergency stop executed successfully: {reason}", 
                                           data={"emergency_results": emergency_results, "success_count": success_count})
            elif success_count > 0:
                return CommandResult.warning(f"🚨 Emergency stop partially successful ({success_count}/{total_operations}): {reason}", 
                                           data={"emergency_results": emergency_results, "success_count": success_count})
            else:
                return CommandResult.error(f"🚨 Emergency stop failed completely: {reason}", 
                                         data={"emergency_results": emergency_results, "success_count": success_count})

        except Exception as e:
            self.logger.critical(f"🚨 CRITICAL ERROR during emergency stop: {e}", exc_info=True)
            return CommandResult.error(f"Critical error executing emergency stop: {e}")

    def _send_ocr_status_update(self, status_event: str):
        """Send OCR status update to GUI via direct HTTP call."""
        try:
            # Send status update directly to GUI backend
            import requests

            status_data = {
                "message_type": "core_status_update",
                "status_event": status_event,
                "timestamp": time.time(),
                "component": "OCR_SERVICE"
            }

            # Send to GUI backend's WebSocket broadcast endpoint
            gui_backend_url = "http://localhost:8001/broadcast/status"
            response = requests.post(gui_backend_url, json=status_data, timeout=2)

            if response.status_code == 200:
                self.logger.info(f"Sent OCR status update '{status_event}' to GUI via direct HTTP")
            else:
                self.logger.warning(f"Failed to send OCR status update '{status_event}': HTTP {response.status_code}")

        except Exception as e:
            self.logger.error(f"Error sending OCR status update '{status_event}': {e}", exc_info=True)

    def _handle_get_all_positions(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle GET_ALL_POSITIONS command - request current positions from broker via DI container."""
        try:
            # Resolve broker service from DI container
            broker_service = self._broker_service

            if not broker_service:
                return CommandResult.error("Broker service not available in DI container")

            if not broker_service.is_connected():
                return CommandResult.error("Broker service not connected")

            # Request positions from broker - this will trigger position events through the event bus
            broker_service.request_open_positions()
            self.logger.info("Position request sent to broker via DI container")

            return CommandResult.success("Position request sent to broker - data will flow through event streams")

        except Exception as e:
            self.logger.error(f"Failed to request positions via DI container: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get positions: {e}")

    def _handle_get_account_summary(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle GET_ACCOUNT_SUMMARY command - request account data from broker via DI container."""
        try:
            # Resolve broker service from DI container
            broker_service = self._broker_service

            if not broker_service:
                return CommandResult.error("Broker service not available in DI container")

            if not broker_service.is_connected():
                return CommandResult.error("Broker service not connected")

            # Request account summary from broker
            broker_service.request_account_summary()
            self.logger.info("Account summary request sent to broker via DI container")

            return CommandResult.success("Account summary request sent to broker - data will flow through event streams")

        except Exception as e:
            self.logger.error(f"Failed to request account summary via DI container: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get account summary: {e}")

    def _handle_get_all_orders(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle GET_ALL_ORDERS command - request current orders from broker via DI container."""
        try:
            # Resolve broker service from DI container
            broker_service = self._broker_service

            if not broker_service:
                return CommandResult.error("Broker service not available in DI container")

            if not broker_service.is_connected():
                return CommandResult.error("Broker service not connected")

            # For orders, we need to check if the broker has a method to request all orders
            # The Lightspeed broker has get_all_positions but may not have request_all_orders
            # Let's check if the method exists
            if hasattr(broker_service, 'request_all_orders'):
                broker_service.request_all_orders()
                self.logger.info("Orders request sent to broker via DI container")
                return CommandResult.success("Orders request sent to broker - data will flow through event streams")
            else:
                # Fallback: request orders via the C++ bridge directly
                if hasattr(broker_service, 'client') and broker_service.client:
                    req = {"cmd": "get_orders_detailed"}
                    if broker_service.client.send_message_no_read(req):
                        self.logger.info("Orders request sent to broker C++ bridge via DI container")
                        return CommandResult.success("Orders request sent to broker - data will flow through event streams")
                    else:
                        return CommandResult.error("Failed to send orders request to broker C++ bridge")
                else:
                    return CommandResult.error("Broker client not available for orders request")

        except Exception as e:
            self.logger.error(f"Failed to request orders via DI container: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get orders: {e}")

    def _handle_get_position_summary(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle GET_POSITION_SUMMARY command - request position summary from Position Manager via DI container."""
        try:
            # Resolve position manager from DI container
            from interfaces.trading.services import IPositionManager
            position_manager = self._position_manager

            if not position_manager:
                return CommandResult.error("Position Manager not available in DI container")

            # Get position summary data
            summary_data = {
                "totalSharesTraded": 0,
                "openPositionsCount": 0,
                "closedPositionsCount": 0,
                "totalTradesCount": 0,
                "openPnL": 0.0,
                "closedPnL": 0.0,
                "totalDayPnL": 0.0,
                "winRate": 0.0,
                "avgWin": 0.0,
                "avgLoss": 0.0,
                "totalVolume": 0,
                "winners": 0,
                "losers": 0,
                "timestamp": time.time()
            }

            # Get actual data from position manager if available
            if hasattr(position_manager, 'get_position_summary'):
                actual_summary = position_manager.get_position_summary()
                if actual_summary:
                    summary_data.update(actual_summary)

            # Publish enhanced position summary to Redis stream
            self._publish_enhanced_position_summary(summary_data)

            self.logger.info("Position summary request processed and published to Redis stream")
            return CommandResult.success("Position summary data published to Redis stream", data=summary_data)

        except Exception as e:
            self.logger.error(f"Failed to get position summary via DI container: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get position summary: {e}")

    def _handle_get_trade_history_obsolete(self, parameters: Dict[str, Any]) -> CommandResult:
        """OBSOLETE: GET_TRADE_HISTORY command - replaced by REQUEST_INITIAL_STATE bootstrap pattern."""
        return CommandResult.error(
            "GET_TRADE_HISTORY command is obsolete. Use 'request_initial_state' command instead. "
            "Trade history data now comes via testrade:trade-history Redis stream.",
            command_id=parameters.get("command_id")
        )

    def _publish_enhanced_position_summary(self, summary_data: Dict[str, Any]):
        """Publish enhanced position summary to Redis stream via Babysitter."""
        try:
            import time

            # Check if IPC data dumping is disabled
            if self._config_service and not self._config_service.is_feature_enabled('ENABLE_IPC_DATA_DUMP'):
                self.logger.debug("TANK_MODE: Skipping IPC send for enhanced position summary - ENABLE_IPC_DATA_DUMP is False")
                return
            
            # Use correlation logger if available
            if self._correlation_logger:
                telemetry_data = {
                    "payload": summary_data,
                    "correlation_id": f"position_summary_enhanced_{int(time.time() * 1000)}",
                    "causation_id": None
                }
                
                self._correlation_logger.log_event(
                    source_component="GuiCommandService",
                    event_name="PositionSummaryEnhanced",
                    payload=telemetry_data
                )
                
                self.logger.info("Enhanced position summary published via correlation logger")
            else:
                self.logger.warning("Correlation logger not available for position summary publishing")

        except Exception as e:
            self.logger.error(f"Error publishing enhanced position summary: {e}", exc_info=True)



    def _handle_obsolete_data_request(self, parameters: Dict[str, Any]) -> CommandResult:
        """Handle obsolete data request commands that are now replaced by bootstrap pattern."""
        # Extract command type from the call stack or parameters if available
        import inspect
        frame = inspect.currentframe()
        try:
            # Get the calling method name to determine which obsolete command was called
            caller_frame = frame.f_back.f_back  # Go up two frames to get the actual command handler call
            command_type = "UNKNOWN"
            if caller_frame and 'command_type' in caller_frame.f_locals:
                command_type = caller_frame.f_locals['command_type']
        finally:
            del frame

        error_msg = f"Command '{command_type}' is obsolete. GUI now uses REQUEST_INITIAL_STATE and consumes Redis streams for data."
        self.logger.warning(f"Core: {error_msg}")
        return CommandResult.error(error_msg)

    def _handle_request_initial_state(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        🚨 CHUNK 12: Handle REQUEST_INITIAL_STATE command - OPTION A: Simple Acknowledgment.

        With Bootstrap API enabled, the GUI frontend uses HTTP Bootstrap APIs for initial data loading.
        REQUEST_INITIAL_STATE is now just an acknowledgment that the system is ready.

        This eliminates the "unknown" correlated messages on testrade:responses:to_gui.
        NO OTHER DATA IS PUBLISHED DIRECTLY BY THIS HANDLER TO testrade:responses:to_gui.
        """
        command_id = parameters.get('command_id', 'unknown')
        self.logger.info(f"Core: Handling REQUEST_INITIAL_STATE (CmdID: {command_id}). Bootstrap API mode - returning acknowledgment.")

        # 🚨 SPEC COMPLIANCE: Check if Bootstrap APIs are actually available
        bootstrap_apis_available = True
        services_available = []
        services_unavailable = []

        # Check PositionManager for Bootstrap API
        try:
            from interfaces.trading.services import IPositionManager
            position_manager = self._position_manager
            if position_manager and hasattr(position_manager, 'get_all_positions_for_gui_bootstrap'):
                services_available.append("positions")
            else:
                services_unavailable.append("positions")
                bootstrap_apis_available = False
        except Exception:
            services_unavailable.append("positions")
            bootstrap_apis_available = False

        # Check OrderRepository for Bootstrap APIs
        try:
            from core.service_interfaces import IOrderRepository
            order_repository = self._order_repository
            if order_repository:
                if hasattr(order_repository, 'get_account_summary_for_gui_bootstrap'):
                    services_available.append("account")
                else:
                    services_unavailable.append("account")
                    bootstrap_apis_available = False

                if hasattr(order_repository, 'get_orders_for_gui_bootstrap'):
                    services_available.append("orders")
                else:
                    services_unavailable.append("orders")
                    bootstrap_apis_available = False
            else:
                services_unavailable.extend(["account", "orders"])
                bootstrap_apis_available = False
        except Exception:
            services_unavailable.extend(["account", "orders"])
            bootstrap_apis_available = False

        # Check broker connection for additional context
        broker_service = getattr(self.app_core, 'broker', None) or getattr(self.app_core, 'broker_bridge', None)
        broker_connected = False
        if broker_service:
            try:
                broker_connected = hasattr(broker_service, 'is_connected') and broker_service.is_connected()
            except Exception:
                pass

        # 🚨 SPEC COMPLIANCE: OPTION A - Simple Acknowledgment Response
        self.logger.info(f"REQUEST_INITIAL_STATE: Acknowledged. GUI to use Bootstrap APIs. (CmdID: {command_id})")

        response_data = {
            "status_message": "Initial state request acknowledged by ApplicationCore. GUI should use dedicated Bootstrap APIs for data.",
            "bootstrap_apis_available": bootstrap_apis_available,  # 🚨 SPEC: Indicate APIs are the way
            "bootstrap_apis_ready": bootstrap_apis_available,      # 🚨 SPEC: Check if services are actually ready
            "bootstrap_correlation_id": command_id,               # 🚨 SPEC: Echo back for consistency
            "available_services": services_available,
            "unavailable_services": services_unavailable,
            "broker_connected": broker_connected,
            "bootstrap_mode": "http_api"
        }

        if bootstrap_apis_available:
            status_message = f"Initial state request processed; APIs are primary source. Available: {', '.join(services_available)}."
            return CommandResult.success(
                message=status_message,
                data=response_data
            )
        else:
            status_message = f"Bootstrap APIs partially available. Missing: {', '.join(services_unavailable)}."
            return CommandResult.warning(
                message=status_message,
                data=response_data
            )

    # === NEW BOOTSTRAP API COMMAND HANDLERS ===

    def _handle_get_all_positions_bootstrap(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle GET_ALL_POSITIONS_BOOTSTRAP command for GUI Bootstrap API.
        Returns current positions data directly in the command response.
        """
        command_id = parameters.get('command_id', 'unknown')
        try:
            self.logger.info(f"Core: Processing GET_ALL_POSITIONS_BOOTSTRAP command (CmdID: {command_id})")

            # 🚨 SPEC COMPLIANCE: Use DI container resolution as specified
            try:
                from interfaces.trading.services import IPositionManager
                position_manager = self._position_manager
            except Exception as e:
                self.logger.error(f"GET_ALL_POSITIONS_BOOTSTRAP: PositionManager not resolved from DI container: {e}")
                return CommandResult.error("Internal error: PositionManager service not available.")

            if not position_manager:
                self.logger.error("GET_ALL_POSITIONS_BOOTSTRAP: PositionManager not resolved.")
                return CommandResult.error("Internal error: PositionManager service not available.")

            # 🚨 SPEC COMPLIANCE: Check for method existence as specified
            if not hasattr(position_manager, 'get_all_positions_for_gui_bootstrap'):
                self.logger.error("GET_ALL_POSITIONS_BOOTSTRAP: PositionManager missing 'get_all_positions_for_gui_bootstrap' method.")
                return CommandResult.error("Internal error: Position data retrieval method not implemented in PositionManager.")

            # Call the Bootstrap-specific method with parameters
            positions_data = position_manager.get_all_positions_for_gui_bootstrap(
                include_market_data=parameters.get("include_market_data", True)
            )

            # Check if the method returned an error
            if "error" in positions_data:
                return CommandResult.error(f"Failed to get positions: {positions_data['error']}")

            # The new method returns properly formatted data, so we can use it directly
            # But we still need to add child records from OrderRepository if available
            formatted_positions_for_bootstrap = positions_data.get("positions", [])

            # Add child records for each position if OrderRepository is available
            for position_record in formatted_positions_for_bootstrap.copy():  # Use copy to avoid modification during iteration
                symbol = position_record.get("symbol")
                lcm_trade_id = position_record.get("lcm_trade_id")
                gui_display_trade_id = position_record.get("trade_id")

                # 🚨 DEBUG: Log YIBO position data to see what's missing
                if symbol == "YIBO":
                    self.logger.info(f"🚨 YIBO DEBUG: Position record keys: {list(position_record.keys())}")
                    self.logger.info(f"🚨 YIBO DEBUG: lcm_trade_id = {lcm_trade_id}")
                    self.logger.info(f"🚨 YIBO DEBUG: trade_id = {gui_display_trade_id}")
                    self.logger.info(f"🚨 YIBO DEBUG: quantity = {position_record.get('quantity')}")

                # 🚨 HIERARCHY FIX: Add child records if we have order data
                # Use injected OrderRepository instead of accessing through ApplicationCore
                if lcm_trade_id and self._order_repository:
                    try:
                        orders_for_trade = self._order_repository.get_orders_for_trade(lcm_trade_id)
                        self.logger.info(f"🚨 BOOTSTRAP HIERARCHY: Found {len(orders_for_trade)} orders for trade {lcm_trade_id} ({symbol})")

                        for order_data in orders_for_trade:
                            child_record = {
                                "symbol": symbol,
                                "is_open": False,
                                "quantity": order_data.get("requested_shares", 0.0),
                                "average_price": order_data.get("avg_fill_price", 0.0) or order_data.get("requested_lmt_price", 0.0),
                                "realized_pnl": 0.0,
                                "unrealized_pnl": 0.0,
                                "last_update_timestamp": order_data.get("timestamp_requested", time.time()),
                                "strategy": position_record.get("strategy", "UNKNOWN"),

                                # Market data
                                "bid_price": None,
                                "ask_price": None,
                                "last_price": None,
                                "market_data_available": False,
                                "hierarchy_data_available": True,

                                # 🚨 HIERARCHY FIX: Child record
                                "is_parent": False,
                                "is_child": True,
                                "parent_trade_id": gui_display_trade_id,
                                "trade_id": f"{gui_display_trade_id}-{order_data.get('local_id', 'unknown')}",
                                "lcm_trade_id": lcm_trade_id,
                                "master_correlation_id": order_data.get("master_correlation_id", position_record.get("master_correlation_id")),

                                # Order-specific fields
                                "action": order_data.get("side", "UNKNOWN"),
                                "side": order_data.get("side", "UNKNOWN"),
                                "status": order_data.get("ls_status", "UNKNOWN"),
                                "price": order_data.get("avg_fill_price", 0.0) or order_data.get("requested_lmt_price", 0.0),
                                "timestamp": order_data.get("timestamp_requested", time.time()),
                                "local_order_id": order_data.get("local_id"),
                                "broker_order_id": order_data.get("ls_order_id")
                            }
                            formatted_positions_for_bootstrap.append(child_record)

                    except Exception as e:
                        self.logger.error(f"🚨 BOOTSTRAP HIERARCHY: Error creating child records for {symbol}: {e}", exc_info=True)

            # Update the response structure with the enhanced positions list
            positions_data["positions"] = formatted_positions_for_bootstrap
            positions_data["hierarchy_data_available"] = True  # We now have proper hierarchy

            # 🚨 SPEC COMPLIANCE: Ensure Bootstrap metadata is included
            positions_data["is_bootstrap"] = True
            positions_data["bootstrap_correlation_id"] = command_id  # command_id is the original API correlation_id

            # Return the data directly in the command response
            return CommandResult.success(
                message="Positions data retrieved successfully",
                data=positions_data  # This will be the "data" field in the response
            )

        except Exception as e:
            self.logger.error(f"Error in GET_ALL_POSITIONS_BOOTSTRAP: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get positions: {e}")

    def _handle_get_account_summary_bootstrap(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle GET_ACCOUNT_SUMMARY_BOOTSTRAP command for GUI Bootstrap API.
        Returns current account summary data directly in the command response.
        """
        command_id = parameters.get('command_id', 'unknown')
        try:
            self.logger.info(f"Core: Processing GET_ACCOUNT_SUMMARY_BOOTSTRAP command (CmdID: {command_id})")

            # 🚨 SPEC COMPLIANCE: Use DI container resolution as specified
            try:
                from core.service_interfaces import IOrderRepository
                order_repository = self._order_repository
            except Exception as e:
                self.logger.error(f"GET_ACCOUNT_SUMMARY_BOOTSTRAP: OrderRepository not resolved from DI container: {e}")
                return CommandResult.error("Internal error: OrderRepository service not available.")

            if not order_repository:
                self.logger.error("GET_ACCOUNT_SUMMARY_BOOTSTRAP: OrderRepository not resolved.")
                return CommandResult.error("Internal error: OrderRepository service not available.")

            # 🚨 SPEC COMPLIANCE: Check for method existence as specified
            if not hasattr(order_repository, 'get_account_summary_for_gui_bootstrap'):
                self.logger.error("GET_ACCOUNT_SUMMARY_BOOTSTRAP: OrderRepository missing 'get_account_summary_for_gui_bootstrap' method.")
                return CommandResult.error("Internal error: Account summary retrieval method not implemented in OrderRepository.")

            # Get account summary from OrderRepository
            account_data = order_repository.get_account_summary_for_gui_bootstrap()

            # Check if the method returned an error
            if "error" in account_data:
                return CommandResult.error(f"Failed to get account summary: {account_data['error']}")

            # Ensure the response is a dictionary
            if not isinstance(account_data, dict):
                return CommandResult.error("Account summary from repository was not a dictionary")

            # 🚨 SPEC COMPLIANCE: Ensure Bootstrap metadata is included
            account_data["is_bootstrap"] = True
            account_data["bootstrap_correlation_id"] = command_id

            self.logger.info(f"Account summary retrieved - connected: {account_data.get('broker_connected', False)}, "
                           f"account_value: {account_data.get('account_value', 0.0)}")

            return CommandResult.success(
                message="Account summary retrieved successfully",
                data=account_data
            )

        except Exception as e:
            self.logger.error(f"Error in GET_ACCOUNT_SUMMARY_BOOTSTRAP: {e}", exc_info=True)
            # Check for specific error types
            if "BrokerBridge not available" in str(e) or "Broker not available" in str(e):
                return CommandResult.error("Broker not available for account summary")
            return CommandResult.error(f"Failed to get account summary: {str(e)}")

    def _handle_get_orders_today_bootstrap(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle GET_ORDERS_TODAY_BOOTSTRAP command for GUI Bootstrap API.
        Returns today's orders data directly in the command response.
        """
        command_id = parameters.get('command_id', 'unknown')
        try:
            self.logger.info(f"Core: Processing GET_ORDERS_TODAY_BOOTSTRAP command (CmdID: {command_id})")

            # 🚨 SPEC COMPLIANCE: Use DI container resolution as specified
            try:
                from core.service_interfaces import IOrderRepository
                order_repository = self._order_repository
            except Exception as e:
                self.logger.error(f"GET_ORDERS_TODAY_BOOTSTRAP: OrderRepository not resolved from DI container: {e}")
                return CommandResult.error("Internal error: OrderRepository service not available.")

            if not order_repository:
                self.logger.error("GET_ORDERS_TODAY_BOOTSTRAP: OrderRepository not resolved.")
                return CommandResult.error("Internal error: OrderRepository service not available.")

            # 🚨 SPEC COMPLIANCE: Check for method existence as specified
            if not hasattr(order_repository, 'get_orders_for_gui_bootstrap'):
                self.logger.error("GET_ORDERS_TODAY_BOOTSTRAP: OrderRepository missing 'get_orders_for_gui_bootstrap' method.")
                return CommandResult.error("Internal error: Order retrieval method not implemented in OrderRepository.")

            # Get filter date from parameters
            filter_date = parameters.get("filter_date", "today")

            # Get Order objects from repository
            orders_list_objects = order_repository.get_orders_for_gui_bootstrap(filter_date=filter_date)
            self.logger.info(f"Retrieved {len(orders_list_objects)} Order objects from OrderRepository")

            # 🚨 CRITICAL FIX: Convert Order objects to dictionaries for JSON serialization
            serializable_orders = []
            for order_obj in orders_list_objects:
                try:
                    # Convert each Order object to a serializable dictionary
                    # This is the CRITICAL FIX for the SerializationError
                    order_dict = {
                        "order_id": str(order_obj.local_id),  # Use local_id as primary order_id
                        "local_order_id": str(order_obj.local_id),
                        "ls_order_id": str(order_obj.ls_order_id) if order_obj.ls_order_id else None,
                        "parent_trade_id": str(order_obj.parent_trade_id) if order_obj.parent_trade_id else None,
                        "symbol": order_obj.symbol,
                        "side": str(order_obj.side.value) if hasattr(order_obj.side, 'value') else str(order_obj.side),
                        "event_type": str(order_obj.event_type.value) if hasattr(order_obj.event_type, 'value') else str(order_obj.event_type),
                        "quantity": float(order_obj.requested_shares),
                        "requested_shares": float(order_obj.requested_shares),
                        "price": float(order_obj.requested_lmt_price) if order_obj.requested_lmt_price else None,
                        "requested_lmt_price": float(order_obj.requested_lmt_price) if order_obj.requested_lmt_price else None,
                        "status": str(order_obj.ls_status.value) if hasattr(order_obj.ls_status, 'value') else str(order_obj.ls_status),
                        "ls_status": str(order_obj.ls_status.value) if hasattr(order_obj.ls_status, 'value') else str(order_obj.ls_status),
                        "order_type": str(order_obj.order_type),
                        "timestamp": float(order_obj.timestamp_requested),
                        "timestamp_requested": float(order_obj.timestamp_requested),
                        "timestamp_broker": float(order_obj.timestamp_broker) if order_obj.timestamp_broker else None,
                        "timestamp_sent": float(order_obj.timestamp_sent) if order_obj.timestamp_sent else None,
                        "filled_quantity": float(order_obj.filled_quantity),
                        "leftover_shares": float(order_obj.leftover_shares),
                        "avg_fill_price": float(order_obj.fills[0].fill_price) if order_obj.fills else None,
                        "remaining_quantity": float(order_obj.leftover_shares),
                        "master_correlation_id": order_obj.master_correlation_id,
                        "comment": order_obj.comment or "",
                        "rejection_reason": order_obj.rejection_reason or "",
                        "cancel_reason": order_obj.cancel_reason or "",
                        "message": order_obj.comment or "",
                        "is_child": order_obj.parent_trade_id is not None,  # Important for hierarchy
                        "fills_count": len(order_obj.fills),
                        "version": int(order_obj.version)
                    }
                    serializable_orders.append(order_dict)

                except Exception as e:
                    self.logger.error(f"Error serializing order {getattr(order_obj, 'local_id', 'unknown')}: {e}", exc_info=True)
                    # Continue with other orders even if one fails

            self.logger.info(f"🚨 SERIALIZATION: Converted {len(serializable_orders)} Order objects to dictionaries")

            response_data = {
                "orders": serializable_orders,  # Now contains serializable dictionaries
                "total_orders": len(serializable_orders),
                "is_bootstrap": True,
                "bootstrap_correlation_id": command_id,  # 🚨 SPEC COMPLIANCE: Add bootstrap correlation ID
                "timestamp": time.time(),
                "repository_available": True
            }

            return CommandResult.success(
                message="Today's orders retrieved successfully",
                data=response_data
            )

        except Exception as e:
            self.logger.error(f"Error in GET_ORDERS_TODAY_BOOTSTRAP: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get today's orders: {str(e)}")

    def _handle_get_market_status_bootstrap(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        Handle GET_MARKET_STATUS_BOOTSTRAP command for GUI Bootstrap API.
        Returns current market status data directly in the command response.
        """
        command_id = parameters.get('command_id', 'unknown')
        try:
            self.logger.info(f"Core: Processing GET_MARKET_STATUS_BOOTSTRAP command (CmdID: {command_id})")

            # Initialize market status with defaults
            market_status = {
                "market_open": True,  # Will be determined by actual logic
                "data_sources_healthy": False,
                "connected_exchanges": [],
                "broker_connected": False,
                "price_data_available": False,
                "last_data_update": None,
                "timestamp": time.time(),
                "is_bootstrap": True,
                "bootstrap_correlation_id": command_id  # 🚨 SPEC COMPLIANCE: Add bootstrap correlation ID
            }

            # Check broker connection status
            broker_service = getattr(self.app_core, 'broker', None) or getattr(self.app_core, 'broker_bridge', None)
            if broker_service:
                try:
                    if hasattr(broker_service, 'is_connected') and broker_service.is_connected():
                        market_status["broker_connected"] = True
                        market_status["data_sources_healthy"] = True

                        # Add connected exchanges if broker provides this info
                        if hasattr(broker_service, 'connected_exchanges'):
                            market_status["connected_exchanges"] = getattr(broker_service, 'connected_exchanges', [])
                        else:
                            # Default exchanges for Lightspeed
                            market_status["connected_exchanges"] = ["XNYS", "XNAS", "ARCX"]

                        self.logger.info("Market status: Broker connected and healthy")
                    else:
                        self.logger.warning("Market status: Broker not connected")

                except Exception as e:
                    self.logger.error(f"Error checking broker connection status: {e}")

            # Check price data availability
            price_provider = getattr(self.app_core, 'price_provider', None)
            if price_provider:
                try:
                    # Check if price provider has recent data
                    if hasattr(price_provider, 'get_last_update_time'):
                        last_update = price_provider.get_last_update_time()
                        if last_update:
                            market_status["last_data_update"] = last_update
                            # Consider data fresh if updated within last 5 minutes
                            if time.time() - last_update < 300:
                                market_status["price_data_available"] = True

                    elif hasattr(price_provider, 'is_data_available'):
                        market_status["price_data_available"] = price_provider.is_data_available()
                    else:
                        # Assume price data is available if price provider exists
                        market_status["price_data_available"] = True

                    self.logger.info(f"Market status: Price data available = {market_status['price_data_available']}")

                except Exception as e:
                    self.logger.error(f"Error checking price data availability: {e}")

            # Determine overall market status
            if market_status["broker_connected"] or market_status["price_data_available"]:
                market_status["data_sources_healthy"] = True

            # Enhanced market hours check (basic implementation)
            try:
                import datetime
                now = datetime.datetime.now()
                # Basic US market hours check (9:30 AM - 4:00 PM ET, Monday-Friday)
                # This is a simplified check - real implementation would handle holidays, etc.
                if now.weekday() < 5:  # Monday = 0, Friday = 4
                    market_open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
                    market_close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
                    market_status["market_open"] = market_open_time <= now <= market_close_time
                else:
                    market_status["market_open"] = False

                self.logger.info(f"Market status: Market open = {market_status['market_open']} (basic hours check)")

            except Exception as e:
                self.logger.error(f"Error determining market hours: {e}")
                # Keep default True if we can't determine

            # Add service availability summary
            market_status["services_summary"] = {
                "broker_service": broker_service is not None,
                "price_provider": price_provider is not None,
                "order_repository": self._order_repository is not None,
                "position_manager": self._position_manager is not None
            }

            self.logger.info(f"Market status prepared: open={market_status['market_open']}, "
                           f"healthy={market_status['data_sources_healthy']}, "
                           f"broker={market_status['broker_connected']}, "
                           f"price_data={market_status['price_data_available']}")

            return CommandResult.success(
                message="Market status retrieved successfully",
                data=market_status
            )

        except Exception as e:
            self.logger.error(f"Error in GET_MARKET_STATUS_BOOTSTRAP: {e}", exc_info=True)
            return CommandResult.error(f"Failed to get market status: {str(e)}")

    def _handle_debug_trigger_on_quote(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        DEBUG COMMAND: Trigger PriceRepository.on_quote() for pipeline testing
        This simulates PriceFetchingService feeding data to test the complete pipeline:
        PriceRepository → MarketDataFilterService → ZMQ → Babysitter → Redis → GUI
        """
        try:
            self.logger.info(f"DEBUG_TRIGGER_ON_QUOTE: Received parameters: {parameters}")

            # Extract and validate parameters
            symbol = parameters.get("symbol", "").upper()
            bid = parameters.get("bid")
            ask = parameters.get("ask")
            bid_size = parameters.get("bid_size", 100)
            ask_size = parameters.get("ask_size", 100)
            timestamp = parameters.get("timestamp", time.time())
            conditions = parameters.get("conditions", [])

            # Validate required parameters
            if not symbol:
                return CommandResult.error("Missing required parameter: symbol")
            if not isinstance(bid, (int, float)) or bid <= 0:
                return CommandResult.error("Invalid bid price: must be positive number")
            if not isinstance(ask, (int, float)) or ask <= 0:
                return CommandResult.error("Invalid ask price: must be positive number")
            if bid >= ask:
                return CommandResult.error("Invalid spread: bid must be less than ask")

            self.logger.info(f"DEBUG: Triggering PriceRepository.on_quote for {symbol} Bid=${bid:.2f} Ask=${ask:.2f}")

            # Resolve PriceRepository instance from ApplicationCore
            price_repository = None

            # Try multiple ways to get PriceRepository
            if hasattr(self.app_core, 'price_repository'):
                price_repository = self._price_provider
                self.logger.info("Found PriceRepository via direct reference")
            elif hasattr(self.app_core, '_di_container'):
                try:
                    # Try to resolve from DI container
                    from core.di_container import DI_IPriceProvider
                    price_repository = self._price_provider
                    self.logger.info("Found PriceRepository via DI container")
                except Exception as e:
                    self.logger.warning(f"Could not resolve PriceRepository from DI: {e}")

            if not price_repository:
                error_msg = "PriceRepository not found in ApplicationCore"
                self.logger.error(f"ERROR: {error_msg}")
                return CommandResult.error(error_msg)

            # Call PriceRepository.on_quote() to trigger the pipeline
            self.logger.info(f"CALLING: price_repository.on_quote({symbol}, {bid}, {ask}, ...)")

            price_repository.on_quote(
                symbol=symbol,
                bid=float(bid),
                ask=float(ask),
                bid_size=int(bid_size),
                ask_size=int(ask_size),
                timestamp=float(timestamp),
                conditions=conditions
            )

            self.logger.info(f"SUCCESS: PriceRepository.on_quote() called for {symbol}")
            self.logger.info(f"PIPELINE TRIGGERED: Watch for diagnostic logs and GUI updates")

            return CommandResult.success(
                f"Successfully triggered pipeline for {symbol} ${bid:.2f}/${ask:.2f}",
                data={
                    "symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "timestamp": timestamp,
                    "pipeline_test": True
                }
            )

        except Exception as e:
            error_msg = f"DEBUG_TRIGGER_ON_QUOTE failed: {e}"
            self.logger.error(f"ERROR: {error_msg}", exc_info=True)
            return CommandResult.error(error_msg)

    def _handle_debug_trigger_on_trade(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        DEBUG COMMAND: Trigger PriceRepository.on_trade() for pipeline testing
        This simulates PriceFetchingService feeding trade data to test the complete pipeline:
        PriceRepository → MarketDataFilterService → ZMQ → Babysitter → Redis → GUI
        """
        try:
            self.logger.info(f"DEBUG_TRIGGER_ON_TRADE: Received parameters: {parameters}")

            # Extract and validate parameters
            symbol = parameters.get("symbol", "").upper()
            price = parameters.get("price")
            size = parameters.get("size")
            timestamp = parameters.get("timestamp", time.time())
            conditions = parameters.get("conditions", [])

            # Validate required parameters
            if not symbol:
                return CommandResult.error("Missing required parameter: symbol")
            if price is None:
                return CommandResult.error("Missing required parameter: price")
            if size is None:
                return CommandResult.error("Missing required parameter: size")

            self.logger.info(f"DEBUG: Triggering PriceRepository.on_trade for {symbol} Price=${price:.2f} Size={size}")

            # Resolve PriceRepository instance from ApplicationCore
            price_repository = None

            # Try multiple ways to get PriceRepository
            if hasattr(self.app_core, 'price_repository'):
                price_repository = self._price_provider
                self.logger.info("Found PriceRepository via direct reference")
            elif hasattr(self.app_core, '_di_container'):
                try:
                    # Try to resolve from DI container
                    from core.di_container import DI_IPriceProvider
                    price_repository = self._price_provider
                    self.logger.info("Found PriceRepository via DI container")
                except Exception as e:
                    self.logger.warning(f"Could not resolve PriceRepository from DI: {e}")

            if not price_repository:
                error_msg = "PriceRepository not found in ApplicationCore"
                self.logger.error(f"{error_msg}")
                return CommandResult.error(error_msg)

            # Call PriceRepository.on_trade() to trigger the pipeline
            self.logger.info(f"CALLING: price_repository.on_trade({symbol}, {price}, {size}, ...)")

            price_repository.on_trade(
                symbol=symbol,
                price=float(price),
                size=int(size),
                timestamp=float(timestamp),
                conditions=conditions
            )

            self.logger.info(f"SUCCESS: PriceRepository.on_trade() called for {symbol}")
            self.logger.info(f"PIPELINE TRIGGERED: Watch for diagnostic logs and GUI updates")

            return CommandResult.success(
                f"Successfully triggered trade pipeline for {symbol} ${price:.2f} x {size}",
                data={
                    "symbol": symbol,
                    "price": price,
                    "size": size,
                    "timestamp": timestamp,
                    "pipeline_test": True
                }
            )

        except Exception as e:
            error_msg = f"DEBUG_TRIGGER_ON_TRADE failed: {e}"
            self.logger.error(f"{error_msg}", exc_info=True)
            return CommandResult.error(error_msg)

    def _handle_debug_simulate_position_update(self, parameters: Dict[str, Any]) -> CommandResult:
        """
        DEBUG COMMAND: Simulate a position update by publishing PositionUpdateEvent
        This allows testing MarketDataFilterService filtering without actual trades
        """
        try:
            self.logger.info(f"DEBUG_SIMULATE_POSITION_UPDATE: Received parameters: {parameters}")

            # Extract and validate parameters
            symbol = parameters.get("symbol", "").upper()
            is_open = parameters.get("is_open", False)
            quantity = parameters.get("quantity", 0.0)
            average_price = parameters.get("average_price", 0.0)

            # Validate required parameters
            if not symbol:
                return CommandResult.error("Missing required parameter: symbol")

            self.logger.info(f"DEBUG: Simulating position update for {symbol} is_open={is_open} qty={quantity}")

            # Import event classes
            try:
                from core.events import PositionUpdateEvent, PositionUpdateEventData
            except ImportError as e:
                error_msg = f"Could not import PositionUpdateEvent classes: {e}"
                self.logger.error(error_msg)
                return CommandResult.error(error_msg)

            # Create and publish the PositionUpdateEvent on AppCore's internal EventBus
            pos_event_data = PositionUpdateEventData(
                symbol=symbol,
                is_open=is_open,
                quantity=float(quantity),
                average_price=float(average_price),
                strategy="long" if is_open and quantity > 0 else "closed",
                last_update_timestamp=time.time()
            )
            pos_event = PositionUpdateEvent(data=pos_event_data)

            # Publish to EventBus
            if self._event_bus:
                self._event_bus.publish(pos_event)
                self.logger.info(f"SUCCESS: Published PositionUpdateEvent for {symbol}, is_open={is_open}")
                self.logger.info(f"FILTER UPDATE: MarketDataFilterService should update active symbols")

                return CommandResult.success(
                    f"Successfully simulated position update for {symbol}",
                    data={
                        "symbol": symbol,
                        "is_open": is_open,
                        "quantity": quantity,
                        "average_price": average_price,
                        "event_published": True
                    }
                )
            else:
                error_msg = "EventBus not found in ApplicationCore"
                self.logger.error(f"{error_msg}")
                return CommandResult.error(error_msg)

        except Exception as e:
            error_msg = f"DEBUG_SIMULATE_POSITION_UPDATE failed: {e}"
            self.logger.error(f"{error_msg}", exc_info=True)
            return CommandResult.error(error_msg)


