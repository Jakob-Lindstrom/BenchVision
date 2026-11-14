"""Low-memory Riesz Pyramid Phase-Based Video Magnification (Wadhwa et al., ICCP 2014)."""

from __future__ import annotations

import gc

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


def compute_riesz_strip(
    strip_stack: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Riesz amplitude A and quaternionic phase (qx, qy) for a (T, H_s, W) strip."""
    # Horizontal derivative [-0.5, 0, 0.5] with reflect padding
    padded_x = np.pad(strip_stack, ((0, 0), (0, 0), (1, 1)), mode="reflect")
    r1 = 0.5 * (padded_x[:, :, 2:] - padded_x[:, :, :-2])

    # Vertical derivative [-0.5, 0, 0.5] with reflect padding
    padded_y = np.pad(strip_stack, ((0, 0), (1, 1), (0, 0)), mode="reflect")
    r2 = 0.5 * (padded_y[:, 2:, :] - padded_y[:, :-2, :])

    r_norm = np.hypot(r1, r2) + 1e-7
    amp = np.hypot(strip_stack, r_norm) + 1e-7
    phi = np.arctan2(r_norm, strip_stack)
    qx = (phi * (r1 / r_norm)).astype(np.float32, copy=False)
    qy = (phi * (r2 / r_norm)).astype(np.float32, copy=False)
    return amp.astype(np.float32, copy=False), qx, qy


@PROCESSING_REGISTRY.register("riesz")
class RieszPyramidMagnification(MagnificationMethod):
    """Riesz Pyramid Phase-Based Video Magnification (strip-chunked for low Pi RAM)."""

    method_key = "riesz"
    display_name = "Riesz Pyramid Phase Magnification (Fast Pi Method)"
    description = (
        "Uses a 3-tap Riesz transform on a Laplacian pyramid to amplify local phase shifts "
        "rather than raw brightness. Fast on Raspberry Pi with fewer halo artifacts."
    )
    speed_rating = "fast"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        levels = max(2, min(params.pyramid_levels, 5))

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Cancelled at start of Riesz processing")

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
            for lvl_idx, b in enumerate(active_slice):
                pyr_bands[lvl_idx][t] = b

        if progress_cb and not progress_cb(0.30):
            raise JobCancelledError("Cancelled after Riesz pyramid construction")

        n_band_levels = len(pyr_bands)
        phase_roi_trace = np.zeros(n_frames, dtype=np.float64)
        strip_rows = 64

        for lvl_idx in range(n_band_levels):
            band = pyr_bands[lvl_idx]
            _, lh, _lw = band.shape
            level_scale = 0.6 if lvl_idx == 0 else 1.0
            eff_alpha = float(params.alpha * level_scale)

            for r0 in range(0, lh, strip_rows):
                r1 = min(r0 + strip_rows, lh)
                orig_strip = band[:, r0:r1, :].copy()
                amp, qx, qy = compute_riesz_strip(orig_strip)

                if (lvl_idx in (1, 2) or n_band_levels == 1) and (r0 <= lh // 2 < r1):
                    dqx_mean = np.mean(qx - np.mean(qx, axis=0, keepdims=True), axis=(1, 2))
                    phase_roi_trace += dqx_mean.astype(np.float64)

                bp_qx = apply_temporal_bandpass(
                    qx,
                    fps=fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    filter_type=params.filter_type,
                )
                bp_qy = apply_temporal_bandpass(
                    qy,
                    fps=fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    filter_type=params.filter_type,
                )

                if params.spatial_blur_sigma > 0:
                    sigma = params.spatial_blur_sigma
                    for t in range(n_frames):
                        w_num_x = cv2.GaussianBlur(bp_qx[t] * amp[t], (0, 0), sigma)
                        w_num_y = cv2.GaussianBlur(bp_qy[t] * amp[t], (0, 0), sigma)
                        w_den = cv2.GaussianBlur(amp[t], (0, 0), sigma) + 1e-6
                        bp_qx[t] = w_num_x / w_den
                        bp_qy[t] = w_num_y / w_den

                new_phi = np.hypot(qx + eff_alpha * bp_qx, qy + eff_alpha * bp_qy)
                shifted_strip = amp * np.cos(new_phi)

                # In-place bandpass on orig_strip for subtle intensity component
                apply_temporal_bandpass(
                    orig_strip,
                    fps=fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                    filter_type=params.filter_type,
                    out=orig_strip,
                )
                # Store only the delta (magnified - original) in band[:, r0:r1, :]
                band[:, r0:r1, :] = (
                    shifted_strip - band[:, r0:r1, :]
                ) + 0.25 * eff_alpha * orig_strip

            if progress_cb and not progress_cb(0.30 + 0.50 * ((lvl_idx + 1) / n_band_levels)):
                raise JobCancelledError("Cancelled during Riesz temporal filtering")

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
            phase_roi_trace[:4] = phase_roi_trace[4]
        raw_sig = (luma_roi - np.mean(luma_roi)) + 10.0 * (
            phase_roi_trace - np.mean(phase_roi_trace)
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
                "pyramid_levels": n_band_levels,
                "alpha": params.alpha,
            },
        )
