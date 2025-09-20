"""
FastAPI Integration for MCP Services

Adds MCP endpoints to the existing IntelliSense FastAPI application.
"""

import json
import logging
import time
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field

# Import existing FastAPI dependencies
from intellisense.controllers.interfaces import IIntelliSenseMasterController
from intellisense.mcp.client import IntelliSenseMCPClient, LLMAnalysisService
from intellisense.mcp.server import IntelliSenseMCPServer
from intellisense.mcp.redis_bridge import RedisMCPBridge
from intellisense.mcp.config import MCPConfig

logger = logging.getLogger(__name__)


# Pydantic models for MCP API requests/responses
class MCPAnalysisRequest(BaseModel):
    """Request model for MCP-powered analysis"""
    analysis_type: str = Field(..., description="Type of analysis to perform")
    include_context: bool = Field(True, description="Include contextual data")
    time_range_minutes: Optional[int] = Field(60, description="Time range for analysis")
    filters: Optional[Dict[str, Any]] = Field(None, description="Additional filters")


class MCPNaturalLanguageQuery(BaseModel):
    """Request model for natural language queries"""
    query: str = Field(..., description="Natural language question")
    session_id: Optional[str] = Field(None, description="Session context")
    include_recent_data: bool = Field(True, description="Include recent trading data")


class MCPAnalysisResponse(BaseModel):
    """Response model for MCP analysis"""
    session_id: Optional[str] = None
    analysis_type: str
    result: Dict[str, Any]
    llm_insights: Optional[Dict[str, Any]] = None
    timestamp: float
    processing_time_ms: float


class MCPQueryResponse(BaseModel):
    """Response model for natural language queries"""
    query: str
    answer: str
    confidence: Optional[float] = None
    sources: Optional[List[str]] = None
    timestamp: float


class MCPHealthResponse(BaseModel):
    """Response model for MCP health check"""
    mcp_server_status: str
    mcp_client_status: str
    redis_bridge_status: str
    llm_endpoints: List[Dict[str, Any]]
    last_analysis_time: Optional[float] = None


class MCPStatsResponse(BaseModel):
    """Response model for MCP statistics"""
    server_stats: Dict[str, Any]
    client_stats: Dict[str, Any]
    bridge_stats: Dict[str, Any]
    uptime_seconds: float


# Global MCP service instances (will be injected)
_mcp_client: Optional[IntelliSenseMCPClient] = None
_mcp_server: Optional[IntelliSenseMCPServer] = None
_redis_bridge: Optional[RedisMCPBridge] = None
_llm_analysis_service: Optional[LLMAnalysisService] = None


def set_mcp_services(client: IntelliSenseMCPClient,
                    server: IntelliSenseMCPServer,
                    bridge: RedisMCPBridge,
                    analysis_service: LLMAnalysisService):
    """Set the global MCP service instances"""
    global _mcp_client, _mcp_server, _redis_bridge, _llm_analysis_service
    _mcp_client = client
    _mcp_server = server
    _redis_bridge = bridge
    _llm_analysis_service = analysis_service
    logger.info("MCP services set for API integration")


def get_mcp_client() -> IntelliSenseMCPClient:
    """Get the MCP client instance"""
    if _mcp_client is None:
        raise HTTPException(status_code=503, detail="MCP client not available")
    return _mcp_client


def get_llm_analysis_service() -> LLMAnalysisService:
    """Get the LLM analysis service"""
    if _llm_analysis_service is None:
        raise HTTPException(status_code=503, detail="LLM analysis service not available")
    return _llm_analysis_service


def get_redis_bridge() -> RedisMCPBridge:
    """Get the Redis bridge"""
    if _redis_bridge is None:
        raise HTTPException(status_code=503, detail="Redis bridge not available")
    return _redis_bridge


# Create the MCP router
mcp_router = APIRouter(prefix="/mcp", tags=["MCP LLM Integration"])


@mcp_router.get("/health", response_model=MCPHealthResponse)
async def mcp_health_check():
    """Check health of all MCP services"""
    try:
        # Check MCP client connectivity
        client_status = "healthy"
        llm_endpoints = []
        
        if _mcp_client:
            connectivity_test = await _mcp_client.test_connectivity()
            client_status = "healthy" if connectivity_test["summary"]["healthy"] > 0 else "unhealthy"
            llm_endpoints = [
                {
                    "url": endpoint,
                    "status": connectivity_test["endpoints"].get(endpoint, {}).get("status", "unknown")
                }
                for endpoint in _mcp_client.llm_endpoints
            ]
        
        # Check server status
        server_status = "healthy" if _mcp_server else "not_initialized"
        
        # Check bridge status
        bridge_status = "healthy" if _redis_bridge and _redis_bridge.is_running else "stopped"
        
        return MCPHealthResponse(
            mcp_server_status=server_status,
            mcp_client_status=client_status,
            redis_bridge_status=bridge_status,
            llm_endpoints=llm_endpoints
        )
        
    except Exception as e:
        logger.error(f"MCP health check failed: {e}")
        raise HTTPException(status_code=500, detail=f"Health check failed: {e}")


@mcp_router.get("/stats", response_model=MCPStatsResponse)
async def get_mcp_statistics():
    """Get comprehensive MCP service statistics"""
    try:
        server_stats = {"status": "not_available"}
        client_stats = {"status": "not_available"}
        bridge_stats = {"status": "not_available"}
        
        if _mcp_server:
            # Server stats would depend on server implementation
            server_stats = {"status": "running", "tools_registered": "unknown"}
        
        if _mcp_client:
            client_stats = _mcp_client.get_endpoint_status()
        
        if _redis_bridge:
            bridge_stats = _redis_bridge.get_bridge_stats()
        
        return MCPStatsResponse(
            server_stats=server_stats,
            client_stats=client_stats,
            bridge_stats=bridge_stats,
            uptime_seconds=bridge_stats.get("uptime_seconds", 0)
        )
        
    except Exception as e:
        logger.error(f"Failed to get MCP stats: {e}")
        raise HTTPException(status_code=500, detail=f"Stats retrieval failed: {e}")


@mcp_router.post("/sessions/{session_id}/analyze", response_model=MCPAnalysisResponse)
async def analyze_session_with_llm(
    session_id: str,
    request: MCPAnalysisRequest,
    controller: IIntelliSenseMasterController = Depends(lambda: None),  # Will be injected
    analysis_service: LLMAnalysisService = Depends(get_llm_analysis_service)
):
    """Perform LLM-powered analysis of a trading session"""
    start_time = time.time()
    
    try:
        # Get session data
        if controller:
            session_data = controller.get_session_status(session_id)
            if not session_data:
                raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        else:
            # Fallback for testing
            session_data = {"session_id": session_id, "status": "test_mode"}
        
        # Add request context
        analysis_context = {
            "session_data": session_data,
            "analysis_type": request.analysis_type,
            "time_range_minutes": request.time_range_minutes,
            "filters": request.filters or {},
            "include_context": request.include_context
        }
        
        # Perform analysis based on type
        if request.analysis_type == "comprehensive":
            result = await analysis_service.analyze_session_comprehensive(analysis_context)
        elif request.analysis_type == "performance":
            mcp_client = get_mcp_client()
            result = await mcp_client.analyze_trading_performance(analysis_context)
        elif request.analysis_type == "risk":
            mcp_client = get_mcp_client()
            result = await mcp_client.analyze_risk_patterns(
                analysis_context.get("risk_events", []),
                analysis_context.get("position_data")
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown analysis type: {request.analysis_type}")
        
        processing_time_ms = (time.time() - start_time) * 1000
        
        return MCPAnalysisResponse(
            session_id=session_id,
            analysis_type=request.analysis_type,
            result=analysis_context,
            llm_insights=result,
            timestamp=time.time(),
            processing_time_ms=processing_time_ms
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LLM analysis failed for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e}")


@mcp_router.post("/query", response_model=MCPQueryResponse)
async def natural_language_query(
    query_request: MCPNaturalLanguageQuery,
    analysis_service: LLMAnalysisService = Depends(get_llm_analysis_service)
):
    """Process natural language queries about trading data"""
    try:
        # Prepare context data
        context_data = {}
        
        if query_request.session_id:
            # Would get session-specific context
            context_data["session_id"] = query_request.session_id
        
        if query_request.include_recent_data:
            # Would include recent trading data
            context_data["recent_data"] = "placeholder_for_recent_data"
        
        # Process the query
        answer = await analysis_service.answer_trading_question(
            query_request.query,
            context_data
        )
        
        return MCPQueryResponse(
            query=query_request.query,
            answer=answer,
            timestamp=time.time()
        )
        
    except Exception as e:
        logger.error(f"Natural language query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Query processing failed: {e}")


@mcp_router.post("/sessions/{session_id}/insights")
async def generate_trading_insights(
    session_id: str,
    background_tasks: BackgroundTasks,
    mcp_client: IntelliSenseMCPClient = Depends(get_mcp_client)
):
    """Generate trading insights for a session in the background"""
    try:
        # Add background task for insight generation
        background_tasks.add_task(
            _generate_insights_background,
            session_id,
            mcp_client
        )
        
        return {
            "session_id": session_id,
            "message": "Insight generation started in background",
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Failed to start insight generation for {session_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Insight generation failed: {e}")


@mcp_router.get("/redis/streams")
async def get_redis_stream_status(
    bridge: RedisMCPBridge = Depends(get_redis_bridge)
):
    """Get status of monitored Redis streams"""
    try:
        stats = bridge.get_bridge_stats()
        
        return {
            "monitoring_active": bridge.is_running,
            "monitored_streams": bridge.monitored_streams,
            "analysis_rules": len(bridge.analysis_rules),
            "events_processed": stats["events_processed"],
            "analyses_triggered": stats["llm_analyses_triggered"],
            "last_stream_ids": stats["last_stream_ids"]
        }
        
    except Exception as e:
        logger.error(f"Failed to get Redis stream status: {e}")
        raise HTTPException(status_code=500, detail=f"Stream status retrieval failed: {e}")


@mcp_router.post("/redis/streams/{stream_name}/query")
async def query_redis_stream(
    stream_name: str,
    count: int = 10,
    start_id: str = "-",
    bridge: RedisMCPBridge = Depends(get_redis_bridge)
):
    """Query data from a specific Redis stream"""
    try:
        if count > 100:
            raise HTTPException(status_code=400, detail="Count cannot exceed 100")
        
        events = await bridge.query_stream_history(stream_name, start_id, "+", count)
        
        return {
            "stream_name": stream_name,
            "event_count": len(events),
            "events": [
                {
                    "event_id": event.event_id,
                    "timestamp": event.timestamp,
                    "correlation_id": event.correlation_id,
                    "event_type": event.event_type,
                    "data": event.data
                }
                for event in events
            ]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to query Redis stream {stream_name}: {e}")
        raise HTTPException(status_code=500, detail=f"Stream query failed: {e}")


@mcp_router.post("/optimization/suggestions")
async def get_optimization_suggestions(
    session_id: Optional[str] = None,
    mcp_client: IntelliSenseMCPClient = Depends(get_mcp_client)
):
    """Get LLM-powered optimization suggestions"""
    try:
        # Gather performance metrics
        performance_metrics = {
            "session_id": session_id,
            "placeholder": "Would gather actual performance data"
        }
        
        # Get suggestions from LLM
        suggestions = await mcp_client.suggest_optimization_strategies(performance_metrics)
        
        return {
            "session_id": session_id,
            "suggestions": suggestions,
            "timestamp": time.time()
        }
        
    except Exception as e:
        logger.error(f"Failed to get optimization suggestions: {e}")
        raise HTTPException(status_code=500, detail=f"Optimization suggestions failed: {e}")


async def _generate_insights_background(session_id: str, mcp_client: IntelliSenseMCPClient):
    """Background task for generating trading insights"""
    try:
        logger.info(f"Starting background insight generation for session {session_id}")
        
        # This would gather session data and generate insights
        correlation_logs = []  # Would be populated with actual data
        market_context = {}    # Would be populated with market data
        
        insights = await mcp_client.generate_trading_insights(correlation_logs, market_context)
        
        logger.info(f"Generated insights for session {session_id}: {insights}")
        
        # Could store insights in database or publish to Redis stream
        
    except Exception as e:
        logger.error(f"Background insight generation failed for {session_id}: {e}")


# Function to add MCP routes to existing FastAPI app
def add_mcp_routes_to_app(app, 
                         mcp_client: IntelliSenseMCPClient,
                         mcp_server: IntelliSenseMCPServer,
                         redis_bridge: RedisMCPBridge,
                         analysis_service: LLMAnalysisService):
    """
    Add MCP routes to an existing FastAPI application
    
    Args:
        app: FastAPI application instance
        mcp_client: MCP client instance
        mcp_server: MCP server instance
        redis_bridge: Redis bridge instance
        analysis_service: LLM analysis service instance
    """
    # Set global service instances
    set_mcp_services(mcp_client, mcp_server, redis_bridge, analysis_service)
    
    # Include the MCP router
    app.include_router(mcp_router)
    
    logger.info("MCP routes added to FastAPI application")


# Example of how to integrate with existing API main.py
def create_mcp_enhanced_app():
    """
    Example of how to create an MCP-enhanced FastAPI app
    
    This would typically be integrated into your existing api/main.py
    """
    from fastapi import FastAPI
    from intellisense.mcp.config import DEFAULT_MCP_CONFIG
    
    # Create FastAPI app (or use existing one)
    app = FastAPI(title="IntelliSense API with MCP Integration")
    
    # Initialize MCP services
    mcp_client = IntelliSenseMCPClient(DEFAULT_MCP_CONFIG.external_llm_endpoints)
    
    # Note: In real implementation, these would be properly initialized
    mcp_server = None  # IntelliSenseMCPServer(master_controller, config, redis_client)
    redis_bridge = None  # RedisMCPBridge(redis_client, mcp_client, streams, rules)
    analysis_service = LLMAnalysisService(mcp_client)
    
    # Add MCP routes
    if mcp_server and redis_bridge:  # Only add if services are available
        add_mcp_routes_to_app(app, mcp_client, mcp_server, redis_bridge, analysis_service)
    
    return app


if __name__ == "__main__":
    import uvicorn
    
    app = create_mcp_enhanced_app()
    uvicorn.run(app, host="0.0.0.0", port=8002)