# modules/ocr/__init__.py
"""
OCR module.

This module contains classes and utilities for OCR processing, data conditioning,
and screen capture functionality.

Note: Some imports are intentionally not included here to avoid circular imports.
Import specific modules directly when needed.
"""

# Core OCR functionality (safe imports only)
from .data_types import OCRParsedData, OCRSnapshot
# Interfaces should be imported directly from interfaces.ocr.services when needed
# to avoid circular imports

# Image processing utilities - removed, C++ accelerator handles all preprocessing
# from .image_utils import apply_preprocessing  # Deprecated - see old_image_utils.py

# Note: OCRService, PythonOCRDataConditioner, and OCRDataConditioningService
# should be imported directly to avoid circular import issues:
# from modules.ocr.ocr_service import OCRService
# from modules.ocr.python_ocr_data_conditioner import PythonOCRDataConditioner
# from modules.ocr.ocr_data_conditioning_service import OCRDataConditioningService

__all__ = [
    # Core OCR data types
    'OCRParsedData', 'OCRSnapshot'
    # Image utilities removed - C++ accelerator handles all preprocessing
]