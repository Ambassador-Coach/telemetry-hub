"""
Basic Usage Example for IntelliSense MCP Integration

This script shows how to use the MCP integration in simple scenarios.
"""

import asyncio
import json
import logging
from typing import Dict, Any

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def example_1_simple_trading_analysis():
    """Example 1: Simple trading session analysis"""
    logger.info("📈 Example 1: Simple Trading Analysis")
    
    try:
        from intellisense.mcp.client import IntelliSenseMCPClient, LLMAnalysisService
        
        # Initialize MCP client
        client = IntelliSenseMCPClient([
            "http://localhost:8004/mcp",  # Your LLM service
            # Add your VS Code agent MCP endpoint here
        ])
        
        # Create analysis service
        analysis_service = LLMAnalysisService(client)
        
        # Sample trading session data
        session_data = {
            "session_id": "morning_session_001",
            "start_time": "09:30:00",
            "end_time": "12:00:00",
            "performance_metrics": {
                "total_trades": 47,
                "successful_trades": 43,
                "success_rate": 0.915,
                "total_pnl": 1247.85,
                "avg_trade_duration": 3.2,
                "max_drawdown": 0.02
            },
            "symbols_traded": ["AAPL", "TSLA", "NVDA", "MSFT"],
            "risk_events": [
                {"type": "position_size_warning", "symbol": "TSLA", "severity": "low"}
            ]
        }
        
        # Ask the LLM to analyze this session
        question = "How did this trading session perform? What are the key strengths and areas for improvement?"
        
        analysis = await analysis_service.answer_trading_question(question, session_data)
        
        logger.info(f"🤖 LLM Analysis: {analysis}")
        
        logger.info("✅ Example 1 completed")
        
    except Exception as e:
        logger.error(f"❌ Example 1 failed: {e}")


async def example_2_real_time_monitoring():
    """Example 2: Real-time trading data monitoring"""
    logger.info("📊 Example 2: Real-time Monitoring Setup")
    
    try:
        from intellisense.mcp.client import IntelliSenseMCPClient
        from intellisense.mcp.redis_bridge import RedisMCPBridge, AnalysisRule
        
        # Mock Redis for demo
        class DemoRedisClient:
            async def xread(self, streams, count=10, block=1000):
                await asyncio.sleep(1)  # Simulate wait
                return []  # No new data for demo
            
            async def xlen(self, stream):
                return 10  # Mock stream length
        
        # Initialize components
        redis_client = DemoRedisClient()
        mcp_client = IntelliSenseMCPClient(["http://localhost:8004/mcp"])
        
        # Define custom analysis rule
        custom_rule = AnalysisRule(
            name="profit_alert",
            stream_patterns=["testrade:order-events"],
            event_types=["ORDER_FILLED"],
            mcp_tool="analyze_profit_opportunity",
            cooldown_seconds=30.0,
            batch_size=3
        )
        
        # Create bridge with custom rule
        bridge = RedisMCPBridge(
            redis_client,
            mcp_client,
            ["testrade:order-events", "testrade:market-data"],
            [custom_rule]
        )
        
        logger.info("🔄 Starting real-time monitoring (demo mode)...")
        await bridge.start_monitoring()
        
        # Let it run for a few seconds
        await asyncio.sleep(5)
        
        # Check stats
        stats = bridge.get_bridge_stats()
        logger.info(f"📈 Monitoring stats: {json.dumps(stats, indent=2)}")
        
        await bridge.stop_monitoring()
        logger.info("🛑 Stopped monitoring")
        
        logger.info("✅ Example 2 completed")
        
    except Exception as e:
        logger.error(f"❌ Example 2 failed: {e}")


async def example_3_api_integration():
    """Example 3: Using MCP through API endpoints"""
    logger.info("🌐 Example 3: API Integration")
    
    try:
        import httpx
        
        base_url = "http://localhost:8002"
        
        async with httpx.AsyncClient() as client:
            # Check MCP health
            logger.info("🏥 Checking MCP health...")
            response = await client.get(f"{base_url}/mcp/health")
            
            if response.status_code == 200:
                health = response.json()
                logger.info(f"Health Status: {health['mcp_client_status']}")
            
            # Ask a natural language question
            logger.info("💬 Asking natural language question...")
            query_data = {
                "query": "What should I focus on to improve my trading performance?",
                "include_recent_data": True
            }
            
            response = await client.post(f"{base_url}/mcp/query", json=query_data)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"🤖 Answer: {result['answer']}")
            else:
                logger.warning(f"Query failed: {response.status_code}")
            
            # Get optimization suggestions
            logger.info("🔧 Getting optimization suggestions...")
            response = await client.post(f"{base_url}/mcp/optimization/suggestions")
            
            if response.status_code == 200:
                suggestions = response.json()
                logger.info(f"💡 Suggestions: {json.dumps(suggestions, indent=2)}")
        
        logger.info("✅ Example 3 completed")
        
    except ImportError:
        logger.warning("⚠️ httpx not available. Install with: pip install httpx")
    except Exception as e:
        logger.error(f"❌ Example 3 failed: {e}")


async def example_4_vs_code_agent_simulation():
    """Example 4: Simulating VS Code agent interactions"""
    logger.info("🆚 Example 4: VS Code Agent Simulation")
    
    try:
        from intellisense.mcp.client import IntelliSenseMCPClient
        
        # This simulates how your VS Code agent would connect
        client = IntelliSenseMCPClient(["http://localhost:8003/mcp"])  # IntelliSense MCP server
        
        # Simulate different agent queries
        agent_scenarios = [
            {
                "scenario": "Debug trading issue",
                "query": "I'm seeing inconsistent OCR results for AAPL. What might be causing this?",
                "context": {"symbol": "AAPL", "issue_type": "ocr_accuracy"}
            },
            {
                "scenario": "Performance optimization",
                "query": "My trading latency is higher than expected. What are the main bottlenecks?",
                "context": {"concern": "latency", "metric": "order_execution"}
            },
            {
                "scenario": "Risk assessment",
                "query": "Analyze my current risk exposure and suggest adjustments",
                "context": {"focus": "risk_management"}
            },
            {
                "scenario": "Strategy review",
                "query": "Generate a summary of my trading strategy effectiveness this week",
                "context": {"timeframe": "weekly", "analysis_type": "strategy"}
            }
        ]
        
        for scenario in agent_scenarios:
            logger.info(f"🎭 Scenario: {scenario['scenario']}")
            logger.info(f"🤖 Agent Query: {scenario['query']}")
            
            # This is what your VS Code agent would do
            response = await client.query_natural_language(
                scenario['query'],
                scenario['context']
            )
            
            answer = response.get('answer', 'No response received')
            logger.info(f"📝 Response: {answer}")
            logger.info("-" * 50)
            
            # Small delay between scenarios
            await asyncio.sleep(1)
        
        logger.info("✅ Example 4 completed")
        
    except Exception as e:
        logger.error(f"❌ Example 4 failed: {e}")


async def example_5_custom_analysis_rule():
    """Example 5: Creating custom analysis rules"""
    logger.info("⚙️ Example 5: Custom Analysis Rules")
    
    try:
        from intellisense.mcp.redis_bridge import AnalysisRule
        
        # Custom condition function
        def high_volume_condition(event):
            """Custom condition: trigger only on high volume events"""
            data = event.data
            volume = data.get('volume', 0)
            return volume > 100000  # High volume threshold
        
        # Create custom analysis rules
        custom_rules = [
            AnalysisRule(
                name="high_volume_detector",
                stream_patterns=["testrade:market-data"],
                event_types=["PRICE_UPDATE"],
                condition=high_volume_condition,
                mcp_tool="analyze_volume_spike",
                cooldown_seconds=60.0,
                batch_size=1
            ),
            
            AnalysisRule(
                name="profit_pattern_analyzer",
                stream_patterns=["testrade:order-events"],
                event_types=["ORDER_FILLED"],
                mcp_tool="detect_profit_patterns",
                cooldown_seconds=120.0,
                batch_size=5,
                batch_timeout=30.0
            ),
            
            AnalysisRule(
                name="risk_escalation_monitor",
                stream_patterns=["testrade:risk-events"],
                event_types=["RISK_VIOLATION"],
                mcp_tool="escalate_risk_analysis",
                cooldown_seconds=0.0,  # Immediate analysis for risk events
                batch_size=1
            )
        ]
        
        # Display rule configurations
        for rule in custom_rules:
            logger.info(f"📋 Rule: {rule.name}")
            logger.info(f"   Monitors: {rule.stream_patterns}")
            logger.info(f"   Events: {rule.event_types}")
            logger.info(f"   Tool: {rule.mcp_tool}")
            logger.info(f"   Cooldown: {rule.cooldown_seconds}s")
            logger.info(f"   Batch: {rule.batch_size} events")
            logger.info("")
        
        logger.info("✅ Example 5 completed")
        
    except Exception as e:
        logger.error(f"❌ Example 5 failed: {e}")


async def main():
    """Run all usage examples"""
    logger.info("🚀 Starting IntelliSense MCP Usage Examples")
    
    examples = [
        ("Simple Trading Analysis", example_1_simple_trading_analysis),
        ("Real-time Monitoring", example_2_real_time_monitoring),
        ("API Integration", example_3_api_integration),
        ("VS Code Agent Simulation", example_4_vs_code_agent_simulation),
        ("Custom Analysis Rules", example_5_custom_analysis_rule)
    ]
    
    for name, example_func in examples:
        logger.info(f"\n{'='*60}")
        logger.info(f"Running: {name}")
        logger.info('='*60)
        
        try:
            await example_func()
        except Exception as e:
            logger.error(f"Example '{name}' failed: {e}")
        
        # Small delay between examples
        await asyncio.sleep(2)
    
    logger.info(f"\n{'='*60}")
    logger.info("🏁 All examples completed!")
    logger.info('='*60)
    
    # Quick setup instructions
    logger.info("\n📚 Quick Setup Instructions:")
    logger.info("1. Install MCP library: pip install mcp")
    logger.info("2. Install optional dependencies: pip install httpx redis")
    logger.info("3. Configure your LLM endpoints in config/default.json")
    logger.info("4. Start your IntelliSense API server")
    logger.info("5. Run this script to see MCP integration in action!")


if __name__ == "__main__":
    asyncio.run(main())