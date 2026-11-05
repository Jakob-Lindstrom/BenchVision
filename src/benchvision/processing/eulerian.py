"""Linear Eulerian Video Magnification (spatial Laplacian pyramid + temporal bandpass)."""

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
from benchvision.processing.filters import apply_temporal_bandpass, find_dominant_frequency


def build_laplacian_pyramid(img: np.ndarray, levels: int) -> list[np.ndarray]:
    """Decompose an image into a Laplacian pyramid with `levels` bandpass bands + 1 residual."""
    gaussian = [img.astype(np.float32)]
    for _ in range(levels):
        curr = gaussian[-1]
        if curr.shape[0] < 4 or curr.shape[1] < 4:
            break
        gaussian.append(cv2.pyrDown(curr))

    laplacian: list[np.ndarray] = []
    for i in range(len(gaussian) - 1):
        h, w = gaussian[i].shape[:2]
        up = cv2.pyrUp(gaussian[i + 1], dstsize=(w, h))
        laplacian.append(gaussian[i] - up)
    laplacian.append(gaussian[-1])
    return laplacian


def collapse_laplacian_pyramid(pyramid: list[np.ndarray]) -> np.ndarray:
    """Reconstruct an image from its Laplacian pyramid."""
    curr = pyramid[-1]
    for i in range(len(pyramid) - 2, -1, -1):
        h, w = pyramid[i].shape[:2]
        curr = cv2.pyrUp(curr, dstsize=(w, h)) + pyramid[i]
    return curr


@PROCESSING_REGISTRY.register("eulerian")
class LinearEulerianMagnification(MagnificationMethod):
    """Linear Eulerian Video Magnification (Wu et al., SIGGRAPH 2012)."""

    method_key = "eulerian"
    display_name = "Linear Eulerian Video Magnification (EVM)"
    description = (
        "Spatial Laplacian pyramid decomposition + temporal bandpass filtering. "
        "Great baseline for small motions and subtle skin color / pulse variations."
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
        levels = max(1, min(params.pyramid_levels, 6))

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Job cancelled during initialization")

        # Convert frames to YCrCb float32 and build spatial pyramids per level
        pyramids_by_level: list[list[np.ndarray]] = []
        raw_roi_sig = np.zeros(n_frames, dtype=np.float64)

        for t in range(n_frames):
            frame_f = frames_bgr[t].astype(np.float32)
            if params.spatial_blur_sigma > 0:
                frame_f = cv2.GaussianBlur(frame_f, (0, 0), params.spatial_blur_sigma)
            ycrcb = cv2.cvtColor(frame_f, cv2.COLOR_BGR2YCrCb)
            raw_roi_sig[t] = float(np.mean(ycrcb[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 0]))
            pyr = build_laplacian_pyramid(ycrcb, levels)
            if t == 0:
                pyramids_by_level = [[lvl] for lvl in pyr]
            else:
                for lvl_idx, lvl in enumerate(pyr):
                    pyramids_by_level[lvl_idx].append(lvl)

        if progress_cb and not progress_cb(0.30):
            raise JobCancelledError("Job cancelled after pyramid construction")

        n_pyr_levels = len(pyramids_by_level)
        amplified_levels: list[np.ndarray] = []

        # Temporal bandpass + amplification per pyramid level (skip DC residual at index -1)
        for lvl_idx in range(n_pyr_levels):
            stack = np.stack(pyramids_by_level[lvl_idx], axis=0)  # (T, H_l, W_l, 3)
            if lvl_idx == n_pyr_levels - 1:
                amplified_levels.append(stack)
                continue

            bp = apply_temporal_bandpass(
                stack,
                fps=fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                filter_type=params.filter_type,
            )

            # Attenuate finest level slightly to suppress sensor shot noise
            level_scale = 0.5 if lvl_idx == 0 else 1.0
            eff_alpha = params.alpha * level_scale
            chrom_scale = params.chrom_attenuation

            delta = np.empty_like(bp)
            delta[..., 0] = bp[..., 0] * eff_alpha
            delta[..., 1] = bp[..., 1] * eff_alpha * chrom_scale
            delta[..., 2] = bp[..., 2] * eff_alpha * chrom_scale

            amplified_levels.append(stack + delta)

            if progress_cb and not progress_cb(0.30 + 0.45 * ((lvl_idx + 1) / n_pyr_levels)):
                raise JobCancelledError("Job cancelled during temporal filtering")

        # Reconstruct frames and compute spatial motion-energy map
        out_frames = np.empty_like(frames_bgr)
        motion_energy = np.zeros((n_frames, h, w), dtype=np.float32)

        for t in range(n_frames):
            frame_pyr = [amplified_levels[lvl_idx][t] for lvl_idx in range(n_pyr_levels)]
            recon_ycrcb = collapse_laplacian_pyramid(frame_pyr)
            orig_ycrcb = cv2.cvtColor(frames_bgr[t].astype(np.float32), cv2.COLOR_BGR2YCrCb)
            diff_luma = np.abs(recon_ycrcb[..., 0] - orig_ycrcb[..., 0])
            motion_energy[t] = diff_luma

            recon_bgr = cv2.cvtColor(recon_ycrcb, cv2.COLOR_YCrCb2BGR)
            out_frames[t] = np.clip(recon_bgr, 0, 255).astype(np.uint8)

        max_energy = float(np.percentile(motion_energy, 99.5)) if np.any(motion_energy > 0) else 1.0
        if max_energy > 1e-6:
            motion_energy = np.clip(motion_energy / max_energy, 0.0, 1.0)

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
                "pyramid_levels": n_pyr_levels - 1,
                "alpha": params.alpha,
            },
        )
