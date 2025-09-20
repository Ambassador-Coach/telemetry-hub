# ApplicationCore/utils/performance_benchmarker.py
import time
import json
import logging
from collections import defaultdict
from threading import Lock, Thread, Event
from typing import Dict, List, Any, Optional
import statistics # For p50, p99 calculations
import queue # Added for asynchronous processing

# Attempt to import redis, but make it optional for now if not fully set up
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    redis = None # type: ignore
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)

class PerformanceBenchmarker:
    def __init__(self,
                 redis_client: Optional[Any] = None,
                 redis_prefix: str = "perf_metrics",
                 metric_ttl_seconds: int = 7 * 24 * 60 * 60, # Default 7 days
                 max_entries_per_metric: int = 10000):       # Default 10k entries
        """
        Initializes the PerformanceBenchmarker.

        Args:
            redis_client: An already connected Redis client instance.
                          If None, metrics will only be stored in memory.
            redis_prefix: Prefix for Redis keys used to store metrics.
            metric_ttl_seconds: TTL for Redis keys (not directly used by zremrangebyrank,
                                but could be for overall key expiry).
            max_entries_per_metric: Max entries to keep per metric in Redis Sorted Sets.
        """
        self.metrics: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._lock = Lock()
        self.redis_client = None
        self.redis_prefix = redis_prefix

        # Store these config values directly
        self.redis_metric_ttl_seconds = metric_ttl_seconds
        self.redis_max_entries_per_metric = max_entries_per_metric

        # Asynchronous Redis writing setup
        self._metric_queue = queue.Queue()
        self._stop_event = Event()
        self._writer_thread = Thread(target=self._redis_writer_thread, daemon=True)
        self._writer_thread.start()

        # Initialize Redis client (existing logic)
        if REDIS_AVAILABLE and redis_client:
            try:
                if hasattr(redis_client, 'ping') and redis_client.ping():
                    self.redis_client = redis_client
                    logger.info(f"PerformanceBenchmarker initialized with active Redis client. TTL: {self.redis_metric_ttl_seconds}s, MaxEntries: {self.redis_max_entries_per_metric}")
                else:
                    logger.warning("PerformanceBenchmarker: Provided Redis client failed ping. Metrics will be in-memory only.")
            except Exception as e:
                logger.warning(f"PerformanceBenchmarker: Error connecting or pinging provided Redis client: {e}. Metrics will be in-memory only.")
        elif REDIS_AVAILABLE and not redis_client:
            logger.info("PerformanceBenchmarker: No Redis client provided. Metrics will be in-memory only.")
        elif not REDIS_AVAILABLE:
            logger.warning("PerformanceBenchmarker: 'redis' library not installed. Metrics will be in-memory only.")

    def __del__(self):
        self.stop()

    def stop(self):
        """Signals the writer thread to stop and waits for it to finish."""
        self._stop_event.set()
        self._writer_thread.join(timeout=5) # Give it some time to finish
        if self._writer_thread.is_alive():
            logger.warning("PerformanceBenchmarker: Redis writer thread did not terminate gracefully.")

    def _redis_writer_thread(self):
        """Dedicated thread to write metrics to Redis asynchronously."""
        while not self._stop_event.is_set():
            try:
                # Get metric data from queue with a timeout to allow checking stop event
                metric_data = self._metric_queue.get(timeout=1)
                if self.redis_client:
                    name = metric_data['name']
                    value = metric_data['value']
                    timestamp = metric_data['timestamp']
                    context = metric_data['context']

                    redis_key = f"{self.redis_prefix}:{name}"
                    try:
                        payload = json.dumps({'timestamp': timestamp, 'value': float(value), 'context': context or {}})
                        self.redis_client.zadd(redis_key, {payload: timestamp})
                        self.redis_client.zremrangebyrank(redis_key, 0, - (self.redis_max_entries_per_metric + 1))
                        logger.debug(f"Metric '{name}' (value: {value}) persisted to Redis sorted set '{redis_key}'.")
                    except Exception as e:
                        logger.error(f"Failed to persist metric '{name}' to Redis in writer thread: {e}")
                self._metric_queue.task_done()
            except queue.Empty:
                continue # No items in queue, check stop event again
            except Exception as e:
                logger.error(f"Error in Redis writer thread: {e}", exc_info=True)

    def capture_metric(self, name: str, value: float, context: Optional[Dict[str, Any]] = None):
        """
        Captures a performance metric. Adds it to an internal queue for asynchronous Redis writing.

        Args:
            name: Name of the metric (e.g., "ocr_conditioning_time_ms").
            value: The measured value (e.g., duration in ms, queue size).
            context: Optional dictionary for additional context (e.g., symbol, event_id).
        """
        timestamp = time.time()
        metric_data = {
            'name': name,
            'timestamp': timestamp,
            'value': float(value), # Ensure value is float
            'context': context or {}
        }

        with self._lock:
            self.metrics[name].append(metric_data)
            # Optional: Limit in-memory list size if it grows too large
            # if len(self.metrics[name]) > SOME_MAX_IN_MEMORY_LIMIT:
            #     self.metrics[name].pop(0)

        if self.redis_client: # Only queue if Redis client is available
            try:
                self._metric_queue.put_nowait(metric_data) # Non-blocking put
            except queue.Full:
                logger.warning(f"PerformanceBenchmarker: Metric queue is full, dropping metric '{name}'.")

    def get_stats(self, name: str, time_window_seconds: Optional[float] = None) -> Dict[str, Any]:
        """
        Calculates statistics for a given metric, optionally within a time window.
        If time_window_seconds is None, uses all in-memory captured metrics.
        If Redis is available and time_window_seconds is provided, can fetch from Redis.

        Args:
            name: The name of the metric.
            time_window_seconds: Optional. If provided, calculate stats for metrics
                                 captured within this many seconds from now.

        Returns:
            A dictionary with 'mean', 'median', 'p95', 'p99', 'min', 'max', 'count', 'stddev'.
        """
        values: List[float] = []
        relevant_metrics: List[Dict[str, Any]] = []

        with self._lock: # Protect read of in-memory self.metrics
            all_metrics_for_name = self.metrics.get(name, [])
        
        if time_window_seconds is not None:
            cutoff_timestamp = time.time() - time_window_seconds
            # First try in-memory metrics
            for m_data in all_metrics_for_name:
                if m_data['timestamp'] >= cutoff_timestamp:
                    relevant_metrics.append(m_data)
            
            # If Redis client is available, augment/replace with Redis data for the window
            if self.redis_client:
                redis_key = f"{self.redis_prefix}:{name}"
                try:
                    # Fetch entries from Redis within the time window
                    redis_entries_json = self.redis_client.zrangebyscore(
                        redis_key, 
                        min=cutoff_timestamp, 
                        max=time.time() # Current time
                    )
                    # If Redis provided data, prefer it for the window as it's more persistent
                    if redis_entries_json:
                        relevant_metrics = [] # Reset if we get Redis data
                        for entry_json in redis_entries_json:
                            try:
                                relevant_metrics.append(json.loads(entry_json))
                            except json.JSONDecodeError:
                                logger.warning(f"Could not decode metric from Redis for {name}: {entry_json[:100]}")
                    logger.debug(f"Fetched {len(relevant_metrics)} metrics for '{name}' from Redis/memory for time window {time_window_seconds}s.")
                except Exception as e:
                    logger.error(f"Error fetching metrics for '{name}' from Redis: {e}. Using in-memory only.")
                    # Fallback to in-memory if Redis fails, re-filter in-memory by time_window
                    relevant_metrics = [m for m in all_metrics_for_name if m['timestamp'] >= cutoff_timestamp]
        else: # No time window, use all in-memory metrics
            relevant_metrics = all_metrics_for_name

        # ROBUST TYPE CHECKING: Filter out non-numeric values to prevent TypeError
        values: List[float] = []
        for m_data in relevant_metrics:
            metric_val = m_data.get('value')
            if isinstance(metric_val, (int, float)):
                values.append(float(metric_val))
            else:
                # Log the problematic metric data for debugging
                logger.warning(f"PerformanceBenchmarker: Non-numeric value encountered for metric '{name}': {metric_val} (type: {type(metric_val)}). Context: {m_data.get('context')}. Skipping this value for stats.")

        if not values:
            return {'mean': 0, 'median': 0, 'p50': 0, 'p95': 0, 'p99': 0, 'min': 0, 'max': 0, 'count': 0, 'stddev': 0}

        values.sort() # Ensure sorted for percentile calculations
        count = len(values)

        # Ensure indices are valid before accessing
        p95_idx = min(int(count * 0.95), count - 1) if count > 0 else 0
        p99_idx = min(int(count * 0.99), count - 1) if count > 0 else 0

        calculated_stats = {
            'mean': statistics.mean(values) if values else 0,
            'median': statistics.median(values) if values else 0,
            'p50': statistics.median(values) if values else 0,
            'p95': values[p95_idx] if count > 0 else 0,
            'p99': values[p99_idx] if count > 0 else 0,
            'min': min(values) if values else 0,
            'max': max(values) if values else 0,
            'count': count,
            'stddev': statistics.stdev(values) if count > 1 else 0
        }
        return calculated_stats

    def _load_baseline_from_redis(self, metric_name: str, baseline_id: str) -> Optional[Dict[str, float]]:
        """Loads a specific baseline snapshot from Redis."""
        if not self.redis_client:
            logger.warning(f"Cannot load baseline for '{metric_name}/{baseline_id}': Redis client not available.")
            return None
        redis_key = f"{self.redis_prefix}:{metric_name}:baseline:{baseline_id}"
        try:
            baseline_json = self.redis_client.get(redis_key)
            if baseline_json:
                return json.loads(baseline_json)
            logger.info(f"No baseline '{baseline_id}' found in Redis for metric '{metric_name}'.")
        except Exception as e:
            logger.error(f"Error loading baseline '{baseline_id}' for metric '{metric_name}' from Redis: {e}")
        return None

    def save_baseline_to_redis(self, metric_name: str, baseline_id: str, stats: Dict[str, float], ttl_seconds: Optional[int] = None):
        """Saves a stats dictionary as a named baseline snapshot in Redis."""
        if not self.redis_client:
            logger.warning(f"Cannot save baseline for '{metric_name}/{baseline_id}': Redis client not available.")
            return False
        redis_key = f"{self.redis_prefix}:{metric_name}:baseline:{baseline_id}"
        try:
            self.redis_client.set(redis_key, json.dumps(stats))
            if ttl_seconds:
                self.redis_client.expire(redis_key, ttl_seconds)
            logger.info(f"Baseline '{baseline_id}' for metric '{metric_name}' saved to Redis: {stats}")
            return True
        except Exception as e:
            logger.error(f"Error saving baseline '{baseline_id}' for metric '{metric_name}' to Redis: {e}")
        return False

    def compare_to_baseline(self, metric_name: str, current_stats: Dict[str, float], baseline_id: str = "default") -> Optional[Dict[str, Any]]:
        """
        Compares current metric statistics to a saved baseline from Redis.
        
        Args:
            metric_name: The name of the metric.
            current_stats: The current statistics dictionary (from get_stats()).
            baseline_id: Identifier for the baseline to compare against (e.g., "release_v1.2").
        
        Returns:
            A dictionary with comparison results, or None if baseline not found/error.
        """
        baseline_stats = self._load_baseline_from_redis(metric_name, baseline_id)
        if not baseline_stats or not current_stats or current_stats['count'] == 0:
            logger.warning(f"Cannot compare for '{metric_name}': Baseline '{baseline_id}' not found or current stats are empty.")
            return {
                'metric_name': metric_name,
                'baseline_id': baseline_id,
                'comparison_possible': False,
                'reason': "Baseline not found or no current stats."
            }

        # Ensure keys exist with defaults before division/comparison
        baseline_p99 = baseline_stats.get('p99', 0)
        current_p99 = current_stats.get('p99', 0)
        baseline_mean = baseline_stats.get('mean', 0) # For throughput, lower mean is better if 'value' is latency
        current_mean = current_stats.get('mean', 0)   # So higher current_mean / baseline_mean is worse throughput

        # Assuming 'value' being benchmarked is latency (lower is better)
        latency_improvement_factor = baseline_p99 / current_p99 if current_p99 > 0 else float('inf') # Higher is better
        
        # For throughput (if 'value' represents items/sec, higher is better)
        # This example assumes 'value' is latency, so throughput needs to be derived or measured differently
        # If 'value' was items processed, then:
        # throughput_improvement_factor = current_mean / baseline_mean if baseline_mean > 0 else float('inf')
        
        # For this benchmarker, let's assume 'value' is typically latency.
        # Throughput would be 1/latency or derived from 'count' over time window.
        # For simplicity, we'll focus on latency regression for now.

        regression_threshold_factor = 1.2 # e.g., current p99 is 20% worse than baseline
        regression_detected = current_p99 > (baseline_p99 * regression_threshold_factor) if baseline_p99 > 0 else False

        return {
            'metric_name': metric_name,
            'baseline_id': baseline_id,
            'comparison_possible': True,
            'current_p99': current_p99,
            'baseline_p99': baseline_p99,
            'current_mean': current_mean,
            'baseline_mean': baseline_mean,
            'latency_improvement_factor': latency_improvement_factor,
            'regression_detected_p99': regression_detected,
            'regression_threshold_factor': regression_threshold_factor
        }

    def clear_in_memory_metrics(self, name: Optional[str] = None):
        """Clears in-memory metrics, either for a specific metric or all.
           Also resets all trackers from performance_tracker.real.
        """
        with self._lock:
            if name:
                if name in self.metrics:
                    self.metrics[name].clear()
                    logger.info(f"PerformanceBenchmarker: Cleared in-memory metrics for '{name}'.")
            else:
                self.metrics.clear()
                logger.info("PerformanceBenchmarker: Cleared all in-memory metrics.")

        # ADDED: Clearing of performance_tracker.real trackers
        try:
            # Import locally to ensure it picks up the correct module state regarding PERFORMANCE_TRACKING_ENABLED
            from utils.performance_tracker import real as performance_tracker_real
            if performance_tracker_real.PERFORMANCE_TRACKING_ENABLED: # Only reset if it was active
                if hasattr(performance_tracker_real, 'reset_all_trackers'):
                    performance_tracker_real.reset_all_trackers()
                    logger.info("PerformanceBenchmarker: Called reset_all_trackers() from performance_tracker.real.")
                else:
                    logger.warning("PerformanceBenchmarker: performance_tracker.real module does not have reset_all_trackers().")
            else:
                logger.info("PerformanceBenchmarker: Fine-grained performance tracking (real.py) is disabled, skipping reset_all_trackers().")
        except ImportError:
            logger.warning("PerformanceBenchmarker: Could not import utils.performance_tracker.real to reset its trackers.")
        except Exception as e_rat:
            logger.error(f"PerformanceBenchmarker: Error calling reset_all_trackers from performance_tracker.real: {e_rat}", exc_info=True)

    def increment(self, name: str, value: float = 1.0, context: Optional[Dict[str, Any]] = None):
        """
        Convenience method for incrementing counter metrics.

        Args:
            name: Name of the metric (e.g., "events_processed_count").
            value: The increment value (default: 1.0).
            context: Optional dictionary for additional context.
        """
        self.capture_metric(name, value, context)

    def gauge(self, name: str, value: float, context: Optional[Dict[str, Any]] = None):
        """
        Convenience method for gauge metrics (alias for capture_metric).

        Args:
            name: Name of the metric (e.g., "queue_size_current").
            value: The gauge value.
            context: Optional dictionary for additional context.
        """
        self.capture_metric(name, value, context)

    def get_metrics_summary(self, key_metric_names: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Provides a summary of statistics for a predefined list of key metrics,
        or all metrics if key_metric_names is None (can be verbose).
        For SHMS, passing a specific list is recommended.

        Args:
            key_metric_names: Optional list of specific metrics to summarize.
                             If None, will summarize all available metrics.

        Returns:
            Dictionary containing metric summaries with statistics.
        """
        summary = {"status": "SUMMARY_OK", "metrics": {}}
        metrics_to_summarize = []

        if key_metric_names:
            metrics_to_summarize = key_metric_names
        else:
            # Fallback to all captured metric names if no specific list provided
            # This could be very verbose and potentially slow if many metrics.
            with self._lock:
                metrics_to_summarize = list(self.metrics.keys())
            if not metrics_to_summarize and self.redis_client:
                # Try to discover keys from Redis (can be slow, use with caution)
                try:
                    # Example: SCAN for keys with prefix. This is not ideal for frequent calls.
                    # discovered_keys = [k.decode('utf-8').replace(f"{self.redis_prefix}:", "")
                    #                    for k in self.redis_client.scan_iter(f"{self.redis_prefix}:*:baseline:*")] # Just an example
                    pass # For now, don't auto-discover all keys from Redis for summary.
                except Exception as e_scan:
                    logger.warning(f"PB: Error trying to discover metric keys from Redis for summary: {e_scan}")

        if not metrics_to_summarize:
            summary["status"] = "NO_KEY_METRICS_SPECIFIED_OR_FOUND"
            return summary

        for metric_name in metrics_to_summarize:
            stats = self.get_stats(metric_name) # Get full stats for this metric
            if stats and stats.get('count', 0) > 0:
                summary["metrics"][metric_name] = {
                    "mean": stats.get('mean'),
                    "p95": stats.get('p95'),
                    "count": stats.get('count'),
                    "max": stats.get('max')
                }
            else:
                summary["metrics"][metric_name] = {"status": "NO_DATA", "count": 0}

        return summary
