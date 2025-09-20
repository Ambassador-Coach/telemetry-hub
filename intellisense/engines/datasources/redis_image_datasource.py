"""
Redis Image Data Source for IntelliSense
Consumes image-grabs stream to provide visual sense data for replay and analysis.
"""

import logging
import uuid
from typing import Optional, Callable, Dict, Any
import json
import base64
from datetime import datetime

from intellisense.capture.redis_stream_consumer_base import RedisStreamConsumerBase
from intellisense.config.session_config import TestSessionConfig

logger = logging.getLogger(__name__)

class RedisImageDataSource:
    """
    Data source that consumes TESTRADE image-grabs stream for IntelliSense visual sense replay.
    
    This provides the "visual sense" - what the trader actually SAW on screen during trading.
    Essential for correlating visual cues with trading decisions in replay analysis.
    """
    
    def __init__(self, session_config: TestSessionConfig, image_callback: Optional[Callable] = None):
        """
        Initialize Redis Image Data Source.
        
        Args:
            session_config: Session configuration with Redis settings
            image_callback: Callback function to handle received images
        """
        self.session_config = session_config
        self.image_callback = image_callback
        self.logger = logger
        self.consumer = None
        self.is_active = False

        # Generate unique consumer name for this instance
        self.consumer_name = f"{session_config.redis_consumer_name_prefix}_images_{uuid.uuid4().hex[:4]}"

        # Memory management for large image streams
        self.max_image_size_mb = 5  # Skip images larger than 5MB
        self.processed_count = 0
        self.skipped_count = 0
        self.total_bytes_processed = 0

        self.logger.info(f"👁️ Visual Sense: RedisImageDataSource initialized with memory management (max {self.max_image_size_mb}MB per image)")
    
    def start(self) -> bool:
        """Start consuming image data from Redis stream."""
        try:
            if self.is_active:
                self.logger.warning("RedisImageDataSource already active")
                return True
                
            # Create Redis stream consumer for image grabs
            self.consumer = RedisStreamConsumerBase(
                stream_name=self.session_config.redis_stream_image_grabs,
                consumer_group_name=self.session_config.redis_consumer_group_name,
                consumer_name=self.consumer_name,
                redis_host=self.session_config.redis_host,
                redis_port=self.session_config.redis_port,
                redis_db=self.session_config.redis_db,
                redis_password=self.session_config.redis_password,
                message_handler=self._on_image_message_received,
                start_id='$'  # Start from latest for live replay
            )
            
            if self.consumer.start():
                self.is_active = True
                self.logger.info(f"✅ Visual Sense: Started consuming images from '{self.session_config.redis_stream_image_grabs}'")
                return True
            else:
                self.logger.error("❌ Visual Sense: Failed to start image consumer")
                return False
                
        except Exception as e:
            self.logger.error(f"❌ Visual Sense: Error starting image data source: {e}", exc_info=True)
            return False
    
    def stop(self):
        """Stop consuming image data."""
        try:
            if self.consumer and self.is_active:
                self.consumer.stop_consuming()
                self.is_active = False
                self.logger.info("🛑 Visual Sense: Stopped image data consumption")
        except Exception as e:
            self.logger.error(f"Error stopping image data source: {e}", exc_info=True)
    
    def _on_image_message_received(self, message_id: str, raw_redis_message: Dict[str, Any]):
        """
        Handle incoming image messages from Redis stream.
        
        Args:
            message_id: Redis message ID
            raw_redis_message: Raw message data from Redis
        """
        try:
            # Parse the Redis message structure
            json_payload_str = raw_redis_message.get("json_payload")
            if not json_payload_str:
                self.logger.warning(f"Visual Sense: Image message {message_id} missing json_payload")
                return
            
            # Parse JSON wrapper
            wrapper_dict = json.loads(json_payload_str)
            metadata = wrapper_dict.get("metadata", {})
            payload = wrapper_dict.get("payload", {})
            
            # Extract image data
            image_data = self._extract_image_data(payload)
            if not image_data:
                self.logger.warning(f"Visual Sense: No valid image data in message {message_id}")
                return

            # Memory management - skip very large images
            image_size_mb = image_data.get("size_bytes", 0) / (1024 * 1024)
            if image_size_mb > self.max_image_size_mb:
                self.skipped_count += 1
                self.logger.warning(f"👁️ Visual Sense: Skipping large image {message_id} ({image_size_mb:.1f}MB > {self.max_image_size_mb}MB limit)")
                return
            
            # Create visual sense event
            visual_event = {
                "event_id": metadata.get("eventId", message_id),
                "correlation_id": metadata.get("correlationId"),
                "timestamp_ns": metadata.get("timestamp_ns"),
                "source_component": metadata.get("sourceComponent"),
                "image_type": payload.get("image_type", "unknown"),
                "image_data": image_data,
                "roi_coordinates": payload.get("roi_coordinates"),
                "capture_timestamp": payload.get("timestamp"),
                "frame_number": payload.get("frame_number"),
                "processing_info": payload.get("processing_info", {})
            }
            
            # Forward to callback for processing
            if self.image_callback:
                self.image_callback(visual_event)

            # Update processing statistics
            self.processed_count += 1
            self.total_bytes_processed += image_data.get("size_bytes", 0)

            # Log progress every 100 images
            if self.processed_count % 100 == 0:
                total_mb = self.total_bytes_processed / (1024 * 1024)
                self.logger.info(f"👁️ Visual Sense: Processed {self.processed_count} images ({total_mb:.1f}MB total, {self.skipped_count} skipped)")

            self.logger.debug(f"👁️ Visual Sense: Processed image {message_id} - {image_data['format']} ({image_data['size_bytes']} bytes)")
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Visual Sense: JSON decode error for message {message_id}: {e}")
        except Exception as e:
            self.logger.error(f"Visual Sense: Error processing image message {message_id}: {e}", exc_info=True)
    
    def _extract_image_data(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract and validate image data from payload.
        
        Args:
            payload: Message payload containing image data
            
        Returns:
            Extracted image data or None if invalid
        """
        try:
            # Handle different image data formats
            image_base64 = None
            image_format = "unknown"
            
            # Check for base64 image data
            if "image_data" in payload:
                image_base64 = payload["image_data"]
                image_format = "base64"
            elif "base64_image" in payload:
                image_base64 = payload["base64_image"]
                image_format = "base64"
            elif "image_bytes" in payload:
                # Convert bytes to base64 if needed
                image_bytes = payload["image_bytes"]
                if isinstance(image_bytes, bytes):
                    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                    image_format = "base64_converted"
                else:
                    image_base64 = image_bytes
                    image_format = "base64"
            
            if not image_base64:
                return None
            
            # Validate base64 data
            try:
                decoded_bytes = base64.b64decode(image_base64)
                size_bytes = len(decoded_bytes)
            except Exception:
                self.logger.warning("Visual Sense: Invalid base64 image data")
                return None
            
            return {
                "format": image_format,
                "base64_data": image_base64,
                "size_bytes": size_bytes,
                "decoded_size": size_bytes
            }
            
        except Exception as e:
            self.logger.error(f"Visual Sense: Error extracting image data: {e}")
            return None
    
    def get_status(self) -> Dict[str, Any]:
        """Get current status of image data source."""
        return {
            "is_active": self.is_active,
            "consumer_name": self.consumer_name,
            "stream_name": getattr(self.session_config, 'redis_stream_image_grabs', 'N/A'),
            "consumer_group": self.session_config.redis_consumer_group_name
        }
