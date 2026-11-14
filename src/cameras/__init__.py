"""Camera plugins and unified camera discovery."""

from src.cameras import picamera2_backend, synthetic, v4l2_usb  # noqa: F401
from src.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
    ControlRange,
    FramePacket,
)
from src.cameras.manager import CameraManager

__all__ = [
    "CAMERA_REGISTRY",
    "CameraBackend",
    "CameraCapabilities",
    "CameraControlSettings",
    "CameraManager",
    "ControlRange",
    "FramePacket",
]
