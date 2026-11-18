"""Parser-level and gateway-mock tests for the ``lara dsh`` CLI.

The gateway is replaced by a fake, so no dsh runtime or model request is
started here; the real-case coverage lives in tests/test_dsh_gateway.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lara_shell.dsh_gateway import DEFAULT_DSH_MODEL, DshGatewayConfig, DshSessionEntry
from lara_shell.ops.cli import dsh as dsh_cli
from lara_shell.ops.cli.main import build_parser, main


class FakeGateway:
    """Stand-in for the gateway `open_gateway` returns; records calls without touching dsh."""

    instances: list["FakeGateway"] = []
    inventory: list[DshSessionEntry] = []
    mode = "fake"

    def __init__(self, config: DshGatewayConfig) -> None:
        self.config = config
        self.run_calls: list[tuple[str, str | None]] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.stat_calls: list[str] = []
        self.entries: list[DshSessionEntry] = list(FakeGateway.inventory)
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

    def list_sessions(self) -> list[DshSessionEntry]:
        return list(self.entries)

    def stat_session(self, session_id: str) -> DshSessionEntry | None:
        self.stat_calls.append(session_id)
        return next((e for e in self.entries if e.session_id == session_id), None)


@pytest.fixture(autouse=True)
def _reset_fake_instances() -> None:
    FakeGateway.instances = []
    FakeGateway.inventory = []


def _use_fake_gateway(monkeypatch: pytest.MonkeyPatch, user_root: Path) -> None:
    """Fake gateway, an empty user root (so no dsh.yaml and no daemon), no DEEPSEEK env."""
    monkeypatch.setattr(dsh_cli, "open_gateway", FakeGateway)
    monkeypatch.setattr(dsh_cli, "user_lara_root", lambda: user_root)
    for name in ("DSH_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DSH_HOME", str(user_root.parent))  # Lara root sits inside the dsh home


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


def test_sessions_parses_home_and_cwd() -> None:
    args = _parse(["dsh", "sessions", "--home", "H", "--cwd", "C"])
    assert args.home == "H"
    assert args.cwd == "C"


def _entry(session_id: str, **overrides: object) -> DshSessionEntry:
    values: dict[str, object] = {
        "session_id": session_id,
        "cwd": "C:/w",
        "created_at": 100,
        "origin": "root",
        "parent_session_id": None,
        "event_count": 3,
        "title": None,
        "live": False,
        "status": None,
    }
    values.update(overrides)
    return DshSessionEntry(**values)  # type: ignore[arg-type]


def test_sessions_renders_runtime_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")
    FakeGateway.inventory = [
        _entry("s-live", created_at=222, title="新会话", live=True, status="running"),
        _entry("s-old", created_at=111, title="旧会话"),
        _entry("s-child", created_at=50, origin="subagent", parent_session_id="s-old"),
    ]

    exit_code = main(["dsh", "sessions", "--home", str(tmp_path / "home")])

    assert exit_code == 0
    (gateway,) = FakeGateway.instances
    assert str(gateway.config.dsh_home) == str(tmp_path / "home")
    payload = json.loads(capsys.readouterr().out)
    assert payload["home"] == str(tmp_path / "home")
    rows = {item["session_id"]: item for item in payload["sessions"]}
    assert list(rows) == ["s-live", "s-old", "s-child"]  # gateway order preserved
    assert rows["s-live"]["live"] is True
    assert rows["s-live"]["status"] == "running"
    assert rows["s-live"]["title"] == "新会话"
    assert rows["s-old"]["live"] is False
    assert rows["s-old"]["status"] is None
    assert rows["s-old"]["resumable"] is True
    assert rows["s-child"]["resumable"] is False  # subagent sessions cannot be resumed
    assert rows["s-child"]["parent_session_id"] == "s-old"
    assert rows["s-child"]["title"] == ""


def test_sessions_empty_runtime_renders_empty_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")

    exit_code = main(["dsh", "sessions", "--home", str(tmp_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"] == []


def test_status_reports_one_session_or_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")
    FakeGateway.inventory = [_entry("s1", live=True, status="idle", event_count=17)]

    assert main(["dsh", "status", "s1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["session_id"] == "s1"
    assert payload["session"]["status"] == "idle"
    assert payload["session"]["event_count"] == 17
    assert FakeGateway.instances[-1].stat_calls == ["s1"]

    assert main(["dsh", "status", "missing"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"] is None


def test_status_requires_session_id() -> None:
    with pytest.raises(SystemExit):
        _parse(["dsh", "status"])


def test_run_requires_prompt() -> None:
    with pytest.raises(SystemExit):
        _parse(["dsh", "run"])


def test_resume_requires_session_and_prompt() -> None:
    with pytest.raises(SystemExit):
        _parse(["dsh", "resume", "s1"])


def test_run_forwards_prompt_and_env_config_to_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")
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
    # dsh resolves its own key from its credential store; the CLI never injects one.
    assert config.api_key is None
    assert config.base_url == "http://test-endpoint/v1"
    assert gateway.run_calls == [("你好", "s1")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "s1"
    assert payload["response"] == "echo:你好"


def test_run_without_session_id_lets_gateway_generate_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")

    exit_code = main(["dsh", "run", "hi"])

    assert exit_code == 0
    (gateway,) = FakeGateway.instances
    assert gateway.run_calls == [("hi", None)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "generated-session"


def test_run_defaults_home_and_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")
    monkeypatch.chdir(tmp_path)

    exit_code = main(["dsh", "run", "hi"])

    assert exit_code == 0
    config = FakeGateway.instances[0].config
    assert config.dsh_home == (tmp_path / "root").parent.resolve()  # dsh's own home, shared with its UIs
    assert config.cwd == tmp_path


def test_resume_forwards_session_and_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")

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


def test_config_shows_and_changes_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_fake_gateway(monkeypatch, tmp_path / "root")

    assert main(["dsh", "config"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"] == {}
    assert payload["effective"]["model"] == DEFAULT_DSH_MODEL
    assert payload["effective"]["provider"] == "deepseek-official"
    assert Path(payload["effective"]["home"]) == tmp_path.resolve()
    assert payload["daemon_running"] is False

    assert main(["dsh", "config", "model=deepseek-v4-pro", "workspace=E:/work"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == ["model", "workspace"]
    assert payload["settings"] == {"model": "deepseek-v4-pro", "workspace": "E:/work"}
    assert payload["effective"]["model"] == "deepseek-v4-pro"
    assert (tmp_path / "root" / "dsh.yaml").is_file()

    assert main(["dsh", "config", "model="]) == 0
    assert json.loads(capsys.readouterr().out)["settings"] == {"workspace": "E:/work"}
    assert main(["dsh", "config", "bogus"]) == 2  # KEY=VALUE required
    assert main(["dsh", "config", "connection=az"]) == 2  # retired key is refused
