"""Cluster node worker.

A node repeatedly pulls one task from the core and runs it via ``hermes chat -q``
inside the requested workspace root.  The workspace is shared by convention:
all nodes receive the same ``workspace_root`` and/or ``git_remote``.  This file
only spawns tasks; it does not copy source trees or persist history locally.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import httpx

from hermes_cluster.protocol import AUTH_HEADER
from hermes_cluster.state import ClusterConfig, default_node_id, local_ip


def _usage() -> Dict[str, float]:
    try:
        import psutil
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        return {
            "cpu_percent": float(psutil.cpu_percent(interval=0.1)),
            "mem_percent": float(vm.percent),
            "swap_percent": float(sm.percent),
        }
    except Exception:
        return {"cpu_percent": 0.0, "mem_percent": 0.0, "swap_percent": 0.0}


def _run_task(task: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    prompt = task.get("prompt") or ""
    task_id = str(task.get("task_id"))
    workspace = task.get("workspace_root") or os.getcwd()
    toolsets = task.get("toolsets") or []
    if isinstance(toolsets, str):
        toolsets = [toolsets]

    cmd = [sys.executable, "-m", "hermes_cli.main", "chat", "-q", prompt]
    if toolsets:
        cmd.extend(["--toolsets", ",".join(toolsets)])

    env = os.environ.copy()
    env["HERMES_CLUSTER_TASK_ID"] = task_id
    env["HERMES_CLUSTER_NODE_ID"] = node_id
    env["HERMES_CLUSTER_WORKSPACE"] = workspace

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace if workspace and os.path.isdir(workspace) else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=int(task.get("metadata", {}).get("timeout", 3600)),
        )
        return {
            "status": "done" if proc.returncode == 0 else "failed",
            "output": proc.stdout[-20000:],
            "result": {"returncode": proc.returncode, "duration_s": time.time() - started},
            "error": None if proc.returncode == 0 else f"hermes exited {proc.returncode}",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "output": "",
            "result": {"duration_s": time.time() - started},
            "error": str(exc),
        }


def run(poll_interval: float = 2.0, once: bool = False, cfg: Optional[ClusterConfig] = None) -> None:  # pragma: no cover - loop
    cfg = cfg or ClusterConfig.load()
    if not cfg or not cfg.auth_token or not cfg.core_url:
        raise SystemExit("cluster config missing -- run `hermes cluster join` first")
    node_id = cfg.node_id or default_node_id()
    headers = {AUTH_HEADER: cfg.auth_token}
    client = httpx.Client(base_url=cfg.core_url.rstrip("/"), headers=headers, timeout=30)
    advertise = f"http://{local_ip()}:0"

    while True:
        stats = _usage()
        if stats.get("cpu_percent", 0) >= 95 or stats.get("mem_percent", 0) >= 95 or stats.get("swap_percent", 0) >= 95:
            # Back off under pressure; do not claim new work while saturated.
            time.sleep(max(poll_interval, 10.0))
            if once:
                return
            continue
        payload = {
            "node_id": node_id,
            "cluster_name": cfg.cluster_name,
            "base_url": advertise,
            "role": "worker",
            "capacity": 1,
            **stats,
        }
        client.post(f"/v1/nodes/{node_id}/heartbeat", json=payload)
        claimed = client.post("/v1/tasks/claim", json={"node_id": node_id}).json().get("task")
        if not claimed:
            if once:
                return
            time.sleep(poll_interval)
            continue
        result = _run_task(claimed, node_id)
        client.post(f"/v1/tasks/{claimed['task_id']}/finish", json={"node_id": node_id, **result})
        if once:
            return


def main(argv: Optional[list] = None) -> None:  # pragma: no cover
    p = argparse.ArgumentParser(prog="hermes cluster node")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    run(poll_interval=args.poll_interval, once=args.once)
