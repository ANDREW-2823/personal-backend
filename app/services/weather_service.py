from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.core.config import settings as env_settings
from app.services.settings_service import get_settings

logger = logging.getLogger("jarvis.weather")

_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "value": None}
_CACHE_SECONDS = 600

# WMO weather interpretation codes used by Open-Meteo.
_CONDITIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Rain showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Severe thunderstorm",
}


def _condition(code: int | None) -> str:
    return _CONDITIONS.get(int(code or 0), "Unknown conditions")


def _disabled(note: str, enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "available": False,
        "note": note,
        "location": None,
        "temperature": None,
        "condition": None,
        "updated_at": None,
        "hourly": [],
    }


def get_weather(db) -> dict[str, Any]:  # noqa: ANN001
    app_settings = get_settings(db)
    if not app_settings.weather_enabled:
        return _disabled("Weather is disabled. Enable it and set a location in Settings.")
    location = (app_settings.weather_location or "").strip()
    if not location:
        return _disabled("Weather is enabled but no location is configured.", enabled=True)

    key = location.lower()
    now = time.monotonic()
    if _CACHE["key"] == key and _CACHE["value"] is not None and now - _CACHE["at"] < _CACHE_SECONDS:
        return _CACHE["value"]

    try:
        with httpx.Client(timeout=8.0) as client:
            geo_response = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "en", "format": "json"},
            )
            geo_response.raise_for_status()
            results = geo_response.json().get("results") or []
            if not results:
                return _disabled(f"No weather location matched “{location}”.", enabled=True)
            place = results[0]
            latitude = place["latitude"]
            longitude = place["longitude"]

            forecast_response = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "is_day,precipitation,weather_code,wind_speed_10m"
                    ),
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "hourly": "temperature_2m,weather_code,precipitation_probability",
                    "timezone": "auto",
                    "forecast_days": 2,
                },
            )
            forecast_response.raise_for_status()
            payload = forecast_response.json()
    except httpx.HTTPError as exc:
        logger.warning("Weather request failed: %s", exc)
        return _disabled(
            "Live weather is temporarily unavailable. Check your connection.",
            enabled=True,
        )

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    precipitation = hourly.get("precipitation_probability") or []

    next_hours: list[dict[str, Any]] = []
    current_time = current.get("time")
    for index, stamp in enumerate(times):
        if current_time and stamp < current_time:
            continue
        if len(next_hours) >= 8:
            break
        next_hours.append(
            {
                "time": stamp,
                "temperature": temps[index] if index < len(temps) else None,
                "code": codes[index] if index < len(codes) else None,
                "condition": _condition(codes[index] if index < len(codes) else None),
                "precipitation_probability": (
                    precipitation[index] if index < len(precipitation) else None
                ),
            }
        )
        if len(times) and stamp == current_time:
            # Start with the current hour, then keep the next seven.
            continue

    display_name = ", ".join(
        part
        for part in [place.get("name"), place.get("admin1"), place.get("country")]
        if part
    )
    value = {
        "enabled": True,
        "available": True,
        "note": None,
        "location": {
            "name": place.get("name"),
            "region": place.get("admin1"),
            "country": place.get("country"),
            "label": display_name,
            "latitude": latitude,
            "longitude": longitude,
        },
        "timezone": payload.get("timezone"),
        "temperature": current.get("temperature_2m"),
        "apparent_temperature": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "precipitation": current.get("precipitation"),
        "is_day": bool(current.get("is_day", 1)),
        "weather_code": current.get("weather_code"),
        "condition": _condition(current.get("weather_code")),
        "high": (daily.get("temperature_2m_max") or [None])[0],
        "low": (daily.get("temperature_2m_min") or [None])[0],
        "hourly": next_hours,
        "observed_at": current.get("time"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _CACHE.update({"key": key, "at": now, "value": value})
    return value


def clear_cache() -> None:
    _CACHE.update({"key": None, "at": 0.0, "value": None})


# Keep the module importable even when weather is not configured.
def weather_configured() -> bool:
    return bool(env_settings.weather_enabled)
