"""Posy-style Motion Extraction plugin (time-shifted inverted-frame cancellation + bandpass gain)."""

from __future__ import annotations

import cv2
import numpy as np

from src.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationMethod,
    MagnificationOutput,
    ProgressCallback,
    alloc_motion_energy,
    shrink_energy,
)
from src.processing.filters import (
    apply_temporal_bandpass,
    find_dominant_frequency,
    normalize_motion_energy_inplace,
)


def compute_posy_frame_shift(fps: float, low_hz: float, high_hz: float, n_frames: int) -> int:
    """Compute the half-period frame offset k = round(fps / (2 * f_center)) for maximum cancellation."""
    f_center = max(0.2, 0.5 * (low_hz + high_hz))
    half_period_frames = max(1, round(fps / (2.0 * f_center)))
    return int(max(1, min(half_period_frames, max(1, n_frames // 3))))


@PROCESSING_REGISTRY.register("motion_extraction")
class MotionExtractionMethod(MagnificationMethod):
    """Posy-style Motion Extraction (time-shifted inverted overlay + bandpass amplification).

    Static background structures cancel out to flat 50% neutral gray (RGB 128, 128, 128),
    leaving only moving edges, vibrations, and periodic changes visible.
    """

    method_key = "motion_extraction"
    display_name = "Motion Extraction (Posy Time-Shift Cancellation)"
    description = (
        "Cancels all stationary background details to flat neutral gray (128) by blending "
        "a time-shifted inverted copy (Posy method) with bandpass contrast amplification."
    )
    speed_rating = "very_fast"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Cancelled before Motion Extraction")

        frame_shift = compute_posy_frame_shift(fps, params.low_hz, params.high_hz, n_frames)
        # At alpha <= 5, gain = 1.0 (pure Posy 50% opacity inverted blend); higher alpha boosts subtle motion
        contrast_gain = max(1.0, float(params.alpha) / 5.0)

        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)
        raw_roi_sig = np.zeros(n_frames, dtype=np.float64)

        # Optional sub-band bandpass component at half resolution to highlight periodic motion cleanly
        sub_h = max(8, h // 2)
        sub_w = max(8, w // 2)
        sub_luma = np.empty((n_frames, sub_h, sub_w), dtype=np.float32)

        for t in range(n_frames):
            curr_f = frames_bgr[t].astype(np.float32)
            if params.spatial_blur_sigma > 0.5:
                curr_f = cv2.GaussianBlur(curr_f, (0, 0), params.spatial_blur_sigma * 0.5)
            luma = 0.114 * curr_f[..., 0] + 0.587 * curr_f[..., 1] + 0.299 * curr_f[..., 2]
            raw_roi_sig[t] = float(np.mean(luma[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]))
            sub_luma[t] = cv2.resize(luma, (sub_w, sub_h), interpolation=cv2.INTER_AREA)

        if progress_cb and not progress_cb(0.35):
            raise JobCancelledError("Cancelled during Motion Extraction pre-pass")

        apply_temporal_bandpass(
            sub_luma,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
            out=sub_luma,
        )

        if progress_cb and not progress_cb(0.65):
            raise JobCancelledError("Cancelled after Motion Extraction bandpass")

        for t in range(n_frames):
            t_prev = max(0, t - frame_shift)
            curr_f = frames_bgr[t].astype(np.float32)
            prev_f = frames_bgr[t_prev].astype(np.float32)

            if params.spatial_blur_sigma > 0.5:
                sigma = params.spatial_blur_sigma * 0.5
                curr_f = cv2.GaussianBlur(curr_f, (0, 0), sigma)
                prev_f = cv2.GaussianBlur(prev_f, (0, 0), sigma)

            # Posy core formula: 0.5 * I(t) + 0.5 * (255 - I(t - k)) = 127.5 + 0.5 * (I(t) - I(t - k))
            posy_delta = 0.5 * (curr_f - prev_f)
            bp_delta = cv2.resize(sub_luma[t], (w, h), interpolation=cv2.INTER_LINEAR)[..., None]

            total_delta = contrast_gain * (0.7 * posy_delta + 0.3 * bp_delta)
            extracted = np.clip(128.0 + total_delta, 0.0, 255.0).astype(np.uint8)
            out_frames[t] = extracted
            motion_energy[t] = shrink_energy(np.mean(np.abs(total_delta), axis=2), motion_energy)

        del sub_luma
        normalize_motion_energy_inplace(motion_energy)

        if n_frames > 4:
            raw_roi_sig[:4] = raw_roi_sig[4]
        filtered_sig = apply_temporal_bandpass(
            raw_roi_sig,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        ).astype(np.float64)

        dom_hz, dom_bpm = find_dominant_frequency(
            raw_roi_sig, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz
        )

        if progress_cb:
            progress_cb(0.95)

        return MagnificationOutput(
            magnified_bgr=out_frames,
            motion_energy=motion_energy,
            raw_roi_signal=raw_roi_sig,
            filtered_roi_signal=filtered_sig,
            timestamps_s=timestamps_s,
            achieved_fps=fps,
            dominant_hz=dom_hz,
            dominant_bpm=dom_bpm,
            extra_metrics={
                "method": self.method_key,
                "frame_shift_k": frame_shift,
                "contrast_gain": round(contrast_gain, 2),
            },
        )


@PROCESSING_REGISTRY.register("posy_rgb_trail")
class PosyRgbTrailMethod(MagnificationMethod):
    """Posy-style RGB Chrono-Chromatic Trail: R = Frame t, G = Frame t-k, B = Frame t-2k."""

    method_key = "posy_rgb_trail"
    display_name = "Posy RGB Chrono-Chromatic Trail (Rainbow Motion)"
    description = (
        "Maps Red = Frame t, Green = Frame t-k, Blue = Frame t-2k so stationary regions "
        "stay normal color while moving or vibrating edges split into rainbow trails."
    )
    speed_rating = "very_fast"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        if progress_cb and not progress_cb(0.1):
            raise JobCancelledError("Cancelled before RGB Trail")

        k = compute_posy_frame_shift(fps, params.low_hz, params.high_hz, n_frames)
        gain = max(1.0, float(params.alpha) / 8.0)

        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)
        raw_roi_sig = np.zeros(n_frames, dtype=np.float64)

        for t in range(n_frames):
            t1 = max(0, t - k)
            t2 = max(0, t - 2 * k)
            f0 = frames_bgr[t].astype(np.float32)
            f1 = frames_bgr[t1].astype(np.float32)
            f2 = frames_bgr[t2].astype(np.float32)

            luma0 = 0.114 * f0[..., 0] + 0.587 * f0[..., 1] + 0.299 * f0[..., 2]
            luma1 = 0.114 * f1[..., 0] + 0.587 * f1[..., 1] + 0.299 * f1[..., 2]
            luma2 = 0.114 * f2[..., 0] + 0.587 * f2[..., 1] + 0.299 * f2[..., 2]
            raw_roi_sig[t] = float(np.mean(luma0[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]))

            # Stationary pixels keep natural color; temporal differences split into R (t), G (t-k), B (t-2k)
            r_ch = np.clip(f1[..., 2] + gain * (luma0 - luma1), 0.0, 255.0)
            g_ch = np.clip(f1[..., 1] + 0.5 * gain * (luma1 - 0.5 * (luma0 + luma2)), 0.0, 255.0)
            b_ch = np.clip(f1[..., 0] + gain * (luma2 - luma1), 0.0, 255.0)

            out_frames[t, ..., 0] = b_ch.astype(np.uint8)
            out_frames[t, ..., 1] = g_ch.astype(np.uint8)
            out_frames[t, ..., 2] = r_ch.astype(np.uint8)
            motion_energy[t] = shrink_energy(0.5 * (np.abs(luma0 - luma1) + np.abs(luma1 - luma2)), motion_energy)

        if progress_cb and not progress_cb(0.8):
            raise JobCancelledError("Cancelled during RGB Trail")

        normalize_motion_energy_inplace(motion_energy)
        if n_frames > 4:
            raw_roi_sig[:4] = raw_roi_sig[4]
        filtered_sig = apply_temporal_bandpass(
            raw_roi_sig,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        ).astype(np.float64)
        dom_hz, dom_bpm = find_dominant_frequency(
            raw_roi_sig, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz
        )

        if progress_cb:
            progress_cb(0.95)

        return MagnificationOutput(
            magnified_bgr=out_frames,
            motion_energy=motion_energy,
            raw_roi_signal=raw_roi_sig,
            filtered_roi_signal=filtered_sig,
            timestamps_s=timestamps_s,
            achieved_fps=fps,
            dominant_hz=dom_hz,
            dominant_bpm=dom_bpm,
            extra_metrics={"method": self.method_key, "frame_shift_k": k, "trail_gain": round(gain, 2)},
        )


@PROCESSING_REGISTRY.register("posy_peak_hold")
class PosyPeakHoldMethod(MagnificationMethod):
    """Posy Motion Persistence: accumulates frame differences over a decaying window."""

    method_key = "posy_peak_hold"
    display_name = "Posy Motion Persistence (Peak-Hold Envelope)"
    description = (
        "Accumulates Posy time-shifted frame differences over an exponentially decaying window "
        "so rapid vibrations leave a glowing motion envelope rather than flickering."
    )
    speed_rating = "very_fast"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        if progress_cb and not progress_cb(0.1):
            raise JobCancelledError("Cancelled before Peak-Hold")

        k = compute_posy_frame_shift(fps, params.low_hz, params.high_hz, n_frames)
        gain = max(1.0, float(params.alpha) / 5.0)
        decay = 0.84

        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)
        raw_roi_sig = np.zeros(n_frames, dtype=np.float64)
        envelope = np.zeros((h, w, 3), dtype=np.float32)

        for t in range(n_frames):
            t_prev = max(0, t - k)
            curr_f = frames_bgr[t].astype(np.float32)
            prev_f = frames_bgr[t_prev].astype(np.float32)
            luma = 0.114 * curr_f[..., 0] + 0.587 * curr_f[..., 1] + 0.299 * curr_f[..., 2]
            raw_roi_sig[t] = float(np.mean(luma[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]))

            delta = curr_f - prev_f
            abs_delta = np.abs(delta) * gain
            envelope = np.maximum(envelope * decay, abs_delta)

            # Blend 50% gray Posy cancellation with glowing cyan/amber persistence envelope
            posy_base = 128.0 + 0.45 * gain * delta
            glow = np.empty_like(envelope)
            glow[..., 0] = envelope[..., 0] * 1.15  # Blue/Cyan highlight
            glow[..., 1] = envelope[..., 1] * 0.95
            glow[..., 2] = envelope[..., 2] * 0.55
            out_frames[t] = np.clip(posy_base + 0.65 * glow, 0.0, 255.0).astype(np.uint8)
            motion_energy[t] = shrink_energy(np.mean(envelope, axis=2), motion_energy)

        if progress_cb and not progress_cb(0.85):
            raise JobCancelledError("Cancelled during Peak-Hold")

        normalize_motion_energy_inplace(motion_energy)
        if n_frames > 4:
            raw_roi_sig[:4] = raw_roi_sig[4]
        filtered_sig = apply_temporal_bandpass(
            raw_roi_sig,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        ).astype(np.float64)
        dom_hz, dom_bpm = find_dominant_frequency(
            raw_roi_sig, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz
        )

        if progress_cb:
            progress_cb(0.95)

        return MagnificationOutput(
            magnified_bgr=out_frames,
            motion_energy=motion_energy,
            raw_roi_signal=raw_roi_sig,
            filtered_roi_signal=filtered_sig,
            timestamps_s=timestamps_s,
            achieved_fps=fps,
            dominant_hz=dom_hz,
            dominant_bpm=dom_bpm,
            extra_metrics={"method": self.method_key, "frame_shift_k": k, "decay": decay},
        )

