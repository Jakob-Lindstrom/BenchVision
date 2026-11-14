#!/usr/bin/env python3
"""
Root launcher for BenchVision — Optical Vibrometer & Motion Magnification Studio.
Automatically uses .venv/bin/python3 if present and invoked with system python.

Usage:
    python run.py [--host 0.0.0.0] [--port 8000] [--data-dir ./data] [--reload]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = ROOT_DIR / ".venv" / "bin" / "python3"


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for BenchVision."""
    parser = argparse.ArgumentParser(
        description="BenchVision Optical Vibrometer & Motion Magnification Studio"
    )
    parser.add_argument(
        "--host",
        type=str,
        default=os.environ.get("BENCHVISION_HOST", "0.0.0.0"),
        help="Bind host address (default: 0.0.0.0 for LAN access on headless Raspberry Pi)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=int(os.environ.get("BENCHVISION_PORT", os.environ.get("PORT", "8000"))),
        help="HTTP port (default: 8000, auto-fallbacks if busy)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=os.environ.get("BENCHVISION_DATA_DIR", "./data"),
        help="Path to self-contained user data directory (default: ./data)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable optional development auto-reload on source changes",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse CLI arguments, print the startup banner, and start the server."""
    venv_dir = ROOT_DIR / ".venv"
    if (
        argv is None
        and VENV_PYTHON.exists()
        and Path(sys.prefix).resolve() != venv_dir.resolve()
    ):
        os.execv(
            str(VENV_PYTHON),
            [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        )

    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    import uvicorn

    from src.main import (
        create_app,
        find_open_port,
        print_startup_banner,
        resolve_data_dir,
    )

    bound_port = find_open_port(args.host, args.port)
    if bound_port != args.port:
        print(f"Notice: Port {args.port} is in use; switching to port {bound_port}.")

    resolved_data_dir = resolve_data_dir(args.data_dir)
    os.environ["BENCHVISION_DATA_DIR"] = str(resolved_data_dir)
    os.environ["BENCHVISION_HOST"] = args.host
    os.environ["BENCHVISION_PORT"] = str(bound_port)

    print_startup_banner(
        host=args.host,
        port=bound_port,
        data_dir=resolved_data_dir,
        reload_enabled=args.reload,
    )

    if args.reload:
        uvicorn.run(
            "src.main:create_app",
            factory=True,
            host=args.host,
            port=bound_port,
            reload=True,
            reload_dirs=[str(ROOT_DIR / "src")],
            log_level="info",
        )
    else:
        app = create_app(data_dir=resolved_data_dir)
        uvicorn.run(app, host=args.host, port=bound_port, log_level="info")


if __name__ == "__main__":
    main()
