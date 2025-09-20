# gui/services/__init__.py
"""GUI services - self-contained, no external dependencies"""

from .redis_service import RedisService

__all__ = ['RedisService']
