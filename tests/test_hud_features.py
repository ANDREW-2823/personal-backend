from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from app.ai import mock_assistant, tools
from app.core.ratelimit import SlidingWindowLimiter
from app.services import spotify_service, system_service, voice_service
from app.services.desktop_bridge import desktop_hub
from app.services.settings_service import get_settings
from fastapi import HTTPException

REQUIRED_TOOLS = {
    "launch_application",
    "open_website",
    "search_web",
    "control_spotify",
    "control_system_volume",
    "get_system_status",
    "create_reminder",
    "list_reminders",
    "cancel_reminder",
    "get_current_time",
}


# ------------------------------------------------------------------ tool schema


def test_required_desktop_tools_are_registered() -> None:
    assert REQUIRED_TOOLS <= set(tools.TOOLS)

    schemas = {item["function"]["name"]: item["function"] for item in tools.tool_schemas()}
    assert set(schemas) == set(tools.TOOLS)
    assert schemas["launch_application"]["parameters"]["required"] == ["application"]
    assert "level" in schemas["control_system_volume"]["parameters"]["properties"]


def test_tool_approval_policy() -> None:
    # App launches are authorized by the Electron allowlist, so common apps open
    # immediately without a per-message approval card.
    assert tools.TOOLS["launch_application"].requires_approval is False
    assert tools.TOOLS["cancel_reminder"].requires_approval is True
    assert tools.TOOLS["open_website"].requires_approval is False
    assert tools.TOOLS["control_system_volume"].requires_approval is False


def test_tool_validation_rejects_out_of_range_volume() -> None:
    for level in (-1, 101):
        with pytest.raises(tools.ToolError):
            tools.validate_tool_call("control_system_volume", {"action": "set", "level": level})

    parsed = tools.validate_tool_call("control_system_volume", {"action": "set", "level": 0})
    assert parsed.level == 0
    parsed = tools.validate_tool_call("control_system_volume", {"action": "set", "level": 100})
    assert parsed.level == 100


def test_unknown_tool_and_missing_required_argument_fail_closed() -> None:
    with pytest.raises(tools.ToolError):
        tools.validate_tool_call("delete_everything", {})
    with pytest.raises(tools.ToolError):
        tools.validate_tool_call("launch_application", {})

    result = tools.execute_tool(SimpleNamespace(), "delete_everything", {})  # type: ignore[arg-type]
    assert result["ok"] is False
    assert "Unknown tool" in result["message"]


def test_launch_tool_does_not_execute_without_desktop_agent(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        desktop_hub,
        "request_sync",
        lambda *args, **kwargs: {"ok": False, "detail": "Desktop agent is not connected."},
    )
    result = tools.execute_tool(db_session, "launch_application", {"application": "Spotify"})
    assert result["ok"] is False
    assert "Desktop agent is not connected" in result["message"]


def test_offline_open_spotify_routes_to_app_launch() -> None:
    assert mock_assistant._strip_wake("Hello Jarvis, open Spotify.") == "open Spotify."
    turn = mock_assistant._spotify_turn("open Spotify", "open spotify")
    assert turn is not None
    assert turn.tool_calls[0]["name"] == "launch_application"
    assert turn.tool_calls[0]["arguments"] == {"application": "spotify"}


def test_offline_open_spotify_with_greeting_launches_the_app(db_session) -> None:
    turn = mock_assistant.reply(
        db_session, "Hello Jarvis, open Spotify.", get_settings(db_session)
    )
    assert [call["name"] for call in turn.tool_calls] == ["launch_application"]


def test_spotify_open_uses_desktop_app_not_oauth(db_session, monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(action: str, **kwargs):
        calls.append(action)
        return {"ok": True, "detail": "Launched Spotify.exe"}

    monkeypatch.setattr(desktop_hub, "request_sync", fake_request)
    result = tools.control_spotify_tool(
        db_session, tools.SpotifyControlParams(action="open")
    )
    assert result["ok"] is True
    assert calls == ["launch_app"]
    assert "Spotify" in result["message"]


# ------------------------------------------------------------------- reminders


def test_reminder_tools_create_list_and_cancel(db_session) -> None:
    created = tools.execute_tool(
        db_session,
        "create_reminder",
        {"title": "Hydrate", "in_minutes": 30, "repeat": "none"},
    )
    assert created["ok"] is True
    reminder_id = created["data"]["reminder_id"]
    assert reminder_id

    listed = tools.execute_tool(db_session, "list_reminders", {"limit": 10})
    assert listed["ok"] is True
    assert "Hydrate" in listed["message"]
    assert listed["data"][0]["id"] == reminder_id

    cancelled = tools.execute_tool(db_session, "cancel_reminder", {"title": "hydrate"})
    assert cancelled["ok"] is True
    assert cancelled["data"]["reminder_id"] == reminder_id

    empty = tools.execute_tool(db_session, "list_reminders", {"limit": 10})
    assert empty["ok"] is True
    assert empty["data"] == []


def test_ambiguous_reminder_cancel_asks_for_clarification(db_session) -> None:
    for title in ("Stand up", "Stand up and stretch"):
        assert tools.execute_tool(
            db_session, "create_reminder", {"title": title, "in_minutes": 15}
        )["ok"]

    result = tools.execute_tool(db_session, "cancel_reminder", {"title": "stand up"})
    assert result["ok"] is False
    assert "Which one should I cancel" in result["message"]


# --------------------------------------------------------------------- volume


def test_system_volume_endpoint_validates_bounds(client, auth_headers, monkeypatch) -> None:
    for level in (-1, 101):
        response = client.put(
            "/api/v1/system/volume", headers=auth_headers, json={"level": level}
        )
        assert response.status_code == 422

    calls: list[tuple[int | None, bool | None]] = []

    def fake_set(level=None, muted=None):  # noqa: ANN001
        calls.append((level, muted))
        return {"volume": level, "muted": bool(muted), "supported": True}

    monkeypatch.setattr(system_service, "set_volume", fake_set)
    response = client.put("/api/v1/system/volume", headers=auth_headers, json={"level": 35})
    assert response.status_code == 200
    assert response.json()["volume"] == 35
    assert calls == [(35, None)]

    response = client.put("/api/v1/system/volume", headers=auth_headers, json={"muted": True})
    assert response.status_code == 200
    assert calls[-1] == (None, True)


def test_system_volume_endpoint_reports_unavailable(client, auth_headers, monkeypatch) -> None:
    def unavailable() -> dict:
        raise system_service.VolumeUnavailable("No audio endpoint")

    monkeypatch.setattr(system_service, "read_volume", unavailable)
    response = client.get("/api/v1/system/volume", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is False
    assert "No audio endpoint" in body["detail"]


def test_volume_tool_clamps_up_and_down(db_session, monkeypatch) -> None:
    state = {"volume": 95, "muted": False, "supported": True}
    monkeypatch.setattr(system_service, "read_volume", lambda: dict(state))

    def fake_set(level=None, muted=None):  # noqa: ANN001
        state["volume"] = int(level if level is not None else state["volume"])
        state["muted"] = bool(muted)
        return dict(state)

    monkeypatch.setattr(system_service, "set_volume", fake_set)

    up = tools.execute_tool(
        db_session, "control_system_volume", {"action": "up", "step": 10}
    )
    assert up["ok"] is True
    assert up["data"]["volume"] == 100

    down = tools.execute_tool(
        db_session, "control_system_volume", {"action": "down", "step": 50}
    )
    assert down["data"]["volume"] == 50

    muted = tools.execute_tool(db_session, "control_system_volume", {"action": "mute"})
    assert muted["data"]["muted"] is True


# -------------------------------------------------------------- system / time


def test_system_status_and_time_endpoints(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(
        system_service,
        "system_status",
        lambda timezone: {"timezone": timezone, "clock": "12:34:56"},
    )
    status = client.get(
        "/api/v1/system/status?timezone=Asia%2FKolkata", headers=auth_headers
    )
    assert status.status_code == 200
    assert status.json() == {"timezone": "Asia/Kolkata", "clock": "12:34:56"}

    current = client.get(
        "/api/v1/system/time?timezone=Asia%2FKolkata", headers=auth_headers
    )
    assert current.status_code == 200
    body = current.json()
    assert body["timezone"] == "Asia/Kolkata"
    assert body["iso"].endswith("+05:30")


def test_hud_endpoints_require_the_api_token(client) -> None:
    for path in (
        "/api/v1/system/status",
        "/api/v1/voice/status",
        "/api/v1/spotify/status",
    ):
        assert client.get(path).status_code == 401


# -------------------------------------------------------------------- spotify


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (401, {"error": {"message": "The access token expired"}}, "auth_required"),
        (
            403,
            {"error": {"message": "Player command failed: Premium required"}},
            "premium_required",
        ),
        (
            403,
            {"error": {"message": "Insufficient client scope"}},
            "scope_required",
        ),
        (404, {"error": {"message": "Device not found"}}, "no_device"),
        (429, {"error": {"message": "Too many requests"}}, "rate_limited"),
        (503, {"error": {"message": "Service unavailable"}}, "spotify_unavailable"),
        (400, {"error": {"message": "Malformed request"}}, "spotify_error"),
    ],
)
def test_spotify_http_errors_map_to_safe_codes(status, payload, code) -> None:
    error = spotify_service._parse_error(httpx.Response(status, json=payload))
    assert error.code == code
    assert error.as_dict()["ok"] is False
    assert error.as_dict()["message"]
    if code in {"premium_required", "auth_required", "no_device", "scope_required"}:
        assert error.fallback


def test_spotify_status_and_connect_are_honest_when_unconfigured(
    client, auth_headers, monkeypatch
) -> None:
    monkeypatch.setattr(spotify_service.settings, "spotify_client_id", "")

    status = client.get("/api/v1/spotify/status", headers=auth_headers)
    assert status.status_code == 200
    body = status.json()
    assert body["configured"] is False
    assert body["connected"] is False
    assert "JARVIS_SPOTIFY_CLIENT_ID" in body["note"]
    assert body["redirect_uri"].endswith("/api/v1/spotify/callback")

    connect = client.post("/api/v1/spotify/connect", headers=auth_headers)
    assert connect.status_code == 400
    assert "not configured" in connect.json()["detail"].lower()


def test_spotify_playback_reports_not_connected_without_crashing(
    client, auth_headers
) -> None:
    response = client.get("/api/v1/spotify/playback", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["track"] is None
    assert body["error"]["code"] == "not_connected"


def test_spotify_search_without_query_is_invalid(client, auth_headers) -> None:
    response = client.post("/api/v1/spotify/search", headers=auth_headers, json={})
    assert response.status_code == 200
    assert response.json()["tracks"] == []
    assert response.json()["error"]["code"] == "invalid_request"


def test_spotify_fallback_never_claims_verified_playback(db_session, monkeypatch) -> None:
    def unavailable(*args, **kwargs):  # noqa: ANN002, ANN003
        raise spotify_service.SpotifyError(
            "Spotify playback requires a Premium account.",
            code="premium_required",
            fallback="Open the track in Spotify.",
        )

    monkeypatch.setattr(spotify_service, "play", unavailable)
    calls: list[str] = []

    def fake_desktop(action, **kwargs):  # noqa: ANN001, ANN003
        calls.append(action)
        return {"ok": True, "detail": "Opened Spotify search"}

    monkeypatch.setattr(desktop_hub, "request_sync", fake_desktop)

    result = tools.control_spotify_tool(
        db_session, tools.SpotifyControlParams(action="play", query="Calm")
    )
    assert result["ok"] is True
    assert result["data"]["verified_playback"] is False
    assert "not verified" in result["message"].lower()
    assert calls == ["spotify_search"]


def test_ambiguous_spotify_request_asks_instead_of_pressing_media_keys(
    db_session, monkeypatch
) -> None:
    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("The fallback must not run for a clarification")

    monkeypatch.setattr(desktop_hub, "request_sync", fail_if_called)

    with pytest.raises(tools.ToolError, match="What would you like me to play"):
        tools.control_spotify_tool(db_session, tools.SpotifyControlParams(action="play"))


def test_spotify_fallback_can_be_disabled(db_session, monkeypatch) -> None:
    monkeypatch.setattr(
        tools,
        "get_settings",
        lambda db: SimpleNamespace(spotify_fallback_enabled=False),
    )
    monkeypatch.setattr(
        spotify_service,
        "pause",
        lambda db: (_ for _ in ()).throw(
            spotify_service.SpotifyError("Premium required", code="premium_required")
        ),
    )

    with pytest.raises(tools.ToolError, match="Premium required"):
        tools.control_spotify_tool(db_session, tools.SpotifyControlParams(action="pause"))


# ---------------------------------------------------------------------- voice


def test_voice_status_and_catalog(client, auth_headers) -> None:
    status = client.get("/api/v1/voice/status", headers=auth_headers)
    assert status.status_code == 200
    body = status.json()
    assert set(body) >= {"stt", "tts", "wake", "audio", "privacy"}
    assert body["wake"]["frame_samples"] == 1280
    assert body["audio"] == {
        "sample_rate": 16_000,
        "channels": 1,
        "format": "pcm_s16le",
    }

    catalog = client.get("/api/v1/voice/voices", headers=auth_headers)
    assert catalog.status_code == 200
    voices = catalog.json()["voices"]
    assert len(voices) >= 5
    assert all(voice["id"] and voice["language"] for voice in voices)


def test_transcribe_rejects_non_audio_and_empty_uploads(client, auth_headers) -> None:
    wrong_type = client.post(
        "/api/v1/voice/transcribe",
        headers=auth_headers,
        files={"audio": ("notes.txt", b"not audio", "text/plain")},
    )
    assert wrong_type.status_code == 415

    empty = client.post(
        "/api/v1/voice/transcribe",
        headers=auth_headers,
        files={"audio": ("empty.webm", b"", "audio/webm")},
    )
    assert empty.status_code == 422
    assert "no audio" in empty.json()["detail"].lower()


def test_speak_validates_text_length(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/voice/speak", headers=auth_headers, json={"text": "x" * 2001}
    )
    assert response.status_code == 422


def test_transcribe_normalizes_locale_to_primary_language(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):  # noqa: ANN001, ANN003
            seen["language"] = kwargs["language"]
            info = SimpleNamespace(
                duration=0.5,
                language=kwargs["language"],
                language_probability=1.0,
            )
            return [], info

    monkeypatch.setattr(voice_service, "_get_stt_model", lambda: FakeModel())
    result = voice_service.transcribe(b"not-real-audio", language="en-GB")

    assert seen["language"] == "en"
    assert result["language"] == "en"
    assert result["empty"] is True


def test_wake_frame_contract_rejects_wrong_size() -> None:
    with pytest.raises(voice_service.VoiceError) as info:
        voice_service.feed_wake_frame(b"short")
    assert info.value.code == "invalid_frame"


# ---------------------------------------------------------------- rate limiting


def test_rate_limiter_trips_and_recovers() -> None:
    limiter = SlidingWindowLimiter()
    limiter.check("stt:test", limit=2, window_seconds=30)
    limiter.check("stt:test", limit=2, window_seconds=30)

    with pytest.raises(HTTPException) as info:
        limiter.check("stt:test", limit=2, window_seconds=30)
    assert info.value.status_code == 429
    assert "Retry-After" in info.value.headers

    limiter.clear("stt:test")
    limiter.check("stt:test", limit=2, window_seconds=30)
