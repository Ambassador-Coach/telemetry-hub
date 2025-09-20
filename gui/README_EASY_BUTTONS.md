# Easy Button System for TESTRADE GUI

## Overview

The new modular GUI backend makes it incredibly easy to add new buttons and functionality while maintaining the exact look and feel of the original GUI3. This system provides:

1. **Button Helper** - Simple decorators to create new buttons
2. **Hot Reload Config** - Change settings without restarting
3. **Pre-built Patterns** - Common button types ready to use
4. **Clean Architecture** - Organized, maintainable code

## Quick Start: Adding a New Button

### Step 1: Define Your Button Handler

```python
from gui.utils.button_helper import ButtonHelper

@button_helper.register_button("my_new_button")
async def my_new_button_handler(parameters: dict):
    """What my button does"""
    
    # Get parameters
    value = parameters.get("value", 100)
    
    # Do something
    result = value * 2
    
    # Update GUI
    await button_helper.broadcast_success(f"Result: {result}")
    
    return {"status": "success", "result": result}
```

### Step 2: Add Button to Frontend

In your `portrait_trading_gui.html` or JavaScript:

```javascript
// Add button to HTML
<button onclick="sendCommand('my_new_button', {value: 200})">My Button</button>

// Or dynamically create
const button = createButton("My Button", "my_new_button", {value: 200});
```

That's it! The button is now fully functional.

## Common Button Patterns

### Toggle Button
```python
@button_helper.register_button("toggle_feature")
async def toggle_feature(params):
    current = button_helper.app_state.feature_enabled
    new_state = not current
    button_helper.app_state.feature_enabled = new_state
    await button_helper.broadcast_success(f"Feature {'on' if new_state else 'off'}")
    return {"status": "success", "enabled": new_state}
```

### Value Update Button
```python
@button_helper.register_button("update_size")
async def update_size(params):
    size = params.get("value")
    if size < 100 or size > 10000:
        return {"status": "error", "error": "Size must be 100-10000"}
    
    config_manager.set("trading_params.json", "size", size)
    await button_helper.broadcast_success(f"Size updated to {size}")
    return {"status": "success", "size": size}
```

### Action Button with Confirmation
```python
@button_helper.register_button("dangerous_action", requires_confirmation=True)
async def dangerous_action(params):
    # This will prompt user to confirm before executing
    await perform_dangerous_operation()
    return {"status": "success"}
```

### Multi-Step Button
```python
@button_helper.register_button("complex_operation")
async def complex_operation(params):
    await button_helper.broadcast_info("Step 1: Preparing...")
    await do_step_1()
    
    await button_helper.broadcast_info("Step 2: Processing...")
    await do_step_2()
    
    await button_helper.broadcast_success("Operation complete!")
    return {"status": "success"}
```

## Hot Reload Configuration

Configuration files automatically reload when changed:

```python
# In your button handler
size = config_manager.get("trading_params.json", "initial_size")

# Update a value (auto-saves and notifies GUI)
config_manager.set("trading_params.json", "initial_size", 2000)
```

Edit `gui/config/trading_params.json` and changes apply instantly!

## Button Helper Features

### Broadcasting Messages
```python
await button_helper.broadcast_success("Operation completed!")
await button_helper.broadcast_error("Something went wrong")
await button_helper.broadcast_info("Processing...")
await button_helper.broadcast_warning("Caution: High risk")
```

### Updating GUI State
```python
await button_helper.update_gui_state("active_count", 5)
await button_helper.update_gui_state("config.roi", [10, 20, 30, 40])
```

### Sending to Redis
```python
await button_helper.send_to_redis_stream("testrade:commands", {
    "command_type": "MY_COMMAND",
    "data": {"key": "value"}
})
```

## File Structure

```
gui/
├── buttons/
│   └── easy_buttons.py      # Your button definitions
├── utils/
│   ├── button_helper.py     # Button creation utilities
│   └── hot_reload_config.py # Configuration management
├── config/
│   ├── trading_params.json  # Trading settings
│   ├── ocr_settings.json    # OCR configuration
│   └── gui_settings.json    # GUI preferences
└── gui_backend.py           # Main application
```

## Adding to Route Manager

In `gui/routes/route_manager.py`, the buttons are automatically registered:

```python
def _setup_control_routes(self):
    # Existing code...
    
    # Add button helper
    from gui.utils.button_helper import ButtonHelper
    from gui.buttons.easy_buttons import setup_easy_buttons
    
    button_helper = ButtonHelper(
        self.app_state,
        self.websocket_manager,
        self.redis_service
    )
    
    # Register all easy buttons
    setup_easy_buttons(button_helper, self.config_manager)
    
    # Buttons are now available at /control/command endpoint!
```

## Testing Your Buttons

```python
# Test via curl
curl -X POST http://localhost:8001/control/command \
  -H "Content-Type: application/json" \
  -d '{"command": "my_new_button", "parameters": {"value": 500}}'

# Test via JavaScript console
await fetch('/control/command', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
        command: 'my_new_button',
        parameters: {value: 500}
    })
});
```

## Benefits

1. **Easy to Add**: New buttons in minutes, not hours
2. **Hot Reload**: Change configs without restart
3. **Type Safe**: Parameters are validated
4. **Error Handling**: Built-in error messages
5. **Consistent**: All buttons work the same way
6. **Maintainable**: Clean, organized code
7. **Exact Look**: Frontend unchanged from GUI3

## Examples in easy_buttons.py

Check out `gui/buttons/easy_buttons.py` for complete examples:
- Toggle buttons
- Value updates with validation  
- Confirmation dialogs
- Multi-step operations
- Preset configurations
- Real-time progress updates
- And more!

## Getting Started

1. Copy the button template from `easy_buttons.py`
2. Modify for your needs
3. Add button to HTML
4. Test it out!

The system handles all the complexity - you just focus on what the button should do!