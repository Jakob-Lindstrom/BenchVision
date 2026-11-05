"""FFT, Welch PSD, STFT spectrogram, and auto-find dominant passband recommender."""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.signal import spectrogram, welch

from benchvision.processing.filters import clamp_passband, find_dominant_frequency


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
    detrended = signal_1d - np.mean(signal_1d)
    win = np.hanning(n)
    fft_vals = np.abs(np.fft.rfft(detrended * win)) / max(n, 1)
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / fps)

    nperseg = min(n, max(16, round(fps * 3.0)))
    w_freqs, w_psd = welch(detrended, fs=fps, nperseg=nperseg)

    # Compute STFT spectrogram for time-frequency plot
    spec_nperseg = min(n, max(16, round(fps * 2.0)))
    spec_noverlap = spec_nperseg // 2
    s_freqs, s_times, s_xx = spectrogram(
        detrended, fs=fps, nperseg=spec_nperseg, noverlap=spec_noverlap
    )
    s_db = 10.0 * np.log10(np.maximum(s_xx, 1e-12))

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
        "spectrogram": {
            "times_s": [round(float(t), 3) for t in s_times],
            "freqs_hz": [round(float(f), 3) for f in s_freqs],
            "power_db": [
                [round(float(val), 2) for val in row] for row in s_db
            ],
        },
        "suggested_band": {
            "low_hz": round(sug_lo, 2),
            "high_hz": round(sug_hi, 2),
            "peak_hz": auto_peak_hz,
            "peak_bpm": auto_peak_bpm,
        },
        "time_series": {
            "t_s": [round(float(t), 4) for t in timestamps_s],
            "raw": [round(float(x), 4) for x in detrended],
            "filtered": [round(float(x), 4) for x in filtered_1d],
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
    combined = (green_mean - np.mean(green_mean)) + 4.0 * (gx - np.mean(gx))
    timestamps_s = np.arange(n, dtype=np.float64) / max(fps, 1.0)

    return compute_signal_spectrum(
        signal_1d=combined.astype(np.float64),
        filtered_1d=combined.astype(np.float64),
        timestamps_s=timestamps_s,
        fps=fps,
        low_hz=low_hz,
        high_hz=high_hz,
    )
