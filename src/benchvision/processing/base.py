"""Abstract interface, parameters schema, and plugin registry for magnification methods."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from benchvision.core.registry import PluginRegistry


class JobParams(BaseModel):
    """Parameters for a single offline video magnification and analysis job."""

    method: str = "eulerian"
    preset_id: str | None = "human_heart"
    low_hz: float = Field(default=0.8, ge=0.02, le=250.0)
    high_hz: float = Field(default=3.0, ge=0.05, le=250.0)
    alpha: float = Field(default=25.0, ge=1.0, le=200.0)
    pyramid_levels: int = Field(default=4, ge=1, le=7)
    spatial_blur_sigma: float = Field(default=1.5, ge=0.0, le=15.0)
    roi: tuple[int, int, int, int] | None = None
    max_width: int | None = Field(default=640, ge=64, le=3840)
    filter_type: str = Field(default="butterworth", pattern="^(ideal|butterworth|iir)$")
    output_mode: str = Field(
        default="side_by_side", pattern="^(side_by_side|magnified|heatmap)$"
    )
    chrom_attenuation: float = Field(default=0.2, ge=0.0, le=1.0)


@dataclass
class MagnificationOutput:
    """Standardized output produced by any MagnificationMethod plugin."""

    magnified_bgr: np.ndarray  # (T, H, W, 3) uint8
    motion_energy: np.ndarray  # (T, H, W) float32 in [0, 1]
    raw_roi_signal: np.ndarray  # (T,) float64
    filtered_roi_signal: np.ndarray  # (T,) float64
    timestamps_s: np.ndarray  # (T,) float64
    achieved_fps: float
    dominant_hz: float
    dominant_bpm: float
    extra_metrics: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[float], bool]  # returns False if job is cancelled


class JobCancelledError(RuntimeError):
    """Raised when a user cancels an active background processing job."""


class MagnificationMethod(ABC):
    """Abstract base class for swappable video magnification & motion analysis methods."""

    method_key: str = "base"
    display_name: str = "Base Magnification"
    description: str = ""
    speed_rating: str = "medium"

    @abstractmethod
    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        """Process a raw clip and return magnified frames, motion energy, and ROI signals."""


def estimate_processing_time_s(
    frame_count: int, width: int, height: int, method: str = "eulerian"
) -> float:
    """Realistic estimate of offline processing time (in seconds) on a Raspberry Pi 4/5."""
    megapixels_total = (frame_count * width * height) / 1_000_000.0
    cost_per_mp = {
        "eulerian": 0.45,
        "riesz": 0.65,
        "phase_steerable": 1.40,
        "optical_flow": 0.85,
        "rppg": 0.25,
    }
    factor = cost_per_mp.get(method, 0.65)
    return round(max(1.0, megapixels_total * factor), 1)


PROCESSING_REGISTRY: PluginRegistry[MagnificationMethod] = PluginRegistry("processing")
