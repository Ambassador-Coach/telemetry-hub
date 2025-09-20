# modules/ocr/data_types.py
# 
# NOTE: OCRParsedData and OCRSnapshot have been moved to data_models/trading.py
# for centralized data model management. Import from data_models instead.
#
# This file contains OCR-specific data types and conditioning results.

import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List # Ensure all are imported
from utils.thread_safe_uuid import get_thread_safe_uuid

# Import from centralized location for backward compatibility
from data_models import OCRParsedData, OCRSnapshot

@dataclass
class CleanedOcrResult:
    """
    World-class cleaned OCR result with metadata/payload separation.
    
    This follows the standard TESTRADE event pattern:
    - metadata: All tracing, routing, and correlation data
    - payload fields: Business logic data (cleaned snapshots, timing data)
    """
    
    # --- METADATA BLOCK: Tracing, Routing, and Correlation Data ---
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # --- PAYLOAD: Business Logic Data ---
    
    # Core conditioning results
    conditioned_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    symbols_processed: int = 0
    lines_processed: int = 0
    
    # T4-T6 High-Precision Milestone Timestamps (nanoseconds)
    T4_ConditioningStart_ns: Optional[int] = None      # Conditioning pipeline start
    T5_ValidationComplete_ns: Optional[int] = None     # Validation and filtering complete
    T6_ConditioningComplete_ns: Optional[int] = None   # Final conditioning output ready
    
    # Conditioning metrics
    flickers_rejected: int = 0
    decimal_corrections_applied: int = 0
    validation_failures: int = 0
    
    # Parent event linkage
    parent_ocr_event_id: Optional[str] = None
    parent_correlation_id: Optional[str] = None
    
    # Legacy compatibility fields (will be deprecated)
    legacy_event_id: Optional[str] = field(default=None, init=False)
    legacy_correlation_id: Optional[str] = field(default=None, init=False)

    def __post_init__(self):
        """Ensure backward compatibility and metadata integrity."""
        # Ensure metadata has required fields
        if not self.metadata.get('eventId'):
            self.metadata['eventId'] = get_thread_safe_uuid()
        
        if not self.metadata.get('timestamp_ns'):
            self.metadata['timestamp_ns'] = time.perf_counter_ns()
        
        if not self.metadata.get('epoch_timestamp_s'):
            self.metadata['epoch_timestamp_s'] = time.time()
            
        if not self.metadata.get('eventType'):
            self.metadata['eventType'] = 'TESTRADE_OCR_CLEANED_RESULT'
            
        if not self.metadata.get('sourceComponent'):
            self.metadata['sourceComponent'] = 'PythonOCRDataConditioner'
        
        # Populate legacy fields for backward compatibility
        self.legacy_event_id = self.metadata.get('eventId')
        self.legacy_correlation_id = self.metadata.get('correlationId')

# Re-export for any existing imports
__all__ = ['OCRParsedData', 'OCRSnapshot', 'CleanedOcrResult']
