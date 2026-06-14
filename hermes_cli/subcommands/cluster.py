"""`hermes cluster` commands."""

from __future__ import annotations

import json
import os
import secrets


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def cmd_cluster(args) -> None:
    action = getattr(args, "cluster_action", None) or "status"
    from hermes_cluster.state import ClusterConfig, default_node_id, generate_token, local_ip

    if action == "init":
        token = getattr(args, "token", None) or generate_token()
        host = getattr(args, "host", "0.0.0.0")
        port = int(getattr(args, "port", 8787))
        public_host = getattr(args, "public_host", None) or local_ip()
        cfg = ClusterConfig(
            cluster_name=getattr(args, "name", None) or "default",
            core_url=f"http://{public_host}:{port}",
            auth_token=token,
            node_id=getattr(args, "node_id", None) or f"core-{default_node_id()}",
            role="core",
        )
        cfg.save()
        print("cluster initialized")
        print(f"config: ~/.hermes/cluster/config.yaml")
        print(f"core_url: {cfg.core_url}")
        print(f"token: {cfg.auth_token}")
        if getattr(args, "print_join", False):
            print(f"join: hermes cluster join {cfg.core_url} --token {cfg.auth_token} --name {cfg.cluster_name}")
        return

    if action == "join":
        cfg = ClusterConfig(
            cluster_name=getattr(args, "name", None) or "default",
            core_url=getattr(args, "core_url"),
            auth_token=getattr(args, "token"),
            node_id=getattr(args, "node_id", None) or default_node_id(),
            role="worker",
        )
        cfg.save()
        print("node joined")
        print(f"config: ~/.hermes/cluster/config.yaml")
        print(f"node_id: {cfg.node_id}")
        print(f"core_url: {cfg.core_url}")
        return

    if action == "core":
        from hermes_cluster.core import run

        run(host=getattr(args, "host", "0.0.0.0"), port=int(getattr(args, "port", 8787)))
        return

    if action == "node":
        from hermes_cluster.node import run

        run(poll_interval=float(getattr(args, "poll_interval", 2.0)), once=bool(getattr(args, "once", False)))
        return

    if action == "submit":
        from hermes_cluster.client import resolve_core_args, submit

        cfg = resolve_core_args(args)
        res = submit(
            cfg.core_url,
            cfg.auth_token,
            getattr(args, "prompt"),
            cluster_name=cfg.cluster_name,
            created_by=cfg.node_id or "cli",
            priority=int(getattr(args, "priority", 0)),
            workspace_root=getattr(args, "workspace_root", None) or os.getcwd(),
            git_remote=getattr(args, "git_remote", None) or "",
            branch=getattr(args, "branch", None) or "",
            toolsets=(getattr(args, "toolsets", None) or "").split(",") if getattr(args, "toolsets", None) else [],
        )
        _print(res)
        return

    if action == "tasks":
        from hermes_cluster.client import list_tasks, resolve_core_args

        cfg = resolve_core_args(args)
        _print(list_tasks(cfg.core_url, cfg.auth_token, limit=int(getattr(args, "limit", 50))))
        return

    if action == "nodes":
        from hermes_cluster.client import list_nodes, resolve_core_args

        cfg = resolve_core_args(args)
        _print(list_nodes(cfg.core_url, cfg.auth_token))
        return

    if action == "status":
        cfg = ClusterConfig.load()
        if cfg is None:
            print("cluster: not configured")
        else:
            _print({"cluster_name": cfg.cluster_name, "core_url": cfg.core_url, "node_id": cfg.node_id, "role": cfg.role, "config": "~/.hermes/cluster/config.yaml"})
        return

    raise SystemExit(f"unknown cluster action: {action}")


def build_cluster_parser(subparsers) -> None:
    p = subparsers.add_parser("cluster", help="Run Hermes cluster core/node task delegation")
    subs = p.add_subparsers(dest="cluster_action")

    init = subs.add_parser("init", help="Initialize this machine as a cluster core")
    init.add_argument("--name", default="default")
    init.add_argument("--host", default="0.0.0.0")
    init.add_argument("--public-host", default=None)
    init.add_argument("--port", type=int, default=8787)
    init.add_argument("--token", default=None)
    init.add_argument("--node-id", default=None)
    init.add_argument("--print-join", action="store_true")
    init.set_defaults(func=cmd_cluster)

    join = subs.add_parser("join", help="Join a core from another machine")
    join.add_argument("core_url")
    join.add_argument("--token", required=True)
    join.add_argument("--name", default="default")
    join.add_argument("--node-id", default=None)
    join.set_defaults(func=cmd_cluster)

    core = subs.add_parser("core", help="Run cluster core HTTP server")
    core.add_argument("--host", default="0.0.0.0")
    core.add_argument("--port", type=int, default=8787)
    core.set_defaults(func=cmd_cluster)

    node = subs.add_parser("node", help="Run node worker loop")
    node.add_argument("--poll-interval", type=float, default=2.0)
    node.add_argument("--once", action="store_true")
    node.set_defaults(func=cmd_cluster)

    submit_p = subs.add_parser("submit", help="Submit a sub-task to the cluster")
    submit_p.add_argument("prompt")
    submit_p.add_argument("--workspace-root", default=None)
    submit_p.add_argument("--git-remote", default="")
    submit_p.add_argument("--branch", default="")
    submit_p.add_argument("--toolsets", default="")
    submit_p.add_argument("--priority", type=int, default=0)
    submit_p.add_argument("--core-url", default=None)
    submit_p.add_argument("--token", default=None)
    submit_p.set_defaults(func=cmd_cluster)

    tasks = subs.add_parser("tasks", help="List cluster tasks")
    tasks.add_argument("--limit", type=int, default=50)
    tasks.add_argument("--core-url", default=None)
    tasks.add_argument("--token", default=None)
    tasks.set_defaults(func=cmd_cluster)

    nodes = subs.add_parser("nodes", help="List cluster nodes")
    nodes.add_argument("--core-url", default=None)
    nodes.add_argument("--token", default=None)
    nodes.set_defaults(func=cmd_cluster)

    status = subs.add_parser("status", help="Show local cluster config")
    status.set_defaults(func=cmd_cluster)

    p.set_defaults(func=cmd_cluster)
