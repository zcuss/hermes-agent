"""`hermes db` commands for CockroachDB/Postgres storage."""

from __future__ import annotations

import json


def cmd_db(args) -> None:
    from hermes_db.config import describe_for_cli, get_database_config, reset_cache

    action = getattr(args, "db_action", None) or "status"
    if action == "config":
        reset_cache()
        print(describe_for_cli())
        return

    cfg = get_database_config(force_reload=True)
    if action == "status":
        print(describe_for_cli())
        if cfg.is_cockroach:
            from hermes_db.pool import CockroachStore

            store = CockroachStore(cfg)
            try:
                print(json.dumps(store.ping(), default=str, indent=2))
            finally:
                store.close()
        return

    if action == "init":
        if not cfg.is_cockroach:
            raise SystemExit("database.backend must be 'cockroach' or 'postgres' before init")
        from hermes_db.pool import CockroachStore

        store = CockroachStore(cfg)
        try:
            store.init_schema()
            print("cockroach schema: initialized")
            print(json.dumps(store.ping(), default=str, indent=2))
        finally:
            store.close()
        return

    raise SystemExit(f"unknown db action: {action}")


def build_db_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "db",
        help="Manage CockroachDB/Postgres storage backend",
        description="Manage CockroachDB/Postgres storage backend",
    )
    subs = parser.add_subparsers(dest="db_action")
    subs.add_parser("config", help="Show resolved database config")
    subs.add_parser("status", help="Ping database and show config")
    subs.add_parser("init", help="Create/upgrade Hermes CockroachDB schema")
    parser.set_defaults(func=cmd_db)
