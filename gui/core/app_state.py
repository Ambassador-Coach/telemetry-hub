# gui/core/app_state.py - Centralized Application State (INDEPENDENT)
import time
import os
from collections import deque
from typing import Dict, List, Optional, Any

class AppConfig:
    """Application configuration class - no external dependencies"""
    def __init__(self):
        # Load Redis connection info from control.json if available
        self._load_redis_config()
        
        # Feature flags
        self.FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API = True
        
        # Redis stream names - can be overridden by environment
        self.redis_stream_raw_ocr = os.getenv('REDIS_STREAM_RAW_OCR', 'testrade:raw-ocr-events')
        self.redis_stream_cleaned_ocr = os.getenv('REDIS_STREAM_CLEANED_OCR', 'testrade:cleaned-ocr-snapshots')
        self.redis_stream_image_grabs = os.getenv('REDIS_STREAM_IMAGE_GRABS', 'testrade:image-grabs')
        self.redis_stream_roi_updates = os.getenv('REDIS_STREAM_ROI_UPDATES', 'testrade:roi-updates')
        self.redis_stream_position_updates = os.getenv('REDIS_STREAM_POSITION_UPDATES', 'testrade:position-updates')
        self.redis_stream_enriched_position_updates = os.getenv('REDIS_STREAM_ENRICHED_POSITION_UPDATES', 'testrade:enriched-position-updates')
        self.redis_stream_order_fills = os.getenv('REDIS_STREAM_ORDER_FILLS', 'testrade:order-fills')
        self.redis_stream_order_status = os.getenv('REDIS_STREAM_ORDER_STATUS', 'testrade:order-status')
        self.redis_stream_order_rejections = os.getenv('REDIS_STREAM_ORDER_REJECTIONS', 'testrade:order-rejections')
        self.redis_stream_core_health = os.getenv('REDIS_STREAM_CORE_HEALTH', 'testrade:health:core')
        self.redis_stream_babysitter_health = os.getenv('REDIS_STREAM_BABYSITTER_HEALTH', 'testrade:health:babysitter')
        self.redis_stream_responses_to_gui = os.getenv('REDIS_STREAM_RESPONSES_TO_GUI', 'testrade:responses:to_gui')
        self.redis_stream_commands_from_gui = os.getenv('REDIS_STREAM_COMMANDS_FROM_GUI', 'testrade:commands:from_gui')
        self.redis_stream_position_summary = os.getenv('REDIS_STREAM_POSITION_SUMMARY', 'testrade:position-summary')
        
        # Performance settings
        self.GUI_PRICE_DATA_PRUNE_INTERVAL_SEC = int(os.getenv('GUI_PRICE_DATA_PRUNE_INTERVAL_SEC', '3600'))
        self.GUI_PRICE_DATA_STALE_THRESHOLD_SEC = int(os.getenv('GUI_PRICE_DATA_STALE_THRESHOLD_SEC', '7200'))
    
    def _load_redis_config(self):
        """Load Redis configuration from control.json"""
        import json
        try:
            # Try to load control.json from project root
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'utils', 'control.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    self.REDIS_HOST = config.get('REDIS_HOST', '172.22.202.120')  # WSL2 IP
                    self.REDIS_PORT = config.get('REDIS_PORT', 6379)
                    self.REDIS_DB = config.get('REDIS_DB', 0)
                    self.REDIS_PASSWORD = config.get('REDIS_PASSWORD', None)
            else:
                # Fallback to WSL2 defaults
                self.REDIS_HOST = '172.22.202.120'
                self.REDIS_PORT = 6379
                self.REDIS_DB = 0
                self.REDIS_PASSWORD = None
        except Exception as e:
            # Fallback to WSL2 defaults
            print(f"Warning: Could not load Redis config from control.json: {e}")
            self.REDIS_HOST = '172.22.202.120'
            self.REDIS_PORT = 6379
            self.REDIS_DB = 0
            self.REDIS_PASSWORD = None

class AppState:
    """Centralized application state management - fully independent"""
    
    def __init__(self, config=None):
        self.config = config or SimpleConfig()
        
        # Core system state
        self.main_event_loop: Optional[Any] = None
        self.background_tasks: List[Any] = []
        
        # Trading data
        self.active_trades: List[Dict[str, Any]] = []
        self.financial_data = {
            "dayPnL": 0.0,
            "winRate": 0.0,
            "totalTrades": 0,
            "accountValue": 50000.0,
            "buyingPower": 25000.0,
            "totalPnL": 0.0
        }
        
        # Market data
        self.price_data: Dict[str, Dict[str, Any]] = {}
        self.active_symbols = set()
        
        # System configuration
        self.current_roi = [64, 159, 681, 296]
        self.system_config = {
            "initialSize": 1000,
            "addType": "Equal",
            "reducePercent": 50,
            "ocrActive": False,
            "recordingActive": False
        }
        
        # Health tracking
        self.last_core_health_time = 0
        self.last_babysitter_health_time = 0
        self.last_core_status = None
        self.last_babysitter_status = None
        
        # Performance tracking
        self.latency_samples = deque(maxlen=10)
        self.last_message_time = time.time()
        
        # API responses (for Bootstrap pattern)
        self.pending_api_responses: Dict[str, Dict[str, Any]] = {}
        self.api_response_timeout_sec: float = 15.0
        
        # System status for WebSocket initial state
        self.system_status = {
            "core_healthy": True,
            "babysitter_healthy": True,
            "last_update": time.time()
        }
    
    def update_active_trade(self, trade_data: Dict[str, Any]) -> bool:
        """Update or add an active trade"""
        trade_id = trade_data.get("trade_id")
        if not trade_id:
            return False
            
        # Find existing trade
        for i, trade in enumerate(self.active_trades):
            if trade.get("trade_id") == trade_id:
                self.active_trades[i] = trade_data
                return True
        
        # Add new trade if not found
        self.active_trades.append(trade_data)
        return True
    
    def remove_active_trade(self, trade_id: str) -> bool:
        """Remove an active trade"""
        for i, trade in enumerate(self.active_trades):
            if trade.get("trade_id") == trade_id:
                self.active_trades.pop(i)
                return True
        return False
    
    def track_message_latency(self):
        """Track latency between messages for GUI display"""
        current_time = time.time()
        if hasattr(self, 'last_message_time'):
            latency_ms = (current_time - self.last_message_time) * 1000
            self.latency_samples.append(latency_ms)
        self.last_message_time = current_time
    
    def get_average_latency(self):
        """Get average latency from recent samples"""
        if not self.latency_samples:
            return 0.0
        return sum(self.latency_samples) / len(self.latency_samples)