"""Spectral analysis, Welch PSD, STFT spectrogram, and automatic frequency band detection."""

from benchvision.analysis.spectrum import (
    compute_roi_spectrum_analysis,
    compute_signal_spectrum,
)

__all__ = [
    "compute_roi_spectrum_analysis",
    "compute_signal_spectrum",
]
