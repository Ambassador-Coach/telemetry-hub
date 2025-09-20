"""
Redis-based Event Injection System for IntelliSense

This module provides a clean, isolated way for IntelliSense to inject test events
into TESTRADE via Redis commands, maintaining complete system separation.
"""

import json
import time
import logging
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime
import redis

logger = logging.getLogger(__name__)


class IntelliSenseRedisInjector:
    """
    Publishes injection commands to Redis for TESTRADE to consume.
    Maintains complete isolation - no direct TESTRADE component access.
    """
    
    def __init__(self, redis_client: redis.Redis, command_stream: str = "intellisense:injection-commands"):
        """
        Initialize the Redis injector.
        
        Args:
            redis_client: Redis client instance
            command_stream: Redis stream for publishing injection commands
        """
        self.redis_client = redis_client
        self.command_stream = command_stream
        self.session_id = None
        
        # Command types that TESTRADE will recognize
        self.INJECT_OCR = "INJECT_OCR_SNAPSHOT"
        self.INJECT_PRICE = "INJECT_PRICE_UPDATE" 
        self.INJECT_TRADE = "INJECT_TRADE_EVENT"
        self.INJECT_BROKER_RESPONSE = "INJECT_BROKER_RESPONSE"
        
        logger.info(f"IntelliSenseRedisInjector initialized with command stream: {command_stream}")
    
    def set_session_id(self, session_id: str):
        """Set the current IntelliSense session ID for tracking."""
        self.session_id = session_id
        logger.info(f"Injector session ID set to: {session_id}")
    
    def _serialize_for_redis(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Serialize a dictionary for Redis XADD.
        Redis requires all values to be strings, bytes, ints, or floats.
        Complex types (dicts, lists) must be JSON-serialized.
        """
        serialized = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                serialized[key] = json.dumps(value)
            elif value is None:
                serialized[key] = ""  # Redis doesn't accept None
            else:
                serialized[key] = value
        return serialized
    
    def inject_ocr_snapshot(self, 
                           frame_number: int,
                           symbol: str,
                           position_data: Dict[str, Any],
                           confidence: float,
                           raw_ocr_data: Optional[Dict[str, Any]] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Inject an OCR snapshot event for TESTRADE to process.
        
        This replaces direct calls to SnapshotInterpreterService.
        TESTRADE will consume this command and process it through its normal OCR pipeline.
        
        Args:
            frame_number: OCR frame number
            symbol: Trading symbol (e.g., "AAPL")
            position_data: Position information (shares, avg_price, etc.)
            confidence: OCR confidence score (0.0 to 1.0)
            raw_ocr_data: Optional raw OCR data
            metadata: Optional additional metadata
            
        Returns:
            Command ID for tracking
        """
        command_id = f"ocr_{self.session_id}_{frame_number}_{uuid.uuid4().hex[:8]}"
        
        command = {
            "command_type": self.INJECT_OCR,
            "command_id": command_id,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "perf_counter_ns": time.perf_counter_ns(),
            "payload": {
                "frame_number": frame_number,
                "symbol": symbol,
                "position_data": position_data,
                "confidence": confidence,
                "raw_ocr_data": raw_ocr_data or {},
                "metadata": metadata or {},
                "injection_source": "intellisense",
                "injection_timestamp": datetime.utcnow().isoformat()
            }
        }
        
        # Publish to Redis stream
        try:
            serialized_command = self._serialize_for_redis(command)
            self.redis_client.xadd(self.command_stream, serialized_command)
            logger.debug(f"Injected OCR snapshot command: {command_id} for frame {frame_number}")
            return command_id
        except Exception as e:
            logger.error(f"Failed to inject OCR snapshot: {e}")
            raise
    
    def inject_price_update(self,
                           symbol: str,
                           price: float,
                           size: int,
                           side: str,  # "BID" or "ASK"
                           timestamp: Optional[float] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Inject a price update event for TESTRADE to process.
        
        Args:
            symbol: Trading symbol
            price: Price value
            size: Size/volume
            side: "BID" or "ASK"
            timestamp: Optional timestamp (uses current time if not provided)
            metadata: Optional additional metadata
            
        Returns:
            Command ID for tracking
        """
        command_id = f"price_{self.session_id}_{symbol}_{uuid.uuid4().hex[:8]}"
        
        command = {
            "command_type": self.INJECT_PRICE,
            "command_id": command_id,
            "session_id": self.session_id,
            "timestamp": timestamp or time.time(),
            "perf_counter_ns": time.perf_counter_ns(),
            "payload": {
                "symbol": symbol,
                "price": price,
                "size": size,
                "side": side,
                "metadata": metadata or {},
                "injection_source": "intellisense",
                "injection_timestamp": datetime.utcnow().isoformat()
            }
        }
        
        try:
            self.redis_client.xadd(self.command_stream, self._serialize_for_redis(command))
            logger.debug(f"Injected price update command: {command_id} for {symbol} {side}@{price}")
            return command_id
        except Exception as e:
            logger.error(f"Failed to inject price update: {e}")
            raise
    
    def inject_trade_event(self,
                          order_id: str,
                          symbol: str,
                          side: str,  # "BUY" or "SELL"
                          quantity: int,
                          price: float,
                          order_type: str = "MARKET",
                          metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Inject a trade/order event for TESTRADE to process.
        
        Args:
            order_id: Order identifier
            symbol: Trading symbol
            side: "BUY" or "SELL"
            quantity: Number of shares
            price: Limit price (or 0 for market orders)
            order_type: "MARKET", "LIMIT", etc.
            metadata: Optional additional metadata
            
        Returns:
            Command ID for tracking
        """
        command_id = f"trade_{self.session_id}_{order_id}_{uuid.uuid4().hex[:8]}"
        
        command = {
            "command_type": self.INJECT_TRADE,
            "command_id": command_id,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "perf_counter_ns": time.perf_counter_ns(),
            "payload": {
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "order_type": order_type,
                "metadata": metadata or {},
                "injection_source": "intellisense",
                "injection_timestamp": datetime.utcnow().isoformat()
            }
        }
        
        try:
            self.redis_client.xadd(self.command_stream, self._serialize_for_redis(command))
            logger.debug(f"Injected trade event command: {command_id} for {symbol} {side} {quantity}@{price}")
            return command_id
        except Exception as e:
            logger.error(f"Failed to inject trade event: {e}")
            raise
    
    def inject_broker_response(self,
                              order_id: str,
                              status: str,  # "FILLED", "REJECTED", "PARTIAL", etc.
                              filled_quantity: int = 0,
                              filled_price: float = 0.0,
                              reject_reason: Optional[str] = None,
                              metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        Inject a broker response event for TESTRADE to process.
        
        Args:
            order_id: Order identifier to respond to
            status: Response status
            filled_quantity: Number of shares filled
            filled_price: Average fill price
            reject_reason: Reason if rejected
            metadata: Optional additional metadata
            
        Returns:
            Command ID for tracking
        """
        command_id = f"broker_{self.session_id}_{order_id}_{uuid.uuid4().hex[:8]}"
        
        command = {
            "command_type": self.INJECT_BROKER_RESPONSE,
            "command_id": command_id,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "perf_counter_ns": time.perf_counter_ns(),
            "payload": {
                "order_id": order_id,
                "status": status,
                "filled_quantity": filled_quantity,
                "filled_price": filled_price,
                "reject_reason": reject_reason,
                "metadata": metadata or {},
                "injection_source": "intellisense",
                "injection_timestamp": datetime.utcnow().isoformat()
            }
        }
        
        try:
            self.redis_client.xadd(self.command_stream, self._serialize_for_redis(command))
            logger.debug(f"Injected broker response command: {command_id} for order {order_id} status {status}")
            return command_id
        except Exception as e:
            logger.error(f"Failed to inject broker response: {e}")
            raise
    
    def get_injection_stats(self) -> Dict[str, Any]:
        """Get statistics about injections for this session."""
        try:
            # Get info about the command stream
            info = self.redis_client.xinfo_stream(self.command_stream)
            return {
                "stream": self.command_stream,
                "session_id": self.session_id,
                "total_messages": info.get("length", 0),
                "first_entry": info.get("first-entry"),
                "last_entry": info.get("last-entry")
            }
        except Exception as e:
            logger.error(f"Failed to get injection stats: {e}")
            return {"error": str(e)}