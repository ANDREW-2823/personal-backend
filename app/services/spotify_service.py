"""Spotify integration.

The backend owns the OAuth exchange and encrypts both tokens at rest. The
renderer never receives credentials. Playback commands use the official Web API
and are verified against the player state where Spotify reports it; nothing is
described as playing unless the API confirms it.

Spotify has hard platform constraints that cannot be coded around:

* the Web Playback API requires a Spotify Premium account;
* a device (desktop app, phone, web player, or the Web Playback SDK) must be
  active/available;
* OAuth requires an application in the Spotify developer dashboard whose
  redirect URI exactly matches ``settings.spotify_redirect_uri``.

When any of these block an action, the service raises :class:`SpotifyError`
with a machine-readable code and a fallback hint. Callers must report the
limitation honestly instead of pretending playback succeeded.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
import urllib.parse
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import SecretError, decrypt_text, encrypt_text
from app.core.timeutils import as_utc, utcnow
from app.models.spotify import SpotifyOAuthState, SpotifyToken

logger = logging.getLogger("jarvis.spotify")

API_BASE = "https://api.spotify.com/v1"
ACCOUNT_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
HTTP_TIMEOUT = 12.0
PLAYER_POLL_ATTEMPTS = 4
PLAYER_POLL_DELAY = 0.35

_refresh_lock = threading.Lock()


class SpotifyError(RuntimeError):
    """A safe, user-facing Spotify failure with a stable code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "spotify_error",
        status: int | None = None,
        fallback: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.fallback = fallback

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": str(self),
            "fallback": self.fallback,
        }


# ------------------------------------------------------------------ transport


def _client() -> httpx.Client:
    """Separate factory so tests can patch the transport boundary."""
    return httpx.Client(timeout=HTTP_TIMEOUT)


def _authorization_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _parse_error(response: httpx.Response) -> SpotifyError:
    status = response.status_code
    message = "Spotify request failed."
    reason = ""
    try:
        body = response.json()
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or message)
            reason = str(error.get("reason") or "")
        elif isinstance(error, str):
            message = error
        if body.get("error_description"):
            message = str(body["error_description"])
    except ValueError:
        message = (response.text or message)[:300]

    detail = f"{message}{f' ({reason})' if reason else ''}".strip()
    if status == 401:
        return SpotifyError(
            "Spotify authorization expired. Reconnect Spotify in Settings.",
            code="auth_required",
            status=status,
            fallback="Reconnect Spotify in Settings → Integrations.",
        )
    if status == 403:
        lowered = f"{message} {reason}".lower()
        if "premium" in lowered:
            return SpotifyError(
                "Spotify playback requires a Premium account.",
                code="premium_required",
                status=status,
                fallback="Open the track in Spotify and press play manually.",
            )
        if "scope" in lowered:
            return SpotifyError(
                "Spotify denied this action because the account lacks the required scope.",
                code="scope_required",
                status=status,
                fallback="Disconnect and reconnect Spotify to grant the full scope set.",
            )
        return SpotifyError(
            f"Spotify refused the request: {detail}",
            code="forbidden",
            status=status,
            fallback="Use the Spotify desktop app directly.",
        )
    if status == 404:
        return SpotifyError(
            "No active Spotify device was found.",
            code="no_device",
            status=status,
            fallback="Open Spotify on a device, then ask again.",
        )
    if status == 429:
        return SpotifyError(
            "Spotify is rate limiting requests. Try again shortly.",
            code="rate_limited",
            status=status,
        )
    if status >= 500:
        return SpotifyError(
            "Spotify is temporarily unavailable.",
            code="spotify_unavailable",
            status=status,
        )
    return SpotifyError(
        f"Spotify error: {detail}",
        code="spotify_error",
        status=status,
    )


def _api(
    db: Session,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    retry: bool = True,
) -> dict[str, Any]:
    token = _valid_token(db)
    url = path if path.startswith("https://") else f"{API_BASE}{path}"
    with _client() as client:
        response = client.request(
            method,
            url,
            params=params,
            json=json,
            headers=_authorization_headers(token),
        )
    if response.status_code == 401 and retry:
        _refresh_token(db)
        return _api(db, method, path, params=params, json=json, retry=False)
    if response.status_code >= 400:
        raise _parse_error(response)
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {"items": payload}


# ------------------------------------------------------------------- accounts


def is_configured() -> bool:
    return bool(settings.spotify_client_id.strip())


def _token_row(db: Session) -> SpotifyToken | None:
    return db.get(SpotifyToken, "default")


def _decrypt_token(row: SpotifyToken) -> tuple[str, str]:
    try:
        access = decrypt_text(row.access_token)
        refresh = decrypt_text(row.refresh_token)
    except SecretError as exc:
        raise SpotifyError(
            "Stored Spotify credentials cannot be decrypted. Reconnect Spotify.",
            code="auth_required",
            fallback="Disconnect and reconnect Spotify in Settings.",
        ) from exc
    return access, refresh


def _valid_token(db: Session) -> str:
    row = _token_row(db)
    if row is None:
        raise SpotifyError(
            "Spotify is not connected.",
            code="not_connected",
            fallback="Connect Spotify in Settings → Integrations.",
        )
    expires = as_utc(row.expires_at)
    if expires is not None and expires <= utcnow() + timedelta(seconds=30):
        _refresh_token(db, row=row)
        row = _token_row(db)
        assert row is not None
    access, _ = _decrypt_token(row)
    return access


def _save_token(
    db: Session,
    payload: dict[str, Any],
    *,
    existing: SpotifyToken | None = None,
) -> SpotifyToken:
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access:
        raise SpotifyError("Spotify did not return an access token.", code="oauth_failed")
    row = existing or _token_row(db)
    if row is None:
        if not refresh:
            raise SpotifyError("Spotify did not return a refresh token.", code="oauth_failed")
        row = SpotifyToken(
            id="default",
            access_token=encrypt_text(access),
            refresh_token=encrypt_text(refresh),
            expires_at=utcnow(),
        )
        db.add(row)
    else:
        row.access_token = encrypt_text(access)
        if refresh:
            row.refresh_token = encrypt_text(refresh)
        row.updated_at = utcnow()
    row.token_type = str(payload.get("token_type") or "Bearer")
    if payload.get("scope"):
        row.scope = str(payload["scope"])
    expires_in = int(payload.get("expires_in") or 3600)
    row.expires_at = utcnow() + timedelta(seconds=max(60, expires_in - 30))
    db.flush()
    return row


def _refresh_token(db: Session, *, row: SpotifyToken | None = None) -> None:
    row = row or _token_row(db)
    if row is None:
        raise SpotifyError(
            "Spotify is not connected.",
            code="not_connected",
            fallback="Connect Spotify in Settings → Integrations.",
        )
    with _refresh_lock:
        db.refresh(row)
        expires = as_utc(row.expires_at)
        if expires is not None and expires > utcnow() + timedelta(seconds=30):
            return
        _, refresh = _decrypt_token(row)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": settings.spotify_client_id,
        }
        if settings.spotify_client_secret:
            data["client_secret"] = settings.spotify_client_secret
        try:
            with _client() as client:
                response = client.post(ACCOUNT_URL, data=data)
        except httpx.HTTPError as exc:
            raise SpotifyError(
                "Could not reach Spotify to refresh the session.",
                code="network",
                fallback="Check your internet connection and retry.",
            ) from exc
        if response.status_code >= 400:
            error = _parse_error(response)
            if error.code in {"auth_required", "forbidden"}:
                error = SpotifyError(
                    "Spotify authorization is no longer valid. Reconnect Spotify.",
                    code="auth_required",
                    fallback="Disconnect and reconnect Spotify in Settings.",
                )
            raise error
        try:
            payload = response.json()
        except ValueError as exc:
            raise SpotifyError(
                "Spotify returned an invalid token response.", code="oauth_failed"
            ) from exc
        _save_token(db, payload, existing=row)
        db.commit()


def authorization_url(db: Session) -> dict[str, Any]:
    if not is_configured():
        raise SpotifyError(
            "Spotify is not configured on the backend.",
            code="not_configured",
            fallback=(
                "Add JARVIS_SPOTIFY_CLIENT_ID to backend/.env and register "
                f"{settings.spotify_redirect_uri} in the Spotify developer dashboard."
            ),
        )
    now = utcnow()
    db.execute(delete(SpotifyOAuthState).where(SpotifyOAuthState.expires_at < now))
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    state = secrets.token_urlsafe(32)
    db.add(
        SpotifyOAuthState(
            state=state,
            code_verifier=verifier,
            expires_at=now + timedelta(minutes=10),
        )
    )
    db.commit()
    query = urllib.parse.urlencode(
        {
            "client_id": settings.spotify_client_id,
            "response_type": "code",
            "redirect_uri": settings.spotify_redirect_uri,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": " ".join(settings.spotify_scope_list),
            "state": state,
            "show_dialog": "true",
        }
    )
    return {
        "url": f"{AUTHORIZE_URL}?{query}",
        "redirect_uri": settings.spotify_redirect_uri,
        "expires_in": 600,
    }


def exchange_code(db: Session, *, code: str, state: str) -> dict[str, Any]:
    record = db.get(SpotifyOAuthState, state)
    if record is None:
        raise SpotifyError(
            "That Spotify sign-in link is invalid or was already used.",
            code="invalid_state",
            fallback="Start the connection again from Settings.",
        )
    expires = as_utc(record.expires_at)
    verifier = record.code_verifier
    # Single use: remove the state before talking to Spotify.
    db.delete(record)
    db.commit()
    if expires is None or expires < utcnow():
        raise SpotifyError(
            "That Spotify sign-in link expired. Start again.",
            code="invalid_state",
            fallback="Start the connection again from Settings.",
        )
    if not code:
        raise SpotifyError("Spotify did not return an authorization code.", code="oauth_failed")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.spotify_redirect_uri,
        "client_id": settings.spotify_client_id,
        "code_verifier": verifier,
    }
    if settings.spotify_client_secret:
        data["client_secret"] = settings.spotify_client_secret
    try:
        with _client() as client:
            response = client.post(ACCOUNT_URL, data=data)
    except httpx.HTTPError as exc:
        raise SpotifyError(
            "Could not reach Spotify to finish connecting.",
            code="network",
            fallback="Check your internet connection and retry.",
        ) from exc
    if response.status_code >= 400:
        raise _parse_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpotifyError(
            "Spotify returned an invalid token response.", code="oauth_failed"
        ) from exc

    row = _save_token(db, payload)
    profile: dict[str, Any] = {}
    try:
        with _client() as client:
            me = client.get(
                f"{API_BASE}/me",
                headers=_authorization_headers(str(payload["access_token"])),
            )
        if me.status_code < 400:
            profile = me.json()
    except (httpx.HTTPError, ValueError):
        profile = {}
    row.display_name = str(profile.get("display_name") or "")[:120] or None
    row.account_id = str(profile.get("id") or "")[:120] or None
    row.product = str(profile.get("product") or "")[:40] or None
    db.commit()
    return account_status(db)


def disconnect(db: Session) -> None:
    row = _token_row(db)
    if row is not None:
        db.delete(row)
    db.execute(delete(SpotifyOAuthState))
    db.commit()


def account_status(db: Session) -> dict[str, Any]:
    row = _token_row(db)
    configured = is_configured()
    base: dict[str, Any] = {
        "configured": configured,
        "connected": row is not None,
        "client_id_set": configured,
        "redirect_uri": settings.spotify_redirect_uri,
        "display_name": None,
        "account_id": None,
        "product": None,
        "premium": None,
        "scopes": [],
        "expires_at": None,
        "note": None,
    }
    if not configured:
        base["note"] = (
            "Spotify integration is not configured. Add JARVIS_SPOTIFY_CLIENT_ID to backend/.env "
            f"and register {settings.spotify_redirect_uri} in the Spotify dashboard."
        )
        return base
    if row is None:
        base["note"] = "Spotify is configured but not connected."
        return base
    product = (row.product or "").lower()
    base.update(
        {
            "display_name": row.display_name,
            "account_id": row.account_id,
            "product": row.product,
            "premium": product == "premium" if product else None,
            "scopes": [scope for scope in (row.scope or "").split() if scope],
            "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else None,
        }
    )
    if product and product != "premium":
        base["note"] = (
            "Connected. Spotify playback control requires a Premium account; "
            "search and open-in-Spotify fallbacks remain available."
        )
    return base


# ------------------------------------------------------------------- playback


def _normalize_track(item: dict[str, Any]) -> dict[str, Any]:
    album = item.get("album") or {}
    images = album.get("images") or []
    image = images[0].get("url") if images else None
    artists = [str(artist.get("name") or "") for artist in item.get("artists") or []]
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "uri": item.get("uri"),
        "url": (item.get("external_urls") or {}).get("spotify"),
        "duration_ms": item.get("duration_ms"),
        "artists": [name for name in artists if name],
        "artist": ", ".join(name for name in artists if name),
        "album": album.get("name"),
        "image": image,
        "explicit": bool(item.get("explicit")),
    }


def _normalize_device(device: dict[str, Any] | None) -> dict[str, Any] | None:
    if not device:
        return None
    return {
        "id": device.get("id"),
        "name": device.get("name"),
        "type": device.get("type"),
        "is_active": bool(device.get("is_active")),
        "volume_percent": device.get("volume_percent"),
        "supports_volume": bool(device.get("supports_volume")),
    }


def _normalize_playback(payload: dict[str, Any]) -> dict[str, Any]:
    item = payload.get("item") or {}
    track = _normalize_track(item) if item else None
    return {
        "active": bool(payload.get("device")) and track is not None,
        "is_playing": bool(payload.get("is_playing")),
        "progress_ms": payload.get("progress_ms"),
        "shuffle": payload.get("shuffle_state"),
        "repeat": payload.get("repeat_state"),
        "device": _normalize_device(payload.get("device")),
        "track": track,
        "fetched_at": utcnow().isoformat(),
    }


def playback(db: Session) -> dict[str, Any]:
    payload = _api(db, "GET", "/me/player")
    if not payload:
        return {
            "active": False,
            "is_playing": False,
            "progress_ms": None,
            "shuffle": None,
            "repeat": None,
            "device": None,
            "track": None,
            "fetched_at": utcnow().isoformat(),
        }
    return _normalize_playback(payload)


def devices(db: Session) -> list[dict[str, Any]]:
    payload = _api(db, "GET", "/me/player/devices")
    return [
        device
        for device in (_normalize_device(item) for item in payload.get("devices") or [])
        if device is not None
    ]


def _require_premium(db: Session) -> None:
    row = _token_row(db)
    if row is None:
        _valid_token(db)
        row = _token_row(db)
    product = (row.product or "").lower() if row else ""
    if product and product != "premium":
        raise SpotifyError(
            "Spotify playback control requires a Premium account.",
            code="premium_required",
            fallback=(
                "Search and open the track in Spotify instead; "
                "playback controls are read-only on Free."
            ),
        )


def _choose_device(db: Session, device_id: str | None) -> str | None:
    available = devices(db)
    if device_id:
        if not any(device.get("id") == device_id for device in available):
            raise SpotifyError(
                "The requested Spotify device is no longer available.",
                code="no_device",
                fallback="Open Spotify and refresh the device list.",
            )
        return device_id
    active = next((device for device in available if device.get("is_active")), None)
    if active and active.get("id"):
        return str(active["id"])
    if available and available[0].get("id"):
        target = str(available[0]["id"])
        _api(db, "PUT", "/me/player", json={"device_ids": [target], "play": False})
        time.sleep(0.25)
        return target
    raise SpotifyError(
        "No Spotify device is available.",
        code="no_device",
        fallback="Open the Spotify app or web player, then ask again.",
    )


def _verify(
    db: Session,
    *,
    expect_playing: bool | None,
    track_uri: str | None = None,
) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for _attempt in range(PLAYER_POLL_ATTEMPTS):
        try:
            last = playback(db)
        except SpotifyError:
            break
        if expect_playing is not None and last.get("is_playing") != expect_playing:
            time.sleep(PLAYER_POLL_DELAY)
            continue
        if track_uri and (last.get("track") or {}).get("uri") != track_uri:
            time.sleep(PLAYER_POLL_DELAY)
            continue
        return {"verified": True, "playback": last}
    return {"verified": False, "playback": last}


def _command_message(action: str, verification: dict[str, Any]) -> str:
    state = verification.get("playback") or {}
    track = state.get("track") or {}
    title = track.get("name")
    artist = track.get("artist")
    if verification.get("verified"):
        if action == "play" and title:
            suffix = f" by {artist}" if artist else ""
            return f"Spotify is playing “{title}”{suffix}."
        if action == "pause":
            return "Spotify playback is paused."
        if action == "resume":
            return "Spotify playback resumed."
        if action == "volume" and state.get("device"):
            return f"Spotify volume is {state['device'].get('volume_percent')}%."
        return "Spotify confirmed the command."
    return (
        "Spotify accepted the command, but the player state could not be verified yet. "
        "Check the Spotify window to confirm."
    )


def play(
    db: Session,
    *,
    query: str | None = None,
    uri: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    _require_premium(db)
    selected_uri = uri
    track: dict[str, Any] | None = None
    if query and not selected_uri:
        result = search(db, query=query, limit=5)
        if not result["tracks"]:
            raise SpotifyError(
                f"No Spotify track matched “{query}”.",
                code="not_found",
                fallback="Try a different search phrase or open Spotify directly.",
            )
        track = result["tracks"][0]
        selected_uri = str(track.get("uri") or "")
    if not selected_uri:
        raise SpotifyError("A track name or Spotify URI is required.", code="invalid_request")
    target = _choose_device(db, device_id)
    params = {"device_id": target} if target else None
    _api(db, "PUT", "/me/player/play", params=params, json={"uris": [selected_uri]})
    verification = _verify(db, expect_playing=True, track_uri=selected_uri)
    return {
        "ok": True,
        "action": "play",
        "verified": verification["verified"],
        "message": _command_message("play", verification),
        "track": track or (verification.get("playback") or {}).get("track"),
        "playback": verification.get("playback"),
        "fallback": (
            None
            if verification["verified"]
            else "Check the Spotify window if audio did not start."
        ),
    }


def pause(db: Session) -> dict[str, Any]:
    _require_premium(db)
    _choose_device(db, None)
    _api(db, "PUT", "/me/player/pause")
    verification = _verify(db, expect_playing=False)
    return {
        "ok": True,
        "action": "pause",
        "verified": verification["verified"],
        "message": _command_message("pause", verification),
        "playback": verification.get("playback"),
    }


def resume(db: Session) -> dict[str, Any]:
    _require_premium(db)
    _choose_device(db, None)
    _api(db, "PUT", "/me/player/play")
    verification = _verify(db, expect_playing=True)
    return {
        "ok": True,
        "action": "resume",
        "verified": verification["verified"],
        "message": _command_message("resume", verification),
        "playback": verification.get("playback"),
    }


def skip(db: Session, *, direction: str) -> dict[str, Any]:
    _require_premium(db)
    _choose_device(db, None)
    _api(db, "POST", "/me/player/next" if direction == "next" else "/me/player/previous")
    state = playback(db)
    track = (state.get("track") or {}).get("name")
    return {
        "ok": True,
        "action": direction,
        "verified": bool(state.get("track")),
        "message": (
            f"Skipped to “{track}”."
            if track
            else f"Sent Spotify {direction} and the player responded."
        ),
        "playback": state,
    }


def set_volume(db: Session, percent: int) -> dict[str, Any]:
    if not 0 <= int(percent) <= 100:
        raise SpotifyError("Spotify volume must be between 0 and 100.", code="invalid_request")
    _require_premium(db)
    _choose_device(db, None)
    _api(db, "PUT", "/me/player/volume", params={"volume_percent": int(percent)})
    time.sleep(0.2)
    state = playback(db)
    actual = (state.get("device") or {}).get("volume_percent")
    return {
        "ok": True,
        "action": "volume",
        "verified": actual == int(percent),
        "message": (
            f"Spotify volume set to {actual}%."
            if actual is not None
            else "Spotify accepted the volume change; the player did not report the new value."
        ),
        "playback": state,
    }


def search(db: Session, *, query: str, limit: int = 8) -> dict[str, Any]:
    clean = query.strip()
    if not clean:
        raise SpotifyError("A search phrase is required.", code="invalid_request")
    payload = _api(
        db,
        "GET",
        "/search",
        params={"q": clean[:200], "type": "track", "limit": max(1, min(20, limit))},
    )
    items = ((payload.get("tracks") or {}).get("items")) or []
    tracks = [_normalize_track(item) for item in items if item]
    return {
        "ok": True,
        "query": clean,
        "tracks": tracks,
        "message": f"Found {len(tracks)} Spotify track(s) for “{clean}”.",
    }
