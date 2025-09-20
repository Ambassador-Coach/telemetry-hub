#!/usr/bin/env python3
"""
TESTRADE Mission Control Server

A world-class unified launcher and process management system that provides:
- Single UI for all TESTRADE modes and processes
- Real-time process monitoring and health checks
- Intelligent error detection and recovery suggestions
- Consolidated console output with filtering
- One-click process restart with context
- Clean startup/shutdown orchestration
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
from collections import deque
import signal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Add TESTRADE root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from mission_control.server.process_monitor import ProcessMonitor
from mission_control.server.console_aggregator import ConsoleAggregator
from mission_control.server.error_detector import ErrorDetector
from mission_control.server.mode_manager import ModeManager
from mission_control.server.zombie_killer import ZombieKiller, cleanup_before_start

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MissionControl')


class LaunchRequest(BaseModel):
    """Request model for launching TESTRADE in a specific mode"""
    mode: str
    options: Optional[Dict[str, Any]] = {}


class ProcessAction(BaseModel):
    """Request model for process actions"""
    process_name: str
    action: str  # start, stop, restart


class MissionControlServer:
    """
    Main Mission Control server that orchestrates all TESTRADE processes
    and provides a unified management interface.
    """
    
    def __init__(self):
        self.app = FastAPI(title="TESTRADE Mission Control")
        self.websocket_clients: Set[WebSocket] = set()
        
        # Core components
        self.process_monitor = ProcessMonitor()
        self.console_aggregator = ConsoleAggregator()
        self.error_detector = ErrorDetector()
        self.mode_manager = ModeManager()
        
        # State tracking
        self.current_mode = "STOPPED"
        self.process_states = {}
        self.console_buffers: Dict[str, deque] = {}
        self.active_alerts = []
        
        # Configure CORS for frontend - must be before routes
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Allow all origins for development
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
        
        # Setup routes
        self._setup_routes()
        
        # Setup startup event
        @self.app.on_event("startup")
        async def startup_event():
            """Initialize components that need async"""
            await self.process_monitor.initialize(crash_callback=self._handle_process_crash)
            logger.info("Mission Control initialized")
        
        @self.app.on_event("shutdown")
        async def shutdown_event():
            """Clean shutdown of all processes"""
            logger.info("Mission Control shutting down...")
            await self.emergency_shutdown()
            logger.info("Mission Control shutdown complete")
        
    def _setup_routes(self):
        """Configure all API routes"""
        
        @self.app.get("/")
        async def root():
            return {"message": "TESTRADE Mission Control API", "version": "1.0.0"}
        
        @self.app.get("/api/status")
        async def get_status():
            """Get current system status"""
            return {
                "mode": self.current_mode,
                "processes": self.process_monitor.get_all_status(),
                "alerts": self.active_alerts,
                "uptime": self.process_monitor.get_uptime()
            }
        
        @self.app.post("/api/launch")
        async def launch_mode(request: LaunchRequest):
            """Launch TESTRADE in specified mode"""
            try:
                logger.info(f"Launching TESTRADE in {request.mode} mode")
                
                # Stop any running processes first
                if self.current_mode != "STOPPED":
                    await self.stop_all_processes()
                
                # Apply mode configuration
                config_updates = self.mode_manager.get_mode_config(request.mode)
                if not config_updates:
                    raise HTTPException(status_code=400, detail=f"Unknown mode: {request.mode}")
                
                # Update control.json
                self.mode_manager.apply_mode_config(request.mode)
                
                # Start processes for this mode
                processes_to_start = config_updates.get("processes", [])
                results = await self.start_processes(processes_to_start)
                
                self.current_mode = request.mode
                
                # Broadcast status update
                await self.broadcast_status_update()
                
                # Check if GUI Backend started successfully
                if "GUI" in results and results["GUI"].get("success"):
                    logger.info("GUI Backend started - Trading GUI available at http://localhost:8001/gui")
                
                return {
                    "status": "success",
                    "mode": request.mode,
                    "results": results,
                    "gui_url": "http://localhost:8001/gui"
                }
                
            except Exception as e:
                logger.error(f"Failed to launch mode {request.mode}: {e}")
                
                # Analyze the failure
                analysis = self.error_detector.analyze_launch_failure(str(e))
                
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": str(e),
                        "analysis": analysis
                    }
                )
        
        @self.app.post("/api/process/{action}")
        async def process_action(action: str, request: ProcessAction):
            """Perform action on a specific process"""
            try:
                if action == "start":
                    result = await self.process_monitor.start_process(request.process_name)
                elif action == "stop":
                    result = await self.process_monitor.stop_process(request.process_name)
                elif action == "restart":
                    # Analyze before restart
                    logs = self.console_aggregator.get_recent_logs(request.process_name, 100)
                    analysis = self.error_detector.analyze_process_failure(request.process_name, logs)
                    
                    result = await self.process_monitor.restart_process(request.process_name)
                    result["analysis"] = analysis
                else:
                    raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
                
                await self.broadcast_status_update()
                return result
                
            except Exception as e:
                logger.error(f"Process action failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.post("/api/shutdown")
        async def shutdown_all():
            """Gracefully shutdown all processes"""
            try:
                await self.stop_all_processes()
                self.current_mode = "STOPPED"
                await self.broadcast_status_update()
                return {"status": "success", "message": "All processes stopped"}
            except Exception as e:
                logger.error(f"Shutdown failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            """WebSocket for real-time updates"""
            await websocket.accept()
            self.websocket_clients.add(websocket)
            
            try:
                # Send initial status
                await websocket.send_json({
                    "type": "initial_status",
                    "data": {
                        "mode": self.current_mode,
                        "processes": self.process_monitor.get_all_status()
                    }
                })
                
                # Keep connection alive
                while True:
                    # Wait for any message (ping/pong)
                    await websocket.receive_text()
                    
            except WebSocketDisconnect:
                self.websocket_clients.remove(websocket)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                self.websocket_clients.discard(websocket)
    
    async def start_processes(self, process_names: List[str]) -> Dict[str, any]:
        """Start multiple processes with proper error handling"""
        results = {}
        
        for process_name in process_names:
            try:
                # Start the process
                result = await self.process_monitor.start_process(process_name)
                results[process_name] = result
                
                # Start console capture only if available
                if result.get("success") and result.get("process_id") and result.get("has_console_capture", True):
                    # Get subprocess from process monitor
                    subprocess_obj = self.process_monitor.subprocesses.get(process_name)
                    asyncio.create_task(
                        self.console_aggregator.capture_process_output(
                            process_name,
                            result["process_id"],
                            subprocess_obj,
                            self.on_console_output
                        )
                    )
                    
            except Exception as e:
                logger.error(f"Failed to start {process_name}: {e}")
                results[process_name] = {
                    "success": False,
                    "error": str(e),
                    "analysis": self.error_detector.analyze_startup_error(process_name, str(e))
                }
        
        return results
    
    async def stop_all_processes(self):
        """Stop all processes in correct order"""
        # Get shutdown order from ProcessOrchestrator principles
        running_processes = self.process_monitor.get_running_processes()
        shutdown_order = self.mode_manager.get_shutdown_order(running_processes)
        
        for process_name in shutdown_order:
            try:
                await self.process_monitor.stop_process(process_name)
            except Exception as e:
                logger.error(f"Failed to stop {process_name}: {e}")
    
    async def on_console_output(self, process_name: str, output: str):
        """Handle console output from a process"""
        # Add to buffer
        if process_name not in self.console_buffers:
            self.console_buffers[process_name] = deque(maxlen=1000)
        self.console_buffers[process_name].append({
            "timestamp": datetime.now().isoformat(),
            "message": output
        })
        
        # Check for alerts
        alert = self.error_detector.check_for_alerts(process_name, output)
        if alert:
            self.active_alerts.append(alert)
            await self.broadcast_alert(alert)
        
        # Broadcast to websocket clients
        await self.broadcast_console_output(process_name, output)
    
    async def emergency_shutdown(self):
        """Emergency shutdown with zombie cleanup"""
        try:
            # First try graceful shutdown
            await self.stop_all_processes()
            
            # Wait a moment
            await asyncio.sleep(2)
            
            # Then use zombie killer for any remaining processes
            killer = ZombieKiller()
            results = killer.emergency_cleanup()
            
            if results["errors"]:
                logger.error(f"Cleanup errors: {results['errors']}")
            else:
                logger.info(f"Cleanup complete: {results['processes_killed']} processes terminated")
                
        except Exception as e:
            logger.error(f"Emergency shutdown error: {e}")
    
    async def broadcast_status_update(self):
        """Broadcast status update to all connected clients"""
        message = {
            "type": "status_update",
            "data": {
                "mode": self.current_mode,
                "processes": self.process_monitor.get_all_status(),
                "alerts": self.active_alerts
            }
        }
        await self._broadcast_to_all(message)
    
    async def broadcast_console_output(self, process_name: str, output: str):
        """Broadcast console output to all connected clients"""
        message = {
            "type": "console_output",
            "data": {
                "process": process_name,
                "timestamp": datetime.now().isoformat(),
                "message": output
            }
        }
        await self._broadcast_to_all(message)
    
    async def broadcast_alert(self, alert: Dict):
        """Broadcast alert to all connected clients"""
        message = {
            "type": "alert",
            "data": alert
        }
        await self._broadcast_to_all(message)
    
    async def _broadcast_to_all(self, message: Dict):
        """Send message to all connected websocket clients"""
        disconnected = set()
        
        for websocket in self.websocket_clients:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.add(websocket)
        
        # Remove disconnected clients
        self.websocket_clients -= disconnected
    
    async def _handle_process_crash(self, process_name: str, exit_code: int):
        """Handle process crash notification from monitor"""
        logger.error(f"[CRASH DETECTED] Process '{process_name}' crashed with exit code {exit_code}")
        
        # Create alert
        alert = {
            "id": f"crash_{process_name}_{int(datetime.now().timestamp())}",
            "type": "error",
            "severity": "critical",
            "process": process_name,
            "title": f"{process_name} Process Crashed",
            "message": f"Process '{process_name}' unexpectedly terminated with exit code {exit_code}",
            "timestamp": datetime.now().isoformat(),
            "actions": ["restart", "investigate"]
        }
        
        # Add to active alerts
        self.active_alerts.append(alert)
        
        # Broadcast alert immediately
        await self.broadcast_alert(alert)
        
        # Broadcast status update
        await self.broadcast_status_update()
        
        # Log to console aggregator if available
        if hasattr(self, 'console_aggregator') and self.console_aggregator:
            self.console_aggregator.add_output(
                process_name, 
                f"[CRASH] Process terminated unexpectedly with exit code {exit_code}"
            )
    
    def run(self, host: str = "0.0.0.0", port: int = 8000):
        """Run the Mission Control server"""
        import uvicorn
        
        # Cleanup before starting
        logger.info("Checking for zombie processes...")
        cleanup_before_start()
        
        logger.info(f"Starting Mission Control server on {host}:{port}")
        
        # Setup graceful shutdown
        def signal_handler(signum, frame):
            logger.info("Received shutdown signal, initiating emergency shutdown...")
            # Create a new event loop for the cleanup
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.emergency_shutdown())
            loop.close()
            os._exit(0)
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Run the server
        uvicorn.run(self.app, host=host, port=port)


if __name__ == "__main__":
    server = MissionControlServer()
    server.run()