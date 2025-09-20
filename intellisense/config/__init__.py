"""
IntelliSense Configuration Module

This module provides configuration classes for IntelliSense test sessions
and component-specific configurations.
"""

from .session_config import (
    OCRTestConfig,
    PriceTestConfig,
    BrokerTestConfig,
    TestSessionConfig
)

__all__ = [
    'OCRTestConfig',
    'PriceTestConfig',
    'BrokerTestConfig',
    'TestSessionConfig'
]
