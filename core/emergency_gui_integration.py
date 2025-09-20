#!/usr/bin/env python3
"""
Emergency GUI Integration Classes for TANK Core

Provides ZMQ and HTTP interfaces for Emergency GUI to communicate with TANK Core:
- TankStatusPublisher: Broadcasts real-time status updates via ZMQ PUB
- TankCommandReceiver: Receives emergency commands via ZMQ PULL  
- LocalStatusAPI: HTTP endpoint for initial status queries

These classes enable the Emergency GUI to function independently of the main
TESTRADE backend infrastructure for true emergency access.
"""

import zmq
import json
import threading
import time
import logging
from typing import Optional, Dict, Any, Callable
from flask import Flask, jsonify

logger = logging.getLogger(__name__)


class TankStatusPublisher:
    """
    ZMQ Publisher for broadcasting TANK Core status updates to Emergency GUI.
    
    Publishes real-time status updates and critical alerts via ZMQ PUB socket.
    Emergency GUI subscribes to these updates for live monitoring.
    """
    
    def __init__(self, port: int = 5561, tank_core=None, zmq_context=None):
        self.port = port
        self.tank_core = tank_core
        self.context = zmq_context or zmq.Context.instance()
        self.publisher = None
        self.running = False
        self._publisher_thread = None
        
        logger.info(f"TankStatusPublisher initialized on port {port}")
    
    def start(self):
        """Start the status publisher."""
        try:
            self.publisher = self.context.socket(zmq.PUB)
            self.publisher.bind(f"tcp://*:{self.port}")
            self.running = True
            
            # Start periodic status broadcasting thread
            self._publisher_thread = threading.Thread(
                target=self._publisher_loop,
                name="EmergencyGUI-StatusPublisher",
                daemon=True
            )
            self._publisher_thread.start()
            
            logger.info(f"TankStatusPublisher started on port {self.port}")
            
        except Exception as e:
            logger.error(f"Failed to start TankStatusPublisher: {e}")
            raise
    
    def stop(self):
        """Stop the status publisher."""
        self.running = False
        if self._publisher_thread:
            self._publisher_thread.join(timeout=2)
        if self.publisher:
            self.publisher.close()
        logger.info("TankStatusPublisher stopped")
    
    def broadcast_status_update(self, status_data: Dict[str, Any]):
        """Broadcast status update to Emergency GUI."""
        if self.running and self.publisher:
            try:
                self.publisher.send_multipart([
                    b"STATUS_UPDATE",
                    json.dumps(status_data).encode('utf-8')
                ], zmq.NOBLOCK)
            except zmq.Again:
                logger.debug("Status update dropped - no subscribers")
            except Exception as e:
                logger.warning(f"Failed to broadcast status update: {e}")
    
    def broadcast_critical_alert(self, alert_data: Dict[str, Any]):
        """Broadcast critical alert to Emergency GUI."""
        if self.running and self.publisher:
            try:
                self.publisher.send_multipart([
                    b"CRITICAL_ALERT", 
                    json.dumps(alert_data).encode('utf-8')
                ], zmq.NOBLOCK)
                logger.info(f"Critical alert broadcasted: {alert_data.get('type', 'Unknown')}")
            except zmq.Again:
                logger.debug("Critical alert dropped - no subscribers")
            except Exception as e:
                logger.error(f"Failed to broadcast critical alert: {e}")
    
    def _publisher_loop(self):
        """Periodic status broadcasting loop."""
        logger.info("TankStatusPublisher loop started")
        
        while self.running:
            try:
                # Get current status and broadcast every 5 seconds
                status = self._get_current_status()
                self.broadcast_status_update(status)
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"Status publisher loop error: {e}")
                time.sleep(1)
        
        logger.info("TankStatusPublisher loop stopped")
    
    def _get_current_status(self) -> Dict[str, Any]:
        """Get current TANK status for broadcasting."""
        try:
            if not self.tank_core:
                return {"error": "TANK Core not available", "timestamp": time.time()}
            
            # Get uptime
            uptime_sec = 0
            if hasattr(self.tank_core, '_start_time'):
                uptime_sec = time.time() - self.tank_core._start_time
            
            # Get OCR status
            ocr_status = "UNKNOWN"
            if hasattr(self.tank_core, 'ocr_process') and self.tank_core.ocr_process:
                ocr_status = "RUNNING" if self.tank_core.ocr_process.is_alive() else "STOPPED"
            elif getattr(self.tank_core.config, 'disable_ocr', False):
                ocr_status = "DISABLED"
            
            # Get IPC stats
            ipc_stats = {}
            if hasattr(self.tank_core, 'babysitter_ipc_client') and self.tank_core.babysitter_ipc_client:
                try:
                    ipc_stats = self.tank_core.babysitter_ipc_client.get_ipc_buffer_stats()
                except Exception as e:
                    logger.debug(f"Failed to get IPC stats: {e}")
            
            # Get position summary
            position_summary = {"total_open_positions": 0, "total_pnl_open": 0.0}
            try:
                position_manager = self.tank_core._di_container.resolve('IPositionManager')
                if position_manager and hasattr(position_manager, 'get_all_open_positions'):
                    open_positions = position_manager.get_all_open_positions()
                    position_summary["total_open_positions"] = len(open_positions)
                    # Calculate total P&L if available
                    total_pnl = sum(pos.get('unrealized_pnl', 0) for pos in open_positions if isinstance(pos, dict))
                    position_summary["total_pnl_open"] = total_pnl
            except Exception as e:
                logger.debug(f"Failed to get position summary: {e}")
            
            return {
                "timestamp": time.time(),
                "tank_core_status": self._get_tank_health(),
                "tank_uptime_sec": uptime_sec,
                "ocr_status": ocr_status,
                "current_roi": self._get_current_roi(),
                "ipc_client_stats": ipc_stats,
                "position_summary": position_summary
            }
            
        except Exception as e:
            logger.error(f"Error getting current status: {e}")
            return {
                "error": str(e),
                "timestamp": time.time(),
                "tank_core_status": "ERROR"
            }
    
    def _get_tank_health(self) -> str:
        """Get TANK Core health status."""
        try:
            # Check critical components
            if not hasattr(self.tank_core, 'babysitter_ipc_client') or not self.tank_core.babysitter_ipc_client:
                return "DEGRADED"
            
            if hasattr(self.tank_core, 'ocr_process') and self.tank_core.ocr_process:
                if not self.tank_core.ocr_process.is_alive():
                    return "DEGRADED"
            
            return "HEALTHY"
            
        except Exception:
            return "UNKNOWN"
    
    def _get_current_roi(self) -> list:
        """Get current ROI coordinates."""
        try:
            # Try to get ROI from OCR service if available
            # This would need to be implemented based on your OCR service structure
            return [0, 0, 100, 100]  # Default ROI
        except Exception:
            return [0, 0, 100, 100]


class TankCommandReceiver:
    """
    ZMQ PULL socket for receiving emergency commands from Emergency GUI.
    
    Processes emergency commands and executes them via TANK Core services.
    """
    
    def __init__(self, port: int = 5560, tank_core=None, zmq_context=None):
        self.port = port
        self.tank_core = tank_core
        self.context = zmq_context or zmq.Context.instance()
        self.receiver = None
        self.running = False
        self._command_thread = None
        
        logger.info(f"TankCommandReceiver initialized on port {port}")
    
    def start(self):
        """Start the command receiver."""
        try:
            self.receiver = self.context.socket(zmq.PULL)
            self.receiver.bind(f"tcp://*:{self.port}")
            self.running = True
            
            # Start command processing thread
            self._command_thread = threading.Thread(
                target=self._command_loop,
                name="EmergencyGUI-CommandReceiver", 
                daemon=True
            )
            self._command_thread.start()
            
            logger.info(f"TankCommandReceiver started on port {self.port}")
            
        except Exception as e:
            logger.error(f"Failed to start TankCommandReceiver: {e}")
            raise
    
    def stop(self):
        """Stop the command receiver."""
        self.running = False
        if self._command_thread:
            self._command_thread.join(timeout=2)
        if self.receiver:
            self.receiver.close()
        logger.info("TankCommandReceiver stopped")
    
    def _command_loop(self):
        """Listen for commands from Emergency GUI."""
        logger.info("TankCommandReceiver loop started")
        
        while self.running:
            try:
                # Receive command with timeout
                if self.receiver.poll(1000):  # 1 second timeout
                    message_parts = self.receiver.recv_multipart(zmq.NOBLOCK)
                    
                    if len(message_parts) >= 2:
                        command = message_parts[0].decode('utf-8')
                        params = json.loads(message_parts[1].decode('utf-8'))
                        
                        # Execute command
                        self._execute_command(command, params)
                        
            except Exception as e:
                logger.error(f"Command receiver error: {e}")
                time.sleep(1)
        
        logger.info("TankCommandReceiver loop stopped")
    
    def _execute_command(self, command: str, params: Dict[str, Any]):
        """Execute emergency commands."""
        try:
            logger.critical(f"EMERGENCY COMMAND: {command} with params: {params}")
            
            if command == "FORCE_CLOSE_ALL_POSITIONS":
                self._force_close_all_positions(params)
            elif command == "STOP_OCR_PROCESS":
                self._stop_ocr_process()
            elif command == "START_OCR_PROCESS":
                self._start_ocr_process()
            elif command == "SET_ROI_ABSOLUTE":
                self._set_roi_absolute(params)
            else:
                logger.warning(f"Unknown emergency command: {command}")
                
        except Exception as e:
            logger.error(f"Emergency command execution error: {e}")
    
    def _force_close_all_positions(self, params: Dict[str, Any]):
        """EMERGENCY: Force close all trading positions."""
        try:
            reason = params.get('reason', 'Emergency GUI Force Close All')
            logger.critical(f"EMERGENCY: Force closing all positions - {reason}")
            
            # Get trade manager from DI container
            trade_manager = self.tank_core._di_container.resolve('ITradeManagerService')
            if trade_manager and hasattr(trade_manager, 'force_close_all_and_halt_system'):
                success = trade_manager.force_close_all_and_halt_system(reason)
                logger.critical(f"Force close all result: {success}")
            else:
                logger.error("Trade manager not available for emergency force close")
                
        except Exception as e:
            logger.error(f"Emergency force close failed: {e}")
    
    def _stop_ocr_process(self):
        """EMERGENCY: Stop OCR processing."""
        try:
            logger.warning("EMERGENCY: Stopping OCR process")
            
            if hasattr(self.tank_core, 'ocr_process') and self.tank_core.ocr_process:
                if self.tank_core.ocr_process.is_alive():
                    self.tank_core.ocr_process.terminate()
                    logger.warning("OCR process terminated via Emergency GUI")
                else:
                    logger.info("OCR process was already stopped")
            else:
                logger.warning("OCR process not available")
                
        except Exception as e:
            logger.error(f"Emergency OCR stop failed: {e}")
    
    def _start_ocr_process(self):
        """EMERGENCY: Start OCR processing."""
        try:
            logger.info("EMERGENCY: Starting OCR process")
            
            # This would need to be implemented based on your OCR restart logic
            # For now, just log the attempt
            logger.info("OCR restart requested via Emergency GUI - manual intervention may be required")
            
        except Exception as e:
            logger.error(f"Emergency OCR start failed: {e}")
    
    def _set_roi_absolute(self, params: Dict[str, Any]):
        """EMERGENCY: Set ROI coordinates."""
        try:
            coordinates = params.get('coordinates', [0, 0, 100, 100])
            logger.info(f"EMERGENCY: Setting ROI to {coordinates}")
            
            # Send ROI command via existing OCR command infrastructure
            if hasattr(self.tank_core, 'send_ocr_command_via_babysitter'):
                command_id = self.tank_core.send_ocr_command_via_babysitter(
                    "SET_ROI_ABSOLUTE",
                    {"coordinates": coordinates, "source": "EmergencyGUI"}
                )
                logger.info(f"ROI update command sent via Babysitter: {command_id}")
            else:
                logger.warning("OCR command infrastructure not available for ROI update")
                
        except Exception as e:
            logger.error(f"Emergency ROI update failed: {e}")


class LocalStatusAPI:
    """
    HTTP API for Emergency GUI to query TANK Core status.
    
    Provides simple HTTP endpoint for initial status queries when WebSocket
    connection is not yet established.
    """
    
    def __init__(self, port: int = 9999, tank_core=None):
        self.port = port
        self.tank_core = tank_core
        self.app = Flask(__name__)
        self.app.logger.disabled = True  # Disable Flask logging
        self._setup_routes()
        self._server_thread = None
        
        logger.info(f"LocalStatusAPI initialized on port {port}")
    
    def _setup_routes(self):
        """Setup HTTP routes."""
        
        @self.app.route('/localstatus')
        def get_status():
            return jsonify(self._get_status_data())
    
    def start(self):
        """Start the HTTP API server."""
        try:
            self._server_thread = threading.Thread(
                target=self._run_server,
                name="EmergencyGUI-HTTPServer",
                daemon=True
            )
            self._server_thread.start()
            
            logger.info(f"LocalStatusAPI started on port {self.port}")
            
        except Exception as e:
            logger.error(f"Failed to start LocalStatusAPI: {e}")
            raise
    
    def stop(self):
        """Stop the HTTP API server."""
        # Flask server will stop when main thread exits (daemon=True)
        logger.info("LocalStatusAPI stopped")
    
    def _run_server(self):
        """Run the Flask server using production WSGI server."""
        try:
            logger.info(f"Starting production WSGI server on 127.0.0.1:{self.port}")

            # Try to use waitress (production WSGI server) if available
            try:
                from waitress import serve
                logger.info("Using Waitress production WSGI server")
                serve(
                    self.app,
                    host='127.0.0.1',
                    port=self.port,
                    threads=4,
                    cleanup_interval=30,
                    channel_timeout=120
                )
            except ImportError:
                # Fallback to Flask development server with warning suppressed
                logger.warning("Waitress not available, falling back to Flask development server")
                import logging
                werkzeug_logger = logging.getLogger('werkzeug')
                werkzeug_logger.setLevel(logging.ERROR)

                self.app.run(
                    host='127.0.0.1',
                    port=self.port,
                    debug=False,
                    use_reloader=False,
                    threaded=True
                )
        except Exception as e:
            logger.error(f"HTTP server error: {e}")
            import traceback
            logger.error(f"HTTP server traceback: {traceback.format_exc()}")
    
    def _get_status_data(self) -> Dict[str, Any]:
        """Get current TANK status for HTTP response."""
        try:
            logger.debug("LocalStatusAPI: Getting status data")

            if not self.tank_core:
                logger.debug("LocalStatusAPI: TANK Core not available")
                return {
                    "error": "TANK Core not available",
                    "timestamp": time.time(),
                    "tank_reachable": False
                }

            # Simple status without calling potentially blocking methods
            logger.debug("LocalStatusAPI: Building simple status response")
            return {
                "timestamp": time.time(),
                "tank_core_status": "RUNNING",
                "tank_uptime_sec": time.time() - getattr(self.tank_core, '_start_time', time.time()),
                "ocr_status": "RUNNING" if hasattr(self.tank_core, 'ocr_process') else "UNKNOWN",
                "current_roi": [62, 158, 667, 277],  # Default ROI
                "ipc_client_stats": {"status": "available"},
                "position_summary": {"total_open_positions": 0, "total_pnl_open": 0.0},
                "tank_reachable": True
            }

        except Exception as e:
            logger.error(f"Error getting status data: {e}")
            return {
                "error": str(e),
                "timestamp": time.time(),
                "tank_reachable": False
            }
