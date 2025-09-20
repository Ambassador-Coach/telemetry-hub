# core/config_service.py

"""
Configuration Service Implementation

This service provides a clean interface to configuration data, removing the need 
for services to directly access ApplicationCore for configuration.

This is part of the ApplicationCore decoupling effort that started with the 
telemetry service migration.
"""

import logging
from typing import Any, Optional, Dict, List
from interfaces.core.services import IConfigService
from utils.global_config import GlobalConfig, reload_global_config

logger = logging.getLogger(__name__)


class ConfigService(IConfigService):
    """
    Concrete implementation of IConfigService that wraps the GlobalConfig.
    
    This service provides a clean interface to configuration data without 
    requiring services to depend directly on ApplicationCore.
    """
    
    def __init__(self, global_config: GlobalConfig):
        """
        Initialize the configuration service.
        
        Args:
            global_config: The GlobalConfig instance to wrap
        """
        self._config = global_config
        logger.info("ConfigService initialized successfully")
    
    def get_config(self) -> GlobalConfig:
        """
        Get the current configuration object.
        
        Returns:
            GlobalConfig: The current configuration instance
        """
        return self._config
    
    def reload_config(self) -> None:
        """
        Reload configuration from file.
        
        This delegates to the global config reload function to maintain 
        consistency with existing behavior.
        """
        try:
            reload_global_config()
            logger.info("Configuration reloaded successfully via ConfigService")
        except Exception as e:
            logger.error(f"Failed to reload configuration: {e}", exc_info=True)
            raise
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value by key (dictionary-style access).
        
        This provides compatibility with dictionary-style access patterns
        that are common throughout the codebase.
        
        Args:
            key: Configuration key name
            default: Default value if key doesn't exist
            
        Returns:
            The configuration value or default if not found
        """
        return getattr(self._config, key, default)
    
    def get_attribute(self, attribute_name: str, default_value: Any = None) -> Any:
        """
        Get a specific configuration attribute by name.
        
        This provides a clean way to access config attributes without 
        needing to know the internal structure.
        
        Args:
            attribute_name: Name of the configuration attribute
            default_value: Default value if attribute doesn't exist
            
        Returns:
            The configuration value or default_value if not found
        """
        return getattr(self._config, attribute_name, default_value)
    
    def has_attribute(self, attribute_name: str) -> bool:
        """
        Check if a configuration attribute exists.
        
        Args:
            attribute_name: Name of the configuration attribute
            
        Returns:
            True if the attribute exists, False otherwise
        """
        return hasattr(self._config, attribute_name)
    
    def __getattr__(self, name: str) -> Any:
        """
        Delegate attribute access to the wrapped GlobalConfig.
        
        This allows services to access config attributes directly like:
        getattr(config_service, 'RISK_MAX_ORDER_QTY', 1000)
        
        Args:
            name: Attribute name to access
            
        Returns:
            The attribute value from the wrapped GlobalConfig
            
        Raises:
            AttributeError: If the attribute doesn't exist
        """
        return getattr(self._config, name)
    
    def get_roi_coordinates(self) -> List[int]:
        """
        Get ROI coordinates with proper defaults.
        
        This is a convenience method for the common ROI access pattern.
        
        Returns:
            List of 4 integers representing ROI coordinates [x1, y1, x2, y2]
        """
        return list(getattr(self._config, 'ROI_COORDINATES', [0, 0, 100, 100]))
    
    def get_redis_stream_name(self, stream_type: str, default_stream: str) -> str:
        """
        Get Redis stream name with fallback to default.
        
        This is a convenience method for the common Redis stream access pattern.
        
        Args:
            stream_type: Type of Redis stream (e.g., 'roi_updates')
            default_stream: Default stream name if config doesn't specify
            
        Returns:
            Redis stream name
        """
        config_key = f'redis_stream_{stream_type}'
        return getattr(self._config, config_key, default_stream)
    
    def is_feature_enabled(self, feature_flag: str) -> bool:
        """
        Check if a feature flag is enabled.
        
        This is a convenience method for boolean feature flags.
        
        Args:
            feature_flag: Name of the feature flag
            
        Returns:
            True if the feature is enabled, False otherwise
        """
        return bool(getattr(self._config, feature_flag, False))