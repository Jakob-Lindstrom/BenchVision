"""Low-overhead MJPEG preview generator and non-blocking live ROI signal buffer."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2
import numpy as np

from src.cameras.base import FramePacket
from src.cameras.manager import CameraManager


class LivePreviewService:
    """Provides low-overhead MJPEG preview frames and a non-blocking live ROI signal buffer."""

    def __init__(self, camera_manager: CameraManager, max_signal_points: int = 150) -> None:
        self.camera_manager = camera_manager
        self.roi: tuple[int, int, int, int] | None = None
        self.roi_b: tuple[int, int, int, int] | None = None
        self.target_size: tuple[int, int] = (640, 480)
        self.crop_preview_to_roi: bool = False
        self.tare_frame: np.ndarray | None = None
        self._persistence_accum: np.ndarray | None = None
        self._strobe_held_frame: np.ndarray | None = None
        self._strobe_last_ns: int = 0
        self._signal_lock = threading.Lock()
        self._signal_buffer: collections.deque[dict[str, float]] = collections.deque(
            maxlen=max_signal_points
        )
        self._frame_history: collections.deque[np.ndarray] = collections.deque(maxlen=16)
        self._preroll_lock = threading.Lock()
        self._preroll_buffer: collections.deque[tuple[np.ndarray, int, int]] = collections.deque(
            maxlen=75
        )
        self._recording_active: bool = False
        self._latest_rec_frame: np.ndarray | None = None
        self._latest_rec_lock = threading.Lock()
        self._dual_pool = ThreadPoolExecutor(max_workers=2)
        self._schlieren_ref: np.ndarray | None = None
        self._slit_canvas: np.ndarray | None = None
        self._last_mode: str = "normal"
        self._dvs_ref: np.ndarray | None = None
        self._dvs_on: np.ndarray | None = None
        self._dvs_off: np.ndarray | None = None
        self._dvs_last_ns: int = 0
        self._dvs_event_rate: float = 0.0

    def set_recording_active(self, active: bool) -> None:
        """Toggle recording state so live preview never steals hardware frames from capture."""
        self._recording_active = bool(active)
        if not self._recording_active:
            with self._latest_rec_lock:
                self._latest_rec_frame = None

    def publish_recording_frame(self, bgr: np.ndarray) -> None:
        """Publish the latest captured frame for non-intrusive preview rendering during recording."""
        with self._latest_rec_lock:
            self._latest_rec_frame = bgr.copy()

    def shutdown(self) -> None:
        """Clean up preview thread pool resources."""
        self._dual_pool.shutdown(wait=False)

    def push_preroll_frame(
        self, bgr: np.ndarray, mono_ns: int, wall_ns: int
    ) -> None:
        """Store frame in circular pre-roll buffer (last ~2.5s) for motion-triggered capture."""
        with self._preroll_lock:
            self._preroll_buffer.append((bgr.copy(), int(mono_ns), int(wall_ns)))

    def consume_preroll_frames(
        self, max_seconds: float = 2.0
    ) -> list[tuple[np.ndarray, int, int]]:
        """Pop and return buffered pre-roll frames within max_seconds of the latest frame."""
        with self._preroll_lock:
            items = list(self._preroll_buffer)
            self._preroll_buffer.clear()
        if not items:
            return []
        latest_ns = items[-1][1]
        cutoff_ns = latest_ns - int(max(0.2, float(max_seconds)) * 1e9)
        return [it for it in items if it[1] >= cutoff_ns]

    def get_preroll_count(self) -> int:
        with self._preroll_lock:
            return len(self._preroll_buffer)

    def evaluate_trigger_spike(
        self, threshold_sigma: float = 3.5, threshold_pct: float = 1.5
    ) -> dict[str, Any]:
        """Check if the latest ROI intensity sample exceeds N*sigma or % change above baseline."""
        with self._signal_lock:
            pts = list(self._signal_buffer)
        preroll_n = self.get_preroll_count()
        if len(pts) < 6:
            return {
                "triggered": False,
                "z_score": 0.0,
                "pct_change": 0.0,
                "current_z_sigma": 0.0,
                "current_pct_jump": 0.0,
                "preroll_frames": preroll_n,
            }
        baseline_vals = np.array([p["value"] for p in pts[:-2]], dtype=np.float64)
        latest_val = float(pts[-1]["value"])
        mean_val = float(np.mean(baseline_vals))
        std_val = max(float(np.std(baseline_vals)), 0.15)
        delta = abs(latest_val - mean_val)
        z_score = delta / std_val
        pct_change = (delta / max(abs(mean_val), 1.0)) * 100.0
        triggered = (z_score >= float(threshold_sigma)) and (
            pct_change >= float(threshold_pct)
        )
        return {
            "triggered": bool(triggered),
            "z_score": round(z_score, 2),
            "pct_change": round(pct_change, 2),
            "current_z_sigma": round(z_score, 2),
            "current_pct_jump": round(pct_change, 2),
            "preroll_frames": preroll_n,
        }

    def set_tare_frame(self, enable: bool = True) -> dict[str, Any]:
        """Lock the current camera frame as a static 50% gray / Schlieren reference (Tare / Zero Frame)."""
        if not enable:
            self.tare_frame = None
            self._schlieren_ref = None
            return {"tare_locked": False}
        cam = self.camera_manager.get_active()
        pkt = cam.get_frame(timeout=2.0)
        self.tare_frame = pkt.bgr.copy()
        self._schlieren_ref = self.tare_frame.copy()
        return {"tare_locked": True, "shape": list(self.tare_frame.shape[:2])}

    def set_roi(
        self,
        roi: tuple[int, int, int, int] | None,
        crop_preview: bool | None = None,
        target_size: tuple[int, int] | None = None,
        roi_b: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Set optional capture/preview ROI (x, y, w, h) and target resolution coordinate space."""
        self.roi = roi
        if roi_b is not None:
            self.roi_b = roi_b
        if crop_preview is not None:
            self.crop_preview_to_roi = bool(crop_preview)
        if target_size is not None and target_size[0] > 0 and target_size[1] > 0:
            self.target_size = (int(target_size[0]), int(target_size[1]))

    def _map_roi_to_frame(
        self,
        frame_w: int,
        frame_h: int,
        roi_box: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Scale ROI from target recording resolution to the actual preview frame resolution."""
        active_box = roi_box if roi_box is not None else self.roi
        if not active_box or len(active_box) != 4:
            return None
        rx, ry, rw, rh = active_box
        tw, th = self.target_size
        sx = frame_w / float(max(tw, 1))
        sy = frame_h / float(max(th, 1))
        min_w = min(16, frame_w)
        min_h = min(16, frame_h)
        x0 = max(0, min(round(rx * sx), frame_w - min_w))
        y0 = max(0, min(round(ry * sy), frame_h - min_h))
        x1 = max(x0 + min_w, min(round((rx + rw) * sx), frame_w))
        y1 = max(y0 + min_h, min(round((ry + rh) * sy), frame_h))
        return (x0, y0, x1, y1)

    def record_roi_sample(self, bgr: np.ndarray, timestamp_monotonic_ns: int) -> None:
        """Compute mean green/luma intensity in ROI without blocking capture."""
        h, w = bgr.shape[:2]
        mapped = self._map_roi_to_frame(w, h)
        if mapped is not None:
            x0, y0, x1, y1 = mapped
            sub = bgr[y0:y1, x0:x1]
        else:
            sub = bgr[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]

        green_mean = float(np.mean(sub[:, :, 1])) if sub.ndim == 3 else float(np.mean(sub))
        with self._signal_lock:
            self._signal_buffer.append(
                {
                    "t_sec": round(timestamp_monotonic_ns / 1e9, 4),
                    "value": round(green_mean, 3),
                }
            )

    def get_live_signal(self) -> list[dict[str, float]]:
        """Return recent ROI intensity samples relative to first timestamp in buffer."""
        with self._signal_lock:
            pts = list(self._signal_buffer)
        if not pts:
            return []
        t0 = pts[0]["t_sec"]
        mean_val = sum(p["value"] for p in pts) / len(pts)
        return [
            {
                "t": round(p["t_sec"] - t0, 3),
                "raw": p["value"],
                "detrended": round(p["value"] - mean_val, 4),
            }
            for p in pts
        ]

    def grab_preview_jpeg(
        self,
        draw_roi: bool = True,
        max_width: int = 640,
        mode: str = "normal",
        shift_frames: int = 2,
        gain: float = 1.0,
        strobe_hz: float = 0.0,
        assist: str = "off",
        cam_view: str = "auto",
    ) -> bytes:
        """Grab a single preview JPEG frame with Posy modes, Tare lock, Stroboscope, Focus/Zebra assist, or Dual-Cam view."""
        dual_on = bool(self.camera_manager.settings.dual_camera_enabled)
        eff_view = ("sbs" if dual_on else "a") if cam_view in ("", "auto") else cam_view

        if self._recording_active:
            active_roi = self.roi
            with self._latest_rec_lock:
                rec_bgr = (
                    self._latest_rec_frame.copy()
                    if self._latest_rec_frame is not None
                    else None
                )
            if rec_bgr is None:
                if self._frame_history:
                    rec_bgr = self._frame_history[-1].copy()
                else:
                    rec_bgr = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(
                        rec_bgr,
                        "RECORDING IN PROGRESS...",
                        (150, 240),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.75,
                        (200, 210, 225),
                        2,
                    )
            pkt = FramePacket(
                frame_index=0,
                timestamp_monotonic_ns=time.monotonic_ns(),
                timestamp_wall_ns=time.time_ns(),
                bgr=rec_bgr,
                jpeg_bytes=None,
            )
        elif dual_on and eff_view == "b":
            cam = self.camera_manager.get_secondary()
            active_roi = self.roi_b if self.roi_b is not None else self.roi
            pkt = cam.get_frame(timeout=3.0)
        elif dual_on and eff_view == "sbs":
            cam_a = self.camera_manager.get_active()
            cam_b = self.camera_manager.get_secondary()
            fut_a = self._dual_pool.submit(cam_a.get_frame, 3.0)
            fut_b = self._dual_pool.submit(cam_b.get_frame, 3.0)
            pkt = fut_a.result()
            pkt_b = fut_b.result()
            self.record_roi_sample(pkt.bgr, pkt.timestamp_monotonic_ns)
            self.push_preroll_frame(pkt.bgr, pkt.timestamp_monotonic_ns, pkt.timestamp_wall_ns)
            h_a, w_a = pkt.bgr.shape[:2]
            frm_b = (
                cv2.resize(pkt_b.bgr, (w_a, h_a), interpolation=cv2.INTER_AREA)
                if pkt_b.bgr.shape[:2] != (h_a, w_a)
                else pkt_b.bgr.copy()
            )
            left = pkt.bgr.copy()
            right = frm_b
            if draw_roi and self.roi:
                mapped_a = self._map_roi_to_frame(w_a, h_a, roi_box=self.roi)
                eff_roi_b = self.roi_b if self.roi_b is not None else self.roi
                mapped_b = self._map_roi_to_frame(w_a, h_a, roi_box=eff_roi_b)
                if mapped_a is not None:
                    ax0, ay0, ax1, ay1 = mapped_a
                    if self.crop_preview_to_roi:
                        left = left[ay0:ay1, ax0:ax1].copy()
                    else:
                        cv2.rectangle(left, (ax0, ay0), (ax1, ay1), (56, 189, 248), 2)
                if mapped_b is not None:
                    bx0, by0, bx1, by1 = mapped_b
                    if self.crop_preview_to_roi:
                        right = right[by0:by1, bx0:bx1].copy()
                        if right.shape[:2] != left.shape[:2]:
                            right = cv2.resize(
                                right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA
                            )
                    else:
                        cv2.rectangle(right, (bx0, by0), (bx1, by1), (11, 158, 245), 2)
            cv2.putText(left, "CAM A", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (56, 189, 248), 2)
            cv2.putText(right, "CAM B", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (11, 158, 245), 2)
            left[:, -1:] = 80
            right[:, :1] = 80
            sbs_vis = np.concatenate([left, right], axis=1)
            pkt = FramePacket(
                frame_index=pkt.frame_index,
                timestamp_monotonic_ns=pkt.timestamp_monotonic_ns,
                timestamp_wall_ns=pkt.timestamp_wall_ns,
                bgr=sbs_vis,
                jpeg_bytes=None,
            )
            active_roi = None
            max_width = max(max_width, 960)
        else:
            cam = self.camera_manager.get_active()
            active_roi = self.roi
            pkt = cam.get_frame(timeout=3.0)
            self.record_roi_sample(pkt.bgr, pkt.timestamp_monotonic_ns)
            self.push_preroll_frame(pkt.bgr, pkt.timestamp_monotonic_ns, pkt.timestamp_wall_ns)

        # Reset history/state buffers if frame dimensions, slit-scan, or DVS mode changed
        if self._frame_history and self._frame_history[-1].shape != pkt.bgr.shape:
            self._frame_history.clear()
            self._persistence_accum = None
            self._strobe_held_frame = None
            self._slit_canvas = None
            self._dvs_ref = None
            self._dvs_on = None
            self._dvs_off = None
            if self.tare_frame is not None and self.tare_frame.shape != pkt.bgr.shape:
                self.tare_frame = None
            if self._schlieren_ref is not None and self._schlieren_ref.shape != pkt.bgr.shape:
                self._schlieren_ref = None
        if mode != self._last_mode and (
            mode in ("slit_scan", "slit_scan_h")
            or self._last_mode in ("slit_scan", "slit_scan_h")
        ):
            self._slit_canvas = None
        if mode != self._last_mode and (mode == "dvs_events" or self._last_mode == "dvs_events"):
            self._dvs_ref = None
            self._dvs_on = None
            self._dvs_off = None
            self._dvs_event_rate = 0.0
        self._last_mode = mode

        # If normal mode, no tare lock, no ROI overlay, no assist, and native JPEG is available at <= max_width
        h, w = pkt.bgr.shape[:2]
        if (
            mode == "normal"
            and assist == "off"
            and self.tare_frame is None
            and strobe_hz <= 0.0
            and (not draw_roi or active_roi is None)
            and pkt.jpeg_bytes
            and w <= max_width
        ):
            self._frame_history.append(pkt.bgr)
            return pkt.jpeg_bytes

        eff_gain = max(1, round(gain))
        k = max(1, min(shift_frames, 7))

        if mode == "schlieren":
            ref = (
                self.tare_frame
                if (self.tare_frame is not None and self.tare_frame.shape == pkt.bgr.shape)
                else self._schlieren_ref
            )
            if ref is None or ref.shape != pkt.bgr.shape:
                self._schlieren_ref = pkt.bgr.copy()
                ref = self._schlieren_ref
            diff = cv2.GaussianBlur(
                pkt.bgr.astype(np.float32) - ref.astype(np.float32), (3, 3), 0
            )
            mag = np.sqrt(np.sum(diff * diff, axis=2))
            scaled = np.clip((mag - 2.5) * max(2.0, float(gain) * 4.0), 0.0, 255.0).astype(np.uint8)
            heat_bgr = cv2.applyColorMap(scaled, cv2.COLORMAP_INFERNO)
            gray_bgr = cv2.cvtColor(cv2.cvtColor(pkt.bgr, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            blended = cv2.addWeighted(gray_bgr, 0.42, heat_bgr, 0.58, 0.0)
            dim_gray = (gray_bgr.astype(np.uint16) * 180 // 255).astype(np.uint8)
            vis = np.where((mag > 1.0)[..., None], blended, dim_gray)
            cv2.putText(
                vis,
                "SCHLIEREN BOS (HEAT / AIRFLOW)",
                (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (11, 158, 245),
                2,
            )
        elif mode in ("slit_scan", "slit_scan_h"):
            if self._slit_canvas is None or self._slit_canvas.shape != pkt.bgr.shape:
                self._slit_canvas = pkt.bgr.copy()
            strip = max(2, min(6, eff_gain + 1))
            if mode == "slit_scan":
                strip = min(strip, max(1, w - 1))
                self._slit_canvas[:, :-strip] = self._slit_canvas[:, strip:]
                cx = w // 2
                self._slit_canvas[:, -strip:] = pkt.bgr[:, cx : cx + 1]
                vis = self._slit_canvas.copy()
                cv2.line(
                    vis,
                    (max(0, w - strip - 1), 0),
                    (max(0, w - strip - 1), h - 1),
                    (248, 189, 56),
                    2,
                )
            else:
                strip = min(strip, max(1, h - 1))
                self._slit_canvas[:-strip, :] = self._slit_canvas[strip:, :]
                cy = h // 2
                self._slit_canvas[-strip:, :] = pkt.bgr[cy : cy + 1, :]
                vis = self._slit_canvas.copy()
                cv2.line(
                    vis,
                    (0, max(0, h - strip - 1)),
                    (w - 1, max(0, h - strip - 1)),
                    (248, 189, 56),
                    2,
                )
            cv2.putText(
                vis,
                "LIVE SLIT-SCAN TIME-RIBBON",
                (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (248, 189, 56),
                2,
            )
        elif mode == "dvs_events":
            gray = cv2.GaussianBlur(
                cv2.cvtColor(pkt.bgr, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3), 0
            )
            log_now = np.log(gray + 16.0)
            c_thresh = max(0.05, 0.22 / max(0.5, float(gain)))
            if self._dvs_ref is None or self._dvs_ref.shape != log_now.shape:
                self._dvs_ref = log_now.copy()
                self._dvs_on = np.zeros_like(log_now, dtype=np.float32)
                self._dvs_off = np.zeros_like(log_now, dtype=np.float32)
                self._dvs_last_ns = pkt.timestamp_monotonic_ns
                self._dvs_event_rate = 0.0
            else:
                delta = log_now - self._dvs_ref
                on_mask = delta >= c_thresh
                off_mask = delta <= -c_thresh
                fired = on_mask | off_mask
                self._dvs_ref[fired] = log_now[fired]
                dt_raw = (pkt.timestamp_monotonic_ns - self._dvs_last_ns) / 1e9
                dt_s = max(0.005, min(0.5, dt_raw)) if dt_raw > 0 else (1.0 / 30.0)
                self._dvs_last_ns = pkt.timestamp_monotonic_ns
                self._dvs_event_rate = float(np.count_nonzero(fired)) / dt_s
                if self._dvs_on is None or self._dvs_off is None:
                    self._dvs_on = np.zeros_like(log_now, dtype=np.float32)
                    self._dvs_off = np.zeros_like(log_now, dtype=np.float32)
                self._dvs_on *= 0.78
                self._dvs_off *= 0.78
                self._dvs_on[on_mask] = 1.0
                self._dvs_off[off_mask] = 1.0
            base = np.zeros((h, w, 3), dtype=np.float32)
            base[..., 0] = 20.0
            base[..., 1] = 8.0
            base[..., 2] = 5.0
            on_col = np.array([248.0, 189.0, 56.0], dtype=np.float32)
            off_col = np.array([90.0, 95.0, 250.0], dtype=np.float32)
            on_f = (self._dvs_on if self._dvs_on is not None else np.zeros((h, w)))[..., None]
            off_f = (self._dvs_off if self._dvs_off is not None else np.zeros((h, w)))[..., None]
            comp = base + on_f * on_col[None, None, :] + off_f * off_col[None, None, :]
            vis = np.clip(comp, 0.0, 255.0).astype(np.uint8)
            cv2.putText(
                vis,
                f"DVS EVENT CAM (+ON/-OFF) | {self._dvs_event_rate / 1000.0:.1f} kEv/s",
                (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (248, 189, 56),
                2,
            )
        elif self.tare_frame is not None and self.tare_frame.shape == pkt.bgr.shape:
            delta = pkt.bgr.astype(np.int16) - self.tare_frame.astype(np.int16)
            vis = np.clip(128 + (delta * max(2, eff_gain)) // 2, 0, 255).astype(np.uint8)
            cv2.putText(
                vis,
                "TARE / ZERO REFERENCE LOCKED",
                (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (34, 197, 94),
                2,
            )
        elif mode == "motion_extraction" and len(self._frame_history) > 0:
            idx = max(0, len(self._frame_history) - max(1, min(shift_frames, 15)))
            prev = self._frame_history[idx]
            if prev.shape == pkt.bgr.shape:
                delta = pkt.bgr.astype(np.int16) - prev.astype(np.int16)
                vis = np.clip(128 + (delta * eff_gain) // 2, 0, 255).astype(np.uint8)
            else:
                vis = pkt.bgr.copy()
        elif mode == "posy_rgb_trail" and len(self._frame_history) >= 2:
            idx1 = max(0, len(self._frame_history) - k)
            idx2 = max(0, len(self._frame_history) - 2 * k)
            f1 = self._frame_history[idx1]
            f2 = self._frame_history[idx2]
            if f1.shape == pkt.bgr.shape and f2.shape == pkt.bgr.shape:
                f0_i = pkt.bgr.astype(np.int16)
                f1_i = f1.astype(np.int16)
                f2_i = f2.astype(np.int16)
                vis = np.empty_like(pkt.bgr)
                vis[..., 2] = np.clip(f1_i[..., 2] + eff_gain * (f0_i[..., 2] - f1_i[..., 2]), 0, 255).astype(np.uint8)
                vis[..., 1] = np.clip(f1_i[..., 1] + (eff_gain * (2 * f1_i[..., 1] - f0_i[..., 1] - f2_i[..., 1])) // 2, 0, 255).astype(np.uint8)
                vis[..., 0] = np.clip(f1_i[..., 0] + eff_gain * (f2_i[..., 0] - f1_i[..., 0]), 0, 255).astype(np.uint8)
            else:
                vis = pkt.bgr.copy()
        elif mode == "posy_peak_hold" and len(self._frame_history) > 0:
            idx = max(0, len(self._frame_history) - k)
            prev = self._frame_history[idx]
            if prev.shape == pkt.bgr.shape:
                delta_f = (pkt.bgr.astype(np.float32) - prev.astype(np.float32)) * float(eff_gain)
                abs_d = np.abs(delta_f)
                if self._persistence_accum is None or self._persistence_accum.shape != pkt.bgr.shape:
                    self._persistence_accum = abs_d
                else:
                    self._persistence_accum = np.maximum(self._persistence_accum * 0.84, abs_d)
                vis = np.clip(128.0 + 0.4 * delta_f + 0.65 * self._persistence_accum, 0, 255).astype(np.uint8)
            else:
                vis = pkt.bgr.copy()
        elif mode == "stroboscope" or strobe_hz > 0.0:
            target_hz = max(0.5, strobe_hz if strobe_hz > 0.0 else 2.0)
            period_ns = int(1e9 / target_hz)
            if (
                self._strobe_held_frame is None
                or self._strobe_held_frame.shape != pkt.bgr.shape
                or (pkt.timestamp_monotonic_ns - self._strobe_last_ns) >= period_ns
            ):
                self._strobe_held_frame = pkt.bgr.copy()
                self._strobe_last_ns = pkt.timestamp_monotonic_ns
            vis = self._strobe_held_frame.copy()
            cv2.putText(
                vis,
                f"STROBE ALIAS: {target_hz:.2f} Hz",
                (10, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (245, 158, 11),
                2,
            )
        else:
            vis = pkt.bgr.copy()

        self._frame_history.append(pkt.bgr)

        if assist == "focus_peaking":
            gray = cv2.cvtColor(pkt.bgr, cv2.COLOR_BGR2GRAY)
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            mag = cv2.magnitude(gx, gy)
            thresh = max(35.0, float(np.percentile(mag, 91.0)))
            edge_mask = mag >= thresh
            vis[edge_mask] = (34, 255, 64)
            cv2.putText(
                vis,
                "FOCUS PEAKING (GREEN EDGES)",
                (10, 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (34, 255, 64),
                2,
            )
        elif assist == "zebras":
            max_ch = np.max(pkt.bgr, axis=2)
            y_idx = np.arange(h, dtype=np.int32)[:, None]
            x_idx = np.arange(w, dtype=np.int32)[None, :]
            stripe_mask = (max_ch >= 250) & ((((x_idx + y_idx) >> 2) & 1) == 0)
            vis[stripe_mask] = (36, 36, 239)
            clipped_pct = round(float(np.mean(max_ch >= 250)) * 100.0, 2)
            cv2.putText(
                vis,
                f"OVEREXPOSURE ZEBRAS (>=250): {clipped_pct}% CLIPPED",
                (10, 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (68, 68, 239),
                2,
            )

        mapped = self._map_roi_to_frame(w, h, roi_box=active_roi) if draw_roi else None
        if mapped is not None and active_roi is not None:
            x0, y0, x1, y1 = mapped
            rx, ry, rw, rh = active_roi
            label = f"ROI: {rw}x{rh} at ({rx},{ry})"
            if self.crop_preview_to_roi:
                vis = vis[y0:y1, x0:x1].copy()
                cv2.putText(
                    vis,
                    f"CROPPED {label}",
                    (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    (56, 189, 248),
                    2,
                )
            else:
                darkened = (vis.astype(np.uint16) * 100 // 255).astype(np.uint8)
                darkened[y0:y1, x0:x1] = vis[y0:y1, x0:x1]
                vis = darkened
                cv2.rectangle(vis, (x0, y0), (x1, y1), (56, 189, 248), 2)
                ty = y0 - 8 if y0 >= 24 else min(h - 8, y0 + 20)
                cv2.putText(
                    vis,
                    label,
                    (max(4, x0 + 4), ty),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    (56, 189, 248),
                    2,
                )

        cur_h, cur_w = vis.shape[:2]
        if self._recording_active and cur_w >= 80 and cur_h >= 32:
            cv2.rectangle(vis, (cur_w - 68, 6), (cur_w - 8, 28), (15, 23, 42), -1)
            cv2.circle(vis, (cur_w - 56, 17), 5, (48, 48, 240), -1)
            cv2.putText(
                vis,
                "REC",
                (cur_w - 46, 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (248, 250, 252),
                1,
                cv2.LINE_AA,
            )

        if cur_w > max_width:
            scale = max_width / float(cur_w)
            vis = cv2.resize(
                vis,
                (max_width, max(1, round(cur_h * scale))),
                interpolation=cv2.INTER_AREA,
            )

        ok, enc = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("Failed to encode preview JPEG")
        return bytes(enc)

    def mjpeg_generator(
        self,
        max_fps: float = 15.0,
        mode: str = "normal",
        shift_frames: int = 2,
        gain: float = 1.0,
        strobe_hz: float = 0.0,
        assist: str = "off",
        cam_view: str = "auto",
    ) -> Generator[bytes, Any, None]:
        """Yield multipart MJPEG frames for HTTP streaming."""
        min_interval = 1.0 / max(max_fps, 1.0)
        while True:
            t0 = time.monotonic()
            try:
                jpg = self.grab_preview_jpeg(
                    draw_roi=True,
                    mode=mode,
                    shift_frames=shift_frames,
                    gain=gain,
                    strobe_hz=strobe_hz,
                    assist=assist,
                    cam_view=cam_view,
                )
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpg)}\r\n\r\n".encode("ascii")
                    + jpg
                    + b"\r\n"
                )
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
                continue

            elapsed = time.monotonic() - t0
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
