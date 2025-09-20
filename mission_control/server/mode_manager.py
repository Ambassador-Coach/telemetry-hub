"""
Mode Manager for Mission Control

Manages TESTRADE operational modes (LIVE, TANK, REPLAY, etc.)
with proper configuration updates and process orchestration.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any, Optional
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)


class ModeManager:
    """
    Manages TESTRADE operational modes and their configurations.
    Handles control.json updates and process selection for each mode.
    """
    
    def __init__(self):
        self.testrade_root = Path(__file__).parent.parent.parent
        self.control_json_path = self.testrade_root / "utils" / "control.json"
        self.backup_dir = self.testrade_root / "utils" / "control_backups"
        
        # Ensure backup directory exists
        self.backup_dir.mkdir(exist_ok=True)
        
        # Define mode configurations
        self.mode_configs = self._define_mode_configs()
        
        # Process dependencies and order
        self.process_dependencies = self._define_process_dependencies()
    
    def _define_mode_configs(self) -> Dict[str, Dict[str, Any]]:
        """Define configuration for each operational mode"""
        return {
            "LIVE": {
                "description": "Live trading with real money",
                "config_updates": {
                    "enable_intellisense_logging": True,
                    "ENABLE_IPC_DATA_DUMP": True,
                    "disable_paper_trading": True,
                    "disable_broker": False,
                    "enable_trading_signals": True,
                    "risk_max_position_size": 10000,
                    "risk_max_notional_per_trade": 25000.0,
                    "development_mode": False
                },
                "processes": [
                    "Redis",
                    "BabysitterService", 
                    "TelemetryService",
                    "Main",
                    "GUI"
                ],
                "warnings": [
                    "[EMOJI] LIVE TRADING MODE - Real money at risk!",
                    "[EMOJI] Ensure all risk parameters are properly configured",
                    "[EMOJI] Verify broker connection before starting"
                ]
            },
            
            "TANK_SEALED": {
                "description": "Isolated testing mode - no external connections",
                "config_updates": {
                    "enable_intellisense_logging": False,
                    "ENABLE_IPC_DATA_DUMP": False,
                    "disable_broker": True,
                    "disable_price_fetching_service": True,
                    "enable_trading_signals": True,
                    "development_mode": True
                },
                "processes": [
                    "Redis",
                    "Main"
                ],
                "warnings": []
            },
            
            "TANK_BROADCASTING": {
                "description": "Testing mode with full telemetry",
                "config_updates": {
                    "enable_intellisense_logging": True,
                    "ENABLE_IPC_DATA_DUMP": True,
                    "disable_broker": True,
                    "disable_price_fetching_service": False,
                    "enable_trading_signals": True,
                    "development_mode": True
                },
                "processes": [
                    "Redis",
                    "BabysitterService",
                    "TelemetryService", 
                    "Main",
                    "GUI"
                ],
                "warnings": []
            },
            
            "REPLAY": {
                "description": "Historical data replay mode",
                "config_updates": {
                    "enable_intellisense_logging": True,
                    "ENABLE_IPC_DATA_DUMP": True,
                    "disable_broker": True,
                    "disable_price_fetching_service": True,
                    "enable_trading_signals": True,
                    "development_mode": True,
                    "replay_mode": True
                },
                "processes": [
                    "Redis",
                    "BabysitterService",
                    "TelemetryService",
                    "Main",
                    "GUI"
                ],
                "warnings": [
                    "[CHART] Replay mode - Using historical data",
                    "[CHART] Ensure replay data files are available"
                ]
            },
            
            "PAPER": {
                "description": "Paper trading with simulated money",
                "config_updates": {
                    "enable_intellisense_logging": True,
                    "ENABLE_IPC_DATA_DUMP": True,
                    "disable_paper_trading": False,
                    "ALPACA_USE_PAPER_TRADING": True,
                    "disable_broker": False,
                    "enable_trading_signals": True,
                    "development_mode": False
                },
                "processes": [
                    "Redis",
                    "BabysitterService",
                    "TelemetryService",
                    "Main",
                    "GUI"
                ],
                "warnings": [
                    "[EMOJI] Paper trading mode - No real money",
                    "[EMOJI] Using paper trading credentials"
                ]
            },
            
            "SAFE_MODE": {
                "description": "Emergency mode - OCR only, no trading",
                "config_updates": {
                    "enable_intellisense_logging": True,
                    "ENABLE_IPC_DATA_DUMP": True,
                    "enable_trading_signals": False,  # Block signal generation
                    "emergency_mode_active": True,
                    "disable_broker": True,
                    "development_mode": True
                },
                "processes": [
                    "Redis",
                    "BabysitterService",
                    "TelemetryService",
                    "Main",
                    "GUI"
                ],
                "warnings": [
                    "[SHIELD] SAFE MODE - Trading signals blocked",
                    "[SHIELD] OCR running but no orders will be placed"
                ]
            }
        }
    
    def _define_process_dependencies(self) -> Dict[str, List[str]]:
        """Define process dependencies for startup order"""
        return {
            "Redis": [],  # No dependencies
            "BabysitterService": ["Redis"],
            "TelemetryService": ["Redis"],
            "Main": ["Redis", "BabysitterService"],  # Main needs babysitter
            "GUI": ["Redis", "BabysitterService", "Main"]  # GUI needs everything
        }
    
    def get_available_modes(self) -> List[Dict[str, str]]:
        """Get list of available modes with descriptions"""
        return [
            {
                "name": mode,
                "description": config["description"],
                "process_count": len(config["processes"]),
                "has_warnings": len(config.get("warnings", [])) > 0
            }
            for mode, config in self.mode_configs.items()
        ]
    
    def get_mode_config(self, mode: str) -> Optional[Dict[str, Any]]:
        """Get configuration for a specific mode"""
        return self.mode_configs.get(mode)
    
    def backup_control_json(self) -> str:
        """Backup current control.json before making changes"""
        if not self.control_json_path.exists():
            logger.warning("control.json not found, nothing to backup")
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"control_backup_{timestamp}.json"
        backup_path = self.backup_dir / backup_name
        
        try:
            shutil.copy2(self.control_json_path, backup_path)
            logger.info(f"Backed up control.json to {backup_name}")
            
            # Keep only last 10 backups
            self._cleanup_old_backups()
            
            return str(backup_path)
        except Exception as e:
            logger.error(f"Failed to backup control.json: {e}")
            return None
    
    def _cleanup_old_backups(self, keep_count: int = 10):
        """Keep only the most recent backups"""
        backups = sorted(self.backup_dir.glob("control_backup_*.json"))
        
        if len(backups) > keep_count:
            for backup in backups[:-keep_count]:
                try:
                    backup.unlink()
                    logger.debug(f"Deleted old backup: {backup.name}")
                except Exception as e:
                    logger.error(f"Failed to delete old backup: {e}")
    
    def apply_mode_config(self, mode: str) -> Dict[str, Any]:
        """Apply configuration changes for a specific mode"""
        if mode not in self.mode_configs:
            raise ValueError(f"Unknown mode: {mode}")
        
        # Backup current configuration
        backup_path = self.backup_control_json()
        
        try:
            # Read current configuration
            with open(self.control_json_path, 'r') as f:
                config = json.load(f)
            
            # Apply mode-specific updates
            mode_config = self.mode_configs[mode]
            config_updates = mode_config["config_updates"]
            
            for key, value in config_updates.items():
                config[key] = value
                logger.info(f"Updated {key} = {value}")
            
            # Add mode metadata
            config["TESTRADE_MODE"] = mode
            config["TESTRADE_MODE_UPDATED"] = datetime.now().isoformat()
            
            # Write updated configuration
            with open(self.control_json_path, 'w') as f:
                json.dump(config, f, indent=4)
            
            logger.info(f"Successfully applied {mode} configuration")
            
            return {
                "success": True,
                "mode": mode,
                "backup_path": backup_path,
                "updates_applied": len(config_updates)
            }
            
        except Exception as e:
            logger.error(f"Failed to apply mode configuration: {e}")
            
            # Try to restore backup
            if backup_path and Path(backup_path).exists():
                try:
                    shutil.copy2(backup_path, self.control_json_path)
                    logger.info("Restored control.json from backup")
                except:
                    pass
            
            raise
    
    def get_process_startup_order(self, processes: List[str]) -> List[str]:
        """Get the correct startup order for processes based on dependencies"""
        # Build dependency graph
        ordered = []
        remaining = set(processes)
        
        while remaining:
            # Find processes with no remaining dependencies
            ready = []
            for proc in remaining:
                deps = self.process_dependencies.get(proc, [])
                if all(dep in ordered or dep not in processes for dep in deps):
                    ready.append(proc)
            
            if not ready:
                # Circular dependency or missing dependency
                logger.warning(f"Could not resolve dependencies for: {remaining}")
                ordered.extend(sorted(remaining))  # Add remaining in alphabetical order
                break
            
            # Add ready processes to ordered list
            ready.sort()  # Consistent ordering
            ordered.extend(ready)
            remaining.difference_update(ready)
        
        return ordered
    
    def get_shutdown_order(self, processes: List[str]) -> List[str]:
        """Get shutdown order (reverse of startup order)"""
        startup_order = self.get_process_startup_order(processes)
        return list(reversed(startup_order))
    
    def validate_mode_requirements(self, mode: str) -> Dict[str, Any]:
        """Validate that all requirements for a mode are met"""
        results = {
            "valid": True,
            "errors": [],
            "warnings": []
        }
        
        mode_config = self.mode_configs.get(mode)
        if not mode_config:
            results["valid"] = False
            results["errors"].append(f"Unknown mode: {mode}")
            return results
        
        # Check control.json exists
        if not self.control_json_path.exists():
            results["valid"] = False
            results["errors"].append("control.json not found")
            return results
        
        # Mode-specific checks
        if mode == "LIVE":
            # Check broker credentials
            try:
                with open(self.control_json_path, 'r') as f:
                    config = json.load(f)
                
                if not config.get("ALPACA_API_KEY") or not config.get("ALPACA_API_SECRET"):
                    results["warnings"].append("Broker API credentials not configured")
                
                if config.get("development_mode", True):
                    results["warnings"].append("Development mode is enabled for LIVE trading")
                    
            except Exception as e:
                results["errors"].append(f"Failed to read configuration: {e}")
                results["valid"] = False
        
        elif mode == "REPLAY":
            # Check for replay data
            replay_dir = self.testrade_root / "data" / "replay"
            if not replay_dir.exists() or not list(replay_dir.glob("*.csv")):
                results["warnings"].append("No replay data files found in data/replay/")
        
        return results
    
    def get_current_mode(self) -> Optional[str]:
        """Get the currently configured mode from control.json"""
        try:
            with open(self.control_json_path, 'r') as f:
                config = json.load(f)
            return config.get("TESTRADE_MODE", "UNKNOWN")
        except Exception:
            return None