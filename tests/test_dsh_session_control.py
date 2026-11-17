"""Protocol tests for the lara-session-control loopback channel.

A fake newline-delimited JSON-RPC server stands in for the dsh plugin, so no
runtime boots here; the real plugin is exercised in tests/test_dsh_gateway.py.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from lara_shell.dsh_gateway import (
    SESSION_CONTROL_PATCH,
    SESSION_CONTROL_PLUGIN_DIR,
    DshSessionEntry,
    SessionControlChannel,
    SessionControlError,
)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.fixture
def fake_plugin() -> Iterator[tuple[int, list[dict[str, Any]], dict[str, Handler]]]:
    """Loopback server answering like index.js; returns (port, received, handlers)."""
    received: list[dict[str, Any]] = []
    handlers: dict[str, Handler] = {}
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn, conn.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                message = json.loads(line)
                received.append(message)
                handler = handlers.get(message["method"])
                if handler is None:
                    reply = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": f"unknown lara-session-control method: {message['method']}"},
                    }
                else:
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": handler(message.get("params") or {})}
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1], received, handlers
    listener.close()
    thread.join(timeout=5)


def test_call_round_trips_params_and_result(fake_plugin) -> None:
    port, received, handlers = fake_plugin
    handlers["lara/ping"] = lambda _p: {"ok": True, "name": "lara-session-control", "version": "0.1.0"}
    handlers["lara/session/stat"] = lambda p: {"session": None if p["sessionId"] == "nope" else {"sessionId": p["sessionId"], "createdAt": 1}}

    channel = SessionControlChannel(port)
    channel.connect(deadline_seconds=2)
    try:
        assert channel.call("lara/ping")["ok"] is True
        assert channel.call("lara/session/stat", {"sessionId": "nope"})["session"] is None
        found = channel.call("lara/session/stat", {"sessionId": "s1"})["session"]
        assert found["sessionId"] == "s1"
    finally:
        channel.close()

    assert [m["method"] for m in received] == ["lara/ping", "lara/session/stat", "lara/session/stat"]
    assert received[1]["params"] == {"sessionId": "nope"}
    assert "params" not in received[0]
    assert [m["id"] for m in received] == [1, 2, 3]


def test_unknown_method_raises_with_plugin_message(fake_plugin) -> None:
    port, _received, _handlers = fake_plugin
    channel = SessionControlChannel(port)
    channel.connect(deadline_seconds=2)
    try:
        with pytest.raises(SessionControlError, match="unknown lara-session-control method: lara/nope"):
            channel.call("lara/nope")
    finally:
        channel.close()


def test_connect_times_out_when_nothing_listens() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    channel = SessionControlChannel(port)
    with pytest.raises(SessionControlError, match="not listening"):
        channel.connect(deadline_seconds=0.3)


def test_call_before_connect_is_rejected() -> None:
    with pytest.raises(SessionControlError, match="not connected"):
        SessionControlChannel(1).call("lara/ping")


def test_session_entry_from_wire_maps_plugin_fields() -> None:
    entry = DshSessionEntry.from_wire(
        {
            "sessionId": "s1",
            "cwd": "E:/w",
            "createdAt": 1794801888831,
            "origin": "subagent",
            "parentSessionId": "root-1",
            "eventCount": 17,
            "title": "只回答：收到",
            "live": True,
            "status": "idle",
        }
    )
    assert entry == DshSessionEntry(
        session_id="s1",
        cwd="E:/w",
        created_at=1794801888831,
        origin="subagent",
        parent_session_id="root-1",
        event_count=17,
        title="只回答：收到",
        live=True,
        status="idle",
    )
    # A persisted-only session carries nulls for live-derived fields.
    sparse = DshSessionEntry.from_wire({"sessionId": "s2", "createdAt": 5, "cwd": None, "title": None, "live": False, "status": None, "eventCount": None, "parentSessionId": None})
    assert sparse.origin == "root"
    assert sparse.live is False
    assert sparse.status is None


def test_plugin_bundle_ships_with_the_package() -> None:
    assert SESSION_CONTROL_PATCH.is_file()
    assert (SESSION_CONTROL_PLUGIN_DIR / "index.js").is_file()
    manifest = json.loads((SESSION_CONTROL_PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert "lara-session-control" in SESSION_CONTROL_PATCH.read_text(encoding="utf-8")
