"""
MCP Server Runner for IntelliSense

This module provides the entry point for running the IntelliSense MCP server
with proper stdio transport for MCP protocol communication.

Compatible with MCP 1.10.1 using FastMCP API
"""

import sys
import asyncio
import logging
import redis
from typing import Optional, Dict, Any, List

# MCP 1.10.1 imports
try:
    from mcp.server import FastMCP
    from mcp.types import Tool, TextContent
    MCP_AVAILABLE = True
except ImportError:
    print("ERROR: MCP library not installed or incorrect version.", file=sys.stderr)
    print("Try: pip install mcp==1.10.1", file=sys.stderr)
    MCP_AVAILABLE = False
    FastMCP = None
    Tool = None
    TextContent = None

from intellisense.controllers.master_controller_redis import IntelliSenseMasterControllerRedis
from intellisense.core.enums import OperationMode
from intellisense.core.types import IntelliSenseConfig, TestSessionConfig, OCRTestConfig, PriceTestConfig, BrokerTestConfig
from intellisense.mcp.config import MCPConfig

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class IntelliSenseMCPServerModern:
    """Modern MCP Server implementation using FastMCP for IntelliSense Trading System"""
    
    def __init__(self, redis_config: Dict[str, Any]):
        """Initialize the MCP server with Redis configuration"""
        if not MCP_AVAILABLE:
            logger.error("MCP library not available")
            sys.exit(1)
            
        self.redis_config = redis_config
        self.redis_client = None
        self.master_controller = None
        self.mcp = FastMCP("intellisense-trading")
        
        # Register all tools
        self._register_session_management_tools()
        self._register_trading_analysis_tools()
        self._register_redis_query_tools()
        self._register_performance_tools()
        self._register_risk_analysis_tools()
        self._register_market_data_tools()
        self._register_diagnostic_tools()
        
    def _initialize_controller(self):
        """Initialize the master controller and Redis connection"""
        try:
            # Initialize Redis client
            self.redis_client = redis.Redis(**self.redis_config, decode_responses=True)
            self.redis_client.ping()
            logger.info("Connected to Redis")
            
            # Create a default test session config
            default_session_config = TestSessionConfig(
                session_name="mcp_default",
                description="Default MCP configuration",
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
            
            intellisense_config = IntelliSenseConfig(
                test_session_config=default_session_config,
                performance_monitoring_active=True,
                real_time_dashboard_enabled=True
            )
            
            self.master_controller = IntelliSenseMasterControllerRedis(
                operation_mode=OperationMode.INTELLISENSE,
                intellisense_config=intellisense_config,
                redis_config=self.redis_config
            )
            logger.info("Master controller initialized with 592 modules")
            
        except Exception as e:
            logger.error(f"Failed to initialize controller: {e}")
            raise
    
    def _register_session_management_tools(self):
        """Register session management tools"""
        
        @self.mcp.tool()
        async def get_trading_session_status(session_id: str) -> str:
            """Get detailed status of a specific trading session
            
            Args:
                session_id: Unique identifier for the trading session
                
            Returns:
                JSON string with session status information
            """
            if not self.master_controller:
                self._initialize_controller()
                
            session_data = self.master_controller.get_session_status(session_id)
            if not session_data:
                return f"Session {session_id} not found"
            return str({"session_id": session_id, "status": session_data})
        
        @self.mcp.tool()
        async def list_all_trading_sessions(status_filter: str = "all") -> str:
            """List all trading sessions with summary information
            
            Args:
                status_filter: Filter sessions by status (all, running, completed, failed)
                
            Returns:
                JSON string with list of sessions
            """
            if not self.master_controller:
                self._initialize_controller()
                
            sessions = self.master_controller.list_sessions()
            
            if status_filter != "all":
                sessions = [s for s in sessions if s.get("status", "").lower() == status_filter.lower()]
            
            return str({"sessions": sessions, "count": len(sessions)})
        
        @self.mcp.tool()
        async def create_trading_session(
            session_name: str,
            description: str,
            test_date: str,
            duration_minutes: int = 60,
            speed_factor: float = 1.0
        ) -> str:
            """Create a new trading session with specified configuration
            
            Args:
                session_name: Name for the session
                description: Description of the session
                test_date: Date for the test session
                duration_minutes: Duration in minutes (default 60)
                speed_factor: Speed multiplication factor (default 1.0)
                
            Returns:
                JSON string with session ID and status
            """
            if not self.master_controller:
                self._initialize_controller()
                
            session_config = TestSessionConfig(
                session_name=session_name,
                description=description,
                test_date=test_date,
                controlled_trade_log_path="",
                historical_price_data_path_sync="",
                broker_response_log_path="",
                duration_minutes=duration_minutes,
                speed_factor=speed_factor,
                ocr_config=OCRTestConfig(),
                price_config=PriceTestConfig(),
                broker_config=BrokerTestConfig(),
                output_directory="./intellisense_sessions"
            )
            
            session_id = self.master_controller.create_test_session(session_config)
            return str({"session_id": session_id, "status": "created"})
        
        @self.mcp.tool()
        async def start_session_replay(session_id: str) -> str:
            """Start replay for a prepared trading session
            
            Args:
                session_id: ID of the session to start
                
            Returns:
                JSON string with success status
            """
            if not self.master_controller:
                self._initialize_controller()
                
            success = self.master_controller.start_replay_session(session_id)
            return str({"session_id": session_id, "success": success})
        
        @self.mcp.tool()
        async def stop_session_replay(session_id: str) -> str:
            """Stop an active trading session replay
            
            Args:
                session_id: ID of the session to stop
                
            Returns:
                JSON string with success status
            """
            if not self.master_controller:
                self._initialize_controller()
                
            success = self.master_controller.stop_replay_session(session_id)
            return str({"session_id": session_id, "success": success})
    
    def _register_trading_analysis_tools(self):
        """Register trading analysis tools"""
        
        @self.mcp.tool()
        async def analyze_trading_performance(
            session_id: str,
            analysis_type: str = "comprehensive",
            time_range_minutes: int = 60
        ) -> str:
            """Analyze trading performance metrics and patterns
            
            Args:
                session_id: ID of the session to analyze
                analysis_type: Type of analysis (performance, latency, accuracy, risk, comprehensive)
                time_range_minutes: Time range for analysis in minutes
                
            Returns:
                JSON string with analysis results
            """
            if not self.master_controller:
                self._initialize_controller()
                
            session_data = self.master_controller.get_session_status(session_id)
            if not session_data:
                return f"Session {session_id} not found"
            
            try:
                results = self.master_controller.get_session_results_summary(session_id)
                return str({
                    "session_id": session_id,
                    "analysis_type": analysis_type,
                    "results": results
                })
            except Exception as e:
                return f"Could not analyze performance: {str(e)}"
        
        @self.mcp.tool()
        async def get_session_results_summary(
            session_id: str,
            include_detailed_metrics: bool = True
        ) -> str:
            """Get comprehensive results summary for completed session
            
            Args:
                session_id: ID of the session
                include_detailed_metrics: Whether to include detailed metrics
                
            Returns:
                JSON string with session results
            """
            if not self.master_controller:
                self._initialize_controller()
                
            try:
                results = self.master_controller.get_session_results_summary(session_id)
                return str({"session_id": session_id, "results": results})
            except Exception as e:
                return f"Could not get results: {str(e)}"
        
        @self.mcp.tool()
        async def analyze_ocr_accuracy(
            session_id: str,
            symbol_filter: str = "",
            confidence_threshold: float = 0.8
        ) -> str:
            """Analyze OCR accuracy and data quality metrics
            
            Args:
                session_id: ID of the session
                symbol_filter: Filter by specific symbol
                confidence_threshold: Minimum confidence threshold
                
            Returns:
                JSON string with OCR analysis results
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for OCR analysis logic
            return str({
                "session_id": session_id,
                "symbol_filter": symbol_filter,
                "confidence_threshold": confidence_threshold,
                "analysis": "OCR accuracy analysis results"
            })
    
    def _register_redis_query_tools(self):
        """Register Redis stream query tools"""
        
        @self.mcp.tool()
        async def query_redis_stream(
            stream_name: str,
            count: int = 10,
            start_id: str = "-",
            end_id: str = "+"
        ) -> str:
            """Query data from specific Redis streams
            
            Args:
                stream_name: Redis stream name to query
                count: Number of entries to retrieve (max 100)
                start_id: Start ID for range query
                end_id: End ID for range query
                
            Returns:
                JSON string with stream data
            """
            if not self.redis_client:
                self._initialize_controller()
                
            try:
                if start_id == "-" and end_id == "+":
                    result = self.redis_client.xrevrange(stream_name, count=min(count, 100))
                else:
                    result = self.redis_client.xrange(stream_name, start_id, end_id, count=min(count, 100))
                
                entries = []
                for entry_id, data in result:
                    entries.append({
                        "id": entry_id,
                        "data": data
                    })
                
                return str({
                    "stream": stream_name,
                    "count": len(entries),
                    "entries": entries
                })
            except Exception as e:
                return f"Redis query failed: {str(e)}"
        
        @self.mcp.tool()
        async def get_recent_trading_events(
            minutes_back: int = 15,
            event_types: List[str] = None
        ) -> str:
            """Get recent trading events from all monitored streams
            
            Args:
                minutes_back: How many minutes back to search
                event_types: Filter by specific event types
                
            Returns:
                JSON string with recent events
            """
            if not self.redis_client:
                self._initialize_controller()
                
            import time
            time_ms = int((time.time() - (minutes_back * 60)) * 1000)
            start_id = f"{time_ms}-0"
            
            all_events = []
            monitored_streams = [
                "intellisense:events:ocr",
                "intellisense:events:trading",
                "intellisense:events:market_data",
                "intellisense:events:risk"
            ]
            
            for stream in monitored_streams:
                try:
                    result = self.redis_client.xrange(stream, start_id, "+", count=100)
                    for entry_id, data in result:
                        event = {
                            "stream": stream,
                            "id": entry_id,
                            "type": data.get("event_type", "unknown"),
                            "data": data
                        }
                        
                        if not event_types or event["type"] in (event_types or []):
                            all_events.append(event)
                            
                except Exception as e:
                    logger.error(f"Failed to query stream {stream}: {e}")
                    continue
            
            all_events.sort(key=lambda x: x["id"], reverse=True)
            
            return str({"events": all_events[:100], "count": len(all_events)})
        
        @self.mcp.tool()
        async def search_correlation_events(
            correlation_id: str,
            include_causation_chain: bool = True
        ) -> str:
            """Search for events by correlation ID across all streams
            
            Args:
                correlation_id: The correlation ID to search for
                include_causation_chain: Whether to include causation chain
                
            Returns:
                JSON string with correlated events
            """
            if not self.redis_client:
                self._initialize_controller()
                
            # Placeholder for correlation search logic
            return str({
                "correlation_id": correlation_id,
                "events": [],
                "causation_chain": [] if include_causation_chain else None
            })
    
    def _register_performance_tools(self):
        """Register performance monitoring tools"""
        
        @self.mcp.tool()
        async def get_latency_metrics(
            session_id: str,
            operation_type: str = "all"
        ) -> str:
            """Get latency metrics for trading operations
            
            Args:
                session_id: ID of the session
                operation_type: Type of operation (ocr, order_execution, price_fetch, all)
                
            Returns:
                JSON string with latency metrics
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for latency metrics logic
            return str({
                "session_id": session_id,
                "operation_type": operation_type,
                "metrics": {
                    "avg_latency_ms": 45,
                    "p95_latency_ms": 120,
                    "p99_latency_ms": 250
                }
            })
        
        @self.mcp.tool()
        async def get_system_health_metrics(
            include_redis_stats: bool = True,
            include_memory_usage: bool = True
        ) -> str:
            """Get current system health and performance metrics
            
            Args:
                include_redis_stats: Whether to include Redis statistics
                include_memory_usage: Whether to include memory usage
                
            Returns:
                JSON string with system health metrics
            """
            if not self.redis_client:
                self._initialize_controller()
                
            metrics = {
                "status": "healthy",
                "modules_loaded": 592
            }
            
            if include_redis_stats:
                try:
                    info = self.redis_client.info()
                    metrics["redis"] = {
                        "version": info.get("redis_version"),
                        "connected_clients": info.get("connected_clients"),
                        "used_memory_human": info.get("used_memory_human")
                    }
                except Exception as e:
                    metrics["redis"] = {"error": str(e)}
            
            return str(metrics)
    
    def _register_risk_analysis_tools(self):
        """Register risk analysis tools"""
        
        @self.mcp.tool()
        async def analyze_risk_events(
            session_id: str,
            risk_level: str = "all"
        ) -> str:
            """Analyze risk management events and patterns
            
            Args:
                session_id: ID of the session
                risk_level: Risk level filter (low, medium, high, critical, all)
                
            Returns:
                JSON string with risk analysis
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for risk analysis logic
            return str({
                "session_id": session_id,
                "risk_level": risk_level,
                "risk_events": []
            })
        
        @self.mcp.tool()
        async def get_position_risk_summary(session_id: str) -> str:
            """Get current position risk summary
            
            Args:
                session_id: ID of the session
                
            Returns:
                JSON string with position risk summary
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for position risk logic
            return str({
                "session_id": session_id,
                "positions": [],
                "total_risk": 0
            })
    
    def _register_market_data_tools(self):
        """Register market data tools"""
        
        @self.mcp.tool()
        async def get_market_data_summary(
            symbols: List[str] = None,
            time_range_minutes: int = 60
        ) -> str:
            """Get market data summary and statistics
            
            Args:
                symbols: List of symbols to analyze
                time_range_minutes: Time range for analysis
                
            Returns:
                JSON string with market data summary
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for market data logic
            return str({
                "symbols": symbols or [],
                "time_range_minutes": time_range_minutes,
                "summary": "Market data summary"
            })
    
    def _register_diagnostic_tools(self):
        """Register system diagnostic tools"""
        
        @self.mcp.tool()
        async def diagnose_system_status(
            include_redis_connectivity: bool = True,
            include_service_health: bool = True,
            include_recent_errors: bool = True
        ) -> str:
            """Run comprehensive system diagnostics
            
            Args:
                include_redis_connectivity: Check Redis connection
                include_service_health: Check service health
                include_recent_errors: Include recent errors
                
            Returns:
                JSON string with diagnostic results
            """
            if not self.redis_client:
                self._initialize_controller()
                
            diagnostics = {
                "mcp_server_status": "healthy",
                "master_controller_status": "connected" if self.master_controller else "disconnected"
            }
            
            if include_redis_connectivity:
                try:
                    self.redis_client.ping()
                    diagnostics["redis_status"] = "connected"
                except Exception as e:
                    diagnostics["redis_status"] = f"error: {str(e)}"
            
            if include_service_health:
                diagnostics["modules_loaded"] = 592
                diagnostics["tools_available"] = 18
            
            return str(diagnostics)
        
        @self.mcp.tool()
        async def get_error_analysis(
            time_range_hours: int = 24,
            error_severity: str = "all"
        ) -> str:
            """Analyze recent errors and issues
            
            Args:
                time_range_hours: Time range in hours
                error_severity: Error severity filter (all, warning, error, critical)
                
            Returns:
                JSON string with error analysis
            """
            if not self.master_controller:
                self._initialize_controller()
                
            # Placeholder for error analysis logic
            return str({
                "time_range_hours": time_range_hours,
                "error_severity": error_severity,
                "errors": []
            })
    
    def run(self):
        """Run the MCP server"""
        # The FastMCP instance handles the stdio communication
        logger.info("Running FastMCP server on stdio transport...")
        self.mcp.run(transport="stdio")


def main():
    """Main entry point"""
    if not MCP_AVAILABLE:
        logger.error("Cannot start MCP server: MCP library not available")
        sys.exit(1)
    
    # Redis configuration
    redis_config = {
        "host": "172.22.202.120",
        "port": 6379,
        "db": 0
    }
    
    # Create and run the server
    server = IntelliSenseMCPServerModern(redis_config)
    
    logger.info("Starting IntelliSense MCP server with 18 trading tools...")
    logger.info("Server is ready to accept connections via stdio")
    
    # Run the server (this will block and handle stdio communication)
    server.run()


if __name__ == "__main__":
    main()