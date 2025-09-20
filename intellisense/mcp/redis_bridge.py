"""
Redis-MCP Bridge for Real-Time Trading Data

Bridges Redis streams with MCP tools for real-time LLM analysis of trading data.
"""

import json
import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, Callable, Set
from dataclasses import dataclass, asdict
from collections import defaultdict, deque

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis = None

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """Represents a Redis stream event"""
    stream_name: str
    event_id: str
    data: Dict[str, Any]
    timestamp: float
    correlation_id: Optional[str] = None
    event_type: Optional[str] = None


@dataclass
class AnalysisRule:
    """Defines when and how to trigger LLM analysis"""
    name: str
    stream_patterns: List[str]  # Stream names or patterns to monitor
    event_types: List[str]      # Event types to trigger on
    mcp_tool: str               # MCP tool to call
    condition: Optional[Callable[[StreamEvent], bool]] = None  # Custom condition
    cooldown_seconds: float = 5.0  # Minimum time between triggers
    batch_size: int = 1         # Number of events to batch before analysis
    batch_timeout: float = 2.0  # Max time to wait for batch completion


class RedisMCPBridge:
    """
    Bridge between Redis streams and MCP tools for real-time LLM analysis
    
    This bridge monitors specified Redis streams and triggers LLM analysis
    based on configurable rules and patterns.
    """
    
    def __init__(self, 
                 redis_client: Any,
                 mcp_client: 'IntelliSenseMCPClient',
                 monitored_streams: List[str],
                 analysis_rules: Optional[List[AnalysisRule]] = None):
        """
        Initialize the Redis-MCP bridge
        
        Args:
            redis_client: Redis client instance
            mcp_client: MCP client for LLM communication
            monitored_streams: List of Redis streams to monitor
            analysis_rules: Rules for triggering LLM analysis
        """
        self.redis_client = redis_client
        self.mcp_client = mcp_client
        self.monitored_streams = monitored_streams
        self.analysis_rules = analysis_rules or []
        
        # State tracking
        self.is_running = False
        self.consumer_tasks: List[asyncio.Task] = []
        self.last_stream_ids: Dict[str, str] = {}
        self.rule_last_triggered: Dict[str, float] = {}
        self.event_batches: Dict[str, deque] = defaultdict(lambda: deque(maxlen=100))
        
        # Statistics
        self.stats = {
            "events_processed": 0,
            "llm_analyses_triggered": 0,
            "errors": 0,
            "start_time": None
        }
        
        logger.info(f"Redis-MCP Bridge initialized with {len(monitored_streams)} streams and {len(analysis_rules)} rules")
    
    def add_analysis_rule(self, rule: AnalysisRule):
        """Add a new analysis rule"""
        self.analysis_rules.append(rule)
        logger.info(f"Added analysis rule: {rule.name}")
    
    def remove_analysis_rule(self, rule_name: str):
        """Remove an analysis rule by name"""
        self.analysis_rules = [r for r in self.analysis_rules if r.name != rule_name]
        logger.info(f"Removed analysis rule: {rule_name}")
    
    async def start_monitoring(self):
        """Start monitoring Redis streams"""
        if self.is_running:
            logger.warning("Bridge is already running")
            return
        
        if not REDIS_AVAILABLE:
            logger.error("Redis library not available. Install with: pip install redis")
            return
        
        self.is_running = True
        self.stats["start_time"] = time.time()
        
        logger.info("Starting Redis stream monitoring...")
        
        # Initialize last stream IDs
        for stream in self.monitored_streams:
            self.last_stream_ids[stream] = "$"  # Start with latest
        
        # Start consumer tasks for each stream
        for stream in self.monitored_streams:
            task = asyncio.create_task(self._consume_stream(stream))
            self.consumer_tasks.append(task)
        
        # Start batch processing task
        batch_task = asyncio.create_task(self._process_batches())
        self.consumer_tasks.append(batch_task)
        
        logger.info(f"Started monitoring {len(self.monitored_streams)} Redis streams")
    
    async def stop_monitoring(self):
        """Stop monitoring Redis streams"""
        if not self.is_running:
            return
        
        self.is_running = False
        logger.info("Stopping Redis stream monitoring...")
        
        # Cancel all consumer tasks
        for task in self.consumer_tasks:
            task.cancel()
        
        # Wait for tasks to complete
        await asyncio.gather(*self.consumer_tasks, return_exceptions=True)
        self.consumer_tasks.clear()
        
        logger.info("Redis stream monitoring stopped")
    
    async def _consume_stream(self, stream_name: str):
        """Consume events from a specific Redis stream"""
        try:
            while self.is_running:
                try:
                    # Read from stream
                    streams = {stream_name: self.last_stream_ids[stream_name]}
                    result = await self.redis_client.xread(streams, count=10, block=1000)
                    
                    if result:
                        for stream, messages in result:
                            for message_id, fields in messages:
                                try:
                                    event = self._parse_stream_event(
                                        stream_name, message_id, fields
                                    )
                                    await self._process_stream_event(event)
                                    self.last_stream_ids[stream_name] = message_id
                                    self.stats["events_processed"] += 1
                                    
                                except Exception as e:
                                    logger.error(f"Error processing event from {stream_name}: {e}")
                                    self.stats["errors"] += 1
                
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error consuming stream {stream_name}: {e}")
                    self.stats["errors"] += 1
                    await asyncio.sleep(1.0)  # Back off on error
                    
        except asyncio.CancelledError:
            logger.info(f"Stream consumer for {stream_name} cancelled")
        except Exception as e:
            logger.error(f"Stream consumer for {stream_name} failed: {e}")
    
    def _parse_stream_event(self, stream_name: str, message_id: str, fields: Dict) -> StreamEvent:
        """Parse a Redis stream message into a StreamEvent"""
        try:
            # Parse JSON payload if present
            data = {}
            for key, value in fields.items():
                if isinstance(value, bytes):
                    value = value.decode('utf-8')
                
                # Try to parse JSON
                if key == 'payload' or key == 'data':
                    try:
                        data.update(json.loads(value))
                    except json.JSONDecodeError:
                        data[key] = value
                else:
                    data[key] = value
            
            # Extract metadata
            correlation_id = data.get('correlation_id') or data.get('event_id')
            event_type = data.get('event_type') or data.get('type')
            
            return StreamEvent(
                stream_name=stream_name,
                event_id=message_id,
                data=data,
                timestamp=time.time(),
                correlation_id=correlation_id,
                event_type=event_type
            )
            
        except Exception as e:
            logger.error(f"Error parsing stream event: {e}")
            return StreamEvent(
                stream_name=stream_name,
                event_id=message_id,
                data={"parse_error": str(e), "raw_fields": str(fields)},
                timestamp=time.time()
            )
    
    async def _process_stream_event(self, event: StreamEvent):
        """Process a stream event against analysis rules"""
        for rule in self.analysis_rules:
            try:
                if await self._should_trigger_rule(rule, event):
                    await self._trigger_rule_analysis(rule, event)
            except Exception as e:
                logger.error(f"Error processing rule {rule.name} for event: {e}")
    
    async def _should_trigger_rule(self, rule: AnalysisRule, event: StreamEvent) -> bool:
        """Check if a rule should be triggered for an event"""
        
        # Check stream pattern match
        stream_match = any(
            pattern in event.stream_name or event.stream_name in pattern
            for pattern in rule.stream_patterns
        )
        if not stream_match:
            return False
        
        # Check event type match
        if rule.event_types and event.event_type:
            event_type_match = event.event_type in rule.event_types
            if not event_type_match:
                return False
        
        # Check custom condition
        if rule.condition and not rule.condition(event):
            return False
        
        # Check cooldown
        last_triggered = self.rule_last_triggered.get(rule.name, 0)
        if time.time() - last_triggered < rule.cooldown_seconds:
            return False
        
        return True
    
    async def _trigger_rule_analysis(self, rule: AnalysisRule, event: StreamEvent):
        """Trigger LLM analysis for a rule"""
        try:
            # Handle batching
            if rule.batch_size > 1:
                self.event_batches[rule.name].append(event)
                if len(self.event_batches[rule.name]) < rule.batch_size:
                    return  # Wait for more events
                
                # Process batch
                events = list(self.event_batches[rule.name])
                self.event_batches[rule.name].clear()
            else:
                events = [event]
            
            # Prepare analysis data
            analysis_data = {
                "rule_name": rule.name,
                "events": [asdict(e) for e in events],
                "trigger_timestamp": time.time(),
                "stream_context": {
                    "monitored_streams": self.monitored_streams,
                    "recent_activity": await self._get_recent_activity_summary()
                }
            }
            
            # Call MCP tool
            logger.info(f"Triggering LLM analysis: {rule.name} -> {rule.mcp_tool}")
            
            result = await self.mcp_client._call_llm_tool(rule.mcp_tool, analysis_data)
            
            # Handle analysis result
            await self._handle_analysis_result(rule, events, result)
            
            # Update statistics
            self.rule_last_triggered[rule.name] = time.time()
            self.stats["llm_analyses_triggered"] += 1
            
        except Exception as e:
            logger.error(f"Error triggering analysis for rule {rule.name}: {e}")
            self.stats["errors"] += 1
    
    async def _process_batches(self):
        """Process event batches with timeout"""
        try:
            while self.is_running:
                current_time = time.time()
                
                for rule in self.analysis_rules:
                    if rule.batch_size <= 1:
                        continue
                    
                    batch = self.event_batches.get(rule.name)
                    if not batch:
                        continue
                    
                    # Check if batch has timed out
                    oldest_event_time = batch[0].timestamp if batch else current_time
                    if current_time - oldest_event_time >= rule.batch_timeout and len(batch) > 0:
                        # Process partial batch
                        events = list(batch)
                        batch.clear()
                        
                        try:
                            await self._trigger_rule_analysis(rule, events[0])  # Use first event as trigger
                        except Exception as e:
                            logger.error(f"Error processing batch timeout for {rule.name}: {e}")
                
                await asyncio.sleep(0.5)  # Check batches every 500ms
                
        except asyncio.CancelledError:
            logger.info("Batch processor cancelled")
        except Exception as e:
            logger.error(f"Batch processor failed: {e}")
    
    async def _get_recent_activity_summary(self) -> Dict[str, Any]:
        """Get summary of recent activity across all streams"""
        try:
            summary = {
                "total_events_processed": self.stats["events_processed"],
                "analyses_triggered": self.stats["llm_analyses_triggered"],
                "active_streams": len(self.monitored_streams),
                "uptime_seconds": time.time() - (self.stats["start_time"] or time.time())
            }
            
            # Add per-stream activity if available
            stream_activity = {}
            for stream in self.monitored_streams:
                try:
                    # Get stream length
                    length = await self.redis_client.xlen(stream)
                    stream_activity[stream] = {"length": length}
                except Exception:
                    stream_activity[stream] = {"length": "unknown"}
            
            summary["stream_activity"] = stream_activity
            return summary
            
        except Exception as e:
            logger.error(f"Error getting activity summary: {e}")
            return {"error": str(e)}
    
    async def _handle_analysis_result(self, 
                                    rule: AnalysisRule, 
                                    events: List[StreamEvent], 
                                    result: Dict[str, Any]):
        """Handle the result of an LLM analysis"""
        try:
            logger.info(f"Analysis result for {rule.name}: {json.dumps(result, indent=2)}")
            
            # Check if result indicates any actions needed
            if "alerts" in result:
                for alert in result["alerts"]:
                    logger.warning(f"LLM Alert [{rule.name}]: {alert}")
            
            if "recommendations" in result:
                for rec in result["recommendations"]:
                    logger.info(f"LLM Recommendation [{rule.name}]: {rec}")
            
            # Could trigger additional actions like:
            # - Publishing to alert streams
            # - Updating system configurations
            # - Triggering additional analysis
            
        except Exception as e:
            logger.error(f"Error handling analysis result for {rule.name}: {e}")
    
    def get_bridge_stats(self) -> Dict[str, Any]:
        """Get bridge statistics"""
        uptime = 0
        if self.stats["start_time"]:
            uptime = time.time() - self.stats["start_time"]
        
        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime,
            "monitored_streams": len(self.monitored_streams),
            "analysis_rules": len(self.analysis_rules),
            "events_processed": self.stats["events_processed"],
            "llm_analyses_triggered": self.stats["llm_analyses_triggered"],
            "errors": self.stats["errors"],
            "events_per_second": self.stats["events_processed"] / max(uptime, 1),
            "last_stream_ids": dict(self.last_stream_ids),
            "active_consumer_tasks": len([t for t in self.consumer_tasks if not t.done()])
        }
    
    async def query_stream_history(self, 
                                 stream_name: str, 
                                 start_id: str = "-", 
                                 end_id: str = "+", 
                                 count: int = 100) -> List[StreamEvent]:
        """Query historical data from a stream"""
        try:
            result = await self.redis_client.xrange(stream_name, start_id, end_id, count)
            events = []
            
            for message_id, fields in result:
                event = self._parse_stream_event(stream_name, message_id, fields)
                events.append(event)
            
            return events
            
        except Exception as e:
            logger.error(f"Error querying stream history for {stream_name}: {e}")
            return []


# Predefined analysis rules for common trading scenarios
def create_default_analysis_rules() -> List[AnalysisRule]:
    """Create default analysis rules for trading scenarios"""
    
    return [
        # High-frequency OCR accuracy monitoring
        AnalysisRule(
            name="ocr_accuracy_monitor",
            stream_patterns=["testrade:cleaned-ocr-snapshots"],
            event_types=["TESTRADE_CLEANED_OCR_SNAPSHOT"],
            mcp_tool="analyze_ocr_accuracy_patterns",
            cooldown_seconds=30.0,
            batch_size=5,
            batch_timeout=10.0
        ),
        
        # Order execution latency analysis
        AnalysisRule(
            name="order_latency_analyzer",
            stream_patterns=["testrade:order-events"],
            event_types=["ORDER_PLACED", "ORDER_FILLED"],
            mcp_tool="analyze_order_execution_latency",
            cooldown_seconds=15.0,
            batch_size=3,
            batch_timeout=5.0
        ),
        
        # Risk event pattern detection
        AnalysisRule(
            name="risk_pattern_detector",
            stream_patterns=["testrade:risk-events"],
            event_types=["RISK_VIOLATION", "POSITION_LIMIT_WARNING"],
            mcp_tool="detect_risk_patterns",
            cooldown_seconds=60.0,
            batch_size=1  # Immediate analysis for risk events
        ),
        
        # Market condition analysis
        AnalysisRule(
            name="market_condition_analyzer",
            stream_patterns=["testrade:market-data"],
            event_types=["PRICE_UPDATE", "VOLUME_SPIKE"],
            mcp_tool="analyze_market_conditions",
            cooldown_seconds=120.0,
            batch_size=10,
            batch_timeout=30.0
        ),
        
        # System performance monitoring
        AnalysisRule(
            name="performance_monitor",
            stream_patterns=["testrade:system-metrics"],
            event_types=["PERFORMANCE_METRICS"],
            mcp_tool="analyze_system_performance",
            cooldown_seconds=300.0,  # 5 minutes
            batch_size=1
        )
    ]


# Example usage
async def main():
    """Example of how to use the Redis-MCP bridge"""
    
    if not REDIS_AVAILABLE:
        print("Redis library not available")
        return
    
    # Configure Redis client
    redis_client = redis.Redis(host='172.22.202.120', port=6379, db=0)
    
    # Configure MCP client (would be imported)
    from intellisense.mcp.client import IntelliSenseMCPClient
    mcp_client = IntelliSenseMCPClient(["http://localhost:8004/mcp"])
    
    # Configure monitored streams
    monitored_streams = [
        "testrade:cleaned-ocr-snapshots",
        "testrade:order-events",
        "testrade:risk-events",
        "testrade:market-data"
    ]
    
    # Create analysis rules
    analysis_rules = create_default_analysis_rules()
    
    # Create bridge
    bridge = RedisMCPBridge(redis_client, mcp_client, monitored_streams, analysis_rules)
    
    try:
        # Start monitoring
        await bridge.start_monitoring()
        
        # Run for demo period
        await asyncio.sleep(60)
        
        # Show statistics
        stats = bridge.get_bridge_stats()
        print("Bridge stats:", json.dumps(stats, indent=2))
        
    finally:
        # Stop monitoring
        await bridge.stop_monitoring()


if __name__ == "__main__":
    asyncio.run(main())