# utils/ipc_endpoints.py

"""
Platform-aware IPC endpoint configuration for TESTRADE.

This module provides intelligent endpoint selection based on:
- Platform (Windows/Linux/WSL)
- Service location (local/remote)
- Performance requirements

For Windows-to-Windows communication, IPC provides 5-10x lower latency than TCP.
"""

import platform
import os
import logging
from typing import Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def is_wsl() -> bool:
    """Detect if running under WSL."""
    try:
        with open('/proc/version', 'r') as f:
            return 'microsoft' in f.read().lower()
    except:
        return False


def get_ipc_base_path() -> str:
    """
    Get the base path for IPC endpoints based on platform.
    
    Returns:
        Base path for IPC files/pipes
    """
    system = platform.system()
    
    if system == "Windows":
        # Windows named pipes don't use filesystem paths
        return ""
    elif system == "Linux":
        if is_wsl():
            # WSL - use /tmp which is usually tmpfs
            base = "/tmp/testrade_ipc_wsl"
        else:
            # Native Linux - use /var/run if available, else /tmp
            if os.path.exists("/var/run") and os.access("/var/run", os.W_OK):
                base = "/var/run/testrade"
            else:
                base = "/tmp/testrade_ipc"
        
        # Create directory with appropriate permissions
        Path(base).mkdir(mode=0o700, parents=True, exist_ok=True)
        return base
    else:
        # macOS or other
        base = "/tmp/testrade_ipc"
        Path(base).mkdir(mode=0o700, parents=True, exist_ok=True)
        return base


def get_ipc_endpoint(service_name: str, channel: str, namespace: str = "testrade") -> str:
    """
    Generate platform-appropriate IPC endpoint.
    
    Args:
        service_name: Name of the service (e.g., "telemetry", "babysitter")
        channel: Channel name (e.g., "bulk", "trading", "system")
        namespace: Optional namespace to avoid conflicts
        
    Returns:
        IPC endpoint string for ZMQ
        
    Examples:
        Windows: "ipc://testrade_telemetry_bulk"
        Linux:   "ipc:///tmp/testrade_ipc/telemetry_bulk.sock"
    """
    endpoint_name = f"{namespace}_{service_name}_{channel}"
    
    if platform.system() == "Windows":
        # Windows named pipes - no filesystem path needed
        return f"ipc://{endpoint_name}"
    else:
        # Unix domain sockets - need absolute path
        base_path = get_ipc_base_path()
        return f"ipc://{base_path}/{endpoint_name}.sock"


def cleanup_ipc_files(service_name: Optional[str] = None):
    """
    Clean up IPC socket files (Unix only).
    
    Args:
        service_name: Specific service to clean up, or None for all
    """
    if platform.system() == "Windows":
        return  # Windows named pipes don't leave files
    
    base_path = get_ipc_base_path()
    if not os.path.exists(base_path):
        return
    
    pattern = f"testrade_{service_name}_*.sock" if service_name else "testrade_*.sock"
    
    from pathlib import Path
    for sock_file in Path(base_path).glob(pattern):
        try:
            sock_file.unlink()
            logger.debug(f"Cleaned up IPC socket: {sock_file}")
        except Exception as e:
            logger.warning(f"Failed to clean up {sock_file}: {e}")


# Pre-generated endpoint mappings
IPC_ENDPOINTS = {
    # TelemetryService endpoints (Windows processes connect here)
    "TELEMETRY_BULK_ENDPOINT_IPC": get_ipc_endpoint("telemetry", "bulk"),
    "TELEMETRY_TRADING_ENDPOINT_IPC": get_ipc_endpoint("telemetry", "trading"),
    "TELEMETRY_SYSTEM_ENDPOINT_IPC": get_ipc_endpoint("telemetry", "system"),
    
    # Babysitter IPC endpoints (TelemetryService connects here)
    "BABYSITTER_BULK_ENDPOINT_IPC": get_ipc_endpoint("babysitter", "bulk"),
    "BABYSITTER_TRADING_ENDPOINT_IPC": get_ipc_endpoint("babysitter", "trading"),
    "BABYSITTER_SYSTEM_ENDPOINT_IPC": get_ipc_endpoint("babysitter", "system"),
    
    # Direct service-to-service IPC (future optimization)
    "CORE_TO_BABYSITTER_IPC": get_ipc_endpoint("core", "direct"),
    "OCR_TO_TELEMETRY_IPC": get_ipc_endpoint("ocr", "direct"),
}


def get_endpoint_info() -> Dict[str, str]:
    """Get human-readable endpoint information for logging."""
    info = {
        "platform": platform.system(),
        "is_wsl": is_wsl(),
        "ipc_base_path": get_ipc_base_path() or "N/A (Windows)",
    }
    
    if platform.system() != "Windows":
        base_path = get_ipc_base_path()
        if os.path.exists(base_path):
            info["ipc_dir_exists"] = True
            info["ipc_dir_writable"] = os.access(base_path, os.W_OK)
        else:
            info["ipc_dir_exists"] = False
    
    return info


def validate_ipc_setup() -> bool:
    """
    Validate that IPC can be used on this system.
    
    Returns:
        True if IPC is properly configured and usable
    """
    try:
        if platform.system() == "Windows":
            # Windows named pipes always available
            return True
        else:
            # Check if we can create socket files
            base_path = get_ipc_base_path()
            test_file = Path(base_path) / ".test_ipc"
            test_file.touch(mode=0o600)
            test_file.unlink()
            return True
    except Exception as e:
        logger.error(f"IPC validation failed: {e}")
        return False


# Auto-validate on import
if validate_ipc_setup():
    logger.info(f"IPC endpoints configured: {get_endpoint_info()}")
else:
    logger.warning("IPC validation failed - falling back to TCP only")


# Performance comparison data (microseconds)
LATENCY_COMPARISON = {
    "tcp_loopback": {
        "mean": 50,
        "p99": 100,
        "description": "TCP over localhost (127.0.0.1)"
    },
    "ipc_windows": {
        "mean": 5,
        "p99": 15,
        "description": "Windows named pipes"
    },
    "ipc_linux": {
        "mean": 3,
        "p99": 10,
        "description": "Unix domain sockets"
    }
}