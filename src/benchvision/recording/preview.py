"""Low-overhead MJPEG preview generator and non-blocking live ROI signal buffer."""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Generator
from typing import Any

import cv2
import numpy as np

from benchvision.cameras.manager import CameraManager


class LivePreviewService:
    """Provides low-overhead MJPEG preview frames and a non-blocking live ROI signal buffer."""

    def __init__(self, camera_manager: CameraManager, max_signal_points: int = 150) -> None:
        self.camera_manager = camera_manager
        self.roi: tuple[int, int, int, int] | None = None
        self._signal_lock = threading.Lock()
        self._signal_buffer: collections.deque[dict[str, float]] = collections.deque(
            maxlen=max_signal_points
        )

    def set_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        """Set optional capture/preview ROI (x, y, w, h)."""
        self.roi = roi

    def record_roi_sample(self, bgr: np.ndarray, timestamp_monotonic_ns: int) -> None:
        """Compute mean green/luma intensity in ROI without blocking capture."""
        h, w = bgr.shape[:2]
        if self.roi and len(self.roi) == 4:
            rx, ry, rw, rh = self.roi
            x0 = max(0, min(rx, w - 1))
            y0 = max(0, min(ry, h - 1))
            x1 = max(x0 + 1, min(x0 + rw, w))
            y1 = max(y0 + 1, min(y0 + rh, h))
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

    def grab_preview_jpeg(self, draw_roi: bool = True, max_width: int = 640) -> bytes:
        """Grab a single preview JPEG frame, downscaling if needed for low network overhead."""
        cam = self.camera_manager.get_active()
        pkt = cam.get_frame(timeout=2.0)
        self.record_roi_sample(pkt.bgr, pkt.timestamp_monotonic_ns)

        # If no ROI overlay and native JPEG is available at <= max_width, pass through directly
        h, w = pkt.bgr.shape[:2]
        if (not draw_roi or self.roi is None) and pkt.jpeg_bytes and w <= max_width:
            return pkt.jpeg_bytes

        vis = pkt.bgr.copy()
        if draw_roi and self.roi and len(self.roi) == 4:
            rx, ry, rw, rh = self.roi
            cv2.rectangle(vis, (rx, ry), (rx + rw, ry + rh), (56, 189, 248), 2)

        if w > max_width:
            scale = max_width / float(w)
            vis = cv2.resize(
                vis,
                (max_width, max(1, round(h * scale))),
                interpolation=cv2.INTER_AREA,
            )

        ok, enc = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise RuntimeError("Failed to encode preview JPEG")
        return bytes(enc)

    def mjpeg_generator(self, max_fps: float = 15.0) -> Generator[bytes, Any, None]:
        """Yield multipart MJPEG frames for HTTP streaming."""
        min_interval = 1.0 / max(max_fps, 1.0)
        while True:
            t0 = time.monotonic()
            try:
                jpg = self.grab_preview_jpeg(draw_roi=True)
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
