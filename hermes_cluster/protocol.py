"""Cluster-wide message types and HTTP protocol."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NodeRegistration:
    node_id: str
    cluster_name: str
    base_url: str = ""
    role: str = "worker"
    capacity: int = 1
    labels: Dict[str, str] = field(default_factory=dict)
    auth_token: str = ""
    status: str = "online"
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    swap_percent: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskSpec:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cluster_name: str = "default"
    parent_task_id: Optional[str] = None
    created_by: str = "core"
    priority: int = 0
    prompt: str = ""
    workspace_root: str = ""
    git_remote: str = ""
    branch: str = ""
    toolsets: List[str] = field(default_factory=list)
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSpec":
        return cls(
            task_id=data.get("task_id") or str(uuid.uuid4()),
            cluster_name=data.get("cluster_name", "default"),
            parent_task_id=data.get("parent_task_id"),
            created_by=data.get("created_by", "core"),
            priority=int(data.get("priority", 0)),
            prompt=data.get("prompt", ""),
            workspace_root=data.get("workspace_root", ""),
            git_remote=data.get("git_remote", ""),
            branch=data.get("branch", ""),
            toolsets=list(data.get("toolsets") or []),
            session_id=data.get("session_id"),
            metadata=dict(data.get("metadata") or {}),
            enqueued_at=float(data.get("enqueued_at") or time.time()),
        )


@dataclass
class TaskResult:
    task_id: str
    node_id: str
    status: str = "done"
    output: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Auth header name used by the core HTTP server.  Single shared secret per
# cluster: the core issues a token via `hermes cluster init` and every node
# receives the same token from `hermes cluster join`.  Multi-cluster installs
# simply run multiple cores, one per cluster_name.
AUTH_HEADER = "X-Hermes-Cluster-Token"
