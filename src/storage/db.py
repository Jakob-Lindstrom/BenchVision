"""SQLite persistence layer inside <data_dir>/benchvision.db with schema versioning."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import SCHEMA_VERSION


class Database:
    """Thread-safe SQLite database storing clip metadata, jobs, and schema version."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextlib.contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._session() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    clip_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    camera_id TEXT NOT NULL,
                    camera_name TEXT NOT NULL,
                    shutter_type TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    target_fps REAL NOT NULL,
                    achieved_fps REAL NOT NULL,
                    frame_count INTEGER NOT NULL,
                    duration_s REAL NOT NULL,
                    storage_format TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL,
                    roi_json TEXT,
                    controls_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    clip_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    params_json TEXT NOT NULL,
                    result_json TEXT,
                    error_message TEXT,
                    FOREIGN KEY (clip_id) REFERENCES recordings(clip_id) ON DELETE CASCADE
                )
                """
            )
            row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            now_iso = datetime.now(UTC).isoformat()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (id, version, updated_at) VALUES (1, ?, ?)",
                    (SCHEMA_VERSION, now_iso),
                )
            elif int(row["version"]) < SCHEMA_VERSION:
                conn.execute(
                    "UPDATE schema_meta SET version = ?, updated_at = ? WHERE id = 1",
                    (SCHEMA_VERSION, now_iso),
                )

    def upsert_recording(self, meta: dict[str, Any]) -> None:
        with self._lock, self._session() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO recordings (
                    clip_id, title, created_at, camera_id, camera_name, shutter_type,
                    width, height, target_fps, achieved_fps, frame_count, duration_s,
                    storage_format, file_size_bytes, roi_json, controls_json, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta["clip_id"],
                    meta.get("title", meta["clip_id"]),
                    meta["created_at"],
                    meta["camera_id"],
                    meta.get("camera_name", meta["camera_id"]),
                    meta.get("shutter_type", "rolling"),
                    int(meta["width"]),
                    int(meta["height"]),
                    float(meta["target_fps"]),
                    float(meta["achieved_fps"]),
                    int(meta["frame_count"]),
                    float(meta["duration_s"]),
                    meta.get("storage_format", "mjpeg_q100"),
                    int(meta.get("file_size_bytes", 0)),
                    json.dumps(meta.get("roi")),
                    json.dumps(meta.get("controls", {})),
                    json.dumps(meta),
                ),
            )

    def list_recordings(self) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT meta_json FROM recordings ORDER BY created_at DESC"
            ).fetchall()
            job_rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
            jobs_by_clip: dict[str, list[dict[str, Any]]] = {}
            for jr in job_rows:
                job_dict = self._row_to_job(jr)
                jobs_by_clip.setdefault(job_dict["clip_id"], []).append(job_dict)
            result = []
            for r in rows:
                item = json.loads(r["meta_json"])
                item["jobs"] = jobs_by_clip.get(item["clip_id"], [])
                result.append(item)
            return result

    def get_recording(self, clip_id: str) -> dict[str, Any] | None:
        with self._lock, self._session() as conn:
            row = conn.execute(
                "SELECT meta_json FROM recordings WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            if row is None:
                return None
            item = json.loads(row["meta_json"])
            job_rows = conn.execute(
                "SELECT * FROM jobs WHERE clip_id = ? ORDER BY created_at DESC",
                (clip_id,),
            ).fetchall()
            item["jobs"] = [self._row_to_job(jr) for jr in job_rows]
            return item

    def delete_recording(self, clip_id: str) -> bool:
        with self._lock, self._session() as conn:
            conn.execute("DELETE FROM jobs WHERE clip_id = ?", (clip_id,))
            cur = conn.execute("DELETE FROM recordings WHERE clip_id = ?", (clip_id,))
            return cur.rowcount > 0

    def update_recording_metadata(
        self,
        clip_id: str,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Update title and/or notes on an existing recording row and return full metadata."""
        with self._lock, self._session() as conn:
            row = conn.execute(
                "SELECT meta_json FROM recordings WHERE clip_id = ?", (clip_id,)
            ).fetchone()
            if row is None:
                return None
            item = json.loads(row["meta_json"])
            if title is not None:
                item["title"] = title
            if notes is not None:
                item["notes"] = notes
            conn.execute(
                "UPDATE recordings SET title = ?, meta_json = ? WHERE clip_id = ?",
                (item.get("title", clip_id), json.dumps(item), clip_id),
            )
            job_rows = conn.execute(
                "SELECT * FROM jobs WHERE clip_id = ? ORDER BY created_at DESC",
                (clip_id,),
            ).fetchall()
            item["jobs"] = [self._row_to_job(jr) for jr in job_rows]
            return item

    def upsert_job(self, job: dict[str, Any]) -> None:
        with self._lock, self._session() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    job_id, clip_id, method, status, progress, created_at,
                    started_at, completed_at, params_json, result_json, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"],
                    job["clip_id"],
                    job["method"],
                    job["status"],
                    float(job.get("progress", 0.0)),
                    job["created_at"],
                    job.get("started_at"),
                    job.get("completed_at"),
                    json.dumps(job.get("params", {})),
                    json.dumps(job["result"]) if job.get("result") is not None else None,
                    job.get("error_message"),
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._session() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row is not None else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
            return [self._row_to_job(r) for r in rows]

    def list_jobs_for_clip(self, clip_id: str) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE clip_id = ? ORDER BY created_at DESC", (clip_id,)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "clip_id": row["clip_id"],
            "method": row["method"],
            "status": row["status"],
            "progress": float(row["progress"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "params": json.loads(row["params_json"]) if row["params_json"] else {},
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error_message": row["error_message"],
        }

