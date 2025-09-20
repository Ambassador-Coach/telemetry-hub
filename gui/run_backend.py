#!/usr/bin/env python3
"""
Run the TESTRADE GUI Backend
"""

import subprocess
import sys
import os

# Change to GUI directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Run the backend
subprocess.run([sys.executable, "gui_backend.py"])