"""
Symbol Loading Service

This service manages the loading and caching of tradeable symbols from CSV files.
It provides a clean interface for symbol management within the DI architecture.
"""

import csv
import os
import time
import logging
import threading
from typing import Set, Optional, Dict, Any
from pathlib import Path

from interfaces.core.services import ILifecycleService, IConfigService

logger = logging.getLogger(__name__)


class SymbolLoadingService(ILifecycleService):
    """
    Service responsible for loading and managing tradeable symbols.
    
    This service replaces the global symbol_loader utility with a proper
    DI-managed service that can be injected into other services.
    """
    
    def __init__(self, config_service: IConfigService):
        """
        Initialize the Symbol Loading Service.
        
        Args:
            config_service: Configuration service for getting symbol file paths
        """
        self.logger = logger
        self.config = config_service
        
        # Symbol storage
        self._tradeable_symbols: Set[str] = set()
        self._non_tradeable_symbols: Set[str] = set()
        self._symbols_loaded = False
        self._loader_lock = threading.Lock()
        
        # Service state
        self._is_running = False
        
        # Metadata
        self._last_loaded_path: Optional[str] = None
        self._last_loaded_timestamp: float = 0
        self._load_count = 0
        
    @property
    def is_ready(self) -> bool:
        """Check if the symbol loading service is ready."""
        return self._is_running and self._symbols_loaded
        
    @property
    def tradeable_symbols(self) -> Set[str]:
        """Get the set of tradeable symbols."""
        self._ensure_symbols_loaded()
        return self._tradeable_symbols.copy()
        
    @property
    def non_tradeable_symbols(self) -> Set[str]:
        """Get the set of non-tradeable symbols."""
        self._ensure_symbols_loaded()
        return self._non_tradeable_symbols.copy()
        
    def start(self) -> None:
        """Start the symbol loading service."""
        if self._is_running:
            self.logger.warning("SymbolLoadingService already running")
            return
            
        self._is_running = True
        
        # Load symbols on startup
        self._load_symbols_from_config()
        
        self.logger.info(f"SymbolLoadingService started. Loaded {len(self._tradeable_symbols)} tradeable symbols.")
        
    def stop(self) -> None:
        """Stop the symbol loading service."""
        if not self._is_running:
            return
            
        self._is_running = False
        self.logger.info("SymbolLoadingService stopped.")
        
    def _ensure_symbols_loaded(self) -> None:
        """Ensure symbols are loaded, loading them if necessary."""
        if not self._symbols_loaded:
            self._load_symbols_from_config()
            
    def _load_symbols_from_config(self) -> None:
        """Load symbols based on configuration."""
        # Get symbol file path from config
        symbol_file_path = getattr(self.config, 'SYMBOL_CSV_PATH', None)
        
        if not symbol_file_path:
            # Use default path
            default_path = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'tradeable_symbols.csv')
            symbol_file_path = os.path.abspath(default_path)
            
        self.load_symbols_from_csv(symbol_file_path, fix_bom=True, force_reload=True)
        
    def load_symbols_from_csv(self, csv_file_path: str, fix_bom: bool = True, force_reload: bool = False) -> bool:
        """
        Load symbols from a CSV file.
        
        Args:
            csv_file_path: Path to the CSV file
            fix_bom: Whether to fix BOM encoding issues
            force_reload: Whether to force reload even if already loaded
            
        Returns:
            True if symbols were loaded successfully, False otherwise
        """
        with self._loader_lock:
            if self._symbols_loaded and not force_reload:
                self.logger.debug("Symbols already loaded and force_reload=False")
                return True
                
            self.logger.info(f"Loading symbols from: {csv_file_path}")
            
            try:
                if not os.path.exists(csv_file_path):
                    self.logger.error(f"Symbol CSV file not found: {csv_file_path}")
                    return False
                    
                tradeable_symbols = set()
                non_tradeable_symbols = set()
                
                # Read CSV file
                with open(csv_file_path, 'r', encoding='utf-8-sig' if fix_bom else 'utf-8') as file:
                    csv_reader = csv.DictReader(file)
                    
                    for row_num, row in enumerate(csv_reader, start=2):  # Start at 2 since header is row 1
                        try:
                            # Extract symbol and tradeable status
                            symbol = row.get('Symbol', '').strip().upper()
                            # Try both 'TRADEABLE' and 'Tradeable' column names for compatibility
                            tradeable_str = (row.get('TRADEABLE', '') or row.get('Tradeable', '')).strip().lower()
                            
                            if not symbol:
                                self.logger.warning(f"Empty symbol in row {row_num}, skipping")
                                continue
                                
                            # Validate symbol format
                            if not self._is_valid_symbol(symbol):
                                self.logger.warning(f"Invalid symbol format '{symbol}' in row {row_num}, skipping")
                                continue
                                
                            # Determine if tradeable
                            is_tradeable = tradeable_str in ['true', 'yes', '1', 'y']
                            
                            if is_tradeable:
                                tradeable_symbols.add(symbol)
                            else:
                                non_tradeable_symbols.add(symbol)
                                
                        except Exception as e:
                            self.logger.error(f"Error processing row {row_num}: {e}")
                            continue
                            
                # Update symbol sets
                self._tradeable_symbols = tradeable_symbols
                self._non_tradeable_symbols = non_tradeable_symbols
                self._symbols_loaded = True
                self._last_loaded_path = csv_file_path
                self._last_loaded_timestamp = time.time()
                self._load_count += 1
                
                self.logger.info(f"Successfully loaded {len(tradeable_symbols)} tradeable symbols and {len(non_tradeable_symbols)} non-tradeable symbols")
                
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to load symbols from {csv_file_path}: {e}", exc_info=True)
                return False
                
    def _is_valid_symbol(self, symbol: str) -> bool:
        """
        Validate symbol format.
        
        Args:
            symbol: Symbol to validate
            
        Returns:
            True if symbol is valid, False otherwise
        """
        # Basic validation: 1-5 characters, letters only
        if not symbol or len(symbol) < 1 or len(symbol) > 5:
            return False
            
        if not symbol.isalpha():
            return False
            
        return True
        
    def is_symbol_tradeable(self, symbol: str) -> bool:
        """
        Check if a symbol is tradeable.
        
        Args:
            symbol: Symbol to check
            
        Returns:
            True if symbol is tradeable, False otherwise
        """
        self._ensure_symbols_loaded()
        return symbol.upper() in self._tradeable_symbols
        
    def get_symbol_stats(self) -> Dict[str, Any]:
        """
        Get statistics about loaded symbols.
        
        Returns:
            Dictionary with symbol loading statistics
        """
        self._ensure_symbols_loaded()
        
        return {
            'tradeable_count': len(self._tradeable_symbols),
            'non_tradeable_count': len(self._non_tradeable_symbols),
            'total_count': len(self._tradeable_symbols) + len(self._non_tradeable_symbols),
            'last_loaded_path': self._last_loaded_path,
            'last_loaded_timestamp': self._last_loaded_timestamp,
            'load_count': self._load_count,
            'symbols_loaded': self._symbols_loaded
        }
        
    def reload_symbols(self) -> bool:
        """
        Reload symbols from the configured source.
        
        Returns:
            True if reload was successful, False otherwise
        """
        self.logger.info("Reloading symbols...")
        return self._load_symbols_from_config()