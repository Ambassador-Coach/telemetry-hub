# /modules/market_data_publishing/__init__.py
"""
Market Data Publishing Module

This module provides lean publishing services for pre-filtered market data.
The FilteredMarketDataPublisher assumes data has already been vetted for relevance
and publishes it using BulletproofBabysitterIPCClient for robust delivery.
"""

from .filtered_market_data_publisher import FilteredMarketDataPublisher

__all__ = ['FilteredMarketDataPublisher']
