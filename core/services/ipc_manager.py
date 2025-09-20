"""
IPC Manager Service

This service manages all Inter-Process Communication (IPC) functionality, including:
- BabysitterIPCClient lifecycle management
- Redis message formatting and standardization
- Publishing data to Redis streams via Babysitter
- Circuit breaker patterns for resilient communication
- TANK mode support for IPC data dumping control

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import json
import time
import dataclasses
from typing import Optional, Dict, Any, Callable
from threading import Lock

from interfaces.core.services import ILifecycleService
from interfaces.core.services import IIPCManager
from core.bulletproof_ipc_client import BulletproofBabysitterIPCClient, IPCInitializationError, PlaceholderMissionControlNotifier
from utils.thread_safe_uuid import get_thread_safe_uuid

logger = logging.getLogger(__name__)


class IPCManager(ILifecycleService, IIPCManager):
    """
    Manages all IPC communication with external services.
    
    This service encapsulates:
    - BabysitterIPCClient lifecycle
    - Message formatting and publishing
    - TANK mode data dumping control
    - Thread-safe operations
    """
    
    def __init__(self,
                 config_service: Any,
                 zmq_context: Optional[Any] = None,
                 mission_control_notifier: Optional[Any] = None):
        """
        Initialize the IPC Manager.
        
        Args:
            config_service: Global configuration object
            zmq_context: ZMQ context for IPC communication
            mission_control_notifier: Mission control notifier instance
        """
        self.logger = logger
        self.config = config_service
        self.zmq_context = zmq_context
        self.mission_control_notifier = mission_control_notifier or PlaceholderMissionControlNotifier(logger)
        
        # IPC Client
        self.babysitter_ipc_client: Optional[BulletproofBabysitterIPCClient] = None
        
        # Control flags
        self._is_running = False
        self._lock = Lock()
        
        # Use centralized mode detection
        from utils.testrade_modes import get_current_mode, requires_ipc_services, requires_external_publishing
        
        current_mode = get_current_mode()
        self.enable_ipc_data_dump = requires_external_publishing()  # Only in LIVE mode
        self.offline_mode = not requires_ipc_services()  # True in TANK_SEALED and TANK_BUFFERED
        
        log_mode = "ENABLED (LIVE mode)" if self.enable_ipc_data_dump else "DISABLED (TANK mode)"
        self.logger.info(f"IPC Data Dumping to Babysitter is {log_mode}.")
        self.logger.info(f"Current TESTRADE mode: {current_mode.value}")
        
    @property
    def is_ready(self) -> bool:
        """Check if the IPC manager is ready."""
        with self._lock:
            if self.offline_mode:
                return True  # Always ready in offline mode
            return self._is_running and self.babysitter_ipc_client is not None
    
    def start(self) -> None:
        """Start the IPC manager and initialize the BabysitterIPCClient."""
        with self._lock:
            if self._is_running:
                self.logger.warning("IPCManager already running")
                return
                
            self._is_running = True
            
            if self.offline_mode:
                self.logger.info("IPCManager starting in OFFLINE mode - no Babysitter connection")
                return
                
            # Check if ZMQ context is available
            if not self.zmq_context:
                self.logger.info("IPCManager starting without ZMQ context - will initialize client when context is provided")
                return
                
            # Initialize BabysitterIPCClient
            self._initialize_babysitter_client()
    
    def _initialize_babysitter_client(self) -> None:
        """Initialize the BulletproofBabysitterIPCClient with proper error handling."""
        try:
            self.logger.info("Initializing BulletproofBabysitterIPCClient...")
            self.babysitter_ipc_client = BulletproofBabysitterIPCClient(
                zmq_context=self.zmq_context,
                ipc_config=self.config,
                logger_instance=self.logger.getChild("BulletproofIPC"),
                mission_control_notifier=self.mission_control_notifier
            )
            self.logger.info("BulletproofBabysitterIPCClient initialized successfully")
            
        except IPCInitializationError as e:
            self.logger.critical(f"BulletproofBabysitterIPCClient FAILED to initialize: {e}", exc_info=True)
            self.babysitter_ipc_client = None
            
        except Exception as e:
            self.logger.critical(f"Generic error initializing BulletproofBabysitterIPCClient: {e}", exc_info=True)
            self.babysitter_ipc_client = None
    
    def stop(self) -> None:
        """Stop the IPC manager and cleanup resources."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            
            # Shutdown BabysitterIPCClient
            if self.babysitter_ipc_client:
                self.logger.info("Closing BabysitterIPCClient...")
                try:
                    self.babysitter_ipc_client.close()
                except Exception as e:
                    self.logger.error(f"Error closing BabysitterIPCClient: {e}", exc_info=True)
                self.babysitter_ipc_client = None
                
            self.logger.info("IPCManager stopped")
    
    def set_zmq_context(self, zmq_context: Any) -> None:
        """
        Set the ZMQ context for IPC communication.
        
        This method allows ApplicationCore to provide the ZMQ context after
        the IPCManager has been created via DI.
        
        Args:
            zmq_context: ZMQ context instance
        """
        with self._lock:
            self.zmq_context = zmq_context
            self.logger.info("ZMQ context set for IPCManager")
            
            # If we're running and don't have a client yet, try to initialize it
            if self._is_running and not self.babysitter_ipc_client and not self.offline_mode:
                self.logger.info("Attempting to initialize BabysitterIPCClient with new ZMQ context...")
                self._initialize_babysitter_client()
    
    def create_redis_message_json(self, payload: Dict[str, Any], event_type_str: str,
                                 correlation_id_val: Optional[str], source_component_name: str,
                                 causation_id_val: Optional[str] = None,
                                 origin_timestamp_s: Optional[float] = None) -> str:
        """
        Create a standardized Redis message JSON string with Golden Timestamp support.
        
        This follows the event sourcing pattern with proper correlation/causation tracking.
        
        Args:
            payload: The actual event data
            event_type_str: Type of the event (e.g., "TESTRADE_RAW_OCR_DATA")
            correlation_id_val: Master correlation ID for end-to-end tracing
            source_component_name: Name of the component publishing the event
            causation_id_val: ID of the event that caused this event (optional)
            origin_timestamp_s: Golden Timestamp - when the data originally occurred (optional)
            
        Returns:
            JSON string of the formatted message
        """
        if correlation_id_val is None:
            self.logger.warning(
                f"Missing master correlationId for eventType '{event_type_str}' "
                f"from '{source_component_name}'. This will break tracing."
            )
            
        message = {
            "metadata": {
                "eventId": get_thread_safe_uuid(),  # Unique ID for this Redis message
                "correlationId": correlation_id_val,
                "causationId": causation_id_val,
                "timestamp_ns": time.perf_counter_ns(),  # When the message is created/published
                "origin_timestamp_s": origin_timestamp_s or time.time(),  # THE GOLDEN TIMESTAMP
                "eventType": event_type_str,
                "sourceComponent": source_component_name,
            },
            "payload": payload
        }
        return json.dumps(message)
    
    def publish_to_redis(self, stream_name: str, payload: Dict[str, Any], event_type: str,
                        correlation_id: str, source_component: str,
                        causation_id: Optional[str] = None) -> bool:
        """
        Publish data to Redis stream via Babysitter IPC.
        
        Args:
            stream_name: Target Redis stream name
            payload: Data to publish
            event_type: Type of event
            correlation_id: Master correlation ID
            source_component: Source component name
            causation_id: Causation ID (optional)
            
        Returns:
            True if successful, False otherwise
        """
        # Check if IPC data dumping is disabled (TANK mode)
        if not self.enable_ipc_data_dump:
            self.logger.debug(
                f"TANK_MODE: Skipping IPC send for {event_type} "
                f"(CorrID: {correlation_id}) to stream {stream_name}"
            )
            return True  # Return success to avoid breaking functionality
            
        if not self.babysitter_ipc_client:
            self.logger.warning(f"BabysitterIPCClient not available. Cannot publish {event_type}")
            return False
            
        try:
            # Create Redis message
            redis_message = self.create_redis_message_json(
                payload=payload,
                event_type_str=event_type,
                correlation_id_val=correlation_id,
                source_component_name=source_component,
                causation_id_val=causation_id
            )
            
            # Send via IPC
            success = self.babysitter_ipc_client.send_data(stream_name, redis_message)
            
            if not success:
                self.logger.warning(
                    f"Failed to send {event_type} (CorrID: {correlation_id}) "
                    f"to stream '{stream_name}'. Check IPC client logs."
                )
                
            return success
            
        except Exception as e:
            self.logger.error(
                f"Error publishing {event_type} to Redis: {e}",
                exc_info=True
            )
            return False
    
    def publish_dataclass(self, stream_name: str, data_obj: Any, event_type: str,
                         correlation_id: str, source_component: str,
                         causation_id: Optional[str] = None) -> bool:
        """
        Publish a dataclass object to Redis stream.
        
        Handles serialization of dataclass objects automatically.
        
        Args:
            stream_name: Target Redis stream name
            data_obj: Dataclass object to publish
            event_type: Type of event
            correlation_id: Master correlation ID
            source_component: Source component name
            causation_id: Causation ID (optional)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Convert dataclass to dict
            payload_dict: Dict[str, Any]
            if hasattr(data_obj, 'to_dict'):
                payload_dict = data_obj.to_dict()
            elif dataclasses.is_dataclass(data_obj):
                payload_dict = dataclasses.asdict(data_obj)
            else:
                self.logger.error(
                    f"Cannot serialize {type(data_obj).__name__} "
                    f"(CorrID: {correlation_id}): not a dict/dataclass"
                )
                return False
                
            return self.publish_to_redis(
                stream_name=stream_name,
                payload=payload_dict,
                event_type=event_type,
                correlation_id=correlation_id,
                source_component=source_component,
                causation_id=causation_id
            )
            
        except Exception as e:
            self.logger.error(
                f"Error serializing dataclass for {event_type}: {e}",
                exc_info=True
            )
            return False
    
    def set_ipc_data_dump_enabled(self, enabled: bool) -> None:
        """
        Enable or disable IPC data dumping (TANK mode control).
        
        Args:
            enabled: True to enable IPC data dumping, False to disable
        """
        with self._lock:
            self.enable_ipc_data_dump = enabled
            mode = "ENABLED" if enabled else "DISABLED"
            self.logger.info(f"IPC Data Dumping to Babysitter is now {mode}")
    
    def get_client_status(self) -> Dict[str, Any]:
        """
        Get the current status of the IPC client.
        
        Returns:
            Dictionary with status information
        """
        with self._lock:
            status = {
                'is_running': self._is_running,
                'offline_mode': self.offline_mode,
                'ipc_data_dump_enabled': self.enable_ipc_data_dump,
                'client_available': self.babysitter_ipc_client is not None
            }
            
            if self.babysitter_ipc_client:
                # Could add more detailed client status here
                status['client_type'] = type(self.babysitter_ipc_client).__name__
                
            return status