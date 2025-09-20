"""
IntelliSense Exception Classes

Defines custom exception hierarchy for IntelliSense operations,
providing specific error types for different failure scenarios.
"""


class IntelliSenseError(Exception):
    """Base exception for IntelliSense operations."""
    pass


class IntelliSenseInitializationError(IntelliSenseError):
    """Raised when IntelliSense initialization fails."""
    pass


class IntelliSenseSessionError(IntelliSenseError):
    """Raised when session operations fail (e.g., session not found, invalid state)."""
    pass


class IntelliSenseDataError(IntelliSenseError):
    """Raised for errors related to data loading, validation, or processing."""
    pass
