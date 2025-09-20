"""
IntelliSense API Module

FastAPI-based REST API for IntelliSense trading intelligence platform.
Provides endpoints for session management, test execution, and analysis results.
"""

__version__ = "1.0.0"
__author__ = "TESTRADE Development Team"

# Safe imports - only expose what's needed
from .schemas import (
    TestSessionCreateRequest,
    SessionCreationResponse,
    SessionStatusDetailResponse,
    ActionResponse,
    ErrorResponse
)

# Note: FastAPI app is available via direct import:
# from intellisense.api.main import app

__all__ = [
    # API Schemas
    'TestSessionCreateRequest',
    'SessionCreationResponse',
    'SessionStatusDetailResponse',
    'ActionResponse',
    'ErrorResponse'
]
