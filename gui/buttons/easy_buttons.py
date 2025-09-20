# gui/buttons/easy_buttons.py - Example of how easy it is to add new buttons

"""
Easy Button Examples
===================
This file shows how easy it is to add new buttons to the GUI using the helper utilities.
Just copy these patterns to add your own buttons!
"""

from utils.button_helper import ButtonHelper, ConfigurableButton, CommonButtons
from utils.hot_reload_config_stub import TradingConfigManager
import asyncio
import logging

logger = logging.getLogger(__name__)

def setup_easy_buttons(button_helper: ButtonHelper, config_manager: TradingConfigManager):
    """
    Set up all the easy-to-add buttons.
    This function is called during app initialization.
    """
    
    # ========================================================================
    # EXAMPLE 1: Simple Toggle Button
    # ========================================================================
    @button_helper.register_button("toggle_all_symbols")
    async def toggle_all_symbols(parameters: dict):
        """Toggle monitoring all symbols vs just active trades"""
        current_state = button_helper.app_state.system_config.get("all_symbols_mode", False)
        new_state = not current_state
        
        # Update state
        button_helper.app_state.system_config["all_symbols_mode"] = new_state
        
        # Send to backend
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "TOGGLE_ALL_SYMBOLS",
            "enabled": new_state
        })
        
        # Notify GUI
        await button_helper.broadcast_success(
            f"All symbols mode {'enabled' if new_state else 'disabled'}"
        )
        
        return {"status": "success", "new_state": new_state}
    
    # ========================================================================
    # EXAMPLE 2: Value Update Button (with validation)
    # ========================================================================
    @button_helper.register_button("update_initial_size")
    async def update_initial_size(parameters: dict):
        """Update initial position size"""
        new_size = parameters.get("value")
        
        # Validate
        if not new_size or new_size < 100:
            return {"status": "error", "error": "Size must be at least 100"}
        if new_size > 10000:
            return {"status": "error", "error": "Size cannot exceed 10,000"}
        
        # Update config (with hot reload!)
        config_manager.set("trading_params.json", "initial_size", new_size)
        
        # Send to backend
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "UPDATE_TRADING_PARAMS",
            "initial_size": new_size
        })
        
        # Notify GUI
        await button_helper.broadcast_success(f"Initial size updated to {new_size}")
        
        return {"status": "success", "new_value": new_size}
    
    # ========================================================================
    # EXAMPLE 3: Action Button with Confirmation
    # ========================================================================
    @button_helper.register_button("force_close_all", requires_confirmation=True)
    async def force_close_all(parameters: dict):
        """Force close all positions (requires confirmation)"""
        reason = parameters.get("reason", "Manual GUI Force Close")
        
        # Send command
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "FORCE_CLOSE_ALL",
            "reason": reason
        })
        
        # Clear active trades locally
        button_helper.app_state.active_trades.clear()
        
        # Notify GUI
        await button_helper.broadcast_warning("Force closing all positions...")
        
        return {"status": "success", "message": "Force close command sent"}
    
    # ========================================================================
    # EXAMPLE 4: Button with Multiple Steps
    # ========================================================================
    @button_helper.register_button("optimize_ocr_settings")
    async def optimize_ocr_settings(parameters: dict):
        """Auto-optimize OCR settings based on current conditions"""
        
        # Step 1: Notify start
        await button_helper.broadcast_info("Starting OCR optimization...")
        
        # Step 2: Get current screenshot
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "CAPTURE_TEST_FRAME"
        })
        
        # Step 3: Wait for response (simplified for example)
        await asyncio.sleep(1)
        
        # Step 4: Apply optimized settings
        new_settings = {
            "blur_kernel": 7,
            "threshold_value": 130,
            "confidence_threshold": 0.85
        }
        
        config_manager.set("ocr_settings.json", "preprocessing", new_settings)
        
        # Step 5: Notify completion
        await button_helper.broadcast_success("OCR settings optimized!")
        
        return {"status": "success", "settings": new_settings}
    
    # ========================================================================
    # EXAMPLE 5: Using Pre-built Button Patterns
    # ========================================================================
    
    # Create a simple toggle for recording mode
    CommonButtons.create_toggle_button(
        button_helper, 
        "recording_mode", 
        "recordingActive"
    )
    
    # Create a value updater for stop loss
    CommonButtons.create_value_button(
        button_helper,
        "stop_loss_percent",
        "stop_loss_percent",
        min_value=0.1,
        max_value=10.0
    )
    
    # ========================================================================
    # EXAMPLE 6: Button with Real-time Updates
    # ========================================================================
    @button_helper.register_button("analyze_performance")
    async def analyze_performance(parameters: dict):
        """Analyze trading performance with real-time updates"""
        
        # Send initial status
        await button_helper.broadcast_info("Analyzing performance...")
        
        # Simulate analysis steps with updates
        steps = [
            ("Loading trade history...", 0.5),
            ("Calculating win rate...", 0.3),
            ("Computing risk metrics...", 0.4),
            ("Generating report...", 0.2)
        ]
        
        for step_msg, delay in steps:
            await button_helper.broadcast_info(step_msg)
            await asyncio.sleep(delay)
        
        # Calculate results (simplified)
        total_trades = len(button_helper.app_state.active_trades)
        win_rate = 65.5  # Example
        
        # Update GUI state
        await button_helper.update_gui_state("performance_metrics", {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "analyzed_at": asyncio.get_event_loop().time()
        })
        
        # Send final result
        await button_helper.broadcast_success(
            f"Analysis complete: {total_trades} trades, {win_rate}% win rate"
        )
        
        return {
            "status": "success",
            "metrics": {
                "total_trades": total_trades,
                "win_rate": win_rate
            }
        }
    
    # ========================================================================
    # EXAMPLE 7: Configurable Button
    # ========================================================================
    
    # Create a configurable button for OCR confidence
    ocr_confidence = ConfigurableButton(
        button_helper,
        "ocr_confidence_threshold",
        0.8
    )
    
    @ocr_confidence.register_handler("update_ocr_confidence")
    async def update_ocr_confidence(parameters: dict):
        """Update OCR confidence threshold"""
        # The current value is automatically injected
        threshold = parameters["ocr_confidence_threshold"]
        
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "UPDATE_OCR_SETTINGS",
            "confidence_threshold": threshold
        })
        
        return {"status": "success", "threshold": threshold}
    
    # ========================================================================
    # EXAMPLE 8: Button that Updates Multiple Things
    # ========================================================================
    @button_helper.register_button("apply_preset")
    async def apply_preset(parameters: dict):
        """Apply a trading preset configuration"""
        preset_name = parameters.get("preset", "conservative")
        
        presets = {
            "conservative": {
                "initial_size": 500,
                "add_type": "None",
                "reduce_percent": 75,
                "stop_loss_percent": 1.0
            },
            "moderate": {
                "initial_size": 1000,
                "add_type": "Equal",
                "reduce_percent": 50,
                "stop_loss_percent": 2.0
            },
            "aggressive": {
                "initial_size": 2000,
                "add_type": "Pyramid",
                "reduce_percent": 25,
                "stop_loss_percent": 3.0
            }
        }
        
        if preset_name not in presets:
            return {"status": "error", "error": f"Unknown preset: {preset_name}"}
        
        preset_config = presets[preset_name]
        
        # Update all values
        for key, value in preset_config.items():
            config_manager.set("trading_params.json", key, value)
            await button_helper.update_gui_state(f"config.{key}", value)
        
        # Send to backend
        await button_helper.send_to_redis_stream("testrade:commands", {
            "command_type": "UPDATE_TRADING_PARAMS",
            **preset_config
        })
        
        await button_helper.broadcast_success(f"Applied {preset_name} preset")
        
        return {"status": "success", "preset": preset_name, "config": preset_config}
    
    logger.info(f"✅ Registered {len(button_helper.registered_buttons)} easy buttons")


# ========================================================================
# TEMPLATE FOR ADDING YOUR OWN BUTTON
# ========================================================================
"""
To add your own button, just copy this template:

@button_helper.register_button("your_button_name")
async def your_button_handler(parameters: dict):
    '''Description of what your button does'''
    
    # 1. Get any parameters from the request
    value = parameters.get("value", "default")
    
    # 2. Do your logic here
    result = do_something(value)
    
    # 3. Update GUI if needed
    await button_helper.update_gui_state("some_key", result)
    
    # 4. Send commands to backend if needed
    await button_helper.send_to_redis_stream("testrade:commands", {
        "command_type": "YOUR_COMMAND",
        "data": result
    })
    
    # 5. Notify the user
    await button_helper.broadcast_success(f"Button action completed: {result}")
    
    # 6. Return response
    return {"status": "success", "result": result}
"""