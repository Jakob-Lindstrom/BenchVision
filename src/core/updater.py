"""GitHub Release update checker and safe archive installer for BenchVision."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_GITHUB_REPO = "Jakob-Lindstrom/BenchVision"
_CACHE_TTL_S = 600.0
_CACHE_LOCK = threading.Lock()
_CHECK_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

PROTECTED_ROOT_DIRS = frozenset(
    {"data", ".venv", "venv", ".git", ".pytest_cache", ".ruff_cache", "__pycache__"}
)
_BACKUP_TARGETS = (
    "src",
    "scripts",
    "tests",
    "run.py",
    "requirements.txt",
    "pyproject.toml",
    "CHANGELOG.md",
    "README.md",
    "LICENSE",
)


def get_github_repo() -> str:
    """Return configured owner/repo slug on GitHub for BenchVision releases."""
    raw = os.environ.get("BENCHVISION_GITHUB_REPO", DEFAULT_GITHUB_REPO).strip()
    if "/" not in raw or raw.startswith("/") or ".." in raw:
        return DEFAULT_GITHUB_REPO
    return raw


def parse_semver(version: str) -> tuple[int, ...]:
    """Parse a semantic version string such as 'v1.2.3' into an integer tuple."""
    cleaned = str(version or "").strip().lstrip("vV")
    nums = re.findall(r"\d+", cleaned.split("-", 1)[0].split("+", 1)[0])
    if not nums:
        return (0,)
    return tuple(int(part) for part in nums[:4])


def _github_get_json(url: str, current_version: str, timeout_s: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BenchVision/{current_version}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_cached_release_check(
    current_version: str, repo: str | None = None
) -> dict[str, Any] | None:
    """Return cached GitHub release metadata if still fresh."""
    eff_repo = repo or get_github_repo()
    key = (eff_repo, str(current_version))
    with _CACHE_LOCK:
        entry = _CHECK_CACHE.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if (time.monotonic() - ts) > _CACHE_TTL_S:
            return None
        return dict(payload)


def check_github_release(
    current_version: str,
    repo: str | None = None,
    timeout_s: float = 4.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Query GitHub Releases (falling back to Tags) for a newer BenchVision release."""
    eff_repo = repo or get_github_repo()
    key = (eff_repo, str(current_version))
    if not force_refresh:
        cached = get_cached_release_check(current_version, repo=eff_repo)
        if cached is not None:
            return cached

    releases_url = f"https://api.github.com/repos/{eff_repo}/releases/latest"
    fallback_release_page = f"https://github.com/{eff_repo}/releases"
    result: dict[str, Any] = {
        "github_repo": eff_repo,
        "current_version": str(current_version),
        "latest_version": str(current_version),
        "latest_tag": f"v{str(current_version).lstrip('vV')}",
        "update_available": False,
        "release_url": fallback_release_page,
        "download_url": None,
        "published_at": None,
        "release_notes": "",
        "check_status": "offline",
        "error": None,
    }

    try:
        try:
            payload = _github_get_json(releases_url, current_version, timeout_s)
            tag_name = str(payload.get("tag_name") or "").strip()
            latest_ver = tag_name.lstrip("vV") or str(current_version)
            assets = payload.get("assets") or []
            asset_url = next(
                (
                    str(a.get("browser_download_url"))
                    for a in assets
                    if str(a.get("name", "")).endswith(".tar.gz")
                    and a.get("browser_download_url")
                ),
                None,
            )
            dl_url = asset_url or payload.get("tarball_url") or (
                f"https://github.com/{eff_repo}/archive/refs/tags/{tag_name}.tar.gz"
                if tag_name
                else None
            )
            result.update(
                {
                    "latest_version": latest_ver,
                    "latest_tag": tag_name or f"v{latest_ver}",
                    "update_available": parse_semver(latest_ver) > parse_semver(current_version),
                    "release_url": str(payload.get("html_url") or fallback_release_page),
                    "download_url": str(dl_url) if dl_url else None,
                    "published_at": payload.get("published_at"),
                    "release_notes": str(payload.get("body") or "").strip(),
                    "check_status": "ok",
                }
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            tags_url = f"https://api.github.com/repos/{eff_repo}/tags?per_page=15"
            tags = _github_get_json(tags_url, current_version, timeout_s)
            if isinstance(tags, list) and tags:
                v_tags = [
                    str(t.get("name") or "")
                    for t in tags
                    if isinstance(t, dict) and t.get("name")
                ]
                v_tags.sort(key=parse_semver, reverse=True)
                best_tag = v_tags[0]
                latest_ver = best_tag.lstrip("vV") or str(current_version)
                result.update(
                    {
                        "latest_version": latest_ver,
                        "latest_tag": best_tag,
                        "update_available": (
                            parse_semver(latest_ver) > parse_semver(current_version)
                        ),
                        "release_url": f"https://github.com/{eff_repo}/releases/tag/{best_tag}",
                        "download_url": (
                            f"https://github.com/{eff_repo}/archive/refs/tags/{best_tag}.tar.gz"
                        ),
                        "check_status": "ok",
                    }
                )
            else:
                result["check_status"] = "no_releases"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        result["check_status"] = "offline"
        result["error"] = str(exc)

    if result["check_status"] in ("ok", "no_releases"):
        with _CACHE_LOCK:
            _CHECK_CACHE[key] = (time.monotonic(), dict(result))
    return result


def extract_release_archive_safely(archive_path: Path, app_dir: Path) -> list[str]:
    """Safely extract a GitHub .tar.gz release into app_dir without touching user data or venvs."""
    app_dir = Path(app_dir).resolve()
    archive_path = Path(archive_path).resolve()
    updated_files: list[str] = []

    with tempfile.TemporaryDirectory(prefix="bv_extract_") as tmp_dir_str:
        stage_root = Path(tmp_dir_str).resolve()
        with tarfile.open(archive_path, "r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                if member.issym() or member.islnk():
                    raise ValueError(f"Refusing link member in archive: {member.name}")
                raw_name = member.name.replace("\\", "/")
                if raw_name.startswith("/"):
                    raise ValueError(f"Refusing absolute path in archive: {member.name}")
                parts = [p for p in raw_name.split("/") if p and p != "."]
                if any(p == ".." for p in parts):
                    raise ValueError(f"Refusing path traversal in archive: {member.name}")
                if not parts or parts[0] in PROTECTED_ROOT_DIRS:
                    continue
                if any(p == "__pycache__" for p in parts) or (
                    member.isfile() and parts[-1].endswith(".pyc")
                ):
                    continue
                target = (stage_root / Path(*parts)).resolve()
                if stage_root != target and stage_root not in target.parents:
                    raise ValueError(f"Unsafe archive destination: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src_fh = tf.extractfile(member)
                    if src_fh is None:
                        continue
                    with src_fh, target.open("wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)

        top_entries = list(stage_root.iterdir())
        if (
            len(top_entries) == 1
            and top_entries[0].is_dir()
            and (
                (top_entries[0] / "src").exists()
                or (top_entries[0] / "pyproject.toml").exists()
                or (top_entries[0] / "run.py").exists()
            )
        ):
            payload_root = top_entries[0]
        else:
            payload_root = stage_root

        for src_file in sorted(payload_root.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(payload_root)
            if not rel.parts or rel.parts[0] in PROTECTED_ROOT_DIRS:
                continue
            dst_file = (app_dir / rel).resolve()
            if app_dir not in dst_file.parents:
                raise ValueError(f"Target outside app directory: {rel}")
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            updated_files.append(rel.as_posix())

    return updated_files


def _backup_app_files(app_dir: Path, backup_dir: Path) -> list[str]:
    backed_up: list[str] = []
    for name in _BACKUP_TARGETS:
        src = app_dir / name
        if not src.exists():
            continue
        dst = backup_dir / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        backed_up.append(name)
    return backed_up


def _restore_app_files(app_dir: Path, backup_dir: Path, names: list[str]) -> None:
    for name in names:
        bak = backup_dir / name
        dst = app_dir / name
        if not bak.exists():
            continue
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(bak, dst)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, dst)


def apply_github_release_update(
    app_dir: Path,
    current_version: str,
    repo: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download, verify, and apply the latest GitHub Release tarball with automatic rollback."""
    app_dir = Path(app_dir).resolve()
    rel = check_github_release(current_version, repo=repo, force_refresh=True)
    if rel.get("check_status") == "offline":
        return {
            "status": "error",
            "message": f"Unable to reach GitHub: {rel.get('error') or 'offline'}",
            **rel,
        }
    if not rel.get("update_available") and not force:
        return {
            "status": "up_to_date",
            "message": f"Already up to date on v{current_version}.",
            **rel,
        }
    dl_url = rel.get("download_url")
    if not dl_url:
        return {
            "status": "error",
            "message": "No downloadable release archive found on GitHub.",
            **rel,
        }

    with tempfile.TemporaryDirectory(prefix="bv_update_") as work_str:
        work_dir = Path(work_str)
        archive_path = work_dir / "release.tar.gz"
        backup_dir = work_dir / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)

        req = urllib.request.Request(
            str(dl_url),
            headers={"User-Agent": f"BenchVision/{current_version}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp, archive_path.open("wb") as out_fh:
                shutil.copyfileobj(resp, out_fh)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            return {
                "status": "error",
                "message": f"Download failed: {exc}",
                **rel,
            }

        backed_up = _backup_app_files(app_dir, backup_dir)
        try:
            updated_files = extract_release_archive_safely(archive_path, app_dir)
            py_exe = app_dir / ".venv" / "bin" / "python3"
            if not py_exe.exists():
                py_exe = Path(sys.executable)
            health_stmt = (
                "import sys; sys.path.insert(0, '.'); "
                "from src.main import create_app; assert create_app is not None"
            )
            subprocess.run(
                [str(py_exe), "-c", health_stmt],
                cwd=str(app_dir),
                check=True,
                capture_output=True,
                text=True,
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            _restore_app_files(app_dir, backup_dir, backed_up)
            return {
                "status": "error",
                "rolled_back": True,
                "message": f"Update validation failed and rolled back cleanly: {exc}",
                **rel,
            }

    restarted_service = False
    if shutil.which("systemctl"):
        try:
            res = subprocess.run(
                ["systemctl", "is-enabled", "benchvision.service"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                subprocess.Popen(
                    ["bash", "-c", "sleep 1.2 && sudo -n systemctl restart benchvision.service"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                restarted_service = True
        except Exception:  # noqa: BLE001, S110
            pass

    return {
        "status": "updated",
        "updated_count": len(updated_files),
        "restarting": restarted_service,
        "message": (
            f"Updated to v{rel.get('latest_version')} ({len(updated_files)} files)."
            + (" Service is restarting..." if restarted_service else " Restart BenchVision to load new code.")
        ),
        **rel,
    }
