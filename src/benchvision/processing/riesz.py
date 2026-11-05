"""Riesz Pyramid Phase-Based Video Magnification (Wadhwa et al., ICCP 2014) — fast for Raspberry Pi."""

from __future__ import annotations

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


def compute_riesz_triple(band: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute 3-tap Riesz transform (R1, R2), local amplitude A, and quaternionic phase components."""
    # 3-tap derivative kernels [-0.5, 0, 0.5]
    kx = np.array([[-0.5, 0.0, 0.5]], dtype=np.float32)
    ky = np.array([[-0.5], [0.0], [0.5]], dtype=np.float32)
    r1 = cv2.filter2D(band, cv2.CV_32F, kx, borderType=cv2.BORDER_REFLECT101)
    r2 = cv2.filter2D(band, cv2.CV_32F, ky, borderType=cv2.BORDER_REFLECT101)

    r_norm = np.hypot(r1, r2) + 1e-7
    amp = np.hypot(band, r_norm) + 1e-7
    phi = np.arctan2(r_norm, band)
    # Quaternionic phase vector: (phi * cos(theta), phi * sin(theta)) = (phi * R1 / r_norm, phi * R2 / r_norm)
    qx = phi * (r1 / r_norm)
    qy = phi * (r2 / r_norm)
    return amp, phi, qx, qy


@PROCESSING_REGISTRY.register("riesz")
class RieszPyramidMagnification(MagnificationMethod):
    """Riesz Pyramid Phase-Based Video Magnification (fast monogenic phase shift)."""

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
        levels = max(1, min(params.pyramid_levels, 5))

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Cancelled at start of Riesz processing")

        ycrcb_frames = np.empty((n_frames, h, w, 3), dtype=np.float32)
        pyramids_by_level: list[list[np.ndarray]] = []
        qx_by_level: list[list[np.ndarray]] = []
        qy_by_level: list[list[np.ndarray]] = []
        amp_by_level: list[list[np.ndarray]] = []

        for t in range(n_frames):
            ycrcb = cv2.cvtColor(frames_bgr[t].astype(np.float32), cv2.COLOR_BGR2YCrCb)
            ycrcb_frames[t] = ycrcb
            luma = ycrcb[..., 0]
            pyr = build_laplacian_pyramid(luma, levels)

            if t == 0:
                n_lvls = len(pyr)
                pyramids_by_level = [[] for _ in range(n_lvls)]
                qx_by_level = [[] for _ in range(n_lvls - 1)]
                qy_by_level = [[] for _ in range(n_lvls - 1)]
                amp_by_level = [[] for _ in range(n_lvls - 1)]

            for lvl_idx, band in enumerate(pyr):
                pyramids_by_level[lvl_idx].append(band)
                if lvl_idx < len(pyr) - 1:
                    amp, _phi, qx, qy = compute_riesz_triple(band)
                    qx_by_level[lvl_idx].append(qx)
                    qy_by_level[lvl_idx].append(qy)
                    amp_by_level[lvl_idx].append(amp)

        if progress_cb and not progress_cb(0.35):
            raise JobCancelledError("Cancelled after Riesz decomposition")

        n_pyr_levels = len(pyramids_by_level)
        magnified_luma_levels: list[np.ndarray] = []
        phase_roi_trace = np.zeros(n_frames, dtype=np.float64)

        for lvl_idx in range(n_pyr_levels - 1):
            orig_band = np.stack(pyramids_by_level[lvl_idx], axis=0)
            amp_stack = np.stack(amp_by_level[lvl_idx], axis=0)
            qx_stack = np.stack(qx_by_level[lvl_idx], axis=0)
            qy_stack = np.stack(qy_by_level[lvl_idx], axis=0)

            # Relative quaternionic phase change from temporal mean
            dqx = qx_stack - np.mean(qx_stack, axis=0, keepdims=True)
            dqy = qy_stack - np.mean(qy_stack, axis=0, keepdims=True)

            bp_qx = apply_temporal_bandpass(
                dqx, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz, filter_type=params.filter_type
            )
            bp_qy = apply_temporal_bandpass(
                dqy, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz, filter_type=params.filter_type
            )

            if params.spatial_blur_sigma > 0:
                sigma = params.spatial_blur_sigma
                for t in range(n_frames):
                    w_num_x = cv2.GaussianBlur(bp_qx[t] * amp_stack[t], (0, 0), sigma)
                    w_num_y = cv2.GaussianBlur(bp_qy[t] * amp_stack[t], (0, 0), sigma)
                    w_den = cv2.GaussianBlur(amp_stack[t], (0, 0), sigma) + 1e-6
                    bp_qx[t] = w_num_x / w_den
                    bp_qy[t] = w_num_y / w_den

            # Accumulate mid-level phase shift into ROI trace for accurate motion spectrum
            if lvl_idx in (1, 2) or n_pyr_levels <= 2:
                lh, lw = qx_stack.shape[1:3]
                roi_q = dqx[:, lh // 4 : 3 * lh // 4, lw // 4 : 3 * lw // 4]
                phase_roi_trace += np.mean(roi_q, axis=(1, 2)).astype(np.float64)

            level_scale = 0.6 if lvl_idx == 0 else 1.0
            eff_alpha = params.alpha * level_scale

            # Phase shift reconstruction: I_mag = A * cos(phi + alpha * dphi)
            new_qx = qx_stack + eff_alpha * bp_qx
            new_qy = qy_stack + eff_alpha * bp_qy
            new_phi = np.hypot(new_qx, new_qy)
            shifted_band = amp_stack * np.cos(new_phi)
            # Blend with small luma component so pure intensity pulses are also preserved
            bp_luma = apply_temporal_bandpass(
                orig_band,
                fps=fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                filter_type=params.filter_type,
            )
            magnified_luma_levels.append(shifted_band + 0.25 * eff_alpha * bp_luma)

            if progress_cb and not progress_cb(0.35 + 0.45 * ((lvl_idx + 1) / (n_pyr_levels - 1))):
                raise JobCancelledError("Cancelled during Riesz temporal filtering")

        # Residual DC level stays unmodified
        magnified_luma_levels.append(np.stack(pyramids_by_level[-1], axis=0))

        out_frames = np.empty_like(frames_bgr)
        motion_energy = np.zeros((n_frames, h, w), dtype=np.float32)

        for t in range(n_frames):
            frame_pyr = [magnified_luma_levels[lvl_idx][t] for lvl_idx in range(n_pyr_levels)]
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
        raw_sig = (luma_roi - np.mean(luma_roi)) + 10.0 * phase_roi_trace
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
                "pyramid_levels": n_pyr_levels - 1,
                "alpha": params.alpha,
            },
        )
