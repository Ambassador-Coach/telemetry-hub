# gui/utils/button_helper.py - Helper utilities for adding new buttons and functionality

"""
Button Helper Module
====================
This module makes it easy to add new buttons to the GUI without dealing with the complexity
of the full backend. Just define your button's functionality and this helper will handle
the rest.

Example Usage:
--------------
# In your route_manager.py or a separate buttons.py file:

from gui.utils.button_helper import ButtonHelper

# Create a helper instance
button_helper = ButtonHelper(app_state, websocket_manager, redis_service)

# Register a new button handler
@button_helper.register_button("my_new_feature")
async def handle_my_new_feature(parameters: dict):
    '''Handler for My New Feature button'''
    # Your custom logic here
    value = parameters.get("value", 100)
    
    # Send updates to GUI
    await button_helper.broadcast_success("My feature activated with value: " + str(value))
    
    # Return response
    return {"status": "success", "message": f"Feature activated with {value}"}

# The helper automatically creates the API endpoint and handles all the plumbing!
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, Any, Callable, Optional
from functools import wraps

logger = logging.getLogger(__name__)

class ButtonHelper:
    """
    Helper class to make adding new buttons and features extremely easy.
    Handles all the boilerplate code for you.
    """
    
    def __init__(self, app_state, websocket_manager, redis_service):
        self.app_state = app_state
        self.websocket_manager = websocket_manager
        self.redis_service = redis_service
        self.registered_buttons: Dict[str, Callable] = {}
        
    def register_button(self, command_name: str, requires_confirmation: bool = False):
        """
        Decorator to register a new button handler.
        
        Usage:
            @button_helper.register_button("my_feature")
            async def handle_my_feature(parameters: dict):
                # Your code here
                return {"status": "success"}
        """
        def decorator(handler_func):
            # Store the handler
            self.registered_buttons[command_name] = {
                "handler": handler_func,
                "requires_confirmation": requires_confirmation,
                "command_name": command_name
            }
            
            # Create wrapper that adds common functionality
            @wraps(handler_func)
            async def wrapper(parameters: Dict[str, Any]):
                start_time = time.time()
                command_id = str(uuid.uuid4())
                
                try:
                    # Log the command
                    logger.info(f"🔘 Button pressed: {command_name} (ID: {command_id})")
                    logger.debug(f"Parameters: {parameters}")
                    
                    # Send acknowledgment to GUI
                    await self.broadcast_info(f"Processing {command_name}...", command_id)
                    
                    # Call the actual handler
                    result = await handler_func(parameters)
                    
                    # Log completion
                    duration = (time.time() - start_time) * 1000
                    logger.info(f"✅ {command_name} completed in {duration:.1f}ms")
                    
                    return result
                    
                except Exception as e:
                    logger.error(f"❌ Error in {command_name}: {e}", exc_info=True)
                    await self.broadcast_error(f"Error: {str(e)}", command_id)
                    return {
                        "status": "error",
                        "error": str(e),
                        "command_id": command_id
                    }
            
            return wrapper
        return decorator
    
    async def broadcast_success(self, message: str, command_id: Optional[str] = None):
        """Send a success message to the GUI"""
        await self.websocket_manager.broadcast({
            "type": "system_message",
            "message": message,
            "level": "success",
            "command_id": command_id,
            "timestamp": time.time()
        })
    
    async def broadcast_error(self, message: str, command_id: Optional[str] = None):
        """Send an error message to the GUI"""
        await self.websocket_manager.broadcast({
            "type": "system_message",
            "message": message,
            "level": "error",
            "command_id": command_id,
            "timestamp": time.time()
        })
    
    async def broadcast_info(self, message: str, command_id: Optional[str] = None):
        """Send an info message to the GUI"""
        await self.websocket_manager.broadcast({
            "type": "system_message",
            "message": message,
            "level": "info",
            "command_id": command_id,
            "timestamp": time.time()
        })
    
    async def broadcast_warning(self, message: str, command_id: Optional[str] = None):
        """Send a warning message to the GUI"""
        await self.websocket_manager.broadcast({
            "type": "system_message",
            "message": message,
            "level": "warning",
            "command_id": command_id,
            "timestamp": time.time()
        })
    
    async def update_gui_state(self, key: str, value: Any):
        """Update a value in the GUI state and broadcast the change"""
        # Update local state
        if hasattr(self.app_state, key):
            setattr(self.app_state, key, value)
        
        # Broadcast the update
        await self.websocket_manager.broadcast({
            "type": "state_update",
            "key": key,
            "value": value,
            "timestamp": time.time()
        })
    
    async def send_to_redis_stream(self, stream_name: str, data: Dict[str, Any]):
        """Send data to a Redis stream"""
        message = {
            "metadata": {
                "source": "GUI_Button",
                "timestamp": time.time()
            },
            "payload": data
        }
        
        await self.redis_service.xadd(
            stream_name,
            {"json_payload": json.dumps(message)}
        )
    
    def get_all_buttons(self) -> Dict[str, Dict[str, Any]]:
        """Get information about all registered buttons"""
        return {
            name: {
                "command": name,
                "requires_confirmation": info["requires_confirmation"]
            }
            for name, info in self.registered_buttons.items()
        }


class ConfigurableButton:
    """
    A helper class for buttons that need configuration values.
    Makes it easy to create buttons with associated settings.
    """
    
    def __init__(self, button_helper: ButtonHelper, config_key: str, default_value: Any):
        self.button_helper = button_helper
        self.config_key = config_key
        self.default_value = default_value
        self._value = default_value
    
    @property
    def value(self):
        """Get the current configuration value"""
        return self._value
    
    @value.setter
    def value(self, new_value):
        """Set the configuration value and notify GUI"""
        self._value = new_value
        asyncio.create_task(
            self.button_helper.update_gui_state(self.config_key, new_value)
        )
    
    def register_handler(self, command_name: str):
        """Register a handler that automatically gets the config value"""
        def decorator(handler_func):
            @self.button_helper.register_button(command_name)
            async def wrapper(parameters: Dict[str, Any]):
                # Inject the current config value
                parameters[self.config_key] = self.value
                return await handler_func(parameters)
            return wrapper
        return decorator


# Example implementations of common button patterns
class CommonButtons:
    """Pre-built button implementations for common trading operations"""
    
    @staticmethod
    def create_toggle_button(button_helper: ButtonHelper, name: str, state_key: str):
        """Create a basic toggle button"""
        @button_helper.register_button(f"toggle_{name}")
        async def toggle_handler(parameters: Dict[str, Any]):
            current_state = getattr(button_helper.app_state, state_key, False)
            new_state = not current_state
            
            await button_helper.update_gui_state(state_key, new_state)
            await button_helper.broadcast_success(
                f"{name} {'enabled' if new_state else 'disabled'}"
            )
            
            return {
                "status": "success",
                "new_state": new_state
            }
        
        return toggle_handler
    
    @staticmethod
    def create_value_button(button_helper: ButtonHelper, name: str, state_key: str, 
                          min_value: float = None, max_value: float = None):
        """Create a button that updates a numeric value"""
        @button_helper.register_button(f"update_{name}")
        async def value_handler(parameters: Dict[str, Any]):
            new_value = parameters.get("value")
            
            if new_value is None:
                return {"status": "error", "error": "No value provided"}
            
            # Validate range
            if min_value is not None and new_value < min_value:
                return {"status": "error", "error": f"Value must be >= {min_value}"}
            if max_value is not None and new_value > max_value:
                return {"status": "error", "error": f"Value must be <= {max_value}"}
            
            await button_helper.update_gui_state(state_key, new_value)
            await button_helper.broadcast_success(f"{name} updated to {new_value}")
            
            return {
                "status": "success",
                "new_value": new_value
            }
        
        return value_handler
    
    @staticmethod
    def create_action_button(button_helper: ButtonHelper, name: str, 
                           action_func: Callable, requires_confirmation: bool = True):
        """Create a button that performs a specific action"""
        @button_helper.register_button(name, requires_confirmation=requires_confirmation)
        async def action_handler(parameters: Dict[str, Any]):
            try:
                result = await action_func(parameters)
                await button_helper.broadcast_success(f"{name} completed successfully")
                return {"status": "success", "result": result}
            except Exception as e:
                await button_helper.broadcast_error(f"{name} failed: {str(e)}")
                raise
        
        return action_handler