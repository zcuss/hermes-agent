"""Cluster client (CLI submit/list helpers)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from hermes_cluster.protocol import AUTH_HEADER
from hermes_cluster.state import ClusterConfig


def submit(core_url: str, auth_token: str, prompt: str, **kwargs: Any) -> Dict[str, Any]:
    payload = {"prompt": prompt, **kwargs}
    r = httpx.post(
        f"{core_url.rstrip('/')}/v1/tasks",
        json=payload,
        headers={AUTH_HEADER: auth_token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def list_tasks(core_url: str, auth_token: str, limit: int = 50) -> Dict[str, Any]:
    r = httpx.get(
        f"{core_url.rstrip('/')}/v1/tasks",
        params={"limit": limit},
        headers={AUTH_HEADER: auth_token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def list_nodes(core_url: str, auth_token: str) -> Dict[str, Any]:
    r = httpx.get(
        f"{core_url.rstrip('/')}/v1/nodes",
        headers={AUTH_HEADER: auth_token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def resolve_core_args(args) -> Optional[ClusterConfig]:
    cfg = ClusterConfig.load()
    if cfg is None:
        raise SystemExit("not joined to any cluster -- run `hermes cluster init` (core host) or `hermes cluster join <url>` (node)")
    core_url = getattr(args, "core_url", None) or cfg.core_url
    token = getattr(args, "token", None) or cfg.auth_token
    return ClusterConfig(
        cluster_name=cfg.cluster_name,
        core_url=core_url,
        auth_token=token,
        node_id=cfg.node_id,
        role=cfg.role,
    )
