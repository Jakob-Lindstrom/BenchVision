"""Low-memory chunked temporal bandpass filters (Ideal FFT, Butterworth SOS, causal IIR)."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt


def clamp_passband(low_hz: float, high_hz: float, fps: float) -> tuple[float, float]:
    """Ensure passband cutoffs lie strictly inside (0, nyquist)."""
    nyq = 0.5 * max(fps, 1.0)
    lo = max(0.02, min(low_hz, nyq * 0.90))
    hi = max(lo + 0.02, min(high_hz, nyq * 0.96))
    return lo, hi


def sanitize_1d_signal(signal_1d: np.ndarray) -> np.ndarray:
    """Back-fill the first 4 frames (signal[:4] = signal[4]) to eliminate Frame-0 reference/exposure jumps."""
    sig = np.asarray(signal_1d, dtype=np.float64)
    if sig.ndim == 1 and len(sig) > 4:
        if isinstance(signal_1d, np.ndarray) and signal_1d.ndim == 1 and signal_1d.flags.writeable:
            signal_1d[:4] = signal_1d[4]
        sig = sig.copy()
        sig[:4] = sig[4]
    return sig


def apply_temporal_bandpass(
    data: np.ndarray,
    fps: float,
    low_hz: float,
    high_hz: float,
    filter_type: str = "butterworth",
    out: np.ndarray | None = None,
    chunk_cols: int = 4096,
) -> np.ndarray:
    """Apply temporal bandpass filter along axis=0 in small spatial chunks (< 5 MB temp RAM)."""
    n_frames = data.shape[0]
    if data.ndim == 1 and n_frames > 4 and data.flags.writeable:
        data[:4] = data[4]

    if out is None:
        out = np.empty(data.shape, dtype=np.float32)

    if n_frames < 4:
        out.fill(0.0)
        return out

    lo, hi = clamp_passband(low_hz, high_hz, fps)
    orig_shape = data.shape
    n_cols = int(np.prod(orig_shape[1:])) if data.ndim > 1 else 1

    flat_in = data.reshape(n_frames, n_cols)
    flat_out = out.reshape(n_frames, n_cols)

    if filter_type == "ideal":
        freqs = np.fft.rfftfreq(n_frames, d=1.0 / fps)
        mask = ((freqs >= lo) & (freqs <= hi)).astype(np.float32)[:, None]
        for c0 in range(0, n_cols, chunk_cols):
            c1 = min(c0 + chunk_cols, n_cols)
            sub = flat_in[:, c0:c1].astype(np.float32, copy=True)
            if n_cols == 1 and n_frames > 4:
                sub[:4] = sub[4]
            centered = sub - np.mean(sub, axis=0, keepdims=True)
            spec = np.fft.rfft(centered, axis=0)
            spec *= mask
            flat_out[:, c0:c1] = np.fft.irfft(spec, n=n_frames, axis=0).astype(np.float32)
        return out

    nyq = 0.5 * fps
    sos = butter(2, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    use_iir = filter_type == "iir" or n_frames < 15
    padlen = min(n_frames - 1, 3 * max(len(sos) * 6, 1))

    for c0 in range(0, n_cols, chunk_cols):
        c1 = min(c0 + chunk_cols, n_cols)
        sub = flat_in[:, c0:c1].astype(np.float32, copy=(n_cols == 1))
        if n_cols == 1 and n_frames > 4:
            sub[:4] = sub[4]
        centered = sub - np.mean(sub, axis=0, keepdims=True)
        if use_iir:
            filtered = sosfilt(sos, centered, axis=0)
            if n_cols == 1 and n_frames > 4:
                filtered[:4] = filtered[4]
        else:
            filtered = sosfiltfilt(
                sos, centered, axis=0, padtype="odd", padlen=padlen
            )
        if n_cols == 1 and n_frames > 6:
            p95 = float(np.percentile(np.abs(filtered[4:]), 95))
            if p95 > 1e-9:
                filtered[:4] = np.clip(filtered[:4], -p95, p95)
        flat_out[:, c0:c1] = filtered.astype(np.float32, copy=False)

    return out


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
    sig = sanitize_1d_signal(signal_1d)
    lo, hi = clamp_passband(low_hz, high_hz or (0.48 * fps), fps)
    detrended = sig - np.mean(sig)
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


def normalize_motion_energy_inplace(motion_energy: np.ndarray) -> np.ndarray:
    """Normalize a 3D (T, H, W) motion_energy array to [0, 1] in-place with < 1 MB temp RAM."""
    if motion_energy.size == 0:
        return motion_energy
    t_step = max(1, motion_energy.shape[0] // 16)
    y_step = max(1, motion_energy.shape[1] // 64)
    x_step = max(1, motion_energy.shape[2] // 64)
    sample = motion_energy[::t_step, ::y_step, ::x_step]
    max_val = float(np.max(sample)) if sample.size > 0 else 0.0
    if max_val <= 1e-6:
        return motion_energy
    p995 = float(np.percentile(sample, 99.5))
    denom = p995 if p995 > 1e-6 else max_val
    np.multiply(motion_energy, 1.0 / denom, out=motion_energy)
    np.clip(motion_energy, 0.0, 1.0, out=motion_energy)
    return motion_energy
