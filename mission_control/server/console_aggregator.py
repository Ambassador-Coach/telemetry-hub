"""
Console Aggregator for Mission Control

Captures, filters, and manages console output from all processes
with intelligent parsing and alert detection.
"""

import asyncio
import re
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any, Deque
import json

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """Structured log entry"""
    timestamp: datetime
    process: str
    level: str  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    message: str
    raw: str
    metadata: Optional[Dict[str, Any]] = None


class LogParser:
    """Parses log lines into structured entries"""
    
    # Common Python logging format
    PYTHON_LOG_PATTERN = re.compile(
        r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[,\.]\d+)\s*-\s*'
        r'(?:PID:\d+\s*-\s*)?'  # Optional PID
        r'([\w\.]+)\s*-\s*'     # Logger name
        r'(\w+)\s*-\s*'         # Level
        r'(.+)$'                # Message
    )
    
    # Trading-specific patterns
    TRADING_PATTERNS = {
        "trade_signal": re.compile(r'Trade\s+Signal|ORDER\s+SIGNAL|signal\s+generated', re.I),
        "order_placed": re.compile(r'Order\s+placed|ORDER\s+SUBMITTED|Submitted\s+order', re.I),
        "order_filled": re.compile(r'Order\s+filled|FILL\s+received|Execution\s+confirmed', re.I),
        "position_update": re.compile(r'Position\s+update|PnL:|Realized\s+PnL', re.I),
        "risk_alert": re.compile(r'Risk\s+limit|RISK\s+WARNING|Exposure\s+exceeded', re.I),
    }
    
    # Error patterns
    ERROR_PATTERNS = {
        "connection_error": re.compile(r'Connection\s+refused|Failed\s+to\s+connect|Socket\s+error', re.I),
        "broker_error": re.compile(r'Broker\s+error|Order\s+rejected|Invalid\s+order', re.I),
        "data_error": re.compile(r'Data\s+error|Missing\s+data|Invalid\s+data', re.I),
        "process_crash": re.compile(r'Traceback|Exception|Fatal\s+error|Segmentation\s+fault', re.I),
    }
    
    @classmethod
    def parse_line(cls, line: str, process_name: str) -> LogEntry:
        """Parse a log line into a structured entry"""
        line = line.strip()
        if not line:
            return None
        
        # Try to parse Python logging format
        match = cls.PYTHON_LOG_PATTERN.match(line)
        if match:
            timestamp_str, logger_name, level, message = match.groups()
            
            # Parse timestamp
            try:
                # Handle both comma and dot separators for milliseconds
                if ',' in timestamp_str:
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S,%f')
                else:
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
            except:
                timestamp = datetime.now()
            
            # Extract metadata from message
            metadata = cls._extract_metadata(message)
            
            return LogEntry(
                timestamp=timestamp,
                process=process_name,
                level=level.upper(),
                message=message,
                raw=line,
                metadata=metadata
            )
        else:
            # Unstructured line - try to detect level
            level = "INFO"
            if any(word in line.upper() for word in ["ERROR", "EXCEPTION", "TRACEBACK", "FATAL"]):
                level = "ERROR"
            elif any(word in line.upper() for word in ["WARNING", "WARN"]):
                level = "WARNING"
            elif any(word in line.upper() for word in ["DEBUG", "TRACE"]):
                level = "DEBUG"
            
            return LogEntry(
                timestamp=datetime.now(),
                process=process_name,
                level=level,
                message=line,
                raw=line,
                metadata=cls._extract_metadata(line)
            )
    
    @classmethod
    def _extract_metadata(cls, message: str) -> Dict[str, Any]:
        """Extract structured metadata from message"""
        metadata = {}
        
        # Check for trading patterns
        for pattern_name, pattern in cls.TRADING_PATTERNS.items():
            if pattern.search(message):
                metadata["trading_event"] = pattern_name
                break
        
        # Check for error patterns
        for pattern_name, pattern in cls.ERROR_PATTERNS.items():
            if pattern.search(message):
                metadata["error_type"] = pattern_name
                break
        
        # Extract correlation IDs
        corr_id_match = re.search(r'correlation[_-]?id[:\s]+([a-f0-9-]+)', message, re.I)
        if corr_id_match:
            metadata["correlation_id"] = corr_id_match.group(1)
        
        # Extract numeric values (prices, quantities, etc)
        numbers = re.findall(r'\$?([\d,]+\.?\d*)', message)
        if numbers:
            try:
                metadata["values"] = [float(n.replace(',', '')) for n in numbers[:5] if n]  # First 5 numbers
            except ValueError:
                pass  # Skip invalid numbers
        
        # Extract symbols
        symbol_match = re.search(r'\b([A-Z]{1,5})\b(?:\s+|$)', message)
        if symbol_match and symbol_match.group(1) not in ['ERROR', 'INFO', 'DEBUG', 'WARNING']:
            metadata["symbol"] = symbol_match.group(1)
        
        return metadata if metadata else None


class ConsoleAggregator:
    """
    Aggregates console output from all processes with filtering and search capabilities.
    """
    
    def __init__(self, max_lines_per_process: int = 10000):
        self.max_lines = max_lines_per_process
        self.buffers: Dict[str, Deque[LogEntry]] = {}
        self.subscribers: List[Callable] = []
        self.filters: Dict[str, Dict[str, Any]] = {}
        
        # Alert rules
        self.alert_rules = self._setup_alert_rules()
    
    def _setup_alert_rules(self) -> List[Dict[str, Any]]:
        """Setup default alert rules"""
        return [
            {
                "name": "Critical Error",
                "condition": lambda entry: entry.level in ["ERROR", "CRITICAL"],
                "severity": "high",
                "cooldown": 60  # Don't repeat same alert for 60 seconds
            },
            {
                "name": "Connection Lost",
                "condition": lambda entry: entry.metadata and entry.metadata.get("error_type") == "connection_error",
                "severity": "critical",
                "cooldown": 300
            },
            {
                "name": "Trading Signal Generated",
                "condition": lambda entry: entry.metadata and entry.metadata.get("trading_event") == "trade_signal",
                "severity": "info",
                "cooldown": 0  # No cooldown for trading events
            },
            {
                "name": "Risk Warning",
                "condition": lambda entry: "risk" in entry.message.lower() and entry.level == "WARNING",
                "severity": "medium",
                "cooldown": 120
            }
        ]
    
    async def capture_process_output(
        self, 
        process_name: str, 
        process_id: int,
        subprocess_obj: Any = None,
        callback: Optional[Callable] = None
    ):
        """Capture output from a process"""
        import subprocess
        import asyncio
        
        try:
            logger.info(f"Starting console capture for {process_name} (PID: {process_id})")
            
            # Initialize buffer for this process
            if process_name not in self.buffers:
                self.buffers[process_name] = deque(maxlen=self.max_lines)
            
            # If we have a subprocess object with stdout pipe
            if subprocess_obj and hasattr(subprocess_obj, 'stdout') and subprocess_obj.stdout:
                while True:
                    try:
                        line = await asyncio.get_event_loop().run_in_executor(
                            None, subprocess_obj.stdout.readline
                        )
                        
                        if not line:  # Process ended
                            break
                            
                        # Process the line
                        line = line.strip()
                        if line:
                            entry = LogParser.parse_line(line, process_name)
                            if entry:
                                self.buffers[process_name].append(entry)
                                
                                # Notify subscribers
                                if callback:
                                    await callback(process_name, line)
                                
                                # Check alerts
                                self._check_alerts(process_name, entry)
                                
                    except Exception as e:
                        logger.error(f"Error reading output from {process_name}: {e}")
                        break
                        
                logger.info(f"Console capture ended for {process_name}")
            else:
                logger.warning(f"No stdout available for {process_name} - console capture not possible")
            
        except Exception as e:
            logger.error(f"Failed to capture output for {process_name}: {e}")
    
    def _check_alerts(self, process_name: str, entry: LogEntry):
        """Check if entry triggers any alerts"""
        for rule in self.alert_rules:
            if rule["condition"](entry):
                # TODO: Implement cooldown logic
                alert = {
                    "rule": rule["name"],
                    "severity": rule["severity"],
                    "process": process_name,
                    "message": entry.message,
                    "timestamp": entry.timestamp.isoformat()
                }
                # Notify subscribers about alert
                for subscriber in self.subscribers:
                    try:
                        subscriber("alert", alert)
                    except Exception as e:
                        logger.error(f"Error notifying subscriber: {e}")
    
    def add_log_entry(self, process_name: str, line: str):
        """Add a log entry (for testing or manual addition)"""
        if process_name not in self.buffers:
            self.buffers[process_name] = deque(maxlen=self.max_lines)
        
        entry = LogParser.parse_line(line, process_name)
        if entry:
            self.buffers[process_name].append(entry)
            
            # Check alerts
            for rule in self.alert_rules:
                if rule["condition"](entry):
                    # Check cooldown
                    # TODO: Implement cooldown logic
                    alert = {
                        "rule": rule["name"],
                        "severity": rule["severity"],
                        "process": process_name,
                        "message": entry.message,
                        "timestamp": entry.timestamp.isoformat()
                    }
                    
                    # Notify subscribers
                    for subscriber in self.subscribers:
                        try:
                            subscriber(process_name, entry, alert)
                        except Exception as e:
                            logger.error(f"Error in console subscriber: {e}")
    
    def get_recent_logs(
        self, 
        process_name: str, 
        count: int = 100,
        level_filter: Optional[List[str]] = None,
        search_text: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get recent logs with optional filtering"""
        if process_name not in self.buffers:
            return []
        
        logs = list(self.buffers[process_name])
        
        # Apply filters
        if level_filter:
            logs = [log for log in logs if log.level in level_filter]
        
        if search_text:
            search_lower = search_text.lower()
            logs = [log for log in logs if search_lower in log.message.lower()]
        
        # Convert to dict format
        return [
            {
                "timestamp": log.timestamp.isoformat(),
                "process": log.process,
                "level": log.level,
                "message": log.message,
                "metadata": log.metadata
            }
            for log in logs[-count:]
        ]
    
    def get_all_logs(
        self,
        count: int = 100,
        processes: Optional[List[str]] = None,
        level_filter: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Get logs from all or specified processes"""
        all_logs = []
        
        for process_name, buffer in self.buffers.items():
            if processes and process_name not in processes:
                continue
            
            for log in buffer:
                if level_filter and log.level not in level_filter:
                    continue
                
                all_logs.append({
                    "timestamp": log.timestamp.isoformat(),
                    "process": log.process,
                    "level": log.level,
                    "message": log.message,
                    "metadata": log.metadata
                })
        
        # Sort by timestamp and return most recent
        all_logs.sort(key=lambda x: x["timestamp"], reverse=True)
        return all_logs[:count]
    
    def search_logs(
        self,
        query: str,
        processes: Optional[List[str]] = None,
        use_regex: bool = False
    ) -> List[Dict[str, Any]]:
        """Search logs across processes"""
        results = []
        
        if use_regex:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error:
                logger.error(f"Invalid regex pattern: {query}")
                return []
        
        for process_name, buffer in self.buffers.items():
            if processes and process_name not in processes:
                continue
            
            for log in buffer:
                match = False
                
                if use_regex:
                    if pattern.search(log.message):
                        match = True
                else:
                    if query.lower() in log.message.lower():
                        match = True
                
                if match:
                    results.append({
                        "timestamp": log.timestamp.isoformat(),
                        "process": log.process,
                        "level": log.level,
                        "message": log.message,
                        "metadata": log.metadata
                    })
        
        return results
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get console statistics"""
        stats = {}
        
        for process_name, buffer in self.buffers.items():
            level_counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
            
            for log in buffer:
                if log.level in level_counts:
                    level_counts[log.level] += 1
            
            stats[process_name] = {
                "total_lines": len(buffer),
                "level_counts": level_counts,
                "oldest_entry": buffer[0].timestamp.isoformat() if buffer else None,
                "newest_entry": buffer[-1].timestamp.isoformat() if buffer else None
            }
        
        return stats
    
    def clear_buffer(self, process_name: str):
        """Clear buffer for a specific process"""
        if process_name in self.buffers:
            self.buffers[process_name].clear()
    
    def subscribe(self, callback: Callable):
        """Subscribe to console events"""
        self.subscribers.append(callback)
    
    def unsubscribe(self, callback: Callable):
        """Unsubscribe from console events"""
        if callback in self.subscribers:
            self.subscribers.remove(callback)