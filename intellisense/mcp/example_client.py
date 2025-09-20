#!/usr/bin/env python3
"""
Example MCP Client for IntelliSense Trading System

This demonstrates how to connect to the IntelliSense MCP server
and use its tools for trading analysis.
"""

import asyncio
import json
import logging
from typing import Dict, Any, List

# Try to import MCP client components
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("ERROR: MCP library not installed. Install with: pip install mcp")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class IntelliSenseMCPClient:
    """Example client for IntelliSense MCP server"""
    
    def __init__(self):
        self.session = None
        
    async def connect(self):
        """Connect to the IntelliSense MCP server"""
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP library not available")
            
        # Configure server connection
        server_params = StdioServerParameters(
            command="python",
            args=["intellisense/mcp/server_runner.py"],
            env={}
        )
        
        # Connect to server
        self.session = await stdio_client(server_params)
        logger.info("Connected to IntelliSense MCP server")
        
        # List available tools
        tools = await self.session.list_tools()
        logger.info(f"Available tools: {len(tools)}")
        
    async def get_system_diagnostics(self) -> Dict[str, Any]:
        """Run system diagnostics"""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
            
        result = await self.session.call_tool(
            "diagnose_system_status",
            arguments={
                "include_redis_connectivity": True,
                "include_service_health": True,
                "include_recent_errors": True
            }
        )
        
        return json.loads(result[0].text)
    
    async def query_redis_stream(self, stream_name: str, count: int = 10) -> Dict[str, Any]:
        """Query a Redis stream"""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
            
        result = await self.session.call_tool(
            "query_redis_stream",
            arguments={
                "stream_name": stream_name,
                "count": count,
                "start_id": "-",
                "end_id": "+"
            }
        )
        
        return json.loads(result[0].text)
    
    async def get_recent_events(self, minutes_back: int = 15) -> List[Dict[str, Any]]:
        """Get recent trading events"""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
            
        result = await self.session.call_tool(
            "get_recent_trading_events",
            arguments={
                "minutes_back": minutes_back,
                "event_types": []  # Get all types
            }
        )
        
        data = json.loads(result[0].text)
        return data.get("events", [])
    
    async def create_session(self, name: str, description: str, test_date: str) -> str:
        """Create a new trading session"""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
            
        result = await self.session.call_tool(
            "create_trading_session",
            arguments={
                "session_name": name,
                "description": description,
                "test_date": test_date,
                "duration_minutes": 60,
                "speed_factor": 1.0
            }
        )
        
        data = json.loads(result[0].text)
        return data.get("session_id")
    
    async def analyze_performance(self, session_id: str) -> Dict[str, Any]:
        """Analyze trading performance for a session"""
        if not self.session:
            raise RuntimeError("Not connected to MCP server")
            
        result = await self.session.call_tool(
            "analyze_trading_performance",
            arguments={
                "session_id": session_id,
                "analysis_type": "comprehensive",
                "time_range_minutes": 60
            }
        )
        
        return json.loads(result[0].text)
    
    async def close(self):
        """Close the MCP connection"""
        if self.session:
            await self.session.close()
            logger.info("Disconnected from IntelliSense MCP server")


async def main():
    """Example usage of the MCP client"""
    
    client = IntelliSenseMCPClient()
    
    try:
        # Connect to server
        await client.connect()
        
        # Run diagnostics
        logger.info("\n=== System Diagnostics ===")
        diagnostics = await client.get_system_diagnostics()
        logger.info(f"Redis Status: {diagnostics.get('redis_status')}")
        logger.info(f"MCP Server Status: {diagnostics.get('mcp_server_status')}")
        
        # Query OCR stream
        logger.info("\n=== OCR Stream Data ===")
        ocr_data = await client.query_redis_stream("testrade:cleaned-ocr-snapshots", count=5)
        logger.info(f"Found {ocr_data.get('count', 0)} OCR entries")
        
        # Get recent events
        logger.info("\n=== Recent Trading Events ===")
        events = await client.get_recent_events(minutes_back=30)
        logger.info(f"Found {len(events)} recent events")
        
        # Create a test session (example)
        logger.info("\n=== Creating Test Session ===")
        # session_id = await client.create_session(
        #     name="MCP Test Session",
        #     description="Testing MCP integration",
        #     test_date="2024-01-01"
        # )
        # logger.info(f"Created session: {session_id}")
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not MCP_AVAILABLE:
        logger.error("Please install MCP: pip install mcp")
    else:
        asyncio.run(main())