#!/usr/bin/env python3
"""CockroachDB-only session state for Hermes Agent.

SQLite has been intentionally removed from this module. Configure CockroachDB
via HERMES_DATABASE_URL or database.url; no state.db/jsonl fallback exists.
"""
from __future__ import annotations

import json, time
from typing import Any, Dict, List, Optional

from hermes_db.pool import CockroachStore

_last_init_error: Optional[str] = None


def get_last_init_error() -> Optional[str]:
    return _last_init_error


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    cause = get_last_init_error()
    return f"{prefix}: {cause}." if cause else f"{prefix}."


def apply_wal_with_fallback(*args, **kwargs):
    raise RuntimeError("SQLite/WAL is removed. Use CockroachDB via HERMES_DATABASE_URL/database.url.")


def is_malformed_db_error(exc: BaseException) -> bool:
    return False


def repair_state_db_schema(*args, **kwargs) -> Dict[str, Any]:
    return {"repaired": False, "error": "SQLite state.db removed; CockroachDB only"}


class SessionDB:
    """CockroachDB-only compatibility wrapper for legacy SessionDB call-sites."""

    def __init__(self, db_path=None, read_only: bool = False):
        global _last_init_error
        try:
            self.store = CockroachStore()
            self.store.init_schema()
            _last_init_error = None
        except Exception as exc:
            _last_init_error = str(exc)
            raise

    def close(self):
        self.store.close()

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        return self.store.create_session(session_id, source, **kwargs)

    def ensure_session(self, session_id: str, source: str = "cli", **kwargs) -> str:
        self.store.create_session(session_id, source, **kwargs)
        return session_id

    def end_session(self, session_id: str, end_reason: str = "ended") -> None:
        self.store.end_session(session_id, end_reason)

    def reopen_session(self, session_id: str) -> None:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hermes_sessions SET ended_at=NULL, end_reason=NULL WHERE id=%s", (session_id,))

    def update_session_cwd(self, session_id: str, cwd: str) -> None:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hermes_sessions SET cwd=%s WHERE id=%s", (cwd, session_id))

    def update_session_meta(self, session_id: str, **kwargs) -> None:
        if not kwargs: return
        sets=[]; vals=[]
        allowed={"user_id","model","system_prompt","cwd","title","archived"}
        for k,v in kwargs.items():
            if k in allowed:
                sets.append(f"{k}=%s"); vals.append(v)
        if sets:
            vals.append(session_id)
            with self.store.connection() as conn, conn.cursor() as cur:
                cur.execute(f"UPDATE hermes_sessions SET {', '.join(sets)} WHERE id=%s", vals)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        self.update_session_meta(session_id, system_prompt=system_prompt)

    def update_session_model(self, session_id: str, model: str) -> None:
        self.update_session_meta(session_id, model=model)

    def update_token_counts(self, session_id: str, **kwargs) -> None:
        allowed={"input_tokens","output_tokens","cache_read_tokens","cache_write_tokens","reasoning_tokens","tool_call_count"}
        sets=[]; vals=[]
        for k,v in kwargs.items():
            if k in allowed:
                sets.append(f"{k}=%s"); vals.append(int(v or 0))
        if sets:
            vals.append(session_id)
            with self.store.connection() as conn, conn.cursor() as cur:
                cur.execute(f"UPDATE hermes_sessions SET {', '.join(sets)} WHERE id=%s", vals)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM hermes_sessions WHERE id=%s", (session_id,))
            row=cur.fetchone()
            return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM hermes_sessions WHERE id=%s", (session_id_or_prefix,))
            row=cur.fetchone()
            if row: return row["id"]
            cur.execute("SELECT id FROM hermes_sessions WHERE id LIKE %s ORDER BY started_at DESC LIMIT 1", (session_id_or_prefix+'%',))
            row=cur.fetchone()
            return row["id"] if row else None

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        return title.strip()[:120] if isinstance(title, str) and title.strip() else None

    def set_session_title(self, session_id: str, title: str) -> bool:
        title=self.sanitize_title(title)
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hermes_sessions SET title=%s WHERE id=%s", (title, session_id))
        return True

    def get_session_title(self, session_id: str) -> Optional[str]:
        s=self.get_session(session_id); return s.get("title") if s else None

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE hermes_sessions SET archived=%s WHERE id=%s", (bool(archived), session_id))
        return True

    def append_message(self, session_id: str, role: str, content=None, **kwargs) -> int:
        return self.store.add_message(session_id, role, content, **kwargs)

    def get_messages(self, session_id: str, include_inactive: bool=False, **kwargs) -> List[Dict[str, Any]]:
        if include_inactive:
            with self.store.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM hermes_messages WHERE session_id=%s ORDER BY seq", (session_id,))
                return list(cur.fetchall() or [])
        return self.store.list_messages(session_id)

    def get_messages_as_conversation(self, session_id: str, **kwargs) -> List[Dict[str, Any]]:
        return [{"role":m.get("role"), "content":m.get("content")} for m in self.get_messages(session_id)]

    def clear_messages(self, session_id: str) -> None:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM hermes_messages WHERE session_id=%s", (session_id,))
            cur.execute("UPDATE hermes_sessions SET message_count=0 WHERE id=%s", (session_id,))

    def delete_session(self, session_id: str, **kwargs) -> bool:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM hermes_sessions WHERE id=%s", (session_id,))
        return True

    def delete_sessions(self, session_ids: List[str], **kwargs) -> int:
        n=0
        for sid in session_ids:
            n += 1 if self.delete_session(sid) else 0
        return n

    def list_sessions_rich(self, limit: int=50, offset: int=0, include_archived: bool=False, **kwargs) -> List[Dict[str, Any]]:
        where="WHERE cluster_name=%s" + ("" if include_archived else " AND archived=false")
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM hermes_sessions {where} ORDER BY started_at DESC LIMIT %s OFFSET %s", (self.store.config.cluster_name, limit, offset))
            return list(cur.fetchall() or [])

    def search_messages(self, query: str, limit: int=20, **kwargs) -> List[Dict[str, Any]]:
        return self.store.search_messages(query, limit=limit)

    def search_sessions(self, query: str, limit: int=20, **kwargs) -> List[Dict[str, Any]]:
        rows=self.store.search_messages(query, limit=limit)
        seen=set(); out=[]
        for r in rows:
            sid=r.get("session_id")
            if sid not in seen:
                seen.add(sid); out.append(self.get_session(sid) or {"id": sid})
        return out

    def session_count(self) -> int:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM hermes_sessions")
            return int(cur.fetchone()["n"])

    def message_count(self) -> int:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM hermes_messages")
            return int(cur.fetchone()["n"])

    def get_meta(self, key: str) -> Optional[str]:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM hermes_kv WHERE scope='meta' AND key=%s", (key,))
            row=cur.fetchone(); return json.dumps(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO hermes_kv(scope,key,value) VALUES('meta',%s,%s::JSONB) ON CONFLICT(scope,key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()", (key, json.dumps(value)))

    # Feature-specific legacy APIs: no-op or simple false until their callers are migrated.
    def try_acquire_compression_lock(self, *a, **k): return True
    def release_compression_lock(self, *a, **k): return None
    def get_compression_lock_holder(self, *a, **k): return None
    def prune_empty_ghost_sessions(self, *a, **k): return 0
    def finalize_orphaned_compression_sessions(self, *a, **k): return 0
    def get_session_by_title(self, title):
        with self.store.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM hermes_sessions WHERE title=%s ORDER BY started_at DESC LIMIT 1", (title,))
            row=cur.fetchone(); return dict(row) if row else None
    def resolve_session_by_title(self, title):
        s=self.get_session_by_title(title); return s.get("id") if s else None
    def get_next_title_in_lineage(self, base_title): return base_title
    def get_compression_tip(self, session_id): return session_id
    def list_cron_job_runs(self, *a, **k): return []
    def replace_messages(self, session_id, messages, **kwargs):
        self.clear_messages(session_id)
        for m in messages: self.append_message(session_id, m.get("role","user"), m.get("content"), **{x:m.get(x) for x in ("tool_call_id","tool_calls","tool_name")})
    def get_messages_around(self, session_id, around_message_id=None, window=5, **kwargs): return self.get_messages(session_id)
    def get_anchored_view(self, session_id, around_message_id=None, window=5, **kwargs): return {"messages": self.get_messages(session_id), "messages_before":0, "messages_after":0}
    def resolve_resume_session_id(self, *a, **k): return self.resolve_session_id(a[0]) if a else None
    def rewind_to_message(self, *a, **k): return False
    def restore_rewound(self, *a, **k): return False
    def list_recent_user_messages(self, *a, **k): return []
    def search_sessions_by_id(self, *a, **k): return []
    def export_session(self, session_id): return {"session": self.get_session(session_id), "messages": self.get_messages(session_id)}
    def export_all(self): return [self.export_session(s["id"]) for s in self.list_sessions_rich(limit=100000, include_archived=True)]
    def delete_session_if_empty(self, *a, **k): return False
    def count_empty_sessions(self, *a, **k): return 0
    def delete_empty_sessions(self, *a, **k): return 0
    def prune_sessions(self, *a, **k): return 0
    def optimize_fts(self): return None
    def vacuum(self): return None
    def maybe_auto_prune_and_vacuum(self): return None
    def request_handoff(self, *a, **k): return None
    def get_handoff_state(self, *a, **k): return None
    def list_pending_handoffs(self, *a, **k): return []
    def claim_handoff(self, *a, **k): return False
    def complete_handoff(self, *a, **k): return None
    def fail_handoff(self, *a, **k): return None
    def enable_telegram_topic_mode(self, *a, **k): return None
    def disable_telegram_topic_mode(self, *a, **k): return None
    def is_telegram_topic_mode_enabled(self, *a, **k): return False
    def get_telegram_topic_binding(self, *a, **k): return None
    def list_telegram_topic_bindings_for_chat(self, *a, **k): return []
    def get_telegram_topic_binding_by_session(self, *a, **k): return None
    def bind_telegram_topic(self, *a, **k): return None
    def is_telegram_session_linked_to_topic(self, *a, **k): return False
    def list_unlinked_telegram_sessions_for_user(self, *a, **k): return []
