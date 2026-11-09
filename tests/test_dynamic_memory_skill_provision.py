"""Provisioning of the bundled dynamic-memory-cli skill into the user skills dir.

The bundled copy ships with the package (``lara.skills``) and is mounted on
first use of the eternal-conversation harness, so the harness's skill
directory resolution works without the removed git dependency.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Awaitable
from pathlib import Path

import pytest

from lara.runtime.eternal_conversation import (
    EternalConversationHarness,
    MemoryJobRequest,
)
from lara.schema import EternalConversationConfig
from lara.skills.provision import (
    MARKER_NAME,
    bundled_skill_dir,
    ensure_dynamic_memory_skill,
)


async def _fail_runner(request: MemoryJobRequest) -> str:
    raise AssertionError("memory job must not run during script resolution")


def _harness(skills_root: Path) -> EternalConversationHarness:
    return EternalConversationHarness(
        EternalConversationConfig(enabled=True),
        run_memory_job=_fail_runner,
        user_skills_root=skills_root,
    )


def _session_dir(tmp_path: Path) -> Path:
    return tmp_path / "lara" / "sessions" / "s1"


def _mounted_paths(skills_root: Path) -> tuple[Path, Path]:
    target = skills_root / "dynamic-memory-cli"
    return target / "scripts" / "dynamic_memory_cli.py", target / "SKILL.md"


def test_provision_mounts_bundled_skill_and_script_resolves(tmp_path: Path) -> None:
    skills_root = tmp_path / "home" / ".lara" / "skills"
    harness = _harness(skills_root)

    script = harness._script(_session_dir(tmp_path))

    mounted_script, mounted_md = _mounted_paths(skills_root)
    assert script == mounted_script
    assert mounted_script.read_bytes() == (
        bundled_skill_dir() / "scripts" / "dynamic_memory_cli.py"
    ).read_bytes()
    assert mounted_md.is_file()
    assert (mounted_script.parent.parent / MARKER_NAME).is_file()

    # The mounted script is runnable standalone.
    completed = subprocess.run(
        [sys.executable, str(mounted_script), "--help"],
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0


def test_provision_is_idempotent(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    ensure_dynamic_memory_skill(skills_root)
    mounted_script, _ = _mounted_paths(skills_root)
    first = mounted_script.read_bytes()

    ensure_dynamic_memory_skill(skills_root)

    assert mounted_script.read_bytes() == first


def test_provision_upgrades_owned_copy(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    ensure_dynamic_memory_skill(skills_root)
    mounted_script, mounted_md = _mounted_paths(skills_root)
    script_bytes = b"outdated script"
    md_bytes = b"outdated skill doc"
    mounted_script.write_bytes(script_bytes)
    mounted_md.write_bytes(md_bytes)
    # Marker as a previous provision of this older bundle would have left it.
    marker = {
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "skill_md_sha256": hashlib.sha256(md_bytes).hexdigest(),
        "provisioned_at": "2026-01-01T00:00:00+00:00",
    }
    (mounted_script.parent.parent / MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )

    ensure_dynamic_memory_skill(skills_root)

    bundled = bundled_skill_dir()
    assert mounted_script.read_bytes() == (
        bundled / "scripts" / "dynamic_memory_cli.py"
    ).read_bytes()
    assert mounted_md.read_bytes() == (bundled / "SKILL.md").read_bytes()


def test_provision_preserves_user_modified_copy(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    ensure_dynamic_memory_skill(skills_root)
    mounted_script, mounted_md = _mounted_paths(skills_root)
    mounted_script.write_bytes(b"user customization")
    mounted_md.write_bytes(b"user doc")

    with pytest.warns(RuntimeWarning, match="user-modified"):
        ensure_dynamic_memory_skill(skills_root)

    assert mounted_script.read_bytes() == b"user customization"
    assert mounted_md.read_bytes() == b"user doc"


def test_explicit_config_path_beats_provisioned_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    explicit = tmp_path / "cli" / "dynamic_memory_cli.py"
    explicit.parent.mkdir(parents=True)
    explicit.write_bytes(b"# explicit cli path\n")
    harness = EternalConversationHarness(
        EternalConversationConfig(
            enabled=True, dynamic_memory_cli_path=str(explicit)
        ),
        run_memory_job=_fail_runner,
        user_skills_root=skills_root,
    )

    assert harness._script(_session_dir(tmp_path)) == explicit
    # Resolution never reached the skills directories, so nothing was mounted.
    assert not (skills_root / "dynamic-memory-cli").exists()


def test_project_skills_dir_still_resolves_first(tmp_path: Path) -> None:
    skills_root = tmp_path / "home" / ".lara" / "skills"
    session_dir = _session_dir(tmp_path)
    project_script = (
        session_dir.parents[1]
        / "skills"
        / "dynamic-memory-cli"
        / "scripts"
        / "dynamic_memory_cli.py"
    )
    project_script.parent.mkdir(parents=True)
    project_script.write_bytes(b"# project-local copy\n")
    harness = _harness(skills_root)

    assert harness._script(session_dir) == project_script


def test_provisioned_skill_is_discoverable_as_skill(tmp_path: Path) -> None:
    from lara.runtime.agent.skill_catalog import scan_multilevel_skills

    skills_root = tmp_path / "skills"
    ensure_dynamic_memory_skill(skills_root)

    skills = scan_multilevel_skills(
        user_skills_dir=skills_root, project_skills_dir=tmp_path / "project-skills"
    )
    assert [skill["name"] for skill in skills] == ["dynamic-memory-cli"]
