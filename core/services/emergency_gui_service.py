"""
Emergency GUI Service

This service manages emergency GUI functionality, including:
- Emergency status publishing via ZMQ
- Emergency command receiving
- Local HTTP API for status queries
- Service lifecycle management
- Configuration and dependency management

Extracted from ApplicationCore to follow Single Responsibility Principle.
"""

import logging
import threading
import time
from typing import Optional, Dict, Any
from threading import Lock

from interfaces.core.services import ILifecycleService
# Import our custom interface (will be relative path when integrated)
from interfaces.core.services import IEmergencyGUIService

logger = logging.getLogger(__name__)


class EmergencyGUIService(ILifecycleService, IEmergencyGUIService):
    """
    Manages Emergency GUI services and integration.
    
    This service encapsulates all emergency GUI logic that was
    previously scattered throughout ApplicationCore.
    """
    
    def __init__(self,
                 config_service: Any,
                 zmq_context: Optional[Any] = None,
                 application_core_ref: Optional[Any] = None):
        """
        Initialize the Emergency GUI Service.
        
        Args:
            config_service: Global configuration object
            zmq_context: ZMQ context for socket creation
            application_core_ref: Optional reference to ApplicationCore for emergency status access (TANK mode)
        """
        self.logger = logger
        self.config = config_service
        self.zmq_context = zmq_context
        self.application_core_ref = application_core_ref
        
        # Emergency GUI attributes
        self._is_running = False
        self._lock = Lock()
        
        # Emergency GUI services
        self.emergency_status_publisher: Optional[Any] = None
        self.emergency_command_receiver: Optional[Any] = None
        self.emergency_http_api: Optional[Any] = None
        
        # Configuration
        self._emergency_gui_enabled = getattr(self.config, 'ENABLE_EMERGENCY_GUI', False)
        self.status_port = getattr(self.config, 'emergency_status_port', 5561)
        self.command_port = getattr(self.config, 'emergency_command_port', 5560)
        self.http_port = getattr(self.config, 'emergency_http_port', 9999)
        
        # State tracking
        self._start_time = None
        self._services_started = False
        self._setup_complete = False
        
        # Check ZMQ availability
        self._zmq_available = self._check_zmq_availability()
        
        # Initialize services if enabled
        if self._emergency_gui_enabled and self._zmq_available:
            self._setup_emergency_gui_integration()
    
    @property
    def is_ready(self) -> bool:
        """Check if the emergency GUI service is ready."""
        with self._lock:
            return (self._is_running and 
                   self._setup_complete and
                   self._services_started)
    
    def is_enabled(self) -> bool:
        """Check if emergency GUI is enabled."""
        return self._emergency_gui_enabled
    
    def start(self) -> None:
        """Start the emergency GUI service."""
        with self._lock:
            if self._is_running:
                self.logger.warning("EmergencyGUIService already running")
                return
                
            if not self._emergency_gui_enabled:
                self.logger.info("Emergency GUI disabled - not starting")
                return
                
            if not self._zmq_available:
                self.logger.warning("Emergency GUI enabled but ZMQ not available")
                return
                
            self._is_running = True
            self._start_time = time.time()
            
            # Start emergency GUI services
            if self._setup_complete:
                self._start_emergency_gui_services()
                self.logger.info("EmergencyGUIService started successfully")
            else:
                self.logger.error("Emergency GUI setup not complete - cannot start")
                self._is_running = False
    
    def stop(self) -> None:
        """Stop the emergency GUI service."""
        with self._lock:
            if not self._is_running:
                return
                
            self._is_running = False
            
            # Stop all emergency GUI services
            self._stop_emergency_gui_services()
            
            self._services_started = False
            self.logger.info("EmergencyGUIService stopped")
    
    def _check_zmq_availability(self) -> bool:
        """Check if ZMQ is available."""
        try:
            import zmq
            return True
        except ImportError:
            self.logger.error("PyZMQ not available. Emergency GUI disabled.")
            return False
    
    def _setup_emergency_gui_integration(self):
        """Setup Emergency GUI services for TANK Core integration."""
        try:
            # Import Emergency GUI classes
            from core.emergency_gui_integration import (
                TankStatusPublisher, TankCommandReceiver, LocalStatusAPI
            )
            
            self.logger.info(f"Setting up Emergency GUI integration - "
                           f"Status:{self.status_port}, Command:{self.command_port}, HTTP:{self.http_port}")
            
            # Initialize Emergency GUI services
            self.emergency_status_publisher = TankStatusPublisher(
                port=self.status_port,
                tank_core=self.application_core_ref,
                zmq_context=self.zmq_context
            )
            
            self.emergency_command_receiver = TankCommandReceiver(
                port=self.command_port,
                tank_core=self.application_core_ref,
                zmq_context=self.zmq_context
            )
            
            self.emergency_http_api = LocalStatusAPI(
                port=self.http_port,
                tank_core=self.application_core_ref
            )
            
            self._setup_complete = True
            self.logger.info("Emergency GUI integration setup complete")
            
        except ImportError as e:
            self.logger.error(f"Failed to import Emergency GUI classes: {e}")
            self._emergency_gui_enabled = False
            self._setup_complete = False
        except Exception as e:
            self.logger.error(f"Failed to setup Emergency GUI integration: {e}", exc_info=True)
            self._emergency_gui_enabled = False
            self._setup_complete = False
    
    def _start_emergency_gui_services(self):
        """Start Emergency GUI services for TANK Core integration."""
        try:
            services_started = []
            
            # Start status publisher
            if self.emergency_status_publisher:
                try:
                    self.emergency_status_publisher.start()
                    services_started.append("Status Publisher")
                    self.logger.info("Emergency GUI Status Publisher started")
                except Exception as e:
                    self.logger.error(f"Failed to start Emergency GUI Status Publisher: {e}")
            
            # Start command receiver  
            if self.emergency_command_receiver:
                try:
                    self.emergency_command_receiver.start()
                    services_started.append("Command Receiver")
                    self.logger.info("Emergency GUI Command Receiver started")
                except Exception as e:
                    self.logger.error(f"Failed to start Emergency GUI Command Receiver: {e}")
            
            # Start HTTP API
            if self.emergency_http_api:
                try:
                    self.emergency_http_api.start()
                    services_started.append("HTTP API")
                    self.logger.info("Emergency GUI HTTP API started")
                except Exception as e:
                    self.logger.error(f"Failed to start Emergency GUI HTTP API: {e}")
            
            if services_started:
                self._services_started = True
                self.logger.info(f"Emergency GUI services started: {', '.join(services_started)}")
            else:
                self.logger.warning("No Emergency GUI services were started")
                
        except Exception as e:
            self.logger.error(f"Failed to start Emergency GUI services: {e}", exc_info=True)
            self._emergency_gui_enabled = False
            self._services_started = False
    
    def _stop_emergency_gui_services(self):
        """Stop Emergency GUI services."""
        try:
            services_stopped = []
            
            # Stop HTTP API first (it's usually easiest to stop)
            if self.emergency_http_api:
                try:
                    if hasattr(self.emergency_http_api, 'stop'):
                        self.emergency_http_api.stop()
                        services_stopped.append("HTTP API")
                        self.logger.info("Emergency GUI HTTP API stopped")
                    else:
                        self.logger.warning("Emergency GUI HTTP API has no stop method")
                except Exception as e:
                    self.logger.error(f"Error stopping Emergency GUI HTTP API: {e}")
            
            # Stop command receiver
            if self.emergency_command_receiver:
                try:
                    if hasattr(self.emergency_command_receiver, 'stop'):
                        self.emergency_command_receiver.stop()
                        services_stopped.append("Command Receiver")
                        self.logger.info("Emergency GUI Command Receiver stopped")
                    else:
                        self.logger.warning("Emergency GUI Command Receiver has no stop method")
                except Exception as e:
                    self.logger.error(f"Error stopping Emergency GUI Command Receiver: {e}")
            
            # Stop status publisher last
            if self.emergency_status_publisher:
                try:
                    if hasattr(self.emergency_status_publisher, 'stop'):
                        self.emergency_status_publisher.stop()
                        services_stopped.append("Status Publisher")
                        self.logger.info("Emergency GUI Status Publisher stopped")
                    else:
                        self.logger.warning("Emergency GUI Status Publisher has no stop method")
                except Exception as e:
                    self.logger.error(f"Error stopping Emergency GUI Status Publisher: {e}")
            
            if services_stopped:
                self.logger.info(f"Emergency GUI services stopped: {', '.join(services_stopped)}")
            else:
                self.logger.info("No Emergency GUI services to stop")
                
        except Exception as e:
            self.logger.error(f"Error stopping Emergency GUI services: {e}", exc_info=True)
    
    def get_service_status(self) -> Dict[str, Any]:
        """Get status of all emergency GUI services."""
        with self._lock:
            uptime = time.time() - self._start_time if self._start_time else 0
            
            status = {
                "is_enabled": self._emergency_gui_enabled,
                "is_running": self._is_running,
                "is_ready": self.is_ready,
                "setup_complete": self._setup_complete,
                "services_started": self._services_started,
                "zmq_available": self._zmq_available,
                "uptime_seconds": uptime,
                "configuration": {
                    "status_port": self.status_port,
                    "command_port": self.command_port,
                    "http_port": self.http_port
                },
                "services": {}
            }
            
            # Check individual service status
            if self.emergency_status_publisher:
                status["services"]["status_publisher"] = {
                    "available": True,
                    "has_stop_method": hasattr(self.emergency_status_publisher, 'stop')
                }
            
            if self.emergency_command_receiver:
                status["services"]["command_receiver"] = {
                    "available": True,
                    "has_stop_method": hasattr(self.emergency_command_receiver, 'stop')
                }
            
            if self.emergency_http_api:
                status["services"]["http_api"] = {
                    "available": True,
                    "has_stop_method": hasattr(self.emergency_http_api, 'stop')
                }
            
            return status
    
    def publish_status_update(self, status_data: Dict[str, Any]) -> bool:
        """Publish status update to emergency GUI."""
        try:
            if not self._is_running or not self.emergency_status_publisher:
                return False
            
            # Use status publisher if it has a publish method
            if hasattr(self.emergency_status_publisher, 'publish_status'):
                self.emergency_status_publisher.publish_status(status_data)
                return True
            elif hasattr(self.emergency_status_publisher, 'send_status'):
                self.emergency_status_publisher.send_status(status_data)
                return True
            else:
                self.logger.warning("Emergency status publisher has no known publish method")
                return False
                
        except Exception as e:
            self.logger.error(f"Error publishing status update to emergency GUI: {e}", exc_info=True)
            return False
    
    def get_emergency_gui_services(self) -> Dict[str, Any]:
        """Get references to emergency GUI services for backward compatibility."""
        return {
            "emergency_status_publisher": self.emergency_status_publisher,
            "emergency_command_receiver": self.emergency_command_receiver,
            "emergency_http_api": self.emergency_http_api
        }
    
    def set_application_core_reference(self, legacy_ref: Any = None):
        """Legacy method - ApplicationCore dependencies removed."""
        # ApplicationCore dependency removed - emergency services now use telemetry service
        self.logger.info("Emergency GUI services now use telemetry service instead of ApplicationCore")