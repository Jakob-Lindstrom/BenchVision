"""Phase (h) tests: install script, systemd service template, CI workflows, and rollback updater."""

from __future__ import annotations

import subprocess
from pathlib import Path

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

    update_text = update_sh.read_text(encoding="utf-8")
    assert "rollback" in update_text
    assert "/api/health" in update_text


def test_update_script_check_only() -> None:
    update_sh = ROOT / "scripts" / "update.sh"
    out = subprocess.check_output(["bash", str(update_sh), "--check-only"], text=True)
    assert "Current version: v0.1.0" in out


def test_ci_workflows_and_readme_present() -> None:
    assert (ROOT / ".github" / "workflows" / "ci.yml").exists()
    assert (ROOT / ".github" / "workflows" / "release.yml").exists()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/install.sh" in readme
    assert "http://" in readme

