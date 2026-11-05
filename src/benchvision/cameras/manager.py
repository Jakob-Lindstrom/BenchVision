"""Unified camera discovery and active camera lifecycle manager."""

from __future__ import annotations

import logging
import threading
from typing import Any

from benchvision.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
)
from benchvision.core.config import AppSettings, LensConfig, save_settings

logger = logging.getLogger("benchvision.cameras")


class CameraManager:
    """Discovers connected cameras and manages the currently selected CameraBackend."""

    def __init__(self, settings: AppSettings, realtime_synthetic: bool = True) -> None:
        self.settings = settings
        self.realtime_synthetic = realtime_synthetic
        self._lock = threading.RLock()
        self._active_backend: CameraBackend | None = None
        self._discovered: list[dict[str, str]] = []

    def discover_all(self) -> list[dict[str, str]]:
        """Auto-detect connected cameras across all registered camera backends."""
        found: list[dict[str, str]] = []
        # Check hardware backends first (picamera2, v4l2), then synthetic
        ordered_keys = [
            k for k in ("picamera2", "v4l2", "synthetic") if k in CAMERA_REGISTRY.list_keys()
        ]
        for key in CAMERA_REGISTRY.list_keys():
            if key not in ordered_keys:
                ordered_keys.append(key)

        for key in ordered_keys:
            cls = CAMERA_REGISTRY.get(key)
            try:
                items = cls.discover()
                found.extend(items)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Camera discovery failed for backend '%s': %s", key, exc)

        self._discovered = found
        return found

    def _instantiate(self, camera_id: str) -> CameraBackend:
        if not self._discovered:
            self.discover_all()

        match = next((c for c in self._discovered if c["camera_id"] == camera_id), None)
        if match is not None:
            btype = match["backend_type"]
            cls = CAMERA_REGISTRY.get(btype)
            if btype == "synthetic":
                return cls(
                    camera_id=match["camera_id"],
                    device_path=match["device_path"],
                    name=match["name"],
                    realtime=self.realtime_synthetic,
                )  # type: ignore[call-arg]
            if btype == "picamera2":
                return cls(
                    camera_id=match["camera_id"],
                    device_path=match["device_path"],
                    name=match["name"],
                    sensor_model=match.get("sensor_model", "imx296"),
                )  # type: ignore[call-arg]
            return cls(
                camera_id=match["camera_id"],
                device_path=match["device_path"],
                name=match["name"],
            )

        if ":" in camera_id:
            prefix, dev = camera_id.split(":", 1)
            if prefix in CAMERA_REGISTRY.list_keys():
                cls = CAMERA_REGISTRY.get(prefix)
                if prefix == "synthetic":
                    return cls(
                        camera_id=camera_id,
                        device_path=dev,
                        name="Synthetic Oscillator",
                        realtime=self.realtime_synthetic,
                    )  # type: ignore[call-arg]
                return cls(camera_id=camera_id, device_path=dev, name=f"{prefix} ({dev})")

        # Fallback to synthetic camera
        syn_cls = CAMERA_REGISTRY.get("synthetic")
        return syn_cls(realtime=self.realtime_synthetic)  # type: ignore[call-arg]

    def get_active(self) -> CameraBackend:
        """Return the currently active CameraBackend instance, creating it if needed."""
        with self._lock:
            if self._active_backend is None:
                self._active_backend = self._instantiate(self.settings.active_camera_id)
            return self._active_backend

    def select_camera(self, camera_id: str) -> CameraCapabilities:
        """Switch active camera, persist in settings, and return its capabilities."""
        with self._lock:
            if self._active_backend is not None:
                self._active_backend.stop()
            self._active_backend = self._instantiate(camera_id)
            self.settings.active_camera_id = self._active_backend.camera_id
            save_settings(self.settings)
            return self.get_capabilities(self._active_backend)

    def get_capabilities(self, backend: CameraBackend | None = None) -> CameraCapabilities:
        """Return camera capabilities enriched with user-configured lens parameters."""
        cam = backend or self.get_active()
        caps = cam.list_capabilities()
        if cam.camera_id in self.settings.camera_lenses:
            lens = self.settings.camera_lenses[cam.camera_id]
            caps.lens_info = lens.model_dump()
        return caps

    def update_lens_config(self, camera_id: str, lens_data: dict[str, Any]) -> LensConfig:
        """Update per-camera optical/lens configuration (focal length, FOV, focus mode)."""
        existing = self.settings.camera_lenses.get(camera_id, LensConfig()).model_dump()
        existing.update({k: v for k, v in lens_data.items() if v is not None})
        lens = LensConfig(**existing)
        self.settings.camera_lenses[camera_id] = lens
        save_settings(self.settings)
        return lens

    def lock_active_controls(self, controls: dict[str, Any]) -> CameraControlSettings:
        """Lock exposure, gain, white balance, and focus on the active camera."""
        with self._lock:
            cam = self.get_active()
            return cam.lock_controls(controls)

    def shutdown(self) -> None:
        """Stop active camera cleanly."""
        with self._lock:
            if self._active_backend is not None:
                self._active_backend.stop()
                self._active_backend = None
