"""Data folder layout manager ensuring zero file writes outside the configured data root."""

from __future__ import annotations

import shutil
from pathlib import Path


class DataLayout:
    """Manages paths inside the single user-configurable data folder."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir).resolve()
        self.recordings_dir = self.root / "recordings"
        self.snapshots_dir = self.root / "snapshots"
        self.exports_dir = self.root / "exports"
        self.logs_dir = self.root / "logs"
        self.db_path = self.root / "benchvision.db"
        self.config_path = self.root / "config.json"
        self.ensure_directories()

    def ensure_directories(self) -> None:
        """Create required subdirectories inside the data root."""
        for directory in (
            self.root,
            self.recordings_dir,
            self.snapshots_dir,
            self.exports_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_safe_path(self, relative_path: Path | str) -> Path:
        """Resolve a path inside data_dir and raise ValueError if it escapes data_dir."""
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Path '{relative_path}' escapes data directory '{self.root}'")
        return candidate

    def clip_dir(self, clip_id: str) -> Path:
        """Return and ensure directory for a specific recording clip."""
        safe_id = "".join(c for c in clip_id if c.isalnum() or c in ("-", "_"))
        if not safe_id:
            raise ValueError(f"Invalid clip_id: {clip_id}")
        path = self.resolve_safe_path(Path("recordings") / safe_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "results").mkdir(parents=True, exist_ok=True)
        return path

    def job_result_dir(self, clip_id: str, job_id: str) -> Path:
        """Return and ensure directory for a specific processing job's results next to the clip."""
        safe_job = "".join(c for c in job_id if c.isalnum() or c in ("-", "_"))
        if not safe_job:
            raise ValueError(f"Invalid job_id: {job_id}")
        clip_path = self.clip_dir(clip_id)
        res_path = self.resolve_safe_path(clip_path.relative_to(self.root) / "results" / safe_job)
        res_path.mkdir(parents=True, exist_ok=True)
        return res_path

    def disk_usage(self) -> dict[str, int | float]:
        """Return total, used, free bytes and data directory usage."""
        usage = shutil.disk_usage(self.root)
        data_bytes = sum(
            f.stat().st_size for f in self.root.rglob("*") if f.is_file()
        )
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "data_dir_bytes": data_bytes,
            "free_gb": round(usage.free / (1024**3), 2),
        }
