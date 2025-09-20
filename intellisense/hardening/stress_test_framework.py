#!/usr/bin/env python3
"""
IntelliSense Stress Testing Framework

Comprehensive stress testing to validate IntelliSense reliability under
production-like conditions including:
- High-frequency event generation
- Memory pressure simulation
- Network latency simulation
- Dependency failure simulation
- Resource exhaustion scenarios
- Concurrent load testing
"""

import logging
import time
import threading
import random
import gc
import psutil
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

logger = logging.getLogger(__name__)

@dataclass
class StressTestConfig:
    """Configuration for stress testing scenarios"""
    name: str
    duration_seconds: float
    concurrent_threads: int
    events_per_second: int
    memory_pressure_mb: int
    simulate_failures: bool
    failure_rate_percent: float
    network_latency_ms: float
    resource_limits: Dict[str, Any]

@dataclass
class StressTestResult:
    """Results from a stress test execution"""
    config_name: str
    start_time: float
    end_time: float
    duration: float
    total_events: int
    successful_events: int
    failed_events: int
    error_rate: float
    avg_latency_ms: float
    max_latency_ms: float
    memory_peak_mb: float
    cpu_peak_percent: float
    errors: List[str]
    performance_degradation: bool
    system_stable: bool

class StressTestFramework:
    """Main stress testing framework for IntelliSense"""
    
    def __init__(self, target_system: Any):
        self.target_system = target_system
        self.test_results: List[StressTestResult] = []
        self.is_running = False
        self._stop_event = threading.Event()
        
        # Performance baselines
        self.baseline_memory_mb = self._get_current_memory_mb()
        self.baseline_cpu_percent = self._get_current_cpu_percent()
        
    def _get_current_memory_mb(self) -> float:
        """Get current memory usage in MB"""
        try:
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except:
            return 0.0
    
    def _get_current_cpu_percent(self) -> float:
        """Get current CPU usage percentage"""
        try:
            process = psutil.Process()
            return process.cpu_percent()
        except:
            return 0.0
    
    def create_high_frequency_config(self) -> StressTestConfig:
        """Create configuration for high-frequency trading simulation"""
        return StressTestConfig(
            name="high_frequency_trading",
            duration_seconds=300.0,  # 5 minutes
            concurrent_threads=10,
            events_per_second=100,
            memory_pressure_mb=0,
            simulate_failures=False,
            failure_rate_percent=0.0,
            network_latency_ms=0.0,
            resource_limits={"max_memory_mb": 1000, "max_cpu_percent": 80}
        )
    
    def create_memory_pressure_config(self) -> StressTestConfig:
        """Create configuration for memory pressure testing"""
        return StressTestConfig(
            name="memory_pressure",
            duration_seconds=180.0,  # 3 minutes
            concurrent_threads=5,
            events_per_second=50,
            memory_pressure_mb=500,  # Allocate 500MB extra
            simulate_failures=False,
            failure_rate_percent=0.0,
            network_latency_ms=0.0,
            resource_limits={"max_memory_mb": 2000, "max_cpu_percent": 90}
        )
    
    def create_failure_simulation_config(self) -> StressTestConfig:
        """Create configuration for dependency failure simulation"""
        return StressTestConfig(
            name="dependency_failures",
            duration_seconds=240.0,  # 4 minutes
            concurrent_threads=8,
            events_per_second=30,
            memory_pressure_mb=0,
            simulate_failures=True,
            failure_rate_percent=15.0,  # 15% failure rate
            network_latency_ms=50.0,
            resource_limits={"max_memory_mb": 1500, "max_cpu_percent": 85}
        )
    
    def create_endurance_config(self) -> StressTestConfig:
        """Create configuration for endurance testing"""
        return StressTestConfig(
            name="endurance_test",
            duration_seconds=1800.0,  # 30 minutes
            concurrent_threads=6,
            events_per_second=20,
            memory_pressure_mb=100,
            simulate_failures=True,
            failure_rate_percent=5.0,
            network_latency_ms=25.0,
            resource_limits={"max_memory_mb": 1200, "max_cpu_percent": 75}
        )
    
    def run_stress_test(self, config: StressTestConfig) -> StressTestResult:
        """Execute a stress test with the given configuration"""
        logger.info(f"Starting stress test: {config.name}")
        
        start_time = time.time()
        self.is_running = True
        self._stop_event.clear()
        
        # Initialize result tracking
        result = StressTestResult(
            config_name=config.name,
            start_time=start_time,
            end_time=0,
            duration=0,
            total_events=0,
            successful_events=0,
            failed_events=0,
            error_rate=0,
            avg_latency_ms=0,
            max_latency_ms=0,
            memory_peak_mb=self.baseline_memory_mb,
            cpu_peak_percent=self.baseline_cpu_percent,
            errors=[],
            performance_degradation=False,
            system_stable=True
        )
        
        # Apply memory pressure if configured
        memory_ballast = None
        if config.memory_pressure_mb > 0:
            memory_ballast = self._create_memory_pressure(config.memory_pressure_mb)
        
        try:
            # Run the stress test
            self._execute_stress_test(config, result)
            
        except Exception as e:
            logger.error(f"Stress test {config.name} failed: {e}")
            result.errors.append(f"Test execution failed: {e}")
            result.system_stable = False
            
        finally:
            # Clean up memory pressure
            if memory_ballast:
                del memory_ballast
                gc.collect()
            
            self.is_running = False
            result.end_time = time.time()
            result.duration = result.end_time - result.start_time
            
            # Calculate final metrics
            if result.total_events > 0:
                result.error_rate = (result.failed_events / result.total_events) * 100
            
            # Check for performance degradation
            result.performance_degradation = (
                result.memory_peak_mb > self.baseline_memory_mb * 2 or
                result.cpu_peak_percent > 90 or
                result.error_rate > 10
            )
            
            self.test_results.append(result)
            logger.info(f"Stress test {config.name} completed: {result.successful_events}/{result.total_events} events successful")
            
        return result
    
    def _create_memory_pressure(self, mb_size: int) -> List[bytes]:
        """Create memory pressure by allocating large objects"""
        logger.info(f"Creating {mb_size}MB memory pressure")
        ballast = []
        chunk_size = 1024 * 1024  # 1MB chunks
        
        for _ in range(mb_size):
            ballast.append(b'x' * chunk_size)
        
        return ballast
    
    def _execute_stress_test(self, config: StressTestConfig, result: StressTestResult):
        """Execute the main stress test logic"""
        end_time = time.time() + config.duration_seconds
        latencies = []
        
        # Create thread pool for concurrent execution
        with ThreadPoolExecutor(max_workers=config.concurrent_threads) as executor:
            
            while time.time() < end_time and not self._stop_event.is_set():
                # Submit events for this second
                futures = []
                events_this_second = config.events_per_second
                
                for _ in range(events_this_second):
                    future = executor.submit(self._execute_single_event, config)
                    futures.append(future)
                
                # Wait for events to complete and collect results
                for future in as_completed(futures, timeout=2.0):
                    try:
                        event_result = future.result()
                        result.total_events += 1
                        
                        if event_result['success']:
                            result.successful_events += 1
                        else:
                            result.failed_events += 1
                            result.errors.append(event_result['error'])
                        
                        latencies.append(event_result['latency_ms'])
                        
                        # Update peak metrics
                        current_memory = self._get_current_memory_mb()
                        current_cpu = self._get_current_cpu_percent()
                        
                        result.memory_peak_mb = max(result.memory_peak_mb, current_memory)
                        result.cpu_peak_percent = max(result.cpu_peak_percent, current_cpu)
                        
                    except Exception as e:
                        result.failed_events += 1
                        result.errors.append(f"Event execution failed: {e}")
                
                # Rate limiting - wait for next second
                time.sleep(max(0, 1.0 - (time.time() % 1.0)))
        
        # Calculate latency metrics
        if latencies:
            result.avg_latency_ms = sum(latencies) / len(latencies)
            result.max_latency_ms = max(latencies)
    
    def _execute_single_event(self, config: StressTestConfig) -> Dict[str, Any]:
        """Execute a single test event"""
        start_time = time.time()
        
        try:
            # Simulate network latency if configured
            if config.network_latency_ms > 0:
                time.sleep(config.network_latency_ms / 1000.0)
            
            # Simulate random failures if configured
            if config.simulate_failures and random.random() < (config.failure_rate_percent / 100.0):
                raise Exception("Simulated dependency failure")
            
            # Execute actual test operation
            success = self._execute_test_operation()
            
            latency_ms = (time.time() - start_time) * 1000
            
            return {
                'success': success,
                'latency_ms': latency_ms,
                'error': None
            }
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            return {
                'success': False,
                'latency_ms': latency_ms,
                'error': str(e)
            }
    
    def _execute_test_operation(self) -> bool:
        """Execute the actual test operation on the target system"""
        try:
            # This would be customized based on what we're testing
            # For now, simulate a basic operation
            if hasattr(self.target_system, 'get_health_status'):
                health = self.target_system.get_health_status()
                return health.get('overall_status') in ['HEALTHY', 'DEGRADED']
            
            return True
            
        except Exception as e:
            logger.debug(f"Test operation failed: {e}")
            return False
    
    def run_comprehensive_stress_tests(self) -> List[StressTestResult]:
        """Run all predefined stress test configurations"""
        configs = [
            self.create_high_frequency_config(),
            self.create_memory_pressure_config(),
            self.create_failure_simulation_config(),
            self.create_endurance_config()
        ]
        
        results = []
        for config in configs:
            try:
                result = self.run_stress_test(config)
                results.append(result)
                
                # Brief pause between tests
                time.sleep(10)
                
            except Exception as e:
                logger.error(f"Failed to run stress test {config.name}: {e}")
        
        return results
    
    def generate_stress_test_report(self) -> Dict[str, Any]:
        """Generate comprehensive stress test report"""
        if not self.test_results:
            return {"error": "No stress test results available"}
        
        total_events = sum(r.total_events for r in self.test_results)
        total_successful = sum(r.successful_events for r in self.test_results)
        total_failed = sum(r.failed_events for r in self.test_results)
        
        overall_success_rate = (total_successful / total_events * 100) if total_events > 0 else 0
        
        return {
            "summary": {
                "total_tests": len(self.test_results),
                "total_events": total_events,
                "total_successful": total_successful,
                "total_failed": total_failed,
                "overall_success_rate": overall_success_rate,
                "system_stable": all(r.system_stable for r in self.test_results),
                "performance_degraded": any(r.performance_degradation for r in self.test_results)
            },
            "test_results": [
                {
                    "name": r.config_name,
                    "duration": r.duration,
                    "events": r.total_events,
                    "success_rate": (r.successful_events / r.total_events * 100) if r.total_events > 0 else 0,
                    "avg_latency_ms": r.avg_latency_ms,
                    "max_latency_ms": r.max_latency_ms,
                    "memory_peak_mb": r.memory_peak_mb,
                    "cpu_peak_percent": r.cpu_peak_percent,
                    "stable": r.system_stable,
                    "error_count": len(r.errors)
                }
                for r in self.test_results
            ],
            "recommendations": self._generate_recommendations()
        }
    
    def _generate_recommendations(self) -> List[str]:
        """Generate recommendations based on stress test results"""
        recommendations = []
        
        for result in self.test_results:
            if result.error_rate > 10:
                recommendations.append(f"High error rate ({result.error_rate:.1f}%) in {result.config_name} - investigate error handling")
            
            if result.memory_peak_mb > self.baseline_memory_mb * 3:
                recommendations.append(f"Excessive memory usage in {result.config_name} - check for memory leaks")
            
            if result.avg_latency_ms > 1000:
                recommendations.append(f"High latency ({result.avg_latency_ms:.1f}ms) in {result.config_name} - optimize performance")
            
            if not result.system_stable:
                recommendations.append(f"System instability detected in {result.config_name} - critical issue")
        
        if not recommendations:
            recommendations.append("All stress tests passed - system appears robust for production")
        
        return recommendations
