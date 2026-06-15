"""CockroachDB-only tests for hermes_state.SessionDB.

The legacy SQLite/FTS5 test suite intentionally does not apply here: this fork
removes state.db/jsonl fallback and requires CockroachDB/Postgres durable state.
"""

import os
import time
import uuid

import pytest

from hermes_db.config import reset_cache
from hermes_state import (
    SessionDB,
    apply_wal_with_fallback,
    format_session_db_unavailable,
    get_last_init_error,
    is_malformed_db_error,
    repair_state_db_schema,
)

DB_URL = os.environ.get(
    "HERMES_TEST_DATABASE_URL",
    "postgresql://root@127.0.0.1:26257/defaultdb?sslmode=disable",
)
_SKIP_UNLESS_DB = os.environ.get("HERMES_TEST_DATABASE_URL") or os.environ.get("HERMES_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _SKIP_UNLESS_DB,
    reason="CockroachDB test env not configured (set HERMES_TEST_DATABASE_URL or HERMES_DATABASE_URL)",
)


@pytest.fixture(autouse=True)
def cockroach_env(monkeypatch):
    monkeypatch.setenv("HERMES_DATABASE_URL", DB_URL)
    monkeypatch.delenv("HERMES_DATABASE_BACKEND", raising=False)
    monkeypatch.setenv("HERMES_CLUSTER", f"test-{uuid.uuid4().hex}")
    monkeypatch.setenv("HERMES_PROFILE", "pytest")
    reset_cache()
    yield
    reset_cache()


@pytest.fixture()
def db():
    session_db = SessionDB()
    yield session_db
    session_db.close()


def test_sqlite_fallback_helpers_are_removed():
    with pytest.raises(RuntimeError, match="SQLite/WAL is removed"):
        apply_wal_with_fallback()
    assert is_malformed_db_error(RuntimeError("database disk image is malformed")) is False
    assert repair_state_db_schema()["repaired"] is False
    assert "CockroachDB only" in repair_state_db_schema()["error"]


def test_session_message_crud_roundtrip(db):
    sid = f"sess-{uuid.uuid4().hex}"
    assert db.create_session(sid, "cli", title="Cockroach smoke", cwd="/tmp") == sid
    assert db.ensure_session(sid, source="cli") == sid

    seq1 = db.append_message(sid, "user", "hello cockroach", token_count=2)
    seq2 = db.append_message(sid, "assistant", {"ok": True}, metadata={"k": "v"})
    assert (seq1, seq2) == (1, 2)

    messages = db.get_messages(sid)
    assert [m["seq"] for m in messages] == [1, 2]
    assert messages[0]["content"] == "hello cockroach"
    assert "ok" in messages[1]["content"]

    rich = db.list_sessions_rich(limit=10)
    row = next(s for s in rich if s["id"] == sid)
    assert row["message_count"] == 2
    assert row["title"] == "Cockroach smoke"

    db.end_session(sid, "done")
    ended = next(s for s in db.list_sessions_rich(limit=10) if s["id"] == sid)
    assert ended["end_reason"] == "done"
    assert ended["ended_at"] is not None


def test_search_messages_uses_cockroach_ilike(db):
    sid = f"sess-{uuid.uuid4().hex}"
    db.create_session(sid, "telegram", title="Searchable")
    db.append_message(sid, "user", "unique needle phrase")
    db.append_message(sid, "assistant", "another response")

    hits = db.search_messages("needle", limit=5)
    assert any(hit["session_id"] == sid and "needle" in hit["content"] for hit in hits)


def test_memory_upsert_and_search(db):
    key = db.store.upsert_memory("unit", "user", "persistent cockroach memory", key=f"mem-{uuid.uuid4().hex}")
    hits = db.store.search_memory("cockroach", namespace="unit")
    assert any(hit["key"] == key for hit in hits)


def test_cluster_node_and_task_flow(db):
    node_id = f"node-{uuid.uuid4().hex}"
    db.store.register_node(node_id, base_url="http://127.0.0.1:9000", labels={"gpu": False})
    nodes = db.store.list_nodes()
    assert any(node["node_id"] == node_id for node in nodes)

    task_id = db.store.create_task(
        prompt="do cluster work",
        created_by="pytest",
        toolsets=["terminal"],
        priority=7,
    )
    claimed = db.store.claim_task(node_id)
    assert claimed is not None
    assert str(claimed["task_id"]) == str(task_id)
    assert claimed["status"] == "running"

    db.store.finish_task(task_id, result={"ok": True})
    with db.store.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, result FROM hermes_cluster_tasks WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
    assert row["status"] == "done"
    assert row["result"]["ok"] is True


def test_init_error_is_reported(monkeypatch):
    monkeypatch.setenv("HERMES_DATABASE_URL", "postgresql://root@127.0.0.1:1/missing?sslmode=disable")
    reset_cache()
    with pytest.raises(Exception):
        SessionDB()
    assert get_last_init_error()
    assert format_session_db_unavailable().startswith("Session database not available:")
