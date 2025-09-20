"""
OCR Analysis Results

This module provides structured data classes for OCR validation and analysis results,
enabling comprehensive comparison of interpreted vs expected position data and
performance metrics for OCR processing pipelines.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

# Assuming Position type is available from core.types if needed for typed positions
# from intellisense.core.types import Position


@dataclass
class OCRValidationResult:
    """
    Result of validating OCR interpretation against expected results.
    
    This class provides comprehensive comparison between interpreted and expected
    position data, including accuracy scoring and detailed discrepancy tracking.
    """
    is_valid: bool
    accuracy_score: float  # 0.0 to 1.0
    discrepancies: List[str] = field(default_factory=list)
    
    # Key: typically f"{symbol}_qty" or f"{symbol}_price"
    # Value: True if matched, False if mismatched or symbol missing in interpretation
    position_matches: Dict[str, bool] = field(default_factory=dict)
    
    # Fields to store the actual compared values for easier debugging
    interpreted_values: Dict[str, Any] = field(default_factory=dict)
    expected_values: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def compare_positions(cls, interpreted_position_dict: Optional[Dict[str, Dict[str, Any]]],
                          expected_position_dict: Optional[Dict[str, Dict[str, Any]]]) -> 'OCRValidationResult':
        # ... (Full implementation from Chunk 10)
        discrepancies = []; position_matches = {}; interpreted_vals_log = {}; expected_vals_log = {}
        if interpreted_position_dict is None: interpreted_position_dict = {}
        if expected_position_dict is None: expected_position_dict = {}
        all_symbols = set(expected_position_dict.keys()) | set(interpreted_position_dict.keys())
        if not all_symbols: return cls(is_valid=True, accuracy_score=1.0, interpreted_values={}, expected_values={})
        total_possible_matches = 0; actual_matches = 0
        for symbol in all_symbols:
            expected_data = expected_position_dict.get(symbol); interpreted_data = interpreted_position_dict.get(symbol)
            expected_vals_log[symbol] = expected_data or "NOT_EXPECTED"; interpreted_vals_log[symbol] = interpreted_data or "NOT_INTERPRETED"
            if expected_data is None: discrepancies.append(f"Unexpected: {symbol}"); position_matches[f"{symbol}_presence"]=False; total_possible_matches+=1; continue
            if interpreted_data is None: discrepancies.append(f"Missing: {symbol}"); position_matches[f"{symbol}_presence"]=False; total_possible_matches+=1; continue
            expected_qty=float(expected_data.get('shares',expected_data.get('qty',0.0))); interpreted_qty=float(interpreted_data.get('shares',interpreted_data.get('qty',0.0)))
            qty_match=abs(expected_qty-interpreted_qty)<0.01; position_matches[f"{symbol}_qty"]=qty_match; total_possible_matches+=1
            if qty_match: actual_matches+=1
            else: discrepancies.append(f"{symbol} qty: exp {expected_qty}, got {interpreted_qty}")
            if abs(expected_qty)>0.001 and 'avg_price' in expected_data and 'avg_price' in interpreted_data:
                expected_price=float(expected_data['avg_price']); interpreted_price=float(interpreted_data['avg_price'])
                price_match=abs(expected_price-interpreted_price)<0.0001; position_matches[f"{symbol}_price"]=price_match; total_possible_matches+=1
                if price_match: actual_matches+=1
                else: discrepancies.append(f"{symbol} price: exp {expected_price:.4f}, got {interpreted_price:.4f}")
        accuracy_score = (actual_matches / total_possible_matches) if total_possible_matches > 0 else 1.0
        return cls(is_valid=len(discrepancies)==0, accuracy_score=accuracy_score, discrepancies=discrepancies,
                   position_matches=position_matches, interpreted_values=interpreted_vals_log, expected_values=expected_vals_log)

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the validation result."""
        return {
            'is_valid': self.is_valid,
            'accuracy_score': self.accuracy_score,
            'total_discrepancies': len(self.discrepancies),
            'total_comparisons': len(self.position_matches),
            'successful_matches': sum(1 for match in self.position_matches.values() if match),
            'symbols_compared': len(set(key.split('_')[0] for key in self.position_matches.keys()))
        }


@dataclass
class OCRAnalysisResult:
    """Result of OCR intelligence analysis for a single OCRTimelineEvent, enhanced for real service integration."""
    frame_number: int
    epoch_timestamp_s: float
    perf_counter_timestamp: Optional[float]
    analysis_completed_timestamp: float = field(default_factory=time.time)
    original_ocr_event_global_sequence_id: Optional[int] = None

    # Performance metrics
    captured_processing_latency_ns: Optional[int] = None # From live capture log

    # Real service performance metrics (NEW for Chunk 11)
    context_retrieval_latency_ns: Optional[int] = None  # Time to get PositionManager context
    interpreter_latency_ns: Optional[int] = None      # Time for SnapshotInterpreter.interpret_snapshot

    # Analysis data
    interpreted_positions_dict: Optional[Dict[str, Dict[str, Any]]] = None # Output from real interpreter
    expected_positions_dict: Optional[Dict[str, Dict[str, Any]]] = None  # Ground truth from event
    position_context: Optional[Dict[str, Any]] = None # Context from real PositionManager (NEW for Chunk 11)

    validation_result: Optional[OCRValidationResult] = None

    # Metadata (NEW for Chunk 11)
    service_versions: Optional[Dict[str, str]] = None # Versions of real services used

    # Error handling
    analysis_error: Optional[str] = None
    diagnostic_info: Optional[Dict[str, Any]] = None # For richer error context (NEW for Chunk 11)
    
    @property
    def is_successful(self) -> bool:
        return self.analysis_error is None

    # performance_improvement_ns property removed as its meaning was nuanced.
    # Individual latencies are more direct.

    @property
    def has_validation_result(self) -> bool:
        """Check if validation result is available."""
        return self.validation_result is not None

    @property
    def is_interpretation_successful(self) -> bool:
        """Check if position interpretation was successful."""
        return self.interpreted_positions_dict is not None

    @property
    def has_expected_data(self) -> bool:
        """Check if expected position data is available for comparison."""
        return self.expected_positions_dict is not None

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get a summary of performance metrics."""
        summary = {
            'captured_processing_latency_ms': None,
            'context_retrieval_latency_ms': None,
            'interpreter_latency_ms': None,
            'has_performance_data': False
        }

        if self.captured_processing_latency_ns is not None:
            summary['captured_processing_latency_ms'] = self.captured_processing_latency_ns / 1_000_000
            summary['has_performance_data'] = True

        if self.context_retrieval_latency_ns is not None:
            summary['context_retrieval_latency_ms'] = self.context_retrieval_latency_ns / 1_000_000
            summary['has_performance_data'] = True

        if self.interpreter_latency_ns is not None:
            summary['interpreter_latency_ms'] = self.interpreter_latency_ns / 1_000_000
            summary['has_performance_data'] = True

        return summary

    def get_analysis_summary(self) -> Dict[str, Any]:
        """Get a comprehensive summary of the analysis result."""
        summary = {
            'frame_number': self.frame_number,
            'epoch_timestamp_s': self.epoch_timestamp_s,
            'analysis_completed_timestamp': self.analysis_completed_timestamp,
            'is_successful': self.is_successful,
            'has_interpretation': self.is_interpretation_successful,
            'has_expected_data': self.has_expected_data,
            'has_validation': self.has_validation_result,
            'analysis_error': self.analysis_error
        }
        
        # Add performance summary
        summary.update(self.get_performance_summary())
        
        # Add validation summary if available
        if self.validation_result:
            summary['validation'] = self.validation_result.get_summary()
        
        # Add position counts
        if self.interpreted_positions_dict:
            summary['interpreted_symbols_count'] = len(self.interpreted_positions_dict)
        
        if self.expected_positions_dict:
            summary['expected_symbols_count'] = len(self.expected_positions_dict)
        
        return summary

    def create_validation_result(self) -> Optional[OCRValidationResult]:
        """
        Create and store validation result by comparing interpreted vs expected positions.
        
        Returns:
            OCRValidationResult if both interpreted and expected data are available, None otherwise
        """
        if self.interpreted_positions_dict is not None or self.expected_positions_dict is not None:
            self.validation_result = OCRValidationResult.compare_positions(
                self.interpreted_positions_dict,
                self.expected_positions_dict
            )
            return self.validation_result
        return None
