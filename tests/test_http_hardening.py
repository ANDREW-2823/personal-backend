from __future__ import annotations

from app.services import system_service


def _origin_headers(auth_headers: dict[str, str], origin: str) -> dict[str, str]:
    return {**auth_headers, "Origin": origin}


def test_cors_allows_vite_dev_origin(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/system/time",
        headers=_origin_headers(auth_headers, "http://localhost:5173"),
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_allows_packaged_renderer_null_origin(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/system/time",
        headers=_origin_headers(auth_headers, "null"),
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"


def test_cors_preflight_for_null_origin_is_answered(client) -> None:
    response = client.options(
        "/api/v1/system/time",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Jarvis-Token",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"
    assert "x-jarvis-token" in response.headers["access-control-allow-headers"].lower()


def test_cors_rejects_unlisted_origin(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/system/time",
        headers=_origin_headers(auth_headers, "https://evil.example"),
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers

    preflight = client.options(
        "/api/v1/system/time",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers


def test_unhandled_error_is_safe_and_keeps_cors_headers(
    client, auth_headers, monkeypatch
) -> None:
    def explode(timezone: str | None = None) -> dict:
        raise RuntimeError("connection string postgres://secret@internal")

    monkeypatch.setattr(system_service, "system_status", explode)
    response = client.get(
        "/api/v1/system/status",
        headers=_origin_headers(auth_headers, "http://localhost:5173"),
    )

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == (
        "JARVIS hit an unexpected internal error. "
        "The backend log contains the full traceback."
    )
    assert "secret" not in response.text
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
