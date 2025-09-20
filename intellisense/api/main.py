"""
FastAPI Application and Routes for IntelliSense API

This module defines the main FastAPI application with all routes for
controlling and monitoring IntelliSense test sessions.
"""

import logging
import time
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
import uuid

# REMOVED: Direct imports of internal modules that cause circular dependencies
# These will be imported lazily when needed

# Import interface for type hints
from intellisense.controllers.interfaces import IIntelliSenseMasterController

# Import required enums and exceptions for delete_session functionality
try:
    from intellisense.core.enums import TestStatus
    from intellisense.core.exceptions import IntelliSenseSessionError
except ImportError:
    # Fallback for development - define minimal placeholders
    class TestStatus:
        RUNNING = "RUNNING"

    class IntelliSenseSessionError(Exception):
        pass

# Import API Schemas - these should be available
from intellisense.api.schemas import (
    TestSessionCreateRequest, SessionCreationResponse,
    SessionSummaryResponse, SessionStatusDetailResponse, ActionResponse,
    SessionResultsSummaryResponse, ErrorResponse, HealthCheckResponse
)

logger = logging.getLogger(__name__)

# --- Application State ---
class AppState:
    """Manages application state including the master controller."""
    def __init__(self):
        self.master_controller: Optional['IIntelliSenseMasterController'] = None
        self.initialized = False
        # Note: active_sessions is no longer needed - the master controller manages sessions internally

# Create global app state instance
app_state = AppState()

def get_or_create_master_controller():
    """Get the singleton master controller or create it if it doesn't exist."""
    if app_state.master_controller is None:
        from intellisense.controllers.master_controller_redis import IntelliSenseMasterControllerRedis
        from intellisense.core.enums import OperationMode
        from intellisense.core.types import IntelliSenseConfig, TestSessionConfig, OCRTestConfig, PriceTestConfig, BrokerTestConfig
        
        logger.info("Creating singleton IntelliSenseMasterControllerRedis...")
        
        redis_config = {
            "host": "172.22.202.120",
            "port": 6379,
            "db": 0
        }
        
        # Create a default config for the controller
        default_session_config = TestSessionConfig(
            session_name="default",
            description="Default configuration",
            test_date="2024-01-01",
            controlled_trade_log_path="",
            historical_price_data_path_sync="",
            broker_response_log_path="",
            duration_minutes=60,
            speed_factor=1.0,
            ocr_config=OCRTestConfig(),
            price_config=PriceTestConfig(),
            broker_config=BrokerTestConfig(),
            output_directory="./intellisense_sessions"
        )
        
        default_intellisense_config = IntelliSenseConfig(
            test_session_config=default_session_config,
            performance_monitoring_active=True,
            real_time_dashboard_enabled=True
        )
        
        app_state.master_controller = IntelliSenseMasterControllerRedis(
            operation_mode=OperationMode.INTELLISENSE,
            intellisense_config=default_intellisense_config,
            redis_config=redis_config
        )
        app_state.initialized = True
        
    return app_state.master_controller

# --- MCP Integration ---
# Global MCP service instances
_mcp_services_initialized = False

def initialize_mcp_services(app: FastAPI, master_controller: IIntelliSenseMasterController):
    """Initialize MCP services and add routes to the application"""
    global _mcp_services_initialized
    
    if _mcp_services_initialized:
        return
    
    try:
        # Import MCP components
        from intellisense.mcp.config import DEFAULT_MCP_CONFIG
        from intellisense.mcp.client import IntelliSenseMCPClient, LLMAnalysisService
        from intellisense.mcp.server import IntelliSenseMCPServer
        from intellisense.mcp.redis_bridge import RedisMCPBridge, create_default_analysis_rules
        from intellisense.mcp.api_integration import add_mcp_routes_to_app
        
        # Initialize MCP client
        mcp_client = IntelliSenseMCPClient(DEFAULT_MCP_CONFIG.external_llm_endpoints)
        
        # Initialize MCP server (requires Redis client)
        redis_client = None  # Would get from application core
        mcp_server = IntelliSenseMCPServer(master_controller, DEFAULT_MCP_CONFIG, redis_client)
        
        # Initialize Redis bridge
        analysis_rules = create_default_analysis_rules()
        redis_bridge = RedisMCPBridge(
            redis_client, 
            mcp_client, 
            DEFAULT_MCP_CONFIG.monitored_streams,
            analysis_rules
        )
        
        # Initialize LLM analysis service
        analysis_service = LLMAnalysisService(mcp_client)
        
        # Add MCP routes to the application
        add_mcp_routes_to_app(app, mcp_client, mcp_server, redis_bridge, analysis_service)
        
        _mcp_services_initialized = True
        logger.info("MCP services initialized and routes added")
        
    except ImportError as e:
        logger.warning(f"MCP services not available: {e}")
    except Exception as e:
        logger.error(f"Failed to initialize MCP services: {e}")

# --- Application Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan event handler."""
    # Startup
    logger.info("IntelliSense API starting up...")
    yield
    # Shutdown
    logger.info("IntelliSense API shutting down...")

# --- IntelliSense API Application ---
app = FastAPI(
    title="IntelliSense API",
    description="API for controlling and monitoring IntelliSense test sessions.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Add CORS middleware for web frontend integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Lazy Loading Controller Dependencies ---
_master_controller_instance = None
_controller_import_attempted = False

def get_master_controller():
    """Lazy-load the master controller to avoid circular imports."""
    global _master_controller_instance, _controller_import_attempted

    if _master_controller_instance is None and not _controller_import_attempted:
        _controller_import_attempted = True
        try:
            # Lazy import to prevent circular dependencies
            from intellisense.controllers.master_controller import IntelliSenseMasterController
            logger.info("Successfully imported IntelliSenseMasterController")
            # Don't instantiate automatically - wait for explicit setup
            _master_controller_instance = None
        except ImportError as e:
            logger.warning(f"Controller import failed: {e}")
            _master_controller_instance = None

    if _master_controller_instance is None:
        raise HTTPException(
            status_code=503,
            detail="MasterController not available. Service not properly initialized or dependencies missing."
        )

    return _master_controller_instance

def set_master_controller(controller):
    """Set the master controller instance for the API."""
    global _master_controller_instance
    _master_controller_instance = controller
    logger.info("API: MasterController instance set successfully.")

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Custom HTTP exception handler."""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": f"HTTP {exc.status_code}",
            "message": exc.detail
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """General exception handler for unhandled exceptions."""
    logger.error(f"API: Unhandled exception: {exc}", exc_info=True)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": "An unexpected error occurred. Please check the server logs."
        }
    )

# --- Health Check and Diagnostic Endpoints ---
@app.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """Health check endpoint."""
    return HealthCheckResponse(
        status="healthy",
        timestamp=time.time()
    )

@app.get("/debug/imports")
async def debug_imports():
    """Diagnostic endpoint for import tracing."""
    import sys

    # Check which IntelliSense modules are loaded
    intellisense_modules = {
        name: str(module) for name, module in sys.modules.items()
        if name.startswith('intellisense')
    }

    return {
        "total_modules_loaded": len(sys.modules),
        "intellisense_modules": intellisense_modules,
        "python_path": sys.path[:5],  # First 5 entries
        "controller_import_attempted": _controller_import_attempted,
        "controller_available": _master_controller_instance is not None
    }

# --- API Endpoints ---

@app.post("/sessions/", response_model=SessionCreationResponse, status_code=status.HTTP_201_CREATED)
async def create_new_session(
    session_create_request: TestSessionCreateRequest
):
    """Creates a new IntelliSense test session and its underlying core engine."""
    logger.info(f"API: Received request to create session: {session_create_request.session_name}")
    try:
        # Lazy import to avoid circular dependencies at module level
        from intellisense.controllers.master_controller_redis import IntelliSenseMasterControllerRedis
        from intellisense.core.enums import OperationMode
        from intellisense.core.types import IntelliSenseConfig, TestSessionConfig

        # Create the TestSessionConfig from the request
        request_dict = session_create_request.model_dump()
        # Remove fields that exist in API schema but not in actual TestSessionConfig dataclass
        request_dict.pop('injection_plan_path', None)
        session_config = TestSessionConfig(**request_dict)
        
        # Get the singleton master controller
        master_controller = get_or_create_master_controller()
        
        # Update the controller's config for this session
        intellisense_config = IntelliSenseConfig(test_session_config=session_config)
        master_controller.intellisense_config = intellisense_config
        
        # Create the test session in the controller
        session_id = master_controller.create_test_session(session_config)

        logger.info(f"API: Session '{session_id}' created and controller instance stored.")
        
        return SessionCreationResponse(
            session_id=session_id,
            status="created",
            message="Test session and core engine created successfully."
        )
    except Exception as e:
        logger.error(f"API: Error creating session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

@app.get("/sessions/", response_model=List[SessionSummaryResponse])
async def list_all_sessions():
    """Lists all registered IntelliSense test sessions with summary information."""
    logger.debug("API: Received request to list all sessions.")
    try:
        # Get the singleton master controller
        master_controller = get_or_create_master_controller()
        
        # Get all sessions from the controller
        sessions_data = master_controller.list_sessions()
        response_list = []
        
        for sess_data in sessions_data:
            if isinstance(sess_data, dict):
                # Handle dictionary response
                response_list.append(SessionSummaryResponse(**sess_data))
            else:
                # Handle TestSession object if available
                response_list.append(SessionSummaryResponse(
                    session_id=sess_data.session_id,
                    session_name=sess_data.config.session_name if hasattr(sess_data, 'config') else sess_data.session_name,
                    status=sess_data.status.value if hasattr(sess_data.status, 'value') else sess_data.status,
                    test_date=sess_data.config.test_date if hasattr(sess_data, 'config') else sess_data.test_date,
                    duration_minutes=sess_data.config.duration_minutes if hasattr(sess_data, 'config') else sess_data.duration_minutes,
                    created_at=sess_data.created_at
                ))
        
        return response_list
    except Exception as e:
        logger.error(f"API: Error listing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

@app.get("/sessions/{session_id}/status", response_model=SessionStatusDetailResponse)
async def get_session_details(
    session_id: str
):
    """Retrieves the detailed status and configuration of a specific test session."""
    logger.debug(f"API: Received request for session status: {session_id}")
    
    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    session_data = master_controller.get_session_status(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")
    
    if isinstance(session_data, dict):
        # Handle dictionary response
        return SessionStatusDetailResponse(**session_data)
    else:
        # Handle TestSession object
        return SessionStatusDetailResponse(
            session_id=session_data.session_id,
            session_name=session_data.config.session_name if hasattr(session_data, 'config') else session_data.session_name,
            status=session_data.status.value if hasattr(session_data.status, 'value') else session_data.status,
            test_date=session_data.config.test_date if hasattr(session_data, 'config') else session_data.test_date,
            duration_minutes=session_data.config.duration_minutes if hasattr(session_data, 'config') else session_data.duration_minutes,
            created_at=session_data.created_at,
            started_at=getattr(session_data, 'started_at', None),
            completed_at=getattr(session_data, 'completed_at', None),
            progress_percent=getattr(session_data, 'progress_percent', 0.0),
            current_phase=getattr(session_data, 'current_phase', None),
            error_count=len(getattr(session_data, 'error_messages', []))
        )

@app.post("/sessions/{session_id}/load_data", response_model=ActionResponse)
async def load_data_for_session(
    session_id: str
):
    """Triggers the data loading process for a created test session."""
    logger.info(f"API: Received request to load data for session: {session_id}")

    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    # Check if session exists
    if not master_controller.get_session_status(session_id):
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    try:
        success = master_controller.load_session_data(session_id)
        if success:
            return ActionResponse(
                session_id=session_id,
                action="load_data",
                status="initiated",
                message="Data loading process initiated."
            )
        else:
            raise HTTPException(
                status_code=500,
                detail="Failed to initiate data loading (see server logs)."
            )
    except Exception as e:
        logger.error(f"API: Error initiating data load for {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error initiating data load: {e}")

@app.post("/sessions/{session_id}/start_replay", response_model=ActionResponse)
async def start_session_replay(
    session_id: str
):
    """Starts the replay for a prepared test session."""
    logger.info(f"API: Received request to start replay for session: {session_id}")

    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    if not master_controller.get_session_status(session_id):
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    try:
        success = master_controller.start_replay_session(session_id)
        if success:
            return ActionResponse(
                session_id=session_id,
                action="start_replay",
                status="initiated",
                message="Replay initiated."
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="Failed to start replay (e.g., not ready, already running, or other error)."
            )
    except Exception as e:
        logger.error(f"API: Error starting replay for {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting replay: {e}")

@app.post("/sessions/{session_id}/stop_replay", response_model=ActionResponse)
async def stop_session_replay(
    session_id: str
):
    """Stops an active replay session."""
    logger.info(f"API: Received request to stop replay for session: {session_id}")

    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    if not master_controller.get_session_status(session_id):
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    try:
        success = master_controller.stop_replay_session(session_id)
        if success:
            return ActionResponse(
                session_id=session_id,
                action="stop_replay",
                status="initiated",
                message="Stop replay request processed."
            )
        else:
            raise HTTPException(
                status_code=409,
                detail="Failed to stop replay (e.g., not running or other error)."
            )
    except Exception as e:
        logger.error(f"API: Error stopping replay for {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error stopping replay: {e}")

@app.get("/sessions/{session_id}/results/summary", response_model=SessionResultsSummaryResponse)
async def get_session_results_summary(
    session_id: str
):
    """Retrieves a summary of the execution results for a completed/stopped test session."""
    logger.debug(f"API: Received request for results summary: {session_id}")

    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    session_data = master_controller.get_session_status(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    # Check if session has completed
    session_status = session_data.status if hasattr(session_data, 'status') else session_data.get('status')
    if hasattr(session_status, 'value'):
        status_value = session_status.value
    else:
        status_value = session_status

    completed_statuses = ["COMPLETED", "STOPPED", "FAILED", "ERROR"]
    if status_value not in completed_statuses:
        raise HTTPException(
            status_code=409,
            detail=f"Session '{session_id}' has not completed. Current status: {status_value}"
        )

    # Get results summary from controller
    try:
        results_summary = master_controller.get_session_results_summary(session_id)
        if results_summary:
            if isinstance(results_summary, dict):
                return SessionResultsSummaryResponse(**results_summary)
            else:
                # Handle object response
                return SessionResultsSummaryResponse(
                    session_id=results_summary.session_id,
                    session_name=results_summary.session_name,
                    overall_status=results_summary.overall_status,
                    total_steps=getattr(results_summary, 'total_steps', None),
                    steps_with_validation_failures=getattr(results_summary, 'steps_with_validation_failures', None),
                    key_latencies_summary=getattr(results_summary, 'key_latencies_summary', None)
                )
        else:
            # Fallback basic summary
            return SessionResultsSummaryResponse(
                session_id=session_id,
                session_name=session_data.session_name if hasattr(session_data, 'session_name') else session_data.get('session_name', 'Unknown'),
                overall_status=status_value,
                key_latencies_summary={"info": "No detailed results available"}
            )
    except Exception as e:
        logger.error(f"API: Error getting results summary for {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error retrieving results summary: {e}")

# --- Additional Utility Endpoints ---

@app.get("/sessions/{session_id}/progress")
async def get_session_progress(
    session_id: str
):
    """Get real-time progress information for a session."""
    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    session_data = master_controller.get_session_status(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    return {
        "session_id": session_id,
        "progress_percent": getattr(session_data, 'progress_percent', 0.0),
        "current_phase": getattr(session_data, 'current_phase', 'unknown'),
        "is_running": getattr(session_data, 'status', 'unknown') == 'RUNNING',
        "last_update": time.time()
    }

@app.delete("/sessions/{session_id}", response_model=ActionResponse)
async def delete_session_endpoint(session_id: str):
    """Shuts down a test session's core engine and deletes the session."""
    logger.info(f"API: Received request to delete session: {session_id}")
    
    # Get the singleton master controller
    master_controller = get_or_create_master_controller()
    
    # Check if session exists
    if not master_controller.get_session_status(session_id):
        raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

    try:
        # The master controller manages session deletion internally
        logger.info(f"API: Deleting session '{session_id}'...")
        success = master_controller.delete_session(session_id)
        
        if success:
            logger.info(f"API: Session '{session_id}' successfully deleted.")

            return ActionResponse(
                session_id=session_id,
                action="delete_session",
                status="deleted",
                message=f"Session '{session_id}' and its core engine have been shut down and cleaned up."
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete session '{session_id}'."
            )
    except Exception as e:
        logger.error(f"API: Unexpected error deleting session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error deleting session: {e}")

# --- MCP Server Management Endpoints ---

@app.get("/mcp/status", response_model=Dict[str, Any])
async def get_mcp_status():
    """Get MCP server status and configuration."""
    logger.debug("API: Received request for MCP server status")
    
    try:
        # Check if MCP is available
        try:
            from mcp import Server
            mcp_available = True
        except ImportError:
            mcp_available = False
            
        master_controller = get_or_create_master_controller()
        
        return {
            "mcp_available": mcp_available,
            "mcp_server_enabled": hasattr(master_controller, 'mcp_server') and master_controller.mcp_server is not None,
            "mcp_config": {
                "server_name": "intellisense-trading",
                "server_version": "1.0.0",
                "server_port": 8003,
                "monitored_streams": [
                    "testrade:cleaned-ocr-snapshots",
                    "testrade:market-data",
                    "testrade:order-events",
                    "testrade:trade-lifecycle",
                    "testrade:risk-events"
                ]
            }
        }
    except Exception as e:
        logger.error(f"API: Error getting MCP status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error getting MCP status: {e}")

@app.post("/mcp/test-connection", response_model=Dict[str, Any])
async def test_mcp_connection():
    """Test MCP server connection and run diagnostics."""
    logger.debug("API: Testing MCP server connection")
    
    try:
        from intellisense.mcp.server import IntelliSenseMCPServer
        from intellisense.mcp.config import MCPConfig
        import redis
        
        # Get master controller
        master_controller = get_or_create_master_controller()
        
        # Initialize Redis client
        redis_config = {
            "host": "172.22.202.120",
            "port": 6379,
            "db": 0
        }
        redis_client = redis.Redis(**redis_config, decode_responses=True)
        
        # Create MCP server instance
        mcp_config = MCPConfig()
        mcp_server = IntelliSenseMCPServer(
            master_controller=master_controller,
            config=mcp_config,
            redis_client=redis_client
        )
        
        # Run diagnostics
        diagnostics = await mcp_server._run_system_diagnostics({
            "include_redis_connectivity": True,
            "include_service_health": True,
            "include_recent_errors": True
        })
        
        return {
            "connection_test": "successful",
            "diagnostics": diagnostics
        }
        
    except Exception as e:
        logger.error(f"API: MCP connection test failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"MCP connection test failed: {e}")

@app.get("/mcp/tools", response_model=Dict[str, Any])
async def list_mcp_tools():
    """List all available MCP tools and their descriptions."""
    logger.debug("API: Listing MCP tools")
    
    try:
        from intellisense.mcp.server import IntelliSenseMCPServer
        from intellisense.mcp.config import MCPConfig
        
        # Create a temporary MCP server instance to get tool definitions
        mcp_server = IntelliSenseMCPServer(None, MCPConfig(), None)
        
        tools = {
            "session_management": mcp_server._get_session_management_tools(),
            "trading_analysis": mcp_server._get_trading_analysis_tools(),
            "redis_queries": mcp_server._get_redis_query_tools(),
            "performance_metrics": mcp_server._get_performance_tools(),
            "risk_analysis": mcp_server._get_risk_analysis_tools(),
            "market_data": mcp_server._get_market_data_tools(),
            "system_diagnostics": mcp_server._get_diagnostic_tools()
        }
        
        # Convert Tool objects to dicts
        tools_dict = {}
        for category, tool_list in tools.items():
            tools_dict[category] = [
                {
                    "name": tool.name if hasattr(tool, 'name') else str(tool),
                    "description": tool.description if hasattr(tool, 'description') else "",
                    "inputSchema": tool.inputSchema if hasattr(tool, 'inputSchema') else {}
                }
                for tool in tool_list
            ]
        
        return {
            "mcp_tools": tools_dict,
            "tool_count": sum(len(tools) for tools in tools_dict.values())
        }
        
    except Exception as e:
        logger.error(f"API: Error listing MCP tools: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error listing MCP tools: {e}")

# --- Application Startup and Shutdown ---
# (Moved to lifespan handler above)

# --- Main Application Entry Point ---

def create_app(master_controller: Optional[IIntelliSenseMasterController] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if master_controller:
        set_master_controller(master_controller)
    return app

# Example usage for running the API
if __name__ == "__main__":
    import uvicorn

    # API server now starts in lightweight mode without automatically launching Core Engine
    logger.info("Starting IntelliSense API server in lightweight mode...")
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")
