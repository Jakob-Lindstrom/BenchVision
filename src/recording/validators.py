"""Pre-recording validation for Nyquist sampling criterion, frequency resolution, and disk space."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def validate_nyquist_and_duration(
    fps: float,
    max_freq_hz: float,
    min_freq_hz: float = 0.5,
    duration_s: float = 10.0,
) -> dict[str, Any]:
    """Validate sampling rate against Nyquist (FPS >= 2 * f_max) and duration against freq resolution."""
    nyquist_limit_hz = fps / 2.0
    required_min_fps = 2.0 * max_freq_hz
    recommended_fps = 2.5 * max_freq_hz
    freq_resolution_hz = 1.0 / max(duration_s, 0.1)
    min_cycles = min_freq_hz * duration_s

    warnings: list[str] = []
    valid = True

    if fps < required_min_fps:
        valid = False
        warnings.append(
            f"Nyquist violation: FPS ({fps:.1f} Hz) is below 2x highest target frequency "
            f"({required_min_fps:.1f} Hz for {max_freq_hz:.1f} Hz). Aliasing will occur."
        )
    elif fps < recommended_fps:
        warnings.append(
            f"Near-Nyquist warning: FPS ({fps:.1f} Hz) is close to 2x {max_freq_hz:.1f} Hz. "
            f"Recommend >= {recommended_fps:.1f} FPS for clean temporal bandpass rolloff."
        )

    if min_cycles < 4.0:
        warnings.append(
            f"Short duration ({duration_s:.1f}s) captures only {min_cycles:.1f} cycles at "
            f"{min_freq_hz:.2f} Hz. Recommend >= {max(4.0 / max(min_freq_hz, 0.05), 8.0):.0f}s."
        )

    return {
        "valid": valid,
        "fps": fps,
        "nyquist_limit_hz": round(nyquist_limit_hz, 2),
        "required_min_fps": round(required_min_fps, 2),
        "recommended_fps": round(recommended_fps, 2),
        "freq_resolution_hz": round(freq_resolution_hz, 4),
        "warnings": warnings,
        "capture_guidance": [
            "Mount the camera on a rigid tripod or clamp; sub-pixel camera shake overwhelms subtle motion.",
            "Use steady DC / daylight illumination; 50/60 Hz AC mains flicker creates strong alias peaks.",
            "Lock exposure, gain, white balance, and focus before starting recording.",
        ],
    }


def estimate_recording_storage(
    width: int,
    height: int,
    fps: float,
    duration_s: float,
    storage_format: str,
    data_dir: Path,
    roi: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Estimate file size for lossless/near-lossless recording and check available disk space."""
    eff_w = roi[2] if roi and len(roi) == 4 and roi[2] > 0 else width
    eff_h = roi[3] if roi and len(roi) == 4 and roi[3] > 0 else height
    raw_frame_bytes = eff_w * eff_h * 3
    total_frames = round(fps * duration_s)

    # Compression ratio relative to raw 24-bit RGB
    ratio_map = {
        "mjpeg_q100": 0.18,  # Near-lossless Q100 MJPEG + lossless NPZ sidecar
        "ffv1_mkv": 0.45,    # Lossless FFV1 / lossless archive
        "raw_npz": 0.65,     # Lossless compressed NumPy array archive
    }
    ratio = ratio_map.get(storage_format, 0.35)
    estimated_bytes = int(raw_frame_bytes * total_frames * ratio)

    usage = shutil.disk_usage(Path(data_dir).resolve())
    # Keep at least 100 MB safety margin
    safety_margin = 100 * 1024 * 1024
    has_space = (usage.free - estimated_bytes) > safety_margin

    return {
        "effective_width": eff_w,
        "effective_height": eff_h,
        "total_frames": total_frames,
        "storage_format": storage_format,
        "estimated_bytes": estimated_bytes,
        "estimated_mb": round(estimated_bytes / (1024 * 1024), 2),
        "free_bytes": usage.free,
        "free_mb": round(usage.free / (1024 * 1024), 2),
        "free_gb": round(usage.free / (1024**3), 2),
        "sufficient_space": has_space,
    }
