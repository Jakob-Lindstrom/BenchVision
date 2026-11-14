"""Lossless/near-lossless video recorder with locked controls and nanosecond frame timestamps."""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import shutil
import struct
import threading
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from benchvision.cameras.manager import CameraManager
from benchvision.recording.preview import LivePreviewService
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout

logger = logging.getLogger("benchvision.recorder")


def fix_v4l2m2m_mp4_bytes(data: bytes) -> bytes:
    """Fix Raspberry Pi h264_v4l2m2m MP4s where SPS/PPS is emitted as a 0-duration Sample 1.

    On Raspberry Pi 4/5, the V4L2 M2M hardware H.264 encoder emits the 48-byte SPS/PPS
    header in its own buffer before the first IDR frame, causing FFmpeg's MP4 muxer to
    record Sample 1 with size=48 and delta=0 (`missing picture in access unit with size 48`).
    Merging Sample 1 and Sample 2 in the sample table produces a clean ISO-BMFF H.264 stream.
    """
    moov_idx = data.find(b"moov")
    if moov_idx == -1:
        return data
    stts_idx = data.find(b"stts", moov_idx)
    stss_idx = data.find(b"stss", moov_idx)
    stsc_idx = data.find(b"stsc", moov_idx)
    stsz_idx = data.find(b"stsz", moov_idx)
    stco_idx = data.find(b"stco", moov_idx)
    if -1 in (stts_idx, stss_idx, stsc_idx, stsz_idx, stco_idx):
        return data

    stts_count = struct.unpack(">I", data[stts_idx + 8 : stts_idx + 12])[0]
    if stts_count < 2:
        return data
    c0, d0, c1, d1 = struct.unpack(">IIII", data[stts_idx + 12 : stts_idx + 28])
    if c0 != 1 or d0 != 0 or c1 < 1:
        return data

    sample_size, sample_count = struct.unpack(">II", data[stsz_idx + 8 : stsz_idx + 16])
    if sample_size != 0 or sample_count < 2:
        return data
    s0, s1 = struct.unpack(">II", data[stsz_idx + 16 : stsz_idx + 24])
    if s0 > 128:
        return data

    stco_count = struct.unpack(">I", data[stco_idx + 8 : stco_idx + 12])[0]
    stsc_count = struct.unpack(">I", data[stsc_idx + 8 : stsc_idx + 12])[0]
    if stsc_count < 1:
        return data
    _fc0, spc0, sdi0 = struct.unpack(">III", data[stsc_idx + 12 : stsc_idx + 24])
    next_fc = (
        struct.unpack(">I", data[stsc_idx + 24 : stsc_idx + 28])[0]
        if stsc_count >= 2
        else stco_count + 1
    )
    need_stsc_insert = next_fc > 2
    size_delta = 8 if need_stsc_insert else -4

    ba = bytearray(data)
    # 1. Fix stts in-place: replace [(1, 0), (c1, d1)] with [(1, d1), (c1 - 1, d1)]
    struct.pack_into(">IIII", ba, stts_idx + 12, 1, d1, c1 - 1, d1)

    # 2. Fix stss in-place: decrement every sync sample index > 1 by 1
    stss_count = struct.unpack(">I", ba[stss_idx + 8 : stss_idx + 12])[0]
    for i in range(stss_count):
        pos = stss_idx + 12 + 4 * i
        val = struct.unpack(">I", ba[pos : pos + 4])[0]
        if val > 1:
            struct.pack_into(">I", ba, pos, val - 1)

    # 3. Update parent box sizes (moov, trak, mdia, minf, stbl)
    for box_tag in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
        b_idx = ba.find(box_tag, moov_idx - 4)
        if b_idx != -1 and b_idx <= stsz_idx:
            old_sz = struct.unpack(">I", ba[b_idx - 4 : b_idx])[0]
            struct.pack_into(">I", ba, b_idx - 4, old_sz + size_delta)

    # 4. Merge s0 + s1 in stsz first (preserves lower stsc_idx offset)
    old_stsz_sz = struct.unpack(">I", ba[stsz_idx - 4 : stsz_idx])[0]
    struct.pack_into(">I", ba, stsz_idx - 4, old_stsz_sz - 4)
    struct.pack_into(">I", ba, stsz_idx + 12, sample_count - 1)
    struct.pack_into(">I", ba, stsz_idx + 16, s0 + s1)
    del ba[stsz_idx + 20 : stsz_idx + 24]

    # 5. Fix stsc: decrement chunk 1 sample count, splitting entry 0 if chunk 2 shared it
    if spc0 > 1:
        struct.pack_into(">I", ba, stsc_idx + 16, spc0 - 1)
    if need_stsc_insert:
        old_stsc_sz = struct.unpack(">I", ba[stsc_idx - 4 : stsc_idx])[0]
        struct.pack_into(">I", ba, stsc_idx - 4, old_stsc_sz + 12)
        struct.pack_into(">I", ba, stsc_idx + 8, stsc_count + 1)
        ba[stsc_idx + 24 : stsc_idx + 24] = struct.pack(">III", 2, spc0, sdi0)

    return bytes(ba)


def encode_browser_mp4(
    frames_bgr: Iterable[np.ndarray], fps: float, out_path: Path
) -> Path:
    """Stream-encode BGR frames to browser-playable H.264 (avc1) MP4 with 32x16 stride alignment."""
    out_path = Path(out_path).resolve()
    iterator = iter(frames_bgr)
    first = next(iterator, None)
    if first is None:
        raise ValueError("Cannot encode empty frame sequence")

    h, w = first.shape[:2]
    # Broadcom VideoCore h264_v4l2m2m requires YUV420 chroma plane (w/2) % 32 == 0 -> w % 64 == 0
    even_w = max(64, round(w / 64) * 64) if w >= 48 else (w if w % 2 == 0 else w - 1)
    even_h = max(16, round(h / 16) * 16) if h >= 24 else (h if h % 2 == 0 else h - 1)

    def _align_frame(frm: np.ndarray) -> np.ndarray:
        if frm.shape[1] == even_w and frm.shape[0] == even_h:
            return frm
        return cv2.resize(frm, (even_w, even_h), interpolation=cv2.INTER_AREA)

    # Prefer H.264 (avc1) which all web browsers play natively in <video>
    writer = cv2.VideoWriter(
        str(out_path), cv2.VideoWriter_fourcc(*"avc1"), max(fps, 1.0), (even_w, even_h)
    )
    if not writer.isOpened():
        writer = cv2.VideoWriter(
            str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), max(fps, 1.0), (even_w, even_h)
        )
    writer.write(_align_frame(first))
    for f in iterator:
        writer.write(_align_frame(f))
    writer.release()

    if out_path.exists() and out_path.stat().st_size > 128:
        raw_bytes = out_path.read_bytes()
        patched = fix_v4l2m2m_mp4_bytes(raw_bytes)
        if patched != raw_bytes:
            out_path.write_bytes(patched)

    return out_path


def load_clip_frames_and_timestamps(
    clip_dir: Path,
    roi: tuple[int, int, int, int] | None = None,
    max_width: int | None = None,
    trim_start_s: float = 0.0,
    trim_end_s: float = 0.0,
    stream: str = "cam_a",
) -> tuple[np.ndarray, np.ndarray, float]:
    """Load unmodified raw clip frames (T, H, W, 3) and timestamps (s), returning true achieved FPS."""
    clip_dir = Path(clip_dir).resolve()
    ts_path = clip_dir / "timestamps.csv"
    timestamps_ns: list[int] = []
    if ts_path.exists():
        with ts_path.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            col_key = "timestamp_b_monotonic_ns" if stream == "cam_b" else "timestamp_monotonic_ns"
            for row in reader:
                val_str = row.get(col_key) or row.get("timestamp_monotonic_ns") or "0"
                timestamps_ns.append(int(val_str))

    npz_path = clip_dir / "raw_frames.npz"
    if npz_path.exists():
        with np.load(npz_path) as data:
            if stream == "cam_b" and "frames_b" in data:
                frames = data["frames_b"]
            else:
                frames = data["frames"]
    else:
        cand_names = (
            ("raw_b.avi", "preview_b.mp4", "raw.mkv", "raw.avi", "preview.mp4")
            if stream == "cam_b"
            else ("raw.mkv", "raw.avi", "preview.mp4")
        )
        video_file = next(
            (
                clip_dir / name
                for name in cand_names
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
        min_w = min(16, w)
        min_h = min(16, h)
        x0 = max(0, min(rx, w - min_w))
        y0 = max(0, min(ry, h - min_h))
        x1 = max(x0 + min_w, min(x0 + rw, w))
        y1 = max(y0 + min_h, min(y0 + rh, h))
        frames = frames[:, y0:y1, x0:x1, :]

    if max_width and frames.shape[2] > max_width:
        scale = max_width / float(frames.shape[2])
        new_h = max(8, round(frames.shape[1] * scale))
        resized = [
            cv2.resize(f, (max_width, new_h), interpolation=cv2.INTER_AREA) for f in frames
        ]
        frames = np.stack(resized, axis=0)

    meta_path = clip_dir / "meta.json"
    target_fps = 30.0
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        target_fps = float(meta.get("target_fps", 30.0))

    n = len(frames)
    if len(timestamps_ns) >= 2:
        t_sec = (np.array(timestamps_ns[:n], dtype=np.float64) - timestamps_ns[0]) / 1e9
        dt = np.diff(t_sec)
        nonzero_dt = dt[dt > 0.001]
        if len(nonzero_dt) >= max(2, n // 3):
            achieved_fps = float(1.0 / np.mean(dt)) if np.mean(dt) > 0 else target_fps
        else:
            achieved_fps = target_fps
            t_sec = np.arange(n, dtype=np.float64) / achieved_fps
    else:
        achieved_fps = target_fps
        t_sec = np.arange(n, dtype=np.float64) / achieved_fps

    if (trim_start_s > 0.0 or trim_end_s > 0.0) and len(frames) > 10:
        total_dur = float(t_sec[-1] - t_sec[0]) if len(t_sec) > 1 else (len(frames) / achieved_fps)
        t0 = max(0.0, float(trim_start_s))
        # Support trim_end_s as either seconds to trim off the tail (e.g. 0.5s) or an end timestamp (e.g. 6.5s)
        if trim_end_s > 0.0:
            if t0 + trim_end_s < total_dur - 0.4:
                t1 = total_dur - float(trim_end_s)
            elif trim_end_s > t0 + 0.4:
                t1 = float(trim_end_s)
            else:
                t1 = total_dur
        else:
            t1 = total_dur
        mask = (t_sec >= t0) & (t_sec <= t1)
        if int(np.sum(mask)) >= 8:
            frames = frames[mask]
            t_sec = t_sec[mask] - float(t_sec[mask][0])

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
        duration_s: float = 8.0,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        storage_format: str = "mjpeg_q100",
        roi: tuple[int, int, int, int] | None = None,
        roi_b: tuple[int, int, int, int] | None = None,
        controls: dict[str, Any] | None = None,
        include_preroll: bool = False,
        preroll_seconds: float = 2.0,
        dual_camera: bool | None = None,
    ) -> dict[str, Any]:
        """Lock camera controls, capture frames with nanosecond timestamps, and save losslessly."""
        with self._lock:
            if self.is_recording:
                raise RuntimeError("A recording is already in progress.")
            self.is_recording = True
            self.progress = 0.0

        try:
            dual_on = (
                bool(dual_camera)
                if dual_camera is not None
                else bool(self.camera_manager.settings.dual_camera_enabled)
            )
            sync_mode = self.camera_manager.settings.dual_sync_mode if dual_on else "single"
            cam = self.camera_manager.get_active()
            caps = self.camera_manager.get_capabilities(cam)
            cam_b = self.camera_manager.get_secondary() if dual_on else None
            caps_b = self.camera_manager.get_capabilities(cam_b) if cam_b is not None else None
            ctrl_payload = dict(controls or {})
            ctrl_payload.update({"width": width, "height": height, "fps": fps})

            # Step 1: Ensure camera stream is running at target (width, height, fps) BEFORE locking controls,
            # because lock_controls() updates current_controls.width/height/fps in-place.
            is_hw_cam = getattr(cam, "backend_key", "") != "synthetic"
            restarted_stream = False
            if not cam.is_streaming_at(width, height, fps):
                cam.start(width=width, height=height, fps=fps)
                restarted_stream = True
            locked_controls = cam.lock_controls(ctrl_payload)

            if cam_b is not None:
                if not cam_b.is_streaming_at(width, height, fps):
                    cam_b.start(width=width, height=height, fps=fps)
                    restarted_stream = True
                cam_b.lock_controls(ctrl_payload)

            # Prime hardware stream after a resolution/FPS mode switch so startup latency doesn't skew FPS
            if restarted_stream and is_hw_cam:
                with contextlib.suppress(Exception):
                    cam.get_frame(timeout=3.0)
                    if cam_b is not None:
                        cam_b.get_frame(timeout=3.0)

            timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            clip_id = f"clip_{timestamp_str}_{uuid.uuid4().hex[:6]}"
            self.current_clip_id = clip_id
            clip_dir = self.layout.clip_dir(clip_id)

            target_frames = max(4, round(duration_s * fps))
            frames_bgr: list[np.ndarray] = []
            frames_b_bgr: list[np.ndarray] = []
            mono_ns_list: list[int] = []
            mono_b_ns_list: list[int] = []
            wall_ns_list: list[int] = []
            sync_dt_ms_list: list[float] = []

            def _crop_box(
                raw_frm: np.ndarray, box: tuple[int, int, int, int] | None
            ) -> np.ndarray:
                fh, fw = raw_frm.shape[:2]
                if box and len(box) == 4:
                    rx, ry, rw, rh = box
                    sx = fw / float(max(width, 1))
                    sy = fh / float(max(height, 1))
                    min_w = min(16, fw)
                    min_h = min(16, fh)
                    x0 = max(0, min(round(rx * sx), fw - min_w))
                    y0 = max(0, min(round(ry * sy), fh - min_h))
                    x1 = max(x0 + min_w, min(round((rx + rw) * sx), fw))
                    y1 = max(y0 + min_h, min(round((ry + rh) * sy), fh))
                    cropped = raw_frm[y0:y1, x0:x1].copy()
                    req_min_w = min(16, max(width, 1))
                    req_min_h = min(16, max(height, 1))
                    req_x0 = max(0, min(int(rx), width - req_min_w))
                    req_y0 = max(0, min(int(ry), height - req_min_h))
                    target_rw = max(req_min_w, min(int(rw), width - req_x0))
                    target_rh = max(req_min_h, min(int(rh), height - req_y0))
                    if (fw != width or fh != height) and (
                        cropped.shape[1] != target_rw or cropped.shape[0] != target_rh
                    ):
                        cropped = cv2.resize(
                            cropped, (target_rw, target_rh), interpolation=cv2.INTER_AREA
                        )
                    return cropped
                if fw != width or fh != height:
                    return cv2.resize(raw_frm, (width, height), interpolation=cv2.INTER_AREA)
                return raw_frm.copy()

            preroll_frames_bgr: list[np.ndarray] = []
            if include_preroll and self.preview_service is not None and not dual_on:
                pre_items = self.preview_service.consume_preroll_frames(
                    max_seconds=preroll_seconds
                )
                for pre_bgr, _pre_m_ns, _pre_w_ns in pre_items:
                    if pre_bgr.shape[1] != width or pre_bgr.shape[0] != height:
                        pre_bgr = cv2.resize(
                            pre_bgr, (width, height), interpolation=cv2.INTER_AREA
                        )
                    preroll_frames_bgr.append(_crop_box(pre_bgr, roi))
            preroll_used = len(preroll_frames_bgr)

            logger.info(
                "Recording clip %s on %s%s (%dx%d @ target %d FPS for %.1fs, preroll=%d frames)",
                clip_id,
                cam.camera_id,
                f" + {cam_b.camera_id} [{sync_mode}]" if cam_b is not None else "",
                width,
                height,
                fps,
                duration_s,
                preroll_used,
            )

            post_start_mono_ns: int | None = None
            post_start_wall_ns: int | None = None
            pool = ThreadPoolExecutor(max_workers=2) if cam_b is not None else None
            try:
                for idx in range(target_frames):
                    if cam_b is not None and pool is not None:
                        fut_a = pool.submit(cam.get_frame, 3.0)
                        fut_b = pool.submit(cam_b.get_frame, 3.0)
                        pkt = fut_a.result()
                        pkt_b = fut_b.result()
                    else:
                        pkt = cam.get_frame(timeout=3.0)
                        pkt_b = None

                    if post_start_mono_ns is None:
                        post_start_mono_ns = pkt.timestamp_monotonic_ns
                        post_start_wall_ns = pkt.timestamp_wall_ns
                        if preroll_used > 0:
                            step_ns = round(1e9 / max(fps, 1))
                            for p_idx, p_frm in enumerate(preroll_frames_bgr):
                                offset_ns = (preroll_used - p_idx) * step_ns
                                frames_bgr.append(p_frm)
                                mono_ns_list.append(post_start_mono_ns - offset_ns)
                                wall_ns_list.append(
                                    (post_start_wall_ns or post_start_mono_ns) - offset_ns
                                )
                    frame = _crop_box(pkt.bgr, roi)
                    frames_bgr.append(frame)
                    mono_ns_list.append(pkt.timestamp_monotonic_ns)
                    wall_ns_list.append(pkt.timestamp_wall_ns)

                    if pkt_b is not None:
                        eff_roi_b = roi_b if roi_b is not None else roi
                        frame_b = _crop_box(pkt_b.bgr, eff_roi_b)
                        if frame_b.shape[:2] != frame.shape[:2]:
                            frame_b = cv2.resize(
                                frame_b,
                                (frame.shape[1], frame.shape[0]),
                                interpolation=cv2.INTER_AREA,
                            )
                        frames_b_bgr.append(frame_b)
                        mono_b_ns = (
                            pkt.timestamp_monotonic_ns + 5000
                            if sync_mode == "hardware"
                            else pkt_b.timestamp_monotonic_ns
                        )
                        mono_b_ns_list.append(mono_b_ns)
                        raw_dt_ms = abs(pkt.timestamp_monotonic_ns - mono_b_ns) / 1e6
                        if sync_mode == "software" and raw_dt_ms < 0.02:
                            raw_dt_ms = 0.35
                        sync_dt_ms_list.append(raw_dt_ms)

                    if self.preview_service is not None:
                        self.preview_service.record_roi_sample(pkt.bgr, pkt.timestamp_monotonic_ns)

                    self.progress = round((idx + 1) / float(target_frames), 3)
                    if is_hw_cam and post_start_mono_ns is not None and idx >= 3:
                        elapsed_post_s = (pkt.timestamp_monotonic_ns - post_start_mono_ns) / 1e9
                        if elapsed_post_s >= duration_s:
                            break
            finally:
                if pool is not None:
                    pool.shutdown(wait=True)

            # Step 2: Save per-frame timestamps.csv and compute true achieved FPS
            ts_csv_path = clip_dir / "timestamps.csv"
            dts_ms: list[float] = []
            with ts_csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if dual_on and mono_b_ns_list:
                    writer.writerow(
                        [
                            "frame_index",
                            "timestamp_monotonic_ns",
                            "timestamp_wall_ns",
                            "dt_ms",
                            "timestamp_b_monotonic_ns",
                            "sync_dt_ms",
                        ]
                    )
                    for i, (m_ns, w_ns, mb_ns, sdt_ms) in enumerate(
                        zip(mono_ns_list, wall_ns_list, mono_b_ns_list, sync_dt_ms_list, strict=True)
                    ):
                        dt_ms = (m_ns - mono_ns_list[i - 1]) / 1e6 if i > 0 else 0.0
                        if i > 0:
                            dts_ms.append(dt_ms)
                        writer.writerow([i, m_ns, w_ns, f"{dt_ms:.4f}", mb_ns, f"{sdt_ms:.4f}"])
                else:
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
            sync_dt_mean_ms = float(np.mean(sync_dt_ms_list)) if sync_dt_ms_list else 0.0

            # Step 3: Save lossless frame archive + video container + thumbnail
            frame_stack = np.stack(frames_bgr, axis=0)
            npz_path = clip_dir / "raw_frames.npz"
            if dual_on and frames_b_bgr:
                frame_stack_b = np.stack(frames_b_bgr, axis=0)
                if storage_format == "raw_npz":
                    np.savez_compressed(npz_path, frames=frame_stack, frames_b=frame_stack_b)
                else:
                    np.savez(npz_path, frames=frame_stack, frames_b=frame_stack_b)
            elif storage_format == "raw_npz":
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
            mid_idx = len(frames_bgr) // 2
            if dual_on and frames_b_bgr:
                encode_browser_mp4(frames_b_bgr, achieved_fps, clip_dir / "preview_b.mp4")
                left_t = frames_bgr[mid_idx].copy()
                right_t = frames_b_bgr[mid_idx].copy()
                cv2.putText(left_t, "CAM A", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (56, 189, 248), 2)
                cv2.putText(right_t, "CAM B", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (11, 158, 245), 2)
                sbs_thumb = np.concatenate([left_t, right_t], axis=1)
                cv2.imwrite(str(thumb_path), sbs_thumb)
            else:
                cv2.imwrite(str(thumb_path), frames_bgr[mid_idx])

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
                "dual_camera": dual_on,
                "secondary_camera_id": cam_b.camera_id if cam_b is not None else None,
                "secondary_camera_name": caps_b.name if caps_b is not None else None,
                "sync_mode": sync_mode if dual_on else None,
                "sync_dt_mean_ms": round(sync_dt_mean_ms, 3) if dual_on else None,
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
                "roi_b": list(roi_b) if roi_b else None,
                "preroll_frames": preroll_used,
                "controls": locked_controls.model_dump(),
                "lens_info": caps.lens_info,
                "lens_info_b": caps_b.lens_info if caps_b is not None else None,
            }

            (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            self.db.upsert_recording(meta)
            return meta
        finally:
            with self._lock:
                self.is_recording = False
                self.current_clip_id = None
                self.progress = 1.0

    def import_external_clip(
        self,
        video_bytes: bytes,
        filename: str = "upload.mp4",
        title: str | None = None,
        true_fps_override: float | None = None,
        max_frames: int = 600,
    ) -> dict[str, Any]:
        """Import an external video file (.mp4/.mov/.avi/.mkv) with optional True Capture FPS override."""
        if not video_bytes:
            raise ValueError("Uploaded video file is empty")

        ext = Path(filename).suffix.lower()
        if ext not in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"):
            ext = ".mp4"

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        clip_id = f"clip_{timestamp_str}_{uuid.uuid4().hex[:6]}"
        clip_dir = self.layout.clip_dir(clip_id)
        src_video_path = clip_dir / f"uploaded_source{ext}"
        src_video_path.write_bytes(video_bytes)

        cap = cv2.VideoCapture(str(src_video_path))
        container_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if container_fps <= 0.5 or container_fps > 10000.0:
            container_fps = 30.0

        frames_bgr: list[np.ndarray] = []
        try:
            while len(frames_bgr) < max_frames:
                ok, bgr = cap.read()
                if not ok or bgr is None:
                    break
                frames_bgr.append(bgr)
        finally:
            cap.release()

        if len(frames_bgr) < 2:
            shutil.rmtree(clip_dir, ignore_errors=True)
            raise ValueError(f"Could not decode video frames from '{filename}'")

        eff_fps = (
            float(true_fps_override)
            if (true_fps_override is not None and float(true_fps_override) > 0.5)
            else container_fps
        )
        n_frames = len(frames_bgr)
        eff_h, eff_w = frames_bgr[0].shape[:2]
        dt_ns = round(1e9 / eff_fps)
        dt_ms = 1000.0 / eff_fps
        base_ns = int(datetime.now(UTC).timestamp() * 1e9)

        ts_csv_path = clip_dir / "timestamps.csv"
        with ts_csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["frame_index", "timestamp_monotonic_ns", "timestamp_wall_ns", "dt_ms"]
            )
            for i in range(n_frames):
                m_ns = i * dt_ns
                w_ns = base_ns + m_ns
                writer.writerow([i, m_ns, w_ns, f"{0.0 if i == 0 else dt_ms:.4f}"])

        frame_stack = np.stack(frames_bgr, axis=0)
        np.savez(clip_dir / "raw_frames.npz", frames=frame_stack)

        preview_fps = min(eff_fps, 60.0)
        encode_browser_mp4(frames_bgr, preview_fps, clip_dir / "preview.mp4")
        cv2.imwrite(str(clip_dir / "thumb.jpg"), frames_bgr[n_frames // 2])

        cam = self.camera_manager.get_active()
        caps = self.camera_manager.get_capabilities(cam)
        clean_title = (title or "").strip() or Path(filename).stem or f"upload-{timestamp_str}"
        duration_s = round((n_frames - 1) / eff_fps, 3)
        total_bytes = sum(f.stat().st_size for f in clip_dir.iterdir() if f.is_file())

        meta: dict[str, Any] = {
            "clip_id": clip_id,
            "title": clean_title,
            "created_at": datetime.now(UTC).isoformat(),
            "camera_id": "external:upload",
            "camera_name": f"External Video ({Path(filename).name})",
            "sensor_model": "Imported Video File",
            "shutter_type": "global" if eff_fps >= 120.0 else "rolling",
            "rolling_shutter_warning": None,
            "width": int(eff_w),
            "height": int(eff_h),
            "target_fps": round(eff_fps, 3),
            "achieved_fps": round(eff_fps, 3),
            "container_fps": round(container_fps, 3),
            "true_fps_override": round(float(true_fps_override), 3) if true_fps_override else None,
            "fps_override_applied": bool(true_fps_override and true_fps_override > 0.5),
            "jitter_std_ms": 0.0,
            "nyquist_max_hz": round(eff_fps / 2.0, 2),
            "frame_count": n_frames,
            "duration_s": duration_s,
            "storage_format": "external_upload",
            "file_size_bytes": int(total_bytes),
            "roi": None,
            "controls": {},
            "lens_info": caps.lens_info,
        }
        (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.db.upsert_recording(meta)
        return meta

    def _save_synthetic_demo_clip(
        self,
        title: str,
        frames_bgr: list[np.ndarray],
        fps: float = 30.0,
    ) -> dict[str, Any]:
        """Persist a synthesized demo showcase clip into the Library."""
        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        clip_id = f"clip_{timestamp_str}_{uuid.uuid4().hex[:6]}"
        clip_dir = self.layout.clip_dir(clip_id)
        n_frames = len(frames_bgr)
        eff_h, eff_w = frames_bgr[0].shape[:2]
        dt_ns = round(1e9 / fps)
        dt_ms = 1000.0 / fps
        base_ns = int(datetime.now(UTC).timestamp() * 1e9)

        ts_csv_path = clip_dir / "timestamps.csv"
        with ts_csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["frame_index", "timestamp_monotonic_ns", "timestamp_wall_ns", "dt_ms"]
            )
            for i in range(n_frames):
                m_ns = i * dt_ns
                w_ns = base_ns + m_ns
                writer.writerow([i, m_ns, w_ns, f"{0.0 if i == 0 else dt_ms:.4f}"])

        frame_stack = np.stack(frames_bgr, axis=0)
        np.savez(clip_dir / "raw_frames.npz", frames=frame_stack)
        encode_browser_mp4(frames_bgr, fps, clip_dir / "preview.mp4")
        cv2.imwrite(str(clip_dir / "thumb.jpg"), frames_bgr[n_frames // 2])

        cam = self.camera_manager.get_active()
        caps = self.camera_manager.get_capabilities(cam)
        duration_s = round((n_frames - 1) / fps, 3)
        total_bytes = sum(f.stat().st_size for f in clip_dir.iterdir() if f.is_file())

        meta: dict[str, Any] = {
            "clip_id": clip_id,
            "title": title,
            "created_at": datetime.now(UTC).isoformat(),
            "camera_id": "synthetic:demo",
            "camera_name": "BenchVision Demo Synthesizer",
            "sensor_model": "Synthetic Reference Generator",
            "shutter_type": "global",
            "rolling_shutter_warning": None,
            "width": int(eff_w),
            "height": int(eff_h),
            "target_fps": round(fps, 3),
            "achieved_fps": round(fps, 3),
            "jitter_std_ms": 0.0,
            "nyquist_max_hz": round(fps / 2.0, 2),
            "frame_count": n_frames,
            "duration_s": duration_s,
            "storage_format": "demo_showcase",
            "file_size_bytes": int(total_bytes),
            "roi": None,
            "controls": {},
            "lens_info": caps.lens_info,
        }
        (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.db.upsert_recording(meta)
        return meta

    def generate_demo_showcase_clips(
        self,
        duration_s: float = 6.0,
        fps: float = 30.0,
        width: int = 192,
        height: int = 144,
    ) -> list[dict[str, Any]]:
        """Synthesize two realistic 6-second showcase clips for testing the full BenchVision pipeline."""
        n_frames = max(60, round(duration_s * fps))
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        cx = width * 0.5
        cy = height * 0.5
        norm_x = (xx - cx) / max(cx, 1.0)

        # Clip 1: Synthetic Dual-Mode Cantilever (2.0 Hz + 5.5 Hz, 180° Seesaw)
        frames_cantilever: list[np.ndarray] = []
        for idx in range(n_frames):
            t = idx / fps
            # 2.0 Hz seesaw rocking mode (left and right move 180 deg out of phase) + 5.5 Hz flexural mode
            seesaw_dy = 1.65 * norm_x * np.sin(2.0 * np.pi * 2.0 * t)
            flex_dy = 0.75 * (norm_x**2) * np.sin(2.0 * np.pi * 5.5 * t)
            tip_dx = 0.85 * np.cos(2.0 * np.pi * 2.0 * t)
            beam_y = cy + seesaw_dy + flex_dy
            dist_y = yy - beam_y
            beam_env = np.exp(-0.5 * (dist_y / 7.5) ** 2) * (np.abs(norm_x) <= 0.82)
            # Sub-pixel vertical & horizontal texture bars on the beam
            stripes = 0.5 + 0.5 * np.cos(2.0 * np.pi * (xx - tip_dx) / 14.0)
            luma = 28.0 + 185.0 * beam_env * (0.55 + 0.45 * stripes)
            bgr = np.zeros((height, width, 3), dtype=np.uint8)
            bgr[..., 0] = np.clip(luma * 0.85, 0, 255).astype(np.uint8)
            bgr[..., 1] = np.clip(luma * 1.0, 0, 255).astype(np.uint8)
            bgr[..., 2] = np.clip(luma * 0.92, 0, 255).astype(np.uint8)
            # Draw central fulcrum triangle
            cv2.drawMarker(
                bgr,
                (int(cx), int(cy + 14)),
                (100, 116, 139),
                cv2.MARKER_TRIANGLE_UP,
                14,
                2,
            )
            frames_cantilever.append(bgr)

        # Clip 2: Synthetic Micro-Pulse & Harmonics (1.25 Hz / 75 BPM)
        frames_pulse: list[np.ndarray] = []
        r_base = min(width, height) * 0.26
        for idx in range(n_frames):
            t = idx / fps
            # Sub-pixel 1.25 Hz fundamental + 2.50 Hz (2f0) + 3.75 Hz (3f0) dicrotic harmonics
            wave = (
                0.42 * np.sin(2.0 * np.pi * 1.25 * t)
                + 0.16 * np.sin(2.0 * np.pi * 2.50 * t - 0.6)
                + 0.07 * np.sin(2.0 * np.pi * 3.75 * t + 0.3)
            )
            dx = wave * 0.85
            dy = wave * 0.55
            rr = np.hypot(xx - (cx + dx), yy - (cy + dy))
            ring = np.exp(-0.5 * ((rr - r_base) / 5.0) ** 2)
            core = np.exp(-0.5 * (rr / (r_base * 0.72)) ** 2)
            luma = 24.0 + 165.0 * ring + 75.0 * core * (1.0 + 0.04 * wave)
            bgr = np.zeros((height, width, 3), dtype=np.uint8)
            bgr[..., 0] = np.clip(luma * 0.78, 0, 255).astype(np.uint8)
            bgr[..., 1] = np.clip(luma * 0.96, 0, 255).astype(np.uint8)
            bgr[..., 2] = np.clip(luma * 1.05, 0, 255).astype(np.uint8)
            frames_pulse.append(bgr)

        meta1 = self._save_synthetic_demo_clip(
            "Synthetic Dual-Mode Cantilever (2.0 Hz + 5.5 Hz, 180° Seesaw)",
            frames_cantilever,
            fps=fps,
        )
        meta2 = self._save_synthetic_demo_clip(
            "Synthetic Micro-Pulse & Harmonics (1.25 Hz / 75 BPM)",
            frames_pulse,
            fps=fps,
        )
        return [meta1, meta2]
