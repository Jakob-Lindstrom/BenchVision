#!/usr/bin/env python3
"""
Convenience launcher for BenchVision on Raspberry Pi.
Automatically uses .venv/bin/python3 if invoked with system python3.
Run with: python3 test.py [--port 8000]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = ROOT_DIR / ".venv" / "bin" / "python3"
SRC_DIR = ROOT_DIR / "src"


def main() -> None:
    venv_dir = ROOT_DIR / ".venv"
    if VENV_PYTHON.exists() and Path(sys.prefix) != venv_dir:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    from benchvision.main import cli_main

    cli_main()


if __name__ == "__main__":
    main()
