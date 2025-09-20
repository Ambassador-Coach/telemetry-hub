# gui/utils/hot_reload_config.py - Hot-reload configuration system

"""
Hot Reload Configuration System
===============================
This module provides automatic configuration reloading without restarting the backend.
Changes to configuration files are automatically detected and applied.

Features:
- Automatic file watching
- Type validation
- Change notifications to GUI
- Rollback on errors
- Configuration history
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent
import yaml

logger = logging.getLogger(__name__)

class ConfigChangeHandler(FileSystemEventHandler):
    """Handles file system events for configuration files"""
    
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.last_modified = {}
    
    def on_modified(self, event):
        if event.is_directory:
            return
            
        # Debounce rapid changes
        file_path = event.src_path
        current_time = time.time()
        last_time = self.last_modified.get(file_path, 0)
        
        if current_time - last_time < 1.0:  # Ignore changes within 1 second
            return
            
        self.last_modified[file_path] = current_time
        
        # Handle the change
        if file_path.endswith(('.json', '.yaml', '.yml')):
            logger.info(f"📁 Configuration file changed: {file_path}")
            asyncio.create_task(self.config_manager.reload_file(file_path))


class HotReloadConfig:
    """
    Main configuration manager with hot-reload capabilities.
    
    Usage:
        config = HotReloadConfig(websocket_manager)
        
        # Register a configuration file
        config.register_file("trading_params.json", {
            "initial_size": 1000,
            "add_type": "Equal",
            "reduce_percent": 50
        })
        
        # Register a callback for changes
        config.on_change("trading_params.json", lambda data: print("Config updated!"))
        
        # Start watching for changes
        await config.start_watching()
        
        # Access configuration
        trading_params = config.get("trading_params.json")
    """
    
    def __init__(self, websocket_manager=None):
        self.websocket_manager = websocket_manager
        self.configs: Dict[str, Dict[str, Any]] = {}
        self.config_paths: Dict[str, Path] = {}
        self.change_callbacks: Dict[str, List[Callable]] = {}
        self.config_history: Dict[str, List[Dict[str, Any]]] = {}
        self.observer = Observer()
        self.watching = False
        
    def register_file(self, filename: str, default_config: Dict[str, Any], 
                     config_dir: Optional[str] = None):
        """Register a configuration file with default values"""
        if config_dir is None:
            config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
        
        # Ensure config directory exists
        os.makedirs(config_dir, exist_ok=True)
        
        file_path = Path(config_dir) / filename
        self.config_paths[filename] = file_path
        
        # Load existing or create with defaults
        if file_path.exists():
            try:
                self.configs[filename] = self._load_file(file_path)
                logger.info(f"✅ Loaded existing config: {filename}")
            except Exception as e:
                logger.error(f"❌ Error loading {filename}, using defaults: {e}")
                self.configs[filename] = default_config
                self._save_file(file_path, default_config)
        else:
            self.configs[filename] = default_config
            self._save_file(file_path, default_config)
            logger.info(f"📝 Created new config file: {filename}")
        
        # Initialize history
        self.config_history[filename] = [{
            "timestamp": datetime.now().isoformat(),
            "config": self.configs[filename].copy()
        }]
        
        return self.configs[filename]
    
    def get(self, filename: str, key: Optional[str] = None, default: Any = None):
        """Get configuration value"""
        if filename not in self.configs:
            return default
            
        config = self.configs[filename]
        
        if key is None:
            return config
            
        # Support nested keys with dot notation
        keys = key.split('.')
        value = config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
                
        return value
    
    def set(self, filename: str, key: str, value: Any):
        """Set configuration value and save"""
        if filename not in self.configs:
            logger.error(f"Configuration file not registered: {filename}")
            return False
            
        # Support nested keys
        keys = key.split('.')
        config = self.configs[filename]
        
        # Navigate to the parent of the target key
        current = config
        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]
        
        # Set the value
        old_value = current.get(keys[-1])
        current[keys[-1]] = value
        
        # Save to file
        file_path = self.config_paths[filename]
        try:
            self._save_file(file_path, config)
            
            # Notify about the change
            asyncio.create_task(self._notify_change(filename, key, old_value, value))
            
            # Add to history
            self._add_to_history(filename, config)
            
            logger.info(f"✅ Updated {filename}: {key} = {value}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error saving {filename}: {e}")
            # Rollback
            current[keys[-1]] = old_value
            return False
    
    def on_change(self, filename: str, callback: Callable):
        """Register a callback for configuration changes"""
        if filename not in self.change_callbacks:
            self.change_callbacks[filename] = []
        self.change_callbacks[filename].append(callback)
    
    async def start_watching(self):
        """Start watching configuration files for changes"""
        if self.watching:
            return
            
        # Set up file watchers for each registered config
        watched_dirs = set()
        
        for file_path in self.config_paths.values():
            parent_dir = str(file_path.parent)
            if parent_dir not in watched_dirs:
                event_handler = ConfigChangeHandler(self)
                self.observer.schedule(event_handler, parent_dir, recursive=False)
                watched_dirs.add(parent_dir)
        
        self.observer.start()
        self.watching = True
        logger.info(f"👁️ Watching {len(watched_dirs)} configuration directories")
    
    def stop_watching(self):
        """Stop watching configuration files"""
        if self.watching:
            self.observer.stop()
            self.observer.join()
            self.watching = False
            logger.info("🛑 Stopped watching configuration files")
    
    async def reload_file(self, file_path: str):
        """Reload a configuration file"""
        # Find which config this file belongs to
        filename = None
        for name, path in self.config_paths.items():
            if str(path) == file_path:
                filename = name
                break
                
        if not filename:
            return
            
        try:
            # Load new configuration
            new_config = self._load_file(Path(file_path))
            
            # Validate the new configuration
            if not self._validate_config(filename, new_config):
                raise ValueError("Configuration validation failed")
            
            # Store old config for rollback
            old_config = self.configs[filename].copy()
            
            # Apply new configuration
            self.configs[filename] = new_config
            
            # Notify callbacks
            for callback in self.change_callbacks.get(filename, []):
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(new_config)
                    else:
                        callback(new_config)
                except Exception as e:
                    logger.error(f"❌ Error in change callback: {e}")
            
            # Broadcast to GUI
            if self.websocket_manager:
                await self.websocket_manager.broadcast({
                    "type": "config_reloaded",
                    "filename": filename,
                    "timestamp": time.time()
                })
            
            # Add to history
            self._add_to_history(filename, new_config)
            
            logger.info(f"♻️ Reloaded configuration: {filename}")
            
        except Exception as e:
            logger.error(f"❌ Error reloading {filename}: {e}")
            
            # Broadcast error to GUI
            if self.websocket_manager:
                await self.websocket_manager.broadcast({
                    "type": "config_reload_error",
                    "filename": filename,
                    "error": str(e),
                    "timestamp": time.time()
                })
    
    def _load_file(self, file_path: Path) -> Dict[str, Any]:
        """Load configuration from file"""
        content = file_path.read_text()
        
        if file_path.suffix == '.json':
            return json.loads(content)
        elif file_path.suffix in ('.yaml', '.yml'):
            return yaml.safe_load(content)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")
    
    def _save_file(self, file_path: Path, config: Dict[str, Any]):
        """Save configuration to file"""
        if file_path.suffix == '.json':
            content = json.dumps(config, indent=2)
        elif file_path.suffix in ('.yaml', '.yml'):
            content = yaml.dump(config, default_flow_style=False)
        else:
            raise ValueError(f"Unsupported file type: {file_path.suffix}")
            
        file_path.write_text(content)
    
    def _validate_config(self, filename: str, config: Dict[str, Any]) -> bool:
        """Validate configuration structure"""
        # Override this method to add custom validation
        return isinstance(config, dict)
    
    async def _notify_change(self, filename: str, key: str, old_value: Any, new_value: Any):
        """Notify about a configuration change"""
        if self.websocket_manager:
            await self.websocket_manager.broadcast({
                "type": "config_changed",
                "filename": filename,
                "key": key,
                "old_value": old_value,
                "new_value": new_value,
                "timestamp": time.time()
            })
    
    def _add_to_history(self, filename: str, config: Dict[str, Any]):
        """Add configuration to history"""
        if filename not in self.config_history:
            self.config_history[filename] = []
            
        self.config_history[filename].append({
            "timestamp": datetime.now().isoformat(),
            "config": config.copy()
        })
        
        # Keep only last 10 versions
        if len(self.config_history[filename]) > 10:
            self.config_history[filename] = self.config_history[filename][-10:]
    
    def get_history(self, filename: str) -> List[Dict[str, Any]]:
        """Get configuration history"""
        return self.config_history.get(filename, [])
    
    def rollback(self, filename: str, version_index: int = -2):
        """Rollback to a previous configuration version"""
        if filename not in self.config_history:
            return False
            
        history = self.config_history[filename]
        if abs(version_index) > len(history):
            return False
            
        previous_config = history[version_index]["config"]
        self.configs[filename] = previous_config.copy()
        
        # Save to file
        file_path = self.config_paths[filename]
        self._save_file(file_path, previous_config)
        
        logger.info(f"↩️ Rolled back {filename} to version {version_index}")
        return True


# Pre-configured setups for common use cases
class TradingConfigManager(HotReloadConfig):
    """Pre-configured hot-reload manager for trading configurations"""
    
    def __init__(self, websocket_manager=None):
        super().__init__(websocket_manager)
        
        # Register common trading configurations
        self.register_file("trading_params.json", {
            "initial_size": 1000,
            "add_type": "Equal",
            "reduce_percent": 50,
            "max_position_size": 10000,
            "stop_loss_percent": 2.0,
            "take_profit_percent": 5.0
        })
        
        self.register_file("ocr_settings.json", {
            "preprocessing": {
                "blur_kernel": 5,
                "threshold_value": 127,
                "dilate_iterations": 1,
                "erode_iterations": 1
            },
            "roi": {
                "x1": 64,
                "y1": 159,
                "x2": 681,
                "y2": 296
            },
            "confidence_threshold": 0.8
        })
        
        self.register_file("gui_settings.json", {
            "theme": "dark",
            "update_interval_ms": 100,
            "chart_timeframe": "1m",
            "show_notifications": True,
            "sound_alerts": False
        })