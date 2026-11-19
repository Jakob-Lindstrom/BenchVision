"""Motion Still Studio: composite an entire video clip into a single long-exposure or stroboscopic photo."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

STILL_STYLES: dict[str, dict[str, str]] = {
    "chrono_paint": {
        "name": "Rainbow Motion Painting",
        "description": "Stationary scene stays sharp while moving objects leave a time-coded rainbow trail.",
    },
    "strobe_ghost": {
        "name": "Stroboscopic Multi-Exposure",
        "description": "Edgerton-style freeze-frame sequence stamping a moving object across the clean background.",
    },
    "median_bg": {
        "name": "Crowd / Ghost Eraser",
        "description": "Temporal median background extractor that removes all moving objects from the scene.",
    },
    "motion_trails": {
        "name": "Long-Exposure Motion Glow",
        "description": "Accumulates peak motion energy over time into a glowing thermal light-painting composite.",
    },
}


def _sample_indices(n_total: int, max_samples: int) -> np.ndarray:
    """Return unique evenly spaced frame indices across [0, n_total - 1]."""
    if n_total <= max_samples:
        return np.arange(n_total, dtype=int)
    return np.unique(np.linspace(0, n_total - 1, max_samples, dtype=int))


def render_motion_still(
    frames_bgr: np.ndarray,
    style: str = "chrono_paint",
    sensitivity: float = 1.0,
    strobe_count: int = 8,
) -> np.ndarray:
    """Composite a 4D (T, H, W, 3) BGR clip into a single high-res uint8 BGR still image."""
    if style not in STILL_STYLES:
        raise ValueError(
            f"Unsupported still style '{style}'. Expected one of {list(STILL_STYLES)}."
        )
    if frames_bgr.ndim != 4 or len(frames_bgr) < 2:
        raise ValueError("Clip must contain at least 2 BGR frames to render a motion still.")

    n, h, w, _ = frames_bgr.shape
    sens = max(0.2, min(5.0, float(sensitivity)))

    bg_idxs = _sample_indices(n, 48 if style == "median_bg" else 28)
    bg = np.median(frames_bgr[bg_idxs].astype(np.float32), axis=0)
    if style == "median_bg":
        return np.clip(bg, 0.0, 255.0).astype(np.uint8)

    if style == "chrono_paint":
        idxs = _sample_indices(n, 48)
        m = len(idxs)
        lut_vals = np.linspace(0, 255, m, dtype=np.uint8).reshape(1, m)
        lut_bgr = cv2.applyColorMap(lut_vals, cv2.COLORMAP_TURBO)[0].astype(np.float32)

        canvas = (bg * 0.55).astype(np.float32)
        best_alpha = np.zeros((h, w), dtype=np.float32)
        thresh = max(4.0, 22.0 / sens)

        for i, f_idx in enumerate(idxs):
            frm = frames_bgr[int(f_idx)].astype(np.float32)
            diff = cv2.GaussianBlur(np.mean(np.abs(frm - bg), axis=2), (3, 3), 0)
            a = np.clip((diff - thresh) / (thresh * 1.8), 0.0, 1.0)
            tint = lut_bgr[i]
            painted = 0.45 * frm + 0.55 * tint[None, None, :]
            weight = a * (0.45 + 0.55 * (1.0 - best_alpha * 0.4))
            canvas = canvas * (1.0 - weight[..., None]) + painted * weight[..., None]
            best_alpha = np.maximum(best_alpha, a)

        return np.clip(canvas, 0.0, 255.0).astype(np.uint8)

    if style == "strobe_ghost":
        count = max(3, min(20, int(strobe_count), n))
        idxs = _sample_indices(n, count)
        canvas = bg.copy()
        best_score = np.zeros((h, w), dtype=np.float32)
        thresh = max(4.0, 20.0 / sens)

        for f_idx in idxs:
            frm = frames_bgr[int(f_idx)].astype(np.float32)
            diff = cv2.GaussianBlur(np.mean(np.abs(frm - bg), axis=2), (3, 3), 0)
            m = np.clip((diff - thresh) / (thresh * 1.2), 0.0, 1.0)
            blend = np.where(diff >= best_score, m, m * 0.55)
            canvas = canvas * (1.0 - blend[..., None]) + frm * blend[..., None]
            best_score = np.maximum(best_score, diff)

        return np.clip(canvas, 0.0, 255.0).astype(np.uint8)

    # style == "motion_trails"
    idxs = _sample_indices(n, 64)
    peak_diff = np.zeros((h, w), dtype=np.float32)
    for f_idx in idxs:
        frm = frames_bgr[int(f_idx)].astype(np.float32)
        diff = cv2.GaussianBlur(np.mean(np.abs(frm - bg), axis=2), (3, 3), 0)
        peak_diff = np.maximum(peak_diff, diff)

    p99 = float(np.percentile(peak_diff, 99.0)) if float(np.max(peak_diff)) > 0.0 else 1.0
    norm = np.clip((peak_diff * sens) / max(p99, 1e-4), 0.0, 1.0)
    heat = cv2.applyColorMap((norm * 255.0).astype(np.uint8), cv2.COLORMAP_INFERNO).astype(
        np.float32
    )
    base = bg * 0.65
    glow = base * (1.0 - 0.85 * norm[..., None]) + heat * (1.10 * norm[..., None])
    return np.clip(glow, 0.0, 255.0).astype(np.uint8)


def list_still_styles() -> list[dict[str, Any]]:
    """Return metadata for UI/API consumers listing available Motion Still Studio styles."""
    return [{"key": k, **v} for k, v in STILL_STYLES.items()]
