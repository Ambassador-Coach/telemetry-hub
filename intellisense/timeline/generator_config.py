"""
Configuration for Timeline Generation

Defines configuration options for the EpochTimelineGenerator to control
timeline generation behavior, filtering, and replay parameters.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class TimelineGeneratorConfig:
    """Configuration for the EpochTimelineGenerator."""
    speed_factor: float = 1.0
    start_time_offset_s: float = 0.0  # Offset from the first event's timestamp to start replay
    filter_event_types: Optional[Set[str]] = None  # e.g., {"OCR_FRAME", "PRICE_TICK"} to only include these
    # Future: filter_by_source_sense: Optional[Set[str]] = None
