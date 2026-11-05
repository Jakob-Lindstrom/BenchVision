"""Background job queue with progress tracking, cancellation, and remote-worker readiness."""

from __future__ import annotations

import csv
import json
import logging
import queue
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from benchvision.processing.base import (
    PROCESSING_REGISTRY,
    JobCancelledError,
    JobParams,
    MagnificationOutput,
    estimate_processing_time_s,
)
from benchvision.recording.recorder import encode_browser_mp4, load_clip_frames_and_timestamps
from benchvision.storage.db import Database
from benchvision.storage.layout import DataLayout

logger = logging.getLogger("benchvision.jobs")


def render_heatmap_frames(
    orig_bgr: np.ndarray, motion_energy: np.ndarray, alpha: float = 0.55
) -> list[np.ndarray]:
    """Overlay normalized [0, 1] spatial motion energy as an INFERNO heatmap on original frames."""
    rendered: list[np.ndarray] = []
    mean_energy = np.mean(motion_energy, axis=0)
    p99 = float(np.percentile(mean_energy, 99.0)) if np.any(mean_energy > 0) else 1.0
    norm_static = np.clip(mean_energy / max(p99, 1e-6), 0.0, 1.0)

    for t in range(len(orig_bgr)):
        instant = motion_energy[t]
        combined = np.clip(0.65 * instant + 0.35 * norm_static, 0.0, 1.0)
        heat_u8 = (combined * 255.0).astype(np.uint8)
        cmap = cv2.applyColorMap(heat_u8, cv2.COLORMAP_INFERNO)
        blended = cv2.addWeighted(orig_bgr[t], 1.0 - alpha, cmap, alpha, 0.0)
        rendered.append(blended)
    return rendered


def render_side_by_side_frames(orig_bgr: np.ndarray, mag_bgr: np.ndarray) -> list[np.ndarray]:
    """Create side-by-side Original vs. Magnified frames with clean labels."""
    sbs_list: list[np.ndarray] = []
    for t in range(len(orig_bgr)):
        left = orig_bgr[t].copy()
        right = mag_bgr[t].copy()
        cv2.putText(
            left, "Original", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (240, 240, 240), 2
        )
        cv2.putText(
            right, "Magnified", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (56, 189, 248), 2
        )
        divider = np.full((left.shape[0], 2, 3), 60, dtype=np.uint8)
        sbs_list.append(np.concatenate([left, divider, right], axis=1))
    return sbs_list


def write_signal_csv(
    csv_path: Path,
    timestamps_s: np.ndarray,
    raw_signal: np.ndarray,
    filtered_signal: np.ndarray,
) -> Path:
    """Export extracted ROI raw and bandpass-filtered temporal signals to CSV."""
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frame_index", "time_s", "raw_signal", "filtered_signal"])
        for i in range(len(timestamps_s)):
            writer.writerow(
                [
                    i,
                    f"{timestamps_s[i]:.5f}",
                    f"{raw_signal[i]:.5f}",
                    f"{filtered_signal[i]:.5f}",
                ]
            )
    return csv_path


class JobQueueManager:
    """Manages offline processing jobs with a background worker queue and cancel support."""

    def __init__(self, layout: DataLayout, db: Database, start_worker: bool = True) -> None:
        self.layout = layout
        self.db = db
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancelled_ids: set[str] = set()
        self._lock = threading.Lock()
        self._running = True
        self._worker_thread: threading.Thread | None = None
        if start_worker:
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def submit_job(
        self, clip_id: str, params: JobParams, run_async: bool = True
    ) -> dict[str, Any]:
        """Create and enqueue a new offline magnification job for an existing recording."""
        clip = self.db.get_recording(clip_id)
        if clip is None:
            raise ValueError(f"Recording clip '{clip_id}' not found")

        method_cls = PROCESSING_REGISTRY.get(params.method)
        est_s = estimate_processing_time_s(
            frame_count=int(clip["frame_count"]),
            width=int(params.max_width or clip["width"]),
            height=int(clip["height"]),
            method=params.method,
        )

        job_id = f"job_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        job_doc: dict[str, Any] = {
            "job_id": job_id,
            "clip_id": clip_id,
            "method": method_cls.method_key,
            "status": "queued",
            "progress": 0.0,
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "params": params.model_dump(),
            "result": {"estimated_seconds": est_s},
            "error_message": None,
        }
        self.db.upsert_job(job_doc)

        if run_async:
            self._queue.put(job_id)
            return job_doc
        return self.execute_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        """Mark a queued or running job as cancelled."""
        with self._lock:
            self._cancelled_ids.add(job_id)
        job = self.db.get_job(job_id)
        if job and job["status"] in ("queued", "running"):
            job["status"] = "cancelled"
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
        return job

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled_ids

    def _worker_loop(self) -> None:
        while self._running:
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self.execute_job(job_id)
            except Exception:
                logger.exception("Unhandled error in worker for job %s", job_id)
            finally:
                self._queue.task_done()

    def execute_job(self, job_id: str) -> dict[str, Any]:
        """Run a single magnification job to completion, saving outputs next to the clip."""
        job = self.db.get_job(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")
        if self.is_cancelled(job_id) or job["status"] == "cancelled":
            job["status"] = "cancelled"
            self.db.upsert_job(job)
            return job

        clip_id = job["clip_id"]
        params = JobParams(**job["params"])
        clip_dir = self.layout.clip_dir(clip_id)
        res_dir = self.layout.job_result_dir(clip_id, job_id)

        job["status"] = "running"
        job["started_at"] = datetime.now(UTC).isoformat()
        job["progress"] = 0.02
        self.db.upsert_job(job)

        def _progress(val: float) -> bool:
            if self.is_cancelled(job_id):
                return False
            job["progress"] = round(float(val), 3)
            self.db.upsert_job(job)
            return True

        try:
            frames_bgr, timestamps_s, achieved_fps = load_clip_frames_and_timestamps(
                clip_dir,
                roi=params.roi,
                max_width=params.max_width,
            )

            # Handle "auto_find" preset by discovering the dominant spectral peak first
            if params.preset_id == "auto_find":
                from benchvision.analysis.spectrum import compute_roi_spectrum_analysis

                auto_spec = compute_roi_spectrum_analysis(
                    frames_bgr=frames_bgr,
                    fps=achieved_fps,
                    low_hz=0.2,
                    high_hz=min(12.0, 0.45 * achieved_fps),
                )
                suggested = auto_spec["suggested_band"]
                params.low_hz = suggested["low_hz"]
                params.high_hz = suggested["high_hz"]

            method_cls = PROCESSING_REGISTRY.get(params.method)
            processor = method_cls()
            out: MagnificationOutput = processor.process(
                frames_bgr=frames_bgr,
                timestamps_s=timestamps_s,
                fps=achieved_fps,
                params=params,
                progress_cb=_progress,
            )

            if self.is_cancelled(job_id):
                raise JobCancelledError("Cancelled before output export")

            # Save parameters
            (res_dir / "params.json").write_text(
                json.dumps(params.model_dump(), indent=2), encoding="utf-8"
            )

            # Save lossless AVI + shareable MP4 + side-by-side MP4 + heatmap MP4
            h, w = out.magnified_bgr.shape[1:3]
            lossless_path = res_dir / "magnified_lossless.avi"
            vw = cv2.VideoWriter(
                str(lossless_path), cv2.VideoWriter_fourcc(*"MJPG"), achieved_fps, (w, h)
            )
            for f in out.magnified_bgr:
                vw.write(f)
            vw.release()

            share_mp4 = encode_browser_mp4(
                out.magnified_bgr, achieved_fps, res_dir / "magnified_share.mp4"
            )
            sbs_frames = render_side_by_side_frames(frames_bgr, out.magnified_bgr)
            sbs_mp4 = encode_browser_mp4(
                sbs_frames, achieved_fps, res_dir / "comparison_sbs.mp4"
            )
            heat_frames = render_heatmap_frames(frames_bgr, out.motion_energy)
            heat_mp4 = encode_browser_mp4(heat_frames, achieved_fps, res_dir / "heatmap.mp4")

            csv_path = write_signal_csv(
                res_dir / "signal.csv",
                out.timestamps_s,
                out.raw_roi_signal,
                out.filtered_roi_signal,
            )

            # Save spectral analysis if analysis module is present
            try:
                from benchvision.analysis.spectrum import compute_signal_spectrum

                spec_data = compute_signal_spectrum(
                    signal_1d=out.raw_roi_signal,
                    filtered_1d=out.filtered_roi_signal,
                    timestamps_s=out.timestamps_s,
                    fps=achieved_fps,
                    low_hz=params.low_hz,
                    high_hz=params.high_hz,
                )
            except ImportError:
                spec_data = {
                    "dominant_hz": out.dominant_hz,
                    "dominant_bpm": out.dominant_bpm,
                }

            (res_dir / "spectrum.json").write_text(
                json.dumps(spec_data, indent=2), encoding="utf-8"
            )

            job["status"] = "completed"
            job["progress"] = 1.0
            job["completed_at"] = datetime.now(UTC).isoformat()
            job["params"] = params.model_dump()
            job["result"] = {
                "dominant_hz": out.dominant_hz,
                "dominant_bpm": out.dominant_bpm,
                "achieved_fps": round(achieved_fps, 3),
                "width": w,
                "height": h,
                "frame_count": len(out.magnified_bgr),
                "files": {
                    "magnified_mp4": share_mp4.name,
                    "comparison_sbs_mp4": sbs_mp4.name,
                    "heatmap_mp4": heat_mp4.name,
                    "magnified_lossless": lossless_path.name,
                    "signal_csv": csv_path.name,
                    "spectrum_json": "spectrum.json",
                },
                "extra_metrics": out.extra_metrics,
            }
            self.db.upsert_job(job)
            return job

        except JobCancelledError:
            job["status"] = "cancelled"
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
            return job
        except Exception as exc:
            logger.exception("Job %s failed", job_id)
            job["status"] = "failed"
            job["error_message"] = str(exc)
            job["completed_at"] = datetime.now(UTC).isoformat()
            self.db.upsert_job(job)
            return job

    def compare_jobs(
        self, clip_id: str, job_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Return a structured comparison of completed jobs on the same clip."""
        all_jobs = self.db.list_jobs_for_clip(clip_id)
        completed = [j for j in all_jobs if j["status"] == "completed"]
        if job_ids:
            wanted = set(job_ids)
            completed = [j for j in completed if j["job_id"] in wanted]

        comparisons: list[dict[str, Any]] = []
        for j in completed:
            res_dir = self.layout.job_result_dir(clip_id, j["job_id"])
            spec_path = res_dir / "spectrum.json"
            snr_db = 0.0
            if spec_path.exists():
                try:
                    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
                    snr_db = float(spec_data.get("snr_db", 0.0))
                except (ValueError, OSError):
                    snr_db = 0.0

            res = j.get("result") or {}
            comparisons.append(
                {
                    "job_id": j["job_id"],
                    "method": j["method"],
                    "params": j["params"],
                    "dominant_hz": res.get("dominant_hz", 0.0),
                    "dominant_bpm": res.get("dominant_bpm", 0.0),
                    "snr_db": snr_db,
                    "extra_metrics": res.get("extra_metrics", {}),
                    "files": res.get("files", {}),
                }
            )

        freqs = [c["dominant_hz"] for c in comparisons if c["dominant_hz"] > 0]
        freq_spread = round(max(freqs) - min(freqs), 4) if len(freqs) >= 2 else 0.0
        return {
            "clip_id": clip_id,
            "job_count": len(comparisons),
            "frequency_spread_hz": freq_spread,
            "comparisons": comparisons,
        }

    def shutdown(self) -> None:
        self._running = False
        self._queue.put(None)
