from __future__ import annotations


def _create_task(client, auth_headers, **overrides):
    payload = {
        "title": "Prepare weekly review",
        "description": "Collect metrics and draft the summary.",
        "priority": "high",
        "category": "Operations",
        "due_at": "2026-10-30T09:00:00+05:30",
        "tags": ["Work", "work", "Deep"],
        "estimated_minutes": 45,
        "subtasks": [
            {"title": "Collect metrics"},
            {"title": "Draft summary", "completed": True},
        ],
    }
    payload.update(overrides)
    return client.post("/api/v1/tasks", headers=auth_headers, json=payload)


def test_task_crud_round_trip(client, auth_headers):
    created = _create_task(client, auth_headers)
    assert created.status_code == 201, created.text
    task = created.json()

    assert task["title"] == "Prepare weekly review"
    assert task["priority"] == "high"
    assert task["status"] == "todo"
    assert task["category"] == "Operations"
    assert task["estimated_minutes"] == 45
    assert task["due_at"].startswith("2026-10-30T03:30:00")
    assert task["tags"] == ["Deep", "Work"]
    assert task["completed_at"] is None
    assert [sub["title"] for sub in task["subtasks"]] == [
        "Collect metrics",
        "Draft summary",
    ]
    assert [sub["position"] for sub in task["subtasks"]] == [0, 1]
    assert task["subtasks"][1]["completed"] is True

    task_id = task["id"]

    listed = client.get("/api/v1/tasks", headers=auth_headers)
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == task_id

    fetched = client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == task_id

    patched = client.patch(
        f"/api/v1/tasks/{task_id}", headers=auth_headers, json={"status": "done"}
    )
    assert patched.status_code == 200, patched.text
    updated = patched.json()
    assert updated["status"] == "done"
    assert updated["completed_at"] is not None
    # Regression: patching an unrelated field must not clear the due date.
    assert updated["due_at"].startswith("2026-10-30T03:30:00")

    deleted = client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers).status_code == 404
    assert client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers).status_code == 404


def test_task_can_be_reopened_and_rescheduled(client, auth_headers):
    task = _create_task(client, auth_headers, due_at=None).json()
    task_id = task["id"]

    done = client.patch(f"/api/v1/tasks/{task_id}", headers=auth_headers, json={"status": "done"})
    assert done.status_code == 200
    assert done.json()["completed_at"] is not None

    reopened = client.patch(
        f"/api/v1/tasks/{task_id}", headers=auth_headers, json={"status": "todo"}
    )
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None

    rescheduled = client.post(
        f"/api/v1/tasks/{task_id}/reschedule",
        headers=auth_headers,
        json={"due_at": "2026-11-02T08:15:00Z"},
    )
    assert rescheduled.status_code == 200
    assert rescheduled.json()["due_at"].startswith("2026-11-02T08:15:00")


def test_task_filters_and_search(client, auth_headers):
    _create_task(client, auth_headers, title="Quarterly roadmap", priority="critical")
    _create_task(
        client,
        auth_headers,
        title="Book flights",
        priority="low",
        category="Personal",
        tags=["Travel"],
        due_at=None,
    )

    high = client.get("/api/v1/tasks?priority=critical", headers=auth_headers).json()
    assert high["total"] == 1
    assert high["items"][0]["title"] == "Quarterly roadmap"

    tagged = client.get("/api/v1/tasks?tag=travel", headers=auth_headers).json()
    assert tagged["total"] == 1
    assert tagged["items"][0]["title"] == "Book flights"

    searched = client.get("/api/v1/tasks?search=roadmap", headers=auth_headers).json()
    assert searched["total"] == 1
    assert searched["items"][0]["title"] == "Quarterly roadmap"

    personal = client.get("/api/v1/tasks?status=cancelled", headers=auth_headers).json()
    assert personal["total"] == 0


def test_task_validation_and_missing_relationships(client, auth_headers):
    assert _create_task(client, auth_headers, title="").status_code == 422
    assert _create_task(client, auth_headers, priority="urgent").status_code == 422

    missing_blocker = _create_task(
        client, auth_headers, blocked_by="00000000-0000-0000-0000-000000000000"
    )
    assert missing_blocker.status_code == 422
    assert "blocked_by" in missing_blocker.json()["detail"]

    first = _create_task(client, auth_headers, title="Blocking task").json()
    blocked = _create_task(client, auth_headers, title="Blocked task", blocked_by=first["id"])
    assert blocked.status_code == 201
    assert blocked.json()["blocked_by"] == first["id"]

    self_block = client.patch(
        f"/api/v1/tasks/{first['id']}",
        headers=auth_headers,
        json={"blocked_by": first["id"]},
    )
    assert self_block.status_code == 422


def test_task_meta_reflects_completed_work(client, auth_headers):
    task = _create_task(client, auth_headers, due_at=None).json()
    client.patch(f"/api/v1/tasks/{task['id']}", headers=auth_headers, json={"status": "done"})

    meta = client.get("/api/v1/tasks/meta", headers=auth_headers)
    assert meta.status_code == 200
    body = meta.json()
    assert body["stats"]["done"] == 1
    assert body["stats"]["todo"] == 0
    assert "Operations" in body["categories"]
