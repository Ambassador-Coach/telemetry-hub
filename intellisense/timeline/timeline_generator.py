"""
Multi-Source Timeline Generator

Generates precise, interleaved master timelines from multiple replay sources
using epoch timestamping correlation for accurate event ordering.
"""

from __future__ import annotations
import logging
import time  # For controller's time.sleep()
from typing import Iterator, Dict, Any, List, Union, TYPE_CHECKING

# Use TYPE_CHECKING to avoid circular imports
if TYPE_CHECKING:
    from intellisense.core.types import TimelineEvent
    from intellisense.core.interfaces import IOCRDataSource, IPriceDataSource, IBrokerDataSource

# Config for the generator
from .generator_config import TimelineGeneratorConfig

logger = logging.getLogger(__name__)

# Type alias for replay sources
ReplaySourceType = Union['IOCRDataSource', 'IPriceDataSource', 'IBrokerDataSource']


class EpochTimelineGenerator:
    """
    Generates a precise, interleaved master timeline from multiple replay sources
    using epoch timestamping correlation.
    """
    
    def __init__(self, config: TimelineGeneratorConfig):
        self.config = config
        self.replay_sources: Dict[str, ReplaySourceType] = {}  # name -> source_instance
        self._timeline_stats: Dict[str, Any] = {}
        logger.info(f"EpochTimelineGenerator initialized with config: {config}")

    def register_replay_source(self, name: str, source: ReplaySourceType):
        """Register a replay source (OCR, Price, or Broker)."""
        if not hasattr(source, 'load_timeline_data') or not hasattr(source, 'timeline_data'):
            logger.error(f"Source '{name}' does not have required 'load_timeline_data' or 'timeline_data' attributes. Cannot register.")
            return
        self.replay_sources[name] = source
        logger.info(f"Replay source '{name}' (type: {type(source).__name__}) registered with EpochTimelineGenerator.")

    def load_all_source_data(self) -> bool:
        """Instructs all registered replay sources to load their timeline data."""
        all_loaded_successfully = True
        for name, source in self.replay_sources.items():
            logger.info(f"Requesting source '{name}' to load its timeline data...")
            if not source.load_timeline_data():  # Assumes load_timeline_data populates source.timeline_data
                logger.error(f"Failed to load timeline data for source: {name}")
                all_loaded_successfully = False
            else:
                logger.info(f"Timeline data loaded successfully for source: {name}")
        return all_loaded_successfully

    def create_master_timeline(self) -> Iterator['TimelineEvent']:
        """
        Generate a master timeline by collecting events from all registered and loaded
        replay sources, then sorting them globally.
        Events are sorted by perf_counter_timestamp, then global_sequence_id.
        """
        # Import at runtime to avoid circular import
        from intellisense.core.types import TimelineEvent
        
        all_events: List[TimelineEvent] = []
        self._timeline_stats['source_event_counts'] = {}

        for name, source in self.replay_sources.items():
            if hasattr(source, 'timeline_data') and source.timeline_data:
                # Apply event type filter if specified
                source_events = source.timeline_data
                if self.config.filter_event_types:
                    source_events = [event for event in source_events if event.event_type in self.config.filter_event_types]
                
                all_events.extend(source_events)
                count = len(source_events)
                self._timeline_stats['source_event_counts'][name] = count
                logger.info(f"Collected {count} events from source '{name}' for master timeline.")
            else:
                logger.warning(f"No timeline data found or loaded for source: {name}")
                self._timeline_stats['source_event_counts'][name] = 0
        
        if not all_events:
            logger.warning("No events collected from any replay sources for master timeline. Returning empty iterator.")
            self._timeline_stats['total_events'] = 0
            self._timeline_stats['time_span_s'] = 0
            return iter([])

        # Robust global sort using perf_counter_timestamp then global_sequence_id
        all_events.sort(key=lambda event: (
            event.perf_counter_timestamp if event.perf_counter_timestamp is not None else float('inf'),
            event.global_sequence_id if event.global_sequence_id is not None else float('inf'),
            event.correlation_id or ''  # Fallback
        ))
        
        self._timeline_stats['total_events'] = len(all_events)
        if all_events:
            first_event_time = all_events[0].perf_counter_timestamp or 0
            last_event_time = all_events[-1].perf_counter_timestamp or first_event_time
            self._timeline_stats['time_span_s'] = last_event_time - first_event_time
            # Apply start_time_offset if configured
            if self.config.start_time_offset_s > 0 and first_event_time > 0:
                logger.info(f"Applying start_time_offset_s of {self.config.start_time_offset_s}s to timeline.")
                # This conceptually shifts the start, actual sleep handled by controller
        
        logger.info(f"Generated master timeline with {self._timeline_stats['total_events']} total events, spanning {self._timeline_stats['time_span_s']:.2f}s (perf_counter time).")
        yield from all_events

    def get_timeline_stats(self) -> Dict[str, Any]:
        """Get statistics about the generated timeline."""
        # Ensure stats are calculated if create_master_timeline hasn't been called or returned no events
        if 'total_events' not in self._timeline_stats:
            self._timeline_stats['total_events'] = 0
            self._timeline_stats['time_span_s'] = 0
            self._timeline_stats['source_event_counts'] = {name: 0 for name in self.replay_sources}
        return self._timeline_stats
