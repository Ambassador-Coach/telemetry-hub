"""
Correlation Logger for End-to-End Trade Tracking

This module provides centralized logging for correlation ID flow across TESTRADE components.
It helps track the IntelliSense UUID from OCR through to GUI display.
"""

import logging
import time
from typing import Optional, Dict, Any

# Create dedicated correlation logger
correlation_logger = logging.getLogger("testrade.correlation")

def log_correlation_flow(trade_id: Optional[str], correlation_id: Optional[str], source: str, 
                        symbol: Optional[str] = None, additional_data: Optional[Dict[str, Any]] = None):
    """
    Log correlation ID flow for end-to-end tracking.
    
    Args:
        trade_id: The trade ID (LCM t_id or position UUID)
        correlation_id: The master correlation ID (IntelliSense UUID)
        source: Source component (e.g., "OCR", "PositionManager", "GUI", "TradeLifecycle")
        symbol: Trading symbol if applicable
        additional_data: Additional context data
    """
    timestamp = time.time()
    
    # Build log message
    log_parts = [
        f"CORRELATION_FLOW",
        f"source={source}",
        f"trade_id={trade_id or 'None'}",
        f"correlation_id={correlation_id or 'None'}",
        f"timestamp={timestamp}"
    ]
    
    if symbol:
        log_parts.append(f"symbol={symbol}")
    
    if additional_data:
        for key, value in additional_data.items():
            log_parts.append(f"{key}={value}")
    
    log_message = " | ".join(log_parts)
    correlation_logger.info(log_message)

def log_correlation_gap(expected_source: str, missing_field: str, context: Dict[str, Any]):
    """
    Log when correlation ID is missing or broken in the flow.
    
    Args:
        expected_source: Where the correlation ID should have come from
        missing_field: Which field is missing (e.g., "master_correlation_id", "trade_id")
        context: Context information for debugging
    """
    timestamp = time.time()
    
    log_parts = [
        f"CORRELATION_GAP",
        f"expected_source={expected_source}",
        f"missing_field={missing_field}",
        f"timestamp={timestamp}"
    ]
    
    for key, value in context.items():
        log_parts.append(f"{key}={value}")
    
    log_message = " | ".join(log_parts)
    correlation_logger.warning(log_message)

def log_correlation_success(trade_id: str, correlation_id: str, flow_path: str):
    """
    Log successful end-to-end correlation tracking.
    
    Args:
        trade_id: The final trade ID displayed in GUI
        correlation_id: The original IntelliSense UUID from OCR
        flow_path: Description of the successful flow path
    """
    timestamp = time.time()
    
    log_message = (
        f"CORRELATION_SUCCESS | "
        f"trade_id={trade_id} | "
        f"correlation_id={correlation_id} | "
        f"flow_path={flow_path} | "
        f"timestamp={timestamp}"
    )
    
    correlation_logger.info(log_message)

# Convenience functions for specific components
def log_ocr_correlation(event_id: str, symbol: str, frame_timestamp: float):
    """Log correlation ID creation in OCR component"""
    log_correlation_flow(
        trade_id=None,
        correlation_id=event_id,
        source="OCR",
        symbol=symbol,
        additional_data={"frame_timestamp": frame_timestamp}
    )

def log_position_correlation(position_uuid: str, lcm_trade_id: Optional[str], 
                           master_correlation_id: Optional[str], symbol: str):
    """Log correlation ID in PositionManager"""
    log_correlation_flow(
        trade_id=position_uuid,
        correlation_id=master_correlation_id,
        source="PositionManager",
        symbol=symbol,
        additional_data={"lcm_trade_id": lcm_trade_id}
    )

def log_trade_lifecycle_correlation(trade_id: str, correlation_id: Optional[str], 
                                  symbol: str, origin: str):
    """Log correlation ID in TradeLifecycleManager"""
    log_correlation_flow(
        trade_id=trade_id,
        correlation_id=correlation_id,
        source="TradeLifecycleManager",
        symbol=symbol,
        additional_data={"origin": origin}
    )

def log_gui_correlation(trade_id: str, parent_trade_id: Optional[str], 
                       symbol: str, is_parent: bool):
    """Log correlation ID in GUI display"""
    log_correlation_flow(
        trade_id=trade_id,
        correlation_id=None,  # GUI doesn't typically have direct access to correlation_id
        source="GUI",
        symbol=symbol,
        additional_data={
            "parent_trade_id": parent_trade_id,
            "is_parent": is_parent
        }
    )
