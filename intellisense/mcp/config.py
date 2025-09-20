"""
MCP Configuration for IntelliSense Trading System
"""

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MCPConfig:
    """Configuration for MCP server and client capabilities"""
    
    # Server configuration
    server_enabled: bool = True
    server_name: str = "intellisense-trading"
    server_version: str = "1.0.0"
    server_port: int = 8003
    server_host: str = "localhost"
    
    # Client configuration
    client_enabled: bool = True
    external_llm_endpoints: List[str] = None
    
    # Redis integration
    redis_bridge_enabled: bool = True
    monitored_streams: List[str] = None
    
    # Tool configuration
    tools_enabled: Dict[str, bool] = None
    
    # Security and limits
    max_concurrent_requests: int = 10
    request_timeout_seconds: int = 30
    enable_request_logging: bool = True
    
    def __post_init__(self):
        """Set default values for mutable defaults"""
        if self.external_llm_endpoints is None:
            self.external_llm_endpoints = [
                "http://localhost:8004/mcp",  # Local LLM service
            ]
            
        if self.monitored_streams is None:
            self.monitored_streams = [
                "testrade:cleaned-ocr-snapshots",
                "testrade:market-data",
                "testrade:order-events",
                "testrade:trade-lifecycle",
                "testrade:risk-events",
                "testrade:maf-decisions"
            ]
            
        if self.tools_enabled is None:
            self.tools_enabled = {
                "session_management": True,
                "trading_analysis": True,
                "redis_queries": True,
                "performance_metrics": True,
                "risk_analysis": True,
                "market_data": True,
                "order_management": True,
                "system_diagnostics": True
            }
    
    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'MCPConfig':
        """Create MCPConfig from dictionary"""
        return cls(**config_dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert MCPConfig to dictionary"""
        return {
            'server_enabled': self.server_enabled,
            'server_name': self.server_name,
            'server_version': self.server_version,
            'server_port': self.server_port,
            'server_host': self.server_host,
            'client_enabled': self.client_enabled,
            'external_llm_endpoints': self.external_llm_endpoints,
            'redis_bridge_enabled': self.redis_bridge_enabled,
            'monitored_streams': self.monitored_streams,
            'tools_enabled': self.tools_enabled,
            'max_concurrent_requests': self.max_concurrent_requests,
            'request_timeout_seconds': self.request_timeout_seconds,
            'enable_request_logging': self.enable_request_logging
        }


# Default configuration instance
DEFAULT_MCP_CONFIG = MCPConfig()