# gui/core/websocket_manager.py - WebSocket Connection Manager

import asyncio
import json
import logging
from typing import Set, Dict, Any, Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Manages WebSocket connections for real-time communication.
    Handles connection lifecycle and message broadcasting.
    """
    
    def __init__(self, app_state):
        self.app_state = app_state
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        
    async def connect(self, websocket: WebSocket):
        """Accept and register a new WebSocket connection"""
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        
        client_info = f"{websocket.client.host}:{websocket.client.port}"
        logger.info(f"🔌 WebSocket connected: {client_info} (Total: {len(self.active_connections)})")
        
        # Send initial state to the new connection
        await self._send_initial_state(websocket)
        
    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection"""
        async with self._lock:
            self.active_connections.discard(websocket)
        
        client_info = f"{websocket.client.host}:{websocket.client.port}" if hasattr(websocket, 'client') else "unknown"
        logger.info(f"🔌 WebSocket disconnected: {client_info} (Remaining: {len(self.active_connections)})")
    
    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        
        if not self.active_connections:
            return
            
        message_str = json.dumps(message)
        disconnected = set()
        
        # Send to all connections
        async with self._lock:
            connections = list(self.active_connections)
        
        for connection in connections:
            try:
                await connection.send_text(message_str)
            except Exception as e:
                logger.debug(f"Error sending to WebSocket: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        if disconnected:
            async with self._lock:
                self.active_connections -= disconnected
            logger.info(f"Cleaned up {len(disconnected)} disconnected WebSocket(s)")
    
    async def send_to_websocket(self, websocket: WebSocket, message: dict):
        """Send message to a specific WebSocket connection"""
        try:
            await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.error(f"Error sending to specific WebSocket: {e}")
            await self.disconnect(websocket)
    
    async def _send_initial_state(self, websocket: WebSocket):
        """Send initial application state to newly connected client"""
        try:
            # Send current financial data
            initial_data = {
                "type": "initial_state",
                "data": {
                    "financial_data": self.app_state.financial_data,
                    "active_trades": self.app_state.active_trades,
                    "system_status": self.app_state.system_status,
                    "timestamp": asyncio.get_event_loop().time()
                }
            }
            await websocket.send_text(json.dumps(initial_data))
            logger.debug("Sent initial state to new WebSocket connection")
            
        except Exception as e:
            logger.error(f"Error sending initial state: {e}")
    
    async def close_all_connections(self):
        """Close all active WebSocket connections"""
        async with self._lock:
            connections = list(self.active_connections)
            self.active_connections.clear()
        
        for connection in connections:
            try:
                await connection.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")
                
        logger.info(f"Closed {len(connections)} WebSocket connection(s)")
    
    def get_connection_count(self) -> int:
        """Get number of active connections"""
        return len(self.active_connections)