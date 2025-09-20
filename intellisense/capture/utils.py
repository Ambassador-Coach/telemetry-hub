"""
IntelliSense Data Capture Utilities

This module provides utilities for data capture during live system operation,
including global sequence generation for correlation logs.
"""

import threading
from typing import Optional


class GlobalSequenceGenerator:
    """
    Thread-safe global sequence ID generator for correlation logs.
    Ensures a monotonic and unique order for all captured events across senses.
    Implemented as a singleton.
    """
    _instance: Optional['GlobalSequenceGenerator'] = None
    _lock = threading.Lock()  # Lock for singleton instantiation
    
    def __init__(self):
        if GlobalSequenceGenerator._instance is not None:
            raise RuntimeError("Singleton GlobalSequenceGenerator already instantiated. Use get_instance().")
        self._sequence_counter = 0
        self._counter_lock = threading.Lock()  # Lock for counter increment
        # print("GlobalSequenceGenerator Singleton Initialized")  # For debugging

    @classmethod
    def get_instance(cls) -> 'GlobalSequenceGenerator':
        """Get singleton instance of sequence generator."""
        if cls._instance is None:
            with cls._lock:
                # Double-check locking
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def get_next_id(self) -> int:
        """Get next global sequence ID."""
        with self._counter_lock:
            self._sequence_counter += 1
            return self._sequence_counter
    
    def reset(self) -> None:
        """Reset sequence counter. Primarily for testing or new recording sessions."""
        with self._counter_lock:
            self._sequence_counter = 0
            # print("GlobalSequenceGenerator Reset")  # For debugging
            
    def get_current_count(self) -> int:
        """Get current sequence count without incrementing."""
        with self._counter_lock:
            return self._sequence_counter


# Convenience functions for global access - used by CorrelationLogger
def get_next_global_sequence_id() -> int:
    """Convenience function to get the next global sequence ID using the singleton instance."""
    return GlobalSequenceGenerator.get_instance().get_next_id()


def reset_global_sequence_id_generator() -> None:
    """Convenience function to reset the global sequence generator."""
    GlobalSequenceGenerator.get_instance().reset()


def get_current_global_sequence_count() -> int:
    """Convenience function to get current sequence count without incrementing."""
    return GlobalSequenceGenerator.get_instance().get_current_count()
