from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from app.ai.tools import TOOLS, execute_tool
from app.core.timeutils import as_utc
from app.models.reminder import Reminder
from sqlalchemy import select

DAY = "2026-11-10"


def _journal_payload(**overrides):
    payload = {
        "title": "Deep work",
        "mood": "focused",
        "summary": "Built the authentication module and reviewed a pull request.",
        "highlight": "Authentication module working end to end",
        "blocks": [
            {
                "id": "block-1",
                "type": "paragraph",
                "html": "<strong>Built the authentication module</strong>",
                "text": "Built the authentication module",
            },
            {
                "id": "block-2",
                "type": "checklist",
                "html": "Reviewed the pull request",
                "text": "Reviewed the pull request",
                "checked": True,
            },
        ],
        "plain_text": "Built the authentication module\nReviewed the pull request",
        "drawing": {
            "strokes": [
                {
                    "tool": "pen",
                    "color": "#1f2937",
                    "size": 3,
                    "points": [{"x": 0.1, "y": 0.2}, {"x": 0.3, "y": 0.4}],
                }
            ],
            "width": 1000,
            "height": 600,
        },
    }
    payload.update(overrides)
    return payload


def _create_task(client, auth_headers, title: str, **overrides):
    payload = {
        "title": title,
        "priority": "high",
        "category": "Work",
        "due_at": f"{DAY}T09:00:00Z",
    }
    payload.update(overrides)
    response = client.post("/api/v1/tasks", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_event(client, auth_headers, **overrides):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now + timedelta(days=5)
    payload = {
        "title": "Planning meeting",
        "description": "Weekly planning",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=1)).isoformat(),
        "location": "Studio",
        "category": "Work",
        "priority": "high",
        "color": "#4FB6C9",
        "reminder_minutes": 30,
    }
    payload.update(overrides)
    response = client.post("/api/v1/calendar/events", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _stream_tokens(body: str) -> str:
    chunks: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("text"), str):
            chunks.append(payload["text"])
    return "".join(chunks)


def test_journal_crud_drawing_and_autosave(client, auth_headers):
    created = client.put(
        f"/api/v1/calendar/journal/{DAY}",
        headers=auth_headers,
        json=_journal_payload(),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["entry_date"] == DAY
    assert body["word_count"] == 8
    assert body["drawing"]["strokes"][0]["tool"] == "pen"
    assert body["drawing"]["width"] == 1000

    reloaded = client.get(f"/api/v1/calendar/journal/{DAY}", headers=auth_headers)
    assert reloaded.status_code == 200
    assert reloaded.json()["blocks"][1]["checked"] is True

    # A second autosave edits text but leaves the drawing untouched.
    saved = client.put(
        f"/api/v1/calendar/journal/{DAY}",
        headers=auth_headers,
        json={
            "title": "Deep work — evening",
            "blocks": [
                {
                    "id": "block-1",
                    "type": "paragraph",
                    "html": "Finished authentication and tests",
                    "text": "Finished authentication and tests",
                }
            ],
            "plain_text": "Finished authentication and tests",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["drawing"]["strokes"][0]["color"] == "#1f2937"
    assert saved.json()["title"] == "Deep work — evening"

    stale = client.put(
        f"/api/v1/calendar/journal/{DAY}",
        headers=auth_headers,
        json={
            "blocks": [],
            "plain_text": "Stale overwrite",
            "expected_updated_at": "2020-01-01T00:00:00Z",
        },
    )
    assert stale.status_code == 409

    entries = client.get(
        "/api/v1/calendar/journal/search",
        headers=auth_headers,
        params={"q": "authentication"},
    )
    assert entries.status_code == 200
    assert any(item["date"] == DAY for item in entries.json())

    deleted = client.delete(f"/api/v1/calendar/journal/{DAY}", headers=auth_headers)
    assert deleted.status_code == 200
    assert client.get(f"/api/v1/calendar/journal/{DAY}", headers=auth_headers).json() is None


def test_journal_attachments_round_trip(client, auth_headers):
    upload = client.post(
        f"/api/v1/calendar/journal/{DAY}/attachments",
        headers=auth_headers,
        json={
            "filename": "note.txt",
            "mime_type": "text/plain",
            "data": "data:text/plain;base64,aGVsbG8=",
        },
    )
    assert upload.status_code == 201, upload.text
    attachment = upload.json()
    assert attachment["size_bytes"] == 5

    download = client.get(
        f"/api/v1/calendar/attachments/{attachment['id']}", headers=auth_headers
    )
    assert download.status_code == 200
    assert download.json()["data"].endswith("aGVsbG8=")

    journal = client.get(f"/api/v1/calendar/journal/{DAY}", headers=auth_headers).json()
    assert len(journal["attachments"]) == 1

    removed = client.delete(
        f"/api/v1/calendar/attachments/{attachment['id']}", headers=auth_headers
    )
    assert removed.status_code == 200


def test_event_crud_recurrence_reminder_and_series_delete(client, auth_headers, db_session):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now + timedelta(days=4)
    event = _create_event(
        client,
        auth_headers,
        start_at=start.isoformat(),
        end_at=(start + timedelta(hours=1)).isoformat(),
        recurrence="weekly",
    )
    assert event["series_id"] == event["id"]
    assert event["reminder_minutes"] == 30
    assert event["category"] == "Work"

    reminder = db_session.scalar(select(Reminder).where(Reminder.event_id == event["id"]))
    assert reminder is not None
    assert reminder.status == "scheduled"
    assert as_utc(reminder.remind_at) < start

    range_end = start + timedelta(days=40)
    listed = client.get(
        "/api/v1/calendar/events",
        headers=auth_headers,
        params={"start": start.date().isoformat(), "end": range_end.date().isoformat()},
    )
    assert listed.status_code == 200
    occurrences = listed.json()
    assert len(occurrences) >= 3
    assert {item["series_id"] for item in occurrences} == {event["id"]}

    updated = client.patch(
        f"/api/v1/calendar/events/{event['id']}",
        headers=auth_headers,
        json={"title": "Planning meeting — updated", "priority": "critical"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Planning meeting — updated"
    assert updated.json()["priority"] == "critical"

    deleted = client.delete(
        f"/api/v1/calendar/events/{event['id']}",
        headers=auth_headers,
        params={"delete_series": "true"},
    )
    assert deleted.status_code == 200
    assert "occurrences" in deleted.json()["message"]
    remaining = client.get(
        "/api/v1/calendar/events",
        headers=auth_headers,
        params={"start": start.date().isoformat(), "end": range_end.date().isoformat()},
    ).json()
    assert remaining == []


def test_calendar_tasks_and_cross_mode_consistency(client, auth_headers):
    task = _create_task(client, auth_headers, "Ship calendar backend")
    client.patch(
        f"/api/v1/tasks/{task['id']}",
        headers=auth_headers,
        json={"status": "in_progress", "color": "#B07CC6"},
    )

    bundle = client.get(f"/api/v1/calendar/day/{DAY}", headers=auth_headers)
    assert bundle.status_code == 200, bundle.text
    body = bundle.json()
    assert any(item["id"] == task["id"] for item in body["tasks"])
    assert body["tasks"][0]["color"] == "#B07CC6"
    assert "Work" in [category["name"] for category in body["categories"]]

    tasks = client.get(
        "/api/v1/calendar/tasks",
        headers=auth_headers,
        params={"start": DAY, "end": DAY, "status": "in_progress"},
    )
    assert tasks.status_code == 200
    assert [item["id"] for item in tasks.json()] == [task["id"]]

    pending = client.get("/api/v1/calendar/pending-tasks", headers=auth_headers)
    assert pending.status_code == 200
    assert any(item["id"] == task["id"] for item in pending.json())


def test_daily_weekly_and_monthly_summaries_keep_categories_distinct(
    client, auth_headers
):
    today = date.today()
    today_iso = today.isoformat()
    journal = client.put(
        f"/api/v1/calendar/journal/{today_iso}",
        headers=auth_headers,
        json=_journal_payload(
            title="Today",
            summary="Worked on the calendar",
            plain_text="Completed the calendar backend\nWrote summary tests",
            highlight="Calendar backend done",
            blocks=[
                {
                    "id": "today-1",
                    "type": "paragraph",
                    "html": "Completed the calendar backend",
                    "text": "Completed the calendar backend",
                }
            ],
        ),
    )
    assert journal.status_code == 200, journal.text

    completed_task = _create_task(
        client,
        auth_headers,
        "Finish calendar backend",
        due_at=f"{today_iso}T09:00:00Z",
        status="done",
    )
    pending_task = _create_task(
        client,
        auth_headers,
        "Write deployment notes",
        due_at=f"{today_iso}T15:00:00Z",
        status="todo",
    )
    event = _create_event(
        client,
        auth_headers,
        title="Calendar review",
        start_at=f"{today_iso}T12:00:00Z",
        end_at=f"{today_iso}T13:00:00Z",
        reminder_minutes=None,
    )

    summary = client.get(
        "/api/v1/calendar/summary/day", headers=auth_headers, params={"day": today_iso}
    )
    assert summary.status_code == 200, summary.text
    data = summary.json()
    assert data["journal_recorded"] is True
    assert any(item["id"] == completed_task["id"] for item in data["tasks_completed"])
    assert any(item["id"] == pending_task["id"] for item in data["tasks_pending"])
    assert any(item["id"] == event["id"] for item in data["events_scheduled"])
    assert all(item["id"] != pending_task["id"] for item in data["tasks_completed"])

    week = client.get(
        "/api/v1/calendar/summary/week",
        headers=auth_headers,
        params={"start": (today - timedelta(days=6)).isoformat(), "end": today_iso},
    )
    assert week.status_code == 200, week.text
    week_data = week.json()
    assert week_data["totals"]["tasks_completed"] >= 1
    assert week_data["recorded_journal_days"] >= 1
    assert any(item["id"] == completed_task["id"] for item in week_data["tasks_completed"])

    month = client.get(
        "/api/v1/calendar/summary/month",
        headers=auth_headers,
        params={"month": today.month, "year": today.year},
    )
    assert month.status_code == 200, month.text
    assert any(item["id"] == completed_task["id"] for item in month.json()["tasks_completed"])

    productivity = client.get(
        "/api/v1/calendar/summary/productivity",
        headers=auth_headers,
        params={"start": (today - timedelta(days=29)).isoformat(), "end": today_iso},
    )
    assert productivity.status_code == 200
    assert productivity.json()["totals"]["completion_rate"] > 0


def test_search_across_journal_events_and_tasks(client, auth_headers):
    client.put(
        f"/api/v1/calendar/journal/{DAY}",
        headers=auth_headers,
        json=_journal_payload(),
    )
    _create_task(client, auth_headers, "Authentication module review")
    _create_event(client, auth_headers, title="Authentication rollout")

    journal = client.get(
        "/api/v1/calendar/journal/search",
        headers=auth_headers,
        params={"q": "authentication", "start": DAY, "end": DAY},
    )
    assert journal.status_code == 200
    assert len(journal.json()) == 1

    combined = client.get(
        "/api/v1/calendar/search", headers=auth_headers, params={"q": "authentication"}
    )
    assert combined.status_code == 200
    body = combined.json()
    assert len(body["entries"]) == 1
    assert any(item["title"] == "Authentication module review" for item in body["tasks"])
    assert any(item["title"] == "Authentication rollout" for item in body["events"])


def test_ai_calendar_tools_and_offline_chat_use_real_records(client, auth_headers, db_session):
    required = {
        "get_daily_summary",
        "get_weekly_summary",
        "get_monthly_summary",
        "get_journal_entry",
        "search_journal_entries",
        "get_events",
        "get_tasks",
        "get_pending_tasks",
        "get_completed_tasks",
        "get_upcoming_events",
        "get_productivity_summary",
    }
    assert required <= set(TOOLS)

    today = date.today()
    client.put(
        f"/api/v1/calendar/journal/{today.isoformat()}",
        headers=auth_headers,
        json=_journal_payload(
            plain_text="Studied PostgreSQL indexing\nAttended the architecture meeting"
        ),
    )
    pending = _create_task(
        client,
        auth_headers,
        "Finish the AI calendar tools",
        due_at=f"{today.isoformat()}T18:00:00Z",
        status="todo",
    )
    event = _create_event(
        client,
        auth_headers,
        title="Architecture meeting",
        start_at=f"{today.isoformat()}T12:00:00Z",
        end_at=f"{today.isoformat()}T13:00:00Z",
        reminder_minutes=None,
    )

    outcome = execute_tool(
        db_session, "get_daily_summary", {"date": today.isoformat()}
    )
    assert outcome["ok"] is True
    assert "PostgreSQL indexing" in outcome["message"]
    assert "Scheduled events" in outcome["message"]
    assert "Tasks still pending" in outcome["message"]
    assert outcome["data"]["journal_recorded"] is True

    pending_outcome = execute_tool(
        db_session, "get_pending_tasks", {"limit": 10}
    )
    assert any(
        item["id"] == pending["id"] for item in pending_outcome["data"]
    )

    conversation = client.post("/api/v1/ai/conversations", headers=auth_headers)
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    response = client.post(
        f"/api/v1/ai/conversations/{conversation_id}/stream",
        headers=auth_headers,
        json={"content": "What happened today?"},
    )
    assert response.status_code == 200, response.text
    answer = _stream_tokens(response.text)
    assert "PostgreSQL indexing" in answer
    assert "Finish the AI calendar tools" in answer
    assert event["title"] in answer
    assert "not proof they happened" in answer

    search = client.post(
        f"/api/v1/ai/conversations/{conversation_id}/stream",
        headers=auth_headers,
        json={"content": "When did I last work on PostgreSQL indexing?"},
    )
    assert search.status_code == 200
    assert today.strftime("%d %b %Y") in _stream_tokens(search.text)


def test_event_reminder_and_timezone_boundaries(client, auth_headers):
    current = client.get("/api/v1/settings", headers=auth_headers).json()
    current["timezone"] = "Asia/Kolkata"
    saved = client.put("/api/v1/settings", headers=auth_headers, json=current)
    assert saved.status_code == 200, saved.text

    event = client.post(
        "/api/v1/calendar/events",
        headers=auth_headers,
        json={
            "title": "Kolkata morning",
            "start_at": f"{DAY}T09:00:00",
            "end_at": f"{DAY}T10:00:00",
        },
    )
    assert event.status_code == 201, event.text
    assert event.json()["start_at"].startswith("2026-11-10T03:30:00")

    task = client.post(
        "/api/v1/tasks",
        headers=auth_headers,
        json={
            "title": "Late local task",
            "due_at": f"{DAY}T23:30:00",
            "priority": "medium",
        },
    )
    assert task.status_code == 201, task.text
    assert task.json()["due_at"].startswith("2026-11-10T18:00:00")

    bundle = client.get(f"/api/v1/calendar/day/{DAY}", headers=auth_headers)
    assert bundle.status_code == 200
    body = bundle.json()
    assert any(item["id"] == event.json()["id"] for item in body["events"])
    assert any(item["id"] == task.json()["id"] for item in body["tasks"])

    # The same UTC instant lands on the correct local day, and the neighbouring
    # local day must not contain these records.
    previous = client.get("/api/v1/calendar/day/2026-11-09", headers=auth_headers).json()
    assert all(item["id"] != event.json()["id"] for item in previous["events"])
    assert all(item["id"] != task.json()["id"] for item in previous["tasks"])
