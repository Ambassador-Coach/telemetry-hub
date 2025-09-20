# Stub version of hot_reload_config for testing without watchdog

import logging

logger = logging.getLogger(__name__)

class HotReloadConfig:
    """Stub version that doesn't use file watching"""
    
    def __init__(self, websocket_manager):
        self.websocket_manager = websocket_manager
        logger.info("📄 HotReloadConfig stub initialized (no file watching)")
        
    async def start_watching(self):
        """Stub - no actual watching"""
        logger.info("📄 HotReloadConfig stub: start_watching called")
        
    def stop_watching(self):
        """Stub - no actual watching"""
        logger.info("📄 HotReloadConfig stub: stop_watching called")

class TradingConfigManager:
    """Stub config manager"""
    
    def __init__(self):
        logger.info("📄 TradingConfigManager stub initialized")