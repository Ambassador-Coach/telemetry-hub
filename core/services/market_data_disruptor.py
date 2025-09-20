#
# ----- HIGH-PERFORMANCE MARKET DATA DISRUPTOR -----
#
import threading
import time
import logging
from typing import List, Dict, Any

# Use numpy for high-performance, pre-allocated memory buffers
import numpy as np

from interfaces.data.services import IMarketDataReceiver
from interfaces.core.services import ILifecycleService

logger = logging.getLogger(__name__)

class MarketDataDisruptor(IMarketDataReceiver, ILifecycleService):
    """
    A high-performance, low-latency market data distribution service that implements
    the core principles of the LMAX Disruptor pattern. It uses a pre-allocated
    ring buffer to achieve near-zero producer overhead and allows for independent,
    parallel consumption of market data events.
    """
    def __init__(self, receivers: List[IMarketDataReceiver], buffer_size: int = 8192):
        """
        Initializes the Disruptor with a pre-allocated ring buffer.

        Args:
            receivers: A list of consumer services that implement IMarketDataReceiver.
            buffer_size: The size of the ring buffer. Must be a power of 2 for efficiency.
        """
        self.logger = logger
        if not (buffer_size > 0 and (buffer_size & (buffer_size - 1) == 0)):
            raise ValueError(f"Disruptor buffer_size must be a power of 2. Received: {buffer_size}")

        # --- 1. Pre-allocate Memory Buffers ---
        # Using structured numpy arrays for cache-friendly, contiguous memory.
        self.trade_buffer = np.empty(buffer_size, dtype=[
            ('symbol', 'S8'), ('price', 'f8'), ('size', 'i8'), ('timestamp', 'f8')
        ])
        self.quote_buffer = np.empty(buffer_size, dtype=[
            ('symbol', 'S8'), ('bid', 'f8'), ('ask', 'f8'),
            ('bid_size', 'i8'), ('ask_size', 'i8'), ('timestamp', 'f8')
        ])

        # --- 2. Ring Buffer State ---
        self._buffer_mask = buffer_size - 1
        self._trade_write_cursor = -1
        self._quote_write_cursor = -1
        
        # Each consumer gets its own independent read cursor.
        self._trade_read_cursors = {id(r): -1 for r in receivers}
        self._quote_read_cursors = {id(r): -1 for r in receivers}

        # --- 3. Consumer and Thread Management ---
        self._trade_consumers = [r for r in receivers if hasattr(r, 'on_trade')]
        self._quote_consumers = [r for r in receivers if hasattr(r, 'on_quote')]
        self._stop_event = threading.Event()
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="MarketDataDisruptorThread")

        # --- 4. Monitoring Metrics ---
        self._overflow_count = 0
        self._metrics_lock = threading.Lock()

        self.logger.info(f"MarketDataDisruptor initialized with buffer size {buffer_size} for {len(receivers)} consumers.")

    @property
    def is_ready(self) -> bool:
        """Check if the MarketDataDisruptor is ready."""
        return self._dispatch_thread.is_alive() if self._dispatch_thread else False

    @property
    def slowest_trade_consumer_cursor(self) -> int:
        """Find the slowest trade consumer's cursor for overflow detection."""
        return min(self._trade_read_cursors.values()) if self._trade_read_cursors else -1

    @property
    def slowest_quote_consumer_cursor(self) -> int:
        """Find the slowest quote consumer's cursor for overflow detection."""
        return min(self._quote_read_cursors.values()) if self._quote_read_cursors else -1

    def start(self):
        """Starts the dedicated dispatcher thread."""
        if not self._dispatch_thread.is_alive():
            self._dispatch_thread.start()
            self.logger.info("MarketDataDisruptor dispatcher thread started.")

    def stop(self):
        """Signals the dispatcher thread to stop."""
        self._stop_event.set()
        self.logger.info("MarketDataDisruptor stopping...")

    def get_stats(self) -> Dict[str, Any]:
        """
        Returns comprehensive monitoring metrics for observability.
        
        Returns:
            Dictionary containing producer cursors, consumer cursors, lag metrics, and overflow count.
        """
        with self._metrics_lock:
            # Calculate consumer lag for each consumer
            trade_lag = {}
            quote_lag = {}
            
            for consumer_id, read_cursor in self._trade_read_cursors.items():
                trade_lag[f"consumer_{consumer_id}"] = self._trade_write_cursor - read_cursor
                
            for consumer_id, read_cursor in self._quote_read_cursors.items():
                quote_lag[f"consumer_{consumer_id}"] = self._quote_write_cursor - read_cursor
            
            return {
                "producer_cursors": {
                    "trade_write_cursor": self._trade_write_cursor,
                    "quote_write_cursor": self._quote_write_cursor
                },
                "consumer_cursors": {
                    "trade_read_cursors": dict(self._trade_read_cursors),
                    "quote_read_cursors": dict(self._quote_read_cursors)
                },
                "consumer_lag": {
                    "trade_lag": trade_lag,
                    "quote_lag": quote_lag
                },
                "overflow_count": self._overflow_count,
                "buffer_size": len(self.trade_buffer),
                "active_consumers": {
                    "trade_consumers": len(self._trade_consumers),
                    "quote_consumers": len(self._quote_consumers)
                }
            }

    def on_trade(self, symbol: str, price: float, size: int, timestamp: float, **kwargs):
        """
        Non-blocking, lock-free write to the trade ring buffer. This is the hot path.
        """
        # Check for potential overflow before writing
        if self._trade_write_cursor - self.slowest_trade_consumer_cursor >= self._buffer_mask:
            # Handle overflow: log, increment metric, and potentially drop.
            self.logger.warning("Disruptor trade buffer overflow imminent or occurred. Data may be lost.")
            with self._metrics_lock:
                self._overflow_count += 1
            # Optionally return here to drop the message if configured to do so.
        
        # Claim the next slot in the ring buffer.
        self._trade_write_cursor += 1
        idx = self._trade_write_cursor & self._buffer_mask
        
        # Write data directly into the pre-allocated numpy array.
        self.trade_buffer[idx] = (symbol.encode(), price, size, timestamp)

    def on_quote(self, symbol: str, bid: float, ask: float, bid_size: int, ask_size: int, timestamp: float, **kwargs):
        """
        Non-blocking, lock-free write to the quote ring buffer. This is the hot path.
        """
        # Check for potential overflow before writing
        if self._quote_write_cursor - self.slowest_quote_consumer_cursor >= self._buffer_mask:
            # Handle overflow: log, increment metric, and potentially drop.
            self.logger.warning("Disruptor quote buffer overflow imminent or occurred. Data may be lost.")
            with self._metrics_lock:
                self._overflow_count += 1
            # Optionally return here to drop the message if configured to do so.
        
        self._quote_write_cursor += 1
        idx = self._quote_write_cursor & self._buffer_mask
        self.quote_buffer[idx] = (symbol.encode(), bid, ask, bid_size, ask_size, timestamp)

    def _dispatch_loop(self):
        """
        The dedicated dispatcher thread. It continuously checks for new data for each
        consumer and dispatches it, ensuring slow consumers do not block fast ones.
        """
        import gc
        gc.disable()
        self.logger.info("Garbage Collection disabled for high-performance dispatcher thread.")
        
        while not self._stop_event.is_set():
            # --- Dispatch Trades ---
            for consumer in self._trade_consumers:
                consumer_id = id(consumer)
                last_read = self._trade_read_cursors[consumer_id]
                
                # Process all new messages available for this specific consumer.
                while last_read < self._trade_write_cursor:
                    next_sequence = last_read + 1
                    idx = next_sequence & self._buffer_mask
                    entry = self.trade_buffer[idx]
                    
                    try:
                        consumer.on_trade(
                            entry['symbol'].decode(),
                            entry['price'],
                            int(entry['size']),
                            entry['timestamp']
                        )
                    except Exception as e:
                        self.logger.error(f"Error in trade consumer {consumer.__class__.__name__}: {e}", exc_info=True)
                    
                    last_read = next_sequence
                
                self._trade_read_cursors[consumer_id] = last_read

            # --- Dispatch Quotes ---
            for consumer in self._quote_consumers:
                consumer_id = id(consumer)
                last_read = self._quote_read_cursors[consumer_id]
                
                while last_read < self._quote_write_cursor:
                    next_sequence = last_read + 1
                    idx = next_sequence & self._buffer_mask
                    entry = self.quote_buffer[idx]
                    
                    try:
                        consumer.on_quote(
                            entry['symbol'].decode(),
                            entry['bid'],
                            entry['ask'],
                            int(entry['bid_size']),
                            int(entry['ask_size']),
                            entry['timestamp']
                        )
                    except Exception as e:
                        self.logger.error(f"Error in quote consumer {consumer.__class__.__name__}: {e}", exc_info=True)
                        
                    last_read = next_sequence
                
                self._quote_read_cursors[consumer_id] = last_read

            # Sleep briefly if no work was done to prevent busy-spinning.
            time.sleep(0.000001) # 1 microsecond

        self.logger.info("MarketDataDisruptor dispatcher thread stopped.")