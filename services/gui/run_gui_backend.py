#!/usr/bin/env python3
# run_gui_backend.py - GUI Backend launcher from project root

"""
GUI Backend Launcher
====================
Launches the GUI backend from the project root directory to ensure
all imports work correctly.
"""

import os
import sys
import signal
import argparse

# Ensure we're in the right directory
project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)

# Add current directory to Python path
sys.path.insert(0, project_root)

# Add GUI directory to Python path for internal imports
gui_dir = os.path.join(project_root, 'gui')
sys.path.insert(0, gui_dir)

# Import and run the GUI backend
from gui.gui_backend import app
import uvicorn

# Thread monitor removed - focus on execution time only
THREAD_MONITOR_AVAILABLE = False

def setup_gui_signal_handlers():
    """Setup signal handlers for graceful independent shutdown."""

    def graceful_shutdown(signum, frame):
        """Handle shutdown signals gracefully without affecting other processes."""
        print(f"GUI Backend received signal {signum} - shutting down gracefully")
        print("This shutdown will NOT affect other TESTRADE services")
        # Thread monitoring removed
        print("GUI Backend shutdown complete")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, graceful_shutdown)
    
    return graceful_shutdown

if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="TESTRADE GUI Backend Server")
    parser.add_argument("--headless", action="store_true", 
                       help="Run with minimal logging (not applicable for GUI)")
    parser.add_argument("--replay", action="store_true", 
                       help="Run in replay mode (connects to mock services)")
    args = parser.parse_args()
    
    # Thread monitoring removed for production stability

    # Setup independent signal handlers
    shutdown_handler = setup_gui_signal_handlers()

    try:
        if args.replay:
            print("GUI Backend starting in REPLAY mode - will connect to mock services")
            # In replay mode, the GUI would connect to mock service endpoints
            # This could be configured through environment variables or config
        else:
            print("GUI Backend starting in LIVE mode")
            
        # Load network config for GUI binding
        import json
        from pathlib import Path
        
        network_config_path = Path(__file__).parent.parent.parent / "network_config.json"
        host = "0.0.0.0"
        port = 8001
        
        if network_config_path.exists():
            with open(network_config_path) as f:
                net_config = json.load(f)
                host = net_config["gui_backend"]["host"]
                port = net_config["gui_backend"]["port"]
                print(f"GUI Backend loaded network config: {host}:{port}")
        
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        print("GUI Backend interrupted - exiting gracefully")
        # Thread monitoring removed
        sys.exit(0)
    except Exception as e:
        print(f"GUI Backend fatal error: {e}")
        # Thread monitoring removed
        sys.exit(1)
    finally:
        # Thread monitoring removed
        pass