"""Phase (b) tests: V4L2 USB webcam backend, Picamera2 CSI profiles, and CameraManager."""

from __future__ import annotations

from src.cameras import CAMERA_REGISTRY, CameraManager
from src.cameras.picamera2_backend import (
    Picamera2Backend,
    kelvin_to_colour_gains,
    resolve_sensor_profile,
)
from src.cameras.v4l2_usb import (
    V4L2UsbCameraBackend,
    parse_v4l2_ctrls_output,
    parse_v4l2_devices_output,
)
from src.core.config import AppSettings

SAMPLE_V4L2_DEVICES = """bcm2835-codec-decode (platform:bcm2835-codec):
\t/dev/video10
\t/dev/video11

bcm2835-isp (platform:bcm2835-isp):
\t/dev/video13
\t/dev/media1

C270 HD WEBCAM (usb-0000:01:00.0-1.2):
\t/dev/video0
\t/dev/video1
\t/dev/media4
"""

SAMPLE_V4L2_CTRLS = """
                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
                           gain 0x00980913 (int)    : min=0 max=255 step=1 default=64 value=32
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=4000 value=4600 flags=inactive
                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=10000 step=1 default=166 value=100 flags=inactive
"""


def test_parse_v4l2_devices_filters_platform_isp() -> None:
    devices = parse_v4l2_devices_output(SAMPLE_V4L2_DEVICES)
    assert len(devices) == 1
    assert devices[0]["camera_id"] == "v4l2:/dev/video0"
    assert devices[0]["name"] == "C270 HD WEBCAM"
    assert devices[0]["shutter_type"] == "rolling"


def test_parse_v4l2_ctrls_extracts_ranges() -> None:
    ctrls = parse_v4l2_ctrls_output(SAMPLE_V4L2_CTRLS)
    assert "exposure_time_absolute" in ctrls
    assert ctrls["exposure_time_absolute"].min_val == 1.0
    assert ctrls["exposure_time_absolute"].max_val == 10000.0
    assert "gain" in ctrls
    assert ctrls["gain"].current_val == 32.0


def test_picamera2_sensor_profiles_and_shutter_warnings() -> None:
    gs = resolve_sensor_profile("imx296")
    assert gs["shutter_type"] == "global"

    mod3 = resolve_sensor_profile("imx708")
    assert mod3["shutter_type"] == "rolling"
    assert mod3["focus_mode"] == "autofocus"

    hq = resolve_sensor_profile("imx477")
    assert hq["shutter_type"] == "rolling"
    assert hq["focal_length_mm"] == 16.0

    cam_gs = Picamera2Backend(sensor_model="imx296")
    caps_gs = cam_gs.list_capabilities()
    assert caps_gs.shutter_type == "global"
    assert caps_gs.rolling_shutter_warning is None

    cam_v2 = Picamera2Backend(sensor_model="imx219")
    caps_v2 = cam_v2.list_capabilities()
    assert caps_v2.shutter_type == "rolling"
    assert caps_v2.rolling_shutter_warning is not None
    assert "Global Shutter" in caps_v2.rolling_shutter_warning


def test_camera_manager_discovery_and_lens_config(app_settings: AppSettings) -> None:
    assert "v4l2" in CAMERA_REGISTRY.list_keys()
    assert "picamera2" in CAMERA_REGISTRY.list_keys()
    assert "synthetic" in CAMERA_REGISTRY.list_keys()

    mgr = CameraManager(app_settings, realtime_synthetic=False)
    discovered = mgr.discover_all()
    assert any(d["camera_id"] == "synthetic:0" for d in discovered)

    # Update lens config for V4L2 camera and verify persistence
    lens = mgr.update_lens_config(
        "v4l2:/dev/video0",
        {"focal_length_mm": 3.6, "fov_degrees": 58.0, "focus_mode": "fixed"},
    )
    assert lens.focal_length_mm == 3.6

    usb_cam = V4L2UsbCameraBackend(
        camera_id="v4l2:/dev/video0", device_path="/dev/video0", name="C270 HD WEBCAM"
    )
    caps = mgr.get_capabilities(usb_cam)
    assert caps.lens_info["focal_length_mm"] == 3.6
    assert caps.rolling_shutter_warning is not None


def test_picamera2_colour_gains_and_stream_tracking() -> None:
    r_mid, b_mid = kelvin_to_colour_gains(4600)
    assert (r_mid, b_mid) == (1.65, 1.55)

    r_warm, b_warm = kelvin_to_colour_gains(2500)
    r_cool, b_cool = kelvin_to_colour_gains(8000)
    assert 0.8 <= r_warm < r_mid < r_cool <= 3.2
    assert 0.8 <= b_cool < b_mid < b_warm <= 3.2

    cam = Picamera2Backend(sensor_model="imx296")
    assert cam.is_streaming_at(640, 480, 30) is False
    cam.running = True
    cam._stream_width = 1456
    cam._stream_height = 1088
    cam._stream_fps = 60
    assert cam.is_streaming_at(1456, 1088, 60) is True
    assert cam.is_streaming_at(640, 480, 30) is False
    cam.stop()
    assert cam.is_streaming_at(1456, 1088, 60) is False

