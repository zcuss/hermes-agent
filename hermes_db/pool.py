"""Connection pool and high-level operations for CockroachDB/Postgres."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .config import DatabaseConfig, get_database_config
from .schema import DDL, SCHEMA_VERSION

try:  # lazy optional dependency
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except Exception:  # pragma: no cover - exercised when dependency absent
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore
    ConnectionPool = None  # type: ignore


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _arr(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


class CockroachStore:
    """Small durable store for sessions, memory and cluster orchestration."""

    def __init__(self, config: Optional[DatabaseConfig] = None):
        self.config = config or get_database_config()
        if not self.config.is_cockroach:
            raise RuntimeError(f"database backend is {self.config.backend!r}, not cockroach/postgres")
        if not self.config.url:
            raise RuntimeError("database.url / HERMES_DATABASE_URL is required")
        if ConnectionPool is None or psycopg is None:
            raise RuntimeError("psycopg[binary,pool] is required for CockroachDB backend")
        kwargs = {
            "autocommit": True,
            "row_factory": dict_row,
            "application_name": self.config.application_name,
        }
        self.pool = ConnectionPool(
            self.config.url,
            min_size=self.config.pool_min,
            max_size=self.config.pool_max,
            kwargs=kwargs,
            open=False,
        )
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self):
        with self.pool.connection() as conn:
            if self.config.statement_timeout_ms:
                # Use set_config() with an integer cast so the value can never
                # smuggle SQL -- psycopg's %s passes it as a parameter, not
                # interpolated text.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('statement_timeout', (%s)::TEXT, false)",
                        (int(self.config.statement_timeout_ms),),
                    )
            yield conn

    def init_schema(self) -> None:
        with self.connection() as conn:
            with conn.cursor() as cur:
                for stmt in DDL:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO hermes_schema_version (component, version, updated_at) VALUES ('core', %s, now()) ON CONFLICT (component) DO UPDATE SET version=EXCLUDED.version, updated_at=now()",
                    (SCHEMA_VERSION,),
                )

    def ping(self) -> Dict[str, Any]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT now() AS now, version() AS version")
                row = cur.fetchone() or {}
        return {"ok": True, "backend": self.config.backend, **row}

    # Sessions/messages -------------------------------------------------
    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        self.init_schema()
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_sessions (
                    id, cluster_name, profile, source, user_id, model, model_config,
                    system_prompt, parent_session_id, started_at, cwd, title, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s::JSONB)
                ON CONFLICT (id) DO UPDATE SET
                    source=EXCLUDED.source,
                    user_id=COALESCE(EXCLUDED.user_id, hermes_sessions.user_id),
                    model=COALESCE(EXCLUDED.model, hermes_sessions.model),
                    model_config=COALESCE(EXCLUDED.model_config, hermes_sessions.model_config),
                    system_prompt=COALESCE(EXCLUDED.system_prompt, hermes_sessions.system_prompt),
                    parent_session_id=COALESCE(EXCLUDED.parent_session_id, hermes_sessions.parent_session_id),
                    cwd=COALESCE(EXCLUDED.cwd, hermes_sessions.cwd),
                    title=COALESCE(EXCLUDED.title, hermes_sessions.title),
                    metadata=COALESCE(EXCLUDED.metadata, hermes_sessions.metadata)
                """,
                (
                    session_id,
                    kwargs.get("cluster_name", self.config.cluster_name),
                    kwargs.get("profile", self.config.profile),
                    source,
                    kwargs.get("user_id"),
                    kwargs.get("model"),
                    _json(kwargs.get("model_config")),
                    kwargs.get("system_prompt"),
                    kwargs.get("parent_session_id"),
                    float(kwargs.get("started_at", time.time())),
                    kwargs.get("cwd"),
                    kwargs.get("title"),
                    _json(kwargs.get("metadata")),
                ),
            )
        return session_id

    def end_session(self, session_id: str, end_reason: str = "ended") -> None:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE hermes_sessions SET ended_at=%s, end_reason=%s WHERE id=%s",
                (time.time(), end_reason, session_id),
            )

    def add_message(self, session_id: str, role: str, content: Any = None, **kwargs: Any) -> int:
        self.init_schema()
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COALESCE(max(seq), 0) + 1 AS seq FROM hermes_messages WHERE session_id=%s", (session_id,))
            seq = int((cur.fetchone() or {"seq": 1})["seq"])
            cur.execute(
                """
                INSERT INTO hermes_messages (
                    session_id, cluster_name, profile, seq, role, content, tool_call_id,
                    tool_calls, tool_name, timestamp, token_count, finish_reason,
                    reasoning, platform_message_id, observed, active, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,%s,%s,%s,%s::JSONB)
                """,
                (
                    session_id,
                    kwargs.get("cluster_name", self.config.cluster_name),
                    kwargs.get("profile", self.config.profile),
                    seq,
                    role,
                    content if isinstance(content, str) or content is None else json.dumps(content, ensure_ascii=False),
                    kwargs.get("tool_call_id"),
                    _arr(kwargs.get("tool_calls")),
                    kwargs.get("tool_name"),
                    float(kwargs.get("timestamp", time.time())),
                    kwargs.get("token_count"),
                    kwargs.get("finish_reason"),
                    kwargs.get("reasoning"),
                    kwargs.get("platform_message_id"),
                    bool(kwargs.get("observed", False)),
                    bool(kwargs.get("active", True)),
                    _json(kwargs.get("metadata")),
                ),
            )
            cur.execute("UPDATE hermes_sessions SET message_count=message_count+1 WHERE id=%s", (session_id,))
        return seq

    def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM hermes_messages WHERE session_id=%s AND active ORDER BY seq", (session_id,))
            return list(cur.fetchall() or [])

    def search_messages(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.session_id, m.seq, m.role, m.content, m.timestamp, s.title, s.source
                FROM hermes_messages m JOIN hermes_sessions s ON s.id=m.session_id
                WHERE m.cluster_name=%s AND m.active AND m.content ILIKE %s
                ORDER BY m.timestamp DESC LIMIT %s
                """,
                (self.config.cluster_name, pattern, limit),
            )
            return list(cur.fetchall() or [])

    # Memory ------------------------------------------------------------
    def upsert_memory(self, namespace: str, target: str, content: str, key: Optional[str] = None, **metadata: Any) -> str:
        self.init_schema()
        key = key or f"{self.config.cluster_name}:{self.config.profile}:{namespace}:{target}:{uuid.uuid4()}"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_memory (key, cluster_name, profile, namespace, target, content, metadata, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::JSONB,now())
                ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, metadata=EXCLUDED.metadata, updated_at=now()
                """,
                (key, self.config.cluster_name, self.config.profile, namespace, target, content, _json(metadata)),
            )
        return key

    def search_memory(self, query: str, namespace: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        pattern = f"%{query}%"
        sql = "SELECT * FROM hermes_memory WHERE cluster_name=%s AND profile=%s AND content ILIKE %s"
        params: List[Any] = [self.config.cluster_name, self.config.profile, pattern]
        if namespace:
            sql += " AND namespace=%s"
            params.append(namespace)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall() or [])

    # Cluster -----------------------------------------------------------
    def register_node(self, node_id: str, **kwargs: Any) -> None:
        self.init_schema()
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_cluster_nodes (
                    node_id, cluster_name, base_url, role, labels, capacity, status,
                    cpu_percent, mem_percent, swap_percent, last_seen, metadata
                ) VALUES (%s,%s,%s,%s,%s::JSONB,%s,%s,%s,%s,%s,now(),%s::JSONB)
                ON CONFLICT (node_id) DO UPDATE SET
                    cluster_name=EXCLUDED.cluster_name, base_url=EXCLUDED.base_url, role=EXCLUDED.role,
                    labels=EXCLUDED.labels, capacity=EXCLUDED.capacity, status=EXCLUDED.status,
                    cpu_percent=EXCLUDED.cpu_percent, mem_percent=EXCLUDED.mem_percent,
                    swap_percent=EXCLUDED.swap_percent, last_seen=now(), metadata=EXCLUDED.metadata
                """,
                (
                    node_id,
                    kwargs.get("cluster_name", self.config.cluster_name),
                    kwargs.get("base_url"),
                    kwargs.get("role", "worker"),
                    _json(kwargs.get("labels")),
                    int(kwargs.get("capacity", 1)),
                    kwargs.get("status", "online"),
                    kwargs.get("cpu_percent"),
                    kwargs.get("mem_percent"),
                    kwargs.get("swap_percent"),
                    _json(kwargs.get("metadata")),
                ),
            )

    def create_task(self, prompt: str, created_by: str, **kwargs: Any) -> str:
        self.init_schema()
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_cluster_tasks (
                    cluster_name, parent_task_id, session_id, created_by, priority,
                    prompt, workspace_root, git_remote, branch, toolsets, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::JSONB,%s::JSONB)
                RETURNING task_id::TEXT AS task_id
                """,
                (
                    kwargs.get("cluster_name", self.config.cluster_name),
                    kwargs.get("parent_task_id"),
                    kwargs.get("session_id"),
                    created_by,
                    int(kwargs.get("priority", 0)),
                    prompt,
                    kwargs.get("workspace_root"),
                    kwargs.get("git_remote"),
                    kwargs.get("branch"),
                    _arr(kwargs.get("toolsets")),
                    _json(kwargs.get("metadata")),
                ),
            )
            row = cur.fetchone() or {}
        return str(row.get("task_id"))

    def claim_task(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hermes_cluster_tasks
                SET status='running', assigned_node=%s, claimed_at=now(), updated_at=now()
                WHERE task_id = (
                    SELECT task_id FROM hermes_cluster_tasks
                    WHERE cluster_name=%s AND status='queued'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                )
                RETURNING *
                """,
                (node_id, self.config.cluster_name),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def finish_task(self, task_id: str, result: Any = None, error: Optional[str] = None) -> None:
        status = "failed" if error else "done"
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hermes_cluster_tasks
                SET status=%s, result=%s::JSONB, error=%s, updated_at=now(), finished_at=now()
                WHERE task_id=%s
                """,
                (status, _json(result or {}), error, task_id),
            )

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM hermes_cluster_tasks WHERE cluster_name=%s ORDER BY created_at DESC LIMIT %s",
                (self.config.cluster_name, limit),
            )
            return list(cur.fetchall() or [])

    def list_nodes(self) -> List[Dict[str, Any]]:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM hermes_cluster_nodes WHERE cluster_name=%s ORDER BY last_seen DESC",
                (self.config.cluster_name,),
            )
            return list(cur.fetchall() or [])


_store: Optional[CockroachStore] = None


def get_store(*, force_new: bool = False) -> CockroachStore:
    global _store
    if _store is None or force_new:
        _store = CockroachStore()
    return _store


def close_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
