from __future__ import annotations


def test_missing_token_is_rejected(client):
    response = client.get("/api/v1/settings")
    assert response.status_code == 401
    assert "token" in response.json()["detail"].lower()


def test_wrong_token_is_rejected(client):
    response = client.get("/api/v1/settings", headers={"X-Jarvis-Token": "not-the-real-token"})
    assert response.status_code == 401


def test_valid_token_reaches_the_api(client, auth_headers):
    response = client.get("/api/v1/settings", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["assistant_name"] == "JARVIS"


def test_tasks_are_protected_too(client):
    response = client.get("/api/v1/tasks")
    assert response.status_code == 401
