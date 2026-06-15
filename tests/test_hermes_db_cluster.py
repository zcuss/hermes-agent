import os

from fastapi.testclient import TestClient

from hermes_cluster.core import _build_app
from hermes_cluster.protocol import TaskResult, TaskSpec
from hermes_cluster.state import ClusterConfig
from hermes_db.config import DatabaseConfig, reset_cache, get_database_config
from hermes_db.pool import close_store


def test_database_config_env_url_selects_cockroach(monkeypatch):
    monkeypatch.setenv("HERMES_DATABASE_URL", "postgresql://root@localhost:26257/hermes?sslmode=disable")
    monkeypatch.delenv("HERMES_DATABASE_BACKEND", raising=False)
    reset_cache()
    cfg = get_database_config(force_reload=True)
    assert cfg.backend == "cockroach"
    assert cfg.is_cockroach
    assert cfg.url.startswith("postgresql://")


def test_database_config_backend_env_overrides(monkeypatch):
    monkeypatch.setenv("HERMES_DATABASE_BACKEND", "sqlite")
    monkeypatch.delenv("HERMES_DATABASE_URL", raising=False)
    reset_cache()
    cfg = get_database_config(force_reload=True)
    assert cfg.backend == "sqlite"
    assert cfg.is_sqlite


def test_task_spec_roundtrip_defaults():
    spec = TaskSpec.from_dict({"prompt": "do work", "toolsets": ["terminal"], "priority": 3})
    assert spec.prompt == "do work"
    assert spec.priority == 3
    assert spec.toolsets == ["terminal"]
    assert spec.task_id
    assert TaskSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()


def test_task_result_roundtrip():
    result = TaskResult(task_id="t1", node_id="n1", output="ok", result={"returncode": 0})
    data = result.to_dict()
    assert data["status"] == "done"
    assert data["result"]["returncode"] == 0


def test_cluster_core_full_lifecycle(monkeypatch):
    monkeypatch.setenv(
        "HERMES_DATABASE_URL",
        os.environ.get(
            "HERMES_TEST_DATABASE_URL",
            "postgresql://root@127.0.0.1:26257/hermes_test?sslmode=disable",
        ),
    )
    monkeypatch.setenv("HERMES_CLUSTER", f"cluster-core-test-{os.urandom(4).hex()}")
    monkeypatch.setenv("HERMES_PROFILE", "pytest")
    reset_cache()
    close_store()
    cfg = ClusterConfig(cluster_name=os.environ["HERMES_CLUSTER"], auth_token="tok", core_url="http://test")
    app = _build_app(cfg)
    headers = {"X-Hermes-Cluster-Token": "tok"}
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/health").json()["ok"] is True
            assert client.get("/v1/nodes").status_code == 401

            r = client.post("/v1/nodes/register", headers=headers, json={"node_id": "node-a", "capacity": 1, "labels": {"role": "worker"}})
            assert r.status_code == 200 and r.json()["ok"] is True
            assert any(n["node_id"] == "node-a" for n in client.get("/v1/nodes", headers=headers).json()["nodes"])

            r = client.post(
                "/v1/tasks",
                headers=headers,
                json={"prompt": "say ok", "created_by": "pytest", "toolsets": ["terminal"], "priority": 3},
            )
            assert r.status_code == 200
            task_id = r.json()["task_id"]

            r = client.post("/v1/tasks/claim", headers=headers, json={"node_id": "node-a"})
            assert r.status_code == 200
            claimed = r.json()["task"]
            assert claimed is not None
            assert str(claimed["task_id"]) == task_id
            assert claimed["status"] == "running"
            assert claimed["assigned_node"] == "node-a"

            r = client.post(
                f"/v1/tasks/{task_id}/finish",
                headers=headers,
                json={"node_id": "node-a", "status": "done", "output": "ok", "result": {"returncode": 0}},
            )
            assert r.status_code == 200 and r.json()["ok"] is True

            r = client.get(f"/v1/tasks/{task_id}", headers=headers)
            assert r.status_code == 200
            task = r.json()["task"]
            assert task["status"] == "done"
            assert task["result"]["result"]["returncode"] == 0
    finally:
        close_store()
        reset_cache()
