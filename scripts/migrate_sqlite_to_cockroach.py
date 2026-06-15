"""Migrate Hermes state from SQLite to CockroachDB.

Reads from the legacy `state.db` (sessions, messages, state_meta,
compression_locks) and writes to the CockroachDB/Postgres schema declared
in ``hermes_db.schema``.

JSON-snapshot directories under ``~/.hermes/sessions/`` are loaded as well.

Required env:
    HERMES_DATABASE_URL  -- libpq DSN, key=value form

Idempotent: ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT = Path("/root/project/hermes-agent")
sys.path.insert(0, str(PROJECT))

from hermes_db.config import get_database_config  # noqa: E402
from hermes_db.pool import get_store  # noqa: E402


def _safe_json(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if not isinstance(value, str):
        return json.dumps({"raw": str(value)})
    try:
        json.loads(value)
        return value
    except Exception:
        return json.dumps({"raw": value})


def migrate_sessions(sqlite_path: Path, store) -> int:
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT * FROM sessions")
    rows = cur.fetchall()
    con.close()

    rows_to_insert = []
    now = time.time()
    for r in rows:
        d = dict(r)
        rows_to_insert.append((
            d["id"],
            d.get("source") or "cli",
            d.get("user_id"),
            d.get("model"),
            _safe_json(d.get("model_config")),
            d.get("system_prompt"),
            d.get("parent_session_id"),
            float(d.get("started_at") or now),
            None,
            None,
            json.dumps({
                "migrated_from": "sqlite.state.db",
                "ended_at": d.get("ended_at"),
                "end_reason": d.get("end_reason"),
                "message_count": d.get("message_count") or 0,
                "tool_call_count": d.get("tool_call_count") or 0,
                "input_tokens": d.get("input_tokens") or 0,
                "output_tokens": d.get("output_tokens") or 0,
                "cache_read_tokens": d.get("cache_read_tokens") or 0,
                "cache_write_tokens": d.get("cache_write_tokens") or 0,
                "reasoning_tokens": d.get("reasoning_tokens") or 0,
            }),
        ))

    if not rows_to_insert:
        return 0
    sql = (
        "INSERT INTO hermes_sessions "
        "(id, source, user_id, model, model_config, system_prompt, "
        "parent_session_id, started_at, cwd, title, metadata) "
        "VALUES (%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s::JSONB) "
        "ON CONFLICT (id) DO NOTHING"
    )
    with store.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows_to_insert)
    return len(rows_to_insert)


def migrate_messages(sqlite_path: Path, store) -> int:
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
        "tool_name, timestamp, token_count, finish_reason, reasoning, "
        "platform_message_id FROM messages ORDER BY session_id, id"
    )
    rows = cur.fetchall()
    con.close()

    if not rows:
        return 0
    cluster = store.config.cluster_name
    profile = store.config.profile
    args_list = []
    seq_by_sid: Dict[str, int] = {}
    for r in rows:
        d = dict(r)
        seq_by_sid[d["session_id"]] = seq_by_sid.get(d["session_id"], 0) + 1
        args_list.append((
            d["session_id"], cluster, profile,
            seq_by_sid[d["session_id"]],
            d["role"], d.get("content"),
            d.get("tool_call_id"),
            _safe_json(d.get("tool_calls")),
            d.get("tool_name"),
            float(d.get("timestamp") or time.time()),
            d.get("token_count"),
            d.get("finish_reason"),
            d.get("reasoning"),
            d.get("platform_message_id"),
        ))
    sql = (
        "INSERT INTO hermes_messages "
        "(session_id, cluster_name, profile, seq, role, content, "
        "tool_call_id, tool_calls, tool_name, timestamp, "
        "token_count, finish_reason, reasoning, platform_message_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (session_id, seq) DO NOTHING"
    )
    with store.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, args_list)
    return len(args_list)


def migrate_state_meta(sqlite_path: Path, store) -> int:
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        cur.execute("SELECT key, value FROM state_meta")
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()

    if not rows:
        return 0
    args = []
    for r in rows:
        d = dict(r)
        args.append((
            "state_meta", d["key"], json.dumps({"raw": d.get("value")}),
        ))
    sql = (
        "INSERT INTO hermes_kv (scope, key, value, updated_at) "
        "VALUES (%s, %s, %s::JSONB, now()) "
        "ON CONFLICT (scope, key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = now()"
    )
    with store.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, args)
    return len(args)


def migrate_json_snapshots(hermes_home: Path, store) -> int:
    snap_dir = hermes_home / "sessions"
    if not snap_dir.is_dir():
        return 0
    n = 0
    for fp in snap_dir.rglob("*.json"):
        try:
            data = json.loads(fp.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        sid = data.get("id") or data.get("session_id") or fp.stem
        source = data.get("source") or "json-snapshot"
        try:
            store.create_session(
                sid,
                source,
                user_id=data.get("user_id"),
                model=data.get("model"),
                model_config=_safe_json(data.get("model_config")),
                system_prompt=data.get("system_prompt"),
                parent_session_id=data.get("parent_session_id"),
                started_at=float(data.get("started_at") or time.time()),
                cwd=data.get("cwd"),
                title=data.get("title"),
                metadata={"migrated_from": str(fp)},
            )
            n += 1
        except Exception as e:
            print(f"  skip snapshot {fp}: {e}")
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
    )
    p.add_argument("--sqlite", default=None, help="override state.db path")
    args = p.parse_args()

    hermes_home = Path(args.hermes_home)
    sqlite_path = Path(args.sqlite) if args.sqlite else hermes_home / "state.db"
    if not sqlite_path.exists():
        print(f"no state.db at {sqlite_path} -- nothing to migrate")
        return 0

    cfg = get_database_config(force_reload=True)
    if not cfg.is_cockroach:
        print("HERMES_DATABASE_URL must point at CockroachDB/Postgres")
        return 2
    store = get_store()
    store.init_schema()

    print(f"migrating {sqlite_path} -> {cfg.backend} {cfg.url}")
    s = migrate_sessions(sqlite_path, store)
    print(f"  sessions={s}")
    m = migrate_messages(sqlite_path, store)
    print(f"  messages={m}")
    k = migrate_state_meta(sqlite_path, store)
    print(f"  state_meta={k}")
    j = migrate_json_snapshots(hermes_home, store)
    print(f"  json_snapshots={j}")
    print(f"done. sessions={s} messages={m} state_meta={k} json_snapshots={j}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
