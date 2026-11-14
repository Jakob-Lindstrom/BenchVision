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

from src.cameras.base import (
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


def parse_v4l2_formats_ext_output(output: str) -> dict[tuple[int, int], list[int]]:
    """Parse `v4l2-ctl --list-formats-ext` output to map MJPG (width, height) -> supported FPS list."""
    modes: dict[tuple[int, int], list[int]] = {}
    in_mjpg = False
    cur_res: tuple[int, int] | None = None
    size_re = re.compile(r"Size:\s+Discrete\s+(\d+)x(\d+)")
    fps_re = re.compile(r"\(([0-9]+(?:\.[0-9]+)?)\s*fps\)")

    for line in output.splitlines():
        if "[" in line and ("'MJPG'" in line or "'JPEG'" in line):
            in_mjpg = True
            cur_res = None
            continue
        if "[" in line and "'" in line and "'MJPG'" not in line and "'JPEG'" not in line:
            in_mjpg = False
            cur_res = None
            continue
        if not in_mjpg:
            continue
        sm = size_re.search(line)
        if sm:
            cur_res = (int(sm.group(1)), int(sm.group(2)))
            modes.setdefault(cur_res, [])
            continue
        if cur_res is not None:
            fm = fps_re.search(line)
            if fm:
                fps_int = round(float(fm.group(1)))
                if fps_int > 0 and fps_int not in modes[cur_res]:
                    modes[cur_res].append(fps_int)
    return modes


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
        self._start_lock = threading.RLock()
        self._cond = threading.Condition()
        self._latest_packet: FramePacket | None = None
        self._frame_idx = 0
        self._last_consumed_idx = -1
        self._mjpg_modes: dict[tuple[int, int], list[int]] | None = None

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

    def _get_mjpg_modes(self) -> dict[tuple[int, int], list[int]]:
        """Query and cache supported MJPG resolutions and discrete frame rates."""
        if self._mjpg_modes is not None:
            return self._mjpg_modes
        raw = self._run_v4l2_ctl(["--list-formats-ext"])
        parsed = parse_v4l2_formats_ext_output(raw)
        if parsed:
            self._mjpg_modes = parsed
            return parsed
        return {}

    def _resolve_hw_capture_mode(
        self, width: int, height: int, fps: int
    ) -> tuple[int, int, int]:
        """Resolve optimal hardware MJPG (width, height, fps) for the requested capture mode.

        Many high-speed UVC cameras (e.g. Logitech BRIO 4K over USB 2.0) expose 60 FPS only
        at 1280x720 MJPG while capping 640x480 and 1920x1080 at 30 FPS. When fps > 30 is
        requested and the requested resolution does not natively list > 30 FPS, automatically
        capture in hardware at the resolution that supports the requested high frame rate.
        """
        modes = self._get_mjpg_modes()
        if not modes:
            return (width, height, fps)

        req_res = (int(width), int(height))
        req_fps = int(fps)
        req_rates = modes.get(req_res, [])
        if req_rates and max(req_rates) >= req_fps:
            return (req_res[0], req_res[1], req_fps)

        if req_fps > 30:
            # Prefer 1280x720 first, then 640x480, then any resolution supporting >= req_fps
            preferred = [(1280, 720), (640, 480), *list(modes.keys())]
            for cand_res in preferred:
                rates = modes.get(cand_res, [])
                if rates and max(rates) >= min(req_fps, 50):
                    best_fps = req_fps if req_fps in rates else max(rates)
                    return (cand_res[0], cand_res[1], best_fps)

        if req_res in modes:
            rates = modes[req_res]
            best_fps = req_fps if (not rates or req_fps in rates) else max(rates)
            return (req_res[0], req_res[1], best_fps)

        return (width, height, fps)

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

        modes = self._get_mjpg_modes()
        res_list: list[tuple[int, int]] = [(640, 480), (1280, 720)]
        if (1920, 1080) in modes:
            res_list.append((1920, 1080))
        fps_list: list[int] = [15, 25, 30]
        if any(max(r, default=0) >= 50 for r in modes.values()):
            fps_list.append(60)

        return CameraCapabilities(
            camera_id=self.camera_id,
            backend_type=self.backend_key,
            name=self.name,
            device_path=self.device_path,
            sensor_model=f"USB UVC ({self.name})",
            shutter_type="rolling",
            rolling_shutter_warning=ROLLING_SHUTTER_ADVISORY,
            resolutions=res_list,
            fps_options=fps_list,
            controls=mapped,
            lens_info={
                "focal_length_mm": 4.0,
                "fov_degrees": 60.0,
                "focus_mode": "manual" if focus_supported else "fixed",
            },
        )

    def lock_controls(self, overrides: dict[str, Any] | None = None) -> CameraControlSettings:
        """Freeze exposure, gain, white balance, and focus before recording.

        If transitioning from Auto mode and the caller passed default/uncalibrated values
        (e.g. gain <= 2.0 on a 0..255 V4L2 webcam), read the live auto-negotiated values
        from hardware and cap exposure to 1/fps (compensating gain) so brightness is preserved.
        """
        merged: dict[str, Any] = dict(overrides or {})
        raw_ctrls = self._run_v4l2_ctl(["--list-ctrls"])
        parsed = parse_v4l2_ctrls_output(raw_ctrls)

        fps = int(merged.get("fps") or self.current_controls.fps or 30)
        max_exp_us = max(2000, int(920_000 // max(fps, 1)))

        if parsed:
            hw_exp_us = (
                int(parsed["exposure_time_absolute"].current_val * 100.0)
                if "exposure_time_absolute" in parsed
                else int(merged.get("exposure_us", 10000))
            )
            hw_gain = (
                float(parsed["gain"].current_val)
                if "gain" in parsed
                else float(merged.get("gain", 1.5))
            )
            hw_wb = (
                int(parsed["white_balance_temperature"].current_val)
                if "white_balance_temperature" in parsed
                else int(merged.get("wb_temp_k", 4600))
            )
            max_g = parsed["gain"].max_val if "gain" in parsed else 255.0

            # Never apply uncalibrated default gain (<= 2.0) on a 0..255 V4L2 USB webcam
            req_exp = int(merged.get("exposure_us", 10000))
            req_gain = float(merged.get("gain", 1.5))
            if max_g >= 100.0 and req_gain <= 2.0:
                if self.current_controls.locked and self.current_controls.gain > 2.0:
                    merged["exposure_us"] = min(int(self.current_controls.exposure_us), max_exp_us)
                    merged["gain"] = float(self.current_controls.gain)
                    merged["wb_temp_k"] = int(self.current_controls.wb_temp_k)
                elif hw_gain > 2.0:
                    eff_exp = min(hw_exp_us, max_exp_us)
                    ratio = max(1.0, hw_exp_us / float(max(eff_exp, 100)))
                    merged["exposure_us"] = eff_exp
                    merged["gain"] = min(max_g, round(hw_gain * ratio, 1))
                    merged["wb_temp_k"] = hw_wb
                else:
                    merged["exposure_us"] = max_exp_us
                    merged["gain"] = min(max_g, 160.0)
                    merged["wb_temp_k"] = hw_wb if hw_wb > 0 else 4600
            elif req_exp > max_exp_us:
                merged["exposure_us"] = max_exp_us

        merged.update(
            {
                "auto_exposure": False,
                "auto_wb": False,
                "autofocus": False,
                "locked": True,
            }
        )
        return self.set_controls(merged)

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
        self._run_v4l2_ctl(["--set-ctrl=exposure_dynamic_framerate=0"])

        return self.current_controls

    def start(self, width: int = 640, height: int = 480, fps: int = 30) -> None:
        with self._start_lock:
            self.stop()
            self.current_controls.width = int(width)
            self.current_controls.height = int(height)
            self.current_controls.fps = int(fps)
            self._stream_width = int(width)
            self._stream_height = int(height)
            self._stream_fps = int(fps)
            self._frame_idx = 0
            self._last_consumed_idx = -1
            self._latest_packet = None
            self.running = True
            self._run_v4l2_ctl(["--set-ctrl=exposure_dynamic_framerate=0"])
            if int(fps) > 30:
                # High-FPS UVC capture requires manual exposure <= 1/fps so auto-exposure does not throttle to 30 FPS
                max_exp_100us = max(10, int(10000 // max(int(fps), 1)) - 5)
                cur_exp_100us = max(1, int(self.current_controls.exposure_us // 100))
                eff_exp_100us = min(cur_exp_100us, max_exp_100us)
                self._run_v4l2_ctl(["--set-ctrl=auto_exposure=1"])
                self._run_v4l2_ctl([f"--set-ctrl=exposure_time_absolute={eff_exp_100us}"])
                if self.current_controls.gain <= 2.0:
                    self._run_v4l2_ctl(["--set-ctrl=gain=160"])
            elif not self.current_controls.locked:
                self._run_v4l2_ctl(["--set-ctrl=auto_exposure=3"])
                self._run_v4l2_ctl(["--set-ctrl=white_balance_automatic=1"])
            self._thread = threading.Thread(target=self._capture_worker, daemon=True)
            self._thread.start()

    def _capture_worker(self) -> None:
        w = self.current_controls.width
        h = self.current_controls.height
        fps = self.current_controls.fps
        hw_w, hw_h, hw_fps = self._resolve_hw_capture_mode(w, h, fps)

        # First try v4l2-ctl MJPG stream (zero CPU re-encode overhead on Pi).
        # Pass --set-parm AFTER --set-fmt-video in the same invocation so UVC does not reset interval to 30 FPS.
        cmd = [
            "v4l2-ctl",
            f"--device={self.device_path}",
            f"--set-fmt-video=width={hw_w},height={hw_h},pixelformat=MJPG",
            f"--set-parm={hw_fps}",
            "--stream-mmap=3",
            "--stream-to=-",
        ]
        proc: subprocess.Popen[bytes] | None = None
        got_mjpg_frame = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._proc = proc
            buf = bytearray()
            stdout = proc.stdout
            if stdout is not None:
                while self.running:
                    chunk = stdout.read(32768)
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
                        got_mjpg_frame = True
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
        except (OSError, ValueError):
            pass
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                if self._proc is proc:
                    self._proc = None

        # Fallback to OpenCV VideoCapture (YUYV/MJPG) if v4l2-ctl MJPG stream exited early while still running
        if self.running and not got_mjpg_frame:
            cap: cv2.VideoCapture | None = None
            try:
                cap = cv2.VideoCapture(self.device_path, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap = cv2.VideoCapture(self.device_path)
                self._cap = cap
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(w))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(h))
                cap.set(cv2.CAP_PROP_FPS, float(fps))
                while self.running and cap.isOpened():
                    ok, bgr = cap.read()
                    if not ok or bgr is None:
                        time.sleep(0.02)
                        continue
                    mono_ns = time.monotonic_ns()
                    wall_ns = time.time_ns()
                    ok_j, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    pkt = FramePacket(
                        frame_index=self._frame_idx,
                        timestamp_monotonic_ns=mono_ns,
                        timestamp_wall_ns=wall_ns,
                        bgr=bgr,
                        jpeg_bytes=bytes(enc) if ok_j else None,
                    )
                    self._frame_idx += 1
                    with self._cond:
                        self._latest_packet = pkt
                        self._cond.notify_all()
            except (OSError, ValueError, cv2.error):
                pass
            finally:
                if cap is not None:
                    cap.release()
                if self._cap is cap:
                    self._cap = None

        if self._thread is threading.current_thread():
            self.running = False
            with self._cond:
                self._cond.notify_all()

    def stop(self) -> None:
        with self._start_lock:
            self.running = False
            self._stream_width = 0
            self._stream_height = 0
            self._stream_fps = 0
            proc = self._proc
            self._proc = None
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except (OSError, subprocess.SubprocessError):
                    pass
            cap = self._cap
            self._cap = None
            if cap is not None:
                cap.release()
            with self._cond:
                self._cond.notify_all()

    def get_frame(self, timeout: float = 2.0) -> FramePacket:
        with self._start_lock:
            if not self.running or (self._thread is not None and not self._thread.is_alive()):
                self.start(
                    self.current_controls.width,
                    self.current_controls.height,
                    self.current_controls.fps,
                )
        with self._cond:
            if self._latest_packet is None:
                self._cond.wait(timeout=max(timeout, 3.5))
            elif self._latest_packet.frame_index <= self._last_consumed_idx:
                self._cond.wait(timeout=timeout)
            if self._latest_packet is None:
                raise RuntimeError(
                    f"Timed out waiting for frame from USB camera {self.device_path}. "
                    "Ensure the camera is connected and not in use by another process."
                )
            self._last_consumed_idx = self._latest_packet.frame_index
            return self._latest_packet

