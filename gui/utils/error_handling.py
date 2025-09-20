# gui/core/error_handling.py - Comprehensive Error Handling

"""
Error Handling and Resilience
==============================
Provides circuit breakers, retry logic, and error recovery mechanisms.

Features:
- Circuit breaker pattern for external services
- Exponential backoff retry
- Error categorization and handling strategies
- Graceful degradation
- Error reporting and alerting
"""

import asyncio
import functools
import logging
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable, Any, Dict, List, TypeVar, Union
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

T = TypeVar('T')


class ErrorCategory(Enum):
    """Categories of errors for different handling strategies"""
    TRANSIENT = "transient"  # Temporary errors that may resolve
    PERMANENT = "permanent"  # Errors that won't resolve without intervention
    RESOURCE = "resource"    # Resource exhaustion errors
    TIMEOUT = "timeout"      # Operation timeouts
    VALIDATION = "validation"  # Input validation errors
    UNKNOWN = "unknown"      # Uncategorized errors


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, rejecting calls
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerStats:
    """Statistics for circuit breaker"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure_time: Optional[datetime] = None
    consecutive_failures: int = 0
    state_changes: List[tuple] = field(default_factory=list)
    
    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls


class CircuitBreaker:
    """
    Circuit breaker implementation for fault tolerance.
    
    Prevents cascading failures by temporarily blocking calls to failing services.
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        
        self._state = CircuitState.CLOSED
        self._stats = CircuitBreakerStats()
        self._half_open_call_count = 0
        
    @property
    def state(self) -> CircuitState:
        return self._state
        
    @property
    def stats(self) -> CircuitBreakerStats:
        return self._stats
        
    def _change_state(self, new_state: CircuitState) -> None:
        """Change circuit state and log it"""
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            self._stats.state_changes.append((datetime.now(), old_state, new_state))
            logger.info(f"Circuit breaker '{self.name}' state changed: {old_state.value} → {new_state.value}")
            
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if not self._stats.last_failure_time:
            return True
            
        time_since_failure = datetime.now() - self._stats.last_failure_time
        return time_since_failure.total_seconds() >= self.recovery_timeout
        
    async def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute function through circuit breaker"""
        # Check circuit state
        if self._state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self._change_state(CircuitState.HALF_OPEN)
                self._half_open_call_count = 0
            else:
                self._stats.rejected_calls += 1
                raise Exception(f"Circuit breaker '{self.name}' is OPEN")
                
        try:
            # Execute the function
            self._stats.total_calls += 1
            result = await func(*args, **kwargs)
            
            # Success handling
            self._on_success()
            return result
            
        except self.expected_exception as e:
            # Failure handling
            self._on_failure()
            raise
            
    def _on_success(self) -> None:
        """Handle successful call"""
        self._stats.successful_calls += 1
        self._stats.consecutive_failures = 0
        
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_call_count += 1
            if self._half_open_call_count >= 3:  # Require 3 successful calls
                self._change_state(CircuitState.CLOSED)
                
    def _on_failure(self) -> None:
        """Handle failed call"""
        self._stats.failed_calls += 1
        self._stats.consecutive_failures += 1
        self._stats.last_failure_time = datetime.now()
        
        if self._state == CircuitState.HALF_OPEN:
            self._change_state(CircuitState.OPEN)
        elif self._stats.consecutive_failures >= self.failure_threshold:
            self._change_state(CircuitState.OPEN)


class RetryPolicy:
    """Configurable retry policy"""
    
    def __init__(
        self,
        max_attempts: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True
    ):
        self.max_attempts = max_attempts
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number"""
        delay = min(
            self.initial_delay * (self.exponential_base ** (attempt - 1)),
            self.max_delay
        )
        
        if self.jitter:
            # Add random jitter to prevent thundering herd
            import random
            delay = delay * (0.5 + random.random() * 0.5)
            
        return delay


def categorize_error(error: Exception) -> ErrorCategory:
    """Categorize an error for appropriate handling"""
    error_type = type(error).__name__
    error_msg = str(error).lower()
    
    # Timeout errors
    if "timeout" in error_type.lower() or "timeout" in error_msg:
        return ErrorCategory.TIMEOUT
        
    # Resource errors
    if any(word in error_msg for word in ["memory", "disk", "space", "quota"]):
        return ErrorCategory.RESOURCE
        
    # Validation errors
    if any(word in error_msg for word in ["invalid", "validation", "bad request"]):
        return ErrorCategory.VALIDATION
        
    # Connection/network errors (usually transient)
    if any(word in error_msg for word in ["connection", "network", "refused"]):
        return ErrorCategory.TRANSIENT
        
    # Default to unknown
    return ErrorCategory.UNKNOWN


def with_retry(
    retry_policy: Optional[RetryPolicy] = None,
    retry_on: tuple = (Exception,),
    circuit_breaker: Optional[CircuitBreaker] = None
):
    """
    Decorator for adding retry logic to async functions.
    
    Usage:
        @with_retry(RetryPolicy(max_attempts=5))
        async def flaky_operation():
            ...
    """
    if retry_policy is None:
        retry_policy = RetryPolicy()
        
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None
            
            for attempt in range(1, retry_policy.max_attempts + 1):
                try:
                    # Use circuit breaker if provided
                    if circuit_breaker:
                        return await circuit_breaker.call(func, *args, **kwargs)
                    else:
                        return await func(*args, **kwargs)
                        
                except retry_on as e:
                    last_exception = e
                    error_category = categorize_error(e)
                    
                    # Don't retry validation errors
                    if error_category == ErrorCategory.VALIDATION:
                        raise
                        
                    if attempt < retry_policy.max_attempts:
                        delay = retry_policy.get_delay(attempt)
                        logger.warning(
                            f"Retry {attempt}/{retry_policy.max_attempts} for {func.__name__} "
                            f"after {error_category.value} error: {e}. "
                            f"Waiting {delay:.1f}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {retry_policy.max_attempts} attempts failed for {func.__name__}: {e}"
                        )
                        raise
                        
            # Should never reach here
            if last_exception:
                raise last_exception
                
        return wrapper
    return decorator


class ErrorHandler:
    """
    Centralized error handling with reporting and recovery strategies.
    """
    
    def __init__(self, service_name: str):
        self.service_name = service_name
        self.error_counts: Dict[str, int] = {}
        self.error_handlers: Dict[ErrorCategory, Callable] = {}
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        
    def register_handler(self, category: ErrorCategory, handler: Callable) -> None:
        """Register a handler for specific error category"""
        self.error_handlers[category] = handler
        
    def get_circuit_breaker(self, name: str, **kwargs) -> CircuitBreaker:
        """Get or create a circuit breaker"""
        if name not in self.circuit_breakers:
            self.circuit_breakers[name] = CircuitBreaker(name, **kwargs)
        return self.circuit_breakers[name]
        
    async def handle_error(self, error: Exception, context: Dict[str, Any]) -> Optional[Any]:
        """Handle an error with appropriate strategy"""
        error_type = type(error).__name__
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1
        
        # Log the error with context
        logger.error(
            f"Error in {self.service_name}: {error_type}: {error}",
            extra={"context": context},
            exc_info=True
        )
        
        # Categorize and handle
        category = categorize_error(error)
        handler = self.error_handlers.get(category)
        
        if handler:
            try:
                return await handler(error, context)
            except Exception as handler_error:
                logger.error(f"Error in error handler: {handler_error}")
                
        # Default handling based on category
        if category == ErrorCategory.TRANSIENT:
            # Transient errors might resolve themselves
            logger.info("Transient error detected, will retry if configured")
        elif category == ErrorCategory.RESOURCE:
            # Resource errors might need cleanup
            logger.warning("Resource error detected, consider cleanup or scaling")
        elif category == ErrorCategory.VALIDATION:
            # Validation errors need fixing at source
            logger.error("Validation error - check input data")
            
        return None
        
    def get_error_stats(self) -> Dict[str, Any]:
        """Get error statistics"""
        return {
            "service": self.service_name,
            "error_counts": self.error_counts,
            "circuit_breakers": {
                name: {
                    "state": cb.state.value,
                    "stats": {
                        "total_calls": cb.stats.total_calls,
                        "failed_calls": cb.stats.failed_calls,
                        "success_rate": cb.stats.success_rate
                    }
                }
                for name, cb in self.circuit_breakers.items()
            }
        }


# Global error handler instance
_global_error_handler: Optional[ErrorHandler] = None


def get_error_handler(service_name: str) -> ErrorHandler:
    """Get or create global error handler"""
    global _global_error_handler
    if _global_error_handler is None:
        _global_error_handler = ErrorHandler(service_name)
    return _global_error_handler


# Example usage patterns
async def example_with_resilience():
    """Example of using error handling features"""
    
    # Get error handler
    error_handler = get_error_handler("ExampleService")
    
    # Get circuit breaker
    redis_breaker = error_handler.get_circuit_breaker(
        "redis",
        failure_threshold=3,
        recovery_timeout=30
    )
    
    # Define retry policy
    retry_policy = RetryPolicy(
        max_attempts=3,
        initial_delay=0.5,
        exponential_base=2
    )
    
    # Use decorator with retry and circuit breaker
    @with_retry(retry_policy=retry_policy, circuit_breaker=redis_breaker)
    async def fetch_data():
        # Simulated operation that might fail
        pass
        
    try:
        result = await fetch_data()
    except Exception as e:
        # Handle with error handler
        await error_handler.handle_error(e, {"operation": "fetch_data"})