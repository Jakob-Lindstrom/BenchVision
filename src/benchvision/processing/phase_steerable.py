"""Phase-Based Video Magnification using Complex Steerable / Quadrature Filter Pyramids."""

from __future__ import annotations

import math

import cv2
import numpy as np

from benchvision.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationMethod,
    MagnificationOutput,
    ProgressCallback,
)
from benchvision.processing.eulerian import build_laplacian_pyramid, collapse_laplacian_pyramid
from benchvision.processing.filters import apply_temporal_bandpass, find_dominant_frequency


def make_quadrature_steerable_kernels(
    orientations: int = 4, ksize: int = 9, sigma: float = 2.0, wavelength: float = 5.0
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Create oriented complex quadrature (even cosine / odd sine) filter pairs."""
    pairs: list[tuple[np.ndarray, np.ndarray]] = []
    half = ksize // 2
    y, x = np.mgrid[-half : half + 1, -half : half + 1].astype(np.float32)
    gauss = np.exp(-(x**2 + y**2) / (2.0 * sigma**2))

    for i in range(orientations):
        theta = i * (math.pi / orientations)
        x_theta = x * math.cos(theta) + y * math.sin(theta)
        k_even = gauss * np.cos(2.0 * math.pi * x_theta / wavelength)
        k_even -= np.mean(k_even)
        k_even /= np.linalg.norm(k_even) + 1e-8

        k_odd = gauss * np.sin(2.0 * math.pi * x_theta / wavelength)
        k_odd -= np.mean(k_odd)
        k_odd /= np.linalg.norm(k_odd) + 1e-8
        pairs.append((k_even.astype(np.float32), k_odd.astype(np.float32)))
    return pairs


@PROCESSING_REGISTRY.register("phase_steerable")
class ComplexSteerablePhaseMagnification(MagnificationMethod):
    """Phase-Based Video Magnification (Complex Steerable / Quadrature Pyramid, SIGGRAPH 2013)."""

    method_key = "phase_steerable"
    display_name = "Phase-Based Magnification (Complex Steerable Pyramid)"
    description = (
        "Decomposes each spatial scale into 4 oriented complex quadrature subbands, "
        "bandpasses local phase over time, and shifts phase to magnify subtle motion."
    )
    speed_rating = "thorough"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        levels = max(1, min(params.pyramid_levels, 4))
        kernels = make_quadrature_steerable_kernels(orientations=4)

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Cancelled at start of Phase Steerable processing")

        ycrcb_frames = np.empty((n_frames, h, w, 3), dtype=np.float32)
        pyramids_by_level: list[list[np.ndarray]] = []

        for t in range(n_frames):
            ycrcb = cv2.cvtColor(frames_bgr[t].astype(np.float32), cv2.COLOR_BGR2YCrCb)
            ycrcb_frames[t] = ycrcb
            pyr = build_laplacian_pyramid(ycrcb[..., 0], levels)
            if t == 0:
                pyramids_by_level = [[b] for b in pyr]
            else:
                for idx, b in enumerate(pyr):
                    pyramids_by_level[idx].append(b)

        n_pyr_levels = len(pyramids_by_level)
        magnified_levels: list[np.ndarray] = []
        phase_trace = np.zeros(n_frames, dtype=np.float64)

        for lvl_idx in range(n_pyr_levels - 1):
            band_stack = np.stack(pyramids_by_level[lvl_idx], axis=0)  # (T, H_l, W_l)
            lh, lw = band_stack.shape[1:3]
            level_delta = np.zeros_like(band_stack, dtype=np.float32)
            level_scale = 0.65 if lvl_idx == 0 else 1.0
            eff_alpha = params.alpha * level_scale

            for ori_idx, (k_even, k_odd) in enumerate(kernels):
                re_stack = np.empty_like(band_stack)
                im_stack = np.empty_like(band_stack)
                for t in range(n_frames):
                    re_stack[t] = cv2.filter2D(
                        band_stack[t], cv2.CV_32F, k_even, borderType=cv2.BORDER_REFLECT101
                    )
                    im_stack[t] = cv2.filter2D(
                        band_stack[t], cv2.CV_32F, k_odd, borderType=cv2.BORDER_REFLECT101
                    )

                amp = np.hypot(re_stack, im_stack) + 1e-7
                phase = np.arctan2(im_stack, re_stack)
                # Unwrap temporal phase relative to frame 0
                dphase = np.unwrap(phase - phase[0:1], axis=0).astype(np.float32)

                bp_phase = apply_temporal_bandpass(
                    dphase,
                    fps=fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    filter_type=params.filter_type,
                )

                if params.spatial_blur_sigma > 0:
                    sigma = params.spatial_blur_sigma
                    for t in range(n_frames):
                        num = cv2.GaussianBlur(bp_phase[t] * amp[t], (0, 0), sigma)
                        den = cv2.GaussianBlur(amp[t], (0, 0), sigma) + 1e-6
                        bp_phase[t] = num / den

                if ori_idx == 0 and (lvl_idx == 1 or n_pyr_levels <= 2):
                    roi_p = dphase[:, lh // 4 : 3 * lh // 4, lw // 4 : 3 * lw // 4]
                    phase_trace += np.mean(roi_p, axis=(1, 2)).astype(np.float64)

                shifted_phase = phase + eff_alpha * bp_phase
                shifted_re = amp * np.cos(shifted_phase)
                level_delta += (shifted_re - re_stack) * 0.5

            bp_luma = apply_temporal_bandpass(
                band_stack,
                fps=fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                filter_type=params.filter_type,
            )
            magnified_levels.append(band_stack + level_delta + 0.2 * eff_alpha * bp_luma)

            if progress_cb and not progress_cb(0.20 + 0.65 * ((lvl_idx + 1) / (n_pyr_levels - 1))):
                raise JobCancelledError("Cancelled during Phase Steerable filtering")

        magnified_levels.append(np.stack(pyramids_by_level[-1], axis=0))

        out_frames = np.empty_like(frames_bgr)
        motion_energy = np.zeros((n_frames, h, w), dtype=np.float32)

        for t in range(n_frames):
            frame_pyr = [magnified_levels[lvl_idx][t] for lvl_idx in range(n_pyr_levels)]
            recon_luma = collapse_laplacian_pyramid(frame_pyr)
            orig_luma = ycrcb_frames[t, ..., 0]
            motion_energy[t] = np.abs(recon_luma - orig_luma)

            ycrcb_out = ycrcb_frames[t].copy()
            ycrcb_out[..., 0] = np.clip(recon_luma, 0, 255)
            bgr_out = cv2.cvtColor(ycrcb_out, cv2.COLOR_YCrCb2BGR)
            out_frames[t] = np.clip(bgr_out, 0, 255).astype(np.uint8)

        max_e = float(np.percentile(motion_energy, 99.5)) if np.any(motion_energy > 0) else 1.0
        if max_e > 1e-6:
            motion_energy = np.clip(motion_energy / max_e, 0.0, 1.0)

        luma_roi = np.mean(
            ycrcb_frames[:, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 0], axis=(1, 2)
        ).astype(np.float64)
        raw_sig = (luma_roi - np.mean(luma_roi)) + 12.0 * phase_trace
        filtered_sig = apply_temporal_bandpass(
            raw_sig,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        ).astype(np.float64)

        dom_hz, dom_bpm = find_dominant_frequency(
            raw_sig, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz
        )

        if progress_cb:
            progress_cb(0.95)

        return MagnificationOutput(
            magnified_bgr=out_frames,
            motion_energy=motion_energy,
            raw_roi_signal=raw_sig,
            filtered_roi_signal=filtered_sig,
            timestamps_s=timestamps_s,
            achieved_fps=fps,
            dominant_hz=dom_hz,
            dominant_bpm=dom_bpm,
            extra_metrics={
                "method": self.method_key,
                "orientations": 4,
                "pyramid_levels": n_pyr_levels - 1,
                "alpha": params.alpha,
            },
        )
