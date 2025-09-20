"""
Distributed Process Monitor for Mission Control
Manages processes across TELEMETRY and BEAST machines
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

logger = logging.getLogger(__name__)


@dataclass
class ProcessInfo:
    """Information about a managed process"""
    name: str
    display_name: str
    machine: str  # TELEMETRY or BEAST
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


class DistributedProcessMonitor:
    """
    Monitors and manages processes across TELEMETRY and BEAST machines.
    TELEMETRY processes are managed locally, BEAST processes via SSH.
    """
    
    def __init__(self):
        self.processes: Dict[str, ProcessInfo] = {}
        self.subprocesses: Dict[str, subprocess.Popen] = {}
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        
        # Machine configuration
        self.current_machine = "TELEMETRY"  # This runs on TELEMETRY
        self.beast_ip = "192.168.50.100"
        self.telemetry_ip = "192.168.50.101"
        
        # Load process definitions
        self.process_definitions = self._load_process_definitions()
        
        # Initialize process info
        for name, definition in self.process_definitions.items():
            self.processes[name] = ProcessInfo(
                name=name,
                display_name=definition["display_name"],
                machine=definition.get("machine", "TELEMETRY")
            )
        
        # Crash notification callback
        self._crash_callback = None
    
    async def initialize(self, crash_callback=None):
        """Initialize the distributed process monitor"""
        self._crash_callback = crash_callback
        await self._check_local_redis()
        await self._check_beast_connectivity()
    
    async def _check_local_redis(self):
        """Check if Redis is running locally on TELEMETRY"""
        try:
            check_result = subprocess.run(
                ["redis-cli", "ping"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if check_result.returncode == 0 and "PONG" in check_result.stdout:
                logger.info("Redis is running on TELEMETRY")
                self.processes["Redis"].state = "running"
                self.processes["Redis"].external = True
                self.processes["Redis"].start_time = time.time()
        except Exception as e:
            logger.debug(f"Redis check failed: {e}")
    
    async def _check_beast_connectivity(self):
        """Check SSH connectivity to BEAST machine"""
        try:
            result = subprocess.run(
                ["ssh", f"beast@{self.beast_ip}", "echo", "connected"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and "connected" in result.stdout:
                logger.info(f"SSH connectivity to BEAST ({self.beast_ip}) confirmed")
                return True
        except Exception as e:
            logger.warning(f"Cannot connect to BEAST via SSH: {e}")
        return False
    
    def _load_process_definitions(self) -> Dict[str, Any]:
        """Load process definitions for distributed architecture"""
        telemetry_root = Path("/home/telemetry/telemetry-hub")
        beast_root = Path("C:\\TESTRADE")  # Windows path on BEAST
        
        return {
            # ========== TELEMETRY Machine Processes ==========
            "Redis": {
                "display_name": "Redis Server",
                "category": "Infrastructure",
                "machine": "TELEMETRY",
                "command": ["redis-server"],
                "check_command": ["redis-cli", "ping"],
                "local": True
            },
            
            "TelemetryService": {
                "display_name": "Telemetry Service",
                "category": "Infrastructure", 
                "machine": "TELEMETRY",
                "command": [
                    sys.executable,
                    str(telemetry_root / "services" / "telemetry" / "telemetry_service_dual_mode.py"),
                    str(telemetry_root / "config" / "control.json")
                ],
                "cwd": str(telemetry_root),
                "local": True
            },
            
            "BabysitterService": {
                "display_name": "Babysitter Service",
                "category": "Infrastructure",
                "machine": "TELEMETRY",
                "command": [
                    sys.executable,
                    str(telemetry_root / "services" / "babysitter" / "babysitter_service.py"),
                    str(telemetry_root / "config" / "control.json")
                ],
                "cwd": str(telemetry_root),
                "local": True
            },
            
            "GUIBackend": {
                "display_name": "GUI Backend",
                "category": "User Interface",
                "machine": "TELEMETRY",
                "command": [
                    sys.executable,
                    str(telemetry_root / "services" / "gui" / "run_gui_backend.py")
                ],
                "cwd": str(telemetry_root),
                "local": True,
                "health_check": "http://localhost:8001/health"
            },
            
            # ========== BEAST Machine Processes (Remote) ==========
            "Main": {
                "display_name": "Main Trading Engine",
                "category": "Core",
                "machine": "BEAST",
                "remote_command": f"cd {beast_root} && python main.py",
                "remote": True,
                "ssh_user": "beast"
            },
            
            "OCRProcess": {
                "display_name": "OCR Process",
                "category": "Core",
                "machine": "BEAST",
                "remote_command": f"cd {beast_root} && python modules\\ocr\\ocr_process_main.py",
                "remote": True,
                "ssh_user": "beast"
            }
        }
    
    async def start_process(self, process_name: str) -> bool:
        """Start a process (local or remote)"""
        if process_name not in self.process_definitions:
            logger.error(f"Unknown process: {process_name}")
            return False
        
        definition = self.process_definitions[process_name]
        process_info = self.processes[process_name]
        
        if process_info.state == "running":
            logger.info(f"{process_name} is already running")
            return True
        
        process_info.state = "starting"
        
        try:
            if definition.get("local", False):
                # Start local process on TELEMETRY
                return await self._start_local_process(process_name, definition)
            elif definition.get("remote", False):
                # Start remote process on BEAST via SSH
                return await self._start_remote_process(process_name, definition)
        except Exception as e:
            logger.error(f"Failed to start {process_name}: {e}")
            process_info.state = "failed"
            process_info.last_error = str(e)
            return False
    
    async def _start_local_process(self, process_name: str, definition: Dict) -> bool:
        """Start a process locally on TELEMETRY"""
        process_info = self.processes[process_name]
        
        try:
            # Special handling for Redis
            if process_name == "Redis":
                result = subprocess.run(
                    ["sudo", "systemctl", "start", "redis-server"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    process_info.state = "running"
                    process_info.start_time = time.time()
                    logger.info(f"Started {process_name}")
                    return True
            else:
                # Start other processes
                proc = subprocess.Popen(
                    definition["command"],
                    cwd=definition.get("cwd"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                self.subprocesses[process_name] = proc
                process_info.pid = proc.pid
                process_info.state = "running"
                process_info.start_time = time.time()
                
                # Start monitoring task
                self.monitoring_tasks[process_name] = asyncio.create_task(
                    self._monitor_process(process_name)
                )
                
                logger.info(f"Started {process_name} (PID: {proc.pid})")
                return True
                
        except Exception as e:
            logger.error(f"Failed to start local process {process_name}: {e}")
            process_info.state = "failed"
            process_info.last_error = str(e)
            return False
    
    async def _start_remote_process(self, process_name: str, definition: Dict) -> bool:
        """Start a process on BEAST via SSH"""
        process_info = self.processes[process_name]
        
        try:
            ssh_user = definition.get("ssh_user", "beast")
            remote_command = definition["remote_command"]
            
            # Start process on BEAST via SSH (non-blocking)
            ssh_command = [
                "ssh",
                f"{ssh_user}@{self.beast_ip}",
                f"start /B {remote_command}"  # Windows background start
            ]
            
            result = subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                process_info.state = "running"
                process_info.start_time = time.time()
                logger.info(f"Started {process_name} on BEAST")
                return True
            else:
                raise Exception(f"SSH command failed: {result.stderr}")
                
        except Exception as e:
            logger.error(f"Failed to start remote process {process_name}: {e}")
            process_info.state = "failed"
            process_info.last_error = str(e)
            return False
    
    async def stop_process(self, process_name: str) -> bool:
        """Stop a process (local or remote)"""
        if process_name not in self.processes:
            return False
        
        process_info = self.processes[process_name]
        definition = self.process_definitions[process_name]
        
        if definition.get("local", False):
            return await self._stop_local_process(process_name)
        elif definition.get("remote", False):
            return await self._stop_remote_process(process_name)
    
    async def _stop_local_process(self, process_name: str) -> bool:
        """Stop a local process on TELEMETRY"""
        if process_name in self.subprocesses:
            proc = self.subprocesses[process_name]
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            del self.subprocesses[process_name]
        
        if process_name in self.monitoring_tasks:
            self.monitoring_tasks[process_name].cancel()
            del self.monitoring_tasks[process_name]
        
        self.processes[process_name].state = "stopped"
        self.processes[process_name].pid = None
        return True
    
    async def _stop_remote_process(self, process_name: str) -> bool:
        """Stop a remote process on BEAST via SSH"""
        definition = self.process_definitions[process_name]
        ssh_user = definition.get("ssh_user", "beast")
        
        # Use taskkill on Windows to stop the process
        if process_name == "Main":
            kill_command = "taskkill /F /IM python.exe /FI \"WINDOWTITLE eq *main.py*\""
        elif process_name == "OCRProcess":
            kill_command = "taskkill /F /IM python.exe /FI \"WINDOWTITLE eq *ocr_process*\""
        else:
            kill_command = f"taskkill /F /IM {process_name}.exe"
        
        ssh_command = [
            "ssh",
            f"{ssh_user}@{self.beast_ip}",
            kill_command
        ]
        
        try:
            subprocess.run(ssh_command, capture_output=True, timeout=10)
            self.processes[process_name].state = "stopped"
            return True
        except Exception as e:
            logger.error(f"Failed to stop remote process {process_name}: {e}")
            return False
    
    async def _monitor_process(self, process_name: str):
        """Monitor a local process health"""
        proc = self.subprocesses.get(process_name)
        if not proc:
            return
        
        process_info = self.processes[process_name]
        
        while proc.poll() is None:
            try:
                # Get process metrics
                ps_proc = psutil.Process(proc.pid)
                process_info.cpu_percent = ps_proc.cpu_percent()
                process_info.memory_mb = ps_proc.memory_info().rss / 1024 / 1024
                
                await asyncio.sleep(5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
        
        # Process has exited
        process_info.state = "stopped"
        process_info.exit_code = proc.returncode
        
        if self._crash_callback and proc.returncode != 0:
            await self._crash_callback(process_name, proc.returncode)
    
    def get_all_status(self) -> Dict[str, Dict]:
        """Get status of all processes"""
        return {name: info.to_dict() for name, info in self.processes.items()}
    
    def get_process_status(self, process_name: str) -> Optional[Dict]:
        """Get status of a specific process"""
        if process_name in self.processes:
            return self.processes[process_name].to_dict()
        return None