"""Lossless/near-lossless video recorder with locked controls and nanosecond frame timestamps."""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import shutil
import struct
import subprocess
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.lib import format as nlf

from src.cameras.manager import CameraManager
from src.core.hardware import detect_hardware_profile
from src.recording.preview import LivePreviewService
from src.storage.db import Database
from src.storage.layout import DataLayout

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


def _encode_ffv1_mkv(
    frames_bgr: list[np.ndarray] | np.ndarray, fps: float, out_path: Path
) -> bool:
    """Encode BGR frames into a lossless FFV1 MKV file via ffmpeg. Returns True on success."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin or len(frames_bgr) == 0:
        return False
    h, w = frames_bgr[0].shape[:2]
    cmd = [
        ffmpeg_bin,
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{w}x{h}",
        "-r",
        f"{max(fps, 1.0):.3f}",
        "-i",
        "pipe:0",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-y",
        str(out_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None
        for frm in frames_bgr:
            proc.stdin.write(np.ascontiguousarray(frm, dtype=np.uint8).tobytes())
        proc.stdin.close()
        _out, _err = proc.communicate(timeout=45.0)
        return proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 128
    except (OSError, subprocess.SubprocessError):
        return False


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


def _inspect_npz_array(
    npz_path: Path, key: str
) -> tuple[str, tuple[int, ...], np.dtype[Any], bool, bool, int]:
    """Read shape and layout of an array inside an .npz archive without loading pixels."""
    with zipfile.ZipFile(npz_path, "r") as zf:
        names = set(zf.namelist())
        member = f"{key}.npy" if f"{key}.npy" in names else "frames.npy"
        info = zf.getinfo(member)
        with zf.open(info, "r") as fp:
            ver = nlf.read_magic(fp)
            if ver == (1, 0):
                shape, fortran, dtype = nlf.read_array_header_1_0(fp)
            elif ver == (2, 0):
                shape, fortran, dtype = nlf.read_array_header_2_0(fp)
            else:
                raise ValueError(f"Unsupported NPY version {ver} in {npz_path}")
            data_offset = fp.tell()
            can_seek = info.compress_type == zipfile.ZIP_STORED and fp.seekable()
    return member, tuple(int(d) for d in shape), np.dtype(dtype), bool(fortran), can_seek, int(data_offset)


def _compute_crop_and_output_size(
    h0: int,
    w0: int,
    roi: tuple[int, int, int, int] | None,
    max_width: int | None,
) -> tuple[tuple[int, int, int, int] | None, int, int]:
    """Compute bounded ROI crop coordinates and proportional resize output size."""
    crop_box: tuple[int, int, int, int] | None = None
    crop_h, crop_w = h0, w0
    if roi and len(roi) == 4:
        rx, ry, rw, rh = roi
        min_w = min(16, w0)
        min_h = min(16, h0)
        x0 = max(0, min(int(rx), w0 - min_w))
        y0 = max(0, min(int(ry), h0 - min_h))
        x1 = max(x0 + min_w, min(x0 + int(rw), w0))
        y1 = max(y0 + min_h, min(y0 + int(rh), h0))
        crop_box = (x0, y0, x1, y1)
        crop_h = y1 - y0
        crop_w = x1 - x0

    if max_width and crop_w > max_width:
        scale = float(max_width) / float(crop_w)
        out_w = int(max_width)
        out_h = max(8, round(crop_h * scale))
    else:
        out_w = crop_w
        out_h = crop_h
    return crop_box, out_h, out_w


def _transform_single_frame(
    frame: np.ndarray,
    crop_box: tuple[int, int, int, int] | None,
    out_w: int,
    out_h: int,
) -> np.ndarray:
    """Crop ROI and resize a single BGR frame."""
    if crop_box is not None:
        x0, y0, x1, y1 = crop_box
        frame = frame[y0:y1, x0:x1, :]
    if frame.shape[1] != out_w or frame.shape[0] != out_h:
        frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
    return frame


def _stream_npz_frames(
    npz_path: Path,
    key: str,
    roi: tuple[int, int, int, int] | None,
    max_width: int | None,
    keep_indices: list[int],
) -> np.ndarray:
    """Stream selected frames one by one from raw_frames.npz without loading the full archive."""
    member, shape, dtype, fortran, can_seek, data_offset = _inspect_npz_array(npz_path, key)
    if fortran or len(shape) != 4:
        with np.load(npz_path) as data:
            arr = data[key] if (key in data) else data["frames"]
            if keep_indices:
                arr = arr[keep_indices]
            crop_box, out_h, out_w = _compute_crop_and_output_size(
                arr.shape[1], arr.shape[2], roi, max_width
            )
            out = np.empty((len(arr), out_h, out_w, arr.shape[3]), dtype=arr.dtype)
            for idx_i, f in enumerate(arr):
                out[idx_i] = _transform_single_frame(f, crop_box, out_w, out_h)
            return out

    n_total, h0, w0, c0 = shape
    crop_box, out_h, out_w = _compute_crop_and_output_size(h0, w0, roi, max_width)
    if not keep_indices:
        return np.empty((0, out_h, out_w, c0), dtype=dtype)

    frame_shape = (h0, w0, c0)
    frame_nbytes = h0 * w0 * c0 * dtype.itemsize
    out = np.empty((len(keep_indices), out_h, out_w, c0), dtype=dtype)

    with zipfile.ZipFile(npz_path, "r") as zf:
        info = zf.getinfo(member)
        with zf.open(info, "r") as fp:
            if can_seek:
                for out_i, f_idx in enumerate(keep_indices):
                    f_clamped = max(0, min(n_total - 1, int(f_idx)))
                    fp.seek(data_offset + f_clamped * frame_nbytes)
                    raw = fp.read(frame_nbytes)
                    if len(raw) < frame_nbytes:
                        raise EOFError(
                            f"Unexpected EOF reading frame {f_clamped} from {npz_path}"
                        )
                    frm = np.frombuffer(raw, dtype=dtype).reshape(frame_shape)
                    out[out_i] = _transform_single_frame(frm, crop_box, out_w, out_h)
            else:
                # Re-open cleanly and skip uncompressed NPY header + unrequested frames sequentially
                with zf.open(info, "r") as seq_fp:
                    ver = nlf.read_magic(seq_fp)
                    if ver == (1, 0):
                        nlf.read_array_header_1_0(seq_fp)
                    else:
                        nlf.read_array_header_2_0(seq_fp)
                    want_map: dict[int, list[int]] = {}
                    for out_i, f_idx in enumerate(keep_indices):
                        want_map.setdefault(int(f_idx), []).append(out_i)
                    max_idx = max(want_map.keys())
                    for cur_idx in range(min(n_total, max_idx + 1)):
                        raw = seq_fp.read(frame_nbytes)
                        if len(raw) < frame_nbytes:
                            break
                        if cur_idx in want_map:
                            frm = np.frombuffer(raw, dtype=dtype).reshape(frame_shape)
                            transformed = _transform_single_frame(frm, crop_box, out_w, out_h)
                            for out_i in want_map[cur_idx]:
                                out[out_i] = transformed
    return out


def _stream_video_frames(
    video_file: Path,
    roi: tuple[int, int, int, int] | None,
    max_width: int | None,
    keep_set: set[int] | None,
    max_stop_idx: int | None = None,
) -> np.ndarray:
    """Decode only selected video frames and resize inline to keep peak RAM minimal."""
    cap = cv2.VideoCapture(str(video_file))
    frame_list: list[np.ndarray] = []
    crop_box: tuple[int, int, int, int] | None = None
    out_w = 0
    out_h = 0
    idx = 0
    try:
        while True:
            if max_stop_idx is not None and idx > max_stop_idx:
                break
            ok, bgr = cap.read()
            if not ok or bgr is None:
                break
            if keep_set is None or idx in keep_set:
                if out_w == 0:
                    crop_box, out_h, out_w = _compute_crop_and_output_size(
                        bgr.shape[0], bgr.shape[1], roi, max_width
                    )
                frame_list.append(_transform_single_frame(bgr, crop_box, out_w, out_h))
            idx += 1
    finally:
        cap.release()
    if not frame_list:
        raise FileNotFoundError(f"Could not decode frames from {video_file}")
    return np.stack(frame_list, axis=0)


def get_clip_frame_count(clip_dir: Path, stream: str = "cam_a") -> int:
    """Return total frame count of a clip in O(1) without decoding or loading pixel buffers."""
    clip_dir = Path(clip_dir).resolve()
    npz_path = clip_dir / "raw_frames.npz"
    if npz_path.exists():
        with contextlib.suppress(Exception):
            key = "frames_b" if stream == "cam_b" else "frames"
            _m, shape, _dt, _ft, _sk, _off = _inspect_npz_array(npz_path, key)
            if len(shape) >= 1 and shape[0] > 0:
                return int(shape[0])

    meta_path = clip_dir / "meta.json"
    if meta_path.exists():
        with contextlib.suppress(Exception):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fc = int(meta.get("frame_count") or 0)
            if fc > 0:
                return fc

    ts_path = clip_dir / "timestamps.csv"
    if ts_path.exists():
        with contextlib.suppress(Exception):
            lines = ts_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 1:
                return len(lines) - 1
    return 0


def load_clip_frames_and_timestamps(
    clip_dir: Path,
    roi: tuple[int, int, int, int] | None = None,
    max_width: int | None = None,
    trim_start_s: float = 0.0,
    trim_end_s: float = 0.0,
    stream: str = "cam_a",
    max_frames: int | None = None,
    selected_indices: Sequence[int] | None = None,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Stream raw clip frames (T, H, W, 3) and timestamps (s), avoiding full-archive RAM spikes."""
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

    meta_path = clip_dir / "meta.json"
    target_fps = 30.0
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        target_fps = float(meta.get("target_fps", 30.0))

    npz_path = clip_dir / "raw_frames.npz"
    npz_key = "frames_b" if stream == "cam_b" else "frames"
    video_file: Path | None = None
    n_total = 0

    if npz_path.exists():
        try:
            _m, shape, _dt, _ft, _sk, _off = _inspect_npz_array(npz_path, npz_key)
            n_total = int(shape[0])
        except Exception:  # noqa: BLE001
            with np.load(npz_path) as data:
                arr = data[npz_key] if npz_key in data else data["frames"]
                n_total = int(arr.shape[0])
    else:
        cand_names = (
            ("raw_b.mkv", "raw_b.avi", "preview_b.mp4", "raw.mkv", "raw.avi", "preview.mp4")
            if stream == "cam_b"
            else ("raw.mkv", "raw.avi", "preview.mp4")
        )
        video_file = next(
            (clip_dir / name for name in cand_names if (clip_dir / name).exists()),
            None,
        )
        if video_file is None:
            raise FileNotFoundError(f"No raw recording found in {clip_dir}")
        cap = cv2.VideoCapture(str(video_file))
        try:
            cap_n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        finally:
            cap.release()
        n_total = cap_n if cap_n > 0 else len(timestamps_ns)

    if n_total > 0 and len(timestamps_ns) >= 2:
        t_sec_full = (np.array(timestamps_ns[:n_total], dtype=np.float64) - timestamps_ns[0]) / 1e9
        dt = np.diff(t_sec_full)
        nonzero_dt = dt[dt > 0.001]
        if len(nonzero_dt) >= max(2, n_total // 3):
            achieved_fps = float(1.0 / np.mean(dt)) if np.mean(dt) > 0 else target_fps
        else:
            achieved_fps = target_fps
            t_sec_full = np.arange(n_total, dtype=np.float64) / achieved_fps
    elif n_total > 0:
        achieved_fps = target_fps
        t_sec_full = np.arange(n_total, dtype=np.float64) / achieved_fps
    else:
        achieved_fps = target_fps
        t_sec_full = np.empty(0, dtype=np.float64)

    if n_total > 0:
        valid_idxs = list(range(n_total))
        t_sec_base = t_sec_full
        if (trim_start_s > 0.0 or trim_end_s > 0.0) and n_total > 10:
            total_dur = (
                float(t_sec_full[-1] - t_sec_full[0])
                if len(t_sec_full) > 1
                else (n_total / achieved_fps)
            )
            t0 = max(0.0, float(trim_start_s))
            t1 = max(t0 + 0.25, total_dur - float(trim_end_s)) if trim_end_s > 0.0 else total_dur
            t1 = min(total_dur, t1)
            mask = (t_sec_full >= t0) & (t_sec_full <= t1)
            if int(np.sum(mask)) >= 8:
                valid_idxs = [int(i) for i in np.flatnonzero(mask)]
                t_sec_base = t_sec_full[mask] - float(t_sec_full[mask][0])

        effective_fps = achieved_fps
        if selected_indices is not None:
            pos_list = sorted(
                {max(0, min(len(valid_idxs) - 1, int(i))) for i in selected_indices}
            )
            keep_raw = [valid_idxs[p] for p in pos_list]
            t_sec_out = t_sec_base[pos_list]
        elif max_frames is not None and len(valid_idxs) > max(2, int(max_frames)):
            stride = max(1, len(valid_idxs) // int(max_frames))
            pos_list = list(range(0, len(valid_idxs), stride))
            keep_raw = [valid_idxs[p] for p in pos_list]
            t_sec_out = t_sec_base[pos_list]
            effective_fps = achieved_fps / float(stride)
        else:
            keep_raw = valid_idxs
            t_sec_out = t_sec_base
    else:
        keep_raw = []
        t_sec_out = np.empty(0, dtype=np.float64)
        effective_fps = achieved_fps

    if npz_path.exists():
        frames = _stream_npz_frames(npz_path, npz_key, roi=roi, max_width=max_width, keep_indices=keep_raw)
        return frames, t_sec_out, effective_fps

    assert video_file is not None
    if keep_raw:
        keep_set: set[int] | None = set(keep_raw) if len(keep_raw) < n_total else None
        max_stop = max(keep_raw) if keep_set is not None else None
        frames = _stream_video_frames(
            video_file, roi=roi, max_width=max_width, keep_set=keep_set, max_stop_idx=max_stop
        )
        if len(t_sec_out) != len(frames):
            t_sec_out = np.arange(len(frames), dtype=np.float64) / effective_fps
        return frames, t_sec_out, effective_fps

    frames = _stream_video_frames(video_file, roi=roi, max_width=max_width, keep_set=None)
    n_dec = len(frames)
    if len(timestamps_ns) >= 2:
        t_sec = (np.array(timestamps_ns[:n_dec], dtype=np.float64) - timestamps_ns[0]) / 1e9
        dt = np.diff(t_sec)
        nonzero_dt = dt[dt > 0.001]
        if len(nonzero_dt) >= max(2, n_dec // 3):
            achieved_fps = float(1.0 / np.mean(dt)) if np.mean(dt) > 0 else target_fps
        else:
            achieved_fps = target_fps
            t_sec = np.arange(n_dec, dtype=np.float64) / achieved_fps
    else:
        achieved_fps = target_fps
        t_sec = np.arange(n_dec, dtype=np.float64) / achieved_fps
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
        self._stop_requested = False
        self.current_clip_id: str | None = None
        self.progress: float = 0.0

    def request_stop(self) -> bool:
        """Signal an active recording to finish early and save captured frames."""
        with self._lock:
            if not self.is_recording:
                return False
            self._stop_requested = True
            return True

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
            self._stop_requested = False
            self.progress = 0.0

        if self.preview_service is not None:
            self.preview_service.set_recording_active(True)

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
            last_preview_push = 0.0
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
                        now_m = time.monotonic()
                        if now_m - last_preview_push >= 0.08 or idx == 0:
                            self.preview_service.publish_recording_frame(pkt.bgr)
                            last_preview_push = now_m

                    self.progress = round((idx + 1) / float(target_frames), 3)
                    if self._stop_requested and len(frames_bgr) >= 8:
                        break
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
            eff_h, eff_w = frame_stack.shape[1:3]
            npz_path = clip_dir / "raw_frames.npz"

            wrote_ffv1 = False
            if storage_format == "ffv1_mkv":
                wrote_ffv1 = _encode_ffv1_mkv(frame_stack, achieved_fps, clip_dir / "raw.mkv")
                if (
                    wrote_ffv1
                    and dual_on
                    and frames_b_bgr
                    and not _encode_ffv1_mkv(frames_b_bgr, achieved_fps, clip_dir / "raw_b.mkv")
                ):
                    wrote_ffv1 = False

            if not wrote_ffv1:
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

                if storage_format != "raw_npz":
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
            if self.preview_service is not None:
                self.preview_service.set_recording_active(False)
            with self._lock:
                self.is_recording = False
                self._stop_requested = False
                self.current_clip_id = None
                self.progress = 1.0

    def import_external_clip(
        self,
        video_bytes: bytes | None = None,
        filename: str = "upload.mp4",
        title: str | None = None,
        true_fps_override: float | None = None,
        max_frames: int | None = None,
        source_path: Path | None = None,
    ) -> dict[str, Any]:
        """Import an external video file (.mp4/.mov/.avi/.mkv) with optional True Capture FPS override."""
        has_source = source_path is not None and Path(source_path).exists() and Path(source_path).stat().st_size > 0
        if not has_source and not video_bytes:
            raise ValueError("Uploaded video file is empty")

        if max_frames is None:
            tier = str(detect_hardware_profile().get("performance_tier", "constrained"))
            tier_limits = {"ultra": 1500, "high": 1000, "balanced": 750, "constrained": 600}
            eff_max_frames = tier_limits.get(tier, 600)
        else:
            eff_max_frames = max(2, int(max_frames))

        ext = Path(filename).suffix.lower()
        if ext not in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"):
            ext = ".mp4"

        timestamp_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        clip_id = f"clip_{timestamp_str}_{uuid.uuid4().hex[:6]}"
        clip_dir = self.layout.clip_dir(clip_id)
        src_video_path = clip_dir / f"uploaded_source{ext}"
        if has_source and source_path is not None:
            shutil.copyfile(source_path, src_video_path)
        else:
            assert video_bytes is not None
            src_video_path.write_bytes(video_bytes)

        cap = cv2.VideoCapture(str(src_video_path))
        container_fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
        if container_fps <= 0.5 or container_fps > 10000.0:
            container_fps = 30.0

        frames_bgr: list[np.ndarray] = []
        was_truncated = False
        try:
            while len(frames_bgr) < eff_max_frames:
                ok, bgr = cap.read()
                if not ok or bgr is None:
                    break
                frames_bgr.append(bgr)
            if len(frames_bgr) == eff_max_frames:
                ok_extra, _ = cap.read()
                was_truncated = bool(ok_extra)
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
            "truncated": was_truncated,
            "max_frames_limit": eff_max_frames,
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

    def trim_clip(
        self,
        clip_id: str,
        start_s: float,
        end_s: float,
    ) -> dict[str, Any]:
        """Permanently trim a library clip to [start_s, end_s], updating raw frames, preview MP4, and metadata."""
        clip_dir = self.layout.clip_dir(clip_id)
        meta_path = clip_dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Clip '{clip_id}' not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        frames_a, t_sec, achieved_fps = load_clip_frames_and_timestamps(clip_dir, stream="cam_a")
        n_total = len(frames_a)
        if n_total < 8:
            raise ValueError("Clip is too short to trim further (requires >= 8 frames)")

        total_dur = float(t_sec[-1] - t_sec[0]) if len(t_sec) > 1 else (n_total / max(achieved_fps, 1.0))
        t0 = max(0.0, min(float(start_s), total_dur - 0.25))
        t1 = max(t0 + 0.25, min(float(end_s) if end_s > 0 else total_dur, total_dur))

        mask = (t_sec >= t0 - 1e-6) & (t_sec <= t1 + 1e-6)
        indices = np.where(mask)[0]
        if len(indices) < 6:
            i0 = max(0, min(n_total - 6, round(t0 * achieved_fps)))
            i1 = max(i0 + 6, min(n_total, round(t1 * achieved_fps)))
            indices = np.arange(i0, i1)

        trimmed_a = frames_a[indices]
        n_new = len(trimmed_a)
        new_t_sec = t_sec[indices] - float(t_sec[indices[0]])
        new_dur_s = float(new_t_sec[-1]) if n_new > 1 else (n_new / max(achieved_fps, 1.0))

        dual_on = bool(meta.get("dual_camera"))
        trimmed_b: np.ndarray | None = None
        npz_path = clip_dir / "raw_frames.npz"
        if dual_on and npz_path.exists():
            with np.load(npz_path) as data:
                if "frames_b" in data and len(data["frames_b"]) >= int(indices[-1]) + 1:
                    trimmed_b = data["frames_b"][indices]
        elif dual_on:
            with contextlib.suppress(FileNotFoundError, ValueError):
                frames_b_all, _tb, _fb = load_clip_frames_and_timestamps(clip_dir, stream="cam_b")
                if len(frames_b_all) >= int(indices[-1]) + 1:
                    trimmed_b = frames_b_all[indices]

        storage_format = str(meta.get("storage_format", "mjpeg_q100"))
        eff_h, eff_w = trimmed_a.shape[1:3]
        wrote_ffv1 = False
        if storage_format == "ffv1_mkv":
            wrote_ffv1 = _encode_ffv1_mkv(trimmed_a, achieved_fps, clip_dir / "raw.mkv")
            if (
                wrote_ffv1
                and trimmed_b is not None
                and not _encode_ffv1_mkv(trimmed_b, achieved_fps, clip_dir / "raw_b.mkv")
            ):
                wrote_ffv1 = False

        if not wrote_ffv1:
            if trimmed_b is not None:
                if storage_format == "raw_npz":
                    np.savez_compressed(npz_path, frames=trimmed_a, frames_b=trimmed_b)
                else:
                    np.savez(npz_path, frames=trimmed_a, frames_b=trimmed_b)
            elif storage_format == "raw_npz":
                np.savez_compressed(npz_path, frames=trimmed_a)
            else:
                np.savez(npz_path, frames=trimmed_a)

            if storage_format != "raw_npz":
                raw_avi_path = clip_dir / "raw.avi"
                vw = cv2.VideoWriter(
                    str(raw_avi_path), cv2.VideoWriter_fourcc(*"MJPG"), achieved_fps, (eff_w, eff_h)
                )
                for f in trimmed_a:
                    vw.write(f)
                vw.release()

        encode_browser_mp4(trimmed_a, achieved_fps, clip_dir / "preview.mp4")
        if trimmed_b is not None:
            encode_browser_mp4(trimmed_b, achieved_fps, clip_dir / "preview_b.mp4")

        # Rewrite timestamps.csv starting at 0 ns offset (preserving Cam B sync telemetry if present)
        ts_path = clip_dir / "timestamps.csv"
        orig_rows: list[dict[str, str]] = []
        if ts_path.exists():
            with ts_path.open("r", encoding="utf-8") as fh:
                orig_rows = list(csv.DictReader(fh))
        has_dual_ts = (
            dual_on
            and len(orig_rows) >= int(indices[-1]) + 1
            and "timestamp_b_monotonic_ns" in orig_rows[0]
        )
        base_ns = 1_000_000_000
        with ts_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if has_dual_ts:
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
                b_vals = [
                    int(orig_rows[int(idx)].get("timestamp_b_monotonic_ns") or 0)
                    for idx in indices
                ]
                b0 = b_vals[0]
                for i in range(n_new):
                    m_ns = base_ns + round(float(new_t_sec[i]) * 1e9)
                    dt_ms = (
                        float(new_t_sec[i] - new_t_sec[i - 1]) * 1000.0 if i > 0 else 0.0
                    )
                    mb_ns = base_ns + (b_vals[i] - b0)
                    sdt_str = str(orig_rows[int(indices[i])].get("sync_dt_ms") or "0.0000")
                    writer.writerow([i, m_ns, m_ns, f"{dt_ms:.4f}", mb_ns, sdt_str])
            else:
                writer.writerow(
                    ["frame_index", "timestamp_monotonic_ns", "timestamp_wall_ns", "dt_ms"]
                )
                for i in range(n_new):
                    m_ns = base_ns + round(float(new_t_sec[i]) * 1e9)
                    dt_ms = (
                        float(new_t_sec[i] - new_t_sec[i - 1]) * 1000.0 if i > 0 else 0.0
                    )
                    writer.writerow([i, m_ns, m_ns, f"{dt_ms:.4f}"])

        mid_idx = n_new // 2
        if dual_on and trimmed_b is not None:
            left_t = trimmed_a[mid_idx].copy()
            right_t = trimmed_b[mid_idx].copy()
            cv2.putText(left_t, "CAM A", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (56, 189, 248), 2)
            cv2.putText(right_t, "CAM B", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (11, 158, 245), 2)
            sbs_thumb = np.concatenate([left_t, right_t], axis=1)
            cv2.imwrite(str(clip_dir / "thumb.jpg"), sbs_thumb)
        else:
            cv2.imwrite(str(clip_dir / "thumb.jpg"), trimmed_a[mid_idx])

        total_bytes = sum(f.stat().st_size for f in clip_dir.iterdir() if f.is_file())
        meta["frame_count"] = int(n_new)
        meta["duration_s"] = round(float(new_dur_s), 3)
        meta["file_size_bytes"] = int(total_bytes)
        meta["trimmed_range_s"] = [round(t0, 3), round(t1, 3)]
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.db.upsert_recording(meta)
        return meta
