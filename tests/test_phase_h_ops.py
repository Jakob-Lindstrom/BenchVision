"""Phase (h) tests: install script, systemd service template, CI workflows, and rollback updater."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_scripts_exist_and_valid_bash_syntax() -> None:
    install_sh = ROOT / "scripts" / "install.sh"
    update_sh = ROOT / "scripts" / "update.sh"
    service_unit = ROOT / "scripts" / "benchvision.service"

    assert install_sh.exists()
    assert update_sh.exists()
    assert service_unit.exists()

    # Check bash syntax with `bash -n`
    subprocess.run(["bash", "-n", str(install_sh)], check=True)
    subprocess.run(["bash", "-n", str(update_sh)], check=True)

    install_text = install_sh.read_text(encoding="utf-8")
    assert "--system-site-packages" in install_text
    assert "python3-picamera2" in install_text
    assert "git clone" in install_text
    assert "/etc/sudoers.d/benchvision" in install_text

    update_text = update_sh.read_text(encoding="utf-8")
    assert "rollback" in update_text
    assert "/api/health" in update_text


def test_update_script_check_only() -> None:
    update_sh = ROOT / "scripts" / "update.sh"
    out = subprocess.check_output(["bash", str(update_sh), "--check-only"], text=True)
    assert f"Current version: v{__version__}" in out


def test_ci_workflows_and_readme_present() -> None:
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()
    assert (ROOT / ".github" / "workflows" / "release.yml").exists()
    assert (ROOT / "CHANGELOG.md").exists()
    assert (ROOT / "src" / "main.py").exists()
    assert not (ROOT / "benchvision").exists()
    assert not (ROOT / "src" / "benchvision").exists()
    assert (ROOT / "run.py").exists()
    assert not (ROOT / "test.py").exists()
    assert (ROOT / "requirements.txt").exists()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/install.sh" in readme
    assert "http://" in readme
    assert "--system-site-packages" in readme

    help_out = subprocess.check_output(
        [str(ROOT / ".venv" / "bin" / "python3"), str(ROOT / "run.py"), "--help"],
        text=True,
    )
    assert "--host" in help_out
    assert "--port" in help_out
    assert "--data-dir" in help_out
    assert "--reload" in help_out


def test_github_updater_semver_and_release_checks(monkeypatch, tmp_path: Path) -> None:
    import io
    import urllib.error

    from fastapi.testclient import TestClient

    from src.core import updater
    from src.main import create_app

    assert updater.parse_semver("v1.2.3") > updater.parse_semver("1.0.0")
    assert updater.parse_semver("v2.0") > updater.parse_semver("v1.9.9")

    # 1. Published GitHub Release with .tar.gz asset
    def fake_releases_json(url: str, current_version: str, timeout_s: float) -> dict:
        assert "releases/latest" in url
        return {
            "tag_name": "v1.2.0",
            "html_url": "https://github.com/Jakob-Lindstrom/BenchVision/releases/tag/v1.2.0",
            "published_at": "2026-12-01T10:00:00Z",
            "body": "Faster Riesz pyramid",
            "assets": [
                {
                    "name": "benchvision-v1.2.0.tar.gz",
                    "browser_download_url": "https://example.invalid/benchvision-v1.2.0.tar.gz",
                }
            ],
        }

    monkeypatch.setattr(updater, "_github_get_json", fake_releases_json)
    rel = updater.check_github_release(
        "1.0.0", repo="Jakob-Lindstrom/BenchVision", force_refresh=True
    )
    assert rel["check_status"] == "ok"
    assert rel["latest_version"] == "1.2.0"
    assert rel["update_available"] is True
    assert rel["download_url"] == "https://example.invalid/benchvision-v1.2.0.tar.gz"

    # 2. HTTP 404 on /releases/latest falls back cleanly to /tags
    def fake_tags_fallback(url: str, current_version: str, timeout_s: float):
        if "releases/latest" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        return [{"name": "v1.1.0"}, {"name": "v0.7.0"}]

    monkeypatch.setattr(updater, "_github_get_json", fake_tags_fallback)
    rel_tag = updater.check_github_release(
        "1.0.0", repo="Jakob-Lindstrom/BenchVision", force_refresh=True
    )
    assert rel_tag["check_status"] == "ok"
    assert rel_tag["latest_version"] == "1.1.0"
    assert rel_tag["update_available"] is True
    assert "archive/refs/tags/v1.1.0.tar.gz" in (rel_tag["download_url"] or "")

    # 3. Offline error degrades cleanly without raising
    def fake_offline(url: str, current_version: str, timeout_s: float):
        raise urllib.error.URLError("Name resolution failure")

    monkeypatch.setattr(updater, "_github_get_json", fake_offline)
    rel_off = updater.check_github_release(
        "1.0.0", repo="Jakob-Lindstrom/BenchVision", force_refresh=True
    )
    assert rel_off["check_status"] == "offline"
    assert rel_off["update_available"] is False

    # 4. API & System UI expose github_repo and update controls cleanly offline
    app = create_app(data_dir=tmp_path / "data", start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        sys_data = client.get("/api/system").json()
        assert sys_data["github_repo"] == "Jakob-Lindstrom/BenchVision"
        html = client.get("/").text
        for token in ("btn-check-updates", "sys-latest-version", "update-status-banner"):
            assert token in html


def test_safe_release_archive_extraction_preserves_data_and_blocks_traversal(
    tmp_path: Path,
) -> None:
    import io
    import tarfile

    import pytest

    from src.core.updater import extract_release_archive_safely

    app_dir = tmp_path / "app"
    (app_dir / "src").mkdir(parents=True)
    (app_dir / "data").mkdir(parents=True)
    (app_dir / ".venv").mkdir(parents=True)
    (app_dir / "src" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (app_dir / "data" / "keep.txt").write_text("user clip", encoding="utf-8")
    (app_dir / ".venv" / "marker.txt").write_text("keep venv", encoding="utf-8")

    good_tar = tmp_path / "good.tar.gz"
    with tarfile.open(good_tar, "w:gz") as tf:
        payloads = {
            "BenchVision-1.2.0/src/__init__.py": b'__version__ = "1.2.0"\n',
            "BenchVision-1.2.0/README.md": b"# Updated\n",
            "BenchVision-1.2.0/data/keep.txt": b"OVERWRITTEN",
        }
        for arcname, raw in payloads.items():
            info = tarfile.TarInfo(arcname)
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))

    updated = extract_release_archive_safely(good_tar, app_dir)
    assert "src/__init__.py" in updated
    assert '1.2.0' in (app_dir / "src" / "__init__.py").read_text(encoding="utf-8")
    assert (app_dir / "data" / "keep.txt").read_text(encoding="utf-8") == "user clip"
    assert (app_dir / ".venv" / "marker.txt").read_text(encoding="utf-8") == "keep venv"

    bad_tar = tmp_path / "bad.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tf:
        raw = b"pwned"
        info = tarfile.TarInfo("../../evil.txt")
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))

    with pytest.raises(ValueError, match="traversal"):
        extract_release_archive_safely(bad_tar, app_dir)


def test_systemd_autostart_status_and_toggle_api(monkeypatch, tmp_path: Path) -> None:
    import types

    from fastapi.testclient import TestClient

    from src.core import updater
    from src.main import create_app

    calls: list[list[str]] = []
    enabled_state = {"enabled": True}

    monkeypatch.setattr(
        updater.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )

    def fake_run(cmd: list[str], **_kwargs) -> types.SimpleNamespace:
        calls.append(cmd)
        if "is-enabled" in cmd:
            return types.SimpleNamespace(
                returncode=0 if enabled_state["enabled"] else 1,
                stdout="enabled\n" if enabled_state["enabled"] else "disabled\n",
                stderr="",
            )
        if "is-active" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if "disable" in cmd:
            enabled_state["enabled"] = False
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "enable" in cmd:
            enabled_state["enabled"] = True
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    app = create_app(data_dir=tmp_path / "data", start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        sys_data = client.get("/api/system").json()
        assert sys_data["service"]["systemd_available"] is True
        assert sys_data["service"]["enabled_on_boot"] is True

        dis = client.post("/api/system/service", json={"enable_on_boot": False}).json()
        assert dis["status"] == "ok"
        assert dis["enabled_on_boot"] is False
        assert any("disable" in c for c in calls)

        ena = client.post("/api/system/service", json={"enable_on_boot": True}).json()
        assert ena["status"] == "ok"
        assert ena["enabled_on_boot"] is True
        assert any("enable" in c for c in calls)

        html = client.get("/").text
        assert "sys-service-status" in html
        assert "btn-toggle-service" in html
        assert "service-status-banner" in html


def test_systemd_autostart_sudo_password_flow(monkeypatch, tmp_path: Path) -> None:
    import types

    from fastapi.testclient import TestClient

    from src.core import updater
    from src.main import create_app

    seen_calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(
        updater.shutil,
        "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )
    monkeypatch.setattr(updater, "SYSTEMD_UNIT_PATH", tmp_path / "benchvision.service")
    monkeypatch.setattr(updater, "SYSTEMD_SUDOERS_PATH", tmp_path / "benchvision.sudoers")

    def fake_run(cmd: list[str], **kwargs) -> types.SimpleNamespace:
        stdin_pw = kwargs.get("input")
        seen_calls.append((cmd, stdin_pw))
        if "is-enabled" in cmd:
            return types.SimpleNamespace(returncode=1, stdout="disabled\n", stderr="")
        if "is-active" in cmd:
            return types.SimpleNamespace(returncode=1, stdout="inactive\n", stderr="")
        if "-n" in cmd:
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="sudo: a password is required",
            )
        if "-S" in cmd:
            if stdin_pw == "wrong-pw\n":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="sudo: 1 incorrect password attempt\nSorry, try again.",
                )
            if stdin_pw == "pi-secret\n":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    app = create_app(data_dir=tmp_path / "data", start_worker=False, realtime_synthetic=False)
    with TestClient(app) as client:
        # 1. Non-interactive toggle without sudoers asks for password
        res_need = client.post("/api/system/service", json={"enable_on_boot": True}).json()
        assert res_need["status"] == "needs_password"
        assert res_need["requires_password"] is True

        # 2. Wrong password returns bad_password immediately without hammering sudo
        bad_start_count = len(seen_calls)
        res_bad = client.post(
            "/api/system/service",
            json={"enable_on_boot": True, "sudo_password": "wrong-pw"},
        ).json()
        assert res_bad["status"] == "bad_password"
        assert res_bad["requires_password"] is True
        bad_sudo_cmds = [c for c, _ in seen_calls[bad_start_count:] if "-S" in c]
        assert len(bad_sudo_cmds) == 1

        # 3. Valid password uses sudo -S with stdin and succeeds
        res_ok = client.post(
            "/api/system/service",
            json={"enable_on_boot": True, "sudo_password": "pi-secret"},
        ).json()
        assert res_ok["status"] == "ok"
        assert res_ok["enabled_on_boot"] is True
        ok_sudo_calls = [(c, pw) for c, pw in seen_calls if "-S" in c and pw == "pi-secret\n"]
        assert any("enable" in c for c, _ in ok_sudo_calls)
        assert any(str(updater.SYSTEMD_SUDOERS_PATH) in " ".join(c) for c, _ in ok_sudo_calls)



