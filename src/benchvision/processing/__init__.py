"""Offline video magnification and motion analysis plugins."""

from benchvision.processing import eulerian  # noqa: F401
from benchvision.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationMethod,
    MagnificationOutput,
    estimate_processing_time_s,
)

__all__ = [
    "PROCESSING_REGISTRY",
    "JobCancelledError",
    "JobParams",
    "MagnificationMethod",
    "MagnificationOutput",
    "estimate_processing_time_s",
]
