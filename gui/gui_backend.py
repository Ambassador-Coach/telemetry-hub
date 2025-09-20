# gui/gui_backend.py - Main Application (INDEPENDENT)
import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import refactored modules (all independent)
from core.app_manager import AppManager
from core.app_state import AppConfig

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(title="TESTRADE Pro API", version="2025.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=os.path.dirname(__file__)), name="static")

# Global app manager - fully independent
app_manager = AppManager(AppConfig())

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - handles startup and shutdown"""
    logger.info("🚀 TESTRADE Pro API Backend starting up...")
    
    # Startup
    try:
        await app_manager.initialize()
        
        # Make app manager available to routes
        app.state.app_manager = app_manager
        
        logger.info("✅ GUI Backend startup complete")
        logger.info("🌐 Ready for connections at http://localhost:8001")
        
    except Exception as e:
        logger.critical(f"❌ Startup failed: {e}", exc_info=True)
        raise
    
    yield  # Application runs here
    
    # Shutdown
    logger.info("🔄 Shutting down TESTRADE GUI Backend...")
    
    try:
        await app_manager.shutdown()
        logger.info("✅ Shutdown complete")
        
    except Exception as e:
        logger.error(f"❌ Shutdown error: {e}", exc_info=True)

# Include all routes through the app manager
app_manager.setup_routes(app)

# Assign lifespan manager
app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run("gui_backend:app", host="0.0.0.0", port=8001, reload=False, log_level="info")