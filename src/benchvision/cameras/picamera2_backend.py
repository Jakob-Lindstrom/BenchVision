"""Picamera2 / libcamera backend for Raspberry Pi CSI cameras (Module 2, 3, HQ, Global Shutter)."""

from __future__ import annotations

import logging
import time
from typing import Any

import cv2

from benchvision.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
    ControlRange,
    FramePacket,
)
from benchvision.cameras.v4l2_usb import ROLLING_SHUTTER_ADVISORY

try:
    from picamera2 import Picamera2  # type: ignore[import-not-found]
except ImportError:
    Picamera2 = None  # type: ignore[assignment]


CSI_SENSOR_PROFILES: dict[str, dict[str, Any]] = {
    "imx219": {
        "display_name": "Raspberry Pi Camera Module 2 (IMX219)",
        "shutter_type": "rolling",
        "resolutions": [(640, 480), (1280, 720), (1920, 1080)],
        "fps_options": [15, 30, 60, 90],
        "focus_mode": "fixed",
        "focal_length_mm": 3.04,
        "fov_degrees": 62.2,
    },
    "imx708": {
        "display_name": "Raspberry Pi Camera Module 3 (IMX708)",
        "shutter_type": "rolling",
        "resolutions": [(640, 480), (1280, 720), (1536, 864), (2304, 1296)],
        "fps_options": [15, 30, 50, 60, 120],
        "focus_mode": "autofocus",
        "focal_length_mm": 4.74,
        "fov_degrees": 66.0,
    },
    "imx477": {
        "display_name": "Raspberry Pi HQ Camera (IMX477)",
        "shutter_type": "rolling",
        "resolutions": [(640, 480), (1332, 990), (2028, 1080)],
        "fps_options": [15, 30, 50, 60, 120],
        "focus_mode": "manual",
        "focal_length_mm": 16.0,
        "fov_degrees": 44.6,
    },
    "imx296": {
        "display_name": "Raspberry Pi Global Shutter Camera (IMX296)",
        "shutter_type": "global",
        "resolutions": [(640, 480), (1456, 1088)],
        "fps_options": [15, 30, 60],
        "focus_mode": "manual",
        "focal_length_mm": 6.0,
        "fov_degrees": 63.0,
    },
}


def resolve_sensor_profile(model_str: str) -> dict[str, Any]:
    """Return optical and shutter profile for a libcamera sensor model string."""
    lower = model_str.lower()
    for key, profile in CSI_SENSOR_PROFILES.items():
        if key in lower:
            return dict(profile, sensor_key=key)
    return {
        "sensor_key": lower or "csi_generic",
        "display_name": f"Raspberry Pi CSI Camera ({model_str or 'Generic'})",
        "shutter_type": "global" if "global" in lower or "296" in lower else "rolling",
        "resolutions": [(640, 480), (1280, 720), (1920, 1080)],
        "fps_options": [15, 30, 60],
        "focus_mode": "manual",
        "focal_length_mm": 4.0,
        "fov_degrees": 62.0,
    }


@CAMERA_REGISTRY.register("picamera2")
class Picamera2Backend(CameraBackend):
    """Picamera2 / libcamera backend supporting Module 2, Module 3, HQ, and Global Shutter."""

    backend_key = "picamera2"

    def __init__(
        self,
        camera_id: str = "picamera2:0",
        device_path: str = "0",
        name: str = "Raspberry Pi CSI Camera",
        sensor_model: str = "imx296",
    ) -> None:
        super().__init__(camera_id=camera_id, device_path=device_path, name=name)
        self.sensor_model = sensor_model
        self._picam: Any = None
        self._frame_idx = 0

    @classmethod
    def discover(cls) -> list[dict[str, str]]:
        """Auto-detect connected Raspberry Pi CSI cameras via Picamera2."""
        if Picamera2 is None:
            return []
        try:
            infos = Picamera2.global_camera_info()
        except Exception:  # noqa: BLE001
            return []

        detected: list[dict[str, str]] = []
        for idx, info in enumerate(infos):
            model = str(info.get("Model", f"csi_{idx}"))
            cam_num = str(info.get("Num", idx))
            profile = resolve_sensor_profile(model)
            detected.append(
                {
                    "camera_id": f"picamera2:{cam_num}",
                    "backend_type": cls.backend_key,
                    "name": profile["display_name"],
                    "device_path": cam_num,
                    "sensor_model": model,
                    "shutter_type": profile["shutter_type"],
                }
            )
        return detected

    def list_capabilities(self) -> CameraCapabilities:
        profile = resolve_sensor_profile(self.sensor_model)
        shutter = profile["shutter_type"]
        focus_mode = profile["focus_mode"]
        has_af = focus_mode == "autofocus"

        return CameraCapabilities(
            camera_id=self.camera_id,
            backend_type=self.backend_key,
            name=profile["display_name"],
            device_path=self.device_path,
            sensor_model=self.sensor_model,
            shutter_type=shutter,
            rolling_shutter_warning=(
                None if shutter == "global" else ROLLING_SHUTTER_ADVISORY
            ),
            resolutions=profile["resolutions"],
            fps_options=profile["fps_options"],
            controls={
                "exposure_us": ControlRange(
                    name="ExposureTime (us)",
                    min_val=100,
                    max_val=100000,
                    step=100,
                    default_val=10000,
                    current_val=float(self.current_controls.exposure_us),
                    unit="us",
                ),
                "gain": ControlRange(
                    name="AnalogueGain",
                    min_val=1.0,
                    max_val=16.0,
                    step=0.1,
                    default_val=1.0,
                    current_val=float(self.current_controls.gain),
                    unit="x",
                ),
                "wb_temp_k": ControlRange(
                    name="White Balance Temp",
                    min_val=2500,
                    max_val=8000,
                    step=100,
                    default_val=4600,
                    current_val=float(self.current_controls.wb_temp_k),
                    unit="K",
                ),
                "focus": ControlRange(
                    name="LensPosition",
                    supported=has_af,
                    writable=has_af,
                    min_val=0.0,
                    max_val=15.0,
                    step=0.1,
                    default_val=1.0,
                    current_val=float(self.current_controls.focus_position),
                    unit="dioptres" if has_af else focus_mode,
                ),
            },
            lens_info={
                "focal_length_mm": profile["focal_length_mm"],
                "fov_degrees": profile["fov_degrees"],
                "focus_mode": focus_mode,
            },
        )

    def set_controls(self, controls: dict[str, Any]) -> CameraControlSettings:
        updated = self.current_controls.model_dump()
        for k, v in controls.items():
            if k in updated and v is not None:
                updated[k] = v
        self.current_controls = CameraControlSettings(**updated)

        if self._picam is not None:
            libcam_ctrls: dict[str, Any] = {
                "AeEnable": bool(self.current_controls.auto_exposure),
                "AwbEnable": bool(self.current_controls.auto_wb),
            }
            if not self.current_controls.auto_exposure:
                libcam_ctrls["ExposureTime"] = int(self.current_controls.exposure_us)
                libcam_ctrls["AnalogueGain"] = float(self.current_controls.gain)
            if "imx708" in self.sensor_model.lower():
                libcam_ctrls["AfMode"] = 2 if self.current_controls.autofocus else 0
                if not self.current_controls.autofocus:
                    libcam_ctrls["LensPosition"] = float(self.current_controls.focus_position)
            try:
                self._picam.set_controls(libcam_ctrls)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("benchvision.cameras").debug("Picamera2 set_controls error: %s", exc)

        return self.current_controls

    def start(self, width: int = 640, height: int = 480, fps: int = 30) -> None:
        if Picamera2 is None:
            raise RuntimeError(
                "Picamera2 is not installed. Run scripts/install.sh on Raspberry Pi OS "
                "to install python3-picamera2 with --system-site-packages."
            )
        self.stop()
        self.current_controls.width = width
        self.current_controls.height = height
        self.current_controls.fps = fps
        self._frame_idx = 0

        cam_idx = int(self.device_path) if self.device_path.isdigit() else 0
        self._picam = Picamera2(cam_idx)
        frame_duration_us = round(1_000_000 / max(fps, 1))
        cfg = self._picam.create_video_configuration(
            main={"size": (width, height), "format": "BGR888"},
            controls={"FrameDurationLimits": (frame_duration_us, frame_duration_us)},
        )
        self._picam.configure(cfg)
        self._picam.start()
        self.running = True

    def stop(self) -> None:
        self.running = False
        if self._picam is not None:
            try:
                self._picam.stop()
                self._picam.close()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("benchvision.cameras").debug("Picamera2 stop error: %s", exc)
            self._picam = None

    def get_frame(self, timeout: float = 2.0) -> FramePacket:
        if not self.running or self._picam is None:
            self.start(
                self.current_controls.width,
                self.current_controls.height,
                self.current_controls.fps,
            )
        mono_ns = time.monotonic_ns()
        wall_ns = time.time_ns()
        arr = self._picam.capture_array("main")
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if arr.shape[-1] == 3 else arr
        ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        pkt = FramePacket(
            frame_index=self._frame_idx,
            timestamp_monotonic_ns=mono_ns,
            timestamp_wall_ns=wall_ns,
            bgr=bgr,
            jpeg_bytes=bytes(enc) if ok else None,
        )
        self._frame_idx += 1
        return pkt
