#!/usr/bin/env python3
"""
Thread-Safe UUID Generator for TESTRADE

Provides thread-safe UUID generation to prevent thread leaks on Windows.
Uses a single background thread with a queue to generate UUIDs efficiently.
"""

import threading
import uuid
import queue
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ThreadSafeUUIDGenerator:
    """
    Thread-safe UUID generator that uses a single background thread
    to generate UUIDs and a queue to distribute them.
    
    This prevents the thread creation issues that can occur with
    direct uuid.uuid4() calls on Windows systems.
    """
    
    _instance: Optional['ThreadSafeUUIDGenerator'] = None
    _lock = threading.Lock()
    
    def __init__(self, queue_size: int = 1000):
        if ThreadSafeUUIDGenerator._instance is not None:
            raise RuntimeError("ThreadSafeUUIDGenerator is a singleton. Use get_instance().")
        
        self._uuid_queue = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._generator_thread: Optional[threading.Thread] = None
        self._queue_size = queue_size
        self._generated_count = 0
        self._requested_count = 0
        
        # Pre-fill the queue
        self._start_generator()
        logger.info(f"ThreadSafeUUIDGenerator initialized with queue size {queue_size}")
    
    @classmethod
    def get_instance(cls, queue_size: int = 1000) -> 'ThreadSafeUUIDGenerator':
        """Get the singleton instance of ThreadSafeUUIDGenerator."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(queue_size)
        return cls._instance
    
    def _start_generator(self):
        """Start the background UUID generation thread."""
        if self._generator_thread and self._generator_thread.is_alive():
            return
        
        self._stop_event.clear()
        self._generator_thread = threading.Thread(
            target=self._uuid_generation_loop,
            name="UUIDGenerator",
            daemon=True
        )
        self._generator_thread.start()
        logger.debug("UUID generator thread started")
    
    def _uuid_generation_loop(self):
        """Background thread loop that generates UUIDs and fills the queue."""
        logger.debug("UUID generation loop started")
        
        while not self._stop_event.is_set():
            try:
                # Check if queue needs filling
                current_size = self._uuid_queue.qsize()
                if current_size < self._queue_size // 2:  # Fill when half empty
                    # Generate UUIDs in batches for efficiency
                    batch_size = min(50, self._queue_size - current_size)
                    for _ in range(batch_size):
                        if self._stop_event.is_set():
                            break
                        
                        new_uuid = str(uuid.uuid4())
                        try:
                            self._uuid_queue.put_nowait(new_uuid)
                            self._generated_count += 1
                        except queue.Full:
                            break  # Queue is full, stop generating
                
                # Sleep briefly to avoid busy waiting
                time.sleep(0.01)  # 10ms
                
            except Exception as e:
                logger.error(f"Error in UUID generation loop: {e}", exc_info=True)
                time.sleep(0.1)  # Longer sleep on error
        
        logger.debug("UUID generation loop stopped")
    
    def get_uuid(self, timeout: float = 1.0) -> str:
        """
        Get a UUID from the queue.
        
        Args:
            timeout: Maximum time to wait for a UUID if queue is empty
            
        Returns:
            A UUID string
            
        Raises:
            RuntimeError: If unable to get UUID within timeout
        """
        self._requested_count += 1
        
        try:
            uuid_str = self._uuid_queue.get(timeout=timeout)
            return uuid_str
        except queue.Empty:
            # Fallback: generate directly if queue is empty
            logger.warning("UUID queue empty, generating directly (potential thread creation)")
            return str(uuid.uuid4())
    
    def get_uuid_nowait(self) -> str:
        """
        Get a UUID immediately without waiting.
        
        Returns:
            A UUID string
            
        Raises:
            RuntimeError: If no UUID available immediately
        """
        try:
            return self._uuid_queue.get_nowait()
        except queue.Empty:
            # Fallback: generate directly
            logger.warning("UUID queue empty, generating directly (potential thread creation)")
            return str(uuid.uuid4())
    
    def get_stats(self) -> dict:
        """Get statistics about UUID generation."""
        return {
            'queue_size': self._uuid_queue.qsize(),
            'max_queue_size': self._queue_size,
            'generated_count': self._generated_count,
            'requested_count': self._requested_count,
            'generator_thread_alive': self._generator_thread.is_alive() if self._generator_thread else False
        }
    
    def stop(self):
        """Stop the UUID generator thread."""
        logger.info("Stopping ThreadSafeUUIDGenerator")
        self._stop_event.set()
        
        if self._generator_thread and self._generator_thread.is_alive():
            self._generator_thread.join(timeout=2.0)
            if self._generator_thread.is_alive():
                logger.warning("UUID generator thread did not stop gracefully")
        
        logger.info("ThreadSafeUUIDGenerator stopped")

# Global instance for easy access
_global_uuid_generator: Optional[ThreadSafeUUIDGenerator] = None

def get_thread_safe_uuid() -> str:
    """
    Get a thread-safe UUID string.
    
    This is the main function to use throughout the codebase
    instead of str(uuid.uuid4()).
    
    Returns:
        A UUID string
    """
    global _global_uuid_generator
    
    if _global_uuid_generator is None:
        _global_uuid_generator = ThreadSafeUUIDGenerator.get_instance()
    
    return _global_uuid_generator.get_uuid()

def get_thread_safe_uuid_nowait() -> str:
    """
    Get a thread-safe UUID string without waiting.
    
    Returns:
        A UUID string
    """
    global _global_uuid_generator
    
    if _global_uuid_generator is None:
        _global_uuid_generator = ThreadSafeUUIDGenerator.get_instance()
    
    return _global_uuid_generator.get_uuid_nowait()

def get_uuid_generator_stats() -> dict:
    """Get statistics about the global UUID generator."""
    global _global_uuid_generator
    
    if _global_uuid_generator is None:
        return {'status': 'not_initialized'}
    
    return _global_uuid_generator.get_stats()

def stop_uuid_generator():
    """Stop the global UUID generator."""
    global _global_uuid_generator
    
    if _global_uuid_generator is not None:
        _global_uuid_generator.stop()
        _global_uuid_generator = None
