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
        default="side_by_side",
        pattern="^(side_by_side|magnified|heatmap|phase_ripple|quiver_vectors|dual_sync)$",
    )
    target_stream: str = Field(
        default="both",
        pattern="^(both|cam_a|cam_b)$",
    )
    chrom_attenuation: float = Field(default=0.2, ge=0.0, le=1.0)
    heatmap_threshold: float = Field(default=0.20, ge=0.0, le=0.95)
    gate_background_noise: bool = False
    stabilize_subpixel: bool = False
    roi_b: tuple[int, int, int, int] | None = None
    trim_start_s: float = Field(default=0.0, ge=0.0, le=600.0)
    trim_end_s: float = Field(default=0.0, ge=0.0, le=600.0)


@dataclass
class MagnificationOutput:
    """Standardized output produced by any MagnificationMethod plugin."""

    magnified_bgr: np.ndarray  # (T, H, W, 3) uint8
    motion_energy: np.ndarray  # (T, H/2, W/2) float32 in [0, 1] (see alloc_motion_energy)
    raw_roi_signal: np.ndarray  # (T,) float64
    filtered_roi_signal: np.ndarray  # (T,) float64
    timestamps_s: np.ndarray  # (T,) float64
    achieved_fps: float
    dominant_hz: float
    dominant_bpm: float
    extra_metrics: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[float], bool]  # returns False if job is cancelled


MOTION_ENERGY_SCALE = 2  # motion energy is stored at 1/2 resolution (1/4 RAM)


def alloc_motion_energy(n_frames: int, h: int, w: int) -> np.ndarray:
    """Allocate a half-resolution (T, H/2, W/2) float32 motion-energy buffer.

    Storing motion energy at full resolution costs 4 bytes/pixel/frame (e.g. 340 MB for a
    12 s 640x385 clip), which was enough to trigger the Linux OOM killer on a 2 GB Pi 4.
    """
    mh = max(1, (h + MOTION_ENERGY_SCALE - 1) // MOTION_ENERGY_SCALE)
    mw = max(1, (w + MOTION_ENERGY_SCALE - 1) // MOTION_ENERGY_SCALE)
    return np.zeros((n_frames, mh, mw), dtype=np.float32)


def shrink_energy(energy_full: np.ndarray, buffer: np.ndarray) -> np.ndarray:
    """Downsample a full-resolution (H, W) energy map to match a motion-energy buffer."""
    import cv2

    mh, mw = buffer.shape[1:3]
    src = np.asarray(energy_full, dtype=np.float32)
    if src.shape[:2] == (mh, mw):
        return src
    return cv2.resize(src, (mw, mh), interpolation=cv2.INTER_AREA)


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
    """Realistic estimate of offline processing time (in seconds) adapted to Pi 4 vs Pi 5."""
    from benchvision.core.hardware import detect_hardware_profile

    hw = detect_hardware_profile()
    megapixels_total = (frame_count * width * height) / 1_000_000.0
    cost_per_mp = {
        "eulerian": 0.45,
        "riesz": 0.55,
        "phase_steerable": 1.35,
        "motion_extraction": 0.20,
        "posy_rgb_trail": 0.22,
        "posy_peak_hold": 0.24,
        "optical_flow": 0.85,
        "rppg": 0.25,
    }
    factor = cost_per_mp.get(method, 0.55) / max(float(hw["speed_multiplier"]), 0.5)
    return round(max(1.0, megapixels_total * factor), 1)


PROCESSING_REGISTRY: PluginRegistry[MagnificationMethod] = PluginRegistry("processing")

