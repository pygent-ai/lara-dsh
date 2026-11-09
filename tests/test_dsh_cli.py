"""Parser-level and gateway-mock tests for the ``lara dsh`` CLI.

The gateway is replaced by a fake, so no dsh runtime or model request is
started here; the real-case coverage lives in tests/test_dsh_gateway.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lara.dsh_gateway import DEFAULT_DSH_MODEL, DshGatewayConfig
from lara_shell.ops.cli import dsh as dsh_cli
from lara_shell.ops.cli.dsh import DEFAULT_DSH_HOME
from lara_shell.ops.cli.main import build_parser, main


class FakeGateway:
    """Stand-in for DshGateway that records calls without touching dsh."""

    instances: list["FakeGateway"] = []

    def __init__(self, config: DshGatewayConfig) -> None:
        self.config = config
        self.run_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []
        FakeGateway.instances.append(self)

    def __enter__(self) -> "FakeGateway":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        self.run_calls.append((prompt, session_id))
        return f"echo:{prompt}", session_id or "generated-session"

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        self.resume_calls.append((session_id, prompt))
        return f"resumed:{prompt}", session_id


@pytest.fixture(autouse=True)
def _reset_fake_instances() -> None:
    FakeGateway.instances = []


def _use_fake_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dsh_cli, "DshGateway", FakeGateway)


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_run_parses_prompt_and_options() -> None:
    args = _parse(
        ["dsh", "run", "hello world", "--session-id", "s1", "--home", "H", "--cwd", "C"]
    )
    assert args.prompt == "hello world"
    assert args.session_id == "s1"
    assert args.home == "H"
    assert args.cwd == "C"


def test_run_options_default_to_none() -> None:
    args = _parse(["dsh", "run", "hello"])
    assert args.session_id is None
    assert args.home is None
    assert args.cwd is None


def test_resume_parses_positional_session_and_prompt() -> None:
    args = _parse(["dsh", "resume", "s1", "continue the work"])
    assert args.session_id == "s1"
    assert args.prompt == "continue the work"
    assert args.home is None
    assert args.cwd is None


def test_run_requires_prompt() -> None:
    with pytest.raises(SystemExit):
        _parse(["dsh", "run"])


def test_resume_requires_session_and_prompt() -> None:
    with pytest.raises(SystemExit):
        _parse(["dsh", "resume", "s1"])


def test_run_forwards_prompt_and_env_config_to_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.setenv("DSH_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://test-endpoint/v1")

    exit_code = main(
        [
            "dsh",
            "run",
            "你好",
            "--session-id",
            "s1",
            "--home",
            str(tmp_path / "home"),
            "--cwd",
            str(tmp_path / "work"),
        ]
    )

    assert exit_code == 0
    (gateway,) = FakeGateway.instances
    config = gateway.config
    assert str(config.dsh_home) == str(tmp_path / "home")
    assert str(config.cwd) == str(tmp_path / "work")
    assert config.model == "test-model"
    assert config.api_key == "test-key"
    assert config.base_url == "http://test-endpoint/v1"
    assert gateway.run_calls == [("你好", "s1")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "s1"
    assert payload["response"] == "echo:你好"


def test_run_without_session_id_lets_gateway_generate_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch)

    exit_code = main(["dsh", "run", "hi"])

    assert exit_code == 0
    (gateway,) = FakeGateway.instances
    assert gateway.run_calls == [("hi", None)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "generated-session"


def test_run_defaults_home_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_fake_gateway(monkeypatch)
    monkeypatch.chdir(tmp_path)

    exit_code = main(["dsh", "run", "hi"])

    assert exit_code == 0
    config = FakeGateway.instances[0].config
    assert config.dsh_home == DEFAULT_DSH_HOME
    assert config.cwd == tmp_path


def test_resume_forwards_session_and_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch)
    monkeypatch.delenv("DSH_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)

    exit_code = main(
        [
            "dsh",
            "resume",
            "s1",
            "继续",
            "--home",
            str(tmp_path),
            "--cwd",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    (gateway,) = FakeGateway.instances
    assert gateway.resume_calls == [("s1", "继续")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "s1"
    assert payload["response"] == "resumed:继续"
    # from_env falls back to the gateway defaults when the env is absent.
    assert gateway.config.model == DEFAULT_DSH_MODEL
