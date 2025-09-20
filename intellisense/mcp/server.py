"""
IntelliSense MCP Server

Exposes IntelliSense trading system capabilities as MCP tools for LLM integration.
"""

import json
import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from dataclasses import asdict

# MCP imports - try different package names
MCP_AVAILABLE = False
MCP_IMPORT_ERROR = None

# Try primary import
try:
    from mcp import Server
    from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
    MCP_AVAILABLE = True
except ImportError as e:
    MCP_IMPORT_ERROR = str(e)
    # Try alternative package names
    try:
        from mcp_sdk import Server
        from mcp_sdk.types import Tool, TextContent, ImageContent, EmbeddedResource
        MCP_AVAILABLE = True
    except ImportError:
        try:
            from modelcontextprotocol import Server
            from modelcontextprotocol.types import Tool, TextContent, ImageContent, EmbeddedResource
            MCP_AVAILABLE = True
        except ImportError:
            # Fallback stubs for development
            class Server:
                def __init__(self, name: str): 
                    self.name = name
                def list_tools(self): 
                    return lambda: []
                def call_tool(self): 
                    return lambda name, args: []
            
            class Tool:
                def __init__(self, name: str = "", description: str = "", inputSchema: dict = None, **kwargs):
                    self.name = name
                    self.description = description
                    self.inputSchema = inputSchema or {}
            
            class TextContent:
                def __init__(self, type: str = "text", text: str = "", **kwargs):
                    self.type = type
                    self.text = text
            
            class ImageContent:
                def __init__(self, **kwargs): 
                    pass
            
            class EmbeddedResource:
                def __init__(self, **kwargs): 
                    pass

if TYPE_CHECKING:
    from intellisense.controllers.interfaces import IIntelliSenseMasterController
    from intellisense.mcp.config import MCPConfig

logger = logging.getLogger(__name__)


class IntelliSenseMCPServer:
    """
    MCP Server for IntelliSense Trading System
    
    Exposes trading system capabilities as standardized MCP tools that can be
    called by any MCP-compatible LLM or agent.
    """
    
    def __init__(self, 
                 master_controller: 'IIntelliSenseMasterController',
                 config: 'MCPConfig',
                 redis_client: Optional[Any] = None):
        """
        Initialize the MCP server
        
        Args:
            master_controller: IntelliSense master controller instance
            config: MCP configuration
            redis_client: Redis client for stream access
        """
        if not MCP_AVAILABLE:
            logger.warning(f"MCP library not available. Import error: {MCP_IMPORT_ERROR}")
            logger.warning("Try installing with: pip install mcp-sdk or pip install model-context-protocol")
            
        self.master_controller = master_controller
        self.config = config
        self.redis_client = redis_client
        self.server = Server(config.server_name) if MCP_AVAILABLE else None
        
        if MCP_AVAILABLE and self.server:
            self._register_tools()
            logger.info(f"IntelliSense MCP Server initialized: {config.server_name}")
        
    def _register_tools(self):
        """Register all available MCP tools"""
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List all available IntelliSense trading tools"""
            tools = []
            
            if self.config.tools_enabled.get("session_management", True):
                tools.extend(self._get_session_management_tools())
                
            if self.config.tools_enabled.get("trading_analysis", True):
                tools.extend(self._get_trading_analysis_tools())
                
            if self.config.tools_enabled.get("redis_queries", True):
                tools.extend(self._get_redis_query_tools())
                
            if self.config.tools_enabled.get("performance_metrics", True):
                tools.extend(self._get_performance_tools())
                
            if self.config.tools_enabled.get("risk_analysis", True):
                tools.extend(self._get_risk_analysis_tools())
                
            if self.config.tools_enabled.get("market_data", True):
                tools.extend(self._get_market_data_tools())
                
            if self.config.tools_enabled.get("system_diagnostics", True):
                tools.extend(self._get_diagnostic_tools())
                
            return tools
        
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[Any]:
            """Handle tool calls from LLMs"""
            try:
                if self.config.enable_request_logging:
                    logger.info(f"MCP tool called: {name} with args: {arguments}")
                
                result = await self._handle_tool_call(name, arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
                
            except Exception as e:
                logger.error(f"Error handling MCP tool call {name}: {e}", exc_info=True)
                error_result = {
                    "error": str(e),
                    "tool": name,
                    "arguments": arguments,
                    "timestamp": time.time()
                }
                return [TextContent(type="text", text=json.dumps(error_result, indent=2))]
    
    def _get_session_management_tools(self) -> List[Tool]:
        """Get session management tools"""
        return [
            Tool(
                name="get_trading_session_status",
                description="Get detailed status of a specific trading session",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Unique identifier for the trading session"
                        }
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="list_all_trading_sessions",
                description="List all trading sessions with summary information",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status_filter": {
                            "type": "string",
                            "enum": ["all", "running", "completed", "failed"],
                            "description": "Filter sessions by status",
                            "default": "all"
                        }
                    }
                }
            ),
            Tool(
                name="create_trading_session",
                description="Create a new trading session with specified configuration",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string"},
                        "description": {"type": "string"},
                        "test_date": {"type": "string"},
                        "duration_minutes": {"type": "integer", "default": 60},
                        "speed_factor": {"type": "number", "default": 1.0}
                    },
                    "required": ["session_name", "description", "test_date"]
                }
            ),
            Tool(
                name="start_session_replay",
                description="Start replay for a prepared trading session",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"}
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="stop_session_replay",
                description="Stop an active trading session replay",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"}
                    },
                    "required": ["session_id"]
                }
            )
        ]
    
    def _get_trading_analysis_tools(self) -> List[Tool]:
        """Get trading analysis tools"""
        return [
            Tool(
                name="analyze_trading_performance",
                description="Analyze trading performance metrics and patterns",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "analysis_type": {
                            "type": "string",
                            "enum": ["performance", "latency", "accuracy", "risk", "comprehensive"],
                            "default": "comprehensive"
                        },
                        "time_range_minutes": {"type": "integer", "default": 60}
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="get_session_results_summary",
                description="Get comprehensive results summary for completed session",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "include_detailed_metrics": {"type": "boolean", "default": True}
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="analyze_ocr_accuracy",
                description="Analyze OCR accuracy and data quality metrics",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "symbol_filter": {"type": "string", "description": "Filter by specific symbol"},
                        "confidence_threshold": {"type": "number", "default": 0.8}
                    },
                    "required": ["session_id"]
                }
            )
        ]
    
    def _get_redis_query_tools(self) -> List[Tool]:
        """Get Redis stream query tools"""
        return [
            Tool(
                name="query_redis_stream",
                description="Query data from specific Redis streams",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "stream_name": {
                            "type": "string",
                            "description": "Redis stream name to query"
                        },
                        "count": {"type": "integer", "default": 10, "maximum": 100},
                        "start_id": {"type": "string", "default": "-"},
                        "end_id": {"type": "string", "default": "+"}
                    },
                    "required": ["stream_name"]
                }
            ),
            Tool(
                name="get_recent_trading_events",
                description="Get recent trading events from all monitored streams",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "minutes_back": {"type": "integer", "default": 15},
                        "event_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by event types"
                        }
                    }
                }
            ),
            Tool(
                name="search_correlation_events",
                description="Search for events by correlation ID across all streams",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "correlation_id": {"type": "string"},
                        "include_causation_chain": {"type": "boolean", "default": True}
                    },
                    "required": ["correlation_id"]
                }
            )
        ]
    
    def _get_performance_tools(self) -> List[Tool]:
        """Get performance monitoring tools"""
        return [
            Tool(
                name="get_latency_metrics",
                description="Get latency metrics for trading operations",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "operation_type": {
                            "type": "string",
                            "enum": ["ocr", "order_execution", "price_fetch", "all"],
                            "default": "all"
                        }
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="get_system_health_metrics",
                description="Get current system health and performance metrics",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "include_redis_stats": {"type": "boolean", "default": True},
                        "include_memory_usage": {"type": "boolean", "default": True}
                    }
                }
            )
        ]
    
    def _get_risk_analysis_tools(self) -> List[Tool]:
        """Get risk analysis tools"""
        return [
            Tool(
                name="analyze_risk_events",
                description="Analyze risk management events and patterns",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "risk_level": {
                            "type": "string", 
                            "enum": ["low", "medium", "high", "critical", "all"],
                            "default": "all"
                        }
                    },
                    "required": ["session_id"]
                }
            ),
            Tool(
                name="get_position_risk_summary",
                description="Get current position risk summary",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"}
                    },
                    "required": ["session_id"]
                }
            )
        ]
    
    def _get_market_data_tools(self) -> List[Tool]:
        """Get market data tools"""
        return [
            Tool(
                name="get_market_data_summary",
                description="Get market data summary and statistics",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of symbols to analyze"
                        },
                        "time_range_minutes": {"type": "integer", "default": 60}
                    }
                }
            )
        ]
    
    def _get_diagnostic_tools(self) -> List[Tool]:
        """Get system diagnostic tools"""
        return [
            Tool(
                name="diagnose_system_status",
                description="Run comprehensive system diagnostics",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "include_redis_connectivity": {"type": "boolean", "default": True},
                        "include_service_health": {"type": "boolean", "default": True},
                        "include_recent_errors": {"type": "boolean", "default": True}
                    }
                }
            ),
            Tool(
                name="get_error_analysis",
                description="Analyze recent errors and issues",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "time_range_hours": {"type": "integer", "default": 24},
                        "error_severity": {
                            "type": "string",
                            "enum": ["all", "warning", "error", "critical"],
                            "default": "all"
                        }
                    }
                }
            )
        ]
    
    async def _handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Handle individual tool calls"""
        
        # Session Management Tools
        if name == "get_trading_session_status":
            session_id = arguments.get("session_id")
            session_data = self.master_controller.get_session_status(session_id)
            if not session_data:
                return {"error": f"Session {session_id} not found"}
            return {"session_id": session_id, "status": session_data}
            
        elif name == "list_all_trading_sessions":
            sessions = self.master_controller.list_sessions()
            status_filter = arguments.get("status_filter", "all")
            
            if status_filter != "all":
                sessions = [s for s in sessions if s.get("status", "").lower() == status_filter.lower()]
            
            return {"sessions": sessions, "count": len(sessions)}
            
        elif name == "create_trading_session":
            # Extract session config from arguments
            from intellisense.core.types import TestSessionConfig, OCRTestConfig, PriceTestConfig, BrokerTestConfig
            
            session_config = TestSessionConfig(
                session_name=arguments["session_name"],
                description=arguments["description"],
                test_date=arguments["test_date"],
                controlled_trade_log_path="",  # Will use Redis streams
                historical_price_data_path_sync="",
                broker_response_log_path="",
                duration_minutes=arguments.get("duration_minutes", 60),
                speed_factor=arguments.get("speed_factor", 1.0),
                ocr_config=OCRTestConfig(),
                price_config=PriceTestConfig(),
                broker_config=BrokerTestConfig(),
                output_directory="./intellisense_sessions"
            )
            
            session_id = self.master_controller.create_test_session(session_config)
            return {"session_id": session_id, "status": "created"}
            
        elif name == "start_session_replay":
            session_id = arguments.get("session_id")
            success = self.master_controller.start_replay_session(session_id)
            return {"session_id": session_id, "success": success}
            
        elif name == "stop_session_replay":
            session_id = arguments.get("session_id")
            success = self.master_controller.stop_replay_session(session_id)
            return {"session_id": session_id, "success": success}
            
        # Analysis Tools
        elif name == "analyze_trading_performance":
            session_id = arguments.get("session_id")
            analysis_type = arguments.get("analysis_type", "comprehensive")
            
            session_data = self.master_controller.get_session_status(session_id)
            if not session_data:
                return {"error": f"Session {session_id} not found"}
            
            # Get results summary if available
            try:
                results = self.master_controller.get_session_results_summary(session_id)
                return {
                    "session_id": session_id,
                    "analysis_type": analysis_type,
                    "results": results,
                    "timestamp": time.time()
                }
            except Exception as e:
                return {"error": f"Could not analyze performance: {str(e)}"}
                
        elif name == "get_session_results_summary":
            session_id = arguments.get("session_id")
            include_detailed = arguments.get("include_detailed_metrics", True)
            
            try:
                results = self.master_controller.get_session_results_summary(session_id)
                return {"session_id": session_id, "results": results}
            except Exception as e:
                return {"error": f"Could not get results: {str(e)}"}
                
        # Redis Query Tools
        elif name == "query_redis_stream":
            if not self.redis_client:
                return {"error": "Redis client not available"}
                
            stream_name = arguments.get("stream_name")
            count = arguments.get("count", 10)
            start_id = arguments.get("start_id", "-")
            end_id = arguments.get("end_id", "+")
            
            try:
                # Query Redis stream
                result = await self._query_redis_stream(stream_name, count, start_id, end_id)
                return {"stream_name": stream_name, "data": result}
            except Exception as e:
                return {"error": f"Redis query failed: {str(e)}"}
                
        elif name == "get_recent_trading_events":
            if not self.redis_client:
                return {"error": "Redis client not available"}
                
            minutes_back = arguments.get("minutes_back", 15)
            event_types = arguments.get("event_types", [])
            
            try:
                events = await self._get_recent_events(minutes_back, event_types)
                return {"events": events, "count": len(events)}
            except Exception as e:
                return {"error": f"Could not retrieve events: {str(e)}"}
                
        # Diagnostic Tools
        elif name == "diagnose_system_status":
            try:
                diagnostics = await self._run_system_diagnostics(arguments)
                return {"diagnostics": diagnostics, "timestamp": time.time()}
            except Exception as e:
                return {"error": f"Diagnostics failed: {str(e)}"}
                
        else:
            return {"error": f"Unknown tool: {name}"}
    
    async def _query_redis_stream(self, stream_name: str, count: int, start_id: str, end_id: str):
        """Query Redis stream data"""
        if not self.redis_client:
            raise ValueError("Redis client not available")
            
        try:
            # Use XRANGE for bounded queries or XREAD for latest
            if start_id == "-" and end_id == "+":
                # Get latest entries
                result = self.redis_client.xrevrange(stream_name, count=count)
            else:
                # Get range
                result = self.redis_client.xrange(stream_name, start_id, end_id, count=count)
            
            # Format results
            entries = []
            for entry_id, data in result:
                entries.append({
                    "id": entry_id,
                    "timestamp": self._parse_stream_timestamp(entry_id),
                    "data": data
                })
            
            return {
                "stream": stream_name,
                "count": len(entries),
                "entries": entries
            }
        except Exception as e:
            logger.error(f"Redis stream query failed: {e}")
            raise
    
    async def _get_recent_events(self, minutes_back: int, event_types: List[str]):
        """Get recent events from monitored streams"""
        if not self.redis_client:
            return []
            
        # Calculate time boundary
        time_ms = int((time.time() - (minutes_back * 60)) * 1000)
        start_id = f"{time_ms}-0"
        
        all_events = []
        
        # Query configured monitored streams
        for stream in self.config.monitored_streams:
            try:
                # Get events from this stream
                result = self.redis_client.xrange(stream, start_id, "+", count=100)
                
                for entry_id, data in result:
                    event = {
                        "stream": stream,
                        "id": entry_id,
                        "timestamp": self._parse_stream_timestamp(entry_id),
                        "type": data.get("event_type", "unknown"),
                        "data": data
                    }
                    
                    # Filter by event type if specified
                    if not event_types or event["type"] in event_types:
                        all_events.append(event)
                        
            except Exception as e:
                logger.error(f"Failed to query stream {stream}: {e}")
                continue
        
        # Sort by timestamp
        all_events.sort(key=lambda x: x["timestamp"], reverse=True)
        
        return all_events
    
    async def _run_system_diagnostics(self, options: Dict[str, Any]):
        """Run comprehensive system diagnostics"""
        diagnostics = {
            "mcp_server_status": "healthy",
            "master_controller_status": "connected" if self.master_controller else "disconnected",
            "redis_status": "unknown",
            "timestamp": time.time()
        }
        
        # Check Redis connectivity
        if options.get("include_redis_connectivity", True) and self.redis_client:
            try:
                self.redis_client.ping()
                diagnostics["redis_status"] = "connected"
                
                # Get Redis info
                info = self.redis_client.info()
                diagnostics["redis_info"] = {
                    "version": info.get("redis_version"),
                    "uptime_seconds": info.get("uptime_in_seconds"),
                    "connected_clients": info.get("connected_clients"),
                    "used_memory_human": info.get("used_memory_human")
                }
                
                # Check stream existence
                stream_status = {}
                for stream in self.config.monitored_streams:
                    try:
                        length = self.redis_client.xlen(stream)
                        stream_status[stream] = {"exists": True, "length": length}
                    except:
                        stream_status[stream] = {"exists": False, "length": 0}
                
                diagnostics["monitored_streams"] = stream_status
                
            except Exception as e:
                diagnostics["redis_status"] = "error"
                diagnostics["redis_error"] = str(e)
        
        # Check service health
        if options.get("include_service_health", True):
            diagnostics["services"] = {}
            
            # Check master controller
            if self.master_controller:
                try:
                    sessions = self.master_controller.list_sessions()
                    diagnostics["services"]["master_controller"] = {
                        "status": "healthy",
                        "active_sessions": len(sessions)
                    }
                except Exception as e:
                    diagnostics["services"]["master_controller"] = {
                        "status": "error",
                        "error": str(e)
                    }
            
            # Check MCP server itself
            diagnostics["services"]["mcp_server"] = {
                "status": "healthy",
                "tools_enabled": list(self.config.tools_enabled.keys()),
                "config": {
                    "host": self.config.server_host,
                    "port": self.config.server_port,
                    "max_concurrent_requests": self.config.max_concurrent_requests
                }
            }
        
        # Check recent errors
        if options.get("include_recent_errors", True):
            # This would query an error tracking stream if available
            diagnostics["recent_errors"] = []
            
        return diagnostics
    
    def _parse_stream_timestamp(self, stream_id: str) -> float:
        """Parse timestamp from Redis stream ID"""
        try:
            # Stream IDs are typically "timestamp-sequence"
            timestamp_ms = int(stream_id.split("-")[0])
            return timestamp_ms / 1000.0
        except:
            return 0.0
    
    async def start_server(self):
        """Start the MCP server"""
        if not MCP_AVAILABLE:
            logger.error("Cannot start MCP server: MCP library not available")
            return False
            
        try:
            # Start the MCP server
            logger.info(f"Starting MCP server on {self.config.server_host}:{self.config.server_port}")
            # Implementation depends on MCP library server startup
            return True
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            return False
    
    async def stop_server(self):
        """Stop the MCP server"""
        if not MCP_AVAILABLE:
            return
            
        try:
            logger.info("Stopping MCP server")
            # Implementation depends on MCP library server shutdown
        except Exception as e:
            logger.error(f"Error stopping MCP server: {e}")


# Example usage
async def main():
    """Example of how to start the MCP server"""
    from intellisense.mcp.config import DEFAULT_MCP_CONFIG
    
    # This would normally be injected
    master_controller = None  # Get from your application
    redis_client = None       # Get from your application
    
    server = IntelliSenseMCPServer(master_controller, DEFAULT_MCP_CONFIG, redis_client)
    await server.start_server()


if __name__ == "__main__":
    asyncio.run(main())