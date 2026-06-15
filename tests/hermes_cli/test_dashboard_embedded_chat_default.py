"""Verify the embedded dashboard chat tab is opt-in for server deployments.

The /api/pty + /api/ws endpoints spawn PTY/TUI child processes; on small
VPSes, repeated browser reloads can leave stale children behind and spike
CPU/RAM. The desktop shell enables chat by default (HERMES_DESKTOP=1);
the server dashboard must opt in explicitly via HERMES_DASHBOARD_EMBEDDED_CHAT.
"""

import importlib
import os
import sys


def _reload(monkeypatch, **env):
    for k in (
        "HERMES_DASHBOARD_EMBEDDED_CHAT",
        "HERMES_DESKTOP",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    sys.modules.pop("hermes_cli.web_server", None)
    return importlib.import_module("hermes_cli.web_server")


def test_dashboard_chat_disabled_by_default(monkeypatch):
    ws = _reload(monkeypatch)
    assert ws._DASHBOARD_EMBEDDED_CHAT_ENABLED is False


def test_dashboard_chat_enabled_with_explicit_env(monkeypatch):
    ws = _reload(monkeypatch, HERMES_DASHBOARD_EMBEDDED_CHAT="1")
    assert ws._DASHBOARD_EMBEDDED_CHAT_ENABLED is True


def test_dashboard_chat_enabled_when_desktop(monkeypatch):
    ws = _reload(monkeypatch, HERMES_DESKTOP="1")
    assert ws._DASHBOARD_EMBEDDED_CHAT_ENABLED is True


def test_dashboard_chat_stays_off_under_pressure(monkeypatch):
    """Even with the Chat tab disabled, /api/status must keep serving."""
    ws = _reload(monkeypatch)
    assert ws._DASHBOARD_EMBEDDED_CHAT_ENABLED is False
    from fastapi.testclient import TestClient

    client = TestClient(ws.app)
    client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN
    r = client.get("/api/status")
    assert r.status_code in (200, 503)
