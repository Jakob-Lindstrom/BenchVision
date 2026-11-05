"""Temporal bandpass filters (Ideal FFT, zero-phase Butterworth SOS, causal IIR)."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt


def clamp_passband(low_hz: float, high_hz: float, fps: float) -> tuple[float, float]:
    """Ensure passband cutoffs lie strictly inside (0, nyquist)."""
    nyq = 0.5 * max(fps, 1.0)
    lo = max(0.02, min(low_hz, nyq * 0.90))
    hi = max(lo + 0.02, min(high_hz, nyq * 0.96))
    return lo, hi


def apply_temporal_bandpass(
    data: np.ndarray,
    fps: float,
    low_hz: float,
    high_hz: float,
    filter_type: str = "butterworth",
) -> np.ndarray:
    """Apply temporal bandpass filter along axis=0 of an N-dimensional array."""
    n_frames = data.shape[0]
    if n_frames < 4:
        return np.zeros_like(data, dtype=np.float32)

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    mean_frame = np.mean(data, axis=0, keepdims=True)
    centered = (data - mean_frame).astype(np.float32, copy=False)

    if filter_type == "ideal":
        freqs = np.fft.rfftfreq(n_frames, d=1.0 / fps)
        mask = (freqs >= lo) & (freqs <= hi)
        spec = np.fft.rfft(centered, axis=0)
        shape = [len(freqs)] + [1] * (centered.ndim - 1)
        spec *= mask.reshape(shape)
        return np.fft.irfft(spec, n=n_frames, axis=0).astype(np.float32)

    nyq = 0.5 * fps
    sos = butter(2, [lo / nyq, hi / nyq], btype="bandpass", output="sos")

    if filter_type == "iir" or n_frames < 15:
        return sosfilt(sos, centered, axis=0).astype(np.float32)

    return sosfiltfilt(sos, centered, axis=0).astype(np.float32)


def find_dominant_frequency(
    signal_1d: np.ndarray,
    fps: float,
    low_hz: float = 0.05,
    high_hz: float | None = None,
) -> tuple[float, float]:
    """Return (dominant_hz, dominant_bpm) within [low_hz, high_hz] using windowed RFFT."""
    n = len(signal_1d)
    if n < 4:
        return 0.0, 0.0
    lo, hi = clamp_passband(low_hz, high_hz or (0.48 * fps), fps)
    detrended = signal_1d - np.mean(signal_1d)
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(detrended * win))
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    valid_mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(valid_mask):
        return 0.0, 0.0
    sub_spec = np.where(valid_mask, spec, -1.0)
    idx = int(np.argmax(sub_spec))
    dom_hz = float(freqs[idx])
    return round(dom_hz, 4), round(dom_hz * 60.0, 2)
