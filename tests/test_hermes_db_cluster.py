import os

from hermes_cluster.protocol import TaskResult, TaskSpec
from hermes_db.config import DatabaseConfig, reset_cache, get_database_config


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
