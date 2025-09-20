"""
IntelliSense Module for TESTRADE

This module provides comprehensive testing and validation capabilities for the trading system,
including replay functionality, ground truth validation, and performance monitoring.

Architecture:
- Core: Fundamental types, enums, and interfaces
- Controllers: Master orchestration and session management
- Factories: Data source creation and management
- Engines: Intelligence engines and data sources

Usage:
    # Import specific components from submodules
    from intellisense.core.enums import OperationMode
    from intellisense.controllers.master_controller import IntelliSenseMasterController
    from intellisense.factories.datasource_factory import DataSourceFactory
"""

__version__ = "1.0.0"

# Debug import tracking
import sys
print(f"IntelliSense init started. Current modules: {len(sys.modules)}")

# ADD: Import tracer for debugging
_import_tracker = {}

class ImportTracker:
    def find_module(self, fullname, path=None):
        if fullname.startswith('intellisense'):
            _import_tracker[fullname] = "LOADING"
            print(f"Loading: {fullname}")
        return None  # Let default import mechanism handle it

    def load_module(self, fullname):
        if fullname in sys.modules:
            module = sys.modules[fullname]
            if fullname.startswith('intellisense'):
                _import_tracker[fullname] = "LOADED"
                print(f"Loaded: {fullname}")
            return module
        return None

# Install import tracker
sys.meta_path.insert(0, ImportTracker())
print(f"Import tracker installed")

__author__ = "TESTRADE Development Team"

# REMOVED: Problematic import that causes circular dependency chain
# from .core.enums import OperationMode

# Let users import specific components from submodules as needed
# This avoids heavy import chains and circular dependency issues
# Users should import directly: from intellisense.core.enums import OperationMode

__all__ = [
    # No automatic imports - users must import explicitly from submodules
]
