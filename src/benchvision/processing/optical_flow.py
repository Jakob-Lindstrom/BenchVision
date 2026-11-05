"""Dense Optical-Flow / Sub-Pixel Displacement Analysis and Motion Warping."""

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


@PROCESSING_REGISTRY.register("optical_flow")
class OpticalFlowDisplacementMethod(MagnificationMethod):
    """Measures sub-pixel displacement in pixels over time via dense optical flow and warps frames."""

    method_key = "optical_flow"
    display_name = "Optical Flow Displacement & Motion Warping"
    description = (
        "Measures physical sub-pixel displacement (in pixels) over time using dense Farneback "
        "optical flow, plots true displacement, and warps pixels to visualize motion."
    )
    speed_rating = "medium"

    def process(
        self,
        frames_bgr: np.ndarray,
        timestamps_s: np.ndarray,
        fps: float,
        params: JobParams,
        progress_cb: ProgressCallback | None = None,
    ) -> MagnificationOutput:
        n_frames, h, w, _ = frames_bgr.shape
        grays = np.empty((n_frames, h, w), dtype=np.float32)
        for t in range(n_frames):
            grays[t] = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2GRAY).astype(np.float32)

        ref_gray = np.mean(grays, axis=0).astype(np.uint8)
        flows = np.zeros((n_frames, h, w, 2), dtype=np.float32)

        for t in range(n_frames):
            curr_u8 = grays[t].astype(np.uint8)
            flows[t] = cv2.calcOpticalFlowFarneback(
                ref_gray,
                curr_u8,
                None,
                pyr_scale=0.5,
                levels=3,
                winsize=15,
                iterations=3,
                poly_n=5,
                poly_sigma=1.2,
                flags=0,
            )
            if progress_cb and t % 15 == 0 and not progress_cb(0.05 + 0.45 * (t / n_frames)):
                raise JobCancelledError("Cancelled during optical flow estimation")

        bp_flow = apply_temporal_bandpass(
            flows,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        )

        grid_x, grid_y = np.meshgrid(
            np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
        )
        out_frames = np.empty_like(frames_bgr)
        motion_energy = np.hypot(bp_flow[..., 0], bp_flow[..., 1])

        for t in range(n_frames):
            map_x = grid_x - params.alpha * bp_flow[t, ..., 0]
            map_y = grid_y - params.alpha * bp_flow[t, ..., 1]
            warped = cv2.remap(
                frames_bgr[t],
                map_x,
                map_y,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT101,
            )
            out_frames[t] = warped

        max_e = float(np.percentile(motion_energy, 99.5)) if np.any(motion_energy > 0) else 1.0
        if max_e > 1e-6:
            motion_energy = np.clip(motion_energy / max_e, 0.0, 1.0)

        # Extract displacement (px) inside central ROI + luma gradient trace
        roi_dx = np.mean(
            flows[:, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 0], axis=(1, 2)
        ).astype(np.float64)
        roi_dy = np.mean(
            flows[:, h // 4 : 3 * h // 4, w // 4 : 3 * w // 4, 1], axis=(1, 2)
        ).astype(np.float64)
        roi_disp = roi_dx + 0.5 * roi_dy

        filtered_disp = apply_temporal_bandpass(
            roi_disp,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
        ).astype(np.float64)

        dom_hz, dom_bpm = find_dominant_frequency(
            roi_disp, fps=fps, low_hz=params.low_hz, high_hz=params.high_hz
        )

        rms_px = float(np.sqrt(np.mean(filtered_disp**2)))
        peak_to_peak_px = float(np.ptp(filtered_disp))

        if progress_cb:
            progress_cb(0.95)

        return MagnificationOutput(
            magnified_bgr=out_frames,
            motion_energy=motion_energy,
            raw_roi_signal=roi_disp,
            filtered_roi_signal=filtered_disp,
            timestamps_s=timestamps_s,
            achieved_fps=fps,
            dominant_hz=dom_hz,
            dominant_bpm=dom_bpm,
            extra_metrics={
                "method": self.method_key,
                "rms_displacement_px": round(rms_px, 5),
                "peak_to_peak_displacement_px": round(peak_to_peak_px, 5),
                "unit": "pixels",
            },
        )
