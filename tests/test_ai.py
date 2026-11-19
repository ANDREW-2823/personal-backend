from __future__ import annotations

import json


def _create_task(client, auth_headers, title: str, priority: str) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "title": title,
            "priority": priority,
            "due_at": "2026-10-30T09:00:00Z",
        },
    )
    assert response.status_code == 201, response.text


def _stream_tokens(body: str) -> str:
    chunks: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        text = payload.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def test_offline_priority_intent_streams_ranked_tasks(client, auth_headers):
    _create_task(client, auth_headers, "Review metrics", "high")
    _create_task(client, auth_headers, "Ship the roadmap", "critical")
    _create_task(client, auth_headers, "Tidy the backlog", "low")

    conversation = client.post("/api/v1/ai/conversations", headers=auth_headers)
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]

    response = client.post(
        f"/api/v1/ai/conversations/{conversation_id}/stream",
        headers=auth_headers,
        json={"content": "What are my three most important unfinished tasks?"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: done" in response.text

    answer = _stream_tokens(response.text)
    assert "Ship the roadmap" in answer
    assert "Review metrics" in answer
    assert "Tidy the backlog" in answer
    # Critical priority ranks above high when the due instants match.
    assert answer.index("Ship the roadmap") < answer.index("Review metrics")
    assert answer.index("Review metrics") < answer.index("Tidy the backlog")
