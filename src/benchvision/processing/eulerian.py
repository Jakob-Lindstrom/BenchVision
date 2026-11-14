"""Low-memory Linear Eulerian Video Magnification (in-place Laplacian delta pyramid)."""

from __future__ import annotations

import gc

import cv2
import numpy as np

from benchvision.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationMethod,
    MagnificationOutput,
    ProgressCallback,
    alloc_motion_energy,
    shrink_energy,
)
from benchvision.processing.filters import (
    apply_temporal_bandpass,
    find_dominant_frequency,
    normalize_motion_energy_inplace,
)


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
    """Linear Eulerian Video Magnification (Wu et al., SIGGRAPH 2012) optimized for Pi RAM."""

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
        levels = max(2, min(params.pyramid_levels, 6))

        if progress_cb and not progress_cb(0.05):
            raise JobCancelledError("Job cancelled during initialization")

        # Skip finest level 0 (1-px sensor/MJPG noise cutoff lambda_c) -> 75% lower RAM & 4x faster
        pyr_bands: list[np.ndarray] = []
        residual_shape: tuple[int, ...] = ()
        skip_finest = True
        raw_roi_sig = np.zeros(n_frames, dtype=np.float64)

        for t in range(n_frames):
            frame_f = frames_bgr[t].astype(np.float32)
            if params.spatial_blur_sigma > 0:
                frame_f = cv2.GaussianBlur(frame_f, (0, 0), params.spatial_blur_sigma)
            ycrcb = cv2.cvtColor(frame_f, cv2.COLOR_BGR2YCrCb)
            raw_roi_sig[t] = float(np.mean(ycrcb[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 0]))
            pyr = build_laplacian_pyramid(ycrcb, levels)
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
            raise JobCancelledError("Job cancelled after pyramid construction")

        n_band_levels = len(pyr_bands)

        # In-place temporal bandpass + amplification per pyramid level (zero extra array copies)
        for lvl_idx in range(n_band_levels):
            band = pyr_bands[lvl_idx]
            apply_temporal_bandpass(
                band,
                fps=fps,
                low_hz=params.low_hz,
                high_hz=params.high_hz,
                filter_type=params.filter_type,
                out=band,
            )

            eff_alpha = float(params.alpha)
            chrom_scale = float(params.chrom_attenuation)

            band[..., 0] *= eff_alpha
            band[..., 1] *= eff_alpha * chrom_scale
            band[..., 2] *= eff_alpha * chrom_scale

            if progress_cb and not progress_cb(0.30 + 0.45 * ((lvl_idx + 1) / n_band_levels)):
                raise JobCancelledError("Job cancelled during temporal filtering")

        zero_residual = np.zeros(residual_shape, dtype=np.float32)
        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)

        for t in range(n_frames):
            delta_pyr = [pyr_bands[lvl_idx][t] for lvl_idx in range(n_band_levels)] + [
                zero_residual
            ]
            delta_sub = collapse_laplacian_pyramid(delta_pyr)
            delta_ycrcb = (
                cv2.pyrUp(delta_sub, dstsize=(w, h)) if skip_finest else delta_sub
            )
            orig_ycrcb = cv2.cvtColor(frames_bgr[t].astype(np.float32), cv2.COLOR_BGR2YCrCb)
            recon_ycrcb = orig_ycrcb + delta_ycrcb
            motion_energy[t] = shrink_energy(np.abs(delta_ycrcb[..., 0]), motion_energy)

            recon_bgr = cv2.cvtColor(recon_ycrcb, cv2.COLOR_YCrCb2BGR)
            out_frames[t] = np.clip(recon_bgr, 0, 255).astype(np.uint8)

        del pyr_bands
        gc.collect()
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
                "pyramid_levels": n_band_levels,
                "alpha": params.alpha,
            },
        )
