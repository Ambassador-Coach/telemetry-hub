from dataclasses import dataclass, field
import time
from typing import Dict, Any
from uuid import uuid4

@dataclass
class BaseInternalEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: float = field(default_factory=time.time)

@dataclass
class CorrelationLogEntryEvent(BaseInternalEvent):
    """Event published by CorrelationLogger when a new entry is logged."""
    log_entry: Dict[str, Any] = field(default_factory=dict) # The full dictionary written to the .jsonl file
