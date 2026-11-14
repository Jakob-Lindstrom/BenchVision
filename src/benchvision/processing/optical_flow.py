"""Dense Optical-Flow / Sub-Pixel Displacement Analysis and Motion Warping."""

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
        downscale = w >= 160 and h >= 120
        fw = w // 2 if downscale else w
        fh = h // 2 if downscale else h
        flow_scale = 2.0 if downscale else 1.0

        # Compute temporal reference frame in a zero-copy 2D accumulator (< 1 MB RAM)
        acc_gray = np.zeros((fh, fw), dtype=np.float64)
        for t in range(n_frames):
            g = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2GRAY)
            if downscale:
                g = cv2.resize(g, (fw, fh), interpolation=cv2.INTER_AREA)
            acc_gray += g
        ref_gray = (acc_gray / max(n_frames, 1)).astype(np.uint8)
        del acc_gray

        flows = np.zeros((n_frames, fh, fw, 2), dtype=np.float32)

        for t in range(n_frames):
            curr_u8 = cv2.cvtColor(frames_bgr[t], cv2.COLOR_BGR2GRAY)
            if downscale:
                curr_u8 = cv2.resize(curr_u8, (fw, fh), interpolation=cv2.INTER_AREA)
            flows[t] = (
                cv2.calcOpticalFlowFarneback(
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
                * flow_scale
            )
            if progress_cb and t % 15 == 0 and not progress_cb(0.05 + 0.45 * (t / n_frames)):
                raise JobCancelledError("Cancelled during optical flow estimation")

        # Extract displacement (px) inside central ROI before in-place bandpass
        roi_dx = np.mean(
            flows[:, fh // 4 : 3 * fh // 4, fw // 4 : 3 * fw // 4, 0], axis=(1, 2)
        ).astype(np.float64)
        roi_dy = np.mean(
            flows[:, fh // 4 : 3 * fh // 4, fw // 4 : 3 * fw // 4, 1], axis=(1, 2)
        ).astype(np.float64)
        roi_disp = roi_dx + 0.5 * roi_dy
        if n_frames > 4:
            roi_disp[:4] = roi_disp[4]

        bp_flow = apply_temporal_bandpass(
            flows,
            fps=fps,
            low_hz=params.low_hz,
            high_hz=params.high_hz,
            filter_type=params.filter_type,
            out=flows,
        )

        if progress_cb and not progress_cb(0.70):
            raise JobCancelledError("Cancelled after optical flow bandpass")

        grid_x, grid_y = np.meshgrid(
            np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32)
        )
        out_frames = np.empty_like(frames_bgr)
        motion_energy = alloc_motion_energy(n_frames, h, w)

        for t in range(n_frames):
            if downscale:
                fx = cv2.resize(bp_flow[t, ..., 0], (w, h), interpolation=cv2.INTER_LINEAR)
                fy = cv2.resize(bp_flow[t, ..., 1], (w, h), interpolation=cv2.INTER_LINEAR)
            else:
                fx = bp_flow[t, ..., 0]
                fy = bp_flow[t, ..., 1]
            motion_energy[t] = shrink_energy(np.hypot(fx, fy), motion_energy)
            map_x = grid_x - params.alpha * fx
            map_y = grid_y - params.alpha * fy
            warped = cv2.remap(
                frames_bgr[t],
                map_x,
                map_y,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT101,
            )
            out_frames[t] = warped

        del flows, bp_flow
        gc.collect()
        # Mask outer border on optical flow motion energy to suppress Farneback edge padding artifacts
        mh, mw = motion_energy.shape[1:3]
        by = max(1, mh // 12)
        bx = max(1, mw // 12)
        if mh > 4 * by and mw > 4 * bx:
            motion_energy[:, :by, :] = 0.0
            motion_energy[:, -by:, :] = 0.0
            motion_energy[:, :, :bx] = 0.0
            motion_energy[:, :, -bx:] = 0.0
        normalize_motion_energy_inplace(motion_energy)

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

