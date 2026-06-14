"""Cluster token + node identity storage.

No task/session/history/memory data is stored here.  This module only keeps the
local node bootstrap config in YAML, same class of file as config.yaml.  All
cluster runtime state lives in CockroachDB.
"""

from __future__ import annotations

import os
import platform
import socket
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home


def _cluster_dir() -> Path:
    return get_hermes_home() / "cluster"


@dataclass
class ClusterConfig:
    cluster_name: str = "default"
    core_url: str = "http://127.0.0.1:8787"
    auth_token: str = ""
    node_id: str = ""
    role: str = "worker"  # "core", "node", or "both"

    @classmethod
    def load(cls) -> Optional["ClusterConfig"]:
        path = _cluster_dir() / "config.yaml"
        if not path.exists():
            return None
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        return cls(**{k: data.get(k, v) for k, v in cls().__dict__.items()})

    def save(self) -> None:
        d = _cluster_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "config.yaml"
        import yaml

        # Best-effort 0600 on POSIX; on Windows we just write.
        path.write_text(yaml.safe_dump(asdict(self), sort_keys=False), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass


def generate_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def default_node_id() -> str:
    return f"{platform.node()}-{uuid.uuid4().hex[:8]}"


def local_ip() -> str:
    """Best-effort non-loopback IPv4 for advertising a node to the core."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
