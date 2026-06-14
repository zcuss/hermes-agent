"""CockroachDB/Postgres storage backend for Hermes.

This package is intentionally independent from the legacy SQLite modules so it
can be enabled without destabilising local-only installs.  Set
``database.backend: cockroach`` and ``database.dsn`` in config.yaml, or export
``HERMES_DATABASE_URL``.
"""

from .config import DatabaseConfig, get_database_config
from .pool import CockroachStore, get_store

__all__ = ["DatabaseConfig", "get_database_config", "CockroachStore", "get_store"]
