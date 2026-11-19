from __future__ import annotations

import gc
import platform
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psutil

from app.core.config import settings
from app.core.timeutils import get_zone, utcnow

# Prime psutil's non-blocking CPU sampler. The first call always returns 0.0,
# so doing it at import keeps the HUD's first reading meaningful.
try:  # pragma: no cover - platform dependent
    psutil.cpu_percent(interval=None)
except Exception:  # noqa: BLE001
    pass

_volume_lock = threading.Lock()


class VolumeUnavailable(RuntimeError):
    pass


def _with_volume[T](operation: Callable[[Any], T]) -> T:
    """Run a volume operation inside a balanced COM apartment.

    pycaw/comtypes are imported lazily so the backend still starts on systems
    without the optional audio dependencies. Every COM wrapper is dropped
    before ``CoUninitialize``; releasing them afterwards makes comtypes call
    ``Release`` without a VTable and logs a noisy teardown error.
    """
    if platform.system() != "Windows":
        raise VolumeUnavailable("System volume control is only supported on Windows.")
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL, CoInitialize, CoUninitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception as exc:  # noqa: BLE001
        raise VolumeUnavailable(
            "Windows audio control is unavailable. Install pycaw and comtypes."
        ) from exc

    CoInitialize()
    device = interface = endpoint = None
    try:
        try:
            device = AudioUtilities.GetSpeakers()
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        except Exception as exc:  # noqa: BLE001
            raise VolumeUnavailable(
                f"Could not access the default audio endpoint: {exc}"
            ) from exc

        try:
            return operation(endpoint)
        except VolumeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VolumeUnavailable(f"System volume control failed: {exc}") from exc
    finally:
        # Drop the wrappers while COM is still initialised, then let the
        # interpreter collect any comtypes cycles before uninitialising.
        endpoint = None
        interface = None
        device = None
        gc.collect()
        CoUninitialize()


def read_volume() -> dict[str, Any]:
    def read(endpoint: Any) -> dict[str, Any]:  # noqa: ANN401 - COM pointer
        percent = round(float(endpoint.GetMasterVolumeLevelScalar()) * 100)
        return {
            "volume": max(0, min(100, percent)),
            "muted": bool(endpoint.GetMute()),
            "supported": True,
        }

    with _volume_lock:
        return _with_volume(read)


def set_volume(percent: int | None = None, muted: bool | None = None) -> dict[str, Any]:
    if percent is not None and not 0 <= int(percent) <= 100:
        raise ValueError("Volume must be between 0 and 100.")

    def apply(endpoint: Any) -> dict[str, Any]:  # noqa: ANN401 - COM pointer
        if percent is not None:
            endpoint.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100.0)), None)
            if percent > 0:
                endpoint.SetMute(0, None)
        if muted is not None:
            endpoint.SetMute(1 if muted else 0, None)
        actual = round(float(endpoint.GetMasterVolumeLevelScalar()) * 100)
        return {
            "volume": max(0, min(100, actual)),
            "muted": bool(endpoint.GetMute()),
            "supported": True,
        }

    with _volume_lock:
        return _with_volume(apply)


def _local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            # No packets are sent for an unconnected UDP socket.
            probe.connect(("1.1.1.1", 53))
            return probe.getsockname()[0]
    except OSError:
        return None


def _network() -> dict[str, Any]:
    try:
        stats = psutil.net_if_stats()
        adapters = [
            name
            for name, stat in stats.items()
            if stat.isup and not name.lower().startswith(("loopback", "lo"))
        ]
        counters = psutil.net_io_counters()
        online = bool(adapters) and _local_ip() is not None
        return {
            "online": online,
            "adapters": adapters,
            "ip": _local_ip(),
            "bytes_sent": counters.bytes_sent if counters else 0,
            "bytes_recv": counters.bytes_recv if counters else 0,
        }
    except Exception:  # noqa: BLE001
        return {
            "online": False,
            "adapters": [],
            "ip": None,
            "bytes_sent": 0,
            "bytes_recv": 0,
        }


def system_status(timezone: str | None = None) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    battery: dict[str, Any] | None = None
    try:
        sensor = psutil.sensors_battery()
        if sensor is not None:
            battery = {
                "percent": round(sensor.percent),
                "plugged": bool(sensor.power_plugged),
                "seconds_left": None if sensor.power_plugged else sensor.secsleft,
            }
    except Exception:  # noqa: BLE001
        battery = None

    tz_name = timezone or settings.default_timezone or "UTC"
    now_utc = utcnow()
    try:
        now_local = now_utc.astimezone(get_zone(tz_name))
    except Exception:  # noqa: BLE001
        tz_name = "UTC"
        now_local = now_utc.astimezone(get_zone("UTC"))

    volume: dict[str, Any]
    try:
        volume = read_volume()
    except VolumeUnavailable as exc:
        volume = {"volume": None, "muted": None, "supported": False, "detail": str(exc)}

    return {
        "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 1),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_percent": round(float(memory.percent), 1),
        "memory_used": int(memory.used),
        "memory_total": int(memory.total),
        "disk_percent": round(float(psutil.disk_usage("/").percent), 1),
        "battery": battery,
        "network": _network(),
        "volume": volume,
        "uptime_seconds": int(max(0, time.time() - psutil.boot_time())),
        "platform": platform.system(),
        "hostname": socket.gethostname(),
        "timezone": tz_name,
        "time": now_local.isoformat(),
        "date": now_local.strftime("%A, %d %B %Y"),
        "clock": now_local.strftime("%H:%M:%S"),
        "generated_at": datetime.now(tz=now_local.tzinfo).isoformat(),
    }


def current_time(timezone: str | None = None) -> dict[str, Any]:
    tz_name = timezone or settings.default_timezone or "UTC"
    now = utcnow().astimezone(get_zone(tz_name))
    return {
        "timezone": tz_name,
        "iso": now.isoformat(),
        "clock": now.strftime("%H:%M"),
        "seconds": now.strftime("%H:%M:%S"),
        "date": now.strftime("%A, %d %B %Y"),
        "day_period": (
            "morning"
            if now.hour < 12
            else "afternoon"
            if now.hour < 17
            else "evening"
            if now.hour < 21
            else "night"
        ),
    }
