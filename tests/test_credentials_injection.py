"""Credential lookup protocol: shell injection vs. kernel environment fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from lara.runtime.model_configuration import CredentialEnvironment
from lara_shell.credentials import build_credential_environment
from lara_shell.credentials.store import (
    user_credentials_path,
    write_env_entries,
)


def test_shell_environment_resolves_user_file_before_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    write_env_entries(
        user_credentials_path(user_root),
        {"LARA_TEST_KEY": "from-user-file", "FILE_ONLY": "file-only"},
    )
    monkeypatch.setenv("LARA_TEST_KEY", "from-process-env")
    monkeypatch.delenv("FILE_ONLY", raising=False)

    environ = build_credential_environment(user_root)

    assert environ["LARA_TEST_KEY"] == "from-user-file"
    assert environ["FILE_ONLY"] == "file-only"
    assert "FILE_ONLY" in set(iter(environ))


def test_shell_environment_falls_back_to_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    monkeypatch.setenv("LARA_ENV_ONLY", "from-process-env")

    environ = build_credential_environment(user_root)

    assert environ["LARA_ENV_ONLY"] == "from-process-env"


def test_kernel_default_lookup_ignores_shell_credential_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    write_env_entries(
        user_credentials_path(user_root),
        {"FILE_ONLY": "file-only"},
    )
    monkeypatch.delenv("FILE_ONLY", raising=False)

    kernel_environ = CredentialEnvironment(user_root)

    # The kernel default must not reach into shell-owned credential storage.
    with pytest.raises(KeyError):
        kernel_environ["FILE_ONLY"]
