# gui/core/app_manager.py - Central Application Orchestrator

import asyncio
import logging
from fastapi import FastAPI

# Import all the components you've built
from core.app_state import AppState, AppConfig
from core.websocket_manager import WebSocketManager
from handlers.handler_registry import HandlerRegistry
from routes.route_manager import RouteManager
from services.redis_service import RedisService
from services.stream_service import StreamService
from utils.hot_reload_config_stub import HotReloadConfig
from utils.button_helper import ButtonHelper
from buttons.easy_buttons import setup_easy_buttons

logger = logging.getLogger(__name__)

class AppManager:
    """
    The central orchestrator for the GUI backend.
    Initializes all services and wires them together using dependency injection.
    """
    
    def __init__(self, config: AppConfig):
        self.config = config
        self.is_initialized = False
        self.background_tasks = []

        # --- Component Initialization ---
        # Each component is initialized here and dependencies are passed in.
        # This avoids global variables and makes the system predictable.

        # 1. Core State & Services (no dependencies)
        logger.info("1/5: Initializing core state and services...")
        self.app_state = AppState(self.config)
        
        # Prepare Redis config
        redis_config = {
            "REDIS_HOST": getattr(config, 'REDIS_HOST', 'localhost'),
            "REDIS_PORT": getattr(config, 'REDIS_PORT', 6379),
            "REDIS_DB": getattr(config, 'REDIS_DB', 0),
            "REDIS_PASSWORD": getattr(config, 'REDIS_PASSWORD', None)
        }
        self.redis_service = RedisService(redis_config)

        # 2. Managers & Handlers (depend on core services)
        logger.info("2/5: Initializing managers and handlers...")
        self.websocket_manager = WebSocketManager(self.app_state)
        self.handler_registry = HandlerRegistry(self.app_state, self.websocket_manager, self.redis_service)
        self.hot_reload_config = HotReloadConfig(self.websocket_manager)

        # 3. High-Level Services (depend on managers)
        # Note: The original message_service.py was very simple; its logic can live
        # directly in the handlers and websocket_manager, so it's omitted for simplicity.
        logger.info("3/5: Initializing high-level services...")
        self.stream_service = StreamService(self.app_state, self.redis_service, self.handler_registry)

        # 4. API and UI Components (depend on everything)
        logger.info("4/5: Initializing UI components...")
        self.button_helper = ButtonHelper(self.app_state, self.websocket_manager, self.redis_service)
        # This function call will register all your buttons
        setup_easy_buttons(self.button_helper, self.hot_reload_config)

        # 5. Route Manager (last, to get all other components)
        logger.info("5/5: Preparing route manager...")
        self.route_manager = None # Will be created in setup_routes

    async def initialize(self):
        """Perform asynchronous initialization for all components."""
        if self.is_initialized:
            return

        logger.info("--- Starting AppManager Initialization ---")
        self.app_state.main_event_loop = asyncio.get_running_loop()
        
        # Initialize services that require async setup
        await self.redis_service.initialize()
        await self.hot_reload_config.start_watching()

        # Start the Redis stream consumer
        await self.stream_service.start_consumers()

        self.is_initialized = True
        logger.info("--- ✅ AppManager Initialization Complete ---")

    def setup_routes(self, app: FastAPI):
        """Creates the RouteManager and sets up all FastAPI endpoints."""
        if self.route_manager:
            return
            
        logger.info("Setting up API routes...")
        # The RouteManager gets access to all initialized components
        self.route_manager = RouteManager(
            app=app,
            app_state=self.app_state,
            websocket_manager=self.websocket_manager,
            redis_service=self.redis_service,
            handler_registry=self.handler_registry
        )
        self.route_manager.setup_all_routes()
        logger.info("✅ API routes configured.")

    async def shutdown(self):
        """Gracefully shut down all components."""
        logger.info("--- Starting AppManager Shutdown ---")
        
        # Stop background tasks first
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)

        # Shut down services in reverse order of initialization
        await self.stream_service.stop_all_consumers()
        await self.websocket_manager.close_all_connections()
        self.hot_reload_config.stop_watching()
        await self.redis_service.shutdown()
        
        self.is_initialized = False
        logger.info("--- ✅ AppManager Shutdown Complete ---")