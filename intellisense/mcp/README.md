# IntelliSense MCP Integration 🤖

Complete MCP (Model Context Protocol) integration for the IntelliSense trading system, enabling seamless LLM integration for trading analysis, insights, and automation.

## 🚀 Features

### MCP Server
- **Trading Tools**: 20+ specialized tools for trading analysis
- **Session Management**: Create, monitor, and analyze trading sessions
- **Real-time Data**: Live Redis stream integration
- **Performance Metrics**: Latency, accuracy, and risk analysis
- **Natural Language Interface**: Ask questions about trading data

### MCP Client  
- **External LLM Integration**: Connect to any MCP-compatible LLM
- **Multiple Endpoints**: Failover and load balancing across LLM services
- **Comprehensive Analysis**: Performance, risk, market, and optimization analysis
- **Background Processing**: Non-blocking analysis tasks

### Redis Bridge
- **Real-time Monitoring**: Live Redis stream analysis
- **Custom Rules**: Configurable analysis triggers
- **Event Batching**: Efficient processing of high-frequency data
- **Pattern Detection**: AI-powered pattern recognition

### API Integration
- **FastAPI Endpoints**: RESTful API for LLM interactions
- **Health Monitoring**: Service status and connectivity checks
- **Natural Language Queries**: Direct chat interface
- **Background Tasks**: Async analysis generation

## 🏗️ Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   VS Code Agent │    │  External LLMs  │    │ Claude/ChatGPT  │
│                 │    │                 │    │                 │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          │ MCP Protocol         │ MCP Protocol         │ MCP Protocol
          │                      │                      │
┌─────────▼──────────────────────▼──────────────────────▼───────┐
│                    IntelliSense MCP Server                    │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────────┐ │
│  │   Trading   │ │   Session   │ │     Performance        │ │
│  │    Tools    │ │ Management  │ │      Metrics           │ │
│  └─────────────┘ └─────────────┘ └─────────────────────────┘ │
└─────────┬─────────────────────────────────────────────────────┘
          │
┌─────────▼─────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Redis Bridge   │    │   MCP Client    │    │   FastAPI       │
│                   │    │                 │    │   Integration   │
│ ┌───────────────┐ │    │ ┌─────────────┐ │    │                 │
│ │   Analysis    │ │    │ │   External  │ │    │ ┌─────────────┐ │
│ │    Rules      │ │    │ │     LLM     │ │    │ │     API     │ │
│ └───────────────┘ │    │ │ Integration │ │    │ │  Endpoints  │ │
└─────────┬─────────┘    │ └─────────────┘ │    │ └─────────────┘ │
          │              └─────────────────┘    └─────────────────┘
┌─────────▼─────────┐
│   Redis Streams   │
│                   │
│ ┌───────────────┐ │
│ │  OCR Data     │ │
│ │  Orders       │ │
│ │  Market Data  │ │
│ │  Risk Events  │ │
│ └───────────────┘ │
└───────────────────┘
```

## 🛠️ Installation

### 1. Install Dependencies
```bash
# Core MCP library
pip install mcp

# Optional dependencies
pip install redis httpx uvicorn
```

### 2. Configure Endpoints
Edit `intellisense/config/default.json`:

```json
{
  "mcp_config": {
    "server_enabled": true,
    "server_port": 8003,
    "client_enabled": true,
    "external_llm_endpoints": [
      "http://localhost:8004/mcp",
      "https://your-llm-service.com/mcp"
    ],
    "redis_bridge_enabled": true,
    "monitored_streams": [
      "testrade:cleaned-ocr-snapshots",
      "testrade:order-events",
      "testrade:risk-events"
    ]
  }
}
```

### 3. Start Services
The MCP integration automatically starts with the IntelliSense API server:

```bash
cd /mnt/c/testrade
python -m intellisense.api.main
```

## 📚 Usage Examples

### Basic Trading Analysis
```python
from intellisense.mcp.client import IntelliSenseMCPClient, LLMAnalysisService

# Initialize client
client = IntelliSenseMCPClient(["http://localhost:8004/mcp"])
analysis_service = LLMAnalysisService(client)

# Analyze trading session
session_data = {
    "session_id": "morning_trades",
    "performance_metrics": {
        "success_rate": 0.94,
        "avg_latency_ms": 45,
        "total_pnl": 1247.85
    }
}

# Get LLM insights
insights = await analysis_service.analyze_session_comprehensive(session_data)
print(insights["executive_summary"])
```

### Natural Language Queries
```python
# Ask questions about trading data
question = "Why is my success rate only 94%? How can I improve it?"
answer = await analysis_service.answer_trading_question(question, session_data)
print(answer)
```

### Real-time Monitoring
```python
from intellisense.mcp.redis_bridge import RedisMCPBridge, AnalysisRule

# Create custom analysis rule
rule = AnalysisRule(
    name="profit_detector",
    stream_patterns=["testrade:order-events"],
    event_types=["ORDER_FILLED"],
    mcp_tool="analyze_profit_opportunity",
    cooldown_seconds=30.0
)

# Start real-time monitoring
bridge = RedisMCPBridge(redis_client, mcp_client, streams, [rule])
await bridge.start_monitoring()
```

### API Integration
```bash
# Check MCP health
curl http://localhost:8002/mcp/health

# Ask natural language question
curl -X POST http://localhost:8002/mcp/query \\
  -H "Content-Type: application/json" \\
  -d '{"query": "What is my current trading performance?"}'

# Analyze specific session
curl -X POST http://localhost:8002/mcp/sessions/session_001/analyze \\
  -H "Content-Type: application/json" \\
  -d '{"analysis_type": "comprehensive"}'
```

## 🔧 Available Tools

### Session Management Tools
- `get_trading_session_status` - Get session details
- `list_all_trading_sessions` - List all sessions
- `create_trading_session` - Create new session
- `start_session_replay` - Start session replay
- `stop_session_replay` - Stop active replay

### Analysis Tools  
- `analyze_trading_performance` - Performance analysis
- `get_session_results_summary` - Results summary
- `analyze_ocr_accuracy` - OCR quality analysis
- `analyze_risk_events` - Risk pattern analysis
- `get_position_risk_summary` - Position risk assessment

### Data Query Tools
- `query_redis_stream` - Query stream data
- `get_recent_trading_events` - Recent events
- `search_correlation_events` - Find related events
- `get_latency_metrics` - Performance metrics
- `get_system_health_metrics` - System status

### Diagnostic Tools
- `diagnose_system_status` - System diagnostics
- `get_error_analysis` - Error analysis
- `get_market_data_summary` - Market data stats

## 🌐 API Endpoints

### MCP Management
- `GET /mcp/health` - MCP service health
- `GET /mcp/stats` - Service statistics

### Analysis
- `POST /mcp/sessions/{session_id}/analyze` - Session analysis
- `POST /mcp/query` - Natural language queries
- `POST /mcp/optimization/suggestions` - Get suggestions

### Real-time Data
- `GET /mcp/redis/streams` - Stream status
- `POST /mcp/redis/streams/{stream}/query` - Query stream

## 🤝 VS Code Integration

Your VS Code agent can connect directly to the IntelliSense MCP server:

```javascript
// VS Code agent configuration
const mcpClient = new MCPClient("http://localhost:8003/mcp");

// Query trading data
const result = await mcpClient.callTool("get_trading_session_status", {
  session_id: "current"
});

// Ask natural language questions
const insights = await mcpClient.callTool("process_natural_language_query", {
  query: "What should I optimize in my trading setup?",
  context_data: { timeframe: "today" }
});
```

## 🧪 Testing

Run the comprehensive test suite:

```bash
cd /mnt/c/testrade
python intellisense/mcp/examples/test_mcp_integration.py
```

Run basic usage examples:

```bash
python intellisense/mcp/examples/basic_usage_example.py
```

## 📊 Configuration Options

### MCP Server Config
```json
{
  "server_enabled": true,
  "server_name": "intellisense-trading",
  "server_port": 8003,
  "tools_enabled": {
    "session_management": true,
    "trading_analysis": true,
    "redis_queries": true,
    "performance_metrics": true,
    "risk_analysis": true
  }
}
```

### MCP Client Config
```json
{
  "client_enabled": true,
  "external_llm_endpoints": [
    "http://localhost:8004/mcp",
    "https://api.anthropic.com/mcp"
  ],
  "max_concurrent_requests": 10,
  "request_timeout_seconds": 30
}
```

### Redis Bridge Config
```json
{
  "redis_bridge_enabled": true,
  "monitored_streams": [
    "testrade:cleaned-ocr-snapshots",
    "testrade:market-data",
    "testrade:order-events",
    "testrade:trade-lifecycle",
    "testrade:risk-events"
  ]
}
```

## 🚨 Troubleshooting

### Common Issues

**MCP Library Not Found**
```bash
pip install mcp
```

**Redis Connection Failed**
- Check Redis server is running on 172.22.202.120:6379
- Verify Redis streams exist
- Check network connectivity

**LLM Endpoint Unreachable**
- Verify LLM service is running
- Check endpoint URLs in configuration
- Test connectivity with curl

**Permission Denied**
- Ensure proper Redis permissions
- Check API server permissions
- Verify MCP server port availability

### Debug Mode
Enable detailed logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Health Checks
```bash
# Check MCP health
curl http://localhost:8002/mcp/health

# Check Redis connectivity
redis-cli -h 172.22.202.120 ping

# Check API server
curl http://localhost:8002/health
```

## 🎯 Next Steps

1. **Connect Your VS Code Agent**: Update your agent to use the MCP endpoints
2. **Add Custom Analysis Rules**: Create rules for your specific trading patterns  
3. **Configure External LLMs**: Add your preferred LLM services
4. **Monitor Performance**: Use the analytics to optimize your setup
5. **Explore Advanced Features**: Experiment with custom tools and integrations

## 🤗 Support

For questions or issues:

1. Check the test scripts in `examples/`
2. Review the health endpoints
3. Enable debug logging
4. Check the main IntelliSense documentation

---

**Happy Trading with AI! 🚀📈**