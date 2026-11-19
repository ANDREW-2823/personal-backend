from __future__ import annotations

PHOTO = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


def test_defaults_include_appearance_and_profile(client, auth_headers):
    response = client.get("/api/v1/settings", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    assert body["appearance"]["preset"] == "crimson"
    assert body["appearance"]["glow_strength"] == 55
    assert body["appearance"]["glass"] == 72
    for surface in ("layout", "sidebar", "nav_active", "button", "glow"):
        gradient = body["appearance"][surface]
        assert set(gradient) == {"angle", "from_color", "via_color", "to_color"}
        assert gradient["from_color"].startswith("#")
        assert gradient["to_color"].startswith("#")

    assert body["profile"] == {
        "display_name": "",
        "role": "Personal workspace",
        "photo": None,
        "photo_updated_at": None,
    }


def test_appearance_and_profile_round_trip(client, auth_headers):
    current = client.get("/api/v1/settings", headers=auth_headers).json()

    current["appearance"]["preset"] = "custom"
    current["appearance"]["layout"] = {
        "angle": 220,
        "from_color": "#112233",
        "via_color": "#445566",
        "to_color": "#778899",
    }
    current["appearance"]["nav_active"] = {
        "angle": 90,
        "from_color": "#AA0000",
        "via_color": None,
        "to_color": "#0000AA",
    }
    current["appearance"]["glow_strength"] = 12
    current["appearance"]["glass"] = 88
    current["accent"] = "#22CC88"
    current["profile"] = {
        "display_name": "Ada Lovelace",
        "role": "Systems architect",
        "photo": PHOTO,
        "photo_updated_at": "2026-10-28T12:00:00+00:00",
    }

    saved = client.put("/api/v1/settings", headers=auth_headers, json=current)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["accent"] == "#22CC88"
    assert body["appearance"]["layout"]["from_color"] == "#112233"
    assert body["appearance"]["layout"]["via_color"] == "#445566"
    assert body["appearance"]["nav_active"]["via_color"] is None
    assert body["appearance"]["glow_strength"] == 12
    assert body["profile"]["display_name"] == "Ada Lovelace"
    assert body["profile"]["photo"] == PHOTO

    reloaded = client.get("/api/v1/settings", headers=auth_headers)
    assert reloaded.status_code == 200
    persisted = reloaded.json()
    assert persisted["appearance"]["preset"] == "custom"
    assert persisted["appearance"]["layout"]["angle"] == 220
    assert persisted["appearance"]["glass"] == 88
    assert persisted["profile"]["role"] == "Systems architect"
    assert persisted["profile"]["photo_updated_at"] == "2026-10-28T12:00:00+00:00"


def test_invalid_gradient_is_rejected(client, auth_headers):
    current = client.get("/api/v1/settings", headers=auth_headers).json()
    current["appearance"]["button"]["from_color"] = "crimson"

    response = client.put("/api/v1/settings", headers=auth_headers, json=current)
    assert response.status_code == 422


def test_invalid_profile_photo_is_rejected(client, auth_headers):
    current = client.get("/api/v1/settings", headers=auth_headers).json()
    current["profile"]["photo"] = "https://example.com/photo.png"

    response = client.put("/api/v1/settings", headers=auth_headers, json=current)
    assert response.status_code == 422


def test_photo_can_be_removed(client, auth_headers):
    current = client.get("/api/v1/settings", headers=auth_headers).json()
    current["profile"]["photo"] = PHOTO
    assert client.put("/api/v1/settings", headers=auth_headers, json=current).status_code == 200

    current["profile"]["photo"] = None
    current["profile"]["photo_updated_at"] = None
    saved = client.put("/api/v1/settings", headers=auth_headers, json=current)
    assert saved.status_code == 200
    assert saved.json()["profile"]["photo"] is None
