# core/services/bulk_data_service.py

import os
import uuid
import base64
import logging
import threading
import time
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
import json

from interfaces.core.services import IConfigService
from interfaces.core.telemetry_interfaces import ITelemetryService

logger = logging.getLogger(__name__)


class BulkDataService:
    """
    Service for handling large binary data (like images) separately from regular telemetry.
    Stores bulk data in a temporary location and returns references for telemetry payloads.
    """
    
    def __init__(self, config_service: IConfigService, telemetry_service: ITelemetryService):
        self._config = config_service
        self._telemetry_service = telemetry_service
        
        # Configure bulk data storage
        self._bulk_data_dir = Path(self._config.get('bulk_data_dir', '/tmp/testrade_bulk'))
        self._bulk_data_dir.mkdir(parents=True, exist_ok=True)
        
        # Cleanup settings
        self._max_age_seconds = self._config.get('bulk_data_max_age_seconds', 300)  # 5 minutes
        self._cleanup_interval = 60  # Run cleanup every minute
        
        # Thread safety
        self._lock = threading.Lock()
        self._stored_items: Dict[str, float] = {}  # reference_id -> timestamp
        
        # Start cleanup thread
        self._stop_event = threading.Event()
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        
        logger.info(f"BulkDataService initialized with storage at: {self._bulk_data_dir}")
    
    def store_image(self, image_data: str, image_type: str, correlation_id: str) -> str:
        """
        Store base64 image data and return a reference ID.
        
        Args:
            image_data: Base64 encoded image data
            image_type: Type of image (e.g., 'raw_frame', 'processed_frame')
            correlation_id: Correlation ID for tracking
            
        Returns:
            Reference ID that can be used to retrieve the image
        """
        try:
            # Generate unique reference ID
            reference_id = f"{image_type}_{correlation_id}_{uuid.uuid4().hex[:8]}"
            
            # Create file path
            file_path = self._bulk_data_dir / f"{reference_id}.json"
            
            # Store image data with metadata
            bulk_data = {
                'reference_id': reference_id,
                'image_type': image_type,
                'correlation_id': correlation_id,
                'timestamp': time.time(),
                'image_data': image_data
            }
            
            # Write to file
            with open(file_path, 'w') as f:
                json.dump(bulk_data, f)
            
            # Track for cleanup
            with self._lock:
                self._stored_items[reference_id] = time.time()
            
            # Publish bulk data storage event
            if self._telemetry_service:
                self._telemetry_service.enqueue(
                    source_component="BulkDataService",
                    event_type="BULK_DATA_STORED",
                    payload={
                        'reference_id': reference_id,
                        'image_type': image_type,
                        'correlation_id': correlation_id,
                        'size_bytes': len(image_data),
                        'storage_path': str(file_path)
                    },
                    stream_override="testrade:bulk-data:metadata"
                )
            
            logger.debug(f"Stored {image_type} bulk data with reference: {reference_id}")
            return reference_id
            
        except Exception as e:
            logger.error(f"Failed to store bulk image data: {e}", exc_info=True)
            return None
    
    def retrieve_image(self, reference_id: str) -> Optional[str]:
        """
        Retrieve image data by reference ID.
        
        Args:
            reference_id: The reference ID returned by store_image
            
        Returns:
            Base64 encoded image data or None if not found
        """
        try:
            file_path = self._bulk_data_dir / f"{reference_id}.json"
            
            if not file_path.exists():
                logger.warning(f"Bulk data not found for reference: {reference_id}")
                return None
            
            with open(file_path, 'r') as f:
                bulk_data = json.load(f)
            
            return bulk_data.get('image_data')
            
        except Exception as e:
            logger.error(f"Failed to retrieve bulk image data: {e}", exc_info=True)
            return None
    
    def _cleanup_loop(self):
        """Background thread that cleans up old bulk data files."""
        logger.info("BulkDataService cleanup thread started")
        
        while not self._stop_event.is_set():
            try:
                self._cleanup_old_files()
            except Exception as e:
                logger.error(f"Error in bulk data cleanup: {e}", exc_info=True)
            
            # Wait for next cleanup cycle
            self._stop_event.wait(self._cleanup_interval)
        
        logger.info("BulkDataService cleanup thread stopped")
    
    def _cleanup_old_files(self):
        """Remove bulk data files older than max age."""
        current_time = time.time()
        cutoff_time = current_time - self._max_age_seconds
        
        with self._lock:
            # Find expired items
            expired_refs = [
                ref for ref, timestamp in self._stored_items.items()
                if timestamp < cutoff_time
            ]
            
            # Remove expired items
            for reference_id in expired_refs:
                try:
                    file_path = self._bulk_data_dir / f"{reference_id}.json"
                    if file_path.exists():
                        file_path.unlink()
                    del self._stored_items[reference_id]
                    logger.debug(f"Cleaned up expired bulk data: {reference_id}")
                except Exception as e:
                    logger.error(f"Failed to cleanup bulk data {reference_id}: {e}")
        
        if expired_refs:
            logger.info(f"Cleaned up {len(expired_refs)} expired bulk data files")
    
    def stop(self):
        """Stop the bulk data service and cleanup thread."""
        logger.info("Stopping BulkDataService...")
        self._stop_event.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5.0)
        logger.info("BulkDataService stopped")