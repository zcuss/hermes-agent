"""Database configuration for the CockroachDB/Postgres backend.

Resolution order (highest priority first):

1. ``HERMES_DATABASE_URL`` environment variable.
2. ``database.url`` value in config.yaml.
3. ``database.dsn`` (alias) value in config.yaml.
4. ``cockroach.url`` (legacy) value in config.yaml.
5. A pre-built DSN assembled from ``cockroach.host``, ``cockroach.port``,
   ``cockroach.user``, ``cockroach.password``, ``cockroach.database``,
   ``cockroach.sslmode``, ``cockroach.ca_cert`` keys.
6. SQLite fallback: empty config -- the legacy SQLite path is used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import quote

from hermes_constants import get_hermes_home


def _load_yaml_config() -> Dict[str, Any]:
    """Cheap, best-effort YAML load of the user's config.yaml."""
    cfg_path = get_hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
    except Exception:
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class DatabaseConfig:
    """Resolved database configuration."""

    backend: str = "sqlite"
    url: str = ""
    cluster_name: str = "default"
    profile: str = "default"
    pool_min: int = 1
    pool_max: int = 8
    statement_timeout_ms: int = 0
    application_name: str = "hermes-db"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_cockroach(self) -> bool:
        return self.backend in ("cockroach", "postgres")

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "url": self.url,
            "cluster_name": self.cluster_name,
            "profile": self.profile,
            "pool_min": self.pool_min,
            "pool_max": self.pool_max,
            "statement_timeout_ms": self.statement_timeout_ms,
            "application_name": self.application_name,
        }


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_dsn_from_parts(parts: Dict[str, Any]) -> str:
    """Assemble a libpq DSN from individual keys (cockroach.host, etc.)."""
    user = parts.get("user") or parts.get("user_id") or "root"
    password = parts.get("password") or ""
    host = parts.get("host") or "localhost"
    port = parts.get("port") or 26257
    database = parts.get("database") or "hermes"
    sslmode = parts.get("sslmode") or "disable"
    ca_cert = parts.get("ca_cert") or parts.get("sslrootcert")

    user_enc = quote(str(user), safe="")
    auth = user_enc
    if password:
        auth = f"{user_enc}:{quote(str(password), safe='')}"
    dsn = f"postgresql://{auth}@{host}:{port}/{database}?sslmode={sslmode}"
    if ca_cert:
        dsn += f"&sslrootcert={quote(str(ca_cert), safe='/')}"
    return dsn


def _resolve() -> DatabaseConfig:
    cfg = _load_yaml_config()
    db_section = cfg.get("database") or {}
    legacy = cfg.get("cockroach") or {}

    env_url = os.environ.get("HERMES_DATABASE_URL", "").strip()
    env_backend = os.environ.get("HERMES_DATABASE_BACKEND", "").strip().lower()
    env_cluster = os.environ.get("HERMES_CLUSTER", "").strip()
    env_profile = os.environ.get("HERMES_PROFILE", "").strip()

    url = ""
    if env_url:
        url = env_url
    elif isinstance(db_section, dict):
        url = str(db_section.get("url") or db_section.get("dsn") or "").strip()
        if not url and isinstance(legacy, dict):
            url = str(legacy.get("url") or legacy.get("dsn") or "").strip()
        if not url and isinstance(legacy, dict) and any(
            legacy.get(k) for k in ("host", "user", "user_id", "password")
        ):
            url = _build_dsn_from_parts(legacy)

    backend = "sqlite"
    if env_backend in ("cockroach", "postgres"):
        backend = env_backend
    elif isinstance(db_section, dict):
        declared = str(db_section.get("backend") or "").strip().lower()
        if declared in ("cockroach", "postgres", "sqlite", "disabled"):
            backend = declared
    if backend in ("sqlite", "disabled") and url:
        # URL set but backend not declared -- assume the URL implies cockroach.
        backend = "cockroach"

    cluster_name = env_cluster or str(
        (db_section.get("cluster_name") if isinstance(db_section, dict) else None)
        or "default"
    ).strip() or "default"

    profile = env_profile or str(
        (db_section.get("profile") if isinstance(db_section, dict) else None)
        or "default"
    ).strip() or "default"

    pool_min = _coerce_int(
        db_section.get("pool_min") if isinstance(db_section, dict) else None, 1
    )
    pool_max = max(
        pool_min,
        _coerce_int(
            db_section.get("pool_max") if isinstance(db_section, dict) else None, 8
        ),
    )
    statement_timeout_ms = _coerce_int(
        db_section.get("statement_timeout_ms") if isinstance(db_section, dict) else None,
        0,
    )
    application_name = str(
        (db_section.get("application_name") if isinstance(db_section, dict) else None)
        or "hermes-db"
    ).strip() or "hermes-db"

    return DatabaseConfig(
        backend=backend,
        url=url,
        cluster_name=cluster_name,
        profile=profile,
        pool_min=pool_min,
        pool_max=pool_max,
        statement_timeout_ms=statement_timeout_ms,
        application_name=application_name,
        extra={"legacy_section": legacy} if isinstance(legacy, dict) else {},
    )


_cached: Optional[DatabaseConfig] = None


def get_database_config(*, force_reload: bool = False) -> DatabaseConfig:
    """Return the resolved :class:`DatabaseConfig` (cached per process)."""
    global _cached
    if _cached is None or force_reload:
        _cached = _resolve()
    return _cached


def reset_cache() -> None:
    """Drop the cached config -- used by tests and ``hermes db config``."""
    global _cached
    _cached = None


def describe_for_cli() -> str:
    """Return a human-readable summary of the active configuration."""
    cfg = get_database_config()
    lines = [
        f"backend         : {cfg.backend}",
        f"cluster_name    : {cfg.cluster_name}",
        f"profile         : {cfg.profile}",
        f"pool_min/max    : {cfg.pool_min}/{cfg.pool_max}",
        f"statement_to_ms : {cfg.statement_timeout_ms}",
        f"application_name: {cfg.application_name}",
    ]
    if cfg.is_cockroach:
        sanitized = cfg.url
        if sanitized and "://" in sanitized and "@" in sanitized:
            scheme, rest = sanitized.split("://", 1)
            if "@" in rest:
                _, host_part = rest.split("@", 1)
                sanitized = f"{scheme}://***@{host_part}"
        lines.append(f"url             : {sanitized}")
    elif cfg.is_sqlite:
        lines.append("url             : <legacy sqlite path>")
    else:
        lines.append("url             : <disabled>")
    return "\n".join(lines)


__all__ = [
    "DatabaseConfig",
    "get_database_config",
    "reset_cache",
    "describe_for_cli",
]
