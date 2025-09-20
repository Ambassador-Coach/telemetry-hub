"""
Intelligent Error Detector for Mission Control

Analyzes startup failures, process crashes, and runtime errors
to provide actionable solutions and recovery suggestions.
"""

import re
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Tuple
import traceback

logger = logging.getLogger(__name__)


@dataclass
class ErrorAnalysis:
    """Detailed error analysis with solutions"""
    error_type: str
    summary: str
    details: str
    solutions: List[str]
    severity: str  # low, medium, high, critical
    auto_recoverable: bool = False
    recovery_actions: Optional[List[str]] = None
    related_processes: Optional[List[str]] = None


class ErrorDetector:
    """
    Intelligent error detection and analysis for TESTRADE processes.
    Provides actionable solutions for common startup and runtime errors.
    """
    
    def __init__(self):
        self.error_patterns = self._build_error_patterns()
        self.solution_database = self._build_solution_database()
        self.alert_patterns = self._build_alert_patterns()
    
    def _build_error_patterns(self) -> Dict[str, Dict[str, Any]]:
        """Build comprehensive error pattern database"""
        return {
            # Network and Connection Errors
            "port_in_use": {
                "patterns": [
                    r"bind.*address already in use",
                    r"EADDRINUSE",
                    r"port\s+(\d+).*already\s+in\s+use",
                    r"Address already in use"
                ],
                "extract": lambda m: {"port": m.group(1) if m.lastindex else None},
                "severity": "high"
            },
            
            "connection_refused": {
                "patterns": [
                    r"Connection refused",
                    r"Failed to connect to .* on port (\d+)",
                    r"ECONNREFUSED",
                    r"target machine actively refused"
                ],
                "extract": lambda m: {"port": m.group(1) if m.lastindex else None},
                "severity": "high"
            },
            
            "redis_connection": {
                "patterns": [
                    r"Could not connect to Redis",
                    r"Redis connection failed",
                    r"redis\.exceptions\.ConnectionError",
                    r"LOADING Redis is loading"
                ],
                "severity": "critical"
            },
            
            # Module and Import Errors
            "missing_module": {
                "patterns": [
                    r"ModuleNotFoundError.*No module named ['\"]([^'\"]+)['\"]",
                    r"ImportError.*cannot import name ['\"]([^'\"]+)['\"]",
                    r"No module named ['\"]([^'\"]+)['\"]"
                ],
                "extract": lambda m: {"module": m.group(1)},
                "severity": "high"
            },
            
            "dll_load_error": {
                "patterns": [
                    r"ImportError.*DLL load failed",
                    r"missing DLL dependencies",
                    r"The specified module could not be found"
                ],
                "severity": "high"
            },
            
            # Broker and Trading Errors
            "broker_connection": {
                "patterns": [
                    r"Could not establish connection with broker",
                    r"Broker handshake failed",
                    r"Lightspeed.*connection.*failed",
                    r"Invalid broker credentials"
                ],
                "severity": "critical"
            },
            
            "broker_auth": {
                "patterns": [
                    r"Authentication failed",
                    r"Invalid API key",
                    r"Unauthorized.*broker",
                    r"Token expired"
                ],
                "severity": "critical"
            },
            
            # Process and System Errors
            "out_of_memory": {
                "patterns": [
                    r"MemoryError",
                    r"Cannot allocate memory",
                    r"out of memory",
                    r"memory allocation failed"
                ],
                "severity": "critical"
            },
            
            "permission_denied": {
                "patterns": [
                    r"Permission denied",
                    r"Access is denied",
                    r"EACCES",
                    r"insufficient privileges"
                ],
                "severity": "high"
            },
            
            # OCR Specific Errors
            "ocr_init_failed": {
                "patterns": [
                    r"Failed to initialize OCR",
                    r"Tesseract.*not found",
                    r"OCR engine initialization failed",
                    r"Could not load tesseract"
                ],
                "severity": "high"
            },
            
            "capture_device_error": {
                "patterns": [
                    r"Failed to open capture device",
                    r"No capture device found",
                    r"Screen capture failed",
                    r"GDI.*failed"
                ],
                "severity": "high"
            },
            
            # Configuration Errors
            "config_missing": {
                "patterns": [
                    r"Configuration file not found",
                    r"control\.json.*not found",
                    r"Missing required configuration"
                ],
                "severity": "critical"
            },
            
            "config_invalid": {
                "patterns": [
                    r"Invalid configuration",
                    r"JSON.*decode.*error",
                    r"Expected .* in config"
                ],
                "severity": "high"
            }
        }
    
    def _build_solution_database(self) -> Dict[str, List[str]]:
        """Build solution database for each error type"""
        return {
            "port_in_use": [
                "Kill the process using this port:",
                "  Windows: netstat -ano | findstr :{port} -> taskkill /PID <pid> /F",
                "  Linux: lsof -i:{port} -> kill -9 <pid>",
                "Change the port in configuration",
                "Restart the conflicting service with a different port"
            ],
            
            "connection_refused": [
                "Ensure the target service is running",
                "Check if firewall is blocking the connection",
                "Verify the correct host and port in configuration",
                "For Redis: Start Redis service in WSL",
                "For Broker: Ensure Lightspeed Gateway is running"
            ],
            
            "redis_connection": [
                "Start Redis in WSL: wsl -d Ubuntu -- redis-server",
                "Check Redis is running: wsl -d Ubuntu -- redis-cli ping",
                "Verify Redis host in control.json (should be WSL IP)",
                "Check WSL networking: wsl hostname -I",
                "Restart WSL if needed: wsl --shutdown"
            ],
            
            "missing_module": [
                "Install the missing module: pip install {module}",
                "Activate virtual environment: .venv\\Scripts\\activate",
                "Check requirements.txt and install all: pip install -r requirements.txt",
                "For C++ modules, ensure they are built: rebuild_ocr.bat"
            ],
            
            "dll_load_error": [
                "Install Visual C++ Redistributables",
                "Check PATH environment variable",
                "Reinstall the package: pip uninstall <package> && pip install <package>",
                "For OpenCV: Install from https://opencv.org/releases/",
                "Run Dependency Walker to check missing DLLs"
            ],
            
            "broker_connection": [
                "Ensure Lightspeed Gateway is running",
                "Check broker configuration in control.json",
                "Verify network connectivity to broker",
                "Check if using correct environment (paper/live)",
                "Contact broker support if credentials are correct"
            ],
            
            "broker_auth": [
                "Verify API credentials in control.json",
                "Check if API key has expired",
                "Ensure using correct environment credentials",
                "Generate new API keys from broker portal",
                "Check account permissions and trading access"
            ],
            
            "out_of_memory": [
                "Close other applications to free memory",
                "Increase virtual memory/swap space",
                "Check for memory leaks in logs",
                "Reduce batch sizes in configuration",
                "Monitor with Task Manager/top"
            ],
            
            "permission_denied": [
                "Run as Administrator (Windows) or with sudo (Linux)",
                "Check file/folder permissions",
                "Ensure no other process is locking the file",
                "For logs: Create logs directory with write permissions",
                "Check antivirus/security software"
            ],
            
            "ocr_init_failed": [
                "Install Tesseract OCR: https://github.com/tesseract-ocr/tesseract",
                "Set TESSERACT_CMD in control.json to tesseract.exe path",
                "Add Tesseract to PATH environment variable",
                "Verify installation: tesseract --version",
                "Download language data files if missing"
            ],
            
            "capture_device_error": [
                "Check display settings and resolution",
                "Update graphics drivers",
                "Disable hardware acceleration if needed",
                "Check ROI coordinates in control.json",
                "Try running with a single monitor"
            ],
            
            "config_missing": [
                "Create control.json from template",
                "Copy control.json.example to control.json",
                "Verify working directory is TESTRADE root",
                "Check file permissions on utils folder"
            ],
            
            "config_invalid": [
                "Validate JSON syntax: https://jsonlint.com/",
                "Check for missing commas or quotes",
                "Ensure all required fields are present",
                "Compare with control.json.example",
                "Look for the specific line mentioned in error"
            ]
        }
    
    def _build_alert_patterns(self) -> List[Dict[str, Any]]:
        """Build patterns for runtime alerts"""
        return [
            {
                "pattern": re.compile(r"memory.*usage.*(\d+).*%", re.I),
                "type": "resource_warning",
                "extract": lambda m: {"memory_percent": int(m.group(1))},
                "threshold": 85
            },
            {
                "pattern": re.compile(r"latency.*(\d+).*ms", re.I),
                "type": "performance_warning",
                "extract": lambda m: {"latency_ms": int(m.group(1))},
                "threshold": 100
            },
            {
                "pattern": re.compile(r"queue.*full|queue.*overflow", re.I),
                "type": "queue_warning"
            },
            {
                "pattern": re.compile(r"connection.*lost|disconnected", re.I),
                "type": "connection_alert"
            }
        ]
    
    def analyze_startup_error(self, process_name: str, error_text: str) -> ErrorAnalysis:
        """Analyze a startup error and provide solutions"""
        error_text = error_text.strip()
        
        # Check against known patterns
        for error_type, config in self.error_patterns.items():
            for pattern_str in config["patterns"]:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                match = pattern.search(error_text)
                
                if match:
                    # Extract additional context
                    context = {}
                    if "extract" in config and match:
                        try:
                            context = config["extract"](match)
                        except:
                            pass
                    
                    # Get solutions
                    solutions = self.solution_database.get(error_type, ["Check logs for more details"])
                    
                    # Format solutions with context
                    formatted_solutions = []
                    for solution in solutions:
                        if context:
                            solution = solution.format(**context)
                        formatted_solutions.append(solution)
                    
                    return ErrorAnalysis(
                        error_type=error_type,
                        summary=f"{process_name} failed to start: {error_type.replace('_', ' ').title()}",
                        details=error_text[:500],  # Truncate long errors
                        solutions=formatted_solutions,
                        severity=config.get("severity", "medium"),
                        auto_recoverable=error_type in ["port_in_use", "connection_refused"],
                        recovery_actions=self._get_recovery_actions(error_type, process_name),
                        related_processes=self._get_related_processes(error_type, process_name)
                    )
        
        # Unknown error - provide generic analysis
        return ErrorAnalysis(
            error_type="unknown_error",
            summary=f"{process_name} failed to start",
            details=error_text[:500],
            solutions=[
                "Check the full error message above",
                "Look for specific error codes or messages",
                "Check if all dependencies are installed",
                "Verify configuration in control.json",
                "Check system logs for more details"
            ],
            severity="medium"
        )
    
    def analyze_process_failure(self, process_name: str, logs: List[str]) -> ErrorAnalysis:
        """Analyze why a process failed based on recent logs"""
        # Combine recent logs
        combined_logs = "\n".join(logs[-50:])  # Last 50 lines
        
        # First check for Python tracebacks
        if "Traceback" in combined_logs:
            return self._analyze_traceback(process_name, combined_logs)
        
        # Then check for known error patterns
        return self.analyze_startup_error(process_name, combined_logs)
    
    def _analyze_traceback(self, process_name: str, logs: str) -> ErrorAnalysis:
        """Analyze Python traceback"""
        # Extract the actual exception
        exception_match = re.search(r"(\w+Error|\w+Exception):\s*(.+?)(?:\n|$)", logs)
        
        if exception_match:
            exception_type = exception_match.group(1)
            exception_msg = exception_match.group(2)
            
            # Check if it matches known patterns
            for error_type, config in self.error_patterns.items():
                for pattern_str in config["patterns"]:
                    if re.search(pattern_str, exception_msg, re.IGNORECASE):
                        return self.analyze_startup_error(process_name, exception_msg)
            
            # Generic Python exception
            return ErrorAnalysis(
                error_type="python_exception",
                summary=f"{process_name} crashed with {exception_type}",
                details=exception_msg,
                solutions=[
                    f"Check the code that caused {exception_type}",
                    "Review the full traceback in logs",
                    "Check if all required services are running",
                    "Verify data inputs and configuration"
                ],
                severity="high"
            )
        
        return self.analyze_startup_error(process_name, logs)
    
    def analyze_launch_failure(self, error_text: str) -> Dict[str, Any]:
        """Analyze why a launch operation failed"""
        analysis = self.analyze_startup_error("Launch", error_text)
        
        return {
            "error_type": analysis.error_type,
            "summary": analysis.summary,
            "details": analysis.details,
            "solutions": analysis.solutions,
            "severity": analysis.severity
        }
    
    def check_for_alerts(self, process_name: str, log_line: str) -> Optional[Dict[str, Any]]:
        """Check if a log line contains an alert condition"""
        for alert_config in self.alert_patterns:
            match = alert_config["pattern"].search(log_line)
            
            if match:
                alert = {
                    "type": alert_config["type"],
                    "process": process_name,
                    "message": log_line,
                    "timestamp": datetime.now().isoformat()
                }
                
                # Extract values if available
                if "extract" in alert_config:
                    values = alert_config["extract"](match)
                    alert["values"] = values
                    
                    # Check threshold
                    if "threshold" in alert_config:
                        for key, value in values.items():
                            if isinstance(value, (int, float)) and value > alert_config["threshold"]:
                                alert["severity"] = "high"
                                alert["threshold_exceeded"] = True
                                break
                
                return alert
        
        return None
    
    def _get_recovery_actions(self, error_type: str, process_name: str) -> List[str]:
        """Get automated recovery actions for an error type"""
        recovery_map = {
            "port_in_use": ["find_and_kill_process", "change_port", "restart"],
            "connection_refused": ["wait_and_retry", "start_dependency", "restart"],
            "redis_connection": ["start_redis", "wait_and_retry"],
            "missing_module": ["install_requirements", "activate_venv"],
            "broker_connection": ["check_broker_status", "wait_and_retry"]
        }
        
        return recovery_map.get(error_type, ["restart"])
    
    def _get_related_processes(self, error_type: str, process_name: str) -> List[str]:
        """Get processes that might be affected by this error"""
        related_map = {
            "redis_connection": ["Babysitter", "GUI", "Main"],
            "broker_connection": ["Main", "TradeManager"],
            "ocr_init_failed": ["Main"],
            "port_in_use": [process_name]  # Only affects itself
        }
        
        return related_map.get(error_type, [])


from datetime import datetime