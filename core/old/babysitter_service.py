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
from pathlib import Path
from typing import Optional, Dict, Any, Deque, List, Callable, Tuple
from collections import deque
from datetime import datetime
import zlib
import binascii

# --- Global Configuration (Loaded from Environment or Defaults) ---
LOG_LEVEL = os.getenv("BABYSITTER_LOG_LEVEL", "INFO").upper()

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
REDIS_PUBLISH_WORKERS = int(os.getenv("BABYSITTER_REDIS_PUBLISH_WORKERS", 3)) # LEGACY: No longer used in multi-lane architecture

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
    level=LOG_LEVEL,
    format='%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - [%(threadName)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/babysitter_service.log', mode='a')
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
        self.problematic_files: set = set()  # Track files that can't be processed
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
                files = sorted([f for f in self.buffer_path.glob("buffer_*.dat") 
                               if f.is_file() and f != self.current_file_path and f not in self.problematic_files])
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
                logger.info(f"DiskReplay: File {file_path} empty or unreadable. Attempting deletion.")
                try:
                    file_path.unlink(missing_ok=True)
                    logger.info(f"DiskReplay: Successfully deleted empty file {file_path}")
                    return True
                except Exception as e_delete:
                    logger.warning(f"DiskReplay: Could not delete empty file {file_path}: {e_delete}. Adding to problematic files list to avoid infinite retry.")
                    self.problematic_files.add(file_path)  # Blacklist this file
                    return True  # Return True to avoid infinite retry loop on undeletable empty files

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


    def close(self):
        with self.write_lock:
            if self.current_file_handle and not self.current_file_handle.closed:
                try: self.current_file_handle.close()
                except Exception as e: logger.error(f"DiskBuffer: Error closing file {self.current_file_path} on shutdown: {e}")
            self.current_file_handle = None
            logger.info("RobustDiskBufferManager closed current file.")


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

        self.redis_client: Optional[redis.Redis] = None
        # Initial connection attempt is done in start() to allow service to init even if Redis is down.

        self.disk_buffer = RobustDiskBufferManager(DISK_BUFFER_DIR, DISK_BUFFER_MAX_FILE_SIZE_MB, DISK_BUFFER_MAX_AGE_DAYS)

        # --- MULTI-LANE ARCHITECTURE: Replace single queue with lane-specific queues ---
        self.publish_queues: Dict[str, Deque[Tuple[str, str]]] = {
            'trading': deque(maxlen=INTERNAL_QUEUE_MAX_SIZE),
            'system': deque(maxlen=INTERNAL_QUEUE_MAX_SIZE),
            'bulk': deque(maxlen=INTERNAL_QUEUE_MAX_SIZE)
        }
        self.queue_locks: Dict[str, threading.Lock] = {
            name: threading.Lock() for name in self.publish_queues
        }
        self.queue_conditions: Dict[str, threading.Condition] = {
            name: threading.Condition(self.queue_locks[name]) for name in self.publish_queues
        }
        # Keep legacy references for backward compatibility during transition
        self.internal_publish_queue = self.publish_queues['system']  # Default fallback
        self.queue_lock = self.queue_locks['system']
        self.queue_condition = self.queue_conditions['system']

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
        # Initialize three PULL sockets for receiving data from ApplicationCore (hot/cold path)
        try:
            # BULK PULL Socket (OCR, images)
            self.ipc_pull_bulk_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_bulk_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            try:
                # Set receiver HWM to absorb bursts from PUSH side
                self.ipc_pull_bulk_socket.setsockopt(zmq.RCVHWM, ZMQ_PULL_HWM_BULK)
            except Exception:
                pass
            self.ipc_pull_bulk_socket.bind(BABYSITTER_IPC_PULL_BULK_ADDRESS)
            logger.info(f"Babysitter BULK PULL socket bound to {BABYSITTER_IPC_PULL_BULK_ADDRESS}")
        except zmq.ZMQError as e:
            logger.critical(f"CRITICAL: Failed to bind Babysitter BULK PULL socket: {e}")
            raise

        try:
            # TRADING PULL Socket (fills, critical positions)
            self.ipc_pull_trading_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_trading_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            try:
                self.ipc_pull_trading_socket.setsockopt(zmq.RCVHWM, ZMQ_PULL_HWM_TRADING)
            except Exception:
                pass
            self.ipc_pull_trading_socket.bind(BABYSITTER_IPC_PULL_TRADING_ADDRESS)
            logger.info(f"Babysitter TRADING PULL socket bound to {BABYSITTER_IPC_PULL_TRADING_ADDRESS}")
        except zmq.ZMQError as e:
            logger.critical(f"CRITICAL: Failed to bind Babysitter TRADING PULL socket: {e}")
            raise

        try:
            # SYSTEM PULL Socket (health, commands, general updates)
            self.ipc_pull_system_socket = self.ipc_context.socket(zmq.PULL)
            self.ipc_pull_system_socket.setsockopt(zmq.LINGER, ZMQ_LINGER_MS)
            try:
                self.ipc_pull_system_socket.setsockopt(zmq.RCVHWM, ZMQ_PULL_HWM_SYSTEM)
            except Exception:
                pass
            self.ipc_pull_system_socket.bind(BABYSITTER_IPC_PULL_SYSTEM_ADDRESS)
            logger.info(f"Babysitter SYSTEM PULL socket bound to {BABYSITTER_IPC_PULL_SYSTEM_ADDRESS}")
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
        if self.redis_client and self._is_redis_healthy_ping_only(): return True
        # Replace 'localhost' with direct IP address to avoid DNS resolution latency
        redis_host = "127.0.0.1" if REDIS_HOST == "localhost" else REDIS_HOST
        logger.info(f"Babysitter attempting Redis connection: {redis_host}:{REDIS_PORT}")
        for attempt in range(max_retries):
            if self.stop_event.is_set(): return False
            try:
                client = redis.Redis(
                    host=redis_host, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD,
                    decode_responses=False,
                    socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SEC,
                    socket_timeout=REDIS_SOCKET_TIMEOUT_SEC
                )
                # Measure Redis connection latency to track DNS resolution improvements
                connection_start_time = time.time()
                client.ping()
                connection_latency_ms = (time.time() - connection_start_time) * 1000

                self.redis_client = client
                logger.info(f"Babysitter connected to Redis successfully. Connection latency: {connection_latency_ms:.2f}ms")

                # Log high latency as warning to track DNS resolution issues
                if connection_latency_ms > 100:
                    logger.warning(f"High Redis connection latency detected: {connection_latency_ms:.2f}ms (threshold: 100ms)")

                return True
            except Exception as e:
                logger.warning(f"Babysitter Redis conn attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = base_delay_seconds * (2**attempt) + (random.random() * base_delay_seconds) # Add jitter
                    self.stop_event.wait(min(wait_time, 5.0)) # Max 5s wait between internal retries
                else:
                    logger.error(f"Babysitter failed to connect to Redis after {max_retries} attempts.")
                    self.redis_client = None
                    return False
        return False

    def _is_redis_healthy_ping_only(self) -> bool:
        if not self.redis_client: return False
        try: return self.redis_client.ping()
        except Exception: return False

    def _is_redis_healthy_with_reconnect(self) -> bool:
        if self.redis_client and self._is_redis_healthy_ping_only():
            return True
        logger.warning("Redis client unhealthy or None. Attempting connect.")
        return self._connect_redis_with_retries(max_retries=1, base_delay_seconds=0.1)

    def _publish_to_redis_with_internal_retry(self, target_stream: str, data_json_string: str, max_len: int) -> bool:
        """
        Tries to publish to Redis with configured retries and a specific MAXLEN.
        """
        if not self._is_redis_healthy_with_reconnect():
            logger.warning(f"PublishRetry: Cannot publish to '{target_stream}', Redis unavailable (pre-check).")
            return False

        for attempt in range(REDIS_PUBLISH_MAX_RETRIES_IN_WORKER):
            if self.stop_event.is_set(): return False
            try:
                payload_for_xadd = {'json_payload': data_json_string.encode('utf-8')}
                # Use the passed-in max_len value
                self.redis_client.xadd(target_stream, payload_for_xadd, id='*', maxlen=max_len, approximate=True)
                return True
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
                logger.warning(f"PublishRetryFail (Conn/Timeout) Att {attempt+1}: '{target_stream}': {e}. Client will be reset.")
                self.redis_client = None
                if attempt < REDIS_PUBLISH_MAX_RETRIES_IN_WORKER - 1:
                    self.stop_event.wait(REDIS_PUBLISH_RETRY_BASE_DELAY_SEC * (2**attempt))
                    if not self._is_redis_healthy_with_reconnect():
                        logger.warning(f"PublishRetry: Reconnect failed during retry for '{target_stream}'.")
                else:
                    logger.error(f"PublishRetry: Final attempt failed for '{target_stream}'.")
            except Exception as e:
                logger.error(f"PublishRetryFail (Unexpected) Att {attempt+1}: '{target_stream}': {e}", exc_info=True)
                break
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
                            
                            # *** LOUD LOGGING - STEP 4 (TRADING) ***
                            stream_name = message_parts[0].decode('utf-8', 'ignore') if message_parts else "NO_STREAM"
                            logger.critical(f"!!!!!!!!!! BABYSITTER RECEIVED (TRADING): Stream '{stream_name}', Parts: {len(message_parts)}, Size: {sum(len(p) for p in message_parts)} bytes !!!!!!!!!!")
                            
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
                            
                            # *** LOUD LOGGING - STEP 4 (SYSTEM) ***
                            stream_name = message_parts[0].decode('utf-8', 'ignore') if message_parts else "NO_STREAM"
                            logger.critical(f"!!!!!!!!!! BABYSITTER RECEIVED (SYSTEM): Stream '{stream_name}', Parts: {len(message_parts)}, Size: {sum(len(p) for p in message_parts)} bytes !!!!!!!!!!")
                            
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
                            
                            # *** LOUD LOGGING - STEP 4 (BULK) ***
                            stream_name = message_parts[0].decode('utf-8', 'ignore') if message_parts else "NO_STREAM"
                            logger.critical(f"!!!!!!!!!! BABYSITTER RECEIVED (BULK): Stream '{stream_name}', Parts: {len(message_parts)}, Size: {sum(len(p) for p in message_parts)} bytes !!!!!!!!!!")
                            
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
        Handles a received IPC message from ApplicationCore.
        This enhanced version handles both 2-part messages (stream, payload) and
        3-part messages (stream, payload, max_len) to allow for per-stream capping.
        """
        # --- START OF REPLACEMENT LOGIC ---
        if not (2 <= len(message_parts) <= 3):
            logger.warning(f"IPCDataRecv ({socket_origin_name}): Malformed message with {len(message_parts)} parts. Expected 2 or 3. Discarding.")
            return

        try:
            target_redis_stream = message_parts[0].decode('utf-8')
            data_json_string = message_parts[1].decode('utf-8')

            # Check for the optional 3rd part: max_stream_len
            stream_maxlen = REDIS_STREAM_MAXLEN_APPROX # Use global default
            if len(message_parts) == 3:
                # A specific cap was provided by the producer
                try:
                    specific_maxlen = int(message_parts[2].decode('utf-8'))
                    if specific_maxlen > 0:
                        stream_maxlen = specific_maxlen
                        if stream_maxlen < 500: # Log only for small caps
                            logger.debug(f"Using specific MAXLEN={stream_maxlen} for stream '{target_redis_stream}'.")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid MAXLEN value '{message_parts[2]}' for stream '{target_redis_stream}'. Using default {stream_maxlen}.")
            
            # --- MULTI-LANE ARCHITECTURE: Route to appropriate lane queue ---
            processed_data_json_string = self._process_graduated_defense_flags(data_json_string)

            # Determine which lane this message belongs to based on socket origin
            lane_name = socket_origin_name  # socket_origin_name is already 'trading', 'system', or 'bulk'
            
            # Add to the appropriate lane queue
            try:
                queue = self.publish_queues[lane_name]
                condition = self.queue_conditions[lane_name]
                
                with condition:
                    queue.append((target_redis_stream, processed_data_json_string, stream_maxlen))
                    condition.notify()  # Wake up the specific worker for this lane
                    
            except KeyError:
                logger.error(f"Unknown lane '{lane_name}' for message. Using system lane as fallback.")
                # Fallback to system lane
                with self.queue_conditions['system']:
                    self.publish_queues['system'].append((target_redis_stream, processed_data_json_string, stream_maxlen))
                    self.queue_conditions['system'].notify()
            except Exception as e:
                logger.error(f"Error queuing message for lane '{lane_name}': {e}. Falling back to disk buffer.")
                if not self.disk_buffer.write(target_redis_stream, processed_data_json_string):
                    logger.critical(f"CRITICAL: DiskWriteFail for '{target_redis_stream}' from {socket_origin_name}. Data may be lost.")

        except Exception as e:
            logger.error(f"IPCDataRecv ({socket_origin_name}): Error processing message: {e}", exc_info=True)
    # --- END OF REPLACEMENT ---

    def _redis_publish_worker_loop(self, lane: str):
        """
        Multi-lane, pipelined Redis publish worker for a specific lane.
        """
        thread_name = threading.current_thread().name
        logger.info(f"Redis Publish Worker ({thread_name}) for lane '{lane}' started.")
        
        lane_queue = self.publish_queues[lane]
        lane_condition = self.queue_conditions[lane]
        
        # Pipelining configuration
        BATCH_SIZE = 100  # Increased batch size for maximum throughput
        
        while not self.stop_event.is_set():
            batch_to_publish = []
            
            with lane_condition:
                # Wait for messages or timeout
                while not lane_queue and not self.stop_event.is_set():
                    lane_condition.wait(timeout=1.0)
                
                if self.stop_event.is_set():
                    break
                
                # Collect a batch of messages from this lane's queue
                while len(batch_to_publish) < BATCH_SIZE and lane_queue:
                    batch_to_publish.append(lane_queue.popleft())

            if batch_to_publish:
                # Process the batch with pipelined Redis publishing
                if self._is_redis_healthy_with_reconnect():
                    try:
                        # Use Redis pipeline for efficient batch publishing
                        pipe = self.redis_client.pipeline(transaction=False)
                        for item in batch_to_publish:
                            if len(item) == 3:
                                target_stream, data_json, stream_maxlen = item
                            else:
                                target_stream, data_json = item
                                stream_maxlen = REDIS_STREAM_MAXLEN_APPROX
                            
                            pipe.xadd(target_stream, {'json_payload': data_json}, 
                                    maxlen=stream_maxlen, approximate=True)
                        
                        # Execute the entire batch in one round-trip
                        pipe.execute()
                        logger.info(f"!!!!!!!!!! BABYSITTER ('{lane}' lane): Published batch of {len(batch_to_publish)} messages to Redis. !!!!!!!!!!")
                        
                    except Exception as e:
                        logger.error(f"Redis pipeline execution failed for lane '{lane}': {e}. Buffering {len(batch_to_publish)} messages to disk.")
                        # If pipeline fails, buffer all items to disk
                        for item in batch_to_publish:
                            target_stream = item[0]
                            data_json = item[1]
                            self.disk_buffer.write(target_stream, data_json)
                else:
                    # Redis is down, buffer the entire batch to disk
                    logger.warning(f"Redis unavailable. Buffering batch of {len(batch_to_publish)} from lane '{lane}' to disk.")
                    for item in batch_to_publish:
                        target_stream = item[0]
                        data_json = item[1]
                        self.disk_buffer.write(target_stream, data_json)
        
        logger.info(f"Redis Publish Worker ({thread_name}) for lane '{lane}' stopped.")

    def _disk_buffer_replay_loop(self):
        logger.info("Disk Buffer Replay Loop started.")
        while not self.stop_event.is_set():
            processed_file_in_cycle = False
            if self._is_redis_healthy_with_reconnect(): # Ensure Redis is generally healthy before replay attempt
                # Check if any of the lane queues has space (use system lane as representative)
                with self.queue_locks['system']:
                    has_space = len(self.publish_queues['system']) < (self.publish_queues['system'].maxlen * 0.8)

                if has_space:
                    files_to_replay = self.disk_buffer.get_files_for_replay()
                    if files_to_replay:
                        oldest_file = files_to_replay[0]
                        logger.info(f"DiskReplay: Attempting file: {oldest_file}")
                        # Pass a lambda wrapper that includes the default max_len
                        publish_func = lambda stream, data: self._publish_to_redis_with_internal_retry(stream, data, REDIS_STREAM_MAXLEN_APPROX)
                        if self.disk_buffer.process_buffered_file(oldest_file, publish_func):
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
        with self.queue_lock: bs_payload["internal_publish_queue_size"] = len(self.internal_publish_queue)
        bs_payload["internal_publish_queue_capacity"] = self.internal_publish_queue.maxlen

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
            if not self._publish_to_redis_with_internal_retry(REDIS_STREAM_BABYSITTER_HEALTH, json.dumps(bs_wrapper), REDIS_STREAM_MAXLEN_APPROX):
                 logger.error(f"HealthMon: Failed to publish Babysitter health after retries.")
        except Exception as e: logger.error(f"HealthMon: Error publishing Babysitter health: {e}", exc_info=True)

        # --- 2. Redis Instance Health ---
        redis_payload = {"timestamp": current_time, "component_name": "RedisInstance", "redis_host": REDIS_HOST, "redis_port": REDIS_PORT}
        # Ping again specifically for this status, self.redis_client might have been reset by a publish failure
        if self._is_redis_healthy_with_reconnect():
            redis_payload["status"] = "CONNECTED_AND_HEALTHY"
            try:
                info = self.redis_client.info()
                redis_payload.update({k: info.get(k) for k in ["redis_version", "uptime_in_seconds", "connected_clients", "used_memory_human"]})
            except Exception as e_info: redis_payload["status"] = f"CONNECTED_INFO_ERROR: {str(e_info)[:100]}"
        else:
            redis_payload["status"] = "DISCONNECTED_OR_UNHEALTHY"
        
        try:
            redis_wrapper = {"metadata": {"eventId": f"{health_event_id_prefix}_redis", "correlationId": f"{health_event_id_prefix}_redis", 
                                          "timestamp_ns": time.perf_counter_ns(), "epoch_timestamp_s": current_time, 
                                          "eventType": "TESTRADE_REDIS_INSTANCE_HEALTH", "sourceComponent": "BabysitterHealthMon"}, 
                             "payload": redis_payload}
            if not self._publish_to_redis_with_internal_retry(REDIS_STREAM_REDIS_INSTANCE_HEALTH, json.dumps(redis_wrapper), REDIS_STREAM_MAXLEN_APPROX):
                logger.error(f"HealthMon: Failed to publish Redis instance health after retries.")
        except Exception as e: logger.error(f"HealthMon: Error publishing Redis instance health: {e}", exc_info=True)


    def _gui_command_consumer_loop(self):
        logger.info("Babysitter GUI Command Consumer Loop started.")
        stream_name = REDIS_STREAM_COMMANDS_FROM_GUI
        group_name = f"bs_gui_cmd_grp_{os.getpid()}"
        consumer_name = f"bs_gui_consumer_{os.getpid()}_{threading.get_ident()}"

        while not self.stop_event.is_set():
            if not self._is_redis_healthy_with_reconnect():
                logger.warning("GUICmdConsumer: Redis not healthy. Pausing.")
                self.stop_event.wait(REDIS_HEALTH_CHECK_INTERVAL_SEC)
                continue
            
            try:
                # Ensure the consumer group exists
                try:
                    self.redis_client.xgroup_create(name=stream_name, groupname=group_name, id='0', mkstream=True)
                except redis.exceptions.ResponseError as e:
                    if "BUSYGROUP" not in str(e).upper():
                        raise # Re-raise if it's not the expected "already exists" error

                # Read pending messages for this consumer. If we crashed, there might be old ones.
                messages = self.redis_client.xreadgroup(
                    groupname=group_name, consumername=consumer_name,
                    streams={stream_name: '0'}, count=100 
                )
                # If no pending messages, read new messages
                if not messages:
                    messages = self.redis_client.xreadgroup(
                        groupname=group_name, consumername=consumer_name,
                        streams={stream_name: '>'}, count=10, block=1000
                    )
                
                if not messages:
                    continue

                for _stream, stream_messages in messages:
                    ids_to_ack = []
                    for msg_id, msg_data in stream_messages:
                        try:
                            # --- Start of Hardened Processing Block ---
                            cmd_wrapper_json_str = msg_data.get(b'json_payload') or msg_data.get(b'data')
                            if not cmd_wrapper_json_str:
                                logger.error(f"GUICmdConsumer: Bad Msg {msg_id}: Missing 'json_payload'. Discarding.")
                                ids_to_ack.append(msg_id)
                                continue

                            cmd_wrapper = json.loads(cmd_wrapper_json_str)
                            
                            actual_cmd_payload = cmd_wrapper.get("payload")
                            if not isinstance(actual_cmd_payload, dict):
                                logger.error(f"GUICmdConsumer: Bad Msg {msg_id}: Payload is not a dictionary. Discarding. Payload: {actual_cmd_payload}")
                                ids_to_ack.append(msg_id)
                                continue

                            target_svc = actual_cmd_payload.get("target_service")
                            cmd_type = actual_cmd_payload.get("command_type")

                            if not isinstance(target_svc, str) or not isinstance(cmd_type, str):
                                logger.error(f"GUICmdConsumer: Bad Msg {msg_id}: Invalid target_service or command_type. Discarding. Msg: {actual_cmd_payload}")
                                ids_to_ack.append(msg_id)
                                continue
                            
                            # --- Logic to forward the command ---
                            socket_to_use = None
                            if target_svc == "CORE_SERVICE": socket_to_use = self.core_command_push_socket
                            elif target_svc == "OCR_PROCESS": socket_to_use = self.ocr_command_push_socket
                            
                            if socket_to_use:
                                # We re-serialize the inner payload to send to the target process
                                inner_payload_json_str = json.dumps(actual_cmd_payload)
                                socket_to_use.send_multipart(
                                    [cmd_type.encode('utf-8'), inner_payload_json_str.encode('utf-8')],
                                    flags=zmq.DONTWAIT
                                )
                                logger.info(f"GUICmdConsumer: Forwarded '{cmd_type}' to '{target_svc}'.")
                            else:
                                logger.warning(f"GUICmdConsumer: No route for target '{target_svc}'. Discarding command '{cmd_type}'.")
                            
                            # If we successfully processed or decided to discard, we ACK the message.
                            ids_to_ack.append(msg_id)
                            # --- End of Hardened Processing Block ---

                        except zmq.Again:
                            # ZMQ buffer full - don't ACK this message, it will be retried
                            logger.warning(f"GUICmdConsumer: ZMQ HWM hit for msg {msg_id}. Will retry later.")
                            break # Stop processing this batch to avoid message reordering
                        except Exception as e:
                            # This is the poison pill handler.
                            logger.error(f"GUICmdConsumer: CRITICAL - Unhandled exception processing msg {msg_id}. This is a POISON PILL. Discarding. Error: {e}", exc_info=True)
                            # ACK the poison pill message so we don't process it again.
                            ids_to_ack.append(msg_id)

                    # After processing a batch, ACK all handled messages
                    if ids_to_ack:
                        self.redis_client.xack(stream_name, group_name, *ids_to_ack)

            except Exception as e_outer:
                logger.error(f"GUICmdConsumer: Outer loop error: {e_outer}", exc_info=True)
                self.redis_client = None # Force reconnect on next loop
                self.stop_event.wait(5.0)
        logger.info("Babysitter GUI Command Consumer Loop stopped.")


    def start(self):
        logger.info(f"Starting RedisBabysitterService (PID: {os.getpid()})...")
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
        # --- MULTI-LANE: Create dedicated worker for each lane ---
        for lane_name in self.publish_queues.keys():
            thread_configs.append((f"BabysitterRedisPub-{lane_name}", lambda ln=lane_name: self._redis_publish_worker_loop(ln)))

        for name, target in thread_configs:
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()
        logger.info(f"RedisBabysitterService started with {len(self.threads)} threads.")

    def shutdown_service(self):
        logger.info("Shutting down RedisBabysitterService...")
        self.stop_event.set()
        with self.queue_lock: self.queue_condition.notify_all()

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
        
        if self.redis_client:
            try: self.redis_client.close()
            except Exception as e: logger.error(f"Error closing Redis client: {e}")
        
        if hasattr(self, 'disk_buffer'): self.disk_buffer.close()
        logger.info("RedisBabysitterService shut down complete.")

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
        if _babysitter_instance and not _babysitter_instance.stop_event.is_set():
             # This means loop exited for reasons other than shutdown_service being fully called by signal
             logger.info("Stop event was not set, calling shutdown_service explicitly.")
             _babysitter_instance.shutdown_service()
        elif _babysitter_instance:
             logger.info("Stop event was set, shutdown_service likely already called or in progress.")
        logger.info(f"--- RedisBabysitterService (PID: {os.getpid()}) finished ---")

if __name__ == "__main__":
    main()
