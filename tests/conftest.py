"""Shared pytest configuration.

The production engine is PostgreSQL-only (pool sizing, JSONB). Tests therefore
override the FastAPI database dependency with an in-memory SQLite database that
lives for the duration of the test module.
"""

from __future__ import annotations

import os

# These must be set before anything imports app.core.config, because the
# Settings object is constructed at import time.
TEST_TOKEN = "jarvis-test-token"
os.environ["JARVIS_API_TOKEN"] = TEST_TOKEN
os.environ["JARVIS_SCHEDULER_ENABLED"] = "false"
os.environ["JARVIS_DEFAULT_TIMEZONE"] = "UTC"
os.environ["JARVIS_ENVIRONMENT"] = "test"

import app.models  # noqa: F401,E402  (registers every table on Base.metadata)
import pytest  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)


@event.listens_for(test_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_database():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Jarvis-Token": TEST_TOKEN}


@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
