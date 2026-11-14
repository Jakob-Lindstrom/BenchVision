"""Synthetic camera backend generating sub-pixel oscillations and color pulses at known Hz."""

from __future__ import annotations

import math
import time
from typing import Any

import cv2
import numpy as np

from src.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
    ControlRange,
    FramePacket,
)


@CAMERA_REGISTRY.register("synthetic")
class SyntheticCameraBackend(CameraBackend):
    """Generates synthetic frames with a known periodic oscillation frequency and amplitude."""

    backend_key = "synthetic"

    def __init__(
        self,
        camera_id: str = "synthetic:0",
        device_path: str = "sim://oscillator",
        name: str = "Synthetic Reference Oscillator (Global Shutter)",
        oscillation_hz: float = 2.0,
        secondary_hz: float = 0.3,
        amplitude_px: float = 0.75,
        color_pulse_amp: float = 4.5,
        noise_std: float = 0.25,
        phase_offset_rad: float = 0.0,
        realtime: bool = False,
    ) -> None:
        super().__init__(camera_id=camera_id, device_path=device_path, name=name)
        self.oscillation_hz = oscillation_hz
        self.secondary_hz = secondary_hz
        self.amplitude_px = amplitude_px
        self.color_pulse_amp = color_pulse_amp
        self.noise_std = noise_std
        self.phase_offset_rad = (
            math.pi / 2.0 if camera_id.endswith(":1") and phase_offset_rad == 0.0 else phase_offset_rad
        )
        self.realtime = realtime
        self._frame_idx = 0
        self._start_mono_ns = 0
        self._start_wall_ns = 0
        self._xx: np.ndarray | None = None
        self._yy: np.ndarray | None = None
        self._rng = np.random.default_rng(42)

    @classmethod
    def discover(cls) -> list[dict[str, str]]:
        return [
            {
                "camera_id": "synthetic:0",
                "backend_type": cls.backend_key,
                "name": "Synthetic Reference Oscillator (2.0 Hz / 120 BPM)",
                "device_path": "sim://oscillator-a",
                "shutter_type": "global",
            },
            {
                "camera_id": "synthetic:1",
                "backend_type": cls.backend_key,
                "name": "Synthetic Orthogonal Reference B (2.0 Hz, 90° Phase / Z-Axis)",
                "device_path": "sim://oscillator-b",
                "shutter_type": "global",
            },
        ]

    def _build_grid(self, width: int, height: int) -> None:
        x = np.arange(width, dtype=np.float32)
        y = np.arange(height, dtype=np.float32)
        self._xx, self._yy = np.meshgrid(x, y)

    def start(self, width: int = 640, height: int = 480, fps: int = 30) -> None:
        self.current_controls.width = width
        self.current_controls.height = height
        self.current_controls.fps = fps
        self._stream_width = int(width)
        self._stream_height = int(height)
        self._stream_fps = int(fps)
        self._frame_idx = 0
        self._start_mono_ns = time.monotonic_ns()
        self._start_wall_ns = time.time_ns()
        self._build_grid(width, height)
        self.running = True

    def stop(self) -> None:
        self.running = False
        self._stream_width = 0
        self._stream_height = 0
        self._stream_fps = 0

    def get_frame(self, timeout: float = 2.0) -> FramePacket:
        if not self.running:
            self.start(
                self.current_controls.width,
                self.current_controls.height,
                self.current_controls.fps,
            )

        fps = max(self.current_controls.fps, 1)
        w = self.current_controls.width
        h = self.current_controls.height
        if self._xx is None or self._xx.shape != (h, w):
            self._build_grid(w, h)

        assert self._xx is not None and self._yy is not None

        if self.realtime and self._frame_idx > 0:
            target_ns = self._start_mono_ns + int(self._frame_idx * (1e9 / fps))
            now_ns = time.monotonic_ns()
            if target_ns > now_ns:
                time.sleep((target_ns - now_ns) / 1e9)
            mono_ns = time.monotonic_ns()
            wall_ns = time.time_ns()
            t_sec = (mono_ns - self._start_mono_ns) / 1e9
        else:
            dt_ns = round(1_000_000_000 / fps)
            mono_ns = self._start_mono_ns + self._frame_idx * dt_ns
            wall_ns = self._start_wall_ns + self._frame_idx * dt_ns
            t_sec = self._frame_idx / float(fps)

        # Sub-pixel horizontal and vertical displacement at primary & secondary frequencies
        phi = self.phase_offset_rad
        dx = self.amplitude_px * math.sin(2.0 * math.pi * self.oscillation_hz * t_sec + phi)
        dy = 0.35 * self.amplitude_px * math.sin(2.0 * math.pi * self.secondary_hz * t_sec + 0.5 * phi)

        cx = w * 0.5 + dx
        cy = h * 0.5 + dy
        sigma = min(w, h) * 0.18

        r2 = (self._xx - cx) ** 2 + (self._yy - cy) ** 2
        envelope = np.exp(-r2 / (2.0 * sigma * sigma))
        carrier = np.cos(2.0 * math.pi * (self._xx - cx) / 24.0)

        # Base grayscale intensity with smooth gradient + Gabor target
        base = 95.0 + 90.0 * envelope * (0.5 + 0.5 * carrier)

        # Add localized periodic intensity/color pulsation in central region (simulates pulse/rPPG)
        pulse = self.color_pulse_amp * math.sin(2.0 * math.pi * self.oscillation_hz * t_sec + phi)
        green_channel = base + pulse * envelope
        red_channel = base + 0.4 * pulse * envelope
        blue_channel = base - 0.2 * pulse * envelope

        if self.noise_std > 0:
            noise = self._rng.normal(0.0, self.noise_std, size=base.shape).astype(np.float32)
            green_channel = green_channel + noise

        bgr = np.stack(
            [
                np.clip(blue_channel, 0, 255).astype(np.uint8),
                np.clip(green_channel, 0, 255).astype(np.uint8),
                np.clip(red_channel, 0, 255).astype(np.uint8),
            ],
            axis=-1,
        )

        ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        jpeg_bytes = bytes(enc) if ok else None

        pkt = FramePacket(
            frame_index=self._frame_idx,
            timestamp_monotonic_ns=mono_ns,
            timestamp_wall_ns=wall_ns,
            bgr=bgr,
            jpeg_bytes=jpeg_bytes,
        )
        self._frame_idx += 1
        return pkt

    def list_capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            camera_id=self.camera_id,
            backend_type=self.backend_key,
            name=self.name,
            device_path=self.device_path,
            sensor_model="Synthetic Global-Shutter Phantom",
            shutter_type="global",
            rolling_shutter_warning=None,
            resolutions=[(320, 240), (640, 480), (1280, 720)],
            fps_options=[15, 30, 60, 120],
            controls={
                "exposure_us": ControlRange(
                    name="Exposure Time (us)",
                    min_val=100,
                    max_val=33000,
                    step=100,
                    default_val=10000,
                    current_val=float(self.current_controls.exposure_us),
                    unit="us",
                ),
                "gain": ControlRange(
                    name="Analog Gain",
                    min_val=1.0,
                    max_val=16.0,
                    step=0.1,
                    default_val=1.0,
                    current_val=float(self.current_controls.gain),
                    unit="x",
                ),
                "wb_temp_k": ControlRange(
                    name="White Balance Temp",
                    min_val=2800,
                    max_val=6500,
                    step=100,
                    default_val=4600,
                    current_val=float(self.current_controls.wb_temp_k),
                    unit="K",
                ),
                "focus": ControlRange(
                    name="Focus",
                    supported=True,
                    writable=True,
                    min_val=0.0,
                    max_val=10.0,
                    step=0.1,
                    default_val=1.0,
                    current_val=float(self.current_controls.focus_position),
                    unit="dioptres",
                ),
            },
            lens_info={
                "focal_length_mm": 6.0,
                "fov_degrees": 55.0,
                "focus_mode": "manual",
            },
        )

    def set_controls(self, controls: dict[str, Any]) -> CameraControlSettings:
        updated = self.current_controls.model_dump()
        for k, v in controls.items():
            if k in updated and v is not None:
                updated[k] = v
        if "oscillation_hz" in controls:
            self.oscillation_hz = float(controls["oscillation_hz"])
        if "amplitude_px" in controls:
            self.amplitude_px = float(controls["amplitude_px"])
        self.current_controls = CameraControlSettings(**updated)
        return self.current_controls
