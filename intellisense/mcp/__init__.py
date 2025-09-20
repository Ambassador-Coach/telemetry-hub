"""
IntelliSense MCP (Model Context Protocol) Integration

This module provides MCP server and client capabilities for the IntelliSense trading system,
enabling LLM integration with trading data, session management, and real-time analysis.
"""

from .server import IntelliSenseMCPServer
from .client import IntelliSenseMCPClient
from .redis_bridge import RedisMCPBridge
from .config import MCPConfig

__all__ = [
    'IntelliSenseMCPServer',
    'IntelliSenseMCPClient', 
    'RedisMCPBridge',
    'MCPConfig'
]