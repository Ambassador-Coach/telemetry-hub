# gui/handlers/handler_registry.py

import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Callable, Optional, List

logger = logging.getLogger(__name__)

def redis_handler(handler):
    """Decorator to mark a function as a Redis stream handler for clarity."""
    return handler

class HandlerRegistry:
    """
    Central registry for all Redis stream message handlers.
    This class contains the actual logic for processing different message types.
    """
    
    def __init__(self, app_state, websocket_manager, redis_service):
        self.app_state = app_state
        self.websocket_manager = websocket_manager
        self.redis_service = redis_service
        self.handlers: Dict[str, Callable] = {}
        
        self._register_all_handlers()
        logger.info(f"✅ HandlerRegistry initialized with {len(self.handlers)} specific handlers.")
    
    def get_all_stream_names(self) -> List[str]:
        """Returns a list of all stream names this registry can handle."""
        return list(self.handlers.keys())

    def get_handler(self, stream_name: str) -> Optional[Callable]:
        """Gets the handler for a specific stream, or a default generic handler."""
        return self.handlers.get(stream_name, self.handle_generic_stream_message)
    
    def _register_all_handlers(self):
        """
        Registers all known stream handlers.
        CRITICAL: These names must exactly match what the core app publishes to.
        """
        self.handlers = {
            # --- OCR & Image Streams ---
            'testrade:image-grabs': self.handle_image_grab_message,
            'testrade:raw-ocr-events': self.handle_raw_ocr_message,
            'testrade:cleaned-ocr-snapshots': self.handle_cleaned_ocr_message,
            
            # --- Health Streams ---
            'testrade:health:core': self.handle_core_health_message,
            'testrade:health:babysitter': self.handle_babysitter_health_message,
            'testrade:health:redis_instance': self.handle_redis_health_message,
            
            # --- Trading & Position Streams ---
            'testrade:position-updates': self.handle_position_updates_message,
            'testrade:enriched-position-updates': self.handle_enriched_position_updates_message,
            'testrade:order-fills': self.handle_order_fills_message,
            'testrade:order-status': self.handle_order_status_message,
            'testrade:order-rejections': self.handle_order_rejection_message,
            'testrade:position-summary': self.handle_position_summary_message,
            'testrade:account-summary': self.handle_account_summary_message,

            # --- Command Response Stream ---
            'testrade:responses:to_gui': self.handle_phase2_response_message
            
            # Add any other streams you need to handle
        }

        # Let's also log the final registered handlers for easy debugging
        logger.info(f"HandlerRegistry is configured to handle the following streams: {list(self.handlers.keys())}")
    
    def _parse_message_payload(self, raw_redis_message: Dict[str, Any]) -> Optional[Dict]:
        """Safely parses the JSON payload from a Redis message."""
        json_payload_str = raw_redis_message.get("json_payload")
        if not json_payload_str:
            return None
        try:
            return json.loads(json_payload_str)
        except (json.JSONDecodeError, TypeError):
            return None

    # --- HANDLER IMPLEMENTATIONS ---

    @redis_handler
    async def handle_image_grab_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """
        ORIGINAL LOGIC: Handles RAW images from 'testrade:image-grabs'.
        Sends a message with type 'image_grab'.
        """
        logger.info(f"--- handle_image_grab_message TRIGGERED for message {message_id} ---")
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper:
            logger.warning("handle_image_grab_message: No valid payload found.")
            return

        image_data = wrapper["payload"].get("frame_base64")
        if image_data:
            await self.websocket_manager.broadcast({
                "type": "image_grab",
                "image_data": image_data # Send at top level as before
            })
        else:
            logger.warning("handle_image_grab_message: Payload exists, but no 'frame_base64' key found.")

    @redis_handler
    async def handle_raw_ocr_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """
        ORIGINAL LOGIC: Handles 'testrade:raw-ocr-events'.
        Sends TWO separate messages: one for the PROCESSED image and one for the RAW text.
        """
        logger.info(f"--- handle_raw_ocr_message TRIGGERED for message {message_id} ---")
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper:
            logger.warning("handle_raw_ocr_message: No valid payload found.")
            return
        
        payload = wrapper["payload"]
        
        # --- 1. Send the PROCESSED image as 'preview_frame' ---
        processed_image = payload.get("processed_frame_base64")
        if processed_image:
            logger.info(f"Broadcasting 'preview_frame' ({len(processed_image)} bytes)")
            await self.websocket_manager.broadcast({
                "type": "preview_frame",
                "image_data": processed_image,
                "confidence": payload.get("overall_confidence", 0.0),
                "content": payload.get("full_raw_text", "") # Send text with it for context
            })
        else:
            logger.debug("handle_raw_ocr_message: No processed_frame_base64 found.")
            
        # --- 2. Send the RAW text as 'raw_ocr' ---
        raw_text = payload.get("full_raw_text", "")
        if raw_text:
            logger.info(f"Broadcasting 'raw_ocr' ({len(raw_text)} chars)")
            await self.websocket_manager.broadcast({
                "type": "raw_ocr",
                "content": raw_text,
                "confidence": payload.get("overall_confidence", 0.0),
                "timestamp": payload.get("frame_timestamp", time.time())
            })
        else:
            logger.debug("handle_raw_ocr_message: No full_raw_text found.")

    @redis_handler
    async def handle_cleaned_ocr_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Enhanced handler for cleaned OCR messages with full diagnostic data."""
        logger.info(f"--- handle_cleaned_ocr_message TRIGGERED for message {message_id} ---")
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper:
            logger.warning("handle_cleaned_ocr_message: No valid payload found.")
            return
        
        payload = wrapper["payload"]
        
        # Get golden timestamp or fallback to current time
        golden_timestamp = payload.get("origin_timestamp_ns")
        if golden_timestamp:
            # Convert nanoseconds to seconds for compatibility
            timestamp_seconds = golden_timestamp / 1_000_000_000
        else:
            timestamp_seconds = payload.get("frame_timestamp", time.perf_counter())
        
        if payload.get("type") == "no_trade_data":
            logger.info("Broadcasting 'processed_ocr' (no trade data)")
            await self.websocket_manager.broadcast({
                "type": "processed_ocr", 
                "no_trade_data": True,
                # Include correlation fields even for no-data messages
                "origin_correlation_id": payload.get("origin_correlation_id"),
                "original_ocr_event_id": payload.get("original_ocr_event_id"),
                "validation_id": payload.get("validation_id"),
                "frame_timestamp": payload.get("frame_timestamp", timestamp_seconds),
                "timestamp": timestamp_seconds,
                "origin_timestamp_ns": payload.get("origin_timestamp_ns"),
                "message_id": message_id
            })
        else:
            logger.info("Broadcasting 'processed_ocr' (with snapshots)")
            await self.websocket_manager.broadcast({
                "type": "processed_ocr",
                "snapshots": payload.get("snapshots", {}),
                
                # TIMESTAMPS - Using golden timestamp hierarchy
                "frame_timestamp": payload.get("frame_timestamp", timestamp_seconds),  # Original frame time
                "timestamp": timestamp_seconds,                                        # Golden timestamp preferred
                "origin_timestamp_ns": payload.get("origin_timestamp_ns"),            # Golden timestamp (nanoseconds)
                
                # CORRELATION & TRACING
                "origin_correlation_id": payload.get("origin_correlation_id"),        # Master correlation ID
                "original_ocr_event_id": payload.get("original_ocr_event_id"),        # Original event ID
                "validation_id": payload.get("validation_id"),                        # Pipeline validation ID
                
                # CONFIDENCE & QUALITY
                "confidence": payload.get("raw_ocr_confidence", 0.0),                # OCR confidence
                
                # DIAGNOSTIC DATA
                "roi_used_for_capture": payload.get("roi_used_for_capture", []),      # ROI coordinates
                "preprocessing_params_used": payload.get("preprocessing_params_used", {}), # Preprocessing params
                
                # METADATA
                "message_id": message_id                                              # Redis message ID for debugging
            })

    @redis_handler
    async def handle_babysitter_health_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handles health updates for the Babysitter service."""
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper: return
        
        payload = wrapper["payload"]
        status = payload.get("status", "unknown")
        
        # Send babysitter health update
        await self.websocket_manager.broadcast({
            "type": "health_update",
            "payload": {
                "component": "babysitter",
                "status": status,
                "details": payload
            }
        })
        
        # Also send ZMQ status based on ZMQ socket information
        zmq_sockets = payload.get("zmq_sockets", {})
        if zmq_sockets:
            # Check if all important ZMQ sockets are connected
            all_connected = (
                zmq_sockets.get("bulk_pull_bound", False) and
                zmq_sockets.get("trading_pull_bound", False) and
                zmq_sockets.get("system_pull_bound", False) and
                zmq_sockets.get("core_command_push_connected", False) and
                zmq_sockets.get("ocr_command_push_connected", False)
            )
            
            zmq_status = "healthy" if all_connected else "error"
            
            await self.websocket_manager.broadcast({
                "type": "health_update",
                "payload": {
                    "component": "zmq",
                    "status": zmq_status,
                    "details": zmq_sockets
                }
            })

    @redis_handler
    async def handle_redis_health_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handles health updates for the Redis instance."""
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper: return
        
        payload = wrapper["payload"]
        status = payload.get("status", "unknown")
        
        # Map Redis status to our expected format
        mapped_status = "healthy" if "CONNECTED" in status and "HEALTHY" in status else "error"
        
        await self.websocket_manager.broadcast({
            "type": "health_update",
            "payload": {
                "component": "redis",
                "status": mapped_status,
                "details": payload
            }
        })
        
    @redis_handler
    async def handle_core_health_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handles health updates for the Core service."""
        wrapper = self._parse_message_payload(raw_redis_message)
        if not wrapper or "payload" not in wrapper: return
        
        # **FIX:** This logic is now self-contained and doesn't touch AppState.
        payload = wrapper["payload"]
        status = payload.get("status", "unknown")
        
        # Update the AppState timestamp for the frontend health check
        if status == 'healthy':
            self.app_state.last_core_health_time = time.time()

        await self.websocket_manager.broadcast({
            "type": "health_update",
            "payload": {
                "component": "core",
                "status": status,
                "details": payload
            }
        })

    @redis_handler
    async def handle_generic_stream_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Default handler for any stream that isn't explicitly registered."""
        stream_name = raw_redis_message.get('stream_name_injected_by_consumer', 'unknown_stream')
        logger.debug(f"Received message {message_id} from unhandled stream '{stream_name}'. Ignoring.")

    # --- PLACEHOLDER HANDLERS FOR MISSING METHODS ---
    # These can be implemented later as needed
    
    @redis_handler
    async def handle_position_updates_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle position updates"""
        logger.debug(f"Position update message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_enriched_position_updates_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle enriched position updates"""
        logger.debug(f"Enriched position update message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_order_fills_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle order fills"""
        logger.debug(f"Order fill message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_order_status_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle order status updates"""
        logger.debug(f"Order status message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_order_rejection_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle order rejections"""
        logger.debug(f"Order rejection message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_position_summary_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle position summary"""
        logger.debug(f"Position summary message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_account_summary_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle account summary"""
        logger.debug(f"Account summary message {message_id} (handler not implemented)")
    
    @redis_handler
    async def handle_phase2_response_message(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """Handle command responses from core"""
        logger.debug(f"Phase2 response message {message_id} (handler not implemented)")