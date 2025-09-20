"""
Performance Metrics Service

This service manages performance monitoring and metrics collection, including:
- Performance benchmarker lifecycle management
- Key performance metrics tracking
- Periodic metrics clearing and maintenance
- Performance baseline analysis and reporting
- Metrics publishing and integration
- Performance configuration management

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List
from threading import Lock

from interfaces.core.services import ILifecycleService, IPerformanceBenchmarker
# Import our custom interface (will be relative path when integrated)
from interfaces.core.services import IPerformanceMetricsService, IIPCManager

logger = logging.getLogger(__name__)


class PerformanceMetricsService(ILifecycleService, IPerformanceMetricsService):
    """
    Manages performance metrics collection and analysis.
    
    This service encapsulates all performance monitoring logic that was
    previously scattered throughout ApplicationCore.
    """
    
    def __init__(self,
                 config_service: Any,
                 ipc_manager: Optional[IIPCManager] = None,
                 performance_benchmarker: Optional[IPerformanceBenchmarker] = None):
        """
        Initialize the Performance Metrics Service.
        
        Args:
            config_service: Global configuration object
            ipc_manager: IPC manager for publishing metrics data
            performance_benchmarker: Performance benchmarker instance
        """
        self.logger = logger
        self.config = config_service
        self.ipc_manager = ipc_manager
        self.performance_benchmarker = performance_benchmarker
        
        # Performance metrics attributes
        self._is_running = False
        self._lock = Lock()
        
        # Metrics clearing timer
        self._metrics_clear_timer: Optional[threading.Timer] = None
        self._metrics_clear_interval_sec = float(
            getattr(self.config, 'PERF_METRICS_AUTO_CLEAR_INTERVAL_SEC', 3600.0)
        )
        
        # Configuration
        self.target_metrics_stream = getattr(
            self.config, 'redis_stream_performance_metrics', 'testrade:metrics:performance'
        )
        
        # Key performance metrics to track
        self.key_performance_metric_names = [
            "ocr_conditioning_queue_size",
            "ocr_conditioning_processing_time_ms",
            "eventbus.handler_time.CleanedOCRSnapshotEvent._handle_cleaned_ocr_snapshot_lightweight",
            "eventbus.queue_time.CleanedOCRSnapshotEvent",
            "eventbus.dispatch_time.CleanedOCRSnapshotEvent",
            "ocr.conditioning_time_ms",
            "pmd.process_trade_time_ms",
            "pmd.process_quote_time_ms",
            "signal_gen.ocr_cleaned_to_order_request_latency_ms",
            "risk.order_request_handling_time_ms",
            "executor.validated_order_handling_time_ms",
            "trade_manager.manual_add_shares_time_ms",
            "trade_manager.force_close_time_ms"
        ]
        
        # State tracking
        self._start_time = None
        self._metrics_published = 0
        self._clearing_cycles = 0
        
    @property
    def is_ready(self) -> bool:
        """Check if the performance metrics service is ready."""
        with self._lock:
            return (self._is_running and 
                   self.performance_benchmarker is not None)
    
    def start(self) -> None:
        """Start the performance metrics service."""
        with self._lock:
            if self._is_running:
                self.logger.warning("PerformanceMetricsService already running")
                return
                
            if not self.performance_benchmarker:
                self.logger.error("Performance benchmarker not available - cannot start")
                return
                
            self._is_running = True
            self._start_time = time.time()
            
            # Setup periodic metrics clearing
            self._setup_periodic_metrics_clearing()
            
            self.logger.info("PerformanceMetricsService started successfully")
    
    def stop(self) -> None:
        """Stop the performance metrics service."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            
            # Stop metrics clearing timer
            if self._metrics_clear_timer:
                self._metrics_clear_timer.cancel()
                self._metrics_clear_timer = None
                
            # Publish final metrics summary
            self._publish_final_metrics_summary()
            
            self.logger.info("PerformanceMetricsService stopped")
    
    def get_performance_benchmarker(self):
        """Get the PerformanceBenchmarker instance for system-wide performance monitoring."""
        return self.performance_benchmarker
    
    def set_performance_benchmarker(self, benchmarker: IPerformanceBenchmarker):
        """Set the performance benchmarker instance."""
        with self._lock:
            self.performance_benchmarker = benchmarker
            self.logger.info("Performance benchmarker instance set")
    
    def _setup_periodic_metrics_clearing(self):
        """Setup periodic metrics clearing timer."""
        if self._metrics_clear_timer:
            self._metrics_clear_timer.cancel()
        
        if self._metrics_clear_interval_sec > 0 and self.performance_benchmarker:
            self._metrics_clear_timer = threading.Timer(
                self._metrics_clear_interval_sec,
                self._periodic_metrics_clear_callback
            )
            self._metrics_clear_timer.daemon = True
            self._metrics_clear_timer.start()
            self.logger.info(f"Periodic metrics clearing enabled (interval: {self._metrics_clear_interval_sec}s)")
        else:
            self.logger.info("Periodic metrics clearing disabled")
    
    def _periodic_metrics_clear_callback(self):
        """Callback for periodic metrics clearing."""
        try:
            if self.performance_benchmarker and hasattr(self.performance_benchmarker, 'reset_all_trackers'):
                # Publish summary before clearing
                self._publish_metrics_summary("PERIODIC_CLEAR")
                
                # Clear metrics
                self.performance_benchmarker.reset_all_trackers()
                self._clearing_cycles += 1
                
                self.logger.info(f"Periodic metrics clearing completed (cycle {self._clearing_cycles})")
                
        except Exception as e:
            self.logger.error(f"Error during periodic metrics clearing: {e}", exc_info=True)
        finally:
            # Setup next clearing cycle
            if self._is_running:
                self._setup_periodic_metrics_clearing()
    
    def dump_performance_baselines(self) -> None:
        """Dump performance baseline statistics for key metrics to logger and stdout."""
        benchmarker = self.get_performance_benchmarker()
        if not benchmarker:
            self.logger.info("PerformanceBenchmarker not available. Cannot dump baseline stats.")
            return

        self.logger.info("--- Performance Baseline Statistics ---")
        print("\n--- Performance Baseline Statistics ---")

        if not self.key_performance_metric_names:
            self.logger.warning("No key_performance_metric_names defined. Cannot dump specific stats.")
            return

        metrics_found = 0
        for metric_name in self.key_performance_metric_names:
            try:
                stats = benchmarker.get_stats(metric_name)
                
                if stats and stats.get('count', 0) > 0:
                    metrics_found += 1
                    output = f"Metric: {metric_name}\n" \
                             f"  Count: {stats['count']}\n" \
                             f"  Mean: {stats['mean']:.4f} ms\n" \
                             f"  Median (p50): {stats['p50']:.4f} ms\n" \
                             f"  p95: {stats['p95']:.4f} ms\n" \
                             f"  p99: {stats['p99']:.4f} ms\n" \
                             f"  Min: {stats['min']:.4f} ms\n" \
                             f"  Max: {stats['max']:.4f} ms\n" \
                             f"  StdDev: {stats['stddev']:.4f} ms\n"
                    self.logger.info(output)
                    print(output)
                else:
                    self.logger.debug(f"No data for metric: {metric_name}")
                    
            except Exception as e:
                self.logger.error(f"Error getting stats for metric '{metric_name}': {e}")
        
        summary_msg = f"Performance baseline dump complete. {metrics_found} metrics with data found."
        self.logger.info(summary_msg)
        print(summary_msg)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get performance metrics summary."""
        if not self.performance_benchmarker:
            return {"error": "Performance benchmarker not available"}
        
        try:
            summary = self.performance_benchmarker.get_metrics_summary()
            
            # Add service-specific information
            enhanced_summary = {
                "timestamp": time.time(),
                "service_uptime_seconds": time.time() - self._start_time if self._start_time else 0,
                "metrics_published": self._metrics_published,
                "clearing_cycles": self._clearing_cycles,
                "key_metrics_count": len(self.key_performance_metric_names),
                "benchmarker_summary": summary or {}
            }
            
            # Add key metrics details if available
            if summary and summary.get("metrics"):
                key_metrics = {}
                for metric_name in self.key_performance_metric_names:
                    if metric_name in summary["metrics"]:
                        key_metrics[metric_name] = summary["metrics"][metric_name]
                enhanced_summary["key_metrics"] = key_metrics
            
            return enhanced_summary
            
        except Exception as e:
            self.logger.error(f"Error getting metrics summary: {e}", exc_info=True)
            return {"error": str(e)}
    
    def clear_metrics(self) -> None:
        """Clear all performance metrics."""
        if not self.performance_benchmarker:
            self.logger.warning("Performance benchmarker not available - cannot clear metrics")
            return
        
        try:
            # Publish summary before clearing
            self._publish_metrics_summary("MANUAL_CLEAR")
            
            # Clear metrics
            if hasattr(self.performance_benchmarker, 'reset_all_trackers'):
                self.performance_benchmarker.reset_all_trackers()
                self.logger.info("Performance metrics cleared manually")
            else:
                self.logger.warning("Performance benchmarker does not support clearing metrics")
                
        except Exception as e:
            self.logger.error(f"Error clearing performance metrics: {e}", exc_info=True)
    
    def _publish_metrics_summary(self, event_type: str):
        """Publish metrics summary to Redis."""
        try:
            if not self.ipc_manager:
                self.logger.debug("IPC manager not available - skipping metrics publishing")
                return
            
            summary = self.get_metrics_summary()
            if "error" in summary:
                self.logger.warning(f"Cannot publish metrics summary due to error: {summary['error']}")
                return
            
            # Create metrics payload
            metrics_payload = {
                "event_type": event_type,
                "timestamp": time.time(),
                "metrics_summary": summary,
                "service_info": {
                    "service_name": "PerformanceMetricsService",
                    "clearing_interval_sec": self._metrics_clear_interval_sec,
                    "key_metrics_tracked": len(self.key_performance_metric_names)
                }
            }
            
            # Publish to Redis
            success = self.ipc_manager.publish_to_redis(
                stream_name=self.target_metrics_stream,
                payload=metrics_payload,
                event_type="TESTRADE_PERFORMANCE_METRICS_SUMMARY",
                correlation_id=f"metrics_{int(time.time())}_{event_type.lower()}",
                source_component="PerformanceMetricsService"
            )
            
            if success:
                self._metrics_published += 1
                self.logger.debug(f"Metrics summary published: {event_type}")
            else:
                self.logger.warning(f"Failed to publish metrics summary: {event_type}")
                
        except Exception as e:
            self.logger.error(f"Error publishing metrics summary: {e}", exc_info=True)
    
    def _publish_final_metrics_summary(self):
        """Publish final metrics summary during shutdown."""
        try:
            if self.performance_benchmarker:
                self.logger.info("Publishing final performance metrics summary...")
                self._publish_metrics_summary("SHUTDOWN")
                
                # Log summary to console as well
                summary = self.performance_benchmarker.get_metrics_summary()
                if summary and summary.get("metrics"):
                    self.logger.info("--- Final Performance Metrics Summary ---")
                    for metric_name, stats in summary["metrics"].items():
                        if stats.get("count", 0) > 0:
                            self.logger.info(
                                f"  {metric_name}: Mean={stats.get('mean', 0):.2f}ms, "
                                f"P95={stats.get('p95', 0):.2f}ms, Count={stats.get('count', 0)}"
                            )
                            
        except Exception as e:
            self.logger.error(f"Error publishing final metrics summary: {e}", exc_info=True)
    
    def get_service_status(self) -> Dict[str, Any]:
        """Get performance metrics service status."""
        with self._lock:
            uptime = time.time() - self._start_time if self._start_time else 0
            
            return {
                "is_running": self._is_running,
                "is_ready": self.is_ready,
                "uptime_seconds": uptime,
                "performance_benchmarker_available": self.performance_benchmarker is not None,
                "metrics_published": self._metrics_published,
                "clearing_cycles": self._clearing_cycles,
                "clearing_interval_sec": self._metrics_clear_interval_sec,
                "key_metrics_count": len(self.key_performance_metric_names),
                "configuration": {
                    "metrics_stream": self.target_metrics_stream,
                    "auto_clear_enabled": self._metrics_clear_interval_sec > 0
                }
            }
    
    def add_key_metric(self, metric_name: str):
        """Add a metric to the key performance metrics list."""
        if metric_name not in self.key_performance_metric_names:
            self.key_performance_metric_names.append(metric_name)
            self.logger.info(f"Added key metric: {metric_name}")
    
    def remove_key_metric(self, metric_name: str):
        """Remove a metric from the key performance metrics list."""
        if metric_name in self.key_performance_metric_names:
            self.key_performance_metric_names.remove(metric_name)
            self.logger.info(f"Removed key metric: {metric_name}")
    
    def get_key_metrics_list(self) -> List[str]:
        """Get the list of key performance metrics being tracked."""
        return self.key_performance_metric_names.copy()