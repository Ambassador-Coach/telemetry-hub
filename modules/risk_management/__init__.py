"""
Risk Management Module

This module provides risk assessment and management capabilities for the TESTRADE system.
It includes interfaces for risk assessment, market data processing, and the main
RiskManagementService implementation.

Components:
- IRiskManagementService: Interface for risk assessment services
- IMarketDataReceiver: Interface for receiving market data
- RiskManagementService: Main implementation of risk management logic
- RiskLevel: Enumeration of risk levels
- RiskAssessment: Data class for risk assessment results
"""

# Import interfaces first (they have fewer dependencies)
from interfaces.risk.services import (
    IRiskManagementService,
    IMarketDataReceiver,
    RiskLevel,
    RiskAssessment,
    CriticalRiskCallback
)

# Import implementation (may have more dependencies)
try:
    from .risk_service import RiskManagementService
except ImportError:
    # Handle potential circular import issues gracefully
    RiskManagementService = None

__all__ = [
    # Interfaces
    'IRiskManagementService',
    'IMarketDataReceiver',

    # Data Types
    'RiskLevel',
    'RiskAssessment',
    'CriticalRiskCallback',

    # Implementations
    'RiskManagementService'
]
