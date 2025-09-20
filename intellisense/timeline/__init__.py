"""
IntelliSense Timeline Module

Contains timeline generation and synchronization components for multi-source
event correlation and replay.
"""

from .generator_config import TimelineGeneratorConfig
from .timeline_generator import EpochTimelineGenerator

__all__ = [
    'TimelineGeneratorConfig',
    'EpochTimelineGenerator'
]
