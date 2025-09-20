# gui/routes/route_manager.py - Consolidated Route Management

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Dict, Any
from fastapi import WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os

from utils.message_utils import create_redis_message_json

logger = logging.getLogger(__name__)

class CommandRequest(BaseModel):
    command: str
    parameters: Dict[str, Any] = {}

class StreamRequest(BaseModel):
    stream_name: str
    message: Dict[str, Any]

class RouteManager:
    """
    Consolidated route manager that handles all API endpoints.
    This brings together all your existing routes in one place.
    """
    
    def __init__(self, app, app_state, websocket_manager, redis_service, handler_registry):
        self.app = app
        self.app_state = app_state
        self.websocket_manager = websocket_manager
        self.redis_service = redis_service
        self.handler_registry = handler_registry
    
    def setup_all_routes(self):
        """Setup all API routes"""
        self._setup_websocket_routes()
        self._setup_bootstrap_api_routes()
        self._setup_control_routes()
        self._setup_health_routes()
        self._setup_gui_routes()
        logger.info("✅ All routes configured")
    
    def _setup_websocket_routes(self):
        """Setup WebSocket routes"""
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.websocket_manager.connect(websocket)
            
            try:
                # Send welcome message
                await websocket.send_text(json.dumps({
                    "type": "system_message", 
                    "message": "Connected to TESTRADE Pro API", 
                    "level": "success"
                }))
                
                # Send initial data
                await self._send_initial_data_to_websocket(websocket)
                
                # Keep connection alive
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                        
                        if message:
                            try:
                                data = json.loads(message)
                                if data.get("type") == "ping":
                                    await websocket.send_text(json.dumps({
                                        "type": "pong", 
                                        "timestamp": time.time()
                                    }))
                            except:
                                pass
                                
                    except asyncio.TimeoutError:
                        # Send heartbeat
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "heartbeat", 
                                "timestamp": time.time()
                            }))
                        except:
                            break
                            
            except WebSocketDisconnect:
                logger.info("WebSocket client disconnected gracefully")
            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)
            finally:
                await self.websocket_manager.disconnect(websocket)
    
    def _setup_bootstrap_api_routes(self):
        """Setup Bootstrap API routes - your existing API endpoints"""
        
        @self.app.get("/api/v1/positions/current", tags=["Bootstrap API"])
        async def get_current_positions():
            """Bootstrap API: Fetch current positions directly from Redis"""
            try:
                if not self.redis_service.is_connected():
                    raise HTTPException(status_code=503, detail="Redis not available")
                
                redis_client = self.redis_service.get_client()
                positions = []
                
                # Try position-summary stream first
                try:
                    entries = redis_client.xrevrange("testrade:position-summary", count=1)
                    if entries:
                        message_id, data = entries[0]
                        if 'json_payload' in data:
                            payload = json.loads(data['json_payload'])
                            if 'positions' in payload:
                                positions = payload['positions']
                except Exception as e:
                    logger.warning(f"Failed to read position-summary: {e}")
                
                # Fallback to position-updates
                if not positions:
                    try:
                        entries = redis_client.xrevrange("testrade:position-updates", count=100)
                        position_map = {}
                        for message_id, data in entries:
                            if 'json_payload' in data:
                                payload = json.loads(data['json_payload'])
                                symbol = payload.get('symbol')
                                if symbol:
                                    if payload.get('quantity', 0) != 0:
                                        position_map[symbol] = payload
                                    else:
                                        position_map.pop(symbol, None)
                        positions = list(position_map.values())
                    except Exception as e:
                        logger.warning(f"Failed to read position-updates: {e}")
                
                return {
                    "positions": positions,
                    "market_data_available": True,
                    "source": "redis_direct",
                    "timestamp": datetime.now().isoformat()
                }
                
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error in /api/v1/positions/current: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to fetch current positions")

        @self.app.get("/api/v1/account/summary", tags=["Bootstrap API"])
        async def get_account_summary():
            """Bootstrap API: Fetch account summary directly from Redis"""
            try:
                redis_client = self.redis_service.get_client()
                
                # Try account-updates first, fallback to account-summary
                entries = redis_client.xrevrange("testrade:account-updates", count=1)
                if not entries:
                    entries = redis_client.xrevrange("testrade:account-summary", count=1)
                
                if entries:
                    entry_id, data = entries[0]
                    json_payload = data.get('json_payload') or data.get(b'json_payload', '{}')
                    if isinstance(json_payload, bytes):
                        json_payload = json_payload.decode('utf-8')
                    
                    wrapper = json.loads(json_payload)
                    account_data = wrapper.get('payload', {})
                    
                    return self.handler_registry.transform_account_data_for_frontend(account_data, is_bootstrap=True)
                else:
                    # Return default values
                    return {
                        "account_id": "",
                        "account_value": 0.0,
                        "buying_power": 0.0,
                        "cash": 0.0,
                        "realized_pnl_day": 0.0,
                        "unrealized_pnl_day": 0.0,
                        "day_pnl": 0.0,
                        "total_pnl": 0.0,
                        "is_bootstrap": True
                    }
                    
            except Exception as e:
                logger.error(f"Error in /api/v1/account/summary: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to fetch account summary")

        @self.app.get("/api/v1/orders/today", tags=["Bootstrap API"])
        async def get_orders_today():
            """Bootstrap API: Fetch today's orders directly from Redis"""
            try:
                redis_client = self.redis_service.get_client()
                orders = []
                today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                
                order_streams = [
                    "testrade:open-orders",
                    "testrade:order-fills", 
                    "testrade:order-status",
                    "testrade:order-requests"
                ]
                
                for stream in order_streams:
                    try:
                        entries = redis_client.xrevrange(stream, count=200)
                        for message_id, data in entries:
                            if 'json_payload' in data:
                                payload = json.loads(data['json_payload'])
                                order_time = payload.get('timestamp', 0)
                                if isinstance(order_time, (int, float)):
                                    order_dt = datetime.fromtimestamp(order_time)
                                    if order_dt >= today_start:
                                        payload['source_stream'] = stream
                                        orders.append(payload)
                    except Exception as e:
                        logger.warning(f"Failed to read from {stream}: {e}")
                
                # De-duplicate orders
                unique_orders = {}
                for order in orders:
                    order_id = order.get('order_id') or order.get('orderId')
                    if order_id:
                        if order_id not in unique_orders or order.get('timestamp', 0) > unique_orders[order_id].get('timestamp', 0):
                            unique_orders[order_id] = order
                    else:
                        unique_orders[f"no_id_{len(unique_orders)}"] = order
                
                return {
                    "orders": list(unique_orders.values()),
                    "count": len(unique_orders),
                    "source": "redis_direct",
                    "timestamp": datetime.now().isoformat()
                }
                
            except Exception as e:
                logger.error(f"Error in /api/v1/orders/today: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to fetch today's orders")

        @self.app.get("/api/v1/market/status", tags=["Bootstrap API"])
        async def get_market_status():
            """Bootstrap API: Get market status"""
            try:
                market_open = True
                data_sources_healthy = True
                
                if hasattr(self.app_state, 'last_core_status'):
                    data_sources_healthy = (self.app_state.last_core_status == 'healthy')
                
                return {
                    "market_open": market_open,
                    "data_sources_healthy": data_sources_healthy,
                    "connected_exchanges": ["XNYS", "XNAS"],
                    "timestamp": datetime.now().isoformat(),
                    "source": "redis_direct"
                }
                
            except Exception as e:
                logger.error(f"Error in /api/v1/market/status: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to fetch market status")

        @self.app.get("/api/v1/trades/rejections", tags=["Bootstrap API"])
        async def get_trade_rejections():
            """Bootstrap API: Fetch trade rejections"""
            try:
                if not self.redis_service.is_connected():
                    return {
                        "trades": [],
                        "total": 0,
                        "isBootstrap": True,
                        "error": "Redis not available"
                    }
                
                redis_client = self.redis_service.get_client()
                rejections = []
                
                try:
                    messages = redis_client.xrange('testrade:order-rejections')
                    
                    for message_id, data in messages:
                        try:
                            json_str = data.get('json_payload', '{}')
                            parsed_message = json.loads(json_str)
                            payload = parsed_message.get('payload', {})
                            
                            rejection_trade = {
                                "symbol": payload.get("symbol", "UNKNOWN"),
                                "side": payload.get("side", "UNKNOWN"),
                                "action": payload.get("side", "UNKNOWN"),
                                "quantity": payload.get("quantity", 0),
                                "price": payload.get("price", 0.0),
                                "status": "REJECTED",
                                "order_id": payload.get("order_id", ""),
                                "rejection_reason": payload.get("rejection_reason", "Order rejected"),
                                "timestamp": payload.get("timestamp", 0) * 1000,
                                "trade_id": f"REJ_{payload.get('order_id', str(message_id)[:8])}",
                                "is_rejection": True,
                                "pnl": 0,
                                "realized_pnl": 0,
                                "commission": 0
                            }
                            rejections.append(rejection_trade)
                        except Exception as e:
                            logger.warning(f"Error parsing rejection message {message_id}: {e}")
                            continue
                    
                    return {
                        "trades": rejections,
                        "total": len(rejections),
                        "isBootstrap": True
                    }
                    
                except Exception as redis_error:
                    logger.error(f"Redis error: {redis_error}")
                    return {
                        "trades": [],
                        "total": 0,
                        "isBootstrap": True,
                        "error": "Could not read from Redis"
                    }
                
            except Exception as e:
                logger.error(f"Error in get_trade_rejections: {e}", exc_info=True)
                return {
                    "trades": [],
                    "total": 0,
                    "isBootstrap": True,
                    "error": str(e)
                }
    
    def _setup_control_routes(self):
        """Setup control and command routes"""
        
        @self.app.post("/control/command", status_code=202)
        async def forward_command_to_core(request_data: CommandRequest):
            """Forward GUI commands to ApplicationCore via Redis"""
            if not self.redis_service.is_connected():
                raise HTTPException(status_code=503, detail="Redis connection not ready")

            # Your existing command mapping
            command_map = {
                "start_ocr": "START_OCR",
                "stop_ocr": "STOP_OCR", 
                "get_ocr_status": "GET_OCR_STATUS",
                "set_roi_absolute": "SET_ROI_ABSOLUTE",
                "roi_adjust": "ROI_ADJUST",
                "start_testrade_core": "START_APPLICATION_CORE",
                "stop_testrade_core": "STOP_APPLICATION_CORE",
                "toggle_confidence_mode": "TOGGLE_CONFIDENCE_MODE",
                "update_ocr_preprocessing_full": "UPDATE_OCR_PREPROCESSING_FULL",
                "update_trading_params": "UPDATE_TRADING_PARAMS",
                "save_configuration": "SAVE_CONFIGURATION",
                "dump_data": "DUMP_DATA",
                "get_global_configuration": "GET_GLOBAL_CONFIGURATION",
                "reset_to_default_config": "RESET_TO_DEFAULT_CONFIG",
                "emergency_stop": "EMERGENCY_STOP",
                "manual_trade": "MANUAL_TRADE",
                "force_close_all": "FORCE_CLOSE_ALL",
                "request_initial_state": "REQUEST_INITIAL_STATE",
                "get_all_orders": "GET_ALL_ORDERS"
            }
            
            command_type = command_map.get(request_data.command.lower())
            if not command_type:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Unknown command: {request_data.command}"
                )

            command_id = str(uuid.uuid4())

            # Determine target service
            target_service = "CORE_SERVICE"
            if command_type in ["SET_ROI_ABSOLUTE", "ROI_ADJUST", "START_OCR", "STOP_OCR", 
                               "GET_OCR_STATUS", "TOGGLE_CONFIDENCE_MODE", "UPDATE_OCR_PREPROCESSING_FULL"]:
                target_service = "OCR_PROCESS"

            # Create command payload
            payload_dict = {
                "command_id": command_id,
                "target_service": target_service,
                "command_type": command_type,
                "parameters": request_data.parameters,
                "timestamp": time.time(),
                "source": "GUI_Backend_Control"
            }
            
            redis_message_json = create_redis_message_json(
                payload=payload_dict,
                event_type_str="TESTRADE_GUI_COMMAND_V2",
                correlation_id_val=command_id,
                source_component_name="GUI_Backend_Control"
            )

            # Send to Redis
            target_stream = getattr(self.app_state.config, 'redis_stream_commands_from_gui', 'testrade:commands:from_gui')
            
            try:
                message_id = await self.redis_service.xadd(target_stream, {"json_payload": redis_message_json})
                logger.info(f"Command '{command_type}' sent to Redis stream with ID: {message_id}")
                
                return {
                    "status": "command_forwarded",
                    "command_id": command_id,
                    "original_command": request_data.command,
                    "target_service": target_service,
                    "core_command_type": command_type,
                    "message": f"Command forwarded to {target_service} for execution"
                }
                
            except Exception as e:
                logger.error(f"Failed to send command '{command_type}' to Redis: {e}")
                raise HTTPException(status_code=500, detail="Failed to forward command")

        @self.app.post("/control/config")
        async def update_config_endpoint(request_data: dict):
            """Update configuration via Redis commands - YOUR EXISTING LOGIC"""
            # Copy your existing update_config_endpoint logic here
            pass

        @self.app.get("/control/config/roi")
        async def get_current_roi():
            """Get current ROI coordinates"""
            try:
                return {
                    "status": "success",
                    "roi": self.app_state.current_roi,
                    "source": "live_state"
                }
            except Exception as e:
                logger.error(f"Error getting current ROI: {e}")
                return {
                    "status": "error",
                    "message": f"Failed to get ROI: {e}",
                    "roi": [64, 159, 681, 296]
                }

        @self.app.post("/stream/send", status_code=202)
        async def send_to_stream(request_data: StreamRequest):
            """Send a message to a Redis stream - for frontend compatibility"""
            if not self.redis_service.is_connected():
                raise HTTPException(status_code=503, detail="Redis connection not ready")

            try:
                # Extract command info from the message
                message = request_data.message
                command_type = message.get('command_type', 'UNKNOWN')
                command_id = message.get('command_id', str(uuid.uuid4()))
                
                # Send through the same mechanism as the control commands
                correlation_id = str(uuid.uuid4())
                
                redis_message = create_redis_message_json(
                    event_type=command_type,
                    payload=message.get('parameters', {}),
                    correlation_id=correlation_id
                )
                
                redis_client = self.redis_service.get_client()
                result = redis_client.xadd(
                    'testrade:gui-commands',
                    redis_message
                )
                
                logger.info(f"Stream message sent: {command_type} -> testrade:gui-commands")
                
                return {
                    "success": True,
                    "command_id": command_id,
                    "stream_message_id": result.decode() if isinstance(result, bytes) else str(result),
                    "method": "stream"
                }
                
            except Exception as e:
                logger.error(f"Failed to send to stream: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Stream send failed: {str(e)}")
    
    def _setup_health_routes(self):
        """Setup health monitoring routes"""
        
        @self.app.get("/health")
        async def health_check():
            """Comprehensive health check - YOUR EXISTING LOGIC"""
            health_status = {
                "status": "healthy",
                "timestamp": datetime.now().isoformat(),
                "websocket_connections": len(self.websocket_manager.active_connections),
                "components": {}
            }

            # Check Redis connectivity
            redis_status = "unknown"
            redis_issues = []
            try:
                if self.redis_service.is_connected():
                    is_ping_ok = await self.redis_service.ping()
                    if is_ping_ok:
                        redis_status = "healthy"
                    else:
                        redis_status = "error"
                        redis_issues.append("Ping failed")
                else:
                    redis_status = "error"
                    redis_issues.append("Not connected")
            except Exception as e:
                redis_status = "error"
                redis_issues.append(f"Connection error: {str(e)}")

            health_status["components"]["redis"] = {
                "status": redis_status,
                "issues": redis_issues
            }

            # Add other health checks...
            # Copy your existing health check logic

            return health_status
    
    def _setup_gui_routes(self):
        """Setup GUI serving routes"""
        
        @self.app.get("/gui", response_class=HTMLResponse)
        async def serve_gui():
            """Serve the GUI HTML with feature flag injection"""
            try:
                # Read HTML file
                html_file_path = os.path.join(os.path.dirname(__file__), "..", "portrait_trading_gui.html")
                with open(html_file_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()

                # Inject feature flag
                feature_flag_value = self.app_state.config.FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API
                feature_flag_script = f"""
    <script>
        var global_app_config = {{
            FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API: {str(feature_flag_value).lower()}
        }};
    </script>"""

                # Replace placeholder
                html_content = html_content.replace(
                    """    <script>
        // This will be populated by the backend when serving the HTML
        // For development, defaults to true (Bootstrap + XREAD mode)
        var global_app_config = {
            FEATURE_FLAG_GUI_USE_XREAD_AND_BOOTSTRAP_API: true
        };
    </script>""",
                    feature_flag_script
                )

                return HTMLResponse(content=html_content)

            except Exception as e:
                logger.error(f"Error serving GUI: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to serve GUI: {e}")
    
    async def _send_initial_data_to_websocket(self, websocket: WebSocket):
        """Send initial data to newly connected WebSocket client"""
        try:
            # Send positions
            await self._send_positions_from_redis(websocket)
            # Send account data  
            await self._send_account_data_from_redis(websocket)
            # Send open orders
            await self._send_open_orders_from_redis(websocket)
        except Exception as e:
            logger.error(f"Error sending initial data: {e}", exc_info=True)

    async def _send_positions_from_redis(self, websocket: WebSocket):
        """Send current positions from Redis to WebSocket client"""
        # Copy your existing send_positions_from_redis logic here
        pass

    async def _send_account_data_from_redis(self, websocket: WebSocket):
        """Send current account data from Redis to WebSocket client"""
        # Copy your existing send_account_data_from_redis logic here
        pass

    async def _send_open_orders_from_redis(self, websocket: WebSocket):
        """Send current open orders from Redis to WebSocket client"""
        # Copy your existing send_open_orders_from_redis logic here
        pass