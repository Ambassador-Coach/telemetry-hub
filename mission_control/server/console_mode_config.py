"""
Console Mode Configuration for Mission Control

Allows switching between integrated console (output in Mission Control)
and standalone console windows for each process.
"""

# Console mode settings
CONSOLE_MODE = "standalone"  # "integrated" or "standalone"

# Per-process overrides (optional)
PROCESS_CONSOLE_MODES = {
    # "Main": "standalone",  # Force Main to always use standalone console
    # "GUI": "integrated",   # Force GUI to always use integrated console
}

def get_console_mode(process_name: str) -> str:
    """Get console mode for a specific process"""
    return PROCESS_CONSOLE_MODES.get(process_name, CONSOLE_MODE)

def is_integrated_mode(process_name: str) -> bool:
    """Check if process should use integrated console"""
    return get_console_mode(process_name) == "integrated"