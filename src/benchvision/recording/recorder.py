"""Lossless/near-lossless video recorder with locked controls and nanosecond frame timestamps."""

from __future__ import annotations

import csv
import json
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from benchvision.cameras.manager import CameraManager
from benchvision.recording.preview import LivePreviewService
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout


def encode_browser_mp4(frames_bgr: list[np.ndarray] | np.ndarray, fps: float, out_path: Path) -> Path:
    """Encode BGR frames to browser-playable MP4 (via ffmpeg if installed, else OpenCV mp4v)."""
    out_path = Path(out_path).resolve()
    if len(frames_bgr) == 0:
        raise ValueError("Cannot encode empty frame sequence")

    h, w = frames_bgr[0].shape[:2]
    # Ensure even dimensions for YUV420p compatibility
    even_w = w if w % 2 == 0 else w - 1
    even_h = h if h % 2 == 0 else h - 1

    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "rawvideo",
            "-vcodec",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{even_w}x{even_h}",
            "-r",
            f"{max(fps, 1.0):.2f}",
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(out_path),
        ]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        assert proc.stdin is not None
        for f in frames_bgr:
            cropped = f[:even_h, :even_w]
            proc.stdin.write(cropped.tobytes())
        proc.stdin.close()
        ret = proc.wait(timeout=30)
        if ret == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path
    except (OSError, subprocess.SubprocessError):
        pass

    # Fallback to OpenCV VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, max(fps, 1.0), (even_w, even_h))
    for f in frames_bgr:
        writer.write(f[:even_h, :even_w])
    writer.release()
    return out_path


def load_clip_frames_and_timestamps(
    clip_dir: Path,
    roi: tuple[int, int, int, int] | None = None,
    max_width: int | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Load unmodified raw clip frames (T, H, W, 3) and timestamps (s), returning true achieved FPS."""
    clip_dir = Path(clip_dir).resolve()
    ts_path = clip_dir / "timestamps.csv"
    timestamps_ns: list[int] = []
    if ts_path.exists():
        with ts_path.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                timestamps_ns.append(int(row["timestamp_monotonic_ns"]))

    npz_path = clip_dir / "raw_frames.npz"
    if npz_path.exists():
        with np.load(npz_path) as data:
            frames = data["frames"]
    else:
        video_file = next(
            (
                clip_dir / name
                for name in ("raw.mkv", "raw.avi", "preview.mp4")
                if (clip_dir / name).exists()
            ),
            None,
        )
        if video_file is None:
            raise FileNotFoundError(f"No raw recording found in {clip_dir}")
        cap = cv2.VideoCapture(str(video_file))
        frame_list: list[np.ndarray] = []
        while True:
            ok, bgr = cap.read()
            if not ok or bgr is None:
                break
            frame_list.append(bgr)
        cap.release()
        frames = np.stack(frame_list, axis=0)

    if roi and len(roi) == 4:
        rx, ry, rw, rh = roi
        h, w = frames.shape[1:3]
        x0 = max(0, min(rx, w - 2))
        y0 = max(0, min(ry, h - 2))
        x1 = max(x0 + 2, min(x0 + rw, w))
        y1 = max(y0 + 2, min(y0 + rh, h))
        frames = frames[:, y0:y1, x0:x1, :]

    if max_width and frames.shape[2] > max_width:
        scale = max_width / float(frames.shape[2])
        new_h = max(8, round(frames.shape[1] * scale))
        resized = [
            cv2.resize(f, (max_width, new_h), interpolation=cv2.INTER_AREA) for f in frames
        ]
        frames = np.stack(resized, axis=0)

    n = len(frames)
    if len(timestamps_ns) >= 2:
        t_sec = (np.array(timestamps_ns[:n], dtype=np.float64) - timestamps_ns[0]) / 1e9
        dt = np.diff(t_sec)
        achieved_fps = float(1.0 / np.mean(dt)) if np.mean(dt) > 0 else 30.0
    else:
        meta_path = clip_dir / "meta.json"
        achieved_fps = 30.0
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            achieved_fps = float(meta.get("achieved_fps", 30.0))
        t_sec = np.arange(n, dtype=np.float64) / achieved_fps

    return frames, t_sec, achieved_fps


class RecordingService:
    """Manages control locking and lossless/near-lossless timestamped clip capture."""

    def __init__(
        self,
        layout: DataLayout,
        db: Database,
        camera_manager: CameraManager,
        preview_service: LivePreviewService | None = None,
    ) -> None:
        self.layout = layout
        self.db = db
        self.camera_manager = camera_manager
        self.preview_service = preview_service
        self._lock = threading.Lock()
        self.is_recording = False
        self.current_clip_id: str | None = None
        self.progress: float = 0.0

    def record_clip(
        self,
        title: str = "Vibration Capture",
        duration_s: float = 5.0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        storage_format: str = "mjpeg_q100",
        roi: tuple[int, int, int, int] | None = None,
        controls: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Lock camera controls, capture frames with nanosecond timestamps, and save losslessly."""
        with self._lock:
            if self.is_recording:
                raise RuntimeError("A recording is already in progress.")
            self.is_recording = True
            self.progress = 0.0

        try:
            cam = self.camera_manager.get_active()
            caps = self.camera_manager.get_capabilities(cam)
            ctrl_payload = dict(controls or {})
            ctrl_payload.update({"width": width, "height": height, "fps": fps})

            # Step 1: Lock exposure, gain, white balance, and focus before capture
            locked_controls = cam.lock_controls(ctrl_payload)
            if not cam.running or (
                cam.current_controls.width != width
                or cam.current_controls.height != height
                or cam.current_controls.fps != fps
            ):
                cam.start(width=width, height=height, fps=fps)

            timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            clip_id = f"clip_{timestamp_str}_{uuid.uuid4().hex[:6]}"
            self.current_clip_id = clip_id
            clip_dir = self.layout.clip_dir(clip_id)

            target_frames = max(4, round(duration_s * fps))
            frames_bgr: list[np.ndarray] = []
            mono_ns_list: list[int] = []
            wall_ns_list: list[int] = []

            for idx in range(target_frames):
                pkt = cam.get_frame(timeout=3.0)
                frame = pkt.bgr
                if roi and len(roi) == 4:
                    rx, ry, rw, rh = roi
                    fh, fw = frame.shape[:2]
                    x0 = max(0, min(rx, fw - 2))
                    y0 = max(0, min(ry, fh - 2))
                    x1 = max(x0 + 2, min(x0 + rw, fw))
                    y1 = max(y0 + 2, min(y0 + rh, fh))
                    frame = frame[y0:y1, x0:x1].copy()
                else:
                    frame = frame.copy()

                frames_bgr.append(frame)
                mono_ns_list.append(pkt.timestamp_monotonic_ns)
                wall_ns_list.append(pkt.timestamp_wall_ns)

                if self.preview_service is not None:
                    self.preview_service.record_roi_sample(pkt.bgr, pkt.timestamp_monotonic_ns)

                self.progress = round((idx + 1) / float(target_frames), 3)

            # Step 2: Save per-frame timestamps.csv and compute true achieved FPS
            ts_csv_path = clip_dir / "timestamps.csv"
            dts_ms: list[float] = []
            with ts_csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["frame_index", "timestamp_monotonic_ns", "timestamp_wall_ns", "dt_ms"]
                )
                for i, (m_ns, w_ns) in enumerate(zip(mono_ns_list, wall_ns_list, strict=True)):
                    dt_ms = (m_ns - mono_ns_list[i - 1]) / 1e6 if i > 0 else 0.0
                    if i > 0:
                        dts_ms.append(dt_ms)
                    writer.writerow([i, m_ns, w_ns, f"{dt_ms:.4f}"])

            total_elapsed_s = (
                (mono_ns_list[-1] - mono_ns_list[0]) / 1e9 if len(mono_ns_list) > 1 else duration_s
            )
            achieved_fps = (
                (len(frames_bgr) - 1) / total_elapsed_s
                if total_elapsed_s > 0 and len(frames_bgr) > 1
                else float(fps)
            )
            jitter_std_ms = float(np.std(dts_ms)) if dts_ms else 0.0

            # Step 3: Save lossless frame archive + video container + thumbnail
            frame_stack = np.stack(frames_bgr, axis=0)
            npz_path = clip_dir / "raw_frames.npz"
            if storage_format == "raw_npz":
                np.savez_compressed(npz_path, frames=frame_stack)
            else:
                np.savez(npz_path, frames=frame_stack)

            eff_h, eff_w = frame_stack.shape[1:3]
            raw_avi_path = clip_dir / "raw.avi"
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            vw = cv2.VideoWriter(str(raw_avi_path), fourcc, achieved_fps, (eff_w, eff_h))
            for f in frames_bgr:
                vw.write(f)
            vw.release()

            preview_mp4_path = clip_dir / "preview.mp4"
            encode_browser_mp4(frames_bgr, achieved_fps, preview_mp4_path)

            thumb_path = clip_dir / "thumb.jpg"
            cv2.imwrite(str(thumb_path), frames_bgr[len(frames_bgr) // 2])

            total_bytes = sum(f.stat().st_size for f in clip_dir.iterdir() if f.is_file())
            meta: dict[str, Any] = {
                "clip_id": clip_id,
                "title": title,
                "created_at": datetime.now(UTC).isoformat(),
                "camera_id": cam.camera_id,
                "camera_name": caps.name,
                "sensor_model": caps.sensor_model,
                "shutter_type": caps.shutter_type,
                "rolling_shutter_warning": caps.rolling_shutter_warning,
                "width": int(eff_w),
                "height": int(eff_h),
                "target_fps": float(fps),
                "achieved_fps": round(float(achieved_fps), 3),
                "jitter_std_ms": round(jitter_std_ms, 3),
                "nyquist_max_hz": round(float(achieved_fps) / 2.0, 2),
                "frame_count": len(frames_bgr),
                "duration_s": round(float(total_elapsed_s), 3),
                "storage_format": storage_format,
                "file_size_bytes": int(total_bytes),
                "roi": list(roi) if roi else None,
                "controls": locked_controls.model_dump(),
                "lens_info": caps.lens_info,
            }

            (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self.db.upsert_recording(meta)
            return meta
        finally:
            with self._lock:
                self.is_recording = False
                self.current_clip_id = None
                self.progress = 1.0
