#!/usr/bin/env python3
"""
Simple Replay Sources for JSONL Files

These classes provide a simple interface for creating replay sources directly from
JSONL correlation log files, without requiring complex configuration objects.

This fixes the real problems identified in the existing replay system:
1. Complex configuration coupling
2. No simple JSONL interface
3. Interface mismatch with EpochTimelineGenerator
"""

import os
import json
import logging
from typing import List, Dict, Any, Iterator, Type, Union
from pathlib import Path

# Import at runtime to avoid circular imports
from intellisense.core.types import (
    TimelineEvent, OCRTimelineEvent, PriceTimelineEvent,
    BrokerTimelineEvent, ProcessedImageFrameTimelineEvent,
    ConfigChangeTimelineEvent, TradingStateChangeTimelineEvent,
    TradeLifecycleTimelineEvent, SystemAlertTimelineEvent,
    AccountSummaryTimelineEvent
)

logger = logging.getLogger(__name__)


class SimpleJSONLReplaySource:
    """
    Simple replay source that loads TimelineEvents from JSONL correlation log files.
    
    This class provides the interface expected by EpochTimelineGenerator without
    requiring complex configuration objects.
    """
    
    def __init__(self, jsonl_file_path: str, event_class: Type[TimelineEvent]):
        """
        Initialize with JSONL file path and event class.
        
        Args:
            jsonl_file_path: Path to the JSONL correlation log file
            event_class: TimelineEvent subclass to reconstruct (OCRTimelineEvent, etc.)
        """
        self.jsonl_file_path = jsonl_file_path
        self.event_class = event_class
        self.timeline_data: List[TimelineEvent] = []
        self._loaded = False
        
        logger.info(f"SimpleJSONLReplaySource created for {jsonl_file_path} with {event_class.__name__}")
    
    def load_timeline_data(self) -> bool:
        """
        Load timeline data from JSONL file.
        
        Returns:
            bool: True if successful, False otherwise
        """
        self.timeline_data = []
        
        if not os.path.exists(self.jsonl_file_path):
            logger.error(f"JSONL file not found: {self.jsonl_file_path}")
            return False
        
        try:
            with open(self.jsonl_file_path, 'r') as f:
                for line_number, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                        
                    try:
                        log_entry = json.loads(line)
                        
                        # Use the from_correlation_log_entry method if available
                        if hasattr(self.event_class, 'from_correlation_log_entry'):
                            event = self.event_class.from_correlation_log_entry(log_entry)
                        else:
                            # Fallback to direct construction
                            event = self.event_class(**log_entry)
                        
                        self.timeline_data.append(event)
                        
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error at line {line_number} in {self.jsonl_file_path}: {e}")
                        continue
                    except Exception as e:
                        logger.error(f"Error parsing line {line_number} in {self.jsonl_file_path}: {e}")
                        continue
            
            # CRITICAL FIX: Sort by GSI and perf_counter_timestamp to ensure correct ordering
            self.timeline_data.sort(key=lambda x: (
                x.global_sequence_id if x.global_sequence_id is not None else float('inf'),
                x.perf_counter_timestamp if x.perf_counter_timestamp is not None else float('inf')
            ))
            
            self._loaded = True
            logger.info(f"Successfully loaded and sorted {len(self.timeline_data)} events from {self.jsonl_file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error loading JSONL file {self.jsonl_file_path}: {e}")
            return False
    
    def get_data_stream(self) -> Iterator[TimelineEvent]:
        """
        Get data stream for compatibility with existing interfaces.
        
        Returns:
            Iterator[TimelineEvent]: Iterator over loaded events
        """
        if not self._loaded:
            logger.warning(f"Timeline data not loaded for {self.jsonl_file_path}")
            return iter([])
        
        yield from self.timeline_data
    
    def is_active(self) -> bool:
        """Check if the replay source is active (always True for simple sources)."""
        return True
    
    def start(self) -> None:
        """Start the replay source (no-op for simple sources)."""
        pass
    
    def stop(self) -> None:
        """Stop the replay source (no-op for simple sources)."""
        pass


class SimpleOCRReplaySource(SimpleJSONLReplaySource):
    """Simple OCR replay source for JSONL files."""
    
    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, OCRTimelineEvent)


class SimplePriceReplaySource(SimpleJSONLReplaySource):
    """Simple Price replay source for JSONL files."""
    
    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, PriceTimelineEvent)


class SimpleBrokerReplaySource(SimpleJSONLReplaySource):
    """Simple Broker replay source for JSONL files."""
    
    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, BrokerTimelineEvent)


class SimpleProcessedImageReplaySource(SimpleJSONLReplaySource):
    """Simple ProcessedImage replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, ProcessedImageFrameTimelineEvent)


class SimpleConfigChangeReplaySource(SimpleJSONLReplaySource):
    """Simple Config Change replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, ConfigChangeTimelineEvent)


class SimpleTradingStateReplaySource(SimpleJSONLReplaySource):
    """Simple Trading State Change replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, TradingStateChangeTimelineEvent)


class SimpleTradeLifecycleReplaySource(SimpleJSONLReplaySource):
    """Simple Trade Lifecycle replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, TradeLifecycleTimelineEvent)


class SimpleSystemAlertReplaySource(SimpleJSONLReplaySource):
    """Simple System Alert replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, SystemAlertTimelineEvent)


class SimpleAccountSummaryReplaySource(SimpleJSONLReplaySource):
    """Simple Account Summary replay source for JSONL files."""

    def __init__(self, jsonl_file_path: str):
        super().__init__(jsonl_file_path, AccountSummaryTimelineEvent)


def create_replay_source_from_jsonl(jsonl_file_path: str) -> SimpleJSONLReplaySource:
    """
    Auto-detect the appropriate replay source type based on JSONL filename.
    
    Args:
        jsonl_file_path: Path to JSONL file
        
    Returns:
        SimpleJSONLReplaySource: Appropriate replay source instance
        
    Raises:
        ValueError: If file type cannot be determined
    """
    file_name = Path(jsonl_file_path).name.lower()

    if 'ocr' in file_name:
        return SimpleOCRReplaySource(jsonl_file_path)
    elif 'price' in file_name:
        return SimplePriceReplaySource(jsonl_file_path)
    elif 'broker' in file_name:
        return SimpleBrokerReplaySource(jsonl_file_path)
    elif 'visual_input_raw' in file_name or 'processed_image' in file_name:
        return SimpleProcessedImageReplaySource(jsonl_file_path)
    elif 'config_change' in file_name or 'config-change' in file_name:
        return SimpleConfigChangeReplaySource(jsonl_file_path)
    elif 'trading_state' in file_name or 'trading-state' in file_name:
        return SimpleTradingStateReplaySource(jsonl_file_path)
    elif 'trade_lifecycle' in file_name or 'trade-lifecycle' in file_name:
        return SimpleTradeLifecycleReplaySource(jsonl_file_path)
    elif 'system_alert' in file_name or 'system-alert' in file_name:
        return SimpleSystemAlertReplaySource(jsonl_file_path)
    elif 'account_summary' in file_name or 'account-summary' in file_name:
        return SimpleAccountSummaryReplaySource(jsonl_file_path)
    else:
        raise ValueError(f"Cannot determine replay source type for file: {jsonl_file_path}")


def load_all_replay_sources_from_directory(directory_path: str) -> Dict[str, SimpleJSONLReplaySource]:
    """
    Load all JSONL files from a directory as replay sources.
    
    Args:
        directory_path: Path to directory containing JSONL files
        
    Returns:
        Dict[str, SimpleJSONLReplaySource]: Map of source names to replay sources
    """
    replay_sources = {}
    
    for jsonl_file in Path(directory_path).glob("*.jsonl"):
        try:
            source = create_replay_source_from_jsonl(str(jsonl_file))
            if source.load_timeline_data():
                # Use the file stem as the source name
                source_name = jsonl_file.stem.replace('_correlation', '')
                replay_sources[source_name] = source
                logger.info(f"Loaded replay source '{source_name}' with {len(source.timeline_data)} events")
            else:
                logger.warning(f"Failed to load timeline data from {jsonl_file}")
        except Exception as e:
            logger.error(f"Error creating replay source for {jsonl_file}: {e}")
    
    return replay_sources


if __name__ == '__main__':
    print("🔧 Simple Replay Sources for JSONL Files")
    print("=" * 50)
    print("This module provides simple interfaces for creating replay sources")
    print("directly from JSONL correlation log files without complex configuration.")
    print("\nExample usage:")
    print("  source = SimpleOCRReplaySource('ocr_correlation.jsonl')")
    print("  source.load_timeline_data()")
    print("  events = list(source.get_data_stream())")
