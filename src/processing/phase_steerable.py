"""Low-memory Phase-Based Video Magnification (Complex Steerable / Quadrature Pyramid)."""

from __future__ import annotations

import gc
import math

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
from src.processing.eulerian import build_laplacian_pyramid, collapse_laplacian_pyramid
from src.processing.filters import (
    apply_temporal_bandpass,
    find_dominant_frequency,
    normalize_motion_energy_inplace,
)


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
        levels = max(2, min(params.pyramid_levels, 4))
        kernels = make_quadrature_steerable_kernels(orientations=4)

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Cancelled at start of Phase Steerable processing")

        pyr_bands: list[np.ndarray] = []
        residual_shape: tuple[int, ...] = ()
        skip_finest = True
        luma_roi = np.zeros(n_frames, dtype=np.float64)

        for t in range(n_frames):
            ycrcb = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2YCrCb)
            luma = ycrcb[..., 0].astype(np.float32)
            luma_roi[t] = float(np.mean(luma[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]))
            pyr = build_laplacian_pyramid(luma, levels)
            if t == 0:
                skip_finest = len(pyr) >= 3
                active_slice = pyr[1:-1] if skip_finest else pyr[:-1]
                pyr_bands = [
                    np.empty((n_frames, *b.shape), dtype=np.float32) for b in active_slice
                ]
                residual_shape = pyr[-1].shape
            active_slice = pyr[1:-1] if skip_finest else pyr[:-1]
            for idx, b in enumerate(active_slice):
                pyr_bands[idx][t] = b

        n_band_levels = len(pyr_bands)
        phase_trace = np.zeros(n_frames, dtype=np.float64)
        strip_rows = 64
        pad = 4  # half of 9x9 kernel

        for lvl_idx in range(n_band_levels):
            band_stack = pyr_bands[lvl_idx]
            _, lh, lw = band_stack.shape
            level_delta = np.zeros_like(band_stack, dtype=np.float32)
            level_scale = 0.65 if lvl_idx == 0 else 1.0
            eff_alpha = float(params.alpha * level_scale)

            for r0 in range(0, lh, strip_rows):
                r1 = min(r0 + strip_rows, lh)
                pr0 = max(0, r0 - pad)
                pr1 = min(lh, r1 + pad)
                top_crop = r0 - pr0
                bot_crop = top_crop + (r1 - r0)

                padded_strip = band_stack[:, pr0:pr1, :]
                sh = r1 - r0

                for ori_idx, (k_even, k_odd) in enumerate(kernels):
                    re_s = np.empty((n_frames, sh, lw), dtype=np.float32)
                    im_s = np.empty((n_frames, sh, lw), dtype=np.float32)
                    for t in range(n_frames):
                        re_full = cv2.filter2D(
                            padded_strip[t], cv2.CV_32F, k_even, borderType=cv2.BORDER_REFLECT101
                        )
                        im_full = cv2.filter2D(
                            padded_strip[t], cv2.CV_32F, k_odd, borderType=cv2.BORDER_REFLECT101
                        )
                        re_s[t] = re_full[top_crop:bot_crop]
                        im_s[t] = im_full[top_crop:bot_crop]

                    amp = np.hypot(re_s, im_s) + 1e-7
                    phase = np.arctan2(im_s, re_s)
                    dphase = (
                        (phase - phase[0:1] + np.float32(np.pi)) % np.float32(2.0 * np.pi)
                        - np.float32(np.pi)
                    )

                    if (
                        ori_idx == 0
                        and (lvl_idx == 1 or n_band_levels == 1)
                        and (r0 <= lh // 2 < r1)
                    ):
                        phase_trace += np.mean(dphase, axis=(1, 2)).astype(np.float64)

                    apply_temporal_bandpass(
                        dphase,
                        fps=fps,
                        low_hz=params.low_hz,
                        high_hz=params.high_hz,
                        filter_type=params.filter_type,
                        out=dphase,
                    )

                    if params.spatial_blur_sigma > 0:
                        sigma = params.spatial_blur_sigma
                        for t in range(n_frames):
                            num = cv2.GaussianBlur(dphase[t] * amp[t], (0, 0), sigma)
                            den = cv2.GaussianBlur(amp[t], (0, 0), sigma) + 1e-6
                            dphase[t] = num / den

                    shifted_re = amp * np.cos(phase + eff_alpha * dphase)
                    level_delta[:, r0:r1, :] += (shifted_re - re_s) * 0.5

                strip_prog = (lvl_idx + (r1 / max(lh, 1))) / max(n_band_levels, 1)
                if progress_cb and not progress_cb(0.20 + 0.65 * strip_prog):
                    raise JobCancelledError("Cancelled during Phase Steerable filtering")

            # In-place bandpass on band_stack for subtle luma component, then add level_delta
            apply_temporal_bandpass(
                band_stack,
                fps=fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                filter_type=params.filter_type,
                out=band_stack,
            )
            band_stack *= 0.2 * eff_alpha
            band_stack += level_delta
            del level_delta

            if progress_cb and not progress_cb(0.20 + 0.65 * ((lvl_idx + 1) / n_band_levels)):
                raise JobCancelledError("Cancelled during Phase Steerable filtering")

        zero_residual = np.zeros(residual_shape, dtype=np.float32)
        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)

        for t in range(n_frames):
            delta_pyr = [pyr_bands[lvl_idx][t] for lvl_idx in range(n_band_levels)] + [
                zero_residual
            ]
            delta_sub = collapse_laplacian_pyramid(delta_pyr)
            delta_luma = (
                cv2.pyrUp(delta_sub, dstsize=(w, h)) if skip_finest else delta_sub
            )
            motion_energy[t] = shrink_energy(np.abs(delta_luma), motion_energy)

            ycrcb_out = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2YCrCb).astype(np.float32)
            ycrcb_out[..., 0] = np.clip(ycrcb_out[..., 0] + delta_luma, 0, 255)
            bgr_out = cv2.cvtColor(ycrcb_out.astype(np.uint8), cv2.COLOR_YCrCb2BGR)
            out_frames[t] = bgr_out

        del pyr_bands
        gc.collect()
        normalize_motion_energy_inplace(motion_energy)

        if n_frames > 4:
            luma_roi[:4] = luma_roi[4]
            phase_trace[:4] = phase_trace[4]
        raw_sig = (luma_roi - np.mean(luma_roi)) + 12.0 * (
            phase_trace - np.mean(phase_trace)
        )
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
                "pyramid_levels": n_band_levels,
                "alpha": params.alpha,
            },
        )
