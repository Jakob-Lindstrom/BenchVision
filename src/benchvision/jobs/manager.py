"""Background job queue with progress tracking, cancellation, and remote-worker readiness."""

from __future__ import annotations

import csv
import gc
import json
import logging
import queue
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from benchvision.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationOutput,
    estimate_processing_time_s,
)
from benchvision.recording.recorder import encode_browser_mp4, load_clip_frames_and_timestamps
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout

logger = logging.getLogger("benchvision.jobs")


def read_mem_available_bytes() -> int | None:
    """Return Linux MemAvailable + free swap headroom in bytes (None if unknown)."""
    try:
        info: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                info[key] = int(parts[0]) * 1024
        avail = info.get("MemAvailable")
        if avail is None:
            return None
        # Allow dipping into a quarter of free swap (zram/SD swap is slow, so be conservative)
        return avail + info.get("SwapFree", 0) // 4
    except (OSError, ValueError):
        return None


# Approximate peak working-set bytes per pixel per frame for each processing method
# (input frames + output frames + half-res motion energy + pyramid/flow buffers + export).
_METHOD_BYTES_PER_PX: dict[str, float] = {
    "eulerian": 13.0,
    "riesz": 11.0,
    "phase_steerable": 14.0,
    "motion_extraction": 9.0,
    "posy_rgb_trail": 8.0,
    "posy_peak_hold": 8.0,
    "optical_flow": 12.0,
    "rppg": 8.0,
}


def fit_frames_to_memory(
    frames_bgr: np.ndarray, method: str, avail_bytes: int | None, budget_frac: float = 0.70
) -> tuple[np.ndarray, float]:
    """Downscale frames spatially if the job would not fit in available RAM.

    Returns (frames, scale). scale == 1.0 means no change.
    """
    if avail_bytes is None or frames_bgr.size == 0:
        return frames_bgr, 1.0
    n, h, w, _ = frames_bgr.shape
    per_px = _METHOD_BYTES_PER_PX.get(method, 12.0)
    need = n * h * w * per_px
    budget = avail_bytes * budget_frac
    if need <= budget:
        return frames_bgr, 1.0
    scale = max(0.25, (budget / need) ** 0.5)
    new_w = max(64, int(w * scale) // 2 * 2)
    new_h = max(48, int(h * scale) // 2 * 2)
    logger.warning(
        "Job needs ~%.0f MB but only ~%.0f MB RAM is available; downscaling %dx%d -> %dx%d",
        need / 1e6,
        budget / 1e6,
        w,
        h,
        new_w,
        new_h,
    )
    small = np.empty((n, new_h, new_w, 3), dtype=np.uint8)
    for t in range(n):
        small[t] = cv2.resize(frames_bgr[t], (new_w, new_h), interpolation=cv2.INTER_AREA)
    return small, new_w / float(w)


def _energy_frame(motion_energy: np.ndarray, t: int, w: int, h: int) -> np.ndarray:
    """Upsample a (possibly half-resolution) motion-energy frame to (h, w)."""
    e = motion_energy[t]
    if e.shape[:2] == (h, w):
        return e
    return cv2.resize(e, (w, h), interpolation=cv2.INTER_LINEAR)


def _upsample_static(static: np.ndarray, w: int, h: int) -> np.ndarray:
    if static.shape[:2] == (h, w):
        return static
    return cv2.resize(static.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)


def compute_vibration_epicenter(
    motion_energy: np.ndarray,
    frame_shape: tuple[int, int] | None = None,
    fps: float = 30.0,
    low_hz: float = 0.5,
    high_hz: float = 10.0,
    source_shape: tuple[int, int] | None = None,
    roi: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Locate the (x, y) epicenter of maximum periodic vibration energy in source-frame pixels."""
    if motion_energy.size == 0 or motion_energy.ndim != 3:
        return {
            "x": 0,
            "y": 0,
            "proc_x": 0,
            "proc_y": 0,
            "rel_x": 0.5,
            "rel_y": 0.5,
            "peak_intensity": 0.0,
        }
    n_t, mh, mw = motion_energy.shape
    h, w = frame_shape if frame_shape else (mh, mw)
    mean_energy = np.mean(motion_energy, axis=0).astype(np.float32)

    if n_t >= 8:
        from benchvision.processing.filters import apply_temporal_bandpass

        bp_me = apply_temporal_bandpass(
            motion_energy, fps=fps, low_hz=low_hz, high_hz=high_hz
        )
        osc_std = np.std(bp_me, axis=0).astype(np.float32)
        combined_energy = 0.65 * osc_std + 0.35 * mean_energy
    else:
        combined_energy = mean_energy

    smoothed = cv2.GaussianBlur(combined_energy, (0, 0), max(1.2, 2.5 * mw / max(w, 1)))
    # Mask border margin (~8%) so pyramid/optical-flow edge padding never creates false corner peaks
    by = max(1, mh // 12)
    bx = max(1, mw // 12)
    if mh > 4 * by and mw > 4 * bx:
        smoothed[:by, :] = 0.0
        smoothed[-by:, :] = 0.0
        smoothed[:, :bx] = 0.0
        smoothed[:, -bx:] = 0.0
    flat_idx = int(np.argmax(smoothed))
    my, mx = divmod(flat_idx, mw)
    peak_val = float(smoothed[my, mx])
    rel_x = mx / float(max(mw - 1, 1))
    rel_y = my / float(max(mh - 1, 1))

    proc_x = round(rel_x * (w - 1))
    proc_y = round(rel_y * (h - 1))
    if roi and len(roi) == 4:
        rx, ry, rw, rh = roi
        src_x = int(rx + round(rel_x * max(1, rw - 1)))
        src_y = int(ry + round(rel_y * max(1, rh - 1)))
    elif source_shape:
        src_h, src_w = source_shape
        src_x = round(rel_x * max(1, src_w - 1))
        src_y = round(rel_y * max(1, src_h - 1))
    else:
        src_x = proc_x
        src_y = proc_y

    return {
        "x": int(src_x),
        "y": int(src_y),
        "proc_x": int(proc_x),
        "proc_y": int(proc_y),
        "rel_x": round(rel_x, 4),
        "rel_y": round(rel_y, 4),
        "peak_intensity": round(peak_val, 4),
    }


def stabilize_frames_subpixel(frames_bgr: np.ndarray) -> np.ndarray:
    """Cancel global camera/tripod translation shake using sub-pixel phase correlation."""
    n, h, w, _ = frames_bgr.shape
    if n < 2:
        return frames_bgr
    ref_gray = cv2.cvtColor(frames_bgr[0], cv2.COLOR_BGR2GRAY).astype(np.float32)
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    out = np.empty_like(frames_bgr)
    out[0] = frames_bgr[0]
    for t in range(1, n):
        cur_gray = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2GRAY).astype(np.float32)
        (dx, dy), _resp = cv2.phaseCorrelate(ref_gray, cur_gray, win)
        if abs(dx) > 0.02 or abs(dy) > 0.02:
            mat = np.float32([[1.0, 0.0, -dx], [0.0, 1.0, -dy]])
            out[t] = cv2.warpAffine(
                frames_bgr[t],
                mat,
                (w, h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
        else:
            out[t] = frames_bgr[t]
    return out


def apply_chroma_attenuation_inplace(
    orig_bgr: np.ndarray, mag_bgr: np.ndarray, chrom_attenuation: float
) -> None:
    """Scale Cr/Cb color magnification relative to luma magnification (0.0 = luma only, 1.0 = full color)."""
    ca = float(np.clip(chrom_attenuation, 0.0, 1.0))
    if abs(ca - 0.2) < 0.05:
        return
    for t in range(len(mag_bgr)):
        orig_ycrcb = cv2.cvtColor(orig_bgr[t], cv2.COLOR_BGR2YCrCb).astype(np.float32)
        mag_ycrcb = cv2.cvtColor(mag_bgr[t], cv2.COLOR_BGR2YCrCb).astype(np.float32)
        mag_ycrcb[..., 1:] = np.clip(
            orig_ycrcb[..., 1:] + ca * (mag_ycrcb[..., 1:] - orig_ycrcb[..., 1:]),
            0.0,
            255.0,
        )
        mag_bgr[t] = cv2.cvtColor(mag_ycrcb.astype(np.uint8), cv2.COLOR_YCrCb2BGR)


def apply_background_noise_gate_inplace(
    orig_bgr: np.ndarray,
    mag_bgr: np.ndarray,
    motion_energy: np.ndarray,
    threshold: float = 0.20,
) -> None:
    """Attenuate magnification on low-energy background pixels (SNR masking) to suppress static sensor grain."""
    if len(mag_bgr) == 0 or motion_energy.size == 0:
        return
    h, w = orig_bgr.shape[1:3]
    mean_energy = np.mean(motion_energy, axis=0) if motion_energy.ndim == 3 else motion_energy
    p99 = float(np.percentile(mean_energy, 99.0)) if float(np.max(mean_energy)) > 0 else 1.0
    norm_static = _upsample_static(np.clip(mean_energy / max(p99, 1e-6), 0.0, 1.0), w, h)
    gate = float(np.clip(threshold, 0.02, 0.90))
    mask = np.clip((norm_static - gate) / max(1.0 - gate, 1e-4), 0.0, 1.0)
    # Smoothly feather the SNR gate mask so foreground transitions have zero hard seams
    sigma = max(2.5, min(w, h) / 40.0)
    smooth_mask = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), sigma)[..., None]
    for t in range(len(mag_bgr)):
        blended = (
            orig_bgr[t].astype(np.float32) * (1.0 - smooth_mask)
            + mag_bgr[t].astype(np.float32) * smooth_mask
        )
        mag_bgr[t] = np.clip(blended, 0.0, 255.0).astype(np.uint8)


def _lzw_compress_gif8(indices: bytes) -> bytes:
    """Compress 8-bit palette indices into GIF89a LZW sub-blocks (min code size = 8)."""
    clear_code = 256
    eoi_code = 257
    next_code = 258
    code_size = 9
    max_code = 512
    table: dict[bytes, int] = {bytes([i]): i for i in range(256)}

    bit_buf = 0
    bit_cnt = 0
    raw_out = bytearray()

    def emit(code: int, size: int) -> None:
        nonlocal bit_buf, bit_cnt
        bit_buf |= code << bit_cnt
        bit_cnt += size
        while bit_cnt >= 8:
            raw_out.append(bit_buf & 0xFF)
            bit_buf >>= 8
            bit_cnt -= 8

    emit(clear_code, code_size)
    if not indices:
        emit(eoi_code, code_size)
    else:
        w_seq = bytes([indices[0]])
        for b in indices[1:]:
            k_seq = w_seq + bytes([b])
            if k_seq in table:
                w_seq = k_seq
            else:
                emit(table[w_seq], code_size)
                if next_code < 4096:
                    table[k_seq] = next_code
                    next_code += 1
                    if next_code > max_code and code_size < 12:
                        code_size += 1
                        max_code <<= 1
                else:
                    emit(clear_code, code_size)
                    table = {bytes([i]): i for i in range(256)}
                    next_code = 258
                    code_size = 9
                    max_code = 512
                w_seq = bytes([b])
        emit(table[w_seq], code_size)
        emit(eoi_code, code_size)

    if bit_cnt > 0:
        raw_out.append(bit_buf & 0xFF)

    blocks = bytearray([8])  # LZW minimum code size = 8
    pos = 0
    total = len(raw_out)
    while pos < total:
        chunk = raw_out[pos : pos + 255]
        blocks.append(len(chunk))
        blocks.extend(chunk)
        pos += 255
    blocks.append(0)  # Block terminator
    return bytes(blocks)


def encode_looping_gif(
    frames_bgr: list[np.ndarray] | np.ndarray,
    fps: float,
    out_path: Path,
    max_width: int = 360,
    max_frames: int = 64,
) -> Path:
    """Encode BGR frames into a palette-optimized looping GIF89a file with zero external dependencies."""
    import struct

    n_total = len(frames_bgr)
    if n_total == 0:
        raise ValueError("Cannot encode GIF from empty frame list")
    step = max(1, n_total // max(1, max_frames))
    sampled = [frames_bgr[i] for i in range(0, n_total, step)][:max_frames]
    eff_fps = max(1.0, float(fps) / float(step))
    delay_cs = max(2, round(100.0 / eff_fps))

    h0, w0 = sampled[0].shape[:2]
    if w0 > max_width:
        scale = max_width / float(w0)
        gw = max_width
        gh = max(16, round(h0 * scale))
    else:
        gw, gh = w0, h0

    # Build 256-entry RGB global color table (6x7x6 uniform perceptual cube = 252 + 4 padding)
    palette = bytearray(256 * 3)
    idx = 0
    for r_i in range(6):
        r_v = round(r_i * 255 / 5)
        for g_i in range(7):
            g_v = round(g_i * 255 / 6)
            for b_i in range(6):
                b_v = round(b_i * 255 / 5)
                palette[idx * 3 : idx * 3 + 3] = bytes((r_v, g_v, b_v))
                idx += 1

    gif = bytearray()
    gif.extend(b"GIF89a")
    gif.extend(struct.pack("<HHBBB", gw, gh, 0xF7, 0, 0))
    gif.extend(palette)
    # NETSCAPE2.0 infinite loop application extension
    gif.extend(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")

    for frm in sampled:
        if frm.shape[1] != gw or frm.shape[0] != gh:
            frm = cv2.resize(frm, (gw, gh), interpolation=cv2.INTER_AREA)
        b_ch = (frm[..., 0].astype(np.uint16) * 5 + 127) // 255
        g_ch = (frm[..., 1].astype(np.uint16) * 6 + 127) // 255
        r_ch = (frm[..., 2].astype(np.uint16) * 5 + 127) // 255
        pal_indices = (r_ch * 42 + g_ch * 6 + b_ch).astype(np.uint8).tobytes()

        # Graphic Control Extension
        gif.extend(struct.pack("<BBBBHB", 0x21, 0xF9, 4, 0x00, delay_cs, 0))
        gif.append(0x00)
        # Image Descriptor
        gif.extend(struct.pack("<BHHHHB", 0x2C, 0, 0, gw, gh, 0x00))
        gif.extend(_lzw_compress_gif8(pal_indices))

    gif.append(0x3B)  # GIF Trailer
    out_path = Path(out_path)
    out_path.write_bytes(bytes(gif))
    return out_path


def render_heatmap_frames(
    orig_bgr: np.ndarray,
    motion_energy: np.ndarray,
    alpha: float = 0.55,
    threshold: float = 0.20,
) -> Iterator[np.ndarray]:
    """Stream normalized [0, 1] spatial motion energy as an INFERNO heatmap with noise gate threshold."""
    h, w = orig_bgr.shape[1:3]
    mean_energy = np.mean(motion_energy, axis=0)
    p99 = float(np.percentile(mean_energy, 99.0)) if float(np.max(mean_energy)) > 0 else 1.0
    norm_static = _upsample_static(np.clip(mean_energy / max(p99, 1e-6), 0.0, 1.0), w, h)
    gate = float(np.clip(threshold, 0.0, 0.95))
    denom = max(1.0 - gate, 1e-4)

    for t in range(len(orig_bgr)):
        instant = _energy_frame(motion_energy, t, w, h)
        combined = np.clip(0.65 * instant + 0.35 * norm_static, 0.0, 1.0)
        gated = np.clip((combined - gate) / denom, 0.0, 1.0)
        heat_u8 = (gated * 255.0).astype(np.uint8)
        cmap = cv2.applyColorMap(heat_u8, cv2.COLORMAP_INFERNO)
        mask = (gated > 0.02)[..., None]
        blended = cv2.addWeighted(orig_bgr[t], 1.0 - alpha, cmap, alpha, 0.0)
        yield np.where(mask, blended, orig_bgr[t])


def render_phase_ripple_frames(
    orig_bgr: np.ndarray,
    motion_energy: np.ndarray,
    threshold: float = 0.20,
) -> Iterator[np.ndarray]:
    """Stream instantaneous phase-wave ripple map (-pi to +pi) radiating from the epicenter."""
    n, h, w, _ = orig_bgr.shape
    # Quarter resolution keeps the three (T, h/4, w/4) float32 buffers small on a 2 GB Pi 4
    sub_w = max(16, w // 4)
    sub_h = max(16, h // 4)
    osc = np.empty((n, sub_h, sub_w), dtype=np.float32)
    for t in range(n):
        gray = cv2.cvtColor(orig_bgr[t], cv2.COLOR_BGR2GRAY)
        osc[t] = cv2.resize(gray, (sub_w, sub_h), interpolation=cv2.INTER_AREA)

    osc -= np.mean(osc, axis=0, keepdims=True)
    d_osc = np.gradient(osc, axis=0).astype(np.float32, copy=False)
    std_osc = float(np.std(osc)) + 1e-6
    std_d = float(np.std(d_osc)) + 1e-6

    mean_energy = np.mean(motion_energy, axis=0)
    p99 = float(np.percentile(mean_energy, 99.0)) if float(np.max(mean_energy)) > 0 else 1.0
    norm_static = _upsample_static(np.clip(mean_energy / max(p99, 1e-6), 0.0, 1.0), w, h)
    gate = float(np.clip(threshold, 0.0, 0.90))

    for t in range(n):
        phase = np.arctan2(d_osc[t] / std_d, osc[t] / std_osc)  # [-pi, +pi]
        hue_u8 = ((phase + np.pi) * (255.0 / (2.0 * np.pi))).astype(np.uint8)
        hue_full = cv2.resize(hue_u8, (w, h), interpolation=cv2.INTER_LINEAR)
        ripple_bgr = cv2.applyColorMap(hue_full, cv2.COLORMAP_HSV)
        act = np.clip(0.6 * _energy_frame(motion_energy, t, w, h) + 0.4 * norm_static, 0.0, 1.0)
        mask = (act > gate * 0.65)[..., None]
        blended = cv2.addWeighted(orig_bgr[t], 0.45, ripple_bgr, 0.55, 0.0)
        yield np.where(mask, blended, (orig_bgr[t].astype(np.uint16) * 160 // 255).astype(np.uint8))


def render_quiver_frames(
    orig_bgr: np.ndarray,
    epicenter: dict[str, Any],
    step: int = 20,
) -> Iterator[np.ndarray]:
    """Stream optical-flow directional quiver arrows over the video without duplicate baked-in crosshair."""
    n, h, w, _ = orig_bgr.shape
    sub_w = max(32, w // 2)
    sub_h = max(24, h // 2)
    ref_gray = cv2.resize(
        cv2.cvtColor(orig_bgr[0], cv2.COLOR_BGR2GRAY), (sub_w, sub_h), interpolation=cv2.INTER_AREA
    )
    _ = epicenter

    for t in range(n):
        cur_gray = cv2.resize(
            cv2.cvtColor(orig_bgr[t], cv2.COLOR_BGR2GRAY), (sub_w, sub_h), interpolation=cv2.INTER_AREA
        )
        flow = cv2.calcOpticalFlowFarneback(
            ref_gray, cur_gray, None, 0.5, 2, 13, 2, 5, 1.1, 0
        )
        canvas = orig_bgr[t].copy()
        for y in range(step // 2, h, step):
            sy = min(sub_h - 1, y // 2)
            for x in range(step // 2, w, step):
                sx = min(sub_w - 1, x // 2)
                dx, dy = float(flow[sy, sx, 0]) * 2.0, float(flow[sy, sx, 1]) * 2.0
                mag = (dx * dx + dy * dy) ** 0.5
                if mag > 0.04:
                    end_x = int(x + float(np.clip(dx * 14.0, -step * 1.4, step * 1.4)))
                    end_y = int(y + float(np.clip(dy * 14.0, -step * 1.4, step * 1.4)))
                    cv2.arrowedLine(
                        canvas,
                        (x, y),
                        (end_x, end_y),
                        (56, 189, 248) if mag < 0.35 else (34, 197, 94),
                        1,
                        tipLength=0.32,
                    )
        yield canvas


def overlay_dual_roi_boxes_on_bgr(
    vis_bgr: np.ndarray,
    dual_roi: dict[str, Any] | None = None,
    src_shape: tuple[int, int] | None = None,
) -> np.ndarray:
    """Draw Probe A (cyan) and Probe B (amber) rectangles with 'PROBE A' / 'PROBE B' badges onto a BGR image."""
    out = vis_bgr.copy()
    if not dual_roi:
        return out
    h, w = out.shape[:2]
    sx = float(w) / float(src_shape[1]) if src_shape and src_shape[1] > 0 else 1.0
    sy = float(h) / float(src_shape[0]) if src_shape and src_shape[0] > 0 else 1.0

    for box_key, color, lbl in (
        ("roi_a", (248, 189, 56), "PROBE A"),
        ("roi_b", (11, 158, 245), "PROBE B"),
    ):
        box = dual_roi.get(box_key)
        if box and len(box) == 4:
            bx = max(0, min(w - 4, round(float(box[0]) * sx)))
            by = max(0, min(h - 4, round(float(box[1]) * sy)))
            bw = max(4, min(w - bx, round(float(box[2]) * sx)))
            bh = max(4, min(h - by, round(float(box[3]) * sy)))
            cv2.rectangle(out, (bx, by), (bx + bw, by + bh), color, 2)
            badge_w = min(w - bx, 72)
            badge_h = min(h - by, 18)
            cv2.rectangle(out, (bx, by), (bx + badge_w, by + badge_h), color, -1)
            cv2.putText(
                out,
                lbl,
                (bx + 4, by + 13),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (15, 23, 42),
                1,
                cv2.LINE_AA,
            )
    return out


def render_diagnostic_report_snapshot(
    frame_bgr: np.ndarray,
    motion_energy: np.ndarray,
    epicenter: dict[str, Any],
    dual_roi: dict[str, Any] | None = None,
    threshold: float = 0.20,
) -> np.ndarray:
    """Render a standalone annotated visual evidence frame with heatmap, epicenter bullseye, and ROI boxes."""
    h, w = frame_bgr.shape[:2]
    mean_energy = np.mean(motion_energy, axis=0) if motion_energy.ndim == 3 else motion_energy
    p99 = float(np.percentile(mean_energy, 99.0)) if float(np.max(mean_energy)) > 0 else 1.0
    norm_static = _upsample_static(np.clip(mean_energy / max(p99, 1e-6), 0.0, 1.0), w, h)
    gate = float(np.clip(threshold, 0.0, 0.90))
    gated = np.clip((norm_static - gate) / max(1.0 - gate, 1e-4), 0.0, 1.0)
    cmap = cv2.applyColorMap((gated * 255.0).astype(np.uint8), cv2.COLORMAP_INFERNO)
    mask = (gated > 0.02)[..., None]
    blended = cv2.addWeighted(frame_bgr, 0.45, cmap, 0.55, 0.0)
    vis = np.where(mask, blended, frame_bgr).copy()

    px = int(epicenter.get("proc_x", round(float(epicenter.get("rel_x", 0.5)) * (w - 1))))
    py = int(epicenter.get("proc_y", round(float(epicenter.get("rel_y", 0.5)) * (h - 1))))
    disp_x = int(epicenter.get("x", px))
    disp_y = int(epicenter.get("y", py))
    cv2.circle(vis, (px, py), 14, (68, 68, 239), 2)
    cv2.drawMarker(vis, (px, py), (68, 68, 239), cv2.MARKER_CROSS, 22, 2)
    cv2.putText(
        vis,
        f"EPICENTER ({disp_x},{disp_y})",
        (max(6, min(w - 165, px - 60)), max(18, py - 18)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (255, 255, 255),
        2,
    )
    if dual_roi:
        vis = overlay_dual_roi_boxes_on_bgr(vis, dual_roi)
    return vis


def render_side_by_side_frames(
    orig_bgr: np.ndarray,
    mag_bgr: np.ndarray,
    max_panel_width: int = 480,
    left_label: str = "Original",
    right_label: str = "Magnified",
) -> Iterator[np.ndarray]:
    """Stream side-by-side Original vs. Magnified (or Cam A vs. Cam B) frames with 64x16 H.264 stride alignment."""
    h, w = orig_bgr.shape[1:3]
    scale = min(1.0, max_panel_width / float(w))
    raw_pw = max(32, round(w * scale))
    raw_ph = max(16, round(h * scale))
    pw = max(32, round(raw_pw / 32) * 32) if raw_pw >= 24 else raw_pw
    ph = max(16, round(raw_ph / 16) * 16) if raw_ph >= 24 else raw_ph
    n_t = min(len(orig_bgr), len(mag_bgr))

    for t in range(n_t):
        left = (
            cv2.resize(orig_bgr[t], (pw, ph), interpolation=cv2.INTER_AREA)
            if (pw != w or ph != h)
            else orig_bgr[t].copy()
        )
        right = (
            cv2.resize(mag_bgr[t], (pw, ph), interpolation=cv2.INTER_AREA)
            if (pw != mag_bgr.shape[2] or ph != mag_bgr.shape[1])
            else mag_bgr[t].copy()
        )
        left[:, -1:] = 60
        right[:, :1] = 60
        cv2.putText(
            left, left_label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (240, 240, 240), 2
        )
        cv2.putText(
            right, right_label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (56, 189, 248), 2
        )
        yield np.concatenate([left, right], axis=1)


def write_signal_csv(
    csv_path: Path,
    timestamps_s: np.ndarray,
    raw_signal: np.ndarray,
    filtered_signal: np.ndarray,
) -> Path:
    """Export extracted ROI raw and bandpass-filtered temporal signals to CSV."""
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frame_index", "time_s", "raw_signal", "filtered_signal"])
        for i in range(len(timestamps_s)):
            writer.writerow(
                [
                    i,
                    f"{timestamps_s[i]:.5f}",
                    f"{raw_signal[i]:.5f}",
                    f"{filtered_signal[i]:.5f}",
                ]
            )
    return csv_path


class JobQueueManager:
    """Manages offline processing jobs with a background worker queue and cancel support."""

    def __init__(self, layout: DataLayout, db: Database, start_worker: bool = True) -> None:
        self.layout = layout
        self.db = db
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancelled_ids: set[str] = set()
        self._lock = threading.Lock()
        self._running = True
        self._recover_interrupted_jobs()
        self._worker_thread: threading.Thread | None = None
        if start_worker:
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _recover_interrupted_jobs(self) -> None:
        """Mark stale 'running' or 'queued' jobs from a prior server run as failed."""
        for job in self.db.list_jobs():
            if job["status"] in ("running", "queued"):
                job["status"] = "failed"
                job["error_message"] = "Interrupted by server restart"
                job["completed_at"] = datetime.now(UTC).isoformat()
                self.db.upsert_job(job)

    def submit_job(
        self, clip_id: str, params: JobParams, run_async: bool = True
    ) -> dict[str, Any]:
        """Create and enqueue a new offline magnification job for an existing recording."""
        clip = self.db.get_recording(clip_id)
        if clip is None:
            raise ValueError(f"Recording clip '{clip_id}' not found")

        method_cls = PROCESSING_REGISTRY.get(params.method)
        est_s = estimate_processing_time_s(
            frame_count=int(clip["frame_count"]),
            width=int(params.max_width or clip["width"]),
            height=int(clip["height"]),
            method=params.method,
        )

        job_id = f"job_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        job_doc: dict[str, Any] = {
            "job_id": job_id,
            "clip_id": clip_id,
            "method": method_cls.method_key,
            "status": "queued",
            "progress": 0.0,
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "params": params.model_dump(),
            "result": {"estimated_seconds": est_s},
            "error_message": None,
        }
        self.db.upsert_job(job_doc)

        if run_async:
            self._queue.put(job_id)
            return job_doc
        return self.execute_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        """Mark a queued or running job as cancelled."""
        with self._lock:
            self._cancelled_ids.add(job_id)
        job = self.db.get_job(job_id)
        if job and job["status"] in ("queued", "running"):
            job["status"] = "cancelled"
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
        return job

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled_ids

    def _worker_loop(self) -> None:
        while self._running:
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self.execute_job(job_id)
            except Exception:
                logger.exception("Unhandled error in worker for job %s", job_id)
            finally:
                self._queue.task_done()

    def execute_job(self, job_id: str) -> dict[str, Any]:
        """Run a single magnification job to completion, saving outputs next to the clip."""
        job = self.db.get_job(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")
        if self.is_cancelled(job_id) or job["status"] == "cancelled":
            job["status"] = "cancelled"
            self.db.upsert_job(job)
            return job

        clip_id = job["clip_id"]
        params = JobParams(**job["params"])
        clip_dir = self.layout.clip_dir(clip_id)
        res_dir = self.layout.job_result_dir(clip_id, job_id)

        job["status"] = "running"
        job["started_at"] = datetime.now(UTC).isoformat()
        job["progress"] = 0.02
        self.db.upsert_job(job)
        logger.info("Starting job %s (%s) on clip %s", job_id, params.method, clip_id)

        def _progress(val: float) -> bool:
            if self.is_cancelled(job_id):
                return False
            job["progress"] = round(float(val), 3)
            self.db.upsert_job(job)
            return True

        try:
            clip_meta_path = clip_dir / "meta.json"
            clip_meta: dict[str, Any] = {}
            if clip_meta_path.exists():
                clip_meta = json.loads(clip_meta_path.read_text(encoding="utf-8"))
            is_dual_clip = bool(clip_meta.get("dual_camera"))
            target_stream = params.target_stream if is_dual_clip else "cam_a"
            primary_stream_key = "cam_b" if target_stream == "cam_b" else "cam_a"

            frames_bgr, timestamps_s, achieved_fps = load_clip_frames_and_timestamps(
                clip_dir,
                roi=params.roi,
                max_width=params.max_width,
                trim_start_s=params.trim_start_s,
                trim_end_s=params.trim_end_s,
                stream=primary_stream_key,
            )

            if params.stabilize_subpixel:
                frames_bgr = stabilize_frames_subpixel(frames_bgr)

            # Protect the server from the Linux OOM killer on 2 GB Pi 4 boards
            frames_bgr, mem_scale = fit_frames_to_memory(
                frames_bgr, params.method, read_mem_available_bytes()
            )
            gc.collect()

            # Handle "auto_find" preset by discovering the dominant spectral peak first
            if params.preset_id == "auto_find":
                from benchvision.analysis.spectrum import compute_roi_spectrum_analysis

                auto_spec = compute_roi_spectrum_analysis(
                    frames_bgr=frames_bgr,
                    fps=achieved_fps,
                    low_hz=0.2,
                    high_hz=min(12.0, 0.45 * achieved_fps),
                )
                suggested = auto_spec["suggested_band"]
                params.low_hz = suggested["low_hz"]
                params.high_hz = suggested["high_hz"]

            method_cls = PROCESSING_REGISTRY.get(params.method)
            processor = method_cls()
            out: MagnificationOutput = processor.process(
                frames_bgr=frames_bgr,
                timestamps_s=timestamps_s,
                fps=achieved_fps,
                params=params,
                progress_cb=_progress,
            )

            if self.is_cancelled(job_id):
                raise JobCancelledError("Cancelled before output export")

            apply_chroma_attenuation_inplace(
                frames_bgr, out.magnified_bgr, params.chrom_attenuation
            )
            if params.gate_background_noise:
                apply_background_noise_gate_inplace(
                    frames_bgr,
                    out.magnified_bgr,
                    out.motion_energy,
                    threshold=params.heatmap_threshold,
                )
            src_w = int(clip_meta.get("width") or frames_bgr.shape[2])
            src_h = int(clip_meta.get("height") or frames_bgr.shape[1])

            epicenter = compute_vibration_epicenter(
                out.motion_energy,
                frame_shape=frames_bgr.shape[1:3],
                fps=achieved_fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                source_shape=(src_h, src_w),
                roi=params.roi,
            )
            out.extra_metrics["epicenter"] = epicenter

            # Save parameters
            (res_dir / "params.json").write_text(
                json.dumps(params.model_dump(), indent=2), encoding="utf-8"
            )

            # Save lossless AVI + shareable MP4 + side-by-side MP4 + heatmap MP4 + ripple & quiver MP4s
            n_frames_out = len(out.magnified_bgr)
            h, w = out.magnified_bgr.shape[1:3]
            lossless_path = res_dir / "magnified_lossless.avi"
            vw = cv2.VideoWriter(
                str(lossless_path), cv2.VideoWriter_fourcc(*"MJPG"), achieved_fps, (w, h)
            )
            for f in out.magnified_bgr:
                vw.write(f)
            vw.release()

            orig_mp4 = encode_browser_mp4(
                frames_bgr, achieved_fps, res_dir / "original_share.mp4"
            )
            share_mp4 = encode_browser_mp4(
                out.magnified_bgr, achieved_fps, res_dir / "magnified_share.mp4"
            )
            sbs_frames = render_side_by_side_frames(frames_bgr, out.magnified_bgr)
            sbs_mp4 = encode_browser_mp4(
                sbs_frames, achieved_fps, res_dir / "comparison_sbs.mp4"
            )
            del sbs_frames

            # Optional Dual-Camera Stream B processing when target_stream == "both"
            dual_sync_mp4_name: str | None = None
            out_b: MagnificationOutput | None = None
            frames_b_bgr: np.ndarray | None = None
            if is_dual_clip and target_stream == "both":
                try:
                    frames_b_bgr, ts_b, _ = load_clip_frames_and_timestamps(
                        clip_dir,
                        roi=params.roi_b,
                        max_width=params.max_width,
                        trim_start_s=params.trim_start_s,
                        trim_end_s=params.trim_end_s,
                        stream="cam_b",
                    )
                    if params.stabilize_subpixel:
                        frames_b_bgr = stabilize_frames_subpixel(frames_b_bgr)
                    frames_b_bgr, _ = fit_frames_to_memory(
                        frames_b_bgr, params.method, read_mem_available_bytes()
                    )
                    proc_b = method_cls()
                    out_b = proc_b.process(
                        frames_bgr=frames_b_bgr,
                        timestamps_s=ts_b,
                        fps=achieved_fps,
                        params=params,
                        progress_cb=lambda _v: not self.is_cancelled(job_id),
                    )
                    apply_chroma_attenuation_inplace(
                        frames_b_bgr, out_b.magnified_bgr, params.chrom_attenuation
                    )
                    if params.gate_background_noise:
                        apply_background_noise_gate_inplace(
                            frames_b_bgr,
                            out_b.magnified_bgr,
                            out_b.motion_energy,
                            threshold=params.heatmap_threshold,
                        )
                    dual_sbs_iter = render_side_by_side_frames(
                        out.magnified_bgr,
                        out_b.magnified_bgr,
                        left_label="Cam A (Magnified)",
                        right_label="Cam B (Magnified)",
                    )
                    dual_sync_mp4 = encode_browser_mp4(
                        dual_sbs_iter, achieved_fps, res_dir / "dual_sync.mp4"
                    )
                    dual_sync_mp4_name = dual_sync_mp4.name
                    out_b.magnified_bgr = np.empty((0,), dtype=np.uint8)
                except (OSError, ValueError, RuntimeError) as exc_b:
                    logger.warning("Dual-camera Stream B processing skipped: %s", exc_b)
                    out_b = None
                    frames_b_bgr = None

            out.magnified_bgr = np.empty((0,), dtype=np.uint8)
            gc.collect()

            heat_frames = render_heatmap_frames(
                frames_bgr, out.motion_energy, threshold=params.heatmap_threshold
            )
            heat_mp4 = encode_browser_mp4(heat_frames, achieved_fps, res_dir / "heatmap.mp4")
            del heat_frames

            ripple_frames = render_phase_ripple_frames(
                frames_bgr, out.motion_energy, threshold=params.heatmap_threshold
            )
            ripple_mp4 = encode_browser_mp4(
                ripple_frames, achieved_fps, res_dir / "phase_ripple.mp4"
            )
            del ripple_frames

            quiver_frames = render_quiver_frames(frames_bgr, epicenter=epicenter)
            quiver_mp4 = encode_browser_mp4(
                quiver_frames, achieved_fps, res_dir / "quiver_vectors.mp4"
            )
            del quiver_frames
            mean_energy_2d = np.mean(out.motion_energy, axis=0)
            out.motion_energy = np.empty((0,), dtype=np.float32)
            gc.collect()

            csv_path = write_signal_csv(
                res_dir / "signal.csv",
                out.timestamps_s,
                out.raw_roi_signal,
                out.filtered_roi_signal,
            )

            # Save spectral analysis + 1D kymograph + dual-ROI phase comparison + physical units
            try:
                from benchvision.analysis.spectrum import (
                    compute_2d_motion_orbit,
                    compute_cross_camera_analysis_and_3d_orbit,
                    compute_dual_roi_phase,
                    compute_kymograph,
                    compute_signal_spectrum,
                )
                from benchvision.core.hardware import compute_mm_per_pixel

                spec_data = compute_signal_spectrum(
                    signal_1d=out.raw_roi_signal,
                    filtered_1d=out.filtered_roi_signal,
                    timestamps_s=out.timestamps_s,
                    fps=achieved_fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                )
                spec_data["epicenter"] = epicenter
                spec_data["kymograph"] = compute_kymograph(
                    frames_bgr,
                    axis="horizontal",
                    line_pos=int(epicenter.get("proc_y", epicenter.get("y", h // 2))),
                )
                spec_data["dual_roi"] = compute_dual_roi_phase(
                    frames_bgr,
                    fps=achieved_fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    roi_b=params.roi_b,
                )

                report_base = render_diagnostic_report_snapshot(
                    frames_bgr[len(frames_bgr) // 2],
                    mean_energy_2d,
                    epicenter=epicenter,
                    dual_roi=None,
                    threshold=params.heatmap_threshold,
                )
                cv2.imwrite(str(res_dir / "report_base.jpg"), report_base)
                report_img = overlay_dual_roi_boxes_on_bgr(report_base, spec_data["dual_roi"])
                cv2.imwrite(str(res_dir / "report_frame.jpg"), report_img)

                lens_info = clip_meta.get("lens_info") or {}
                fov_deg = float(lens_info.get("fov_degrees", 60.0))
                dist_cm = float(lens_info.get("target_distance_cm", 50.0))
                ruler_mm_px = lens_info.get("ruler_mm_per_pixel")
                ruler_ref_w = int(lens_info.get("ruler_ref_width_px", 640))
                mm_per_px = compute_mm_per_pixel(
                    dist_cm,
                    fov_deg,
                    w,
                    ruler_mm_per_pixel=float(ruler_mm_px) if ruler_mm_px else None,
                    ruler_ref_width_px=ruler_ref_w,
                )
                disp_px = float(
                    out.extra_metrics.get(
                        "peak_displacement_px",
                        max(0.01, float(np.std(out.filtered_roi_signal)) * 0.15),
                    )
                )
                disp_mm = float(disp_px * mm_per_px)
                dom_f = float(spec_data.get("dominant_hz", out.dominant_hz) or 0.0)
                vel_rms_mm_s = float((2.0 * np.pi * dom_f * disp_mm) / np.sqrt(2.0))
                peak_accel_g = float((((2.0 * np.pi * dom_f) ** 2) * disp_mm) / 9806.65)
                spec_data["calibration"] = {
                    "target_distance_cm": dist_cm,
                    "fov_degrees": fov_deg,
                    "calibration_mode": "ruler_2pt" if ruler_mm_px else "pinhole_fov",
                    "mm_per_pixel": round(mm_per_px, 5),
                    "peak_displacement_px": round(disp_px, 4),
                    "peak_displacement_mm": round(disp_mm, 4),
                    "peak_displacement_um": round(disp_mm * 1000.0, 2),
                    "velocity_rms_mm_s": round(vel_rms_mm_s, 4),
                    "peak_accel_g": round(peak_accel_g, 5),
                }
                spec_data["orbit_2d"] = compute_2d_motion_orbit(
                    frames_bgr,
                    fps=achieved_fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    epicenter=epicenter,
                    mm_per_pixel=mm_per_px,
                )
                spec_data["dual_camera"] = bool(is_dual_clip and target_stream == "both" and out_b is not None)
                if is_dual_clip and target_stream == "both" and out_b is not None and frames_b_bgr is not None:
                    epicenter_b = compute_vibration_epicenter(
                        out_b.motion_energy,
                        frame_shape=frames_b_bgr.shape[1:3],
                        fps=achieved_fps,
                        low_hz=params.low_hz,
                        high_hz=params.high_hz,
                    )
                    orbit_b = compute_2d_motion_orbit(
                        frames_b_bgr,
                        fps=achieved_fps,
                        low_hz=params.low_hz,
                        high_hz=params.high_hz,
                        epicenter=epicenter_b,
                        mm_per_pixel=mm_per_px,
                    )
                    spec_data["dual_camera_sync"] = compute_cross_camera_analysis_and_3d_orbit(
                        raw_a=out.raw_roi_signal,
                        filt_a=out.filtered_roi_signal,
                        raw_b=out_b.raw_roi_signal,
                        filt_b=out_b.filtered_roi_signal,
                        timestamps_s=out.timestamps_s,
                        fps=achieved_fps,
                        low_hz=params.low_hz,
                        high_hz=params.high_hz,
                        orbit_a=spec_data["orbit_2d"],
                        orbit_b=orbit_b,
                        sync_mode=str(clip_meta.get("sync_mode", "software")),
                        sync_dt_mean_ms=float(clip_meta.get("sync_dt_mean_ms", 0.0) or 0.0),
                    )
            except ImportError:
                spec_data = {
                    "dominant_hz": out.dominant_hz,
                    "dominant_bpm": out.dominant_bpm,
                    "epicenter": epicenter,
                }
            finally:
                del frames_bgr
                if frames_b_bgr is not None:
                    del frames_b_bgr
                gc.collect()

            (res_dir / "spectrum.json").write_text(
                json.dumps(spec_data, indent=2), encoding="utf-8"
            )

            files_map: dict[str, str] = {
                "original_mp4": orig_mp4.name,
                "magnified_mp4": share_mp4.name,
                "comparison_sbs_mp4": sbs_mp4.name,
                "heatmap_mp4": heat_mp4.name,
                "phase_ripple_mp4": ripple_mp4.name,
                "quiver_vectors_mp4": quiver_mp4.name,
                "magnified_lossless": lossless_path.name,
                "signal_csv": csv_path.name,
                "spectrum_json": "spectrum.json",
            }
            if dual_sync_mp4_name:
                files_map["dual_sync_mp4"] = dual_sync_mp4_name

            job["status"] = "completed"
            job["progress"] = 1.0
            job["completed_at"] = datetime.now(UTC).isoformat()
            job["params"] = params.model_dump()
            job["result"] = {
                "dominant_hz": out.dominant_hz,
                "dominant_bpm": out.dominant_bpm,
                "achieved_fps": round(achieved_fps, 3),
                "width": w,
                "height": h,
                "frame_count": n_frames_out,
                "epicenter": epicenter,
                "calibration": spec_data.get("calibration", {}),
                "memory_downscale": round(mem_scale, 3),
                "dual_camera": bool(is_dual_clip and target_stream == "both" and dual_sync_mp4_name),
                "target_stream": target_stream,
                "sync_mode": str(clip_meta.get("sync_mode", "software")) if is_dual_clip else "single",
                "sync_dt_mean_ms": float(clip_meta.get("sync_dt_mean_ms", 0.0) or 0.0) if is_dual_clip else 0.0,
                "files": files_map,
                "extra_metrics": out.extra_metrics,
            }
            self.db.upsert_job(job)
            logger.info(
                "Completed job %s (%s): dominant %.4f Hz (%.2f BPM)",
                job_id,
                params.method,
                out.dominant_hz,
                out.dominant_bpm,
            )
            return job

        except JobCancelledError:
            job["status"] = "cancelled"
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
            return job
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            job["status"] = "failed"
            job["error_message"] = str(exc)
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
            return job

    def compare_jobs(
        self, clip_id: str, job_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Return a structured comparison of completed jobs on the same clip."""
        all_jobs = self.db.list_jobs_for_clip(clip_id)
        completed = [j for j in all_jobs if j["status"] == "completed"]
        if job_ids:
            wanted = set(job_ids)
            completed = [j for j in completed if j["job_id"] in wanted]

        comparisons: list[dict[str, Any]] = []
        for j in completed:
            res_dir = self.layout.job_result_dir(clip_id, j["job_id"])
            spec_path = res_dir / "spectrum.json"
            snr_db = 0.0
            if spec_path.exists():
                try:
                    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
                    snr_db = float(spec_data.get("snr_db", 0.0))
                except (ValueError, OSError):
                    snr_db = 0.0

            res = j.get("result") or {}
            comparisons.append(
                {
                    "job_id": j["job_id"],
                    "method": j["method"],
                    "params": j["params"],
                    "dominant_hz": res.get("dominant_hz", 0.0),
                    "dominant_bpm": res.get("dominant_bpm", 0.0),
                    "snr_db": snr_db,
                    "extra_metrics": res.get("extra_metrics", {}),
                    "files": res.get("files", {}),
                }
            )

        freqs = [c["dominant_hz"] for c in comparisons if c["dominant_hz"] > 0]
        freq_spread = round(max(freqs) - min(freqs), 4) if len(freqs) >= 2 else 0.0
        return {
            "clip_id": clip_id,
            "job_count": len(comparisons),
            "frequency_spread_hz": freq_spread,
            "comparisons": comparisons,
        }

    def shutdown(self) -> None:
        self._running = False
        self._queue.put(None)
