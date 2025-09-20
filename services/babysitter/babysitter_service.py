#!/usr/bin/env python3
"""
TESTRADE RedisBabysitterService - Robust Redis Data Management

Handles:
- Non-blocking data reception from TESTRADE Core via ZeroMQ.
- Immediate disk buffering of received data for durability.
- Asynchronous publishing of data from an internal queue to Redis by worker threads.
- Robust Redis connection management with retries and health checks.
- Automatic replay of disk-buffered data when Redis recovers from an outage.
- Pruning of old disk buffer files based on age and ensuring current write file safety.
- Consumption of GUI commands from a Redis stream and forwarding them to Core/OCR via ZMQ.
- Publishing its own health and Redis instance health to dedicated Redis streams.
"""

import zmq
import redis
import json
import logging
import threading
import time
import signal
import sys
import os
import random # For jitter in retries
import queue
import struct
from pathlib import Path
from typing import Optional, Dict, Any, Deque, List, Callable, Tuple
from collections import deque
from datetime import datetime
import zlib
import binascii

# Binary format constants (matching mmap format)
BINARY_MSG_TOTAL_LEN_BYTES = 4  # uint32 for total message length
BINARY_MSG_NUM_PARTS_BYTES = 2  # uint16 for number of parts
BINARY_MSG_PART_LEN_BYTES = 4   # uint32 for each part length

# Message type markers to distinguish formats in files
MSG_TYPE_LEGACY = b'L'  # Legacy JSON format
MSG_TYPE_MULTIPART = b'M'  # Multipart binary format

# >>> ADD THIS IMPORT <<<
# SessionRecorder is optional; defer logging about its absence until logger is initialized.
try:
    from core.services.session_recorder import SessionRecorder
except ImportError:
    try:
        from services.session_recorder import SessionRecorder
    except ImportError:
        SessionRecorder = None  # Logging about this happens after logger setup
# >>> END IMPORT <<<

# Thread monitoring removed - focus on execution time only
ThreadMonitor = None

# --- Global Configuration (Loaded from Environment or Defaults) ---
LOG_LEVEL = os.getenv("BABYSITTER_LOG_LEVEL", "INFO").upper()
# Recording toggle: default OFF for development until stabilized
RECORDING_ENABLED = os.getenv("BABYSITTER_RECORDING_ENABLED", "0").strip() in {"1", "true", "TRUE", "yes", "YES"}

# IPC Configuration - PULL Socket Addresses (Where Babysitter Listens for Data from ApplicationCore)
BABYSITTER_IPC_PULL_BULK_ADDRESS = os.getenv("BABYSITTER_IPC_PULL_BULK_ADDRESS", "tcp://*:5555")
BABYSITTER_IPC_PULL_TRADING_ADDRESS = os.getenv("BABYSITTER_IPC_PULL_TRADING_ADDRESS", "tcp://*:5556")
BABYSITTER_IPC_PULL_SYSTEM_ADDRESS = os.getenv("BABYSITTER_IPC_PULL_SYSTEM_ADDRESS", "tcp://*:5557")

# PUSH Socket Addresses (Where Babysitter Sends Commands TO ApplicationCore / OCR Process)
CORE_IPC_COMMAND_PUSH_ADDRESS = os.getenv("BABYSITTER_CORE_IPC_COMMAND_PUSH_ADDRESS", "tcp://127.0.0.1:5560")
OCR_PROCESS_IPC_COMMAND_PUSH_ADDRESS = os.getenv("BABYSITTER_OCR_IPC_COMMAND_PUSH_ADDRESS", "tcp://127.0.0.1:5559")

# ZMQ PULL Socket HWMs (RCVHWM - Receiver High Water Marks) - BULLETPROOF CONFIGURATION
ZMQ_PULL_HWM_BULK = int(os.getenv("BABYSITTER_ZMQ_PULL_HWM_BULK", 50000))      # Match SNDHWM of client's bulk PUSH
ZMQ_PULL_HWM_TRADING = int(os.getenv("BABYSITTER_ZMQ_PULL_HWM_TRADING", 5000)) # Match SNDHWM of client's trading PUSH
ZMQ_PULL_HWM_SYSTEM = int(os.getenv("BABYSITTER_ZMQ_PULL_HWM_SYSTEM", 10000))  # Match SNDHWM of client's system PUSH
ZMQ_LINGER_MS = int(os.getenv("BABYSITTER_ZMQ_LINGER_MS", 1000))  # How long to wait for pending messages on close

# >>> ADD THIS CONFIGURATION <<<
# For MVP, we only record the streams essential for "Shadow Boxer" replay.
# All other streams will be recorded by the Tape Deck in future phases.
# --- INTELLISENSE MVP CONFIGURATION ---
# This set defines the complete, minimal dataset required for the "Shadow Boxer"
# replay and validation mission. The Tape Deck will ONLY record these streams.
MVP_STREAMS_TO_RECORD = {
    # --------------------------------------------------------------------------
    # CATEGORY 1: RAW STIMULI (The Inputs for Re-injection)
    # These streams are the unaltered reality fed into the TANK during replay.
    # --------------------------------------------------------------------------

    # The Visual Sense: Raw, unprocessed image frames from the C++ accelerator.
    "testrade:raw-ocr-events",

    # The Market Sense: Raw, unfiltered trade and quote data from the provider.
    "testrade:market-tick",
    "testrade:market-quote",
    # Note: We capture raw market data. The TANK's real filtering logic will be
    # re-run during replay, which is a more accurate test.

    # --------------------------------------------------------------------------
    # CATEGORY 2: PROCESSED PERCEPTION (The System's Interpretation)
    # This is recorded for validation and debugging the TANK's internal logic.
    # --------------------------------------------------------------------------

    # The Conditioned Visual Sense: The final 5-token data after all Python
    # cleaning and flicker filtering. Allows for direct validation of the
    # conditioning pipeline's performance.
    "testrade:cleaned-ocr-snapshots",

    # --------------------------------------------------------------------------
    # CATEGORY 3: GROUND TRUTH (The "Answer Key" for Measurement)
    # This data is used to score the TANK's performance during replay analysis.
    # --------------------------------------------------------------------------

    # The TANK's Original Actions: A record of what our system actually did
    # during the live session. This serves as the initial baseline.
    "testrade:order-fills",
    "testrade:order-status",
    "testrade:position-updates",

    # The Master Trader's Actions: The ultimate benchmark.
    # This stream will be populated by a separate service monitoring the
    # master trader's feed. Add this stream once that service is online.
    # "master_trader:fills",
}
# >>> END CONFIGURATION <<<

# Redis Configuration
REDIS_HOST = os.getenv("BABYSITTER_REDIS_HOST", "localhost") # Default to localhost for common dev
REDIS_PORT = int(os.getenv("BABYSITTER_REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("BABYSITTER_REDIS_DB", 0))
REDIS_PASSWORD = os.getenv("BABYSITTER_REDIS_PASSWORD", None)
REDIS_CONNECT_TIMEOUT_SEC = float(os.getenv("BABYSITTER_REDIS_CONNECT_TIMEOUT_SEC", 2.0)) # Shorter
REDIS_SOCKET_TIMEOUT_SEC = float(os.getenv("BABYSITTER_REDIS_SOCKET_TIMEOUT_SEC", 2.0)) # Shorter
REDIS_PUBLISH_MAX_RETRIES_IN_WORKER = int(os.getenv("BABYSITTER_REDIS_PUBLISH_MAX_RETRIES_IN_WORKER", 3)) # Retries within _publish_to_redis
REDIS_PUBLISH_RETRY_BASE_DELAY_SEC = float(os.getenv("BABYSITTER_REDIS_PUBLISH_RETRY_BASE_DELAY_SEC", 0.2))
REDIS_HEALTH_CHECK_INTERVAL_SEC = float(os.getenv("BABYSITTER_REDIS_HEALTH_CHECK_INTERVAL_SEC", 3.0))
BABYSITTER_HEALTH_PUBLISH_INTERVAL_SEC = float(os.getenv("BABYSITTER_HEALTH_PUBLISH_INTERVAL_SEC", 8.0))

# Redis Stream Names
REDIS_STREAM_COMMANDS_FROM_GUI = os.getenv("REDIS_STREAM_COMMANDS_FROM_GUI", "testrade:commands:from_gui")
REDIS_STREAM_BABYSITTER_HEALTH = os.getenv("REDIS_STREAM_BABYSITTER_HEALTH", "testrade:health:babysitter")
REDIS_STREAM_REDIS_INSTANCE_HEALTH = os.getenv("REDIS_STREAM_REDIS_INSTANCE_HEALTH", "testrade:health:redis_instance")
REDIS_STREAM_MAXLEN_APPROX = int(os.getenv("REDIS_STREAM_MAXLEN_APPROX", 1000)) # Global maxlen for all Babysitter-published streams

# Disk Buffer Configuration
DISK_BUFFER_DIR = Path(os.getenv("BABYSITTER_DISK_BUFFER_PATH", "data/babysitter_disk_buffer"))
DISK_BUFFER_MAX_FILE_SIZE_MB = int(os.getenv("BABYSITTER_MAX_DISK_FILE_MB", 5)) # Much smaller for minimal bursts
DISK_BUFFER_MAX_AGE_DAYS = int(os.getenv("BABYSITTER_DISK_BUFFER_MAX_AGE_DAYS", 3))
DISK_REPLAY_INTERVAL_SEC = float(os.getenv("BABYSITTER_DISK_REPLAY_INTERVAL_SEC", 1.0)) # Faster replay
DISK_BUFFER_PRUNE_INTERVAL_SEC = float(os.getenv("BABYSITTER_DISK_BUFFER_PRUNE_INTERVAL_SEC", 1800.0)) # Every 30 mins

# Internal Queue & Worker Configuration (increased for bulletproof configuration)
INTERNAL_QUEUE_MAX_SIZE = int(os.getenv("BABYSITTER_MAX_INTERNAL_QUEUE", 50000)) # Larger to absorb ZMQ HWM bursts
REDIS_PUBLISH_WORKERS = int(os.getenv("BABYSITTER_REDIS_PUBLISH_WORKERS", 3)) # More workers for larger throughput

# psutil for system stats (optional)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger = logging.getLogger("RedisBabysitterService_NoPSUtil") # Different logger name if psutil fails
    logger.warning("psutil library not found. CPU/Memory monitoring for Babysitter will be disabled.")


# --- Logger Setup ---
Path("logs").mkdir(parents=True, exist_ok=True) # Ensure logs directory exists
logging.basicConfig(
    level=logging.CRITICAL,  # Only log critical errors
    format='%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.NullHandler()  # Discard all logs
    ]
)
logger = logging.getLogger("RedisBabysitterService") # Main logger for the service


class RobustDiskBufferManager:
    def __init__(self, buffer_path: Path, max_file_size_mb: int, max_file_age_days: int):
        self.buffer_path = buffer_path
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.max_file_age_seconds = max_file_age_days * 24 * 60 * 60
        self.buffer_path.mkdir(parents=True, exist_ok=True)
        
        self.current_file_path: Optional[Path] = None
        self.current_file_handle: Optional[Any] = None
        self.current_file_size: int = 0
        self.write_lock = threading.Lock()
        self._ensure_file_open_with_lock()
        logger.info(f"RobustDiskBufferManager initialized. Path: {self.buffer_path}, MaxFileSize: {max_file_size_mb}MB, MaxAge: {max_file_age_days}days")

    def _ensure_file_open_with_lock(self):
        """Ensures current_file_handle is open, rotating if necessary. Assumes lock is held."""
        if self.current_file_handle and not self.current_file_handle.closed:
            if self.current_file_size < self.max_file_size_bytes:
                return True
            else: # Max size reached, need to rotate
                logger.info(f"DiskBuffer: Max file size {self.max_file_size_bytes / (1024*1024):.2f}MB reached for {self.current_file_path}. Rotating.")

        if self.current_file_handle and not self.current_file_handle.closed:
            try: self.current_file_handle.close()
            except Exception as e: logger.error(f"DiskBuffer: Error closing file {self.current_file_path} for rotation: {e}")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pid = os.getpid()
        self.current_file_path = self.buffer_path / f"buffer_{timestamp}_{pid}.dat"
        try:
            self.current_file_handle = open(self.current_file_path, "ab")
            self.current_file_size = 0
            logger.info(f"DiskBuffer: Opened new file: {self.current_file_path}")
            return True
        except Exception as e:
            logger.critical(f"DiskBuffer: CRITICAL - Failed to open new file {self.current_file_path}: {e}", exc_info=True)
            self.current_file_handle = None
            return False

    def write(self, target_redis_stream: str, data_json_string: str) -> bool:
        with self.write_lock:
            if not self._ensure_file_open_with_lock():
                logger.error("DiskBuffer: File not open and could not be reopened. Write failed for stream '{target_redis_stream}'.")
                return False
            try:
                stream_bytes = target_redis_stream.encode('utf-8')
                stream_len_bytes = len(stream_bytes).to_bytes(2, 'big')
                data_bytes = data_json_string.encode('utf-8')
                newline = b'\n'
                entry_bytes = stream_len_bytes + stream_bytes + data_bytes + newline
                
                bytes_written = self.current_file_handle.write(entry_bytes)
                self.current_file_handle.flush()
                try: os.fsync(self.current_file_handle.fileno())
                except OSError as e_fsync: logger.warning(f"DiskBuffer: os.fsync failed for {self.current_file_path}: {e_fsync}")
                
                self.current_file_size += bytes_written
                return True
            except Exception as e:
                logger.error(f"DiskBuffer: Failed to write to {self.current_file_path} for stream '{target_redis_stream}': {e}", exc_info=True)
                if self.current_file_handle: self.current_file_handle.close()
                self.current_file_handle = None
                return False

    def get_files_for_replay(self) -> List[Path]:
        with self.write_lock:
            try:
                files = sorted([f for f in self.buffer_path.glob("buffer_*.dat") if f.is_file() and f != self.current_file_path])
                return files
            except Exception as e:
                logger.error(f"DiskBuffer: Error listing files for replay: {e}")
                return []
            
    def process_buffered_file(self, file_path: Path, publish_func: Callable[[str, str], bool]) -> bool:
        logger.info(f"DiskReplay: Processing file: {file_path}")
        processed_count = 0
        entries_to_process: List[Tuple[str, str]] = []
        
        try: # Read all entries first
            with open(file_path, "rb") as f:
                while True:
                    stream_len_bytes = f.read(2)
                    if not stream_len_bytes: break
                    if len(stream_len_bytes) < 2: 
                        logger.error(f"DiskReplay: Corrupt length in {file_path}. Aborting file."); return False
                    stream_len = int.from_bytes(stream_len_bytes, 'big')
                    
                    stream_name_bytes = f.read(stream_len)
                    if len(stream_name_bytes) < stream_len:
                        logger.error(f"DiskReplay: Corrupt stream name in {file_path}. Aborting file."); return False
                    target_redis_stream = stream_name_bytes.decode('utf-8', errors='replace')
                    
                    line_buffer = bytearray()
                    while True:
                        byte = f.read(1)
                        if not byte or byte == b'\n': break
                        line_buffer.extend(byte)
                    data_json_string = line_buffer.decode('utf-8', errors='replace')
                    if data_json_string: entries_to_process.append((target_redis_stream, data_json_string))
            
            if not entries_to_process:
                logger.info(f"DiskReplay: File {file_path} empty or unreadable. Deleting."); file_path.unlink(missing_ok=True); return True

        except Exception as e_read: # Error reading the file itself
            logger.error(f"DiskReplay: CRITICAL error reading file {file_path}: {e_read}. File will not be deleted.", exc_info=True)
            return False # Do not delete corrupted file, requires manual inspection

        # Attempt to publish entries
        remaining_entries: List[Tuple[str, str]] = []
        for target_stream, data_json in entries_to_process:
            if publish_func(target_stream, data_json):
                processed_count += 1
            else: # Publish failed, Redis likely down or message bad
                remaining_entries.append((target_stream, data_json))
                logger.warning(f"DiskReplay: Failed to publish entry for '{target_stream}' from {file_path}. Will keep remaining in file.")
                break # Stop processing this file for now to preserve order and not hammer Redis
        
        if not remaining_entries:
            logger.info(f"DiskReplay: Successfully replayed all {processed_count} entries from {file_path}. Deleting file.")
            file_path.unlink(missing_ok=True)
            return True
        else: # Some entries failed or were not attempted, rewrite remaining entries to the SAME file
            logger.warning(f"DiskReplay: {processed_count}/{len(entries_to_process)} entries replayed from {file_path}. {len(remaining_entries)} remain. Rewriting.")
            try:
                with open(file_path, "wb") as f_rewrite: # Overwrite with remaining
                    for target_stream, data_json in remaining_entries:
                        stream_bytes = target_stream.encode('utf-8')
                        stream_len_bytes = len(stream_bytes).to_bytes(2, 'big')
                        data_bytes = data_json.encode('utf-8')
                        newline = b'\n'
                        f_rewrite.write(stream_len_bytes + stream_bytes + data_bytes + newline)
                logger.info(f"DiskReplay: Rewrote {len(remaining_entries)} un-replayed entries back to {file_path}.")
            except Exception as e_rewrite:
                logger.error(f"DiskReplay: CRITICAL error rewriting remaining entries to {file_path}: {e_rewrite}", exc_info=True)
                # Original file is now overwritten or in an unknown state. Data might be lost if rewrite failed.
            return False

    def prune_old_files(self):
        with self.write_lock:
            logger.debug(f"DiskBufferPrune: Pruning files in {self.buffer_path} older than {self.max_file_age_seconds / 3600:.1f} hrs")
            now = time.time()
            pruned_count = 0
            files_checked = 0
            try:
                for file_path in self.buffer_path.glob("buffer_*.dat"):
                    files_checked +=1
                    if file_path == self.current_file_path: continue
                    try:
                        if (now - file_path.stat().st_mtime) > self.max_file_age_seconds:
                            logger.info(f"DiskBufferPrune: Deleting old file {file_path}")
                            file_path.unlink(missing_ok=True)
                            pruned_count += 1
                    except FileNotFoundError: continue # Already deleted
                    except Exception as e_del: logger.error(f"DiskBufferPrune: Error deleting {file_path}: {e_del}")
                if files_checked > 0 : logger.info(f"DiskBufferPrune: Checked {files_checked} files, pruned {pruned_count}.")
            except Exception as e_glob: logger.error(f"DiskBufferPrune: Error globbing files: {e_glob}")

    def write_multipart(self, message_parts: List[bytes]) -> bool:
        """
        Write a multipart message to disk in binary-native format.
        
        Format: [type_marker(1)][total_len(4)][num_parts(2)][len1(4)][part1][len2(4)][part2]...
        
        Args:
            message_parts: List of bytes objects representing the complete multipart message
                          Expected format: [stream_name, metadata_json, binary_part1, binary_part2, ...]
            
        Returns:
            True if successfully written, False otherwise
        """
        with self.write_lock:
            if not self._ensure_file_open_with_lock():
                logger.error("DiskBuffer: File not open and could not be reopened. Multipart write failed.")
                return False
            
            try:
                # Calculate total content length
                total_content_len = BINARY_MSG_NUM_PARTS_BYTES
                for part in message_parts:
                    total_content_len += BINARY_MSG_PART_LEN_BYTES + len(part)
                
                # Build binary message
                # 1. Type marker
                entry_bytes = MSG_TYPE_MULTIPART
                
                # 2. Total length
                entry_bytes += struct.pack('<I', total_content_len)
                
                # 3. Number of parts
                entry_bytes += struct.pack('<H', len(message_parts))
                
                # 4. Each part with length prefix
                for part in message_parts:
                    entry_bytes += struct.pack('<I', len(part))
                    entry_bytes += part
                
                # Write to file
                bytes_written = self.current_file_handle.write(entry_bytes)
                self.current_file_handle.flush()
                
                try:
                    os.fsync(self.current_file_handle.fileno())
                except OSError as e:
                    logger.warning(f"DiskBuffer: os.fsync failed for multipart write: {e}")
                
                self.current_file_size += bytes_written
                
                if len(message_parts) >= 2:
                    stream_name = message_parts[0].decode('utf-8', errors='replace')
                    logger.debug(f"DiskBuffer: Wrote multipart message for '{stream_name}', {len(message_parts)} parts, {bytes_written} bytes")
                
                return True
                
            except Exception as e:
                logger.error(f"DiskBuffer: Failed to write multipart message: {e}", exc_info=True)
                # Close file handle on error
                if self.current_file_handle:
                    self.current_file_handle.close()
                self.current_file_handle = None
                return False

    def close(self):
        with self.write_lock:
            if self.current_file_handle and not self.current_file_handle.closed:
                try: self.current_file_handle.close()
                except Exception as e: logger.error(f"DiskBuffer: Error closing file {self.current_file_path} on shutdown: {e}")
            self.current_file_handle = None
            logger.info("RobustDiskBufferManager closed current file.")


class ThreadSafeRedisManager:
    """
    Thread-safe Redis client manager with aggressive invalidation and exponential backoff.
    
    This class ensures thread-safe Redis access with proper connection management,
    preventing race conditions and implementing intelligent retry logic.
    """
    
    def __init__(self, config):
        self._config = config
        self._lock = threading.Lock()
        self._redis_client = None
        self._last_failure_ts = 0
        self._reconnect_backoff_sec = 1.0  # Initial backoff
        self._logger = logging.getLogger("ThreadSafeRedisManager")
        
    def get_client(self) -> Optional[redis.Redis]:
        """The ONLY way to get the Redis client. Thread-safe with exponential backoff."""
        with self._lock:
            # First, check if we have a client. If not, try to connect.
            if self._redis_client is None:
                # Implement exponential backoff to avoid hammering Redis
                if time.time() - self._last_failure_ts < self._reconnect_backoff_sec:
                    return None  # Still in backoff period
                
                return self._connect()  # Attempt connection
            
            # If we DO have a client, we MUST verify it's alive
            try:
                if self._redis_client.ping():
                    self._reconnect_backoff_sec = 1.0  # Reset backoff on success
                    return self._redis_client
                else:
                    # This should not happen, ping should raise on failure
                    # But as a failsafe, we invalidate
                    self._invalidate_connection("Ping returned False")
                    return None
            except Exception as e:
                # Ping failed. The connection is dead. Invalidate it.
                self._invalidate_connection(f"Ping failed: {e}")
                return None
    
    def _connect(self) -> Optional[redis.Redis]:
        """Internal connect method. MUST be called within the lock."""
        self._logger.info(f"Attempting new Redis connection (backoff: {self._reconnect_backoff_sec:.1f}s)...")
        try:
            client = redis.Redis(
                host=self._config.get('REDIS_HOST', 'localhost'),
                port=self._config.get('REDIS_PORT', 6379),
                db=self._config.get('REDIS_DB', 0),
                password=self._config.get('REDIS_PASSWORD', None),
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                socket_keepalive=True,
                socket_keepalive_options={},
                retry_on_timeout=False,
                decode_responses=False
            )
            client.ping()
            self._redis_client = client
            self._logger.info("Redis connection successful.")
            self._reconnect_backoff_sec = 1.0  # Reset backoff on success
            return self._redis_client
        except Exception as e:
            self._logger.error(f"Failed to connect to Redis: {e}")
            self._invalidate_connection(f"Connect failed: {e}")
            return None
    
    def _invalidate_connection(self, reason: str):
        """
        Internal method to aggressively clean up a dead connection
        and increase the backoff delay.
        """
        self._logger.warning(f"Invalidating Redis connection. Reason: {reason}")
        if self._redis_client:
            try:
                self._redis_client.close()
            except Exception:
                pass
        self._redis_client = None
        self._last_failure_ts = time.time()
        # Increase backoff, up to a max of 30 seconds
        self._reconnect_backoff_sec = min(30.0, self._reconnect_backoff_sec * 1.5)
    
    def close(self):
        """Safely close the Redis connection."""
        with self._lock:
            if self._redis_client:
                try:
                    self._redis_client.close()
                except Exception as e:
                    self._logger.error(f"Error closing Redis client: {e}")
                self._redis_client = None
    
    def disconnect(self):
        """
        Safely disconnect the Redis client.
        """
        with self._lock:
            if self._redis_client:
                try:
                    self._redis_client.close()
                except Exception as e:
                    self._logger.error(f"Error closing Redis connection: {e}")
                finally:
                    self._redis_client = None


class RedisBabysitterService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []

        self.ipc_context = zmq.Context.instance()
        # Three PULL sockets for receiving data from ApplicationCore
        self.ipc_pull_bulk_socket: Optional[zmq.Socket] = None
        self.ipc_pull_trading_socket: Optional[zmq.Socket] = None
        self.ipc_pull_system_socket: Optional[zmq.Socket] = None
        # Two PUSH sockets for sending commands
        self.core_command_push_socket: Optional[zmq.Socket] = None
        self.ocr_command_push_socket: Optional[zmq.Socket] = None
        self._initialize_zmq_sockets()

        # THREAD-SAFE REDIS: Use manager instead of direct client
        redis_config = {
            'REDIS_HOST': REDIS_HOST,
            'REDIS_PORT': REDIS_PORT,
            'REDIS_DB': REDIS_DB,
            'REDIS_PASSWORD': REDIS_PASSWORD
        }
        self.redis_manager = ThreadSafeRedisManager(redis_config)
        # NOTE: No more self.redis_client - all access goes through manager

        self.disk_buffer = RobustDiskBufferManager(DISK_BUFFER_DIR, DISK_BUFFER_MAX_FILE_SIZE_MB, DISK_BUFFER_MAX_AGE_DAYS)

        # >>> ADD THIS BLOCK to instantiate the recorder <<<
        # SESSION RECORDER (TAPE DECK) INITIALIZATION
        # Default to D: drive (500GB) to handle 30fps x2 stream volumes (~6.5GB/hour)
        session_log_dir = os.getenv("BABYSITTER_SESSION_LOG_DIR", "D:\\testrade-sessions")
        if not RECORDING_ENABLED:
            self.session_recorder = None
            logger.info("Session recording disabled (BABYSITTER_RECORDING_ENABLED=0)")
        else:
            if SessionRecorder:
                try:
                    self.session_recorder = SessionRecorder(output_dir=session_log_dir)
                    logger.info("SessionRecorder initialized for Operation Tape Deck")
                except Exception as e:
                    logger.error(f"Failed to initialize SessionRecorder: {e}")
                    self.session_recorder = None
            else:
                self.session_recorder = None
                logger.info("SessionRecorder not available - session recording disabled")
        # >>> END BLOCK <<<

        # NEW: This is the primary internal buffer that decouples ingest from processing.
        # It holds tuples of (target_stream, data_json_string).
        self.processing_queue: queue.Queue[Tuple[str, str]] = queue.Queue(maxsize=INTERNAL_QUEUE_MAX_SIZE)
        
        # We no longer need the old lock and condition variable for this queue.
        # The queue.Queue object is already thread-safe.

        # Statistics for monitoring direct vs buffered publishing
        self.direct_publish_count = 0
        self.buffered_publish_count = 0
        self.stats_lock = threading.Lock()

        self.psutil_process_handle: Optional[psutil.Process] = None
        if PSUTIL_AVAILABLE:
            try:
                self.psutil_process_handle = psutil.Process(os.getpid())
                self.psutil_process_handle.cpu_percent(interval=None) 
            except Exception as e: logger.warning(f"psutil.Process failed for Babysitter: {e}")

        logger.info("RedisBabysitterService initialized.")

    def _initialize_zmq_sockets(self):
        # Initialize three PULL sockets for receiving data from ApplicationCore
        try:
            # BULK PULL Socket (OCR, images)
            self.ipc_pull_bulk_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_bulk_socket.set_hwm(ZMQ_PULL_HWM_BULK)
            self.ipc_pull_bulk_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            self.ipc_pull_bulk_socket.bind(BABYSITTER_IPC_PULL_BULK_ADDRESS)
            logger.info(f"Babysitter BULK PULL socket bound to {BABYSITTER_IPC_PULL_BULK_ADDRESS} (HWM: {ZMQ_PULL_HWM_BULK})")
        except zmq.ZMQError as e:
            logger.critical(f"CRITICAL: Failed to bind Babysitter BULK PULL socket: {e}")
            raise

        try:
            # TRADING PULL Socket (fills, critical positions)
            self.ipc_pull_trading_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_trading_socket.set_hwm(ZMQ_PULL_HWM_TRADING)
            self.ipc_pull_trading_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            self.ipc_pull_trading_socket.bind(BABYSITTER_IPC_PULL_TRADING_ADDRESS)
            logger.info(f"Babysitter TRADING PULL socket bound to {BABYSITTER_IPC_PULL_TRADING_ADDRESS} (HWM: {ZMQ_PULL_HWM_TRADING})")
        except zmq.ZMQError as e:
            logger.critical(f"CRITICAL: Failed to bind Babysitter TRADING PULL socket: {e}")
            raise

        try:
            # SYSTEM PULL Socket (health, commands, general updates)
            self.ipc_pull_system_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_system_socket.set_hwm(ZMQ_PULL_HWM_SYSTEM)
            self.ipc_pull_system_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            self.ipc_pull_system_socket.bind(BABYSITTER_IPC_PULL_SYSTEM_ADDRESS)
            logger.info(f"Babysitter SYSTEM PULL socket bound to {BABYSITTER_IPC_PULL_SYSTEM_ADDRESS} (HWM: {ZMQ_PULL_HWM_SYSTEM})")
        except zmq.ZMQError as e:
            logger.critical(f"CRITICAL: Failed to bind Babysitter SYSTEM PULL socket: {e}")
            raise
        
        try:
            self.core_command_push_socket = self.ipc_context.socket(zmq.PUSH)
            # Configure for immediate delivery (N-1 lag elimination)
            self.core_command_push_socket.setsockopt(zmq.IMMEDIATE, 1)  # Immediate delivery
            self.core_command_push_socket.set_hwm(10)                   # HWM = 10 (was 1, too restrictive)
            self.core_command_push_socket.setsockopt(zmq.LINGER, 0)     # No linger on close
            self.core_command_push_socket.connect(CORE_IPC_COMMAND_PUSH_ADDRESS)
            logger.info(f"Babysitter PUSH socket for Core commands connected to {CORE_IPC_COMMAND_PUSH_ADDRESS} with IMMEDIATE mode")
        except zmq.ZMQError as e:
            logger.error(f"Failed to connect PUSH socket for Core commands: {e}. Core command forwarding disabled.")
            if self.core_command_push_socket: self.core_command_push_socket.close()
            self.core_command_push_socket = None

        try:
            self.ocr_command_push_socket = self.ipc_context.socket(zmq.PUSH)
            # Configure for immediate delivery (N-1 lag elimination)
            self.ocr_command_push_socket.setsockopt(zmq.IMMEDIATE, 1)   # Immediate delivery
            self.ocr_command_push_socket.set_hwm(10)                    # HWM = 10 (was 1, too restrictive)
            self.ocr_command_push_socket.setsockopt(zmq.LINGER, 0)      # No linger on close
            self.ocr_command_push_socket.connect(OCR_PROCESS_IPC_COMMAND_PUSH_ADDRESS)
            logger.info(f"Babysitter PUSH socket for OCR commands connected to {OCR_PROCESS_IPC_COMMAND_PUSH_ADDRESS} with IMMEDIATE mode")
        except zmq.ZMQError as e:
            logger.error(f"Failed to connect PUSH socket for OCR commands: {e}. OCR command forwarding disabled.")
            if self.ocr_command_push_socket: self.ocr_command_push_socket.close()
            self.ocr_command_push_socket = None

    def _connect_redis_with_retries(self, max_retries=3, base_delay_seconds=0.2) -> bool:
        """THREAD-SAFE: Connection is now handled by ThreadSafeRedisManager"""
        # Simply check if we can get a client from the manager
        client = self.redis_manager.get_client()
        return client is not None

    def _is_redis_healthy_ping_only(self) -> bool:
        """THREAD-SAFE: Check Redis health through manager"""
        client = self.redis_manager.get_client()
        return client is not None

    def _is_redis_healthy_with_reconnect(self) -> bool:
        """THREAD-SAFE: Health check through manager (manager handles reconnection)"""
        return self._is_redis_healthy_ping_only()

    def _publish_to_redis_with_internal_retry(self, target_stream: str, data_json_string: str) -> bool:
        """THREAD-SAFE: Publish to Redis through manager with retries."""
        # Get a guaranteed-healthy client from the manager
        client = self.redis_manager.get_client()
        
        if not client:
            # The manager handles logging, we just know it failed
            logger.warning(f"PublishRetry: Cannot publish to '{target_stream}', Redis unavailable.")
            return False
        
        for attempt in range(REDIS_PUBLISH_MAX_RETRIES_IN_WORKER):
            if self.stop_event.is_set(): 
                return False
            try:
                # We now know `client` is a valid, connected object
                payload_for_xadd = {'json_payload': data_json_string.encode('utf-8')}
                client.xadd(target_stream, payload_for_xadd, id='*', maxlen=REDIS_STREAM_MAXLEN_APPROX, approximate=True)
                return True
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning(f"PublishRetryFail (Conn/Timeout) Att {attempt+1}: '{target_stream}': {e}")
                # Get fresh client for next attempt
                client = self.redis_manager.get_client()
                if not client:
                    logger.error(f"PublishRetry: Unable to get Redis client for '{target_stream}'")
                    return False
                if attempt < REDIS_PUBLISH_MAX_RETRIES_IN_WORKER - 1:
                    self.stop_event.wait(REDIS_PUBLISH_RETRY_BASE_DELAY_SEC * (2**attempt))
            except Exception as e:
                # The xadd can still fail, but it won't be an AttributeError
                logger.error(f"PublishRetryFail (Unexpected) Att {attempt+1}: '{target_stream}': {e}", exc_info=True)
                break  # Break retry loop on unexpected error
        return False

    def _ipc_receive_loop(self):
        logger.info("Babysitter IPC Data Receive Loop (3-socket PULL architecture) started.")
        logger.info("Using ZMQ as primary backpressure buffer, disk fallback only when Redis is down.")
        poller = zmq.Poller()

        # Register all three PULL sockets with the poller (with initialization checks)
        if self.ipc_pull_trading_socket:  # Check if socket was initialized successfully
            poller.register(self.ipc_pull_trading_socket, zmq.POLLIN)
            logger.debug("Registered trading PULL socket with poller")
        if self.ipc_pull_system_socket:
            poller.register(self.ipc_pull_system_socket, zmq.POLLIN)
            logger.debug("Registered system PULL socket with poller")
        if self.ipc_pull_bulk_socket:
            poller.register(self.ipc_pull_bulk_socket, zmq.POLLIN)
            logger.debug("Registered bulk PULL socket with poller")

        if not poller.sockets:  # Important check
            logger.critical("IPCDataRecv: No PULL sockets were successfully registered with Poller. Loop cannot effectively run.")
            return  # Exit if no sockets to monitor

        while not self.stop_event.is_set():
            try:
                socks = dict(poller.poll(timeout=200))  # 200ms timeout as recommended

                # Process trading socket first if it has data
                if self.ipc_pull_trading_socket in socks and socks[self.ipc_pull_trading_socket] == zmq.POLLIN:
                    while True:  # Loop to get all pending messages on this socket
                        try:
                            message_parts = self.ipc_pull_trading_socket.recv_multipart(flags=zmq.DONTWAIT)
                            self._handle_received_ipc_message("trading", message_parts)
                        except zmq.Again:  # No more messages on this socket for now
                            break
                        except zmq.ZMQError as e_sock:
                            logger.error(f"ZMQError receiving from trading socket: {e_sock}")
                            break  # Break inner loop on socket error

                # Process system socket next
                if self.ipc_pull_system_socket in socks and socks[self.ipc_pull_system_socket] == zmq.POLLIN:
                    while True:
                        try:
                            message_parts = self.ipc_pull_system_socket.recv_multipart(flags=zmq.DONTWAIT)
                            self._handle_received_ipc_message("system", message_parts)
                        except zmq.Again:
                            break
                        except zmq.ZMQError as e_sock:
                            logger.error(f"ZMQError receiving from system socket: {e_sock}")
                            break

                # Process bulk socket last
                if self.ipc_pull_bulk_socket in socks and socks[self.ipc_pull_bulk_socket] == zmq.POLLIN:
                    while True:
                        try:
                            message_parts = self.ipc_pull_bulk_socket.recv_multipart(flags=zmq.DONTWAIT)
                            self._handle_received_ipc_message("bulk", message_parts)
                        except zmq.Again:
                            break
                        except zmq.ZMQError as e_sock:
                            logger.error(f"ZMQError receiving from bulk socket: {e_sock}")
                            break

            except zmq.ZMQError as e_poll:  # Error during poller.poll() itself
                if e_poll.errno == zmq.ETERM:  # Context terminated
                    logger.info("IPCDataRecv: ZMQ Context terminated during poll. Exiting loop.")
                    break
                logger.error(f"IPCDataRecv: ZMQError in poller.poll(): {e_poll}")
                self.stop_event.wait(0.5)  # Brief pause before retrying poll
            except Exception as e_outer:
                logger.error(f"IPCDataRecv: Unexpected error in outer polling loop: {e_outer}", exc_info=True)
                self.stop_event.wait(0.5)  # Brief pause
        logger.info("Babysitter IPC Data Receive Loop stopped.")

    def _process_graduated_defense_flags(self, data_json_string: str) -> str:
        """
        Processes graduated defense flags in the message payload and handles decompression if needed.
        Returns the processed data_json_string ready for Redis publishing.
        """
        try:
            # Parse the message wrapper
            message_wrapper = json.loads(data_json_string)
            payload = message_wrapper.get("payload", {})

            # Check for graduated defense flags
            is_compressed = payload.get("_is_compressed", False)
            compression_type = payload.get("_compression_type")
            was_stripped = payload.get("_was_stripped")
            was_aggressively_triaged = payload.get("_was_aggressively_triaged", False)

            # Log graduated defense processing if any flags are present
            defense_flags = []
            if is_compressed:
                defense_flags.append(f"compressed({compression_type})")
            if was_stripped:
                defense_flags.append(f"stripped({was_stripped})")
            if was_aggressively_triaged:
                defense_flags.append("triaged")

            if defense_flags:
                logger.debug(f"BabysitterGradDefense: Processing message with flags: {', '.join(defense_flags)}")

            # Handle decompression if needed
            if is_compressed and compression_type:
                compressed_data_field = payload.get("_compressed_data")
                if compressed_data_field:
                    try:
                        # Decode hex-encoded compressed data
                        compressed_bytes = binascii.unhexlify(compressed_data_field)

                        # Decompress based on type
                        if compression_type == "zlib":
                            decompressed_json = zlib.decompress(compressed_bytes).decode('utf-8')
                            decompressed_payload = json.loads(decompressed_json)

                            # Replace the payload with decompressed data, preserving metadata
                            message_wrapper["payload"] = decompressed_payload

                            # Remove compression flags from the decompressed payload
                            if "_is_compressed" in message_wrapper["payload"]:
                                del message_wrapper["payload"]["_is_compressed"]
                            if "_compression_type" in message_wrapper["payload"]:
                                del message_wrapper["payload"]["_compression_type"]
                            if "_compressed_data" in message_wrapper["payload"]:
                                del message_wrapper["payload"]["_compressed_data"]

                            logger.info(f"BabysitterGradDefense: Successfully decompressed {compression_type} data")
                            return json.dumps(message_wrapper)

                        else:
                            logger.warning(f"BabysitterGradDefense: Unsupported compression type '{compression_type}'. Publishing as-is.")

                    except Exception as e:
                        logger.error(f"BabysitterGradDefense: Failed to decompress {compression_type} data: {e}. Publishing as-is.")

            # If no decompression needed or decompression failed, return original
            return data_json_string

        except json.JSONDecodeError as e:
            logger.warning(f"BabysitterGradDefense: Invalid JSON in message, cannot process defense flags: {e}. Publishing as-is.")
            return data_json_string
        except Exception as e:
            logger.error(f"BabysitterGradDefense: Unexpected error processing defense flags: {e}. Publishing as-is.", exc_info=True)
            return data_json_string

    def _handle_received_ipc_message(self, socket_origin_name: str, message_parts: List[bytes]):
        """
        Handles both legacy 2-part and new multipart message formats.
        The FIRST action is now to record the raw, unaltered message to the Tape Deck.
        """
        # >>> MODIFY THIS SECTION <<<
        # --- TAPE DECK SELECTIVE RECORDING ---
        if self.session_recorder and self.session_recorder.is_active:
            try:
                # The stream name is always the first part of our multipart message
                stream_name = message_parts[0].decode('utf-8')
                
                if stream_name in MVP_STREAMS_TO_RECORD:
                    if self.session_recorder:
                        self.session_recorder.record_event(message_parts)
                        logger.debug(f"Tape Deck: Recorded MVP stream: {stream_name}")
                else:
                    # Optional debug logging for dropped streams (disabled by default for performance)
                    # logger.debug(f"Tape Deck: Skipping non-MVP stream: {stream_name}")
                    pass

            except Exception as e:
                logger.error(f"Tape Deck: Error in selective recording logic: {e}")
                # Fallback: record anyway to avoid losing critical data
                if self.session_recorder:
                    self.session_recorder.record_event(message_parts)
        # --- END OF RECORDING HOOK ---
        # >>> END MODIFICATION <<<
        
        # Validate minimum message parts
        if len(message_parts) < 2:
            logger.warning(f"IPCDataRecv ({socket_origin_name}): Malformed message ({len(message_parts)} parts), expected at least 2.")
            return

        try:
            # Decode stream name and metadata
            target_redis_stream = message_parts[0].decode('utf-8')
            metadata_json_string = message_parts[1].decode('utf-8')
            
            # Check if this is a multipart message with binary blobs
            if len(message_parts) > 2:
                # RACE-SPEC ENGINE: Multipart binary message
                binary_blobs = message_parts[2:]  # All remaining parts are binary data
                
                # Parse metadata to get correlation_id
                try:
                    metadata_dict = json.loads(metadata_json_string)
                    correlation_id = metadata_dict.get("correlation_id")
                    
                    if not correlation_id:
                        logger.warning(f"Multipart message missing correlation_id in metadata. Stream: {target_redis_stream}")
                        # Still process it but without correlation
                        correlation_id = "unknown"
                    
                    # Create storage task for worker
                    storage_task = {
                        "type": "multipart",
                        "stream_name": target_redis_stream,
                        "metadata_json": metadata_json_string,
                        "binary_blobs": binary_blobs,
                        "correlation_id": correlation_id
                    }
                    
                    # Queue for processing
                    self.processing_queue.put_nowait(storage_task)
                    
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode metadata JSON from multipart message: {e}")
                    return
                    
            else:
                # Legacy 2-part message
                # Process graduated defense flags (decompression, etc.)
                processed_data_json_string = self._process_graduated_defense_flags(metadata_json_string)
                
                # Create legacy storage task
                storage_task = {
                    "type": "legacy",
                    "stream_name": target_redis_stream,
                    "data_json": processed_data_json_string
                }
                
                # Queue for processing
                self.processing_queue.put_nowait(storage_task)

        except queue.Full:
            # This is a critical failure state. It means the Babysitter's internal
            # buffer is full and it cannot keep up with the data rate.
            # The upstream BulletproofIPCClient will now handle this back-pressure.
            logger.error(f"CRITICAL: Babysitter internal processing_queue is full. Upstream producers will now buffer.")
        except Exception as e:
            logger.error(f"IPCDataRecv ({socket_origin_name}): Error processing message for queue: {e}", exc_info=True)

    def _processing_worker_loop(self):
        """
        BINARY-NATIVE Worker thread loop. Handles both legacy and multipart message storage.
        Legacy: Simple JSON to Redis stream
        Multipart: Metadata to stream + binary blobs to separate keys
        FIXED: Uses binary-native disk buffering (no Base64 encoding overhead)
        """
        thread_name = threading.current_thread().name
        logger.info(f"Processing Worker ({thread_name}) started.")
        
        while not self.stop_event.is_set():
            try:
                # Get task from queue (now a dict with type info)
                task = self.processing_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            try:
                if task.get("type") == "multipart":
                    # RACE-SPEC ENGINE: Handle multipart binary message
                    stream_name = task["stream_name"]
                    metadata_json = task["metadata_json"]
                    binary_blobs = task["binary_blobs"]
                    correlation_id = task["correlation_id"]
                    
                    # INTELLIGENT STORAGE PATTERN
                    # Step 1: Store metadata in Redis stream
                    if self._is_redis_healthy_with_reconnect():
                        if self._publish_to_redis_with_internal_retry(stream_name, metadata_json):
                            # Step 2: Store binary blobs with TTL
                            if binary_blobs:
                                client = self.redis_manager.get_client()
                                if client:
                                    try:
                                        pipe = client.pipeline()
                                        ttl_seconds = 3600  # 1 hour TTL for recent images
                                        for i, blob in enumerate(binary_blobs):
                                            # Create unique key for each blob
                                            blob_key = f"binary_blob:{correlation_id}:part_{i}"
                                            pipe.set(blob_key, blob)
                                            pipe.expire(blob_key, ttl_seconds)
                                        pipe.execute()
                                        logger.info(f"ProcessingWorker ({thread_name}): Stored {len(binary_blobs)} binary blobs for {correlation_id}")
                                    except Exception as e:
                                        logger.error(f"ProcessingWorker ({thread_name}): Failed to store binary blobs for {correlation_id}: {e}")
                                        # CRITICAL: Metadata is in Redis but blobs failed
                                        # TODO: Add alert mechanism or cleanup
                                else:
                                    logger.warning(f"ProcessingWorker ({thread_name}): Cannot store binary blobs, Redis unavailable")
                        else:
                            # THE FIX: Buffer complete multipart message in binary format
                            logger.warning(f"ProcessingWorker ({thread_name}): Failed to publish metadata, falling back to binary disk buffer")
                            
                            # Construct complete message parts list
                            message_parts = [
                                stream_name.encode('utf-8'),
                                metadata_json.encode('utf-8')
                            ] + binary_blobs
                            
                            # Use binary-native disk buffer
                            if not self.disk_buffer.write_multipart(message_parts):
                                logger.critical(f"CRITICAL: Binary disk write failed for multipart '{stream_name}'. DATA MAY BE LOST.")
                    else:
                        # Redis unhealthy - THE FIX: Use binary disk buffer
                        logger.warning(f"ProcessingWorker ({thread_name}): Redis unhealthy, using binary disk buffer for multipart")
                        
                        # Construct complete message parts list
                        message_parts = [
                            stream_name.encode('utf-8'),
                            metadata_json.encode('utf-8')
                        ] + binary_blobs
                        
                        # Use binary-native disk buffer
                        if not self.disk_buffer.write_multipart(message_parts):
                            logger.critical(f"CRITICAL: Binary disk write failed for multipart '{stream_name}'. DATA MAY BE LOST.")
                            
                else:
                    # Legacy 2-part message handling
                    stream_name = task["stream_name"]
                    data_json = task["data_json"]
                    
                    if self._is_redis_healthy_with_reconnect():
                        if self._publish_to_redis_with_internal_retry(stream_name, data_json):
                            # Success
                            pass
                        else:
                            logger.warning(f"ProcessingWorker ({thread_name}): Failed to publish '{stream_name}' to Redis, falling back to disk")
                            if not self.disk_buffer.write(stream_name, data_json):
                                logger.critical(f"CRITICAL: DiskWriteFail for '{stream_name}' by {thread_name}. DATA MAY BE LOST.")
                    else:
                        # Redis unhealthy, write to disk
                        if not self.disk_buffer.write(stream_name, data_json):
                            logger.critical(f"CRITICAL: DiskWriteFail for '{stream_name}' by {thread_name}. DATA MAY BE LOST.")
                
            except Exception as e:
                logger.error(f"ProcessingWorker ({thread_name}): Unexpected error processing task: {e}", exc_info=True)
            
            self.processing_queue.task_done()
            
        logger.info(f"Processing Worker ({thread_name}) stopped.")

    def _disk_buffer_replay_loop(self):
        logger.info("Disk Buffer Replay Loop started.")
        while not self.stop_event.is_set():
            processed_file_in_cycle = False
            if self._is_redis_healthy_with_reconnect(): # Ensure Redis is generally healthy before replay attempt
                # Check if processing queue has space (80% threshold)
                has_space = self.processing_queue.qsize() < (self.processing_queue.maxsize * 0.8)

                if has_space:
                    files_to_replay = self.disk_buffer.get_files_for_replay()
                    if files_to_replay:
                        oldest_file = files_to_replay[0]
                        logger.info(f"DiskReplay: Attempting file: {oldest_file}")
                        # Pass _publish_to_redis_with_internal_retry which has its own retries
                        if self.disk_buffer.process_buffered_file(oldest_file, self._publish_to_redis_with_internal_retry):
                            logger.info(f"DiskReplay: Successfully replayed and deleted {oldest_file}.")
                            processed_file_in_cycle = True
                        else:
                            logger.warning(f"DiskReplay: Incomplete replay of {oldest_file}. Will retry file later.")
                            self.stop_event.wait(DISK_REPLAY_INTERVAL_SEC * 2) 
                            continue 
                    # else: logger.debug("DiskReplay: No files to replay.") # Can be noisy
                # else: logger.debug("DiskReplay: Internal queue near full. Deferring replay.") # Can be noisy
            # else: logger.debug("DiskReplay: Redis not healthy. Skipping cycle.") # Can be noisy

            if not processed_file_in_cycle:
                self.stop_event.wait(DISK_REPLAY_INTERVAL_SEC)
        logger.info("Disk Buffer Replay Loop stopped.")

    def _disk_buffer_prune_loop(self):
        logger.info("Disk Buffer Pruning Loop started.")
        while not self.stop_event.is_set():
            try: self.disk_buffer.prune_old_files()
            except Exception as e: logger.error(f"DiskPrune: Error: {e}", exc_info=True)
            self.stop_event.wait(DISK_BUFFER_PRUNE_INTERVAL_SEC)
        logger.info("Disk Buffer Pruning Loop stopped.")

    def _health_monitor_loop(self):
        logger.info("Babysitter Health Monitor loop started.")
        while not self.stop_event.is_set():
            try: self._publish_babysitter_and_redis_health()
            except Exception as e: logger.error(f"HealthMon: Error: {e}", exc_info=True)
            self.stop_event.wait(BABYSITTER_HEALTH_PUBLISH_INTERVAL_SEC)
        logger.info("Babysitter Health Monitor loop stopped.")

    def _publish_babysitter_and_redis_health(self):
        # First, ensure we have a client to publish health, try to connect if not
        if not self._is_redis_healthy_with_reconnect():
            logger.warning("HealthMon: Cannot publish health, Redis connection for publishing unavailable.")
            return

        current_time = time.time()
        health_event_id_prefix = f"health_{int(current_time)}"

        # --- 1. Babysitter Health ---
        bs_payload = {"timestamp": current_time, "component_name": "RedisBabysitterService", "status": "RUNNING"}
        if hasattr(self, 'disk_buffer') and self.disk_buffer:
            # Use try-except for file operations to prevent health monitor from crashing
            try:
                disk_files = self.disk_buffer.get_files_for_replay()
                bs_payload["disk_buffer_files_count"] = len(disk_files)
                total_size = sum(f.stat().st_size for f in disk_files if f.exists())
                bs_payload["disk_buffer_total_size_mb"] = total_size / (1024*1024)
            except Exception as e_disk: bs_payload["disk_buffer_status_error"] = str(e_disk)
        # Update to use new processing_queue
        bs_payload["internal_publish_queue_size"] = self.processing_queue.qsize()
        bs_payload["internal_publish_queue_capacity"] = self.processing_queue.maxsize

        # Add ZMQ Socket Health Status
        bs_payload["zmq_sockets"] = {
            "bulk_pull_bound": self.ipc_pull_bulk_socket is not None and not self.ipc_pull_bulk_socket.closed,
            "trading_pull_bound": self.ipc_pull_trading_socket is not None and not self.ipc_pull_trading_socket.closed,
            "system_pull_bound": self.ipc_pull_system_socket is not None and not self.ipc_pull_system_socket.closed,
            "core_command_push_connected": self.core_command_push_socket is not None and not self.core_command_push_socket.closed,
            "ocr_command_push_connected": self.ocr_command_push_socket is not None and not self.ocr_command_push_socket.closed
        }

        if PSUTIL_AVAILABLE and self.psutil_process_handle:
            try:
                bs_payload["cpu_percent"] = self.psutil_process_handle.cpu_percent(None)
                bs_payload["memory_mb"] = self.psutil_process_handle.memory_info().rss / (1024*1024)
            except Exception: pass

        try:
            bs_wrapper = {"metadata": {"eventId": f"{health_event_id_prefix}_bs", "correlationId": f"{health_event_id_prefix}_bs", 
                                      "timestamp_ns": time.perf_counter_ns(), "epoch_timestamp_s": current_time, 
                                      "eventType": "TESTRADE_BABYSITTER_HEALTH", "sourceComponent": "BabysitterHealthMon"}, 
                          "payload": bs_payload }
            # Use _publish_to_redis_with_internal_retry for health messages too for consistency
            if not self._publish_to_redis_with_internal_retry(REDIS_STREAM_BABYSITTER_HEALTH, json.dumps(bs_wrapper)):
                 logger.error(f"HealthMon: Failed to publish Babysitter health after retries.")
        except Exception as e: logger.error(f"HealthMon: Error publishing Babysitter health: {e}", exc_info=True)

        # --- 2. Redis Instance Health ---
        redis_payload = {"timestamp": current_time, "component_name": "RedisInstance", "redis_host": REDIS_HOST, "redis_port": REDIS_PORT}
        # Check if Redis is available through manager
        client = self.redis_manager.get_client()
        if client:
            redis_payload["status"] = "CONNECTED_AND_HEALTHY"
            try:
                info = client.info()
                redis_payload.update({k: info.get(k) for k in ["redis_version", "uptime_in_seconds", "connected_clients", "used_memory_human"]})
            except Exception as e_info: redis_payload["status"] = f"CONNECTED_INFO_ERROR: {str(e_info)[:100]}"
        else:
            redis_payload["status"] = "DISCONNECTED_OR_UNHEALTHY"
        
        try:
            redis_wrapper = {"metadata": {"eventId": f"{health_event_id_prefix}_redis", "correlationId": f"{health_event_id_prefix}_redis", 
                                          "timestamp_ns": time.perf_counter_ns(), "epoch_timestamp_s": current_time, 
                                          "eventType": "TESTRADE_REDIS_INSTANCE_HEALTH", "sourceComponent": "BabysitterHealthMon"}, 
                             "payload": redis_payload}
            if not self._publish_to_redis_with_internal_retry(REDIS_STREAM_REDIS_INSTANCE_HEALTH, json.dumps(redis_wrapper)):
                logger.error(f"HealthMon: Failed to publish Redis instance health after retries.")
        except Exception as e: logger.error(f"HealthMon: Error publishing Redis instance health: {e}", exc_info=True)


    def _gui_command_consumer_loop(self):
        logger.info("Babysitter GUI Command Consumer Loop started.")
        stream_name = REDIS_STREAM_COMMANDS_FROM_GUI
        # Ensure unique group name per PID, and unique consumer name per thread for XREADGROUP
        group_name = f"bs_gui_cmd_grp_{os.getpid()}" 
        consumer_name_base = f"bs_gui_consumer_{os.getpid()}"
        thread_id_suffix = threading.current_thread().name.split('-')[-1] # Get number from "Thread-N"
        consumer_name = f"{consumer_name_base}_{thread_id_suffix}"

        while not self.stop_event.is_set():
            client = self.redis_manager.get_client()
            if not client:
                logger.warning("GUICmdConsumer: Redis not available. Pausing.")
                self.stop_event.wait(REDIS_HEALTH_CHECK_INTERVAL_SEC)
                continue
            try:
                try:
                    client.xgroup_create(name=stream_name, groupname=group_name, id='0', mkstream=True)
                except redis.exceptions.ResponseError as e:
                    if "BUSYGROUP" not in str(e).upper(): # Check case-insensitively
                        logger.error(f"GUICmdConsumer: Error creating/ensuring consumer group '{group_name}': {e}")
                        self.stop_event.wait(5.0) # Wait before retrying group creation
                        continue 
                
                messages = client.xreadgroup(
                    groupname=group_name, consumername=consumer_name,
                    streams={stream_name: '>'}, count=10, block=1000 # Read up to 10, block for 1s
                )
                if not messages: continue

                for _stream_key_ignored, stream_messages in messages:
                    ids_to_ack = []
                    stop_batch_processing = False
                    for msg_id, msg_data_bytes in stream_messages:
                        should_ack_this_message = True 
                        try:
                            # Try both 'json_payload' and 'data' field names for compatibility
                            cmd_wrapper_json_str = msg_data_bytes.get(b'json_payload') or msg_data_bytes.get(b'data')
                            if not cmd_wrapper_json_str:
                                available_fields = list(msg_data_bytes.keys())
                                logger.error(f"GUICmdConsumer: Msg {msg_id} missing 'json_payload' or 'data' field. Available fields: {available_fields}"); continue
                            
                            cmd_wrapper = json.loads(cmd_wrapper_json_str.decode('utf-8', errors='replace'))
                            actual_cmd_payload_dict = cmd_wrapper.get("payload", {})
                            
                            target_svc = actual_cmd_payload_dict.get("target_service")
                            cmd_type = actual_cmd_payload_dict.get("command_type")
                            cmd_id = actual_cmd_payload_dict.get("command_id", "unknown_cmd_id")
                            inner_payload_for_ipc_json_str = json.dumps(actual_cmd_payload_dict) # This is what Core/OCR expects

                            logger.info(f"GUICmdConsumer: Received '{cmd_type}' for '{target_svc}' (CmdID: {cmd_id}).")

                            # >>> ADD THIS LOGIC <<<
                            if target_svc == "BABYSITTER_SERVICE":
                                self._handle_gui_command(actual_cmd_payload_dict)
                                # After handling, we still ACK the message.
                            # >>> END LOGIC <<<
                            
                            socket_to_use: Optional[zmq.Socket] = None
                            if target_svc == "CORE_SERVICE": socket_to_use = self.core_command_push_socket
                            elif target_svc == "OCR_PROCESS": socket_to_use = self.ocr_command_push_socket
                            
                            if socket_to_use:
                                try:
                                    socket_to_use.send_multipart(
                                        [cmd_type.encode('utf-8'), inner_payload_for_ipc_json_str.encode('utf-8')],
                                        flags=zmq.DONTWAIT
                                    )
                                    logger.info(f"GUICmdConsumer: Forwarded '{cmd_type}' (CmdID: {cmd_id}) to '{target_svc}'.")
                                except zmq.Again: 
                                    logger.warning(f"GUICmdConsumer: ZMQ HWM for '{target_svc}' on cmd '{cmd_type}' (CmdID: {cmd_id}). Not ACKed.")
                                    should_ack_this_message = False 
                                    stop_batch_processing = True # Stop this batch, Redis will resend
                                    break 
                                except zmq.ZMQError as ze:
                                    logger.error(f"GUICmdConsumer: ZMQError forwarding '{cmd_type}' to '{target_svc}': {ze}. Msg {msg_id} will be ACKed.")
                            else:
                                logger.warning(f"GUICmdConsumer: No route or socket for target '{target_svc}', cmd '{cmd_type}'. Msg {msg_id} will be ACKed.")
                        except json.JSONDecodeError as jde:
                             raw_data = msg_data_bytes.get(b'json_payload', b'') or msg_data_bytes.get(b'data', b'')
                             logger.error(f"GUICmdConsumer: JSONDecodeError for msg {msg_id}: {jde}. Raw: {raw_data[:200]}")
                        except Exception as e_proc:
                            logger.error(f"GUICmdConsumer: Error processing msg {msg_id}: {e_proc}", exc_info=True)
                        finally:
                            if should_ack_this_message: ids_to_ack.append(msg_id)
                    
                    if ids_to_ack: # ACK messages that were processed or are unprocessable
                        try: client.xack(stream_name, group_name, *ids_to_ack)
                        except Exception as e_ack: logger.error(f"GUICmdConsumer: XACK error: {e_ack}")
                    if stop_batch_processing: break # Exit outer loop over streams if HWM hit

            except redis.exceptions.TimeoutError:
                # This is a normal "idle" state. The blocking read timed out.
                # We can log it at a DEBUG level if we want, but not as an ERROR.
                logger.debug("GUICmdConsumer: No new GUI commands received (timeout).")
                continue
                
            except redis.exceptions.ConnectionError as e_conn:
                # This is a REAL error. The connection is broken.
                logger.error(f"GUICmdConsumer: Redis connection error: {e_conn}. Manager will handle reconnection.")
                self.stop_event.wait(REDIS_HEALTH_CHECK_INTERVAL_SEC)
                
            except redis.exceptions.RedisError as e_redis:
                # Other Redis errors that aren't timeouts or connection errors
                logger.error(f"GUICmdConsumer: Redis error: {e_redis}. Manager will handle reconnection.")
                self.stop_event.wait(REDIS_HEALTH_CHECK_INTERVAL_SEC)
                
            except Exception as e_outer:
                # Catch any other unexpected errors.
                logger.error(f"GUICmdConsumer: Outer loop error: {e_outer}", exc_info=True)
                self.stop_event.wait(1.0) 
        logger.info("Babysitter GUI Command Consumer Loop stopped.")


    def start(self):
        logger.info(f"Starting RedisBabysitterService (PID: {os.getpid()})...")

        # >>> ADD THIS BLOCK to start the recorder <<<
        # Start the session recorder to open the .sdr file for writing
        if self.session_recorder:
            if not self.session_recorder.start_new_session():
                logger.critical("Failed to start SessionRecorder. Recording will be disabled.")
        # >>> END BLOCK <<<
        
        if not self._connect_redis_with_retries():
            logger.critical("Babysitter FAILED to connect to Redis on startup. Service will run in a degraded mode (disk buffering core data, no GUI command processing or health publishing until Redis is back).")
        
        self.stop_event.clear()
        thread_configs = [
            ("BabysitterIPCRecv", self._ipc_receive_loop),
            ("BabysitterDiskReplay", self._disk_buffer_replay_loop),
            ("BabysitterDiskPrune", self._disk_buffer_prune_loop),
            ("BabysitterHealthMon", self._health_monitor_loop),
            ("BabysitterGUICmdConsume", self._gui_command_consumer_loop)
        ]
        # Replace the old RedisPub workers with the new, more robust processing workers.
        for i in range(REDIS_PUBLISH_WORKERS):
            thread_configs.append((f"BabysitterProcessingWorker-{i}", self._processing_worker_loop))

        for name, target in thread_configs:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        logger.info(f"RedisBabysitterService started with {len(self.threads)} threads.")

    def shutdown_service(self):
        logger.info("Shutting down RedisBabysitterService...")
        self.stop_event.set()
        # No need for lock/condition with queue.Queue

        # Close all three PULL sockets
        logger.debug("Closing Babysitter ZMQ PULL sockets (from Core)...")
        for socket_name, socket_obj in [
            ("BULK PULL", self.ipc_pull_bulk_socket),
            ("TRADING PULL", self.ipc_pull_trading_socket),
            ("SYSTEM PULL", self.ipc_pull_system_socket)
        ]:
            if socket_obj and not socket_obj.closed:
                try:
                    socket_obj.close(linger=0)
                    logger.debug(f"Closed {socket_name} socket")
                except Exception as e:
                    logger.error(f"Error closing {socket_name} socket: {e}")

        # Close PUSH sockets
        logger.debug("Closing Babysitter ZMQ PUSH socket (to Core)...")
        if hasattr(self, 'core_command_push_socket') and self.core_command_push_socket and not self.core_command_push_socket.closed:
            try: self.core_command_push_socket.close(linger=0)
            except Exception as e: logger.error(f"Error closing Core command PUSH socket: {e}")

        logger.debug("Closing Babysitter ZMQ PUSH socket (to OCR)...")
        if hasattr(self, 'ocr_command_push_socket') and self.ocr_command_push_socket and not self.ocr_command_push_socket.closed:
            try: self.ocr_command_push_socket.close(linger=0)
            except Exception as e: logger.error(f"Error closing OCR command PUSH socket: {e}")
        
        if hasattr(self, 'ipc_context') and self.ipc_context and not self.ipc_context.closed:
             try: 
                 logger.debug("Terminating Babysitter ZMQ context...")
                 self.ipc_context.term()
             except Exception as e: logger.error(f"Error terminating ZMQ context: {e}")

        logger.debug("Joining Babysitter worker threads...")
        active_threads = [t for t in self.threads if t.is_alive()]
        join_timeout_per_thread = 3.0 / len(active_threads) if active_threads else 3.0
        for t in self.threads: # Iterate original list in case some finished
            try:
                if t.is_alive(): t.join(timeout=join_timeout_per_thread)
                if t.is_alive(): logger.warning(f"Babysitter thread {t.name} did not shut down gracefully.")
            except Exception as e: logger.error(f"Error joining thread {t.name}: {e}")
        self.threads.clear()

        # >>> ADD THIS BLOCK to stop the recorder <<<
        # --- TAPE DECK CLEANUP ---
        # Close the session recorder file cleanly AFTER threads are joined
        if self.session_recorder:
            self.session_recorder.stop()
        # --- END OF TAPE DECK CLEANUP ---
        # >>> END BLOCK <<<

        # Close Redis connection through manager
        try:
            self.redis_manager.close()
        except Exception as e:
            logger.error(f"Error closing Redis manager: {e}")
        
        if hasattr(self, 'disk_buffer'): self.disk_buffer.close()
        logger.info("RedisBabysitterService shut down complete.")

    def _handle_gui_command(self, command_payload: dict):
        """
        Handles specific commands intended for the Babysitter itself.
        """
        command_type = command_payload.get("command_type")
        
        if command_type == "START_SESSION_RECORDING":
            if self.session_recorder:
                logger.info("GUI command received: START_SESSION_RECORDING")
                if self.session_recorder.start_new_session():
                    logger.info("Session recording started successfully")
                else:
                    logger.error("Failed to start session recording")
            else:
                logger.warning("Cannot start recording: SessionRecorder is not initialized.")
        
        elif command_type == "STOP_SESSION_RECORDING":
            if self.session_recorder:
                logger.info("GUI command received: STOP_SESSION_RECORDING")
                self.session_recorder.stop()
                logger.info("Session recording stopped")
            else:
                logger.warning("Cannot stop recording: SessionRecorder is not initialized.")
        
        elif command_type == "TOGGLE_RECORDING_MODE":
            # Legacy support for toggle command - decide based on current state
            if self.session_recorder:
                if self.session_recorder.is_active:
                    logger.info("GUI command received: TOGGLE_RECORDING_MODE (stopping)")
                    self.session_recorder.stop()
                    logger.info("Session recording stopped")
                else:
                    logger.info("GUI command received: TOGGLE_RECORDING_MODE (starting)")
                    if self.session_recorder.start_new_session():
                        logger.info("Session recording started successfully")
                    else:
                        logger.error("Failed to start session recording")
            else:
                logger.warning("Cannot toggle recording: SessionRecorder is not initialized.")
        
        else:
            logger.warning(f"Unknown BABYSITTER_SERVICE command: {command_type}")

# --- Main Execution & Signal Handling ---
_babysitter_instance: Optional[RedisBabysitterService] = None

def _signal_shutdown_handler(signum, frame): # Underscore to indicate internal use
    global _babysitter_instance
    signal_name = signal.Signals(signum).name if hasattr(signal, 'Signals') and isinstance(signum, signal.Signals) else str(signum)
    logger.info(f"Received signal {signal_name}. Initiating Babysitter shutdown...")
    if _babysitter_instance:
        if not _babysitter_instance.stop_event.is_set(): # Prevent multiple shutdown calls
             _babysitter_instance.shutdown_service()
    # Forcing exit if threads hang can be risky, but might be needed if join fails
    # sys.exit(0) # Usually let finally block handle exit

def main():
    """Main entry point for the Babysitter service."""
    global _babysitter_instance

    script_dir = Path(__file__).parent.resolve()
    (script_dir / "logs").mkdir(parents=True, exist_ok=True)
    (script_dir / "data" / "babysitter_disk_buffer").mkdir(parents=True, exist_ok=True) # Ensure specific buffer path

    logger.info(f"--- Starting RedisBabysitterService (PID: {os.getpid()}) ---")

    # Thread monitoring removed for production stability

    _babysitter_instance = RedisBabysitterService()

    signal.signal(signal.SIGINT, _signal_shutdown_handler)
    signal.signal(signal.SIGTERM, _signal_shutdown_handler)
    if os.name == 'nt' and hasattr(signal, 'SIGBREAK'): # Windows specific
        signal.signal(signal.SIGBREAK, _signal_shutdown_handler)

    try:
        _babysitter_instance.start()
        # Keep main thread alive while service runs using the stop_event
        while not _babysitter_instance.stop_event.is_set():
            _babysitter_instance.stop_event.wait(timeout=1.0) # Interruptible wait
        logger.info("Babysitter stop_event is set, main loop exiting.")
    except Exception as e_main: # Catch errors during start or unexpected main loop issues
        logger.critical(f"Babysitter CRITICAL error in main execution: {e_main}", exc_info=True)
    finally:
        logger.info("Babysitter main process: ensuring final shutdown...")

        # Thread monitoring removed

        if _babysitter_instance and not _babysitter_instance.stop_event.is_set():
             # This means loop exited for reasons other than shutdown_service being fully called by signal
             logger.info("Stop event was not set, calling shutdown_service explicitly.")
             _babysitter_instance.shutdown_service()
        elif _babysitter_instance:
             logger.info("Stop event was set, shutdown_service likely already called or in progress.")
        logger.info(f"--- RedisBabysitterService (PID: {os.getpid()}) finished ---")

def setup_babysitter_signal_handlers():
    """Setup signal handlers for graceful independent shutdown."""
    
    def graceful_shutdown(signum, frame):
        """Handle shutdown signals gracefully without affecting other processes."""
        logger.info(f"🚀 Babysitter Service received signal {signum} - shutting down gracefully")
        logger.info("🛡️  This shutdown will NOT affect other TESTRADE services")
        logger.info("🚀 Babysitter Service shutdown complete")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, graceful_shutdown)
    
    return graceful_shutdown

if __name__ == "__main__":
    # Setup independent signal handlers before starting
    shutdown_handler = setup_babysitter_signal_handlers()
    
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🚀 Babysitter Service interrupted - exiting gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Babysitter Service fatal error: {e}")
        sys.exit(1)
