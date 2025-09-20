"""
IntelliSense MCP Client

Connects to external LLM services via MCP for enhanced trading analysis and insights.
"""

import json
import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, Union
from dataclasses import asdict

# MCP imports (will need to be installed)
try:
    from mcp import ClientSession
    from mcp.types import Tool, TextContent, ImageContent
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    # Fallback stubs
    class ClientSession:
        def __init__(self, url: str): self.url = url
        async def call_tool(self, name: str, args: dict): return {"error": "MCP not available"}
    
    class Tool:
        def __init__(self, **kwargs): pass
    
    class TextContent:
        def __init__(self, **kwargs): pass

logger = logging.getLogger(__name__)


class IntelliSenseMCPClient:
    """
    MCP Client for connecting to external LLM services
    
    This client allows the IntelliSense system to send trading data and requests
    to external LLM services that support the MCP protocol.
    """
    
    def __init__(self, 
                 llm_endpoints: List[str],
                 default_timeout: int = 30,
                 max_retries: int = 3):
        """
        Initialize the MCP client
        
        Args:
            llm_endpoints: List of MCP-compatible LLM service URLs
            default_timeout: Default timeout for requests in seconds
            max_retries: Maximum number of retry attempts
        """
        if not MCP_AVAILABLE:
            logger.warning("MCP library not available. Install with: pip install mcp")
            
        self.llm_endpoints = llm_endpoints
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.sessions: Dict[str, ClientSession] = {}
        
        if MCP_AVAILABLE:
            self._initialize_sessions()
            logger.info(f"IntelliSense MCP Client initialized with {len(llm_endpoints)} endpoints")
    
    def _initialize_sessions(self):
        """Initialize MCP client sessions for each endpoint"""
        for endpoint in self.llm_endpoints:
            try:
                self.sessions[endpoint] = ClientSession(endpoint)
                logger.debug(f"Initialized MCP session for {endpoint}")
            except Exception as e:
                logger.error(f"Failed to initialize session for {endpoint}: {e}")
    
    async def analyze_trading_performance(self, 
                                        session_data: Dict[str, Any],
                                        analysis_type: str = "comprehensive") -> Dict[str, Any]:
        """
        Send trading performance data to LLM for analysis
        
        Args:
            session_data: Trading session data and metrics
            analysis_type: Type of analysis requested
            
        Returns:
            LLM analysis results
        """
        try:
            request_data = {
                "task": "analyze_trading_performance",
                "data": session_data,
                "analysis_type": analysis_type,
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("trading_performance_analysis", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Trading performance analysis failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def generate_trading_insights(self, 
                                      correlation_logs: List[Dict],
                                      market_context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Generate trading insights from correlation logs and market data
        
        Args:
            correlation_logs: List of correlation log entries
            market_context: Optional market context data
            
        Returns:
            LLM-generated insights and recommendations
        """
        try:
            request_data = {
                "task": "generate_trading_insights",
                "correlation_logs": correlation_logs,
                "market_context": market_context or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("trading_insights_generation", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Trading insights generation failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def analyze_risk_patterns(self, 
                                  risk_events: List[Dict],
                                  position_data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Analyze risk patterns and suggest improvements
        
        Args:
            risk_events: List of risk management events
            position_data: Current position data
            
        Returns:
            Risk analysis and recommendations
        """
        try:
            request_data = {
                "task": "analyze_risk_patterns",
                "risk_events": risk_events,
                "position_data": position_data or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("risk_pattern_analysis", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Risk pattern analysis failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def suggest_optimization_strategies(self, 
                                            performance_metrics: Dict[str, Any],
                                            latency_data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get LLM suggestions for system optimization
        
        Args:
            performance_metrics: System performance metrics
            latency_data: Latency measurement data
            
        Returns:
            Optimization suggestions and strategies
        """
        try:
            request_data = {
                "task": "suggest_optimization_strategies",
                "performance_metrics": performance_metrics,
                "latency_data": latency_data or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("optimization_strategy_generation", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Optimization strategy generation failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def interpret_market_conditions(self, 
                                        market_data: Dict[str, Any],
                                        trading_context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get LLM interpretation of current market conditions
        
        Args:
            market_data: Current market data
            trading_context: Trading strategy context
            
        Returns:
            Market condition analysis and trading recommendations
        """
        try:
            request_data = {
                "task": "interpret_market_conditions",
                "market_data": market_data,
                "trading_context": trading_context or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("market_condition_analysis", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Market condition analysis failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def generate_natural_language_report(self, 
                                             report_data: Dict[str, Any],
                                             report_type: str = "summary") -> Dict[str, Any]:
        """
        Generate natural language reports from trading data
        
        Args:
            report_data: Raw report data to convert
            report_type: Type of report (summary, detailed, executive)
            
        Returns:
            Natural language report
        """
        try:
            request_data = {
                "task": "generate_natural_language_report",
                "report_data": report_data,
                "report_type": report_type,
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("natural_language_reporting", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Natural language report generation failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def query_natural_language(self, 
                                   query: str,
                                   context_data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Process natural language queries about trading data
        
        Args:
            query: Natural language query
            context_data: Relevant context data
            
        Returns:
            Natural language response with relevant data
        """
        try:
            request_data = {
                "task": "process_natural_language_query",
                "query": query,
                "context_data": context_data or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("natural_language_query", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Natural language query processing failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def generate_test_scenarios(self, 
                                    historical_data: Dict[str, Any],
                                    scenario_requirements: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Generate new test scenarios based on historical data
        
        Args:
            historical_data: Historical trading and market data
            scenario_requirements: Requirements for generated scenarios
            
        Returns:
            Generated test scenarios and injection plans
        """
        try:
            request_data = {
                "task": "generate_test_scenarios",
                "historical_data": historical_data,
                "scenario_requirements": scenario_requirements or {},
                "timestamp": time.time()
            }
            
            result = await self._call_llm_tool("test_scenario_generation", request_data)
            return result
            
        except Exception as e:
            logger.error(f"Test scenario generation failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def _call_llm_tool(self, tool_name: str, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call an LLM tool across available endpoints with retry logic
        
        Args:
            tool_name: Name of the tool to call
            request_data: Data to send to the tool
            
        Returns:
            Tool response
        """
        if not MCP_AVAILABLE:
            return {"error": "MCP library not available", "tool": tool_name}
        
        last_error = None
        
        # Try each endpoint
        for endpoint in self.llm_endpoints:
            session = self.sessions.get(endpoint)
            if not session:
                continue
                
            # Retry logic for each endpoint
            for attempt in range(self.max_retries):
                try:
                    logger.debug(f"Calling {tool_name} on {endpoint} (attempt {attempt + 1})")
                    
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, request_data),
                        timeout=self.default_timeout
                    )
                    
                    # Parse result if it's a TextContent
                    if hasattr(result, '__iter__') and len(result) > 0:
                        if hasattr(result[0], 'text'):
                            return json.loads(result[0].text)
                        else:
                            return {"result": str(result[0])}
                    
                    return result
                    
                except asyncio.TimeoutError:
                    last_error = f"Timeout calling {tool_name} on {endpoint}"
                    logger.warning(last_error)
                    
                except json.JSONDecodeError as e:
                    last_error = f"JSON decode error from {endpoint}: {e}"
                    logger.warning(last_error)
                    
                except Exception as e:
                    last_error = f"Error calling {tool_name} on {endpoint}: {e}"
                    logger.warning(last_error)
                    
                # Wait before retry
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
        
        # All endpoints failed
        logger.error(f"All LLM endpoints failed for {tool_name}. Last error: {last_error}")
        return {
            "error": f"All LLM endpoints failed: {last_error}",
            "tool": tool_name,
            "timestamp": time.time()
        }
    
    async def test_connectivity(self) -> Dict[str, Any]:
        """
        Test connectivity to all configured LLM endpoints
        
        Returns:
            Connectivity test results
        """
        results = {
            "timestamp": time.time(),
            "endpoints": {},
            "summary": {"total": len(self.llm_endpoints), "healthy": 0, "failed": 0}
        }
        
        for endpoint in self.llm_endpoints:
            try:
                # Simple ping test
                test_result = await self._call_llm_tool("health_check", {"ping": True})
                
                if "error" not in test_result:
                    results["endpoints"][endpoint] = {"status": "healthy", "response": test_result}
                    results["summary"]["healthy"] += 1
                else:
                    results["endpoints"][endpoint] = {"status": "failed", "error": test_result["error"]}
                    results["summary"]["failed"] += 1
                    
            except Exception as e:
                results["endpoints"][endpoint] = {"status": "failed", "error": str(e)}
                results["summary"]["failed"] += 1
        
        return results
    
    def get_endpoint_status(self) -> Dict[str, Any]:
        """
        Get current status of all endpoints
        
        Returns:
            Endpoint status information
        """
        status = {
            "mcp_available": MCP_AVAILABLE,
            "endpoints_configured": len(self.llm_endpoints),
            "sessions_active": len(self.sessions),
            "endpoints": []
        }
        
        for endpoint in self.llm_endpoints:
            endpoint_info = {
                "url": endpoint,
                "session_active": endpoint in self.sessions,
                "last_used": None  # Could be tracked if needed
            }
            status["endpoints"].append(endpoint_info)
        
        return status


# Enhanced LLM Analysis Service using MCP Client
class LLMAnalysisService:
    """
    High-level service for LLM-powered trading analysis using MCP
    
    This service provides a simplified interface for common LLM analysis tasks.
    """
    
    def __init__(self, mcp_client: IntelliSenseMCPClient):
        """
        Initialize the LLM analysis service
        
        Args:
            mcp_client: Configured MCP client instance
        """
        self.mcp_client = mcp_client
        self.logger = logging.getLogger(__name__)
    
    async def analyze_session_comprehensive(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Perform comprehensive analysis of a trading session
        
        Args:
            session_data: Complete session data including metrics, events, and results
            
        Returns:
            Comprehensive analysis results
        """
        try:
            # Extract different data types
            performance_metrics = session_data.get("performance_metrics", {})
            risk_events = session_data.get("risk_events", [])
            correlation_logs = session_data.get("correlation_logs", [])
            market_data = session_data.get("market_data", {})
            
            # Run multiple analyses in parallel
            analyses = await asyncio.gather(
                self.mcp_client.analyze_trading_performance(session_data, "comprehensive"),
                self.mcp_client.analyze_risk_patterns(risk_events, session_data.get("positions")),
                self.mcp_client.generate_trading_insights(correlation_logs, market_data),
                self.mcp_client.suggest_optimization_strategies(performance_metrics),
                return_exceptions=True
            )
            
            # Combine results
            comprehensive_analysis = {
                "session_id": session_data.get("session_id"),
                "timestamp": time.time(),
                "performance_analysis": analyses[0] if not isinstance(analyses[0], Exception) else {"error": str(analyses[0])},
                "risk_analysis": analyses[1] if not isinstance(analyses[1], Exception) else {"error": str(analyses[1])},
                "trading_insights": analyses[2] if not isinstance(analyses[2], Exception) else {"error": str(analyses[2])},
                "optimization_suggestions": analyses[3] if not isinstance(analyses[3], Exception) else {"error": str(analyses[3])}
            }
            
            # Generate overall summary
            summary_result = await self.mcp_client.generate_natural_language_report(
                comprehensive_analysis, "executive_summary"
            )
            
            comprehensive_analysis["executive_summary"] = summary_result
            
            return comprehensive_analysis
            
        except Exception as e:
            self.logger.error(f"Comprehensive session analysis failed: {e}")
            return {"error": str(e), "timestamp": time.time()}
    
    async def answer_trading_question(self, question: str, context_data: Dict[str, Any]) -> str:
        """
        Answer natural language questions about trading data
        
        Args:
            question: Natural language question
            context_data: Relevant trading context
            
        Returns:
            Natural language answer
        """
        try:
            result = await self.mcp_client.query_natural_language(question, context_data)
            
            if "error" in result:
                return f"I encountered an error: {result['error']}"
            
            return result.get("answer", "I couldn't generate an answer for that question.")
            
        except Exception as e:
            self.logger.error(f"Question answering failed: {e}")
            return f"I encountered an error while processing your question: {e}"


# Example usage
async def main():
    """Example of how to use the MCP client"""
    
    # Configure LLM endpoints
    llm_endpoints = [
        "http://localhost:8004/mcp",  # Local LLM service
        "https://api.anthropic.com/mcp"  # External service (example)
    ]
    
    # Create MCP client
    client = IntelliSenseMCPClient(llm_endpoints)
    
    # Test connectivity
    connectivity = await client.test_connectivity()
    print("Connectivity test:", json.dumps(connectivity, indent=2))
    
    # Create analysis service
    analysis_service = LLMAnalysisService(client)
    
    # Example session data
    session_data = {
        "session_id": "test_session_123",
        "performance_metrics": {"latency_ms": 50, "accuracy": 0.95},
        "risk_events": [{"type": "position_limit", "severity": "medium"}],
        "correlation_logs": [{"event_id": "123", "latency": 25}],
        "market_data": {"symbol": "AAPL", "price": 150.0}
    }
    
    # Perform comprehensive analysis
    analysis = await analysis_service.analyze_session_comprehensive(session_data)
    print("Analysis result:", json.dumps(analysis, indent=2))


if __name__ == "__main__":
    asyncio.run(main())