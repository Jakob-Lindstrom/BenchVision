"""USB V4L2 webcam backend supporting v4l2-ctl hardware controls and MJPG/OpenCV capture."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from benchvision.cameras.base import (
    CAMERA_REGISTRY,
    CameraBackend,
    CameraCapabilities,
    CameraControlSettings,
    ControlRange,
    FramePacket,
)

PLATFORM_DRIVER_PREFIXES = (
    "bcm2835-codec",
    "bcm2835-isp",
    "rpi-hevc-dec",
    "unicam",
    "pispbe",
)

ROLLING_SHUTTER_ADVISORY = (
    "Rolling-shutter sensor detected. Sequential row readout can introduce geometric skew "
    "under high-frequency mechanical vibration. Consider the Raspberry Pi Global Shutter "
    "Camera (IMX296) for high-frequency vibration analysis."
)


def parse_v4l2_devices_output(output: str) -> list[dict[str, str]]:
    """Parse `v4l2-ctl --list-devices` output and return USB/external capture devices."""
    devices: list[dict[str, str]] = []
    lines = output.splitlines()
    current_header = ""
    found_for_block = False

    for raw_line in lines:
        if not raw_line.strip():
            current_header = ""
            found_for_block = False
            continue
        if not raw_line.startswith("\t") and not raw_line.startswith(" "):
            current_header = raw_line.rstrip(":").strip()
            found_for_block = False
            continue

        dev_path = raw_line.strip()
        if not dev_path.startswith("/dev/video") or found_for_block:
            continue

        lower_hdr = current_header.lower()
        if any(p in lower_hdr for p in PLATFORM_DRIVER_PREFIXES):
            continue

        clean_name = re.sub(r"\s*\([^)]*\)\s*$", "", current_header).strip() or dev_path
        devices.append(
            {
                "camera_id": f"v4l2:{dev_path}",
                "backend_type": "v4l2",
                "name": clean_name,
                "device_path": dev_path,
                "shutter_type": "rolling",
            }
        )
        found_for_block = True

    return devices


def parse_v4l2_ctrls_output(output: str) -> dict[str, ControlRange]:
    """Parse `v4l2-ctl --list-ctrls` output into ControlRange models."""
    controls: dict[str, ControlRange] = {}
    pattern = re.compile(
        r"^\s*([a-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)$"
    )
    kv_pattern = re.compile(r"([a-z]+)=(-?\d+)")

    for line in output.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        ctrl_name, _ctrl_type, rest = m.groups()
        kv = {k: float(v) for k, v in kv_pattern.findall(rest)}
        inactive = "flags=inactive" in rest
        controls[ctrl_name] = ControlRange(
            name=ctrl_name,
            supported=True,
            writable=not inactive,
            min_val=kv.get("min", 0.0),
            max_val=kv.get("max", 100.0),
            step=kv.get("step", 1.0),
            default_val=kv.get("default", 0.0),
            current_val=kv.get("value", kv.get("default", 0.0)),
        )
    return controls


@CAMERA_REGISTRY.register("v4l2")
class V4L2UsbCameraBackend(CameraBackend):
    """USB webcam backend using V4L2 controls and MJPG capture (OpenCV + v4l2-ctl fallback)."""

    backend_key = "v4l2"

    def __init__(
        self,
        camera_id: str = "v4l2:/dev/video0",
        device_path: str = "/dev/video0",
        name: str = "USB V4L2 Webcam",
    ) -> None:
        super().__init__(camera_id=camera_id, device_path=device_path, name=name)
        self._cap: cv2.VideoCapture | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._latest_packet: FramePacket | None = None
        self._frame_idx = 0

    @classmethod
    def discover(cls) -> list[dict[str, str]]:
        """Auto-detect connected USB V4L2 cameras via v4l2-ctl."""
        try:
            out = subprocess.check_output(
                ["v4l2-ctl", "--list-devices"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
            return parse_v4l2_devices_output(out)
        except (OSError, subprocess.SubprocessError):
            if Path("/dev/video0").exists():
                return [
                    {
                        "camera_id": "v4l2:/dev/video0",
                        "backend_type": cls.backend_key,
                        "name": "USB Video Device (/dev/video0)",
                        "device_path": "/dev/video0",
                        "shutter_type": "rolling",
                    }
                ]
            return []

    def _run_v4l2_ctl(self, args: list[str]) -> str:
        try:
            return subprocess.check_output(
                ["v4l2-ctl", f"--device={self.device_path}", *args],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2.5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""

    def list_capabilities(self) -> CameraCapabilities:
        raw_ctrls = self._run_v4l2_ctl(["--list-ctrls"])
        parsed_ctrls = parse_v4l2_ctrls_output(raw_ctrls)

        mapped: dict[str, ControlRange] = {}
        if "exposure_time_absolute" in parsed_ctrls:
            c = parsed_ctrls["exposure_time_absolute"]
            # V4L2 exposure_time_absolute is in 100us units
            mapped["exposure_us"] = ControlRange(
                name="Exposure Time (us)",
                supported=True,
                writable=True,
                min_val=c.min_val * 100.0,
                max_val=c.max_val * 100.0,
                step=max(c.step * 100.0, 100.0),
                default_val=c.default_val * 100.0,
                current_val=c.current_val * 100.0,
                unit="us",
            )
        else:
            mapped["exposure_us"] = ControlRange(
                name="Exposure Time (us)",
                min_val=500,
                max_val=33000,
                step=100,
                default_val=10000,
                current_val=float(self.current_controls.exposure_us),
                unit="us",
            )

        if "gain" in parsed_ctrls:
            g = parsed_ctrls["gain"]
            mapped["gain"] = ControlRange(
                name="Gain",
                supported=True,
                writable=True,
                min_val=g.min_val,
                max_val=g.max_val,
                step=max(g.step, 1.0),
                default_val=g.default_val,
                current_val=g.current_val,
                unit="raw",
            )

        if "white_balance_temperature" in parsed_ctrls:
            wb = parsed_ctrls["white_balance_temperature"]
            mapped["wb_temp_k"] = ControlRange(
                name="White Balance Temp",
                supported=True,
                writable=True,
                min_val=wb.min_val,
                max_val=wb.max_val,
                step=max(wb.step, 50.0),
                default_val=wb.default_val,
                current_val=wb.current_val,
                unit="K",
            )

        focus_supported = "focus_absolute" in parsed_ctrls
        mapped["focus"] = ControlRange(
            name="Focus",
            supported=focus_supported,
            writable=focus_supported,
            min_val=parsed_ctrls["focus_absolute"].min_val if focus_supported else 0.0,
            max_val=parsed_ctrls["focus_absolute"].max_val if focus_supported else 0.0,
            step=parsed_ctrls["focus_absolute"].step if focus_supported else 1.0,
            default_val=0.0,
            current_val=0.0,
            unit="steps" if focus_supported else "fixed-focus",
        )

        return CameraCapabilities(
            camera_id=self.camera_id,
            backend_type=self.backend_key,
            name=self.name,
            device_path=self.device_path,
            sensor_model=f"USB UVC ({self.name})",
            shutter_type="rolling",
            rolling_shutter_warning=ROLLING_SHUTTER_ADVISORY,
            resolutions=[(640, 480), (1280, 720)],
            fps_options=[15, 25, 30],
            controls=mapped,
            lens_info={
                "focal_length_mm": 4.0,
                "fov_degrees": 60.0,
                "focus_mode": "manual" if focus_supported else "fixed",
            },
        )

    def set_controls(self, controls: dict[str, Any]) -> CameraControlSettings:
        updated = self.current_controls.model_dump()
        for k, v in controls.items():
            if k in updated and v is not None:
                updated[k] = v
        self.current_controls = CameraControlSettings(**updated)

        ctrl_pairs: list[str] = []
        if self.current_controls.auto_exposure:
            ctrl_pairs.append("auto_exposure=3")
        else:
            ctrl_pairs.append("auto_exposure=1")
            exp_100us = max(1, int(self.current_controls.exposure_us // 100))
            ctrl_pairs.append(f"exposure_time_absolute={exp_100us}")

        ctrl_pairs.append(f"gain={int(self.current_controls.gain)}")

        if self.current_controls.auto_wb:
            ctrl_pairs.append("white_balance_automatic=1")
        else:
            ctrl_pairs.append("white_balance_automatic=0")
            ctrl_pairs.append(
                f"white_balance_temperature={int(self.current_controls.wb_temp_k)}"
            )

        if not self.current_controls.autofocus:
            ctrl_pairs.append("focus_automatic_continuous=0")
            ctrl_pairs.append(f"focus_absolute={int(self.current_controls.focus_position)}")

        for pair in ctrl_pairs:
            self._run_v4l2_ctl([f"--set-ctrl={pair}"])

        return self.current_controls

    def start(self, width: int = 640, height: int = 480, fps: int = 30) -> None:
        self.stop()
        self.current_controls.width = width
        self.current_controls.height = height
        self.current_controls.fps = fps
        self._frame_idx = 0
        self.running = True
        self._thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._thread.start()

    def _capture_worker(self) -> None:
        w = self.current_controls.width
        h = self.current_controls.height
        fps = self.current_controls.fps

        # First try v4l2-ctl MJPG stream (zero CPU re-encode overhead on Pi)
        self._run_v4l2_ctl([f"--set-parm={fps}"])
        cmd = [
            "v4l2-ctl",
            f"--device={self.device_path}",
            f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
            "--stream-mmap=3",
            "--stream-to=-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            buf = bytearray()
            assert self._proc.stdout is not None
            while self.running:
                chunk = self._proc.stdout.read(32768)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        if start > 0:
                            del buf[:start]
                        break
                    jpg = bytes(buf[start : end + 2])
                    del buf[: end + 2]
                    mono_ns = time.monotonic_ns()
                    wall_ns = time.time_ns()
                    arr = np.frombuffer(jpg, dtype=np.uint8)
                    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if bgr is None:
                        continue
                    pkt = FramePacket(
                        frame_index=self._frame_idx,
                        timestamp_monotonic_ns=mono_ns,
                        timestamp_wall_ns=wall_ns,
                        bgr=bgr,
                        jpeg_bytes=jpg,
                    )
                    self._frame_idx += 1
                    with self._cond:
                        self._latest_packet = pkt
                        self._cond.notify_all()
        except OSError:
            pass
        finally:
            if self._proc is not None:
                self._proc.terminate()
                self._proc = None

    def stop(self) -> None:
        self.running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.5)
            except (OSError, subprocess.SubprocessError):
                pass
            self._proc = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def get_frame(self, timeout: float = 2.0) -> FramePacket:
        if not self.running:
            self.start(
                self.current_controls.width,
                self.current_controls.height,
                self.current_controls.fps,
            )
        with self._cond:
            if self._latest_packet is None:
                self._cond.wait(timeout=timeout)
            if self._latest_packet is None:
                raise RuntimeError(
                    f"Timed out waiting for frame from USB camera {self.device_path}. "
                    "Ensure the camera is connected and not in use by another process."
                )
            return self._latest_packet
