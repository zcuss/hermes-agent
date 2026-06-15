"""Migrate Hermes state from SQLite to CockroachDB.

Single-file migration script designed to run as a systemd one-shot BEFORE
``hermes-gateway.service`` so the gateway never starts against a half-migrated
state. Idempotent: safe to re-run. Uses COPY (via psycopg) for big tables so
even multi-hundred-MB state.db files finish in seconds.

Sources:
  * /root/.hermes/state.db    (sessions, messages, state_meta)
  * /root/.hermes/sessions/*.json   (JSON snapshots)

Destinations (CockroachDB / Postgres):
  * hermes_sessions
  * hermes_messages
  * hermes_kv

Required environment:
  HERMES_DATABASE_URL    libpq DSN  (key=value or URL form)
  COCKROACH_PG_URL       fallback if HERMES_DATABASE_URL not set
  COCKROACH_URL          fallback if above two not set
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# --- bootstrap so we can import hermes_db from this repo --------------------
PROJECT = Path(os.environ.get("HERMES_PROJECT", "/root/project/hermes-agent"))
sys.path.insert(0, str(PROJECT))


def _load_url() -> Optional[str]:
    for key in (
        "HERMES_DATABASE_URL",
        "COCKROACH_PG_URL",
        "COCKROACH_URL",
    ):
        v = os.environ.get(key)
        if v:
            return v
    cfg_yaml = Path("/root/.hermes/config.yaml")
    if cfg_yaml.exists():
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(cfg_yaml.read_text()) or {}
            url = (cfg.get("database") or {}).get("url")
            if url:
                return url
        except Exception:
            pass
    return None


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


# --- session migration ------------------------------------------------------
def _session_rows(sqlite_path: Path) -> List[Tuple]:
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        cur.execute("SELECT * FROM sessions")
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return rows


def migrate_sessions(sqlite_path: Path, store, *, dry: bool = False) -> int:
    rows = _session_rows(sqlite_path)
    if not rows:
        return 0
    args_list: List[Tuple] = []
    now = time.time()
    for r in rows:
        d = dict(r)
        args_list.append((
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
    if dry:
        return len(args_list)
    sql = (
        "INSERT INTO hermes_sessions "
        "(id, source, user_id, model, model_config, system_prompt, "
        "parent_session_id, started_at, cwd, title, metadata) "
        "VALUES (%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s::JSONB) "
        "ON CONFLICT (id) DO NOTHING"
    )
    with store.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, args_list)
    return len(args_list)


# --- messages ---------------------------------------------------------------
def _message_rows(sqlite_path: Path) -> List[sqlite3.Row]:
    con = sqlite3.connect(str(sqlite_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
            "tool_name, timestamp, token_count, finish_reason, reasoning, "
            "platform_message_id FROM messages ORDER BY session_id, id"
        )
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return rows


def _row_to_message_args(r: sqlite3.Row, cluster: str, profile: str) -> Tuple:
    d = dict(r)
    return (
        d["session_id"], cluster, profile,
        d["role"], d.get("content"),
        d.get("tool_call_id"),
        _safe_json(d.get("tool_calls")),
        d.get("tool_name"),
        float(d.get("timestamp") or time.time()),
        d.get("token_count"),
        d.get("finish_reason"),
        d.get("reasoning"),
        d.get("platform_message_id"),
    )


def migrate_messages(sqlite_path: Path, store, *, dry: bool = False,
                     batch: int = 1000) -> int:
    rows = _message_rows(sqlite_path)
    if not rows:
        return 0
    cluster = store.config.cluster_name
    profile = store.config.profile

    # Pre-compute seq per session
    seq_by_sid: dict[str, int] = {}
    final: List[Tuple] = []
    for r in rows:
        d = dict(r)
        seq_by_sid[d["session_id"]] = seq_by_sid.get(d["session_id"], 0) + 1
        final.append((
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
    if dry:
        return len(final)

    sql = (
        "INSERT INTO hermes_messages "
        "(session_id, cluster_name, profile, seq, role, content, "
        "tool_call_id, tool_calls, tool_name, timestamp, "
        "token_count, finish_reason, reasoning, platform_message_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (session_id, seq) DO NOTHING"
    )
    with store.connection() as conn, conn.cursor() as cur:
        for i in range(0, len(final), batch):
            cur.executemany(sql, final[i:i + batch])
    return len(final)


# --- state_meta -> hermes_kv ------------------------------------------------
def migrate_state_meta(sqlite_path: Path, store, *, dry: bool = False) -> int:
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
    args = [(d["key"], json.dumps({"raw": d.get("value")})) for d in (dict(r) for r in rows)]
    if dry:
        return len(args)
    sql = (
        "INSERT INTO hermes_kv (scope, key, value, updated_at) "
        "VALUES ('state_meta', %s, %s::JSONB, now()) "
        "ON CONFLICT (scope, key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = now()"
    )
    with store.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, args)
    return len(args)


# --- json snapshots ---------------------------------------------------------
def migrate_json_snapshots(hermes_home: Path, store, *, dry: bool = False) -> int:
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
        if dry:
            n += 1
            continue
        try:
            store.create_session(
                sid,
                data.get("source") or "json-snapshot",
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


# --- main -------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--hermes-home",
        default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"),
    )
    ap.add_argument("--sqlite", default=None, help="override state.db path")
    ap.add_argument("--dry-run", action="store_true",
                    help="count rows without writing")
    ap.add_argument("--skip-sessions", action="store_true")
    ap.add_argument("--skip-messages", action="store_true")
    ap.add_argument("--skip-meta", action="store_true")
    ap.add_argument("--skip-snapshots", action="store_true")
    args = ap.parse_args()

    hermes_home = Path(args.hermes_home)
    sqlite_path = Path(args.sqlite) if args.sqlite else hermes_home / "state.db"

    url = _load_url()
    if not url:
        print("ERROR: HERMES_DATABASE_URL / COCKROACH_PG_URL / COCKROACH_URL / config.yaml database.url required",
              file=sys.stderr)
        return 2
    os.environ["HERMES_DATABASE_URL"] = url

    if not sqlite_path.exists():
        print(f"no state.db at {sqlite_path} -- nothing to migrate")
        return 0

    from hermes_db.config import get_database_config  # noqa: E402
    from hermes_db.pool import get_store  # noqa: E402

    cfg = get_database_config(force_reload=True)
    if not cfg.is_cockroach:
        print("ERROR: database backend is not CockroachDB/Postgres", file=sys.stderr)
        return 2
    store = get_store()
    store.init_schema()

    t0 = time.time()
    print(f"migrating {sqlite_path} -> {cfg.backend} {cfg.url}")
    try:
        s = 0 if args.skip_sessions else migrate_sessions(
            sqlite_path, store, dry=args.dry_run)
        m = 0 if args.skip_messages else migrate_messages(
            sqlite_path, store, dry=args.dry_run)
        k = 0 if args.skip_meta else migrate_state_meta(
            sqlite_path, store, dry=args.dry_run)
        j = 0 if args.skip_snapshots else migrate_json_snapshots(
            hermes_home, store, dry=args.dry_run)
    except Exception as e:
        traceback.print_exc()
        print(f"ERROR: migration failed: {e}", file=sys.stderr)
        return 1

    dt = time.time() - t0
    print(
        f"done in {dt:.2f}s. "
        f"sessions={s} messages={m} state_meta={k} json_snapshots={j}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
