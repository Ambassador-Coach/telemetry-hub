# File: modules/system_monitoring/system_health_service.py
import time  # Using time instead of समय placeholder
import psutil
import logging
import threading
import os
import uuid
from typing import Dict, Any, Optional, List, Callable
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

# Import interfaces (ensure these paths are correct for your project)
from interfaces.core.services import IBulletproofBabysitterIPCClient, IConfigService, IPerformanceBenchmarker, IEventBus as DI_IEventBus
from interfaces.logging.services import ICorrelationLogger

# FUZZY'S FIX: Import SignalDecisionEvent for telemetry handling
from core.events import SignalDecisionEvent, SignalDecisionEventData

try:
    from modules.monitoring.pipeline_validator import PipelineValidator
except ImportError as e:
    logging.warning(f"Import warning in SystemHealthMonitoringService: {e}")
    PipelineValidator = Any

logger = logging.getLogger(__name__)

class SystemHealthMonitoringService:
    def __init__(self,
                 config_service: IConfigService,
                 ocr_process_manager: Optional[Any] = None, # IOCRProcessManager for OCR process access
                 event_bus: Optional[DI_IEventBus] = None, # Event bus for EventBus health metrics
                 performance_metrics: Optional[List[str]] = None, # Performance metric names list
                 correlation_logger: Optional[ICorrelationLogger] = None,
                 perf_benchmarker: Optional[IPerformanceBenchmarker] = None,
                 pipeline_validator_ref: Optional[Any] = None, # Pass PipelineValidator instance
                 ipc_client: IBulletproofBabysitterIPCClient = None
                ):
        self.config = config_service
        self._ocr_process_manager = ocr_process_manager
        self._event_bus = event_bus
        self._performance_metrics = performance_metrics or []
        self._correlation_logger = correlation_logger
        self.perf_benchmarker = perf_benchmarker
        self.pipeline_validator = pipeline_validator_ref # Store the reference
        self.ipc_client = ipc_client

        self.monitoring_interval_sec = float(getattr(self.config, 'system_health_monitoring_interval_sec', 2.0)) # SME: 1-2s for enhanced performance
        self.baseline_publish_interval_sec = float(getattr(self.config, 'system_health_baseline_interval', 60.0))
        self.last_baseline_publish_ts = 0.0

        # For Parallel Metric Gathering (Performance Enhancement)
        self._num_workers = int(getattr(self.config, 'system_health_metric_gather_workers', 4))
        self._executor = None  # ThreadPoolExecutor creation deferred to start() method
        self._metric_collection_timeout_sec = float(getattr(self.config, 'system_health_metric_gather_timeout_sec', 0.1)) # 100ms timeout per metric group

        self.thresholds = { # Load these from self.config
            "cpu_usage_percent": getattr(self.config, 'system_health_cpu_threshold', 80.0),
            "app_core_cpu_usage_percent": getattr(self.config, 'system_health_app_core_cpu_threshold', 70.0), # Specific process
            "ocr_process_cpu_usage_percent": getattr(self.config, 'system_health_ocr_process_cpu_threshold', 70.0), # Specific process
            "memory_usage_percent": getattr(self.config, 'system_health_memory_threshold', 85.0),
            "event_bus_max_queue_size": getattr(self.config, 'system_health_event_bus_max_queue_size', 5000),
            "risk_max_latency_p95_ms": getattr(self.config, 'system_health_risk_max_latency_p95_ms', 200),
            "market_data_mmap_max_fill_pct": getattr(self.config, 'system_health_market_data_mmap_max_fill_pct', 90.0),
            "market_data_bulk_channel_clogged_duration_sec": getattr(self.config, 'system_health_market_data_bulk_channel_clogged_duration_sec', 60),
        }
        self.alert_cooldown_period_sec = float(getattr(self.config, 'system_health_alert_cooldown_period_sec', 300.0))
        self.alert_cooldowns = defaultdict(float)
        self._clogged_channel_start_time: Dict[str, float] = {}

        # For non-blocking psutil.cpu_percent(interval=None)
        self._last_cpu_times = None # For overall system CPU
        self._last_app_core_proc_cpu_times = None # For AppCore process
        self._app_core_process_handle: Optional[psutil.Process] = None
        self._last_ocr_proc_cpu_times = None # For OCR process
        self._ocr_process_handle: Optional[psutil.Process] = None

        # Metrics history for trend analysis
        self.metrics_history = {
            'cpu_samples': deque(maxlen=100),
            'memory_samples': deque(maxlen=100),
            'eventbus_queue_samples': deque(maxlen=100),
            'risk_latency_samples': deque(maxlen=100),
        }

        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        logger.info(f"SystemHealthMonitoringService initialized. ThreadPoolExecutor creation deferred to start() (workers={self._num_workers}, timeout={self._metric_collection_timeout_sec}s).")

    def start(self):
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.warning("SHMS monitor thread already running.")
            return
        
        # Create ThreadPoolExecutor for parallel metric gathering
        if not self._executor:
            self._executor = ThreadPoolExecutor(max_workers=self._num_workers, thread_name_prefix="SHMSMetricWorker")
            logger.info(f"SystemHealthMonitoringService: Created ThreadPoolExecutor with {self._num_workers} workers")
        
        # Get ApplicationCore process handle
        try:
            self._app_core_process_handle = psutil.Process(os.getpid())
            self._last_app_core_proc_cpu_times = self._app_core_process_handle.cpu_times()
        except Exception as e:
            logger.error(f"SHMS: Failed to get ApplicationCore psutil.Process handle: {e}")
            self._app_core_process_handle = None

        # Get OCR process handle (if PID is available from OCRProcessManager)
        if self._ocr_process_manager and hasattr(self._ocr_process_manager, 'get_ocr_process_pid'):
            try:
                ocr_pid = self._ocr_process_manager.get_ocr_process_pid()
                if ocr_pid:
                    self._ocr_process_handle = psutil.Process(ocr_pid)
                    self._last_ocr_proc_cpu_times = self._ocr_process_handle.cpu_times()
                else:
                    logger.info("SHMS: OCR process PID not available from OCRProcessManager")
                    self._ocr_process_handle = None
            except psutil.NoSuchProcess:
                logger.warning(f"SHMS: OCR process with PID {ocr_pid} not found at SHMS start.")
                self._ocr_process_handle = None
            except Exception as e:
                logger.error(f"SHMS: Failed to get OCR psutil.Process handle: {e}")
                self._ocr_process_handle = None
        else:
             logger.info("SHMS: OCRProcessManager not available or does not provide OCR process PID")

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(target=self._monitoring_loop, name="SHMSMonitorThread", daemon=True)
        # CRITICAL: Set thread to low priority if possible (platform-dependent, often not simple in Python stdlib)
        # For now, rely on frequent yields (time.sleep) in the loop.
        self._monitor_thread.start()
        logger.info(f"SystemHealthMonitoringService monitor thread started with interval {self.monitoring_interval_sec}s.")

    def stop(self):
        logger.info("SystemHealthMonitoringService stop requested.")
        self._stop_event.set() # Signal monitoring loop to stop

        # Shutdown ThreadPoolExecutor first to stop any running metric collection
        if hasattr(self, '_executor') and self._executor:
            logger.info("SHMS: Shutting down ThreadPoolExecutor...")
            try:
                # Try Python 3.9+ cancel_futures parameter for immediate cancellation
                self._executor.shutdown(wait=True, cancel_futures=True)
            except TypeError:
                # Fallback for older Python versions (< 3.9)
                logger.debug("SHMS: Using fallback ThreadPoolExecutor shutdown for older Python")
                self._executor.shutdown(wait=True)
            logger.info("SHMS: ThreadPoolExecutor shut down.")

        # Stop the monitoring thread
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self.monitoring_interval_sec + 1.0)
            if self._monitor_thread.is_alive():
                logger.warning("SHMS: Monitor thread did not stop gracefully.")
        self._monitor_thread = None

        logger.info("SystemHealthMonitoringService stopped.")

    def _monitoring_loop(self):
        logger.info(f"SHMS Monitoring Loop Started. Main Interval: {self.monitoring_interval_sec}s")
        self._last_cpu_times = psutil.cpu_times_percent() # Initialize for first diff calc

        while not self._stop_event.is_set():
            loop_start_time = time.time()
            current_gathered_metrics = {} # To store results from futures

            try:
                # --- Parallel Metric Gathering ---
                metric_futures: Dict[str, Any] = { # Using Any for futures
                    "system_resources": self._executor.submit(self._get_system_resources),
                    "network_health": self._executor.submit(self._get_network_health),
                    "pipeline_health": self._executor.submit(self._get_pipeline_health),
                    "application_health": self._executor.submit(self._get_application_health),
                    "ocr_subsystem": self._executor.submit(self._get_ocr_subsystem_health),
                    "eventbus_health": self._executor.submit(self._get_eventbus_health),
                    "risk_service": self._executor.submit(self._get_risk_service_health),
                    "market_data_pipeline": self._executor.submit(self._get_market_data_pipeline_health),
                    "performance_summary": self._executor.submit(self._get_performance_summary)
                }

                for key, future in metric_futures.items():
                    try:
                        current_gathered_metrics[key] = future.result(timeout=self._metric_collection_timeout_sec)
                    except FutureTimeoutError:
                        logger.warning(f"SHMS: Timeout collecting metric group '{key}' (>{self._metric_collection_timeout_sec}s).")
                        current_gathered_metrics[key] = {"error": "timeout", "metric_group": key}
                    except Exception as e_future:
                        logger.error(f"SHMS: Error collecting metric group '{key}': {e_future}", exc_info=True)
                        current_gathered_metrics[key] = {"error": str(e_future), "metric_group": key}

                current_gathered_metrics["overall_collection_timestamp"] = time.time()

                # --- End Parallel Metric Gathering ---

                # Update metrics history and calculate trends
                try:
                    self._update_metrics_history(current_gathered_metrics)
                    current_gathered_metrics['trend_analysis'] = self._calculate_trend_analysis()
                except Exception as e:
                    logger.error(f"SHMS: Error updating metrics history or calculating trends: {e}")
                    current_gathered_metrics['trend_analysis'] = {'error': str(e)}

                # Check thresholds and publish alerts using current_gathered_metrics
                alerts_to_publish = self._check_comprehensive_thresholds(current_gathered_metrics, loop_start_time)
                for alert_data in alerts_to_publish:
                    # Pass only necessary context from current_gathered_metrics to avoid huge alert payloads
                    alert_context_snapshot = {
                        "system_resources": current_gathered_metrics.get("system_resources"),
                        "ocr_subsystem": current_gathered_metrics.get("ocr_subsystem"),
                        "eventbus_health": current_gathered_metrics.get("eventbus_health"),
                        "risk_service": current_gathered_metrics.get("risk_service"),
                        "market_data_pipeline": current_gathered_metrics.get("market_data_pipeline"),
                    }
                    self._publish_system_alert(alert_context_snapshot, alert_data)

                # Publish baseline periodically using current_gathered_metrics
                if loop_start_time - self.last_baseline_publish_ts >= self.baseline_publish_interval_sec:
                    self._publish_system_baseline(current_gathered_metrics)
                    self.last_baseline_publish_ts = loop_start_time

            except Exception as e_loop: # Catch errors in the main loop logic itself
                logger.error(f"SHMS: Unhandled exception in monitoring loop: {e_loop}", exc_info=True)

            loop_duration = time.time() - loop_start_time
            sleep_time = max(0.05, self.monitoring_interval_sec - loop_duration) # Min sleep 50ms (enhanced performance)
            self._stop_event.wait(sleep_time)

        logger.info("SHMS Monitoring Loop Stopped.")

    def _get_non_blocking_cpu_percent(self, process_handle: Optional[psutil.Process], last_proc_cpu_times_attr_name: str) -> float:
        """Calculates CPU percent for a specific process non-blockingly."""
        if not process_handle: 
            return -1.0
        try:
            last_times = getattr(self, last_proc_cpu_times_attr_name)
            current_times = process_handle.cpu_times()
            if last_times is None: # First call
                setattr(self, last_proc_cpu_times_attr_name, current_times)
                return 0.0 # No interval yet for first call

            # Update for next call and use psutil's non-blocking cpu_percent
            setattr(self, last_proc_cpu_times_attr_name, current_times)
            return process_handle.cpu_percent(interval=None) # This is non-blocking and correct.

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return -1.0 # Process died or no perms
        except Exception as e:
            logger.debug(f"SHMS: Error getting non-blocking CPU for {process_handle.pid if process_handle else 'unknown'}: {e}")
            return -2.0 # Indicates error

    def _get_system_resources(self) -> Dict[str, Any]:
        """Get system resource metrics using non-blocking CPU monitoring."""
        try:
            # Overall system CPU (non-blocking since last call)
            system_cpu = psutil.cpu_percent(interval=None)

            app_core_cpu = self._get_non_blocking_cpu_percent(self._app_core_process_handle, "_last_app_core_proc_cpu_times")
            ocr_proc_cpu = self._get_non_blocking_cpu_percent(self._ocr_process_handle, "_last_ocr_proc_cpu_times")

            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            return {
                "overall_cpu_usage_percent": system_cpu,
                "app_core_cpu_usage_percent": app_core_cpu,
                "ocr_process_cpu_usage_percent": ocr_proc_cpu,
                "cpu": {
                    "usage_percent": system_cpu,
                    "app_core_cpu_percent": app_core_cpu,
                    "ocr_process_cpu_percent": ocr_proc_cpu,
                },
                "memory": {
                    "usage_percent": memory.percent,
                    "usage_mb": memory.used / (1024*1024),
                    "available_mb": memory.available / (1024*1024),
                },
                "disk": {
                    "usage_percent": disk.percent,
                    "free_gb": disk.free / (1024*1024*1024),
                }
            }
        except Exception as e:
            logger.error(f"SHMS: Error gathering system resources: {e}")
            return {}

    # NOTE: _gather_enhanced_metrics is effectively replaced by the parallel submission logic
    # in _monitoring_loop. The individual _get_..._health() methods remain the same as previously designed,
    # but they are now executed in separate threads by the ThreadPoolExecutor directly in the monitoring loop.
    # CRITICAL: Each _get_..._health() method is self-contained and thread-safe.

    def _get_network_health(self) -> Dict[str, Any]:
        """Get network health metrics using network_diagnostics."""
        try:
            # Use network_diagnostics if available
            try:
                from utils.network_diagnostics import NetworkDiagnostics
                net_diag = NetworkDiagnostics()
                connectivity_result = net_diag.check_connectivity()
                latency_result = net_diag.check_latency()

                return {
                    'connectivity_status': connectivity_result.get('status', 'unknown'),
                    'latency_ms': latency_result.get('latency_ms', -1),
                    'packet_loss_percent': latency_result.get('packet_loss_percent', -1),
                }
            except Exception as e:
                logger.debug(f"NetworkDiagnostics not available: {e}")

            # Fallback to basic psutil network stats
            net_io = psutil.net_io_counters()
            return {
                'bytes_sent': net_io.bytes_sent,
                'bytes_recv': net_io.bytes_recv,
                'packets_sent': net_io.packets_sent,
                'packets_recv': net_io.packets_recv,
                'connectivity_status': 'unknown',
                'latency_ms': -1,
            }
        except Exception as e:
            logger.error(f"Error getting network health: {e}")
            return {}

    def _get_application_health(self) -> Dict[str, Any]:
        """Get application-level health metrics."""
        try:
            # Basic application health indicators
            thread_count = threading.active_count()

            # IPC client health
            ipc_healthy = hasattr(self.ipc_client, 'is_healthy') and self.ipc_client.is_healthy()
            ipc_offline = getattr(self.ipc_client, 'offline_mode', False)

            # Performance benchmarker status
            perf_benchmarker_available = self.perf_benchmarker is not None

            return {
                'thread_count': thread_count,
                'ipc_client_healthy': ipc_healthy,
                'ipc_offline_mode': ipc_offline,
                'performance_benchmarker_available': perf_benchmarker_available,
                'config_service_available': self.config is not None,
                'process': {
                    'pid': os.getpid(),
                    'uptime_seconds': time.time() - psutil.Process().create_time(),
                }
            }
        except Exception as e:
            logger.error(f"Error getting application health: {e}")
            return {}

    def _get_ocr_subsystem_health(self) -> Dict[str, Any]:
        """Get OCR subsystem health metrics."""
        try:
            ocr_health = {
                'process_status': 'unknown',
                'cpu_usage_percent': -1,
                'conditioning_queue_size': 0,
            }

            # OCR process CPU usage
            if self._ocr_process_handle:
                try:
                    ocr_cpu = self._get_non_blocking_cpu_percent(self._ocr_process_handle, "_last_ocr_proc_cpu_times")
                    ocr_health['cpu_usage_percent'] = ocr_cpu
                    ocr_health['process_status'] = 'running' if ocr_cpu >= 0 else 'error'
                except Exception as e:
                    logger.debug(f"Error getting OCR CPU: {e}")
                    ocr_health['process_status'] = 'error'
            else:
                ocr_health['process_status'] = 'not_available'

            # OCR performance metrics from PerformanceBenchmarker
            if self.perf_benchmarker:
                try:
                    # Get OCR-related metrics
                    ocr_metrics = self.perf_benchmarker.get_metrics_summary(['ocr_grab_fps', 'ocr_opencv_time_ms', 'ocr_tesseract_time_ms'])
                    ocr_health.update(ocr_metrics)
                except Exception as e:
                    logger.debug(f"Error getting OCR performance metrics: {e}")

            return ocr_health
        except Exception as e:
            logger.error(f"Error getting OCR subsystem health: {e}")
            return {}

    def _get_eventbus_health(self) -> Dict[str, Any]:
        """Get EventBus health metrics."""
        try:
            eventbus_health = {
                'status': 'unknown',
                'queue_size': 0,
                'avg_processing_time_ms': -1,
            }

            # Get EventBus health metrics from injected event bus
            if self._event_bus:
                try:
                    if hasattr(self._event_bus, 'get_queue_size'):
                        queue_size = self._event_bus.get_queue_size()
                        eventbus_health['queue_size'] = queue_size
                        eventbus_health['status'] = 'healthy' if queue_size < self.thresholds['event_bus_max_queue_size'] else 'overloaded'
                    else:
                        eventbus_health['status'] = 'available_no_metrics'
                except Exception as e:
                    logger.debug(f"Error accessing EventBus: {e}")
                    eventbus_health['status'] = 'error'
            else:
                eventbus_health['status'] = 'not_injected'

            # EventBus performance metrics from PerformanceBenchmarker
            if self.perf_benchmarker:
                try:
                    eb_metrics = self.perf_benchmarker.get_metrics_summary(['event_bus.publish_latency_ms', 'event_bus.queue_size_current'])
                    eventbus_health.update(eb_metrics)
                except Exception as e:
                    logger.debug(f"Error getting EventBus performance metrics: {e}")

            return eventbus_health
        except Exception as e:
            logger.error(f"Error getting EventBus health: {e}")
            return {}

    def _get_risk_service_health(self) -> Dict[str, Any]:
        """Get Risk Service health metrics from PerformanceBenchmarker."""
        try:
            risk_health = {
                'status': 'unknown',
                'latency_p95_ms': -1,
                'request_count': 0,
                'queue_size': 0,
            }

            if self.perf_benchmarker:
                try:
                    # Get risk service metrics
                    risk_metrics = self.perf_benchmarker.get_metrics_summary([
                        'risk.order_request_handling_time_ms',
                        'risk.order_request_received_count',
                        'risk.order_requests_processed_count',
                        'risk.pmd_queue_size'
                    ])

                    # Calculate P95 latency if we have latency data
                    if 'risk.order_request_handling_time_ms' in risk_metrics:
                        latency_data = risk_metrics['risk.order_request_handling_time_ms']
                        if isinstance(latency_data, list) and latency_data:
                            latency_data.sort()
                            p95_index = int(len(latency_data) * 0.95)
                            risk_health['latency_p95_ms'] = latency_data[p95_index] if p95_index < len(latency_data) else latency_data[-1]

                    risk_health.update(risk_metrics)
                    risk_health['status'] = 'healthy' if risk_health['latency_p95_ms'] < self.thresholds['risk_max_latency_p95_ms'] else 'slow'

                except Exception as e:
                    logger.debug(f"Error getting risk service metrics: {e}")
                    risk_health['status'] = 'error'

            return risk_health
        except Exception as e:
            logger.error(f"Error getting risk service health: {e}")
            return {}

    def _get_market_data_pipeline_health(self) -> Dict[str, Any]:
        """Get Market Data Pipeline health metrics."""
        try:
            md_health = {
                'status': 'unknown',
                'mmap_buffer_stats': {},
                'channel_health': {},
            }

            # Get IPC buffer stats for market data channels
            if hasattr(self.ipc_client, 'get_ipc_buffer_stats'):
                try:
                    bulk_stats = self.ipc_client.get_ipc_buffer_stats('bulk')
                    trading_stats = self.ipc_client.get_ipc_buffer_stats('trading')

                    md_health['mmap_buffer_stats'] = {
                        'bulk_channel': bulk_stats,
                        'trading_channel': trading_stats,
                    }

                    # Check for clogged channels
                    current_time = time.time()
                    for channel_name, stats in md_health['mmap_buffer_stats'].items():
                        if isinstance(stats, dict):
                            fill_pct = stats.get('fill_percentage', 0)
                            if fill_pct > self.thresholds['market_data_mmap_max_fill_pct']:
                                if channel_name not in self._clogged_channel_start_time:
                                    self._clogged_channel_start_time[channel_name] = current_time

                                clogged_duration = current_time - self._clogged_channel_start_time[channel_name]
                                md_health['channel_health'][channel_name] = {
                                    'status': 'clogged',
                                    'clogged_duration': clogged_duration,
                                    'fill_percentage': fill_pct,
                                }
                            else:
                                # Channel recovered
                                if channel_name in self._clogged_channel_start_time:
                                    del self._clogged_channel_start_time[channel_name]
                                md_health['channel_health'][channel_name] = {
                                    'status': 'healthy',
                                    'fill_percentage': fill_pct,
                                }

                except Exception as e:
                    logger.debug(f"Error getting IPC buffer stats: {e}")

            # Market data performance metrics from PerformanceBenchmarker
            if self.perf_benchmarker:
                try:
                    md_metrics = self.perf_benchmarker.get_metrics_summary([
                        'market_data_processing_time',
                        'market_data_rate',
                        'market_data_parse_time',
                        'broker_response_time'
                    ])
                    md_health.update(md_metrics)
                except Exception as e:
                    logger.debug(f"Error getting market data performance metrics: {e}")

            # Overall status
            clogged_channels = [ch for ch, health in md_health['channel_health'].items() if health.get('status') == 'clogged']
            md_health['status'] = 'clogged' if clogged_channels else 'healthy'

            return md_health
        except Exception as e:
            logger.error(f"Error getting market data pipeline health: {e}")
            return {}

    def _get_pipeline_health(self) -> Dict[str, Any]:
        """Get pipeline health using PipelineValidator."""
        try:
            if self.pipeline_validator and hasattr(self.pipeline_validator, 'analyze_pipeline_run'):
                try:
                    pipeline_analysis = self.pipeline_validator.analyze_pipeline_run()
                    return {
                        'status': 'healthy' if pipeline_analysis.get('overall_health', False) else 'unhealthy',
                        'analysis': pipeline_analysis,
                    }
                except Exception as e:
                    logger.debug(f"Error running pipeline analysis: {e}")
                    return {'status': 'error', 'error': str(e)}
            else:
                return {'status': 'not_available'}
        except Exception as e:
            logger.error(f"Error getting pipeline health: {e}")
            return {}

    def _get_performance_summary(self) -> Dict[str, Any]:
        """Get overall performance summary from PerformanceBenchmarker."""
        if not self.perf_benchmarker:
            return {"status": "PERF_BENCHMARKER_NA", "summary": {}}

        summary_output = {"status": "OK", "summary": {}}
        try:
            # Define key metrics SHMS is interested in for the summary payload
            # These should align with ApplicationCore.key_performance_metric_names or a subset
            key_metrics_for_shms_summary = [
                "ocr_process.tesseract_time_ms", # Example key metrics
                "event_bus.publish_latency_ms",
                "risk.order_request_handling_time_ms",
                # Add other consistently available and important metrics
            ]
            # Use injected performance metrics list:
            if self._performance_metrics:
                 key_metrics_for_shms_summary = self._performance_metrics

            if hasattr(self.perf_benchmarker, 'get_metrics_summary'):
                summary_output["summary"] = self.perf_benchmarker.get_metrics_summary(key_metric_names=key_metrics_for_shms_summary)
                # The status from get_metrics_summary itself can be used or overridden
                summary_output["status"] = summary_output["summary"].pop("status", "OK_FROM_PB")

            else: # Fallback if method somehow still missing, though it shouldn't be
                summary_output["status"] = "PB_GET_METRICS_SUMMARY_MISSING"
                self.logger.warning("SHMS: PerformanceBenchmarker instance does not have get_metrics_summary method.")

        except Exception as e:
            self.logger.debug(f"SHMS: Error getting performance summary from PerformanceBenchmarker: {e}")
            summary_output["status"] = "ERROR_ACCESSING_METRICS"
            summary_output["error_details"] = str(e)
        return summary_output

    def _calculate_health_score(self, metrics: Dict[str, Any]) -> float:
        """Calculate overall health score from 0.0 to 1.0."""
        try:
            scores = []

            # EventBus health (lower latency = better)
            eb_latency = metrics.get('event_bus.publish_latency_ms', [])
            if eb_latency and isinstance(eb_latency, list):
                avg_latency = sum(eb_latency) / len(eb_latency)
                eb_score = max(0.0, 1.0 - (avg_latency / 100.0))  # 100ms = 0 score
                scores.append(eb_score)

            # Risk service health (lower latency = better)
            risk_latency = metrics.get('risk.order_request_handling_time_ms', [])
            if risk_latency and isinstance(risk_latency, list):
                avg_latency = sum(risk_latency) / len(risk_latency)
                risk_score = max(0.0, 1.0 - (avg_latency / 200.0))  # 200ms = 0 score
                scores.append(risk_score)

            # OCR health (higher FPS = better)
            ocr_fps = metrics.get('ocr_grab_fps', [])
            if ocr_fps and isinstance(ocr_fps, list):
                avg_fps = sum(ocr_fps) / len(ocr_fps)
                ocr_score = min(1.0, avg_fps / 30.0)  # 30 FPS = 1.0 score
                scores.append(ocr_score)

            return sum(scores) / len(scores) if scores else 0.5
        except Exception as e:
            logger.debug(f"Error calculating health score: {e}")
            return 0.5

    def _check_comprehensive_thresholds(self, metrics: Dict[str, Any], current_time: float) -> List[Dict[str, Any]]:
        """
        Check all metrics against configured thresholds and generate alerts.
        CRITICAL: All threshold checks must be in the main try block to ensure they run.
        """
        alerts = []

        try:
            # Check system resource thresholds
            system_resources = metrics.get('system_resources', {})

            # CPU threshold
            cpu_usage = system_resources.get('cpu', {}).get('usage_percent', 0)
            if cpu_usage > self.thresholds['cpu_usage_percent']:
                alert_key = 'cpu_usage_high'
                if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                    alerts.append({
                        'alert_type': 'SYSTEM_RESOURCE_THRESHOLD_EXCEEDED',
                        'severity': 'WARNING',
                        'metric': 'cpu_usage_percent',
                        'current_value': cpu_usage,
                        'threshold': self.thresholds['cpu_usage_percent'],
                        'message': f"CPU usage ({cpu_usage:.1f}%) exceeds threshold ({self.thresholds['cpu_usage_percent']:.1f}%)"
                    })
                    self.alert_cooldowns[alert_key] = current_time

            # Memory threshold
            memory_usage = system_resources.get('memory', {}).get('usage_percent', 0)
            if memory_usage > self.thresholds['memory_usage_percent']:
                alert_key = 'memory_usage_high'
                if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                    alerts.append({
                        'alert_type': 'SYSTEM_RESOURCE_THRESHOLD_EXCEEDED',
                        'severity': 'CRITICAL',
                        'metric': 'memory_usage_percent',
                        'current_value': memory_usage,
                        'threshold': self.thresholds['memory_usage_percent'],
                        'message': f"Memory usage ({memory_usage:.1f}%) exceeds threshold ({self.thresholds['memory_usage_percent']:.1f}%)"
                    })
                    self.alert_cooldowns[alert_key] = current_time

            # OCR process CPU threshold
            ocr_cpu_usage = system_resources.get('cpu', {}).get('ocr_process_cpu_percent', 0)
            if ocr_cpu_usage > self.thresholds['ocr_process_cpu_usage_percent']:
                alert_key = 'ocr_cpu_high'
                if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                    alerts.append({
                        'alert_type': 'OCR_SUBSYSTEM_THRESHOLD_EXCEEDED',
                        'severity': 'WARNING',
                        'metric': 'ocr_process_cpu_percent',
                        'current_value': ocr_cpu_usage,
                        'threshold': self.thresholds['ocr_process_cpu_usage_percent'],
                        'message': f"OCR process CPU usage ({ocr_cpu_usage:.1f}%) exceeds threshold ({self.thresholds['ocr_process_cpu_usage_percent']:.1f}%)"
                    })
                    self.alert_cooldowns[alert_key] = current_time

            # EventBus thresholds
            eventbus_health = metrics.get('eventbus_health', {})
            eventbus_queue_size = eventbus_health.get('queue_size', 0)
            if eventbus_queue_size > self.thresholds['event_bus_max_queue_size']:
                alert_key = 'eventbus_queue_high'
                if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                    alerts.append({
                        'alert_type': 'EVENTBUS_THRESHOLD_EXCEEDED',
                        'severity': 'CRITICAL',
                        'metric': 'eventbus_queue_size',
                        'current_value': eventbus_queue_size,
                        'threshold': self.thresholds['event_bus_max_queue_size'],
                        'message': f"EventBus queue size ({eventbus_queue_size}) exceeds threshold ({self.thresholds['event_bus_max_queue_size']})"
                    })
                    self.alert_cooldowns[alert_key] = current_time

            # Risk Service thresholds
            risk_health = metrics.get('risk_service', {})
            risk_latency_p95 = risk_health.get('latency_p95_ms', 0)
            if risk_latency_p95 > self.thresholds['risk_max_latency_p95_ms']:
                alert_key = 'risk_latency_high'
                if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                    alerts.append({
                        'alert_type': 'RISK_SERVICE_THRESHOLD_EXCEEDED',
                        'severity': 'WARNING',
                        'metric': 'risk_latency_p95_ms',
                        'current_value': risk_latency_p95,
                        'threshold': self.thresholds['risk_max_latency_p95_ms'],
                        'message': f"Risk service P95 latency ({risk_latency_p95:.1f}ms) exceeds threshold ({self.thresholds['risk_max_latency_p95_ms']:.1f}ms)"
                    })
                    self.alert_cooldowns[alert_key] = current_time

            # Market Data Pipeline thresholds
            market_data_health = metrics.get('market_data_pipeline', {})
            mmap_stats = market_data_health.get('mmap_buffer_stats', {})
            for channel_name, stats in mmap_stats.items():
                if isinstance(stats, dict):
                    fill_percentage = stats.get('fill_percentage', 0)
                    if fill_percentage > self.thresholds['market_data_mmap_max_fill_pct']:
                        alert_key = f'market_data_mmap_fill_{channel_name}'
                        if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                            alerts.append({
                                'alert_type': 'MARKET_DATA_THRESHOLD_EXCEEDED',
                                'severity': 'WARNING',
                                'metric': 'market_data_mmap_fill_percentage',
                                'channel': channel_name,
                                'current_value': fill_percentage,
                                'threshold': self.thresholds['market_data_mmap_max_fill_pct'],
                                'message': f"Market data mmap buffer '{channel_name}' fill ({fill_percentage:.1f}%) exceeds threshold ({self.thresholds['market_data_mmap_max_fill_pct']:.1f}%)"
                            })
                            self.alert_cooldowns[alert_key] = current_time

            # Check for clogged channels
            channel_health = market_data_health.get('channel_health', {})
            for channel_name, health in channel_health.items():
                if health.get('status') == 'clogged':
                    clogged_duration = health.get('clogged_duration', 0)
                    if clogged_duration > self.thresholds['market_data_bulk_channel_clogged_duration_sec']:
                        alert_key = f'market_data_clogged_{channel_name}'
                        if current_time - self.alert_cooldowns[alert_key] > self.alert_cooldown_period_sec:
                            alerts.append({
                                'alert_type': 'MARKET_DATA_CHANNEL_CLOGGED',
                                'severity': 'CRITICAL',
                                'metric': 'market_data_channel_clogged_duration',
                                'channel': channel_name,
                                'current_value': clogged_duration,
                                'threshold': self.thresholds['market_data_bulk_channel_clogged_duration_sec'],
                                'message': f"Market data channel '{channel_name}' has been clogged for {clogged_duration:.1f} seconds"
                            })
                            self.alert_cooldowns[alert_key] = current_time

            return alerts

        except Exception as e:
            logger.error(f"Error checking thresholds: {e}")
            return []

    def _publish_system_alert(self, metrics_snapshot_for_context: Dict[str, Any], alert_definition: Dict[str, Any]) -> None:
        """
        Publish system alert to Redis stream using proper TESTRADE message formatting.
        CRITICAL: Must use _create_redis_message_json and send data_json_string to IPC client.
        """
        try:
            # Create comprehensive alert payload
            alert_payload = {
                'alert_details': alert_definition,
                'system_context': {
                    'cpu_usage_percent': metrics_snapshot_for_context.get('system_resources', {}).get('cpu', {}).get('usage_percent', 0),
                    'memory_usage_percent': metrics_snapshot_for_context.get('system_resources', {}).get('memory', {}).get('usage_percent', 0),
                    'application_uptime_seconds': metrics_snapshot_for_context.get('application_health', {}).get('process', {}).get('uptime_seconds', 0),
                },
                'subsystem_status': {
                    'ocr_status': metrics_snapshot_for_context.get('ocr_subsystem', {}).get('process_status', 'unknown'),
                    'eventbus_status': metrics_snapshot_for_context.get('eventbus_health', {}).get('status', 'unknown'),
                    'risk_service_status': metrics_snapshot_for_context.get('risk_service', {}).get('status', 'unknown'),
                    'market_data_status': metrics_snapshot_for_context.get('market_data_pipeline', {}).get('status', 'unknown'),
                },
                'timestamp': time.time(),
                'correlation_id': f"alert_{int(time.time() * 1000)}_{alert_definition.get('metric', 'unknown')}",
            }

            # Publish via central telemetry queue if available
            if self._telemetry_service:
                # Use central telemetry queue (preferred)
                # Pass the data directly without JSON serialization
                success = self._telemetry_service.enqueue(
                    source_component="SystemHealthService",
                    event_type="TESTRADE_SYSTEM_ALERT",
                    payload=alert_payload,
                    stream_override="testrade:system-alerts"
                )
                if success:
                    logger.warning(f"Published system alert via telemetry: {alert_definition['alert_type']} - {alert_definition['message']}")
                else:
                    logger.error(f"Failed to publish system alert via telemetry: {alert_definition['alert_type']}")
            else:
                # Fallback - log warning, telemetry service should be available
                logger.warning(f"Telemetry service not available for system alert: {alert_definition['alert_type']} - skipping direct IPC")

        except Exception as e:
            logger.error(f"Error publishing system alert: {e}", exc_info=True)

    def _publish_system_baseline(self, metrics_snapshot: Dict[str, Any]) -> None:
        """
        Publish system baseline metrics to Redis stream using proper TESTRADE message formatting.
        CRITICAL: Must use _create_redis_message_json and send data_json_string to IPC client.
        """
        try:
            # Create comprehensive baseline payload
            baseline_payload = {
                'system_metrics': metrics_snapshot.get('system_resources', {}),
                'network_metrics': metrics_snapshot.get('network_health', {}),
                'application_metrics': metrics_snapshot.get('application_health', {}),
                'ocr_metrics': metrics_snapshot.get('ocr_subsystem', {}),
                'eventbus_metrics': metrics_snapshot.get('eventbus_health', {}),
                'risk_service_metrics': metrics_snapshot.get('risk_service', {}),
                'market_data_metrics': metrics_snapshot.get('market_data_pipeline', {}),
                'performance_summary': metrics_snapshot.get('performance_summary', {}),
                'trend_analysis': metrics_snapshot.get('trend_analysis', {}),
                'pipeline_health': metrics_snapshot.get('pipeline_health', {}),
                'timestamp': time.time(),
                'correlation_id': f"baseline_{int(time.time() * 1000)}",
            }

            # Publish via correlation logger if available
            if self._correlation_logger:
                # Use correlation logger (preferred)
                self._correlation_logger.log_event(
                    source_component="SystemHealthService",
                    event_name="SystemBaseline",
                    payload=baseline_payload
                )
                logger.info(f"Published system baseline metrics via correlation logger")
            else:
                # Fallback - log warning, correlation logger should be available
                logger.warning(f"Correlation logger not available for system baseline - skipping direct IPC")

        except Exception as e:
            logger.error(f"Error publishing system baseline: {e}", exc_info=True)

    def _update_metrics_history(self, metrics: Dict[str, Any]) -> None:
        """Update metrics history for trend analysis."""
        try:
            current_time = time.time()

            # Update system resource history
            system_resources = metrics.get('system_resources', {})
            if 'cpu' in system_resources:
                self.metrics_history['cpu_samples'].append({
                    'timestamp': current_time,
                    'value': system_resources['cpu'].get('usage_percent', 0)
                })

            if 'memory' in system_resources:
                self.metrics_history['memory_samples'].append({
                    'timestamp': current_time,
                    'value': system_resources['memory'].get('usage_percent', 0)
                })

            # Update EventBus history
            eventbus_health = metrics.get('eventbus_health', {})
            if 'queue_size' in eventbus_health:
                self.metrics_history['eventbus_queue_samples'].append({
                    'timestamp': current_time,
                    'value': eventbus_health['queue_size']
                })

            # Update Risk service history
            risk_health = metrics.get('risk_service', {})
            if 'latency_p95_ms' in risk_health:
                self.metrics_history['risk_latency_samples'].append({
                    'timestamp': current_time,
                    'value': risk_health['latency_p95_ms']
                })

        except Exception as e:
            logger.error(f"Error updating metrics history: {e}")

    def _calculate_trend_analysis(self) -> Dict[str, Any]:
        """Calculate trend analysis from metrics history."""
        try:
            trends = {}

            for metric_name, samples in self.metrics_history.items():
                if len(samples) >= 2:
                    recent_samples = list(samples)[-10:]  # Last 10 samples
                    values = [s['value'] for s in recent_samples]

                    if len(values) >= 2:
                        # Simple trend calculation
                        first_half = values[:len(values)//2]
                        second_half = values[len(values)//2:]

                        avg_first = sum(first_half) / len(first_half)
                        avg_second = sum(second_half) / len(second_half)

                        trend_direction = 'increasing' if avg_second > avg_first else 'decreasing' if avg_second < avg_first else 'stable'
                        trend_magnitude = abs(avg_second - avg_first) / avg_first if avg_first > 0 else 0

                        trends[metric_name] = {
                            'direction': trend_direction,
                            'magnitude': trend_magnitude,
                            'current_avg': avg_second,
                            'previous_avg': avg_first,
                        }

            return trends
        except Exception as e:
            logger.error(f"Error calculating trend analysis: {e}")
            return {}

    # Method removed - now using telemetry service directly
    def _get_message_formatter(self) -> Optional[Callable]:
        logger.debug("SHMS: _get_message_formatter deprecated - using telemetry service directly")
        return None
