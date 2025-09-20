"""
Process Monitor for Mission Control

Manages and monitors all TESTRADE processes with health checks,
resource monitoring, and intelligent restart capabilities.
"""

import asyncio
import json
import logging
import os
import psutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
import platform

from .console_mode_config import is_integrated_mode

logger = logging.getLogger(__name__)


@dataclass
class ProcessInfo:
    """Information about a managed process"""
    name: str
    display_name: str
    pid: Optional[int] = None
    state: str = "stopped"  # stopped, starting, running, failed
    start_time: Optional[float] = None
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    restart_count: int = 0
    last_error: Optional[str] = None
    exit_code: Optional[int] = None
    external: bool = False  # True if process is running outside Mission Control
    
    def to_dict(self) -> Dict:
        data = asdict(self)
        if self.start_time:
            data["uptime"] = time.time() - self.start_time
        return data


class ProcessMonitor:
    """
    Monitors and manages all TESTRADE processes with platform-aware execution.
    """
    
    def __init__(self):
        self.processes: Dict[str, ProcessInfo] = {}
        self.subprocesses: Dict[str, subprocess.Popen] = {}
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        
        # Load process definitions
        self.process_definitions = self._load_process_definitions()
        
        # Initialize process info
        for name, definition in self.process_definitions.items():
            self.processes[name] = ProcessInfo(
                name=name,
                display_name=definition["display_name"]
            )
        
        # Flag to check Redis on first use
        self._redis_checked = False
        
        # Crash notification callback
        self._crash_callback = None
    
    async def initialize(self, crash_callback=None):
        """Initialize the process monitor - call this after event loop is running
        
        Args:
            crash_callback: Optional async callback to call when a process crashes
        """
        await self._check_external_redis()
        self._redis_checked = True
        self._crash_callback = crash_callback
    
    async def _check_external_redis(self):
        """Check if Redis is already running externally on startup"""
        try:
            platform_name = "windows" if platform.system() == "Windows" else "linux"
            redis_def = self.process_definitions.get("Redis", {})
            platform_config = redis_def.get("platform", {}).get(platform_name, {})
            
            if "check_command" in platform_config:
                check_result = subprocess.run(
                    platform_config["check_command"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if check_result.returncode == 0 and "PONG" in check_result.stdout:
                    logger.info("Redis is already running externally at startup")
                    self.processes["Redis"].state = "running"
                    self.processes["Redis"].external = True
                    self.processes["Redis"].start_time = time.time()
        except Exception as e:
            logger.debug(f"Redis startup check failed: {e}")
    
    def _load_process_definitions(self) -> Dict[str, Any]:
        """Load process definitions from configuration"""
        # Get TESTRADE root directory
        testrade_root = Path(__file__).parent.parent.parent
        
        return {
            "Redis": {
                "display_name": "Redis Server",
                "category": "Infrastructure",
                "platform": {
                    "windows": {
                        "command": ["wsl", "-d", "Ubuntu", "--", "redis-server"],
                        "check_command": ["wsl", "-d", "Ubuntu", "--", "redis-cli", "ping"]
                    },
                    "linux": {
                        "command": ["redis-server"],
                        "check_command": ["redis-cli", "ping"]
                    }
                }
            },
            "BabysitterService": {
                "display_name": "Babysitter Service",
                "category": "Infrastructure",
                "platform": {
                    "windows": {
                        "command": [sys.executable, str(testrade_root / "core" / "babysitter_service.py")],
                        "cwd": str(testrade_root)
                    }
                }
            },
            "TelemetryService": {
                "display_name": "Telemetry Service",
                "category": "Infrastructure",
                "platform": {
                    "windows": {
                        "command": [sys.executable, str(testrade_root / "core" / "services" / "telemetry_service_dual_mode.py")],
                        "cwd": str(testrade_root)
                    }
                }
            },
            "Main": {
                "display_name": "Main Trading Engine",
                "category": "Core",
                "platform": {
                    "windows": {
                        "command": [sys.executable, str(testrade_root / "main.py")],
                        "cwd": str(testrade_root),
                        "env": {"PYTHONUNBUFFERED": "1"}
                    }
                }
            },
            "GUI": {
                "display_name": "GUI Backend",
                "category": "Interface",
                "platform": {
                    "windows": {
                        "command": [sys.executable, str(testrade_root / "run_gui_backend.py")],
                        "cwd": str(testrade_root)
                    }
                }
            }
        }
    
    async def start_process(self, process_name: str) -> Dict[str, Any]:
        """Start a specific process"""
        logger.info(f"Starting process: {process_name}")
        
        # Check Redis on first use
        if not self._redis_checked:
            await self._check_external_redis()
            self._redis_checked = True
        
        if process_name not in self.process_definitions:
            return {
                "success": False,
                "error": f"Unknown process: {process_name}"
            }
        
        # Check if already running
        if process_name in self.subprocesses and self.subprocesses[process_name].poll() is None:
            return {
                "success": True,
                "message": f"{process_name} is already running",
                "process_id": self.subprocesses[process_name].pid
            }
        
        try:
            # Get platform-specific command
            platform_name = "windows" if platform.system() == "Windows" else "linux"
            definition = self.process_definitions[process_name]
            
            if "platform" not in definition or platform_name not in definition["platform"]:
                return {
                    "success": False,
                    "error": f"{process_name} not supported on {platform_name}"
                }
            
            platform_config = definition["platform"][platform_name]
            command = platform_config["command"]
            cwd = platform_config.get("cwd", None)
            env = os.environ.copy()
            if "env" in platform_config:
                env.update(platform_config["env"])
            
            # Special check for Redis - see if it's already running
            if process_name == "Redis" and "check_command" in platform_config:
                try:
                    logger.info(f"Checking if Redis is already running with: {' '.join(platform_config['check_command'])}")
                    check_result = subprocess.run(
                        platform_config["check_command"],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    logger.info(f"Redis check result: returncode={check_result.returncode}, stdout='{check_result.stdout.strip()}'")
                    if check_result.returncode == 0 and "PONG" in check_result.stdout:
                        logger.info("Redis is already running externally - not starting new instance")
                        self.processes[process_name].state = "running"
                        self.processes[process_name].pid = None  # External process
                        self.processes[process_name].external = True
                        self.processes[process_name].start_time = time.time()
                        return {
                            "success": True,
                            "message": "Redis is already running (external process)",
                            "external": True
                        }
                except Exception as e:
                    logger.error(f"Redis check failed: {e}")
                    # Continue to start Redis normally
            
            # Update process state
            self.processes[process_name].state = "starting"
            self.processes[process_name].last_error = None
            
            # Start the process
            logger.info(f"Executing: {' '.join(command)}")
            
            # Special handling for WSL commands
            if command[0] == "wsl":
                # WSL needs special subprocess flags
                startupinfo = None
                creationflags = 0
            else:
                # Check console mode for this process
                integrated = is_integrated_mode(process_name)
                
                if integrated:
                    # Windows console handling - hide console for integration
                    if platform.system() == "Windows":
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = subprocess.SW_HIDE
                        creationflags = subprocess.CREATE_NO_WINDOW
                    else:
                        startupinfo = None
                        creationflags = 0
                else:
                    # Standalone console mode
                    if platform.system() == "Windows":
                        startupinfo = None
                        creationflags = subprocess.CREATE_NEW_CONSOLE
                    else:
                        startupinfo = None
                        creationflags = 0
            
            # Capture output if in integrated mode
            if is_integrated_mode(process_name):
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    startupinfo=startupinfo,
                    creationflags=creationflags
                )
            else:
                # Standalone mode - no output capture
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    startupinfo=startupinfo,
                    creationflags=creationflags
                )
            
            self.subprocesses[process_name] = proc
            
            # Wait a moment to check if process started successfully
            await asyncio.sleep(0.5)
            
            if proc.poll() is None:
                # Process is running
                self.processes[process_name].pid = proc.pid
                self.processes[process_name].state = "running"
                self.processes[process_name].start_time = time.time()
                
                # Start monitoring
                if process_name in self.monitoring_tasks:
                    self.monitoring_tasks[process_name].cancel()
                
                self.monitoring_tasks[process_name] = asyncio.create_task(
                    self._monitor_process(process_name)
                )
                
                logger.info(f"{process_name} started successfully (PID: {proc.pid})")
                
                return {
                    "success": True,
                    "process_id": proc.pid,
                    "message": f"{process_name} started successfully",
                    "has_console_capture": is_integrated_mode(process_name)
                }
            else:
                # Process failed to start
                self.processes[process_name].state = "failed"
                self.processes[process_name].exit_code = proc.returncode
                
                # Try to get error output
                try:
                    stdout, _ = proc.communicate(timeout=1)
                    error_msg = stdout.strip() if stdout else f"Process exited with code {proc.returncode}"
                except:
                    error_msg = f"Process exited immediately with code {proc.returncode}"
                
                self.processes[process_name].last_error = error_msg
                
                return {
                    "success": False,
                    "error": error_msg,
                    "exit_code": proc.returncode
                }
                
        except Exception as e:
            logger.error(f"Failed to start {process_name}: {e}")
            self.processes[process_name].state = "failed"
            self.processes[process_name].last_error = str(e)
            
            return {
                "success": False,
                "error": str(e)
            }
    
    async def stop_process(self, process_name: str) -> Dict[str, Any]:
        """Stop a specific process gracefully"""
        logger.info(f"Stopping process: {process_name}")
        
        # Check if it's an external process
        if self.processes[process_name].external:
            if process_name == "Redis":
                return {
                    "success": False,
                    "message": "Redis is running externally in WSL and cannot be stopped by Mission Control"
                }
            return {
                "success": False,
                "message": f"{process_name} is an external process and cannot be stopped"
            }
        
        if process_name not in self.subprocesses:
            return {
                "success": True,
                "message": f"{process_name} is not running"
            }
        
        proc = self.subprocesses[process_name]
        
        if proc.poll() is not None:
            # Already stopped
            del self.subprocesses[process_name]
            self.processes[process_name].state = "stopped"
            return {
                "success": True,
                "message": f"{process_name} was already stopped"
            }
        
        try:
            # Cancel monitoring
            if process_name in self.monitoring_tasks:
                self.monitoring_tasks[process_name].cancel()
            
            # For Windows, use taskkill for more reliable termination
            if platform.system() == "Windows" and proc.pid:
                try:
                    # Kill the process tree to ensure all child processes are terminated
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=5
                    )
                    logger.info(f"Forcefully terminated {process_name} process tree")
                except Exception as e:
                    logger.warning(f"Failed to use taskkill: {e}, falling back to terminate()")
                    proc.terminate()
            else:
                # Unix-style termination
                proc.terminate()
            
            # Wait for process to end
            try:
                await asyncio.wait_for(
                    asyncio.create_task(self._wait_for_process(proc)),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                # Force kill if still running
                logger.warning(f"{process_name} did not stop, forcing kill")
                proc.kill()
                await asyncio.sleep(0.5)  # Brief wait after kill
            
            # Update state
            self.processes[process_name].state = "stopped"
            self.processes[process_name].pid = None
            self.processes[process_name].exit_code = proc.returncode
            
            del self.subprocesses[process_name]
            
            logger.info(f"{process_name} stopped successfully")
            
            return {
                "success": True,
                "message": f"{process_name} stopped successfully"
            }
            
        except Exception as e:
            logger.error(f"Failed to stop {process_name}: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def restart_process(self, process_name: str) -> Dict[str, Any]:
        """Restart a process"""
        logger.info(f"Restarting process: {process_name}")
        
        # Check if it's an external process
        if self.processes[process_name].external:
            if process_name == "Redis":
                return {
                    "success": False,
                    "message": "Redis is running externally in WSL and cannot be restarted by Mission Control"
                }
            return {
                "success": False,
                "message": f"{process_name} is an external process and cannot be restarted"
            }
        
        # Stop if running
        await self.stop_process(process_name)
        
        # Wait a moment
        await asyncio.sleep(1.0)
        
        # Start again
        result = await self.start_process(process_name)
        
        if result["success"]:
            self.processes[process_name].restart_count += 1
        
        return result
    
    async def _monitor_process(self, process_name: str):
        """Monitor a process for health and resource usage"""
        proc = self.subprocesses.get(process_name)
        if not proc:
            return
        
        try:
            while proc.poll() is None:
                # Get process stats using psutil
                try:
                    ps_proc = psutil.Process(proc.pid)
                    self.processes[process_name].cpu_percent = ps_proc.cpu_percent(interval=0.1)
                    self.processes[process_name].memory_mb = ps_proc.memory_info().rss / 1024 / 1024
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                
                await asyncio.sleep(2.0)  # Check every 2 seconds
            
            # Process has exited
            self.processes[process_name].state = "stopped"
            self.processes[process_name].exit_code = proc.returncode
            
            if proc.returncode != 0:
                self.processes[process_name].state = "failed"
                logger.error(f"{process_name} exited with code {proc.returncode}")
                
                # Call crash callback if set
                if self._crash_callback:
                    try:
                        await self._crash_callback(process_name, proc.returncode)
                    except Exception as e:
                        logger.error(f"Error calling crash callback: {e}")
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error monitoring {process_name}: {e}")
    
    async def _wait_for_process(self, proc: subprocess.Popen):
        """Wait for a process to exit"""
        while proc.poll() is None:
            await asyncio.sleep(0.1)
    
    def get_all_status(self) -> Dict[str, Dict]:
        """Get status of all processes"""
        return {
            name: info.to_dict()
            for name, info in self.processes.items()
        }
    
    def get_running_processes(self) -> List[str]:
        """Get list of currently running processes"""
        return [
            name for name, info in self.processes.items()
            if info.state == "running"
        ]
    
    def get_uptime(self) -> Optional[float]:
        """Get system uptime (time since first process started)"""
        start_times = [
            info.start_time for info in self.processes.values()
            if info.start_time is not None
        ]
        
        if start_times:
            return time.time() - min(start_times)
        return None