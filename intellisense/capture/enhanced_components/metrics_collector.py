"""
IntelliSense Component Capture Metrics Collector

This module provides utilities for collecting and managing capture-related metrics
for enhanced components. It tracks processing performance, notification success rates,
and other operational metrics for components integrated with the capture system.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ComponentCaptureMetrics:
    """
    A utility class to hold and update common capture-related metrics
    for an enhanced component.
    """
    
    def __init__(self):
        self.items_processed: int = 0
        self.notifications_sent: int = 0
        self.notification_errors: int = 0
        self.total_processing_latency_ns: int = 0
        self.total_notification_latency_ns: int = 0
        self.processing_error_count: int = 0

    def record_item_processed(self, processing_latency_ns: int, success: bool = True):
        """Record that an item was processed with the given latency."""
        self.items_processed += 1
        self.total_processing_latency_ns += processing_latency_ns
        if not success:
            self.processing_error_count += 1

    def record_notification_attempt(self, notification_latency_ns: int, success: bool = True):
        """Record a notification attempt with the given latency."""
        if success:
            self.notifications_sent += 1
            self.total_notification_latency_ns += notification_latency_ns
        else:
            self.notification_errors += 1

    def get_metrics(self) -> Dict[str, Any]:
        """Get comprehensive metrics dictionary."""
        avg_processing_latency_ns = (
            self.total_processing_latency_ns / self.items_processed
        ) if self.items_processed > 0 else 0
        
        avg_notification_latency_ns = (
            self.total_notification_latency_ns / self.notifications_sent
        ) if self.notifications_sent > 0 else 0
        
        return {
            "items_processed": self.items_processed,
            "processing_errors": self.processing_error_count,
            "avg_processing_latency_ms": avg_processing_latency_ns / 1_000_000,
            "notifications_sent": self.notifications_sent,
            "notification_errors": self.notification_errors,
            "avg_notification_latency_ms": avg_notification_latency_ns / 1_000_000,
            "success_rate": (
                (self.items_processed - self.processing_error_count) / self.items_processed
            ) if self.items_processed > 0 else 1.0,
            "notification_success_rate": (
                self.notifications_sent / (self.notifications_sent + self.notification_errors)
            ) if (self.notifications_sent + self.notification_errors) > 0 else 1.0
        }

    def reset(self):
        """Reset all metrics to zero."""
        self.items_processed = 0
        self.notifications_sent = 0
        self.notification_errors = 0
        self.total_processing_latency_ns = 0
        self.total_notification_latency_ns = 0
        self.processing_error_count = 0
