#!/usr/bin/env python3
"""
TESTRADE Data Replay Service
============================

Replays historical market data through the complete TESTRADE pipeline to test:
- PriceRepository → ActiveSymbolsService → FilteredMarketDataPublisher → Redis → GUI flow
- EventBus → PositionUpdateEvent → ActiveSymbolsService filtering
- ZMQ port configurations and HWM handling

Usage:
    python data_replay_service.py --real-filter --speed 5.0

This service:
1. Creates a mock ApplicationCore with EventBus
2. Initializes real ActiveSymbolsService and FilteredMarketDataPublisher with proper ZMQ connections
3. Simulates position events to test filtering
4. Replays historical data at configurable speed
5. Tests complete market data pipeline end-to-end
"""

import argparse
import json
import logging
import time
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class MarketDataPoint:
    """Represents a single market data point (quote or trade)"""
    timestamp: float
    symbol: str
    data_type: str  # 'QUOTE' or 'TRADE'
    data: Dict[str, Any]

class MockBabysitterIPCClient:
    """Mock BabysitterIPCClient for testing"""

    def send_data(self, target_stream: str, data_json_string: str) -> bool:
        """Mock send_data method - just logs the attempt"""
        logger.info(f"MOCK_BABYSITTER: Would send to {target_stream}: {len(data_json_string)} bytes")
        return True

    def send_trading_data(self, target_stream: str, data_json_string: str, critical_retry: bool = False) -> bool:
        """Mock send_trading_data method"""
        retry_msg = " (with critical retry)" if critical_retry else ""
        logger.info(f"MOCK_BABYSITTER: Would send trading data to {target_stream}: {len(data_json_string)} bytes{retry_msg}")
        return True

    def close(self):
        """Mock close method"""
        pass

class MockApplicationCore:
    """Mock ApplicationCore for testing"""

    def __init__(self, event_bus, config, use_real_babysitter: bool = False):
        self.event_bus = event_bus
        self.config = config

        if use_real_babysitter:
            try:
                import zmq
                from core.bulletproof_ipc_client import BulletproofBabysitterIPCClient, PlaceholderMissionControlNotifier

                # Initialize ZMQ context and mission control notifier
                zmq_context = zmq.Context.instance()
                mission_notifier = PlaceholderMissionControlNotifier(logger)

                self.babysitter_ipc_client = BulletproofBabysitterIPCClient(
                    zmq_context=zmq_context,
                    ipc_config=config,
                    logger_instance=logger,
                    mission_control_notifier=mission_notifier
                )
                logger.info(f"Using REAL BulletproofBabysitterIPCClient")
            except Exception as e:
                logger.warning(f"Failed to create real BulletproofBabysitterIPCClient: {e}. Using mock.")
                self.babysitter_ipc_client = MockBabysitterIPCClient()
        else:
            self.babysitter_ipc_client = MockBabysitterIPCClient()

        # Mock the Redis message creation method
        self._create_redis_message_json = self._mock_create_redis_message_json
        
    def _mock_create_redis_message_json(self, payload, event_type_str, correlation_id_val, source_component_name, causation_id_val=None):
        """Mock Redis message creation"""
        import uuid
        return json.dumps({
            "metadata": {
                "eventId": str(uuid.uuid4()),
                "correlationId": correlation_id_val or str(uuid.uuid4()),
                "causationId": causation_id_val,
                "timestamp": time.time(),
                "eventType": event_type_str,
                "sourceComponent": source_component_name
            },
            "payload": payload
        })

class DataReplayService:
    """Service for replaying historical market data through TESTRADE pipeline"""

    def __init__(self, use_real_filter: bool = False, use_real_babysitter: bool = False, replay_speed: float = 1.0):
        self.use_real_filter = use_real_filter
        self.use_real_babysitter = use_real_babysitter
        self.replay_speed = replay_speed
        self.stop_event = threading.Event()

        # Components
        self.event_bus = None
        self.mock_app_core = None
        self.active_symbols_service = None
        self.filtered_market_data_publisher = None
        self.price_repository = None

        # Data
        self.market_data_points: List[MarketDataPoint] = []
        
    def initialize_components(self):
        """Initialize all components for data replay"""
        try:
            # Import required modules
            from core.event_bus import EventBus
            from utils.global_config import GlobalConfig
            
            # Load configuration
            config = GlobalConfig()
            logger.info("Configuration loaded")
            
            # Create EventBus
            self.event_bus = EventBus()
            logger.info("EventBus created")
            
            # Create mock ApplicationCore
            self.mock_app_core = MockApplicationCore(self.event_bus, config, use_real_babysitter=self.use_real_babysitter)
            if self.use_real_babysitter:
                logger.info("Mock ApplicationCore created with REAL BabysitterIPCClient")
            else:
                logger.info("Mock ApplicationCore created with mock BabysitterIPCClient")
            
            if self.use_real_filter:
                # Import and create new architecture services
                from modules.active_symbols.active_symbols_service import ActiveSymbolsService
                from modules.market_data_publishing.filtered_market_data_publisher import FilteredMarketDataPublisher

                # Create ActiveSymbolsService for centralized symbol relevance management
                self.active_symbols_service = ActiveSymbolsService(
                    event_bus=self.event_bus,
                    position_manager_ref=None  # No position manager in test environment
                )
                logger.info("Real ActiveSymbolsService created")

                # Create FilteredMarketDataPublisher for robust publishing
                self.filtered_market_data_publisher = FilteredMarketDataPublisher(
                    # ApplicationCore dependency removed,
                    config_service=config,
                    bulletproof_ipc_client=self.mock_app_core.babysitter_ipc_client
                )
                logger.info("Real FilteredMarketDataPublisher created")

            # Create PriceRepository with new architecture integration
            from modules.price_fetching.price_repository import PriceRepository

            self.price_repository = PriceRepository(
                config_service=config,
                event_bus=self.event_bus,
                market_data_publisher=self.filtered_market_data_publisher if self.use_real_filter else None,
                active_symbols_service=self.active_symbols_service if self.use_real_filter else None,
                # ApplicationCore dependency removed
            )
            logger.info("PriceRepository created with new architecture integration")
            
            # Start EventBus processing
            self.event_bus.start()
            logger.info("EventBus processing started")
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize components: {e}", exc_info=True)
            return False
    
    def simulate_position_open(self, symbol: str, quantity: int = 100, average_price: float = 200.0):
        """Simulate opening a position to test ActiveSymbolsService filtering"""
        if not self.use_real_filter or not self.event_bus:
            logger.info(f"Position open simulation skipped (use_real_filter={self.use_real_filter})")
            return
        
        try:
            from core.events import PositionUpdateEvent, PositionUpdateEventData
            
            # Create position open event
            position_data = PositionUpdateEventData(
                symbol=symbol,
                quantity=quantity,
                average_price=average_price,
                is_open=True,
                strategy="long",
                last_update_timestamp=time.time()
            )
            
            position_event = PositionUpdateEvent(data=position_data)
            logger.info(f"Simulated position open: {symbol} qty={quantity} @ ${average_price:.2f}")
            
            # Publish the event to EventBus
            self.event_bus.publish(position_event)
            
            # Small delay to allow filter to process
            time.sleep(0.1)
            
        except ImportError as e:
            logger.warning(f"Could not import position events: {e}")
        except Exception as e:
            logger.error(f"Error simulating position open: {e}")
    
    def load_historical_data(self, quotes_file: str, trades_file: str):
        """Load historical market data from JSON files"""
        self.market_data_points.clear()
        
        # Load quotes
        if Path(quotes_file).exists():
            with open(quotes_file, 'r') as f:
                quotes_data = json.load(f)
                for quote in quotes_data:
                    self.market_data_points.append(MarketDataPoint(
                        timestamp=quote['timestamp'],
                        symbol=quote['symbol'],
                        data_type='QUOTE',
                        data=quote
                    ))
            logger.info(f"Loaded {len([p for p in self.market_data_points if p.data_type == 'QUOTE'])} quotes")
        
        # Load trades
        if Path(trades_file).exists():
            with open(trades_file, 'r') as f:
                trades_data = json.load(f)
                for trade in trades_data:
                    self.market_data_points.append(MarketDataPoint(
                        timestamp=trade['timestamp'],
                        symbol=trade['symbol'],
                        data_type='TRADE',
                        data=trade
                    ))
            logger.info(f"Loaded {len([p for p in self.market_data_points if p.data_type == 'TRADE'])} trades")
        
        # Sort by timestamp
        self.market_data_points.sort(key=lambda x: x.timestamp)
        logger.info(f"Total data points loaded: {len(self.market_data_points)}")
        
        if self.market_data_points:
            start_time = datetime.fromtimestamp(self.market_data_points[0].timestamp).strftime('%H:%M:%S')
            end_time = datetime.fromtimestamp(self.market_data_points[-1].timestamp).strftime('%H:%M:%S')
            logger.info(f"Data time range: {start_time} - {end_time}")
    
    def start_data_replay(self, symbol: str):
        """Start replaying market data for the specified symbol"""
        if not self.market_data_points:
            logger.error("No market data loaded. Call load_historical_data() first.")
            return
        
        logger.info(f"Starting data replay for {symbol} at {self.replay_speed}x speed...")
        
        # Filter data for the specified symbol
        symbol_data = [p for p in self.market_data_points if p.symbol.upper() == symbol.upper()]
        if not symbol_data:
            logger.error(f"No data found for symbol {symbol}")
            return
        
        # Start replay in separate thread
        replay_thread = threading.Thread(
            target=self._replay_worker,
            args=(symbol_data,),
            name="DataReplayWorker"
        )
        replay_thread.start()
        logger.info("Data replay worker started")
        
        return replay_thread
    
    def _replay_worker(self, data_points: List[MarketDataPoint]):
        """Worker thread that replays market data"""
        if not data_points:
            return
        
        start_timestamp = data_points[0].timestamp
        replay_start_time = time.time()
        
        for point in data_points:
            if self.stop_event.is_set():
                break
            
            # Calculate when this data point should be sent
            elapsed_real_time = point.timestamp - start_timestamp
            target_time = replay_start_time + (elapsed_real_time / self.replay_speed)
            
            # Wait until it's time to send this data point
            current_time = time.time()
            if target_time > current_time:
                time.sleep(target_time - current_time)
            
            # Send the data point to PriceRepository
            try:
                if point.data_type == 'QUOTE':
                    self.price_repository.on_quote(
                        symbol=point.symbol,
                        bid=point.data.get('bid_price', point.data.get('bid', 0.0)),
                        ask=point.data.get('ask_price', point.data.get('ask', 0.0)),
                        bid_size=point.data.get('bid_size', 0),
                        ask_size=point.data.get('ask_size', 0),
                        timestamp=point.timestamp
                    )
                elif point.data_type == 'TRADE':
                    self.price_repository.on_trade(
                        symbol=point.symbol,
                        price=point.data.get('price', 0.0),
                        size=point.data.get('size', 0),
                        timestamp=point.timestamp
                    )
            except Exception as e:
                logger.error(f"Error sending {point.data_type} for {point.symbol}: {e}")
        
        logger.info("Data replay completed")
    
    def stop(self):
        """Stop the data replay service"""
        logger.info("Stopping data replay...")
        self.stop_event.set()

        if self.event_bus:
            self.event_bus.stop()

        # Close Babysitter client if using real one
        if self.mock_app_core and hasattr(self.mock_app_core.babysitter_ipc_client, 'close'):
            try:
                self.mock_app_core.babysitter_ipc_client.close()
                logger.info("BabysitterIPCClient closed")
            except Exception as e:
                logger.warning(f"Error closing BabysitterIPCClient: {e}")

def main():
    parser = argparse.ArgumentParser(description='TESTRADE Data Replay Service')
    parser.add_argument('--real-filter', action='store_true',
                       help='Use real ActiveSymbolsService and FilteredMarketDataPublisher instead of mock')
    parser.add_argument('--real-babysitter', action='store_true',
                       help='Use real BabysitterIPCClient to publish to Redis')
    parser.add_argument('--speed', type=float, default=5.0,
                       help='Replay speed multiplier (default: 5.0x)')

    args = parser.parse_args()

    print("TESTRADE Data Replay Service")
    print("=" * 50)
    print(f"ActiveSymbols + FilteredPublisher: {'Real' if args.real_filter else 'Mock'}")
    print(f"BabysitterIPC: {'Real' if args.real_babysitter else 'Mock'}")
    print(f"Speed: {args.speed}x")
    print("=" * 50)

    # Create and initialize service
    replay_service = DataReplayService(
        use_real_filter=args.real_filter,
        use_real_babysitter=args.real_babysitter,
        replay_speed=args.speed
    )
    
    try:
        # Initialize components
        if not replay_service.initialize_components():
            logger.error("Failed to initialize components")
            return
        
        # If using real filter, simulate AAPL position open
        if args.real_filter:
            logger.info("Simulating AAPL position open for filter testing...")
            replay_service.simulate_position_open("AAPL", quantity=100, average_price=198.0)
        
        # Load historical data
        quotes_file = "data/friday_may30_2025/AAPL_quotes_20250530_0945_1045.json"
        trades_file = "data/friday_may30_2025/AAPL_trades_20250530_0945_1045.json"
        
        try:
            replay_service.load_historical_data(quotes_file, trades_file)
        except Exception as e:
            logger.error(f"Failed to load historical data: {e}")
            return
        
        print(f"\nStarting real-time replay of AAPL data...")
        if args.real_filter and args.real_babysitter:
            print(f"FULL PIPELINE: PriceRepository → ActiveSymbolsService → FilteredMarketDataPublisher → BabysitterIPC → Redis → GUI")
            print(f"This will publish real market data to Redis streams that your GUI can consume!")
        elif args.real_filter:
            print(f"PARTIAL PIPELINE: PriceRepository → ActiveSymbolsService → FilteredMarketDataPublisher → Mock Babysitter")
            print(f"Market data filtering will work but won't reach Redis/GUI")
        else:
            print(f"TEST MODE: PriceRepository only (no filtering or Redis publishing)")
        print(f"Watch for diagnostic logs showing the data flow!")
        print(f"Press Ctrl+C to stop")
        
        # Start data replay
        replay_thread = replay_service.start_data_replay("AAPL")
        
        if replay_thread:
            try:
                replay_thread.join()
            except KeyboardInterrupt:
                print("\nCtrl+C received. Stopping replay...")
                replay_service.stop()
                replay_thread.join(timeout=2.0)
        
    except Exception as e:
        logger.error(f"Error during replay: {e}", exc_info=True)
    finally:
        replay_service.stop()
        print("Data replay service stopped.")

if __name__ == "__main__":
    main()
