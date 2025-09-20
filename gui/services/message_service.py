# gui/services/message_service.py - Message Broadcasting Service

import json
import logging
import time
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class MessageService:
    """
    Handles message formatting and broadcasting to WebSocket clients.
    Manages message types and ensures proper formatting.
    """
    
    def __init__(self, app_state, websocket_manager):
        self.app_state = app_state
        self.websocket_manager = websocket_manager
        
    async def broadcast_message(self, message_type: str, data: Any):
        """Broadcast a message to all connected WebSocket clients"""
        message = {
            "type": message_type,
            "data": data,
            "timestamp": time.time()
        }
        
        await self.websocket_manager.broadcast(message)
        logger.debug(f"📡 Broadcasting {message_type} to {len(self.websocket_manager.active_connections)} connections")
    
    async def process_stream_message(self, stream_name: str, message_id: str, data: dict):
        """Process a message from Redis stream and broadcast to clients"""
        # Extract JSON payload if present
        json_payload = data.get('json_payload')
        if json_payload:
            try:
                payload = json.loads(json_payload) if isinstance(json_payload, str) else json_payload
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from {stream_name}: {e}")
                return
        else:
            payload = data
            
        # Map stream names to message types
        message_type = self._get_message_type(stream_name)
        
        # Add metadata
        broadcast_data = {
            "stream": stream_name,
            "message_id": message_id,
            "payload": payload,
            "received_at": time.time()
        }
        
        # Broadcast to clients
        await self.broadcast_message(message_type, broadcast_data)
    
    def _get_message_type(self, stream_name: str) -> str:
        """Map Redis stream names to WebSocket message types"""
        # Strip prefix if present
        if ':' in stream_name:
            stream_type = stream_name.split(':')[-1]
        else:
            stream_type = stream_name
            
        # Map to WebSocket message types
        type_mapping = {
            'raw-ocr-events': 'raw_ocr',
            'cleaned-ocr-snapshots': 'processed_ocr',
            'image-grabs': 'preview_frame',
            'roi-updates': 'roi_update',
            'position-updates': 'position_update',
            'enriched-position-updates': 'enriched_position',
            'order-fills': 'order_fill',
            'order-status': 'order_status',
            'order-rejections': 'order_rejection',
            'health:core': 'health_update',
            'health:babysitter': 'babysitter_health',
            'responses:to_gui': 'command_response',
            'position-summary': 'position_summary'
        }
        
        return type_mapping.get(stream_type, stream_type)
    
    async def send_error(self, error_message: str, error_type: str = "error"):
        """Send error message to all clients"""
        await self.broadcast_message("error", {
            "type": error_type,
            "message": error_message,
            "timestamp": time.time()
        })
    
    async def send_system_status(self, status: str, details: Optional[Dict] = None):
        """Send system status update"""
        await self.broadcast_message("system_status", {
            "status": status,
            "details": details or {},
            "timestamp": time.time()
        })