"""
Zombie Killer - Ensures complete process cleanup for Mission Control

Handles:
- Orphaned child processes
- Processes holding ports
- ZMQ socket cleanup
- Process tree termination
"""

import os
import psutil
import subprocess
import platform
import logging
from typing import List, Set, Dict, Optional
import time

logger = logging.getLogger(__name__)

# Known TESTRADE ports
TESTRADE_PORTS = {
    5555: "Babysitter IPC Bulk",
    5556: "Babysitter IPC Priority", 
    7777: "Telemetry Service",
    6379: "Redis",
    8000: "Mission Control API",
    8001: "GUI Backend API",
    3000: "Mission Control UI"
}

class ZombieKiller:
    """Ensures complete cleanup of TESTRADE processes"""
    
    def __init__(self):
        self.platform = platform.system()
        
    def find_processes_by_port(self, port: int) -> List[int]:
        """Find all processes using a specific port"""
        pids = set()
        
        try:
            for conn in psutil.net_connections():
                if conn.laddr and conn.laddr.port == port:
                    if conn.pid:
                        pids.add(conn.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
            
        return list(pids)
    
    def find_testrade_processes(self) -> Dict[int, psutil.Process]:
        """Find all TESTRADE-related processes"""
        testrade_processes = {}
        keywords = [
            'testrade', 'babysitter', 'telemetry', 'ocr_accelerator',
            'mission_control', 'gui_backend', 'main.py', 'redis-server'
        ]
        
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # Check process name
                    name = proc.info['name'].lower()
                    cmdline = ' '.join(proc.info['cmdline'] or []).lower()
                    
                    # Check if it's a TESTRADE process
                    for keyword in keywords:
                        if keyword in name or keyword in cmdline:
                            testrade_processes[proc.info['pid']] = proc
                            break
                            
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                    
        except Exception as e:
            logger.error(f"Error finding TESTRADE processes: {e}")
            
        return testrade_processes
    
    def kill_process_tree(self, pid: int, include_parent: bool = True) -> bool:
        """Kill a process and all its children"""
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            
            # Kill children first
            for child in children:
                try:
                    logger.info(f"Killing child process {child.pid} ({child.name()})")
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            # Kill parent
            if include_parent:
                try:
                    logger.info(f"Killing parent process {pid} ({parent.name()})")
                    parent.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                    
            # Wait for processes to die
            gone, alive = psutil.wait_procs(children + ([parent] if include_parent else []), timeout=3)
            
            # Force kill any survivors
            for proc in alive:
                try:
                    logger.warning(f"Force killing stubborn process {proc.pid}")
                    if self.platform == "Windows":
                        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], 
                                     capture_output=True)
                    else:
                        proc.kill()
                except:
                    pass
                    
            return True
            
        except psutil.NoSuchProcess:
            return True
        except Exception as e:
            logger.error(f"Failed to kill process tree for {pid}: {e}")
            return False
    
    def release_port(self, port: int) -> bool:
        """Kill all processes using a specific port"""
        pids = self.find_processes_by_port(port)
        
        if not pids:
            logger.info(f"Port {port} is free")
            return True
            
        logger.info(f"Found {len(pids)} processes using port {port}: {pids}")
        
        success = True
        for pid in pids:
            if not self.kill_process_tree(pid):
                success = False
                
        # Verify port is free
        time.sleep(0.5)
        remaining = self.find_processes_by_port(port)
        if remaining:
            logger.error(f"Failed to free port {port}, processes still using it: {remaining}")
            return False
            
        logger.info(f"Successfully freed port {port}")
        return success
    
    def cleanup_all_ports(self) -> Dict[int, bool]:
        """Release all known TESTRADE ports"""
        results = {}
        
        for port, description in TESTRADE_PORTS.items():
            logger.info(f"Checking port {port} ({description})")
            results[port] = self.release_port(port)
            
        return results
    
    def terminate_all_testrade_processes(self) -> int:
        """Find and terminate all TESTRADE processes"""
        processes = self.find_testrade_processes()
        
        if not processes:
            logger.info("No TESTRADE processes found")
            return 0
            
        logger.info(f"Found {len(processes)} TESTRADE processes")
        
        killed = 0
        for pid, proc in processes.items():
            try:
                logger.info(f"Terminating {proc.name()} (PID: {pid})")
                if self.kill_process_tree(pid):
                    killed += 1
            except:
                pass
                
        return killed
    
    def emergency_cleanup(self) -> Dict[str, any]:
        """Perform complete emergency cleanup"""
        logger.info("Starting emergency cleanup...")
        
        results = {
            "processes_killed": 0,
            "ports_freed": {},
            "errors": []
        }
        
        # First, try to terminate all TESTRADE processes
        try:
            results["processes_killed"] = self.terminate_all_testrade_processes()
        except Exception as e:
            results["errors"].append(f"Process termination error: {e}")
        
        # Then cleanup all ports
        try:
            results["ports_freed"] = self.cleanup_all_ports()
        except Exception as e:
            results["errors"].append(f"Port cleanup error: {e}")
        
        # Final verification
        remaining = self.find_testrade_processes()
        if remaining:
            results["errors"].append(f"{len(remaining)} processes still running")
            
        return results


def cleanup_before_start():
    """Run cleanup before starting Mission Control"""
    killer = ZombieKiller()
    
    # Check for existing Mission Control
    mc_pids = killer.find_processes_by_port(8000)
    if mc_pids:
        logger.info("Found existing Mission Control, cleaning up...")
        for pid in mc_pids:
            killer.kill_process_tree(pid)
    
    # Check critical ports
    critical_ports = [5555, 5556, 7777, 8000, 8001]
    for port in critical_ports:
        if killer.find_processes_by_port(port):
            logger.warning(f"Port {port} is in use, cleaning up...")
            killer.release_port(port)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    killer = ZombieKiller()
    results = killer.emergency_cleanup()
    
    print("\n=== Emergency Cleanup Results ===")
    print(f"Processes killed: {results['processes_killed']}")
    print(f"Ports freed: {results['ports_freed']}")
    if results['errors']:
        print(f"Errors: {results['errors']}")