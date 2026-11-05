"""Camera plugins and unified camera discovery."""

from benchvision.cameras import synthetic  # noqa: F401
from benchvision.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
    ControlRange,
    FramePacket,
)

__all__ = [
    "CAMERA_REGISTRY",
    "CameraBackend",
    "CameraCapabilities",
    "CameraControlSettings",
    "ControlRange",
    "FramePacket",
]
