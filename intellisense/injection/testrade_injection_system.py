#!/usr/bin/env python3
"""
TESTRADE Injection System for IntelliSense
==========================================

Comprehensive injection system for testing TESTRADE signal generation,
MAF filtering, and order flow. Designed for IntelliSense integration.

Key capabilities:
1. Inject positions into PositionManager
2. Inject OCR snapshots with realistic changes
3. Trigger signal generation and MAF decisions
4. Monitor results through Redis streams
"""

import json
import time
import logging
import redis
import uuid
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from enum import Enum

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class InjectionType(Enum):
    """Types of injections supported"""
    POSITION_UPDATE = "POSITION_UPDATE"
    RAW_OCR = "RAW_OCR"
    CLEANED_OCR = "CLEANED_OCR"
    MARKET_DATA = "MARKET_DATA"
    

class ScenarioType(Enum):
    """Pre-defined test scenarios"""
    COST_INCREASE = "COST_INCREASE"  # Triggers ADD signal
    COST_DECREASE = "COST_DECREASE"  # Might trigger REDUCE
    PROFIT_INCREASE = "PROFIT_INCREASE"  # Triggers REDUCE signal
    LOSS_INCREASE = "LOSS_INCREASE"  # Might trigger ADD
    POSITION_OPEN = "POSITION_OPEN"  # New position
    POSITION_CLOSE = "POSITION_CLOSE"  # Close position


class TESTRADEInjectionSystem:
    """
    Main injection system for TESTRADE testing via IntelliSense.
    Provides clean methods to inject various events and monitor results.
    """
    
    def __init__(self, redis_host='172.22.202.120', redis_port=6379):
        self.redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        self.command_stream = "testrade:commands:from_gui"
        self.raw_ocr_stream = "testrade:raw-ocr-events"
        self.cleaned_ocr_stream = "testrade:cleaned-ocr-snapshots"
        
        # Streams to monitor
        self.monitor_streams = {
            "maf_decisions": "testrade:maf-decisions",
            "order_requests": "testrade:order-requests",
            "validated_orders": "testrade:validated-orders",
            "position_updates": "testrade:position-updates"
        }
        
        # Track injections for correlation
        self.injection_history = []
        
    def inject_position(self, symbol: str, quantity: float, average_price: float, 
                       realized_pnl: float = 0.0, is_open: bool = True) -> str:
        """
        Inject a position update that will be recognized by PositionManager.
        Uses the DEBUG_SIMULATE_POSITION_UPDATE command.
        
        Returns: correlation_id of the injection
        """
        correlation_id = str(uuid.uuid4())
        
        params = {
            "symbol": symbol.upper(),
            "quantity": quantity,
            "average_price": average_price,
            "is_open": is_open,
            "realized_pnl_session": realized_pnl,
            "strategy": "intellisense_test",
            "update_source_trigger": "INTELLISENSE_POSITION_INJECTION"
        }
        
        command = self._create_gui_command("DEBUG_SIMULATE_POSITION_UPDATE", params, correlation_id)
        self.redis_client.xadd(self.command_stream, {'json_payload': json.dumps(command)})
        
        self.injection_history.append({
            "type": InjectionType.POSITION_UPDATE,
            "correlation_id": correlation_id,
            "timestamp": time.time(),
            "data": params
        })
        
        logger.info(f"✅ Injected position: {symbol} qty={quantity} @ ${average_price}")
        return correlation_id
        
    def inject_cleaned_ocr_snapshot(self, symbol: str, cost_basis: float, 
                                   realized_pnl: float = 0.0, 
                                   strategy_hint: str = "Scalp",
                                   detected_changes: Optional[Dict] = None) -> str:
        """
        Inject a cleaned OCR snapshot that will trigger signal generation.
        This goes directly to the orchestrator.
        
        Returns: correlation_id of the injection
        """
        correlation_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        
        # Build the cleaned OCR event
        event = {
            "metadata": {
                "eventId": event_id,
                "correlationId": correlation_id,
                "causationId": correlation_id,
                "timestamp_ns": time.perf_counter_ns(),
                "epoch_timestamp_s": time.time(),
                "eventType": "TESTRADE_CLEANED_OCR_SNAPSHOT",
                "sourceComponent": "IntelliSenseInjector"
            },
            "payload": {
                "frame_timestamp": time.time(),
                "snapshots": {
                    symbol.upper(): {
                        "symbol": symbol.upper(),
                        "strategy_hint": strategy_hint,
                        "cost_basis": cost_basis,
                        "pnl_per_share": 0.0,
                        "realized_pnl": realized_pnl
                    }
                },
                "original_ocr_event_id": correlation_id,
                "raw_ocr_confidence": 99.0,
                "origin_correlation_id": correlation_id
            }
        }
        
        # Add detected changes if provided
        if detected_changes:
            event["payload"]["detected_changes"] = detected_changes
            
        self.redis_client.xadd(self.cleaned_ocr_stream, {'json_payload': json.dumps(event)})
        
        self.injection_history.append({
            "type": InjectionType.CLEANED_OCR,
            "correlation_id": correlation_id,
            "timestamp": time.time(),
            "data": event["payload"]["snapshots"][symbol.upper()]
        })
        
        logger.info(f"📸 Injected cleaned OCR: {symbol} cost=${cost_basis} pnl=${realized_pnl}")
        return correlation_id
        
    def create_scenario(self, symbol: str, scenario: ScenarioType, 
                       base_position: Optional[Dict] = None) -> List[str]:
        """
        Create a complete test scenario with position and OCR changes.
        
        Args:
            symbol: Trading symbol
            scenario: Type of scenario to create
            base_position: Optional base position (will create one if not provided)
            
        Returns: List of correlation IDs for tracking
        """
        correlation_ids = []
        symbol = symbol.upper()
        
        # Default base position if not provided
        if not base_position:
            base_position = {
                "quantity": 1000,
                "average_price": 150.00,
                "realized_pnl": 0.0
            }
            
        # Inject base position first
        logger.info(f"\n🎬 Creating scenario: {scenario.value} for {symbol}")
        
        if scenario != ScenarioType.POSITION_OPEN:
            # Set up initial position
            corr_id = self.inject_position(
                symbol, 
                base_position["quantity"],
                base_position["average_price"],
                base_position["realized_pnl"]
            )
            correlation_ids.append(corr_id)
            time.sleep(1)  # Give system time to process
            
        # Now inject OCR changes based on scenario
        if scenario == ScenarioType.COST_INCREASE:
            # First snapshot - baseline
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                base_position["average_price"],
                base_position["realized_pnl"]
            )
            correlation_ids.append(corr_id)
            time.sleep(1)
            
            # Second snapshot - cost increased (triggers ADD)
            new_cost = base_position["average_price"] + 0.50
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                new_cost,
                base_position["realized_pnl"],
                detected_changes={
                    "master_change_type": "COST_INCREASE",
                    symbol: {"cost_change": 0.50, "pnl_change": 0.0}
                }
            )
            correlation_ids.append(corr_id)
            
        elif scenario == ScenarioType.PROFIT_INCREASE:
            # First snapshot - baseline
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                base_position["average_price"],
                base_position["realized_pnl"]
            )
            correlation_ids.append(corr_id)
            time.sleep(1)
            
            # Second snapshot - profit increased (triggers REDUCE)
            new_pnl = base_position["realized_pnl"] + 25.0
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                base_position["average_price"],
                new_pnl,
                detected_changes={
                    "master_change_type": "PNL_INCREASE",
                    symbol: {"cost_change": 0.0, "pnl_change": 25.0}
                }
            )
            correlation_ids.append(corr_id)
            
        elif scenario == ScenarioType.POSITION_OPEN:
            # Inject new position opening
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                base_position["average_price"],
                0.0,
                strategy_hint="Scalp",
                detected_changes={
                    "master_change_type": "POSITION_OPENED",
                    symbol: {"cost_change": base_position["average_price"], "pnl_change": 0.0}
                }
            )
            correlation_ids.append(corr_id)
            
        elif scenario == ScenarioType.POSITION_CLOSE:
            # Inject position closing
            final_pnl = base_position.get("realized_pnl", 50.0)
            corr_id = self.inject_cleaned_ocr_snapshot(
                symbol,
                0.0,  # Cost becomes 0 when closed
                final_pnl,
                strategy_hint="Closed",
                detected_changes={
                    "master_change_type": "POSITION_CLOSED",
                    symbol: {"cost_change": -base_position["average_price"], "pnl_change": final_pnl}
                }
            )
            correlation_ids.append(corr_id)
            
        return correlation_ids
        
    def monitor_results(self, duration: int = 10, 
                       correlation_ids: Optional[List[str]] = None) -> Dict[str, List]:
        """
        Monitor Redis streams for results of injections.
        
        Args:
            duration: How long to monitor (seconds)
            correlation_ids: Optional list of correlation IDs to track
            
        Returns: Dictionary of stream_name -> list of events
        """
        results = {stream: [] for stream in self.monitor_streams.keys()}
        start_time = time.time()
        last_ids = {stream: '$' for stream in self.monitor_streams.values()}
        
        logger.info(f"\n👀 Monitoring for {duration}s...")
        
        while time.time() - start_time < duration:
            for stream_key, stream_name in self.monitor_streams.items():
                try:
                    messages = self.redis_client.xread(
                        {stream_name: last_ids[stream_name]}, 
                        count=10, 
                        block=100
                    )
                    
                    for stream, stream_messages in messages:
                        for msg_id, data in stream_messages:
                            last_ids[stream_name] = msg_id
                            
                            if 'json_payload' in data:
                                event = json.loads(data['json_payload'])
                                
                                # Check if this event is related to our injections
                                event_corr_id = event['metadata'].get('correlationId')
                                if not correlation_ids or event_corr_id in correlation_ids:
                                    results[stream_key].append(event)
                                    self._display_event(stream_key, event)
                                    
                except Exception as e:
                    if "block" not in str(e).lower():
                        logger.error(f"Error monitoring {stream_name}: {e}")
                        
        return results
        
    def _display_event(self, stream_type: str, event: Dict):
        """Display event in a readable format"""
        event_type = event['metadata']['eventType']
        timestamp = datetime.fromtimestamp(event['metadata']['epoch_timestamp_s'])
        
        if stream_type == "maf_decisions":
            payload = event['payload']
            if 'SUPPRESSED' in event_type:
                logger.warning(f"\n🚫 MAF SUPPRESSION at {timestamp.strftime('%H:%M:%S')}")
                logger.warning(f"   Symbol: {payload['symbol']}")
                logger.warning(f"   Action: {payload['action']}")
                logger.warning(f"   Reason: {payload['details']['reason']}")
                logger.warning(f"   Time Remaining: {payload['details'].get('time_remaining', 'N/A'):.1f}s")
            else:
                logger.info(f"\n✅ MAF OVERRIDE at {timestamp.strftime('%H:%M:%S')}")
                logger.info(f"   Symbol: {payload['symbol']}")
                logger.info(f"   Reason: {payload['details']['reason']}")
                
        elif stream_type == "order_requests":
            payload = event['payload']
            logger.info(f"\n📝 ORDER REQUEST at {timestamp.strftime('%H:%M:%S')}")
            logger.info(f"   Symbol: {payload['symbol']}")
            logger.info(f"   Side: {payload['side']}")
            logger.info(f"   Quantity: {payload['quantity']}")
            
    def _create_gui_command(self, command_type: str, parameters: Dict, 
                           correlation_id: str) -> Dict:
        """Create a properly formatted GUI command"""
        return {
            "metadata": {
                "eventId": str(uuid.uuid4()),
                "correlationId": correlation_id,
                "causationId": None,
                "timestamp_ns": time.perf_counter_ns(),
                "eventType": "TESTRADE_GUI_COMMAND_V2",
                "sourceComponent": "IntelliSenseInjector"
            },
            "payload": {
                "command_id": correlation_id,
                "target_service": "CORE_SERVICE",
                "command_type": command_type,
                "parameters": parameters,
                "timestamp": time.time(),
                "source": "IntelliSenseInjector"
            }
        }
        
    def test_maf_suppression(self, symbol: str = "AAPL"):
        """
        Complete test of MAF suppression with tight settings.
        """
        logger.info("\n" + "="*60)
        logger.info("MAF Suppression Test")
        logger.info("="*60)
        
        # Create cost increase scenario (triggers ADD then suppression)
        logger.info("\n1️⃣ Testing ADD suppression...")
        corr_ids = self.create_scenario(symbol, ScenarioType.COST_INCREASE)
        time.sleep(2)
        
        # Try another cost increase - should be suppressed
        logger.info("\n2️⃣ Attempting another ADD (should be suppressed)...")
        corr_id = self.inject_cleaned_ocr_snapshot(
            symbol, 150.75, 0.0,
            detected_changes={
                "master_change_type": "COST_INCREASE",
                symbol: {"cost_change": 0.25, "pnl_change": 0.0}
            }
        )
        corr_ids.append(corr_id)
        
        # Monitor for MAF suppression
        results = self.monitor_results(duration=5, correlation_ids=corr_ids)
        
        # Now test REDUCE suppression
        logger.info("\n3️⃣ Testing REDUCE suppression...")
        corr_ids2 = self.create_scenario(symbol, ScenarioType.PROFIT_INCREASE)
        time.sleep(2)
        
        # Try another profit increase - should be suppressed
        logger.info("\n4️⃣ Attempting another REDUCE (should be suppressed)...")
        corr_id = self.inject_cleaned_ocr_snapshot(
            symbol, 150.00, 50.0,
            detected_changes={
                "master_change_type": "PNL_INCREASE", 
                symbol: {"cost_change": 0.0, "pnl_change": 25.0}
            }
        )
        corr_ids2.append(corr_id)
        
        # Monitor for MAF suppression
        results2 = self.monitor_results(duration=5, correlation_ids=corr_ids2)
        
        # Summary
        total_suppressions = len(results.get("maf_decisions", [])) + len(results2.get("maf_decisions", []))
        logger.info(f"\n📊 Test Summary: Found {total_suppressions} MAF events")
        

def main():
    """Example usage of the injection system"""
    injector = TESTRADEInjectionSystem()
    
    # Run MAF suppression test
    injector.test_maf_suppression("AAPL")
    

if __name__ == "__main__":
    main()