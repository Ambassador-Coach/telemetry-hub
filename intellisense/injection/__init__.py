"""
IntelliSense Redis-based Injection System

Provides clean, isolated event injection into TESTRADE via Redis commands.
"""

from .redis_injector import IntelliSenseRedisInjector

__all__ = ['IntelliSenseRedisInjector']