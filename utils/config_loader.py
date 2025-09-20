"""
Distributed Configuration Loader
Loads control.json and applies machine-specific overrides
"""

import json
import os
import socket
from pathlib import Path
from typing import Dict, Any

class DistributedConfig:
    """
    Smart configuration loader for distributed TESTRADE architecture.
    Automatically applies machine-specific settings based on hostname/IP.
    """
    
    def __init__(self, config_path: str = None):
        if config_path is None:
            # Try multiple locations
            possible_paths = [
                "/home/telemetry/telemetry-hub/config/control.json",
                "C:\\TESTRADE\\config\\control.json",
                "./config/control.json",
                "../config/control.json"
            ]
            for path in possible_paths:
                if Path(path).exists():
                    config_path = path
                    break
            else:
                raise FileNotFoundError("control.json not found in standard locations")
        
        # Load base configuration
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        
        # Detect which machine we're on
        self.machine_role = self._detect_machine_role()
        
        # Apply machine-specific overrides
        self._apply_machine_overrides()
        
        # Set endpoint values based on machine role
        self._configure_endpoints()
        
        # Set Redis configuration based on machine
        self._configure_redis()
        
        print(f"Configuration loaded for: {self.machine_role}")
    
    def _detect_machine_role(self) -> str:
        """Detect if we're on BEAST or TELEMETRY machine."""
        hostname = socket.gethostname().lower()
        
        # Check by hostname
        if 'beast' in hostname:
            return 'BEAST'
        elif 'telemetry' in hostname:
            return 'TELEMETRY'
        
        # Check by IP
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            if local_ip == '192.168.50.100':
                return 'BEAST'
            elif local_ip == '192.168.50.101':
                return 'TELEMETRY'
        except:
            pass
        
        # Check by OS (fallback)
        if os.name == 'nt':  # Windows
            return 'BEAST'
        else:  # Linux/Unix
            return 'TELEMETRY'
    
    def _apply_machine_overrides(self):
        """Apply machine-specific configuration overrides."""
        overrides = self.config.get('machine_specific_overrides', {}).get(self.machine_role, {})
        
        for key, value in overrides.items():
            if not key.startswith('comment'):
                self.config[key] = value
    
    def _configure_endpoints(self):
        """Set telemetry endpoints based on machine role."""
        endpoints = self.config.get('telemetry_endpoints', {})
        
        if self.machine_role == 'BEAST':
            # BEAST pushes to TELEMETRY
            self.config['TELEMETRY_BULK_ENDPOINT'] = endpoints['bulk']['beast_push_to']
            self.config['TELEMETRY_TRADING_ENDPOINT'] = endpoints['trading']['beast_push_to']
            self.config['TELEMETRY_SYSTEM_ENDPOINT'] = endpoints['system']['beast_push_to']
        else:  # TELEMETRY
            # TELEMETRY binds to receive
            self.config['TELEMETRY_BULK_ENDPOINT'] = endpoints['bulk']['telemetry_bind']
            self.config['TELEMETRY_TRADING_ENDPOINT'] = endpoints['trading']['telemetry_bind']
            self.config['TELEMETRY_SYSTEM_ENDPOINT'] = endpoints['system']['telemetry_bind']
    
    def _configure_redis(self):
        """Set Redis configuration based on machine."""
        redis_config = self.config.get('redis_config', {})
        
        if self.machine_role == 'BEAST':
            # BEAST should not connect to Redis
            beast_redis = redis_config.get('beast', {})
            self.config['REDIS_HOST'] = beast_redis.get('REDIS_HOST', 'DO_NOT_CONNECT')
        else:  # TELEMETRY
            # TELEMETRY connects to local Redis
            telemetry_redis = redis_config.get('telemetry', {})
            self.config['REDIS_HOST'] = telemetry_redis.get('REDIS_HOST', 'localhost')
            self.config['REDIS_PORT'] = telemetry_redis.get('REDIS_PORT', 6379)
            self.config['REDIS_DB'] = telemetry_redis.get('REDIS_DB', 0)
            self.config['REDIS_PASSWORD'] = telemetry_redis.get('REDIS_PASSWORD', None)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value."""
        return self.config.get(key, default)
    
    def __getattr__(self, key: str) -> Any:
        """Allow attribute-style access to config values."""
        if key in self.config:
            return self.config[key]
        raise AttributeError(f"Configuration key '{key}' not found")
    
    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary."""
        return self.config.copy()


# Backward compatibility function
def load_global_config(config_path: str = None) -> DistributedConfig:
    """Load configuration with machine-specific settings applied."""
    return DistributedConfig(config_path)