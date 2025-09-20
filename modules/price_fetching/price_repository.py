# modules/price_fetching/price_repository.py
import threading
import logging
import time
import uuid
import asyncio
import json
from typing import Dict, Optional, Set, Any, List

# Import for thread-safe UUID generation  
from utils.thread_safe_uuid import get_thread_safe_uuid

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from alpaca_trade_api.rest_async import AsyncRest
except ImportError:
    AsyncRest = None

from interfaces.data.services import IPriceProvider
from data_models.pricing import PriceData, StalePriceError, PriceUnavailableError
from utils.global_config import GlobalConfig

logger = logging.getLogger(__name__)

class PriceRepository(IPriceProvider):
    """
    JIT Price Oracle: A high-performance, thread-safe repository that provides
    the freshest price data via a unified cache and a non-blocking asyncio-based
    REST API fallback mechanism.
    """

    def __init__(self, config_service: GlobalConfig, rest_client: Optional[Any] = None, **kwargs):
        self.logger = logger
        self._config = config_service
        self._rest_client = rest_client # The synchronous client used to get headers/URL

        self._price_cache: Dict[str, PriceData] = {}
        self._cache_lock = threading.RLock()

        self._api_requests_in_flight: Set[str] = set()
        self._in_flight_lock = threading.Lock()
        
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_thread: Optional[threading.Thread] = None
        self._api_session: Optional[aiohttp.ClientSession] = None
        self._is_stopped = False
        
        self.logger.info("JIT Price Oracle (PriceRepository) initialized.")

    # --- Lifecycle Methods ---
    def start(self):
        if not (aiohttp and self._rest_client):
            self.logger.warning("aiohttp or REST client not available, async API fallback disabled.")
            return

        self._is_stopped = False
        self._async_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_loop_in_thread, daemon=True, name="PriceRepo-AsyncIO")
        self._async_thread.start()

    def stop(self):
        # Prevent double-stop
        if self._is_stopped:
            self.logger.debug("PriceRepository already stopped, skipping")
            return
            
        self._is_stopped = True
        
        if self._async_loop:
            try:
                # Check if the loop is still running before trying to stop it
                if self._async_loop.is_running():
                    self._async_loop.call_soon_threadsafe(self._async_loop.stop)
            except RuntimeError as e:
                # Event loop is already closed, which is fine
                self.logger.debug(f"Event loop already closed during stop: {e}")
            
            if self._async_thread and self._async_thread.is_alive():
                self._async_thread.join(timeout=2.0)
                if self._async_thread.is_alive():
                    self.logger.warning("Async thread did not stop within timeout")
            
            # Clear references after stopping
            self._async_loop = None
            self._async_thread = None
        
        self.logger.info("PriceRepository stopped successfully")

    @property
    def is_ready(self) -> bool:
        return self._api_session is not None and not self._api_session.closed

    # --- Data Ingress ---
    def on_quote(self, symbol: str, bid: float, ask: float, bid_size: int, ask_size: int,
                 timestamp: float, conditions: Optional[List[str]] = None, **kwargs):
        if ask > 0: 
            self._update_cache(symbol, ask, "stream_ask")

    def on_trade(self, symbol: str, price: float, size: int, timestamp: float, 
                 conditions: Optional[List[str]] = None, **kwargs):
        self._update_cache(symbol, price, "stream_trade", if_newer=True)

    # --- Data Egress ---
    def get_latest_price(self, symbol: str, max_age_ns: int) -> PriceData:
        symbol_upper = symbol.upper()
        with self._cache_lock:
            cached_data = self._price_cache.get(symbol_upper)
        if cached_data and (time.perf_counter_ns() - cached_data.timestamp_ns <= max_age_ns):
            return cached_data
        raise StalePriceError(f"Price for {symbol_upper} is stale or unavailable in cache.")

    def get_price_data(self, symbol: str, max_age_ns: int, correlation_id: Optional[str] = None, 
                      trigger_background_refresh: bool = False) -> PriceData:
        """
        JIT Price Oracle cache access with comprehensive instrumentation.
        
        Args:
            symbol: Stock symbol to fetch
            max_age_ns: Maximum acceptable age in nanoseconds 
            correlation_id: Correlation ID for traceability
            trigger_background_refresh: If True, trigger non-blocking background refresh when stale
        
        Returns:
            PriceData: Fresh price data from cache
            
        Raises:
            StalePriceError: If price is stale or unavailable
            PriceUnavailableError: If symbol not found
        """
        # Generate correlation ID if not provided
        if correlation_id is None:
            correlation_id = get_thread_safe_uuid()
            
        symbol_upper = symbol.upper()
        
        # PriceRequest_CacheCheck_ns: Start cache lookup timing
        cache_check_start_ns = time.perf_counter_ns()
        
        with self._cache_lock:
            cached_data = self._price_cache.get(symbol_upper)
            cache_check_end_ns = time.perf_counter_ns()
            
        cache_lookup_latency_us = (cache_check_end_ns - cache_check_start_ns) / 1000
        current_time_ns = time.perf_counter_ns()
        
        if cached_data:
            age_ns = current_time_ns - cached_data.timestamp_ns
            age_ms = age_ns / 1_000_000
            
            if age_ns <= max_age_ns:
                # CACHE HIT - Fresh data available
                self.logger.info(
                    f"PriceOracle_CacheHit: Symbol={symbol_upper}, "
                    f"CacheLatency_us={cache_lookup_latency_us:.1f}, "
                    f"Age_ms={age_ms:.1f}, MaxAge_ms={max_age_ns/1_000_000:.1f}, "
                    f"Price=${cached_data.price:.4f}, Source={cached_data.source}, "
                    f"CorrelationId={correlation_id}"
                )
                return cached_data
            else:
                # CACHE STALE - Data exists but too old
                self.logger.warning(
                    f"PriceOracle_CacheStale: Symbol={symbol_upper}, "
                    f"CacheLatency_us={cache_lookup_latency_us:.1f}, "
                    f"Age_ms={age_ms:.1f}, MaxAge_ms={max_age_ns/1_000_000:.1f}, "
                    f"Price=${cached_data.price:.4f}, Source={cached_data.source}, "
                    f"CorrelationId={correlation_id}"
                )
                
                # Trigger background refresh if requested
                if trigger_background_refresh:
                    self._trigger_background_refresh(symbol_upper, correlation_id)
                    
                raise StalePriceError(f"Price for {symbol_upper} is stale (age: {age_ms:.1f}ms, max: {max_age_ns/1_000_000:.1f}ms)")
        else:
            # CACHE MISS - No data available
            self.logger.warning(
                f"PriceOracle_CacheMiss: Symbol={symbol_upper}, "
                f"CacheLatency_us={cache_lookup_latency_us:.1f}, "
                f"CorrelationId={correlation_id}"
            )
            
            # Trigger background refresh if requested
            if trigger_background_refresh:
                self._trigger_background_refresh(symbol_upper, correlation_id)
                
            raise PriceUnavailableError(f"Price for {symbol_upper} not available in cache")
            
    def _trigger_background_refresh(self, symbol: str, correlation_id: str):
        """Trigger non-blocking background API refresh for a symbol."""
        if not (self._async_loop and self._async_loop.is_running()):
            self.logger.warning(f"Cannot trigger background refresh for {symbol}: async loop not running. CorrelationId={correlation_id}")
            return
            
        with self._in_flight_lock:
            if symbol in self._api_requests_in_flight:
                self.logger.debug(f"Background refresh already in flight for {symbol}. CorrelationId={correlation_id}")
                return
            else:
                self._api_requests_in_flight.add(symbol)
        
        # Fire-and-forget async refresh
        coro = self._fetch_price_async(symbol, correlation_id)
        future = asyncio.run_coroutine_threadsafe(coro, self._async_loop)
        
        def cleanup_on_completion(fut):
            with self._in_flight_lock:
                self._api_requests_in_flight.discard(symbol)
            try:
                price_data = fut.result()
                if price_data:
                    self._update_cache(symbol, price_data.price, price_data.source)
                    self.logger.debug(f"Background refresh completed for {symbol}. CorrelationId={correlation_id}")
                else:
                    self.logger.warning(f"Background refresh failed for {symbol}: no data returned. CorrelationId={correlation_id}")
            except Exception as e:
                self.logger.error(f"Background refresh error for {symbol}: {e}. CorrelationId={correlation_id}")
                
        future.add_done_callback(cleanup_on_completion)

    def get_price_blocking(self, symbol: str, timeout_sec: float = 2.0, correlation_id: Optional[str] = None) -> PriceData:
        # Generate correlation ID if not provided
        if correlation_id is None:
            correlation_id = get_thread_safe_uuid()
            
        try:
            return self.get_latest_price(symbol, max_age_ns=100_000_000) # Check cache with 100ms tolerance first
        except (StalePriceError, PriceUnavailableError):
            self.logger.warning(f"CRITICAL FETCH for {symbol}: Cache failed. Blocking for API response. CorrelationId={correlation_id}")
            return self._trigger_and_wait_for_api_refresh(symbol, timeout_sec, correlation_id)

    # --- Internal Logic ---
    def _run_async_loop_in_thread(self):
        asyncio.set_event_loop(self._async_loop)
        try:
            self._async_loop.run_until_complete(self._create_session())
            self._async_loop.run_forever()
        finally:
            self._async_loop.run_until_complete(self._close_session())
            self._async_loop.close()

    async def _create_session(self):
        if self._rest_client and aiohttp:
            # Create session with auth headers like the backup did
            headers = {
                'APCA-API-KEY-ID': self._rest_client._key_id,
                'APCA-API-SECRET-KEY': self._rest_client._secret_key
            }
            
            self._api_session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5.0)
            )
            self.logger.info("aiohttp session created with Alpaca authentication headers.")

    async def _close_session(self):
        if self._api_session:
            await self._api_session.close()
            self._api_session = None

    def _trigger_and_wait_for_api_refresh(self, symbol: str, timeout_sec: float, correlation_id: str) -> PriceData:
        with self._in_flight_lock:
            if symbol in self._api_requests_in_flight:
                self.logger.warning(f"Blocking fetch for {symbol} waiting on an already in-flight request. CorrelationId={correlation_id}")
            else:
                self._api_requests_in_flight.add(symbol)

        if not (self._async_loop and self._async_loop.is_running()):
            raise PriceUnavailableError("Async I/O loop is not running.")

        coro = self._fetch_price_async(symbol, correlation_id)
        future = asyncio.run_coroutine_threadsafe(coro, self._async_loop)

        try:
            price_data = future.result(timeout=timeout_sec)
            if not price_data:
                raise PriceUnavailableError(f"API returned no valid data for {symbol}.")
            
            self._update_cache(symbol, price_data.price, price_data.source)
            return price_data
        except asyncio.TimeoutError:
            raise StalePriceError(f"API call for {symbol} timed out after {timeout_sec}s.")
        finally:
            with self._in_flight_lock:
                self._api_requests_in_flight.discard(symbol)
                
    async def _fetch_price_async(self, symbol: str, correlation_id: Optional[str] = None) -> Optional[PriceData]:
        """
        Fetch price from Alpaca REST API asynchronously with comprehensive instrumentation.
        
        Args:
            symbol: Stock symbol to fetch
            correlation_id: Correlation ID for traceability
        
        Returns:
            Optional[PriceData]: Price data if successful, None if failed
        """
        # Generate correlation ID if not provided
        if correlation_id is None:
            correlation_id = get_thread_safe_uuid()
            
        if not self._api_session:
            self.logger.warning(f"ApiFetch_NoSession: Symbol={symbol}, CorrelationId={correlation_id}")
            return None
            
        symbol_upper = symbol.upper()
        timeout = aiohttp.ClientTimeout(total=2.0)
        
        # ApiFetch_Start_ns: Begin API call timing
        api_fetch_start_ns = time.perf_counter_ns()
        
        try:
            # Use the DATA API URL, not the trading API URL
            # Trading API is for account/orders, Data API is for market data
            base_url = getattr(self._config, 'ALPACA_DATA_URL', 'https://data.alpaca.markets')
            
            # Try latest trade first (exactly like backup)
            trade_url = f"{base_url}/v2/stocks/{symbol_upper}/trades/latest"
            
            trade_start_ns = time.perf_counter_ns()
            async with self._api_session.get(trade_url, timeout=timeout) as response:
                trade_end_ns = time.perf_counter_ns()
                trade_latency_ms = (trade_end_ns - trade_start_ns) / 1_000_000
                
                if response.status == 200:
                    data = await response.json()
                    if 'trade' in data and 'p' in data['trade']:
                        price = float(data['trade']['p'])
                        if price > 0:
                            # ApiFetch_End_ns: Successful trade API response
                            api_fetch_end_ns = time.perf_counter_ns()
                            total_api_latency_ms = (api_fetch_end_ns - api_fetch_start_ns) / 1_000_000
                            
                            self.logger.info(
                                f"ApiFetch_Success: Symbol={symbol_upper}, "
                                f"Source=trade_api, Price=${price:.4f}, "
                                f"TradeLatency_ms={trade_latency_ms:.1f}, "
                                f"TotalLatency_ms={total_api_latency_ms:.1f}, "
                                f"CorrelationId={correlation_id}"
                            )
                            
                            return PriceData(
                                symbol=symbol_upper,
                                price=price,
                                timestamp_ns=api_fetch_end_ns,
                                source="async_rest_trade"
                            )
                ### --- MODIFICATION START --- ###
                elif response.status in [404, 422]:
                    self.logger.warning(f"ApiFetch_SymbolNotFound: Symbol '{symbol_upper}' not found via trade API (status {response.status}). This is expected for invalid symbols. CorrelationId={correlation_id}")
                    # This indicates the symbol does not exist or has no trades. This is not an error, it's a result.
                    # We will return None at the end of the function.
                ### --- MODIFICATION END --- ###
                else:
                    self.logger.debug(
                        f"ApiFetch_TradeFailure: Symbol={symbol_upper}, "
                        f"Status={response.status}, Latency_ms={trade_latency_ms:.1f}, "
                        f"CorrelationId={correlation_id}"
                    )
            
            # If trade failed, try latest quote (exactly like backup)
            quote_url = f"{base_url}/v2/stocks/{symbol_upper}/quotes/latest"
            
            quote_start_ns = time.perf_counter_ns()
            async with self._api_session.get(quote_url, timeout=timeout) as response:
                quote_end_ns = time.perf_counter_ns()
                quote_latency_ms = (quote_end_ns - quote_start_ns) / 1_000_000
                
                if response.status == 200:
                    data = await response.json()
                    if 'quote' in data and 'ap' in data['quote']:
                        price = float(data['quote']['ap'])  # ask price
                        if price > 0:
                            # ApiFetch_End_ns: Successful quote API response
                            api_fetch_end_ns = time.perf_counter_ns()
                            total_api_latency_ms = (api_fetch_end_ns - api_fetch_start_ns) / 1_000_000
                            
                            self.logger.info(
                                f"ApiFetch_Success: Symbol={symbol_upper}, "
                                f"Source=quote_api, Price=${price:.4f}, "
                                f"QuoteLatency_ms={quote_latency_ms:.1f}, "
                                f"TotalLatency_ms={total_api_latency_ms:.1f}, "
                                f"CorrelationId={correlation_id}"
                            )
                            
                            return PriceData(
                                symbol=symbol_upper,
                                price=price,
                                timestamp_ns=api_fetch_end_ns,
                                source="async_rest_ask"
                            )
                ### --- MODIFICATION START --- ###
                elif response.status in [404, 422]:
                    self.logger.warning(f"ApiFetch_SymbolNotFound: Symbol '{symbol_upper}' not found via quote API (status {response.status}). Concluding symbol is invalid or untradeable. CorrelationId={correlation_id}")
                    return None # Explicitly return None for invalid symbol
                ### --- MODIFICATION END --- ###
                else:
                    self.logger.debug(
                        f"ApiFetch_QuoteFailure: Symbol={symbol_upper}, "
                        f"Status={response.status}, Latency_ms={quote_latency_ms:.1f}, "
                        f"CorrelationId={correlation_id}"
                    )
                    
            # ApiFetch_End_ns: Failed - both trade and quote failed
            api_fetch_end_ns = time.perf_counter_ns()
            total_api_latency_ms = (api_fetch_end_ns - api_fetch_start_ns) / 1_000_000
            
            self.logger.warning(f"ApiFetch_NoData: Could not retrieve any price data for {symbol_upper} from API. CorrelationId={correlation_id}")
                    
        except asyncio.TimeoutError:
            self.logger.error(f"ApiFetch_Error: Timeout fetching price for {symbol_upper}. CorrelationId={correlation_id}")
        except Exception as e:
            self.logger.error(f"ApiFetch_Error: Unhandled exception for {symbol_upper}: {e}. CorrelationId={correlation_id}", exc_info=True)
            
        return None

    def _update_cache(self, symbol: str, price: float, source: str, if_newer: bool = False):
        """Update the price cache with thread safety."""
        symbol_upper = symbol.upper()
        timestamp_ns = time.perf_counter_ns()
        
        new_price_data = PriceData(
            symbol=symbol_upper,
            price=price,
            timestamp_ns=timestamp_ns,
            source=source
        )
        
        with self._cache_lock:
            if if_newer:
                existing = self._price_cache.get(symbol_upper)
                if existing and existing.timestamp_ns >= timestamp_ns:
                    return  # Skip if existing is newer or same age
            
            self._price_cache[symbol_upper] = new_price_data
            self.logger.debug(f"Updated cache for {symbol_upper}: ${price:.4f} from {source}")

    # --- Legacy Compatibility Methods ---
    def get_latest_price_legacy(self, symbol: str) -> Optional[float]:
        """Legacy method for backward compatibility."""
        try:
            price_data = self.get_latest_price(symbol, max_age_ns=5_000_000_000)  # 5 second tolerance
            return price_data.price
        except (StalePriceError, PriceUnavailableError):
            return None

    def get_bid_price(self, symbol: str) -> Optional[float]:
        """Legacy method - returns latest price as bid approximation."""
        return self.get_latest_price_legacy(symbol)

    def get_ask_price(self, symbol: str) -> Optional[float]:
        """Legacy method - returns latest price as ask approximation."""
        return self.get_latest_price_legacy(symbol)

    def is_price_stale(self, symbol: str, max_age_seconds: float) -> bool:
        """Legacy method for staleness check."""
        try:
            self.get_latest_price(symbol, max_age_ns=int(max_age_seconds * 1_000_000_000))
            return False
        except (StalePriceError, PriceUnavailableError):
            return True

    def get_reliable_price(self, symbol: str, side: Optional[str] = None, allow_api_fallback: bool = True, correlation_id: Optional[str] = None) -> Optional[float]:
        """Legacy method - attempts blocking fetch with correlation_id support."""
        try:
            if allow_api_fallback:
                # Use blocking fetch for critical operations
                price_data = self.get_price_blocking(symbol, timeout_sec=1.0, correlation_id=correlation_id)
            else:
                # Use cache-only for non-critical operations
                price_data = self.get_latest_price(symbol, max_age_ns=1_000_000_000)  # 1 second tolerance
            return price_data.price
        except (StalePriceError, PriceUnavailableError):
            return None

    # --- Additional Legacy Methods for Compatibility ---
    def get_reference_price(self, symbol: str, perf_timestamps: Optional[Dict] = None) -> Optional[float]:
        """Gets a reference price for OCR Handler compatibility."""
        return self.get_latest_price_legacy(symbol)

    def is_subscribed(self, symbol: str) -> bool:
        """Check if symbol has cached data (legacy compatibility)."""
        with self._cache_lock:
            return symbol.upper() in self._price_cache

    def subscribe_symbol(self, symbol: str, interval: float = 0.0, fast_first: bool = False, window_secs: float = 5.0) -> None:
        """Legacy method - JIT Oracle doesn't need explicit subscription."""
        self.logger.debug(f"subscribe_symbol called for {symbol} - JIT Oracle handles this automatically")

    def update_cache_manually(self, symbol: str, bid: Optional[float] = None, ask: Optional[float] = None, last: Optional[float] = None):
        """Manually update cache for legacy compatibility."""
        if last is not None:
            self._update_cache(symbol, last, "manual_last")
        elif ask is not None:
            self._update_cache(symbol, ask, "manual_ask")
        elif bid is not None:
            self._update_cache(symbol, bid, "manual_bid")

    # --- Async Compatibility Wrapper ---
    async def get_latest_price_async(self, symbol: str) -> Optional[float]:
        """Async wrapper for legacy compatibility with OCR conditioner."""
        try:
            price_data = self.get_latest_price(symbol, max_age_ns=5_000_000_000)  # 5 second tolerance
            return price_data.price
        except (StalePriceError, PriceUnavailableError):
            return None