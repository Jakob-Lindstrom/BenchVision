"""Offline video magnification and motion analysis plugins."""

from typing import Any

from src.processing import (  # noqa: F401
    eulerian,
    motion_extraction,
    optical_flow,
    phase_steerable,
    riesz,
    rppg,
)
from src.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationMethod,
    MagnificationOutput,
    estimate_processing_time_s,
)


def list_processing_methods() -> list[dict[str, Any]]:
    """Return metadata for all registered magnification/analysis plugins."""
    ordered = [
        "eulerian",
        "riesz",
        "phase_steerable",
        "motion_extraction",
        "optical_flow",
        "rppg",
    ]
    keys = [k for k in ordered if k in PROCESSING_REGISTRY.list_keys()]
    for k in PROCESSING_REGISTRY.list_keys():
        if k not in keys:
            keys.append(k)

    items: list[dict[str, Any]] = []
    for k in keys:
        cls = PROCESSING_REGISTRY.get(k)
        items.append(
            {
                "key": cls.method_key,
                "name": cls.display_name,
                "description": cls.description,
                "speed_rating": cls.speed_rating,
            }
        )
    return items


__all__ = [
    "PROCESSING_REGISTRY",
    "JobCancelledError",
    "JobParams",
    "MagnificationMethod",
    "MagnificationOutput",
    "estimate_processing_time_s",
    "list_processing_methods",
]
