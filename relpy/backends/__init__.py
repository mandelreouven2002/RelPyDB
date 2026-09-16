"""
RelPy storage backends.

A backend persists RelPy's tables somewhere durable while the query engine
keeps running in memory, unchanged. See :mod:`relpy.backends.base` for the
design and :func:`relpy.backends.factory.create_backend` for construction.
"""

from .base import MemoryBackend, StorageBackend
from .factory import create_backend, normalize_backend_name

__all__ = [
    "StorageBackend",
    "MemoryBackend",
    "create_backend",
    "normalize_backend_name",
]
