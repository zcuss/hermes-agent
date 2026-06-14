"""CockroachDB schema for Hermes durable state."""

SCHEMA_VERSION = 1

DDL = [
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """
    CREATE TABLE IF NOT EXISTS hermes_schema_version (
        component TEXT PRIMARY KEY,
        version INT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hermes_sessions (
        id TEXT PRIMARY KEY,
        cluster_name TEXT NOT NULL DEFAULT 'default',
        profile TEXT NOT NULL DEFAULT 'default',
        source TEXT NOT NULL,
        user_id TEXT NULL,
        model TEXT NULL,
        model_config JSONB NULL,
        system_prompt TEXT NULL,
        parent_session_id TEXT NULL,
        started_at DOUBLE PRECISION NOT NULL,
        ended_at DOUBLE PRECISION NULL,
        end_reason TEXT NULL,
        message_count INT NOT NULL DEFAULT 0,
        tool_call_count INT NOT NULL DEFAULT 0,
        input_tokens INT NOT NULL DEFAULT 0,
        output_tokens INT NOT NULL DEFAULT 0,
        cache_read_tokens INT NOT NULL DEFAULT 0,
        cache_write_tokens INT NOT NULL DEFAULT 0,
        reasoning_tokens INT NOT NULL DEFAULT 0,
        cwd TEXT NULL,
        title TEXT NULL,
        archived BOOL NOT NULL DEFAULT false,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hermes_sessions_cluster_started ON hermes_sessions (cluster_name, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hermes_sessions_source ON hermes_sessions (source, id)",
    """
    CREATE TABLE IF NOT EXISTS hermes_messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id TEXT NOT NULL REFERENCES hermes_sessions(id) ON DELETE CASCADE,
        cluster_name TEXT NOT NULL DEFAULT 'default',
        profile TEXT NOT NULL DEFAULT 'default',
        seq BIGINT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NULL,
        tool_call_id TEXT NULL,
        tool_calls JSONB NULL,
        tool_name TEXT NULL,
        timestamp DOUBLE PRECISION NOT NULL,
        token_count INT NULL,
        finish_reason TEXT NULL,
        reasoning TEXT NULL,
        platform_message_id TEXT NULL,
        observed BOOL NOT NULL DEFAULT false,
        active BOOL NOT NULL DEFAULT true,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
        UNIQUE (session_id, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hermes_messages_session_seq ON hermes_messages (session_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_hermes_messages_cluster_ts ON hermes_messages (cluster_name, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS hermes_memory (
        key TEXT PRIMARY KEY,
        cluster_name TEXT NOT NULL DEFAULT 'default',
        profile TEXT NOT NULL DEFAULT 'default',
        namespace TEXT NOT NULL,
        target TEXT NOT NULL,
        content TEXT NOT NULL,
        embedding JSONB NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hermes_memory_scope ON hermes_memory (cluster_name, profile, namespace, target)",
    """
    CREATE TABLE IF NOT EXISTS hermes_kv (
        scope TEXT NOT NULL,
        key TEXT NOT NULL,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (scope, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hermes_cluster_nodes (
        node_id TEXT PRIMARY KEY,
        cluster_name TEXT NOT NULL DEFAULT 'default',
        base_url TEXT NULL,
        role TEXT NOT NULL DEFAULT 'worker',
        labels JSONB NOT NULL DEFAULT '{}'::JSONB,
        capacity INT NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'online',
        cpu_percent DOUBLE PRECISION NULL,
        mem_percent DOUBLE PRECISION NULL,
        swap_percent DOUBLE PRECISION NULL,
        last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hermes_cluster_nodes_seen ON hermes_cluster_nodes (cluster_name, status, last_seen DESC)",
    """
    CREATE TABLE IF NOT EXISTS hermes_cluster_tasks (
        task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        cluster_name TEXT NOT NULL DEFAULT 'default',
        parent_task_id UUID NULL,
        session_id TEXT NULL,
        created_by TEXT NOT NULL,
        assigned_node TEXT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        priority INT NOT NULL DEFAULT 0,
        prompt TEXT NOT NULL,
        workspace_root TEXT NULL,
        git_remote TEXT NULL,
        branch TEXT NULL,
        toolsets JSONB NOT NULL DEFAULT '[]'::JSONB,
        result JSONB NULL,
        error TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        claimed_at TIMESTAMPTZ NULL,
        finished_at TIMESTAMPTZ NULL,
        metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_hermes_cluster_tasks_queue ON hermes_cluster_tasks (cluster_name, status, priority DESC, created_at ASC)",
    "CREATE INDEX IF NOT EXISTS idx_hermes_cluster_tasks_node ON hermes_cluster_tasks (assigned_node, status, updated_at DESC)",
]
