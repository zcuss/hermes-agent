"""Cluster core HTTP server.

The core is a thin orchestrator: durable state lives in CockroachDB, the
agent loop runs in the node.  Endpoints:

* ``GET  /health``                     — liveness.
* ``POST /v1/nodes/register``          — node heartbeat / registration.
* ``POST /v1/nodes/{id}/heartbeat``    — recurring stats update.
* ``GET  /v1/nodes``                   — list online nodes.
* ``POST /v1/tasks``                   — enqueue a task.
* ``GET  /v1/tasks/{id}``              — fetch task status/result.
* ``GET  /v1/tasks``                   — list recent tasks.
* ``POST /v1/tasks/claim``             — node pulls next queued task.
* ``POST /v1/tasks/{id}/finish``       — node returns a result.

All endpoints except ``/health`` require ``X-Hermes-Cluster-Token``.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Any, Dict, Optional

try:  # FastAPI is a runtime dep
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    import uvicorn
except Exception:  # pragma: no cover
    FastAPI = None  # type: ignore
    HTTPException = None  # type: ignore
    Request = None  # type: ignore
    JSONResponse = None  # type: ignore
    uvicorn = None  # type: ignore

from hermes_cluster.protocol import AUTH_HEADER, TaskResult, TaskSpec
from hermes_cluster.state import ClusterConfig


logger = logging.getLogger("hermes-cluster-core")


def _build_app(core_cfg: ClusterConfig):
    if FastAPI is None:
        raise RuntimeError("fastapi is required to run the cluster core -- install fastapi + uvicorn")

    app = FastAPI(title="hermes-cluster-core", version="1.0.0")
    from hermes_db.pool import get_store  # late import so SQLite-only installs don't fail

    state = {"store": None, "cfg": core_cfg}

    @app.on_event("startup")
    def _startup() -> None:
        state["store"] = get_store()
        state["store"].init_schema()
        logger.info("cluster core up: cluster=%s db=%s", core_cfg.cluster_name, state["store"].config.url)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        if state["store"] is not None:
            state["store"].close()

    def _check_token(request: Request) -> None:
        provided = request.headers.get(AUTH_HEADER) or request.headers.get(AUTH_HEADER.lower())
        if not provided or provided != core_cfg.auth_token:
            raise HTTPException(status_code=401, detail="invalid cluster token")

    @app.get("/health")
    def health():
        return {"ok": True, "cluster": core_cfg.cluster_name, "time": time.time()}

    @app.post("/v1/nodes/register")
    def register_node(req: Request, body: Dict[str, Any]):
        _check_token(req)
        node_id = body.get("node_id")
        if not node_id:
            raise HTTPException(400, "node_id is required")
        state["store"].register_node(
            node_id=node_id,
            cluster_name=body.get("cluster_name", core_cfg.cluster_name),
            base_url=body.get("base_url", ""),
            role=body.get("role", "worker"),
            labels=body.get("labels", {}),
            capacity=int(body.get("capacity", 1)),
            status=body.get("status", "online"),
            cpu_percent=body.get("cpu_percent"),
            mem_percent=body.get("mem_percent"),
            swap_percent=body.get("swap_percent"),
            metadata=body.get("metadata", {}),
        )
        return {"ok": True}

    @app.post("/v1/nodes/{node_id}/heartbeat")
    def heartbeat(req: Request, node_id: str, body: Dict[str, Any]):
        _check_token(req)
        state["store"].register_node(
            node_id=node_id,
            cluster_name=body.get("cluster_name", core_cfg.cluster_name),
            base_url=body.get("base_url", ""),
            role=body.get("role", "worker"),
            labels=body.get("labels", {}),
            capacity=int(body.get("capacity", 1)),
            status=body.get("status", "online"),
            cpu_percent=body.get("cpu_percent"),
            mem_percent=body.get("mem_percent"),
            swap_percent=body.get("swap_percent"),
            metadata=body.get("metadata", {}),
        )
        return {"ok": True}

    @app.get("/v1/nodes")
    def list_nodes(req: Request, limit: int = 100):
        _check_token(req)
        return {"nodes": state["store"].list_nodes()[:limit]}

    @app.post("/v1/tasks")
    def enqueue(req: Request, body: Dict[str, Any]):
        _check_token(req)
        if "prompt" not in body or not body["prompt"]:
            raise HTTPException(400, "prompt is required")
        body.setdefault("cluster_name", core_cfg.cluster_name)
        spec = TaskSpec.from_dict(body)
        task_id = state["store"].create_task(
            spec.prompt,
            spec.created_by,
            cluster_name=spec.cluster_name,
            parent_task_id=spec.parent_task_id,
            session_id=spec.session_id,
            priority=spec.priority,
            workspace_root=spec.workspace_root,
            git_remote=spec.git_remote,
            branch=spec.branch,
            toolsets=spec.toolsets,
            metadata=spec.metadata,
        )
        return {"task_id": task_id}

    @app.post("/v1/tasks/claim")
    def claim(req: Request, body: Dict[str, Any]):
        _check_token(req)
        node_id = body.get("node_id")
        if not node_id:
            raise HTTPException(400, "node_id is required")
        task = state["store"].claim_task(node_id)
        if not task:
            return {"task": None}
        return {"task": task}

    @app.post("/v1/tasks/{task_id}/finish")
    def finish(req: Request, task_id: str, body: Dict[str, Any]):
        _check_token(req)
        result = TaskResult(
            task_id=task_id,
            node_id=body.get("node_id", "unknown"),
            status=body.get("status", "done"),
            output=body.get("output", ""),
            result=body.get("result", {}),
            error=body.get("error"),
        )
        state["store"].finish_task(task_id, result=result.to_dict(), error=result.error)
        return {"ok": True}

    @app.get("/v1/tasks")
    def list_tasks(req: Request, limit: int = 50):
        _check_token(req)
        return {"tasks": state["store"].list_tasks(limit=limit)}

    @app.get("/v1/tasks/{task_id}")
    def get_task(req: Request, task_id: str):
        _check_token(req)
        for t in state["store"].list_tasks(limit=200):
            if str(t.get("task_id")) == task_id:
                return {"task": t}
        raise HTTPException(404, "task not found")

    return app


def run(host: str = "0.0.0.0", port: int = 8787, cfg: Optional[ClusterConfig] = None) -> None:  # pragma: no cover - long-running
    cfg = cfg or ClusterConfig.load() or ClusterConfig()
    if not cfg.auth_token:
        raise SystemExit("cluster auth_token missing -- run `hermes cluster init` first")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    app = _build_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


def main(argv: Optional[list] = None) -> None:  # pragma: no cover - entry
    p = argparse.ArgumentParser(prog="hermes cluster core")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args(argv)
    run(host=args.host, port=args.port)
