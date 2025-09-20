"""
Command Result Classes for GUI Command Processing

This module provides standardized result classes for GUI command execution.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
from enum import Enum


class CommandStatus(Enum):
    """Status enumeration for command results."""
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class CommandResult:
    """
    Standardized result for GUI command execution.
    
    Attributes:
        status: The command execution status
        message: Human-readable message describing the result
        data: Optional additional data to return
        command_id: Optional command ID for correlation
    """
    status: CommandStatus
    message: str
    data: Optional[Dict[str, Any]] = None
    command_id: Optional[str] = None
    
    @classmethod
    def success(cls, message: str, data: Optional[Dict[str, Any]] = None, command_id: Optional[str] = None) -> 'CommandResult':
        """Create a success result."""
        return cls(CommandStatus.SUCCESS, message, data, command_id)
    
    @classmethod
    def error(cls, message: str, data: Optional[Dict[str, Any]] = None, command_id: Optional[str] = None) -> 'CommandResult':
        """Create an error result."""
        return cls(CommandStatus.ERROR, message, data, command_id)
    
    @classmethod
    def warning(cls, message: str, data: Optional[Dict[str, Any]] = None, command_id: Optional[str] = None) -> 'CommandResult':
        """Create a warning result."""
        return cls(CommandStatus.WARNING, message, data, command_id)
    
    @classmethod
    def info(cls, message: str, data: Optional[Dict[str, Any]] = None, command_id: Optional[str] = None) -> 'CommandResult':
        """Create an info result."""
        return cls(CommandStatus.INFO, message, data, command_id)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary for serialization."""
        result = {
            "status": self.status.value,
            "message": self.message
        }
        if self.data:
            result["data"] = self.data
        if self.command_id:
            result["command_id"] = self.command_id
        return result
