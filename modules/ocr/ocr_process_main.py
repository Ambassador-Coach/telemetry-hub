"""
OCR Process Main Module

This module contains the entry point for the OCR process that runs in a separate
multiprocessing.Process. It handles the initialization of the OCR service and
communication with the parent process via a pipe.
"""

import logging
import os
import threading
import time
from multiprocessing.connection import Connection
from typing import Dict, Any, Optional

# Use relative imports for modules within the same package (modules.ocr)
from .ocr_service import OCRService
from interfaces.ocr.services import OcrConfigData  # Import the dataclass from interfaces
from .data_types import OCRParsedData, OCRSnapshot

# Logger for this specific process module
logger = logging.getLogger(__name__)  # Will be configured by setup_ocr_process_logging


def setup_ocr_process_logging(log_base_dir: str, process_name: Optional[str] = None):
    """
    Sets up improved logging for the OCR process with better formatting and levels.

    Args:
        log_base_dir: Base directory for log files (unused - disk logging disabled)
        process_name: Name of the process for log file naming (unused - disk logging disabled)
    """
    if process_name is None:
        process_name = threading.current_thread().name  # Gets the Process name if set
        if not process_name or process_name == "MainThread":  # Fallback if name not set or running directly
            process_name = f"OCRProcessPID-{os.getpid()}"  # More unique fallback

    # Get the root logger for this process
    proc_root_logger = logging.getLogger()
    # Remove any handlers inherited from the parent process if any (multiprocessing can sometimes inherit)
    for handler in proc_root_logger.handlers[:]:
        proc_root_logger.removeHandler(handler)

    # Set logging level to INFO for better visibility of important events
    proc_root_logger.setLevel(logging.INFO)

    # Add improved console handler with better formatting
    formatter = logging.Formatter(
        '[%(asctime)s] [OCR-%(process)d] [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)
    proc_root_logger.addHandler(stream_handler)

    # Log initialization message
    proc_root_logger.info(f"OCR process logging initialized for {process_name}")


class OCRProcessInternalObserver:
    """
    Observer for OCRService that sends OCR results over the pipe to the parent process.
    """
    _should_stop_processing = False  # Flag to stop if pipe breaks

    def __init__(self, pipe_to_parent: Connection):
        """
        Initialize the observer with the pipe connection to the parent process.

        Args:
            pipe_to_parent: The child end of the pipe to send data to the parent
        """
        self.pipe_to_parent = pipe_to_parent
        self.stop_event = threading.Event()

    def on_status_update(self, message: str):
        """
        Log status updates from OCRService within this process's log.

        Args:
            message: Status message from OCRService
        """
        logger.info(f"OCRService Status (Internal): {message}")

    def on_ocr_result(self, data_package_from_ocr_service: Dict[str, Any]):
        """
        Handle OCR results from OCRService and send them over the pipe.

        Args:
            data_package_from_ocr_service: OCR data package from OCRService
        """
        if self._should_stop_processing:
            logger.warning("Pipe broken, not sending OCR result.")
            return  # Don't try to send if pipe broke

        try:
            # The data package is already what we need. Just wrap it.
            # No transformation needed here anymore.
            self.pipe_to_parent.send({
                "type": "ocr_parsed_data",
                "data": data_package_from_ocr_service
            })
        except (BrokenPipeError, EOFError):
            logger.error("Pipe to ApplicationCore failed. OCR process will stop sending.")
            self._should_stop_processing = True
            self.stop_event.set()
        except Exception as e:
            logger.error(f"Error sending OCR data via pipe: {e}", exc_info=True)


def run_ocr_service_process(
    child_pipe_conn: Connection,
    ocr_config: OcrConfigData,  # CRITICAL: Expect the dataclass object, not a dict
    log_base_dir: str,
    telemetry_config: Optional[Dict[str, Any]] = None  # NEW: Telemetry config for CorrelationLogger
):
    """Main function for the OCR process."""
    process_name = f"OCRChildProcess-{os.getpid()}"
    setup_ocr_process_logging(log_base_dir, process_name)
    logger.info(f"OCR Service Process (PID: {os.getpid()}) started.")

    ocr_process_observer = OCRProcessInternalObserver(pipe_to_parent=child_pipe_conn)
    ocr_service_instance = None
    zmq_command_listener = None
    correlation_logger_instance = None
    telemetry_service_instance = None

    try:
        # CRITICAL FIX: Two-step initialization - TelemetryService + Adapter
        if telemetry_config:
            try:
                # Step 1: Create config wrapper for TelemetryService
                class ConfigWrapper:
                    def __init__(self, config_dict):
                        for key, value in config_dict.items():
                            setattr(self, key, value)
                
                config_wrapper = ConfigWrapper(telemetry_config)
                
                # Step 2: Create Redis-direct IPC client for TelemetryService
                class OptimizedRedisDirectIPCClient:
                    def __init__(self, redis_config):
                        self.redis_config = redis_config
                        self._redis_client = None
                        self._connect_to_redis()
                    
                    def _connect_to_redis(self):
                        try:
                            import redis
                            self._redis_client = redis.Redis(
                                host=self.redis_config['REDIS_HOST'],
                                port=self.redis_config['REDIS_PORT'],
                                db=self.redis_config['REDIS_DB'],
                                password=self.redis_config.get('REDIS_PASSWORD'),
                                socket_connect_timeout=self.redis_config.get('redis_connect_timeout_sec', 2.0),
                                socket_timeout=self.redis_config.get('redis_socket_timeout_sec', 2.0),
                                retry_on_timeout=self.redis_config.get('redis_retry_on_timeout', True)
                            )
                            # Test connection
                            self._redis_client.ping()
                            logger.info("OCR Process: Direct Redis connection established")
                        except Exception as e:
                            logger.error(f"OCR Process: Failed to connect to Redis: {e}")
                            self._redis_client = None
                    
                    def send_data(self, stream: str, message: str) -> bool:
                        """Send JSON string directly to Redis stream"""
                        if not self._redis_client:
                            logger.warning(f"OCR Process: No Redis connection - dropping message to {stream}")
                            return False
                        
                        try:
                            import json
                            import time
                            # Parse the JSON message to extract the formatted data
                            data_dict = json.loads(message)
                            
                            # Add OCR process timestamp
                            data_dict['ocr_process_timestamp'] = time.time()
                            
                            # Send the formatted JSON as a single field to Redis
                            redis_fields = {'data': json.dumps(data_dict)}
                            result = self._redis_client.xadd(stream, redis_fields)
                            logger.debug(f"OCR Process: Sent to Redis stream {stream}: {result}")
                            return True
                        except json.JSONDecodeError as e:
                            logger.error(f"OCR Process: Invalid JSON in message for {stream}: {e}")
                            return False
                        except Exception as e:
                            logger.error(f"OCR Process: Error sending to Redis stream {stream}: {e}")
                            return False
                    
                    def send_data_dict(self, stream: str, data_dict: dict) -> bool:
                        """Send dict by delegating to send_data with JSON string"""
                        try:
                            import json
                            # Convert dict to JSON string and use the standard send_data method
                            json_message = json.dumps(data_dict)
                            return self.send_data(stream, json_message)
                        except Exception as e:
                            logger.error(f"OCR Process: Error converting dict to JSON for stream {stream}: {e}")
                            return False
                
                redis_direct_client = OptimizedRedisDirectIPCClient(telemetry_config)
                
                # Step 3: Create the main TelemetryService with config wrapper and Redis-direct client
                from core.services.telemetry_streaming_service import TelemetryStreamingService
                from modules.correlation_logging.loggers import TelemetryCorrelationLogger
                
                telemetry_service_instance = TelemetryStreamingService(
                    ipc_client=redis_direct_client,  # Use Redis-direct client for OCR process
                    config=config_wrapper
                )
                telemetry_service_instance.start()
                logger.info("TelemetryService initialized successfully in OCR process")
                
                # Step 4: Create the adapter, giving it the service instance
                correlation_logger_instance = TelemetryCorrelationLogger(
                    telemetry_service=telemetry_service_instance
                )
                logger.info("TelemetryCorrelationLogger (Adapter) created successfully")
                
            except Exception as e:
                logger.error(f"Failed to initialize telemetry/correlation systems in OCR process: {e}", exc_info=True)
                correlation_logger_instance = None
                telemetry_service_instance = None
        else:
            logger.warning("No telemetry_config provided - OCRService will run without CorrelationLogger")
            correlation_logger_instance = None

        # The ocr_config is now the correct type, no mapping or filtering needed!
        ocr_service_instance = OCRService(
            observer=ocr_process_observer,
            config=ocr_config,  # Pass the object directly
            pipe_to_parent=child_pipe_conn,  # Ensure pipe is passed for status updates
            correlation_logger=correlation_logger_instance  # CRITICAL FIX: Pass the CorrelationLogger
        )
        logger.info("OCRService instance created in child process.")

        ocr_service_instance.start_ocr()
        logger.info("OCR processing started.")
        
        # ZMQ command listener setup with configurable address
        try:
            from modules.ocr.ocr_zmq_command_listener import OCRZMQCommandListener
            # Build config dict with ZMQ address from OcrConfigData
            zmq_config = {
                'ocr_ipc_command_pull_address': ocr_config.ocr_ipc_command_pull_address,
                'initial_roi': ocr_config.initial_roi
            }
            zmq_command_listener = OCRZMQCommandListener(
                ocr_service_instance=ocr_service_instance,
                config_dict=zmq_config,  # Pass config with ZMQ address
                pipe_to_parent=child_pipe_conn,
                logger_instance=logger
            )
            zmq_command_listener.start()
            logger.info(f"OCR ZMQ command listener started on {ocr_config.ocr_ipc_command_pull_address}")
        except Exception as e:
            logger.error(f"Failed to start OCR ZMQ command listener: {e}. OCR commands will not be available.")
            # Attempt to send error message via pipe
            try:
                child_pipe_conn.send({
                    "type": "ocr_error_event",
                    "data": {"error": f"ZMQ listener startup failed: {e}"}
                })
            except Exception:
                pass  # Pipe might be broken

        # Main loop to keep the process alive
        while not ocr_process_observer.stop_event.is_set():
            time.sleep(0.5)

    except Exception as e:
        logger.critical(f"OCR Process: Unhandled exception during service run: {e}", exc_info=True)
    finally:
        logger.info("OCR Service Process shutting down...")
        
        # Improved shutdown order: OCR service first, then ZMQ listener, then pipe
        if ocr_service_instance:
            try:
                logger.info("Shutting down OCR service...")
                ocr_service_instance.shutdown()
                logger.info("OCR service shutdown completed")
            except Exception as e:
                logger.error(f"Error shutting down OCR service: {e}")
        
        if zmq_command_listener:
            try:
                logger.info("Shutting down ZMQ command listener...")
                zmq_command_listener.stop()
                logger.info("ZMQ command listener shutdown completed")
            except Exception as e:
                logger.error(f"Error stopping ZMQ command listener: {e}")
        
        # CRITICAL FIX: Shutdown TelemetryService (which handles CorrelationLogger)
        if telemetry_service_instance:
            try:
                logger.info("Shutting down TelemetryService...")
                telemetry_service_instance.stop()
                logger.info("TelemetryService shutdown completed")
            except Exception as e:
                logger.error(f"Error shutting down TelemetryService: {e}")
        
        # Close pipe last
        if not child_pipe_conn.closed:
            try:
                logger.info("Closing pipe connection...")
                child_pipe_conn.close()
                logger.info("Pipe connection closed")
            except Exception as e:
                logger.error(f"Error closing pipe connection: {e}")
        
        logger.info(f"OCR Service Process (PID: {os.getpid()}) shutdown completed gracefully.")