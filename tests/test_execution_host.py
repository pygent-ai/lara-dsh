"""Tests for lara_sdk.execution_host: speak-channel wiring.

Since pygent 0.6.3 a channel's ``deliver(text, attachments)`` receives the
resolved attachment paths as a separate argument; the channel is the surface's
own code, so only the wiring is asserted here.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pygent.agent.pygent_v1.runtime import SpeakChannel

from lara_sdk import execution_host as module
from lara_sdk.execution_host import LaraExecutionHost
from pygent.agent.pygent_v1.schema import RunConfig

CFG = cast(RunConfig, "cfg")  # the fake service only records what it is handed


class _Channel:
    """A SpeakChannel stand-in: records deliveries, delivers nothing."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, tuple[object, ...]]] = []

    def deliver(self, text: str, attachments: tuple[object, ...]) -> bool:
        self.deliveries.append((text, attachments))
        return True


def test_build_runtime_passes_channels_and_exchange_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_service(config: Any, **kwargs: Any) -> str:
        captured["config"] = config
        captured.update(kwargs)
        return "runtime"

    monkeypatch.setattr(module, "PygentRuntimeService", fake_service)
    channel = cast(SpeakChannel, _Channel())
    host = LaraExecutionHost(speak_channels=[channel])

    runtime = host._build_runtime(CFG, reminders=None, own_reminders=True)

    assert runtime == "runtime"
    assert captured["config"] == "cfg"
    assert captured["speak_channels"] == (channel,)
    assert captured["reminders"] is None and captured["own_reminders"] is True
    # The fork-reply exchange hook is still wired, as in LocalExecutionHost.
    hook = captured["on_exchange_ready"]
    assert hook.func == host._deliver_exchange and hook.args == ("cfg",)


def test_host_without_channels_matches_base_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(module, "PygentRuntimeService", lambda config, **kw: captured.update(kw))
    LaraExecutionHost()._build_runtime(CFG)
    assert captured["speak_channels"] == ()
