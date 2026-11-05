"""Abstract CameraBackend interface, data models, and camera plugin registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from benchvision.core.registry import PluginRegistry


class ControlRange(BaseModel):
    """Supported range and current state for a single hardware camera control."""

    name: str
    supported: bool = True
    writable: bool = True
    min_val: float = 0.0
    max_val: float = 100.0
    step: float = 1.0
    default_val: float = 0.0
    current_val: float = 0.0
    unit: str = ""


class CameraCapabilities(BaseModel):
    """Capabilities, shutter characteristics, and optical properties of a camera."""

    camera_id: str
    backend_type: str
    name: str
    device_path: str
    sensor_model: str = "Unknown"
    shutter_type: str = Field(default="rolling", pattern="^(rolling|global)$")
    rolling_shutter_warning: str | None = None
    resolutions: list[tuple[int, int]] = Field(
        default_factory=lambda: [(640, 480), (1280, 720)]
    )
    fps_options: list[int] = Field(default_factory=lambda: [15, 30, 60])
    controls: dict[str, ControlRange] = Field(default_factory=dict)
    lens_info: dict[str, Any] = Field(
        default_factory=lambda: {
            "focal_length_mm": 4.0,
            "fov_degrees": 60.0,
            "focus_mode": "fixed",
        }
    )


class CameraControlSettings(BaseModel):
    """User-selected camera control settings for locking before recording."""

    width: int = 640
    height: int = 480
    fps: int = 30
    auto_exposure: bool = False
    exposure_us: int = 10000
    gain: float = 1.0
    auto_wb: bool = False
    wb_temp_k: int = 4600
    autofocus: bool = False
    focus_position: float = 0.0
    locked: bool = False


@dataclass
class FramePacket:
    """Single captured video frame with monotonic and wall-clock nanosecond timestamps."""

    frame_index: int
    timestamp_monotonic_ns: int
    timestamp_wall_ns: int
    bgr: np.ndarray
    jpeg_bytes: bytes | None = None


class CameraBackend(ABC):
    """Abstract base class for all camera plugins (Synthetic, USB V4L2, Picamera2)."""

    backend_key: str = "base"

    def __init__(self, camera_id: str, device_path: str, name: str) -> None:
        self.camera_id = camera_id
        self.device_path = device_path
        self.name = name
        self.running: bool = False
        self.current_controls = CameraControlSettings()

    @classmethod
    @abstractmethod
    def discover(cls) -> list[dict[str, str]]:
        """Auto-detect connected cameras handled by this backend."""

    @abstractmethod
    def start(
        self, width: int = 640, height: int = 480, fps: int = 30
    ) -> None:
        """Start capturing frames at the requested resolution and frame rate."""

    @abstractmethod
    def stop(self) -> None:
        """Stop capturing frames and release hardware resources."""

    @abstractmethod
    def get_frame(self, timeout: float = 2.0) -> FramePacket:
        """Return the latest captured FramePacket."""

    @abstractmethod
    def list_capabilities(self) -> CameraCapabilities:
        """Return supported resolutions, frame rates, shutter info, and controls."""

    @abstractmethod
    def set_controls(self, controls: dict[str, Any]) -> CameraControlSettings:
        """Apply manual or automatic camera controls (exposure, gain, WB, focus)."""

    def lock_controls(self, settings: dict[str, Any] | None = None) -> CameraControlSettings:
        """Explicitly disable auto-exposure, auto-WB, and autofocus and lock values for recording."""
        payload: dict[str, Any] = dict(settings or {})
        payload["auto_exposure"] = False
        payload["auto_wb"] = False
        payload["autofocus"] = False
        payload["locked"] = True
        # Clamp exposure duration below frame period (1 / fps) to prevent silent FPS drop
        fps = int(payload.get("fps", self.current_controls.fps) or 30)
        max_exposure_us = int((1_000_000 / max(fps, 1)) * 0.9)
        exp_us = int(payload.get("exposure_us", self.current_controls.exposure_us))
        payload["exposure_us"] = min(exp_us, max_exposure_us)
        return self.set_controls(payload)


CAMERA_REGISTRY: PluginRegistry[CameraBackend] = PluginRegistry("camera")
