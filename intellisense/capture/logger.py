"""
IntelliSense Correlation Logger

This module provides the CorrelationLogger class for thread-safe, asynchronous
correlation log writing. It manages writing events from different senses to their
respective JSON Lines (.jsonl) files within a given session path.
"""

import logging
import json
import os
import queue  # Using standard library queue
import threading
import time  # For epoch and perf_counter timestamps
import base64  # For image data decoding
from typing import Optional, Dict, Any, IO, TYPE_CHECKING

# Import convenience function from utils
from .utils import get_next_global_sequence_id, reset_global_sequence_id_generator

# Import the interface this class implements
from .interfaces import ICorrelationLogger

# LAZY IMPORT SOLUTION: Break circular dependencies with dynamic loading
if TYPE_CHECKING:
    from intellisense.core.internal_events import CorrelationLogEntryEvent
    from intellisense.core.internal_event_bus import InternalIntelliSenseEventBus
    from intellisense.core.types import ProcessedImageFrameTimelineEvent
    from core.events import CorrelationLoggerProtocol
else:
    # Import protocol at runtime for implementation
    try:
        from core.events import CorrelationLoggerProtocol
    except ImportError:
        # Fallback if protocol not available
        class CorrelationLoggerProtocol:
            pass

logger = logging.getLogger(__name__)


class CorrelationLogger(ICorrelationLogger, CorrelationLoggerProtocol):
    """
    Thread-safe, asynchronous correlation log writer.
    Manages writing events from different senses to their respective
    JSON Lines (.jsonl) files within a given session path.
    """
    # Max items in queue before log_event might block or warn (if put_nowait used extensively)
    DEFAULT_MAX_QUEUE_SIZE = 100000
    # How often the writer thread checks the queue if it was empty
    DEFAULT_WRITER_IDLE_TIMEOUT_S = 0.1
    # How long to wait for queue to empty on shutdown
    DEFAULT_SHUTDOWN_JOIN_TIMEOUT_S = 5.0

    def __init__(self, session_data_path: str,
                 internal_event_bus: Optional['InternalIntelliSenseEventBus'] = None, # NEW
                 max_queue_size: Optional[int] = None):
        """
        Initializes the CorrelationLogger.

        Args:
            session_data_path: Base directory path for the current capture session.
                               Log files will be created within this directory.
            internal_event_bus: Optional internal event bus for publishing log entry events.
            max_queue_size: Optional maximum size for the internal event queue.
        """
        if not os.path.exists(session_data_path):
            try:
                os.makedirs(session_data_path, exist_ok=True)
            except OSError as e:
                logger.error(f"Failed to create session data path '{session_data_path}': {e}")
                raise

        self.session_path = session_data_path
        self._internal_event_bus = internal_event_bus # Store internal bus
        self._max_queue_size = max_queue_size if max_queue_size is not None else self.DEFAULT_MAX_QUEUE_SIZE
        self._log_queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=self._max_queue_size)

        # Lazy import caching - will be loaded when needed
        self._CorrelationLogEntryEvent = None

        self._writer_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()  # Signals the writer thread to stop

        self._file_handles_lock = threading.Lock()
        self._file_handles: Dict[str, IO[str]] = {}  # filepath -> file_handle

        reset_global_sequence_id_generator()  # Reset global sequence for each new logger instance/session
        logger.info(f"CorrelationLogger initialized for session path: {self.session_path}")
        self._start_writer_thread()

    def _get_log_filepath(self, source_sense: str) -> str:
        """Determines the filepath for a given source sense."""
        sense_filename = f"{source_sense.lower()}_correlation.jsonl"
        return os.path.join(self.session_path, sense_filename)

    def log_event(self, source_sense: str, event_payload: Dict[str, Any], source_component_name: Optional[str] = None, perf_timestamp_ns_override: Optional[int] = None, **kwargs) -> None:
        """
        Queues an event for asynchronous logging.
        Constructs the full log entry with timestamps and sequence ID.

        Args:
            source_sense: The sense originating the event (e.g., "OCR", "PRICE", "BROKER").
            event_payload: The specific data for the event from that sense.
                           This dictionary should include any 'processing_latency_ns' if measured by the source.
            source_component_name: Optional name of the component that generated the data.
            perf_timestamp_ns_override: Optional performance counter timestamp to use instead of a new one.
            **kwargs: Additional fields to include at the top level of the log entry.
                     For BrokerTimelineEvent, this includes original_order_id and broker_order_id_on_event.
        """
        if self._shutdown_event.is_set():
            logger.warning(f"CorrelationLogger is shutting down. Dropping event from {source_sense}.")
            return
        
        log_entry = {
            'global_sequence_id': get_next_global_sequence_id(),
            'perf_timestamp_ns': perf_timestamp_ns_override if perf_timestamp_ns_override is not None else time.perf_counter_ns(),
            'epoch_timestamp_s': time.time(),
            'source_sense': source_sense.upper(),
            'event_payload': event_payload  # This is the dict passed from the listener
        }
        if source_component_name:  # Add if provided
            log_entry['source_component_name'] = source_component_name

        # Add any additional fields from kwargs (e.g., original_order_id, broker_order_id_on_event for BrokerTimelineEvent)
        for key, value in kwargs.items():
            if value is not None:  # Only add non-None values
                log_entry[key] = value

        try:
            self._log_queue.put_nowait(log_entry)  # Use put_nowait to avoid blocking producer
        except queue.Full:
            logger.warning(f"Correlation log queue is full (max_size: {self._max_queue_size}). Dropping event from {source_sense}.")
        except Exception as e:  # Catch other potential queue errors
            logger.error(f"Unexpected error queuing log event: {e}", exc_info=True)

    def log_processed_image_event(self, image_timeline_event: 'ProcessedImageFrameTimelineEvent') -> None:
        """
        Queues a processed image event for asynchronous logging.
        Saves image bytes to disk and logs the file path along with metadata.
        """
        if self._shutdown_event.is_set():
            logger.warning(f"CorrelationLogger is shutting down. Dropping processed image event GSI: {image_timeline_event.global_sequence_id}.")
            return

        # Import here to avoid circular dependencies
        from intellisense.core.types import ProcessedImageFrameData

        if not isinstance(image_timeline_event.data, ProcessedImageFrameData):
            logger.error(f"CorrelationLogger: log_processed_image_event called with incorrect data type. Expected ProcessedImageFrameData, got {type(image_timeline_event.data)}")
            return

        # --- Image Saving Logic ---
        image_payload: ProcessedImageFrameData = image_timeline_event.data
        image_bytes_b64_str = None

        # PDS should populate image_payload.image_bytes_b64 directly from Redis payload
        if image_payload.image_bytes_b64:
            image_bytes_b64_str = image_payload.image_bytes_b64
        else:
            logger.warning(f"CorrelationLogger: No 'image_bytes_b64' found in ProcessedImageFrameTimelineEvent.data.raw_redis_payload for GSI {image_timeline_event.global_sequence_id}. Cannot save image.")
            # Still log the metadata entry, but image_file_path will be None

        saved_image_path: Optional[str] = None
        if image_bytes_b64_str:
            try:
                image_bytes = base64.b64decode(image_bytes_b64_str)

                # Define images directory within the session path
                images_dir = os.path.join(self.session_path, "processed_images")
                os.makedirs(images_dir, exist_ok=True)

                # Use master_correlation_id for filename if available, else GSI
                filename_id = image_payload.master_correlation_id or image_timeline_event.correlation_id or str(image_timeline_event.global_sequence_id)
                image_filename = f"{filename_id}.{image_payload.image_format_from_payload.lower() if image_payload.image_format_from_payload else 'png'}"
                full_image_path = os.path.join(images_dir, image_filename)

                with open(full_image_path, 'wb') as img_file:
                    img_file.write(image_bytes)
                saved_image_path = os.path.join("processed_images", image_filename)  # Relative path for log
                logger.debug(f"CorrelationLogger: Saved processed image for GSI {image_timeline_event.global_sequence_id} to {full_image_path}")
            except Exception as e:
                logger.error(f"CorrelationLogger: Failed to save image for GSI {image_timeline_event.global_sequence_id}: {e}", exc_info=True)

        # Update the payload to be logged with the file path
        # Set the relative path in the data object before logging
        image_payload.image_file_path_relative = saved_image_path

        # Use the specialized logging method that excludes raw image bytes
        log_event_payload = image_payload.to_dict_for_log()

        # Construct the main log entry for the .jsonl file
        log_entry = {
            'global_sequence_id': image_timeline_event.global_sequence_id,
            'perf_timestamp_ns': image_timeline_event.perf_counter_timestamp,  # This is T0
            'epoch_timestamp_s': image_timeline_event.epoch_timestamp_s,       # This is T0 epoch
            'source_sense': image_timeline_event.source_sense,
            'event_type': image_timeline_event.event_type,
            'correlation_id': image_timeline_event.correlation_id,  # This is master_correlation_id
            'source_info': image_timeline_event.source_info,
            # component_processing_latency_ns is for the PDS component, not image preprocessing
            'component_processing_latency_ns': image_timeline_event.processing_latency_ns,
            'event_payload': log_event_payload  # This now contains image_file_path and other metadata
        }

        try:
            self._log_queue.put_nowait(log_entry)
        except queue.Full:
            logger.warning(f"Correlation log queue is full. Dropping processed image event GSI: {image_timeline_event.global_sequence_id}.")
        except Exception as e:
            logger.error(f"Unexpected error queuing processed image log event: {e}", exc_info=True)

    def _start_writer_thread(self) -> None:
        """Starts the background writer thread."""
        if self._writer_thread is not None and self._writer_thread.is_alive():
            logger.warning("CorrelationLogger writer thread already running.")
            return

        self._shutdown_event.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_worker,
            name="CorrelationLogWriterThread",
            daemon=True  # Daemon thread will exit when main program exits
        )
        self._writer_thread.start()
        logger.info("CorrelationLogger writer thread started.")

    def _writer_worker(self) -> None:
        """Background worker for writing correlation logs from the queue."""
        logger.info("CorrelationLogWriterThread worker loop started.")
        while not self._shutdown_event.is_set():
            try:
                # Get log entry with timeout to allow checking shutdown_event periodically
                log_entry = self._log_queue.get(timeout=self.DEFAULT_WRITER_IDLE_TIMEOUT_S)
                self._write_log_entry_to_file(log_entry)
                self._log_queue.task_done()
            except queue.Empty:
                # Queue was empty, loop and check shutdown_event again
                continue
            except Exception as e:
                # Log error but continue running unless it's a critical file I/O issue
                logger.error(f"Error in CorrelationLogWriterThread worker: {e}", exc_info=True)
                # Potentially add more sophisticated error handling here, e.g., if disk is full.
                time.sleep(1)  # Sleep briefly after an error

        # Shutdown signaled, process any remaining items in the queue
        logger.info("CorrelationLogWriterThread: Shutdown signaled. Processing remaining queue items...")
        while not self._log_queue.empty():
            try:
                log_entry = self._log_queue.get_nowait()
                self._write_log_entry_to_file(log_entry)
                self._log_queue.task_done()
            except queue.Empty:
                break  # Should not happen if not self._log_queue.empty()
            except Exception as e:
                logger.error(f"Error processing remaining queue item during shutdown: {e}", exc_info=True)
        
        logger.info("CorrelationLogWriterThread worker loop finished.")
        self._close_all_file_handles()

    def _write_log_entry_to_file(self, log_entry: Dict[str, Any]) -> None:
        """Writes a single log entry to the appropriate sense-specific file."""
        source_sense = log_entry.get('source_sense', 'UNKNOWN_SENSE').upper()
        filepath = self._get_log_filepath(source_sense)

        with self._file_handles_lock:
            if filepath not in self._file_handles:
                try:
                    # Ensure directory exists one last time (should be created by __init__)
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    self._file_handles[filepath] = open(filepath, 'a', encoding='utf-8')  # Append mode
                    logger.info(f"Opened correlation log file: {filepath}")
                except IOError as e:
                    logger.error(f"Failed to open correlation log file {filepath}: {e}")
                    return  # Cannot write

            file_handle = self._file_handles[filepath]

        try:
            json_line = json.dumps(log_entry) + '\n'
            file_handle.write(json_line)
            # Flushing frequently can impact performance, but ensures data is written.
            # Consider batching flushes or flushing based on time/queue size.
            # For now, flush per write for immediate visibility during development.
            file_handle.flush()
            # After successful write, publish to internal bus if available
            if self._internal_event_bus:
                # Lazy import to break circular dependency
                if self._CorrelationLogEntryEvent is None:
                    from intellisense.core.internal_events import CorrelationLogEntryEvent
                    self._CorrelationLogEntryEvent = CorrelationLogEntryEvent
                    logger.debug("Lazy loaded CorrelationLogEntryEvent for internal event publishing")

                event = self._CorrelationLogEntryEvent(log_entry=log_entry)
                logger.debug(f"CorrelationLogger: Attempting to publish CorrelationLogEntryEvent (GSI: {log_entry.get('global_sequence_id')}) on bus ID: {id(self._internal_event_bus)}")
                self._internal_event_bus.publish(event)
        except IOError as e:
            logger.error(f"IOError writing to correlation log file {filepath}: {e}")
            # Potentially re-queue or handle error more robustly
        except TypeError as e:  # If log_entry is not JSON serializable
            logger.error(f"TypeError: Log entry not JSON serializable for {filepath}. Entry: {log_entry}. Error: {e}", exc_info=True)
        except Exception as e: # Catch-all for safety
            logger.error(f"Unexpected error in _write_log_entry_to_file: {e}", exc_info=True)

    def _close_all_file_handles(self):
        """Closes all open file handles."""
        with self._file_handles_lock:
            logger.info(f"Closing {len(self._file_handles)} correlation log file handle(s)...")
            for filepath, handle in self._file_handles.items():
                try:
                    if not handle.closed:
                        handle.flush()
                        handle.close()
                        logger.info(f"Closed correlation log file: {filepath}")
                except Exception as e:
                    logger.error(f"Error closing correlation log file {filepath}: {e}", exc_info=True)
            self._file_handles.clear()

    def close(self) -> None:
        """Signals the writer thread to shut down and waits for it to complete."""
        logger.info("CorrelationLogger close requested.")
        if self._shutdown_event.is_set():
            logger.info("CorrelationLogger already closing or closed.")
            return

        self._shutdown_event.set()  # Signal writer thread to stop
        
        # Optionally, put a sentinel on the queue to wake up the writer if it's blocking on get()
        # try:
        #     self._log_queue.put_nowait(None)  # Sentinel object
        # except queue.Full:
        #     pass  # If full, writer will eventually see shutdown_event

        if self._writer_thread and self._writer_thread.is_alive():
            logger.info(f"Waiting for CorrelationLogWriterThread to join (timeout: {self.DEFAULT_SHUTDOWN_JOIN_TIMEOUT_S}s)...")
            self._writer_thread.join(timeout=self.DEFAULT_SHUTDOWN_JOIN_TIMEOUT_S)
            if self._writer_thread.is_alive():
                logger.warning("CorrelationLogWriterThread did not join cleanly within timeout.")
        
        self._close_all_file_handles()  # Ensure handles are closed even if thread join failed
        logger.info("CorrelationLogger closed.")

    def get_queue_size(self) -> int:
        """Returns the current size of the internal log queue."""
        return self._log_queue.qsize()

    def get_session_path(self) -> Optional[str]:
        """Returns the file system path for the current logging session."""
        return self.session_path

    def is_active(self) -> bool:
        """Returns True if the logger is currently active and logging."""
        return not self._shutdown_event.is_set()
