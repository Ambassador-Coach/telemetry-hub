#!/usr/bin/env python3
"""
SessionRecorder (Tape Deck)

Records multipart binary events to an append-only session file (.sdr).
Thread-safe, low-overhead writer compatible with the Babysitter service.

Binary block layout per event:
  [Total Block Bytes: uint64][Timestamp ns: uint64][Num Parts: uint8]
  repeat Num Parts times: [Part Len: uint32][Part Bytes]
"""

import os
import threading
import time
import struct
import shutil
from datetime import datetime
from typing import List, Optional, Any
import logging

logger = logging.getLogger(__name__)


class SessionRecorder:
    HEADER_FORMAT = '<QQB'   # total_len(uint64), timestamp_ns(uint64), num_parts(uint8)
    HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

    PART_LEN_FORMAT = '<I'   # part_len(uint32)
    PART_LEN_SIZE = struct.calcsize(PART_LEN_FORMAT)

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.lock = threading.Lock()
        self.session_file: Optional[Any] = None
        self.current_filepath: Optional[str] = None
        self.is_active = False
        self.bytes_written_this_session = 0
        self.last_storage_check = 0.0
        self.storage_check_interval = 30.0
        logger.info(f"SessionRecorder initialized. Output directory: {self.output_dir}")
        self._check_storage_space()

    def start_new_session(self) -> bool:
        self.stop()
        with self.lock:
            try:
                os.makedirs(self.output_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                session_filename = f"session_{timestamp}.sdr"
                self.current_filepath = os.path.join(self.output_dir, session_filename)
                self.session_file = open(self.current_filepath, "ab")
                self.is_active = True
                self.bytes_written_this_session = 0
                logger.info(f"Tape Deck: New session started at {self.current_filepath}")
                return True
            except IOError as e:
                logger.critical(f"Tape Deck: Cannot open session file {self.current_filepath}: {e}")
                self.is_active = False
                return False

    def record_event(self, message_parts: List[bytes]):
        if not self.is_active or not self.session_file:
            return
        try:
            timestamp_ns = time.time_ns()
            num_parts = len(message_parts)

            variable = bytearray()
            for part in message_parts:
                variable.extend(struct.pack(self.PART_LEN_FORMAT, len(part)))
                variable.extend(part)

            total_block_length = self.HEADER_SIZE + len(variable)
            header = struct.pack(self.HEADER_FORMAT, total_block_length, timestamp_ns, num_parts)

            with self.lock:
                if self.session_file and not self.session_file.closed:
                    self.session_file.write(header)
                    self.session_file.write(variable)
                    self.bytes_written_this_session += total_block_length
                    self._periodic_storage_check()
                else:
                    logger.warning("Tape Deck: Attempted write to closed session file.")
        except Exception as e:
            logger.error(f"Tape Deck: Write failed for {self.current_filepath}: {e}", exc_info=True)

    def stop(self):
        with self.lock:
            if self.session_file and not self.session_file.closed:
                try:
                    self.session_file.flush()
                    os.fsync(self.session_file.fileno())
                    self.session_file.close()
                    logger.info(f"Tape Deck: Session file closed: {self.current_filepath}")
                except Exception as e:
                    logger.error(f"Tape Deck: Error during close: {e}")
                finally:
                    self.session_file = None
                    self.current_filepath = None
            self.is_active = False

    def _check_storage_space(self):
        try:
            if os.path.exists(self.output_dir):
                total, used, free = shutil.disk_usage(self.output_dir)
                free_gb = free / (1024**3)
                total_gb = total / (1024**3)
                if free_gb < 10:
                    logger.critical(f"Tape Deck: CRITICAL - Only {free_gb:.1f}GB free at {self.output_dir}")
                elif free_gb < 50:
                    logger.warning(f"Tape Deck: WARNING - Only {free_gb:.1f}GB free at {self.output_dir}")
                else:
                    logger.info(f"Tape Deck: Storage OK - {free_gb:.1f}GB free of {total_gb:.1f}GB")
            else:
                logger.warning(f"Tape Deck: Output directory not found yet: {self.output_dir}")
        except Exception as e:
            logger.error(f"Tape Deck: Storage check failed: {e}")

    def _periodic_storage_check(self):
        now = time.time()
        if now - self.last_storage_check > self.storage_check_interval:
            self._check_storage_space()
            self.last_storage_check = now

