# /modules/active_symbols/__init__.py
"""
Active Symbols Service Module

This module provides centralized management of symbols that are currently relevant
for filtered data streaming, either due to active positions or pending open orders.
"""

from .active_symbols_service import ActiveSymbolsService

__all__ = ['ActiveSymbolsService']
