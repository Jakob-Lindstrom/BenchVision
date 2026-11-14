"""FFT, Welch PSD, STFT spectrogram, and auto-find dominant passband recommender."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from scipy.signal import detrend as scipy_detrend
from scipy.signal import spectrogram, welch

from benchvision.processing.filters import (
    apply_temporal_bandpass,
    clamp_passband,
    find_dominant_frequency,
    sanitize_1d_signal,
)


def compute_kymograph(
    frames_bgr: np.ndarray,
    axis: str = "horizontal",
    line_pos: float | None = None,
    max_spatial_pts: int = 160,
) -> dict[str, Any]:
    """Generate a 1D Slit-Scan spacetime Kymograph (T x L) across a vibrating edge."""
    n, h, w, _ = frames_bgr.shape
    if axis == "vertical":
        if line_pos is None:
            raw_pos = w // 2
        elif 0.0 < float(line_pos) < 1.0:
            raw_pos = int(float(line_pos) * w)
        else:
            raw_pos = int(line_pos)
        pos = max(0, min(raw_pos, w - 1))
        # Slice (T, H, 3) -> luma (T, H)
        sl = frames_bgr[:, :, pos, :].astype(np.float32)
        luma = 0.114 * sl[..., 0] + 0.587 * sl[..., 1] + 0.299 * sl[..., 2]
        length = h
    else:
        axis = "horizontal"
        if line_pos is None:
            raw_pos = h // 2
        elif 0.0 < float(line_pos) < 1.0:
            raw_pos = int(float(line_pos) * h)
        else:
            raw_pos = int(line_pos)
        pos = max(0, min(raw_pos, h - 1))
        # Slice (T, W, 3) -> luma (T, W)
        sl = frames_bgr[:, pos, :, :].astype(np.float32)
        luma = 0.114 * sl[..., 0] + 0.587 * sl[..., 1] + 0.299 * sl[..., 2]
        length = w

    if length > max_spatial_pts:
        luma = cv2.resize(luma, (max_spatial_pts, n), interpolation=cv2.INTER_AREA)

    # Enhance temporal contrast along each spatial pixel so subtle vibrations stand out clearly
    temporal_mean = np.mean(luma, axis=0, keepdims=True)
    enhanced = np.clip(luma + 3.5 * (luma - temporal_mean), 0.0, 255.0).astype(np.uint8)

    return {
        "axis": axis,
        "line_pos": int(pos),
        "n_frames": int(n),
        "spatial_length": int(enhanced.shape[1]),
        "width_px": int(w),
        "height_px": int(h),
        "matrix": [[int(v) for v in row] for row in enhanced],
    }


def compute_dual_roi_phase(
    frames_bgr: np.ndarray,
    fps: float,
    low_hz: float = 0.5,
    high_hz: float = 10.0,
    roi_a: tuple[int, int, int, int] | list[int] | None = None,
    roi_b: tuple[int, int, int, int] | list[int] | None = None,
) -> dict[str, Any]:
    """Compare two virtual ROI probes (ROI A vs. ROI B) and compute their phase shift in degrees."""
    n, h, w, _ = frames_bgr.shape
    if not roi_a or len(roi_a) != 4:
        roi_a = (max(0, w // 8), max(0, h // 6), max(8, w // 3), max(8, h * 2 // 3))
    if not roi_b or len(roi_b) != 4:
        roi_b = (max(0, w * 13 // 24), max(0, h // 6), max(8, w // 3), max(8, h * 2 // 3))

    def _extract_probe(r: tuple[int, int, int, int] | list[int]) -> tuple[np.ndarray, list[int]]:
        rx, ry, rw, rh = int(r[0]), int(r[1]), int(r[2]), int(r[3])
        x0 = max(0, min(rx, w - 2))
        y0 = max(0, min(ry, h - 2))
        x1 = max(x0 + 2, min(x0 + rw, w))
        y1 = max(y0 + 2, min(y0 + rh, h))
        patch = frames_bgr[:, y0:y1, x0:x1, 1].astype(np.float32)
        g_mean = np.mean(patch, axis=(1, 2))
        gx = np.mean(np.abs(np.diff(patch, axis=2)), axis=(1, 2)) if (x1 - x0) > 2 else np.zeros(n)
        if n > 4:
            g_mean[:4] = g_mean[4]
            gx[:4] = gx[4]
        sig = (g_mean - np.mean(g_mean)) + 3.0 * (gx - np.mean(gx))
        return sig.astype(np.float64), [x0, y0, x1 - x0, y1 - y0]

    sig_a, box_a = _extract_probe(roi_a)
    sig_b, box_b = _extract_probe(roi_b)

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    filt_a = apply_temporal_bandpass(sig_a, fps=fps, low_hz=lo, high_hz=hi).astype(np.float64)
    filt_b = apply_temporal_bandpass(sig_b, fps=fps, low_hz=lo, high_hz=hi).astype(np.float64)

    # Compute cross-spectral phase difference at dominant frequency in [lo, hi]
    freqs = np.fft.rfftfreq(n, d=1.0 / max(fps, 1.0))
    fa = np.fft.rfft(filt_a * np.hanning(n))
    fb = np.fft.rfft(filt_b * np.hanning(n))
    mask = (freqs >= lo) & (freqs <= hi)
    if np.any(mask):
        cross_power = np.abs(fa[mask]) * np.abs(fb[mask])
        peak_idx = int(np.argmax(cross_power))
        sel_idx = np.where(mask)[0][peak_idx]
        cross_val = fa[sel_idx] * np.conj(fb[sel_idx])
        phase_deg = float(np.degrees(np.angle(cross_val)))
    else:
        phase_deg = 0.0

    std_a = float(np.std(filt_a))
    std_b = float(np.std(filt_b))
    if std_a > 1e-9 and std_b > 1e-9:
        corr = float(np.mean((filt_a - np.mean(filt_a)) * (filt_b - np.mean(filt_b))) / (std_a * std_b))
    else:
        corr = 0.0

    abs_deg = abs(phase_deg)
    if abs_deg <= 35.0:
        mode_label = "In-Phase (0° Synchronized)"
    elif abs_deg >= 145.0:
        mode_label = "Out-of-Phase (180° Pivot / Seesaw)"
    else:
        mode_label = f"Phase-Shifted Wave ({phase_deg:+.1f}°)"

    t_s = np.arange(n, dtype=np.float64) / max(fps, 1.0)
    return {
        "roi_a": box_a,
        "roi_b": box_b,
        "phase_shift_deg": round(phase_deg, 1),
        "correlation": round(corr, 3),
        "mode_label": mode_label,
        "t_s": [round(float(t), 4) for t in t_s],
        "signal_a": [round(float(v), 4) for v in filt_a],
        "signal_b": [round(float(v), 4) for v in filt_b],
    }


def compute_lighting_aliases(fps: float, dominant_hz: float = 0.0) -> dict[str, Any]:
    """Calculate where 50 Hz (100 Hz ripple) and 60 Hz (120 Hz ripple) AC lighting flicker aliases at capture_fps."""
    eff_fps = max(float(fps), 1.0)
    alias_100 = abs(100.0 - round(100.0 / eff_fps) * eff_fps)
    alias_120 = abs(120.0 - round(120.0 / eff_fps) * eff_fps)
    warn_50 = alias_100 > 0.15 and abs(dominant_hz - alias_100) <= 0.35
    warn_60 = alias_120 > 0.15 and abs(dominant_hz - alias_120) <= 0.35
    warning_msg: str | None = None
    if warn_50:
        warning_msg = f"Peak ({dominant_hz:.2f} Hz) aligns with 50 Hz AC lighting alias (100 Hz @ {eff_fps:.1f} FPS -> {alias_100:.2f} Hz)"
    elif warn_60:
        warning_msg = f"Peak ({dominant_hz:.2f} Hz) aligns with 60 Hz AC lighting alias (120 Hz @ {eff_fps:.1f} FPS -> {alias_120:.2f} Hz)"
    return {
        "fps": round(eff_fps, 2),
        "alias_50hz_mains_hz": round(alias_100, 2),
        "alias_60hz_mains_hz": round(alias_120, 2),
        "is_alias_suspect": bool(warn_50 or warn_60),
        "warning": warning_msg,
    }


def compute_2d_motion_orbit(
    frames_bgr: np.ndarray,
    fps: float,
    low_hz: float = 0.5,
    high_hz: float = 5.0,
    epicenter: dict[str, Any] | None = None,
    mm_per_pixel: float = 0.25,
) -> dict[str, Any]:
    """Extract sub-pixel 2D trajectory (X(t) vs. Y(t)) and principal oscillation angle (theta deg) around the epicenter/ROI."""
    n, h, w, _ = frames_bgr.shape
    cx = int(epicenter.get("proc_x", w // 2)) if epicenter else w // 2
    cy = int(epicenter.get("proc_y", h // 2)) if epicenter else h // 2
    rad_x = max(10, min(w // 3, 48))
    rad_y = max(10, min(h // 3, 48))
    x0 = max(0, min(w - 6, cx - rad_x))
    y0 = max(0, min(h - 6, cy - rad_y))
    x1 = max(x0 + 6, min(w, cx + rad_x))
    y1 = max(y0 + 6, min(h, cy + rad_y))

    patch = frames_bgr[:, y0:y1, x0:x1, 1].astype(np.float32)
    ph, pw = patch.shape[1:3]
    yy, xx = np.mgrid[0:ph, 0:pw].astype(np.float32)
    xx -= pw * 0.5
    yy -= ph * 0.5

    ref = np.mean(patch[: min(n, 16)], axis=0)
    gx_ref = cv2.Sobel(ref, cv2.CV_32F, 1, 0, ksize=3)
    gy_ref = cv2.Sobel(ref, cv2.CV_32F, 0, 1, ksize=3)
    denom_x = float(np.sum(gx_ref**2)) + 1e-3
    denom_y = float(np.sum(gy_ref**2)) + 1e-3

    raw_x = np.zeros(n, dtype=np.float64)
    raw_y = np.zeros(n, dtype=np.float64)
    for t in range(n):
        dt_img = patch[t] - ref
        # Least-squares Lucas-Kanade optical flow projection + intensity moment fallback
        dx_lk = -float(np.sum(dt_img * gx_ref)) / denom_x
        dy_lk = -float(np.sum(dt_img * gy_ref)) / denom_y
        w_sum = float(np.sum(patch[t])) + 1e-3
        cx_t = float(np.sum(patch[t] * xx)) / w_sum
        cy_t = float(np.sum(patch[t] * yy)) / w_sum
        raw_x[t] = 0.65 * dx_lk + 0.35 * cx_t
        raw_y[t] = 0.65 * dy_lk + 0.35 * cy_t

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    filt_x_px = apply_temporal_bandpass(sanitize_1d_signal(raw_x), fps=fps, low_hz=lo, high_hz=hi).astype(np.float64)
    filt_y_px = apply_temporal_bandpass(sanitize_1d_signal(raw_y), fps=fps, low_hz=lo, high_hz=hi).astype(np.float64)

    um_per_px = max(1.0, float(mm_per_pixel) * 1000.0)
    x_um = filt_x_px * um_per_px
    y_um = filt_y_px * um_per_px

    var_x = float(np.var(x_um))
    var_y = float(np.var(y_um))
    cov_xy = float(np.mean((x_um - np.mean(x_um)) * (y_um - np.mean(y_um))))
    theta_rad = 0.5 * np.arctan2(2.0 * cov_xy, var_x - var_y + 1e-12)
    theta_deg = float(np.degrees(theta_rad))
    trace = var_x + var_y
    det = max(0.0, var_x * var_y - cov_xy * cov_xy)
    disc = float(np.sqrt(max(0.0, trace * trace - 4.0 * det)))
    lam1 = max(1e-9, 0.5 * (trace + disc))
    lam2 = max(0.0, 0.5 * (trace - disc))
    eccentricity = float(np.sqrt(max(0.0, min(1.0, 1.0 - lam2 / lam1))))
    span_x = float(np.percentile(x_um, 98) - np.percentile(x_um, 2)) if n >= 4 else 0.0
    span_y = float(np.percentile(y_um, 98) - np.percentile(y_um, 2)) if n >= 4 else 0.0

    t_s = np.arange(n, dtype=np.float64) / max(fps, 1.0)
    return {
        "t_s": [round(float(t), 4) for t in t_s],
        "x_px": [round(float(v), 5) for v in filt_x_px],
        "y_px": [round(float(v), 5) for v in filt_y_px],
        "x_um": [round(float(v), 3) for v in x_um],
        "y_um": [round(float(v), 3) for v in y_um],
        "principal_angle_deg": round(theta_deg, 1),
        "span_x_um": round(span_x, 2),
        "span_y_um": round(span_y, 2),
        "eccentricity": round(eccentricity, 3),
        "unit": "µm",
    }


def compute_stft_spectrogram(
    signal_1d: np.ndarray,
    fps: float,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Compute a high-overlap, zero-padded STFT spectrogram with Frame-0 transient suppression."""
    n = len(signal_1d)
    if n < 8:
        return {"times_s": [], "freqs_hz": [], "power_db": []}

    sig = sanitize_1d_signal(signal_1d)
    sig = scipy_detrend(sig, type="linear")
    sig = sig - float(np.mean(sig))
    if n > 6:
        skip = max(4, int(np.ceil(n * 0.05)))
        if skip < n:
            p95 = float(np.percentile(np.abs(sig[skip:]), 95))
            if p95 > 1e-9:
                sig[:skip] = np.clip(sig[:skip], -p95, p95)

    spec_nperseg = min(n, max(16, round(fps * 1.6)))
    spec_noverlap = max(1, min(spec_nperseg - 1, int(spec_nperseg * 0.90)))
    target_fft = max(256, int(spec_nperseg * 4))
    nfft = 1 << (target_fft - 1).bit_length()

    s_freqs, s_times, s_xx = spectrogram(
        sig,
        fs=fps,
        window="hann",
        nperseg=spec_nperseg,
        noverlap=spec_noverlap,
        nfft=nfft,
        detrend="linear",
    )
    s_db = 10.0 * np.log10(np.maximum(s_xx, 1e-12))

    # Suppress any residual first-window boundary splatter at t=0
    if s_db.shape[1] >= 3:
        s_db[:, 0] = np.minimum(s_db[:, 0], s_db[:, 1] + 1.5)

    total_dur = float(duration_s) if (duration_s and duration_s > 0) else float((n - 1) / max(fps, 1.0))
    if len(s_times) >= 2:
        mapped_times = np.linspace(0.0, total_dur, len(s_times))
    elif len(s_times) == 1:
        mapped_times = np.array([0.5 * total_dur], dtype=np.float64)
    else:
        mapped_times = s_times

    return {
        "times_s": [round(float(t), 3) for t in mapped_times],
        "freqs_hz": [round(float(f), 3) for f in s_freqs],
        "power_db": [[round(float(val), 2) for val in row] for row in s_db],
    }


def compute_signal_spectrum(
    signal_1d: np.ndarray,
    filtered_1d: np.ndarray,
    timestamps_s: np.ndarray,
    fps: float,
    low_hz: float = 0.2,
    high_hz: float = 10.0,
) -> dict[str, Any]:
    """Compute FFT, Welch PSD, STFT spectrogram, dominant Hz/BPM, and suggested passband."""
    n = len(signal_1d)
    if n < 8:
        return {
            "dominant_hz": 0.0,
            "dominant_bpm": 0.0,
            "snr_db": 0.0,
            "freqs_hz": [],
            "fft_magnitude": [],
            "welch_freqs_hz": [],
            "welch_psd": [],
            "spectrogram": {"times_s": [], "freqs_hz": [], "power_db": []},
            "suggested_band": {"low_hz": low_hz, "high_hz": high_hz, "peak_hz": 0.0},
            "time_series": {"t_s": [], "raw": [], "filtered": []},
        }

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    clean_raw = sanitize_1d_signal(signal_1d)
    clean_filt = sanitize_1d_signal(filtered_1d)
    detrended = scipy_detrend(clean_raw, type="linear")
    detrended = detrended - float(np.mean(detrended))
    if n > 6:
        p95_raw = float(np.percentile(np.abs(detrended[4:]), 95))
        if p95_raw > 1e-9:
            detrended[:4] = np.clip(detrended[:4], -p95_raw, p95_raw)
        p95_filt = float(np.percentile(np.abs(clean_filt[4:]), 95))
        if p95_filt > 1e-9:
            clean_filt[:4] = np.clip(clean_filt[:4], -p95_filt, p95_filt)
    win = np.hanning(n)
    fft_vals = np.abs(np.fft.rfft(detrended * win)) / max(n, 1)
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / fps)

    nperseg = min(n, max(16, round(fps * 3.0)))
    w_freqs, w_psd = welch(detrended, fs=fps, nperseg=nperseg)

    duration_s = float(timestamps_s[-1] - timestamps_s[0]) if len(timestamps_s) >= 2 else float((n - 1) / max(fps, 1.0))
    spec_dict = compute_stft_spectrogram(detrended, fps=fps, duration_s=duration_s)

    dom_hz, dom_bpm = find_dominant_frequency(detrended, fps=fps, low_hz=lo, high_hz=hi)

    # Calculate in-band SNR (peak power vs median background power)
    band_mask = (fft_freqs >= lo) & (fft_freqs <= hi)
    if np.any(band_mask):
        band_mag = fft_vals[band_mask]
        peak_val = float(np.max(band_mag))
        noise_floor = float(np.median(band_mag)) + 1e-9
        snr_db = float(20.0 * np.log10(max(peak_val / noise_floor, 1.0)))
    else:
        snr_db = 0.0

    # Auto-find dominant frequency across wide physiological/mechanical range [0.15 Hz, 0.45*fps]
    auto_peak_hz, auto_peak_bpm = find_dominant_frequency(
        detrended, fps=fps, low_hz=0.15, high_hz=0.45 * fps
    )
    half_bw = max(0.35, auto_peak_hz * 0.28)
    sug_lo, sug_hi = clamp_passband(
        max(0.08, auto_peak_hz - half_bw), auto_peak_hz + half_bw, fps
    )

    return {
        "dominant_hz": dom_hz,
        "dominant_bpm": dom_bpm,
        "snr_db": round(snr_db, 2),
        "freqs_hz": [round(float(x), 4) for x in fft_freqs],
        "fft_magnitude": [round(float(x), 6) for x in fft_vals],
        "welch_freqs_hz": [round(float(x), 4) for x in w_freqs],
        "welch_psd": [round(float(x), 6) for x in w_psd],
        "spectrogram": spec_dict,
        "lighting_aliases": compute_lighting_aliases(fps=fps, dominant_hz=dom_hz),
        "suggested_band": {
            "low_hz": round(sug_lo, 2),
            "high_hz": round(sug_hi, 2),
            "peak_hz": auto_peak_hz,
            "peak_bpm": auto_peak_bpm,
        },
        "time_series": {
            "t_s": [round(float(t), 4) for t in timestamps_s],
            "raw": [round(float(x), 4) for x in detrended],
            "filtered": [round(float(x), 4) for x in clean_filt],
        },
    }


def compute_roi_spectrum_analysis(
    frames_bgr: np.ndarray,
    fps: float,
    roi: tuple[int, int, int, int] | None = None,
    low_hz: float = 0.2,
    high_hz: float = 10.0,
) -> dict[str, Any]:
    """Extract ROI green/luminance signal from a raw clip and run full spectral & auto-band analysis."""
    n, h, w, _ = frames_bgr.shape
    if roi and len(roi) == 4:
        rx, ry, rw, rh = roi
        x0 = max(0, min(rx, w - 2))
        y0 = max(0, min(ry, h - 2))
        x1 = max(x0 + 2, min(x0 + rw, w))
        y1 = max(y0 + 2, min(y0 + rh, h))
        sub = frames_bgr[:, y0:y1, x0:x1, :]
    else:
        sub = frames_bgr[:, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, :]

    # Use spatial gradient + green channel luma so both intensity and motion peaks are captured
    green_mean = np.mean(sub[..., 1].astype(np.float32), axis=(1, 2))
    gx = np.mean(np.abs(np.diff(sub[..., 1].astype(np.float32), axis=2)), axis=(1, 2))
    if n > 4:
        green_mean[:4] = green_mean[4]
        gx[:4] = gx[4]
    combined = (green_mean - np.mean(green_mean)) + 4.0 * (gx - np.mean(gx))
    timestamps_s = np.arange(n, dtype=np.float64) / max(fps, 1.0)
    filt_combined = apply_temporal_bandpass(
        combined, fps=fps, low_hz=low_hz, high_hz=high_hz
    ).astype(np.float64)

    return compute_signal_spectrum(
        signal_1d=combined.astype(np.float64),
        filtered_1d=filt_combined,
        timestamps_s=timestamps_s,
        fps=fps,
        low_hz=low_hz,
        high_hz=high_hz,
    )


def compute_cross_camera_analysis_and_3d_orbit(
    raw_a: np.ndarray,
    filt_a: np.ndarray,
    raw_b: np.ndarray,
    filt_b: np.ndarray,
    timestamps_s: np.ndarray,
    fps: float,
    low_hz: float,
    high_hz: float,
    orbit_a: dict[str, Any],
    orbit_b: dict[str, Any],
    sync_mode: str = "software",
    sync_dt_mean_ms: float = 0.0,
) -> dict[str, Any]:
    """Compute Cam A vs. Cam B cross-spectral phase shift, coherence, and 3D (X, Y, Z) orbit."""
    n = min(len(raw_a), len(raw_b), len(filt_a), len(filt_b), len(timestamps_s))
    if n < 8:
        return {"enabled": False}

    ts = timestamps_s[:n]
    spec_b = compute_signal_spectrum(
        signal_1d=raw_b[:n],
        filtered_1d=filt_b[:n],
        timestamps_s=ts,
        fps=fps,
        low_hz=low_hz,
        high_hz=high_hz,
    )

    fa = sanitize_1d_signal(filt_a[:n]) - float(np.mean(filt_a[:n]))
    fb = sanitize_1d_signal(filt_b[:n]) - float(np.mean(filt_b[:n]))
    win = np.hanning(n)
    fft_a = np.fft.rfft(fa * win)
    fft_b = np.fft.rfft(fb * win)
    freqs = np.fft.rfftfreq(n, d=1.0 / max(fps, 1.0))

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    mask = (freqs >= lo) & (freqs <= hi)
    if np.any(mask):
        idx_band = np.where(mask)[0]
        cross_power = np.abs(fft_a[idx_band]) * np.abs(fft_b[idx_band])
        peak_idx = int(idx_band[int(np.argmax(cross_power))])
    else:
        peak_idx = max(1, int(np.argmax(np.abs(fft_a[1:]))) + 1)

    shared_hz = float(freqs[peak_idx]) if peak_idx < len(freqs) else float(low_hz)
    cross_val = fft_a[peak_idx] * np.conj(fft_b[peak_idx])
    phase_rad = float(np.angle(cross_val))
    phase_deg = float(np.degrees(phase_rad))
    lag_ms = float((phase_rad / (2.0 * np.pi * max(shared_hz, 0.05))) * 1000.0)

    denom = float(np.linalg.norm(fa) * np.linalg.norm(fb))
    coherence = float(abs(np.dot(fa, fb)) / denom) if denom > 1e-9 else 0.0
    # Spectral magnitude coherence at peak bin
    spec_denom = float(abs(fft_a[peak_idx]) * abs(fft_b[peak_idx]))
    if spec_denom > 1e-9:
        coherence = max(coherence, min(1.0, float(abs(cross_val) / spec_denom)))

    xa = np.asarray(orbit_a.get("x_um") or [], dtype=np.float64)[:n]
    ya = np.asarray(orbit_a.get("y_um") or [], dtype=np.float64)[:n]
    zb = np.asarray(orbit_b.get("x_um") or [], dtype=np.float64)[:n]
    yb = np.asarray(orbit_b.get("y_um") or [], dtype=np.float64)[:n]

    m = min(len(xa), len(ya), len(zb), len(yb))
    if m >= 4:
        x_3d = xa[:m]
        y_3d = 0.5 * (ya[:m] + yb[:m])
        z_3d = zb[:m]
        span_x = float(np.percentile(x_3d, 98) - np.percentile(x_3d, 2))
        span_y = float(np.percentile(y_3d, 98) - np.percentile(y_3d, 2))
        span_z = float(np.percentile(z_3d, 98) - np.percentile(z_3d, 2))
        r_rms = float(np.sqrt(np.mean(x_3d * x_3d + y_3d * y_3d + z_3d * z_3d)))
    else:
        x_3d = np.zeros(0, dtype=np.float64)
        y_3d = np.zeros(0, dtype=np.float64)
        z_3d = np.zeros(0, dtype=np.float64)
        span_x = span_y = span_z = r_rms = 0.0

    return {
        "enabled": True,
        "sync_mode": sync_mode,
        "sync_dt_mean_ms": round(float(sync_dt_mean_ms), 2),
        "phase_coherent": sync_mode in ("hardware", "software"),
        "shared_dominant_hz": round(shared_hz, 3),
        "cam_b_dominant_hz": spec_b["dominant_hz"],
        "cam_b_snr_db": spec_b["snr_db"],
        "phase_shift_deg": round(phase_deg, 1),
        "time_lag_ms": round(lag_ms, 2),
        "coherence": round(coherence, 3),
        "cam_b_fft_magnitude": spec_b["fft_magnitude"],
        "cam_b_welch_psd": spec_b["welch_psd"],
        "cam_b_time_series": spec_b["time_series"],
        "orbit_3d": {
            "x_um": [round(float(v), 3) for v in x_3d],
            "y_um": [round(float(v), 3) for v in y_3d],
            "z_um": [round(float(v), 3) for v in z_3d],
            "span_x_um": round(span_x, 2),
            "span_y_um": round(span_y, 2),
            "span_z_um": round(span_z, 2),
            "radius_3d_rms_um": round(r_rms, 2),
            "unit": "µm",
        },
    }

