"""Tests for lara_sdk.execution_host: speak-channel wiring and payload split."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from lara_sdk import execution_host as module
from lara_sdk.execution_host import LaraExecutionHost, split_speak_delivery
from pygent.agent.pygent_v1.schema import RunConfig

CFG = cast(RunConfig, "cfg")  # the fake service only records what it is handed


class _Channel:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def deliver(self, text: str) -> bool:
        self.texts.append(text)
        return True


def test_build_runtime_passes_channels_and_exchange_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_service(config: Any, **kwargs: Any) -> str:
        captured["config"] = config
        captured.update(kwargs)
        return "runtime"

    monkeypatch.setattr(module, "PygentRuntimeService", fake_service)
    channel = _Channel()
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


def test_split_recovers_trailing_attachment_paths(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "report.pdf").write_bytes(b"x")
    (tmp_path / "chart.png").write_bytes(b"x")

    text, attachments = split_speak_delivery("做好了，两个文件给你。\nchart.png\nout/report.pdf", tmp_path)
    assert text == "做好了，两个文件给你。"
    assert attachments == ["chart.png", "out/report.pdf"]  # original order preserved


def test_split_leaves_plain_text_alone(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_bytes(b"x")
    # No attachment lines at all.
    assert split_speak_delivery("只是一句话。", tmp_path) == ("只是一句话。", [])
    # Multi-line prose whose last line is not a file stays intact.
    assert split_speak_delivery("第一行\n第二行", tmp_path) == ("第一行\n第二行", [])
    # A path that does not exist is prose, not an attachment.
    assert split_speak_delivery("看看\nmissing.txt", tmp_path) == ("看看\nmissing.txt", [])
    # A mention with surrounding whitespace is prose; attachments are exact lines.
    assert split_speak_delivery("看看\n notes.md", tmp_path) == ("看看\n notes.md", [])
    # Absolute paths are never treated as workspace attachments.
    absolute = str(tmp_path / "notes.md")
    assert split_speak_delivery(f"看看\n{absolute}", tmp_path) == (f"看看\n{absolute}", [])


def test_split_attachment_only_delivery(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    # speak(text="", attachments=["a.txt"]) is delivered as just the path.
    assert split_speak_delivery("a.txt", tmp_path) == ("", ["a.txt"])
