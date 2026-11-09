"""Provision the bundled dynamic-memory-cli skill into the user skills dir.

The bundled skill lives next to this module at
``dynamic-memory-cli/`` (``SKILL.md`` + ``scripts/dynamic_memory_cli.py``);
its canonical source is pygent-ai/memory-cli branch
``codex/dynamic-memory-cli`` at commit
``c2b500673b0c9da45bc343f7c3ff252ef7e035ec`` — the same revision the removed
git dependency was locked to in ``uv.lock``.

:func:`ensure_dynamic_memory_skill` copies the bundle to
``<user_skills_root>/dynamic-memory-cli/`` so that the eternal-conversation
harness's skill-directory resolution and the skill catalog scan both find it.
A ``.provisioned.json`` marker records the hashes of the mounted files: an
existing copy is upgraded to the bundled version only when its content still
matches the marker (i.e. the user has not touched it); a user-modified copy
is left untouched and reported through ``RuntimeWarning``.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import dataclass
from pathlib import Path

from lara.core.io import utc_now

SKILL_NAME = "dynamic-memory-cli"
MARKER_NAME = ".provisioned.json"
_SCRIPT_RELPATH = Path("scripts") / "dynamic_memory_cli.py"


@dataclass(frozen=True, slots=True)
class _BundledSkill:
    script: bytes
    skill_md: bytes
    script_sha256: str
    skill_md_sha256: str


def bundled_skill_dir() -> Path:
    return Path(__file__).resolve().parent / SKILL_NAME


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_bundle() -> _BundledSkill:
    root = bundled_skill_dir()
    script = (root / _SCRIPT_RELPATH).read_bytes()
    skill_md = (root / "SKILL.md").read_bytes()
    return _BundledSkill(
        script=script,
        skill_md=skill_md,
        script_sha256=_hash_bytes(script),
        skill_md_sha256=_hash_bytes(skill_md),
    )


def _read_marker(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(path: Path, bundle: _BundledSkill) -> None:
    payload = {
        "script_sha256": bundle.script_sha256,
        "skill_md_sha256": bundle.skill_md_sha256,
        "provisioned_at": utc_now(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _install(target: Path, bundle: _BundledSkill) -> None:
    (target / _SCRIPT_RELPATH).parent.mkdir(parents=True, exist_ok=True)
    (target / _SCRIPT_RELPATH).write_bytes(bundle.script)
    (target / "SKILL.md").write_bytes(bundle.skill_md)
    _write_marker(target / MARKER_NAME, bundle)


def ensure_dynamic_memory_skill(user_skills_root: Path) -> None:
    """Mount the bundled skill under ``user_skills_root``; idempotent.

    First mount copies the bundle wholesale. Later calls no-op while the
    mounted copy equals the bundled version, upgrade it when it still matches
    the ``.provisioned.json`` marker, and warn without touching it when the
    user has modified it.
    """
    bundle = _load_bundle()
    target = Path(user_skills_root).expanduser() / SKILL_NAME
    script_path = target / _SCRIPT_RELPATH
    skill_md_path = target / "SKILL.md"

    current_script = script_path.read_bytes() if script_path.is_file() else None
    current_md = skill_md_path.read_bytes() if skill_md_path.is_file() else None
    if current_script is None:
        _install(target, bundle)
        return

    script_is_bundled = _hash_bytes(current_script) == bundle.script_sha256
    md_is_bundled = current_md is not None and (
        _hash_bytes(current_md) == bundle.skill_md_sha256
    )
    if script_is_bundled and md_is_bundled:
        # Content already current; only the marker may need self-healing.
        marker = _read_marker(target / MARKER_NAME)
        if (
            marker.get("script_sha256") != bundle.script_sha256
            or marker.get("skill_md_sha256") != bundle.skill_md_sha256
        ):
            _write_marker(target / MARKER_NAME, bundle)
        return

    marker = _read_marker(target / MARKER_NAME)
    owned = current_md is not None and (
        marker.get("script_sha256") == _hash_bytes(current_script)
        and marker.get("skill_md_sha256") == _hash_bytes(current_md)
    )
    if not owned:
        warnings.warn(
            f"user-modified {SKILL_NAME} skill at {target} was left untouched; "
            "the bundled version was not upgraded",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    _install(target, bundle)
