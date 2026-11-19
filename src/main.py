"""FastAPI application entrypoint, state container, and CLI runner."""

from __future__ import annotations

import argparse
import os
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import __version__
from src.api.routes import router as api_router
from src.cameras.manager import CameraManager
from src.core.config import DEFAULT_DATA_DIR, load_or_create_settings
from src.core.hardware import detect_hardware_profile
from src.core.logging import configure_logging
from src.jobs.manager import JobQueueManager
from src.recording.preview import LivePreviewService
from src.recording.recorder import RecordingService
from src.storage.db import Database
from src.storage.layout import DataLayout


class BenchVisionState:
    """Holds initialized services for a BenchVision application instance."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        start_worker: bool = True,
        realtime_synthetic: bool = True,
    ) -> None:
        self.settings = load_or_create_settings(data_dir)
        self.layout = DataLayout(self.settings.data_dir)
        configure_logging(self.layout.root, level=self.settings.log_level)
        self.db = Database(self.layout.db_path)
        self.camera_manager = CameraManager(
            self.settings, realtime_synthetic=realtime_synthetic
        )
        # Auto-detect cameras on startup; if a real USB/CSI camera is present and still default, select it
        discovered = self.camera_manager.discover_all()
        if (
            realtime_synthetic
            and not self.settings.setup_completed
            and self.settings.active_camera_id == "synthetic:0"
        ):
            hw_cam = next(
                (c for c in discovered if c["backend_type"] in ("picamera2", "v4l2")),
                None,
            )
            if hw_cam is not None:
                self.settings.active_camera_id = hw_cam["camera_id"]

        self.preview_service = LivePreviewService(self.camera_manager)
        self.recording_service = RecordingService(
            layout=self.layout,
            db=self.db,
            camera_manager=self.camera_manager,
            preview_service=self.preview_service,
        )
        self.job_manager = JobQueueManager(
            layout=self.layout, db=self.db, start_worker=start_worker
        )

    def shutdown(self) -> None:
        self.job_manager.shutdown()
        self.preview_service.shutdown()
        self.camera_manager.shutdown()


def create_app(
    data_dir: Path | str | None = None,
    start_worker: bool = True,
    realtime_synthetic: bool = True,
) -> FastAPI:
    """Create and configure the BenchVision FastAPI application."""
    state = BenchVisionState(
        data_dir=data_dir,
        start_worker=start_worker,
        realtime_synthetic=realtime_synthetic,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        state.shutdown()

    app = FastAPI(
        title="BenchVision — Raspberry Pi Motion & Video Magnification",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.bv = state
    app.include_router(api_router)

    static_dir = Path(__file__).resolve().parent / "ui" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.middleware("http")
    async def disable_static_cache(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @app.get("/")
    def serve_spa_index() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )

    return app


def find_open_port(host: str, preferred_port: int) -> int:
    """Find preferred_port or the next available TCP port if preferred_port is busy."""
    for port in range(preferred_port, preferred_port + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    return preferred_port


def get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
    except OSError:
        return "localhost"


def resolve_data_dir(raw_dir: str | Path | None) -> Path:
    """Resolve data directory reliably relative to the repository root when default."""
    if raw_dir is None or str(raw_dir).strip() in ("", "./data", "data"):
        return DEFAULT_DATA_DIR.resolve()
    return Path(raw_dir).resolve()


def print_startup_banner(
    host: str,
    port: int,
    data_dir: Path,
    reload_enabled: bool = False,
) -> None:
    """Print a clean startup banner with URLs, hardware profile, and active data directory."""
    ip = get_local_ip()
    hw = detect_hardware_profile()
    hw_model = hw.get("model", "Raspberry Pi 4 / 5")
    ram_gb = hw.get("total_ram_gb", 0.0)
    print("=" * 68)
    print(f"  BenchVision v{__version__} — Raspberry Pi 4 & 5 Optical Vibrometer")
    print("=" * 68)
    print(f"  Local URL:      http://localhost:{port}")
    print(f"  LAN IP URL:     http://{ip}:{port}  (bind: {host})")
    print(f"  Hardware Model: {hw_model} ({ram_gb} GB RAM)")
    print(f"  Data Directory: {data_dir}")
    if reload_enabled:
        print("  Dev Reload:     enabled")
    print("=" * 68)


def cli_main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for launching the BenchVision server."""
    parser = argparse.ArgumentParser(
        description="BenchVision Optical Vibrometer & Motion Magnification Server"
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
        help="Enable auto-reload on code changes for development",
    )
    args = parser.parse_args(argv)

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
        pkg_dir = Path(__file__).resolve().parent
        uvicorn.run(
            "src.main:create_app",
            factory=True,
            host=args.host,
            port=bound_port,
            reload=True,
            reload_dirs=[str(pkg_dir)],
            log_level="info",
        )
    else:
        app = create_app(data_dir=resolved_data_dir)
        uvicorn.run(app, host=args.host, port=bound_port, log_level="info")


if __name__ == "__main__":
    cli_main()

