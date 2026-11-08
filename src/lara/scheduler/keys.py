"""Per-source HMAC keys for scheduler submissions.

The initial implementation uses HMAC-SHA256 with one secret per submission
source (``user`` and ``agent``). This is local-only, zero-dependency
authentication: secrets live under the user's Lara directory and are never
written to the scheduler database or workspace. The format is intentionally
small so it can be replaced by Ed25519 signatures later without changing the
submission envelope or store schema.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Mapping

SOURCES = ("user", "agent")


def scheduler_keys_dir(user_lara_root: str | Path) -> Path:
    return Path(user_lara_root) / "scheduler" / "keys"


class SchedulerKeysMissing(RuntimeError):
    """Raised when a scheduler operation needs keys that were not initialized."""


def key_path(root: str | Path, source: str) -> Path:
    if source not in SOURCES:
        raise ValueError(f"unknown scheduler source: {source!r}")
    return Path(root) / f"{source}.key"


def init_keys(root: str | Path, *, force: bool = False) -> dict[str, Path]:
    """Create two source keys; existing keys are preserved unless forced."""

    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    created: dict[str, Path] = {}
    for source in SOURCES:
        path = key_path(root_path, source)
        if path.exists() and not force:
            created[source] = path
            continue
        path.write_text(os.urandom(32).hex() + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        created[source] = path
    return created


def load_keys(root: str | Path) -> dict[str, bytes]:
    root_path = Path(root)
    keys: dict[str, bytes] = {}
    missing: list[str] = []
    for source in SOURCES:
        path = key_path(root_path, source)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            missing.append(source)
            continue
        if not value:
            missing.append(source)
            continue
        try:
            keys[source] = bytes.fromhex(value)
        except ValueError as exc:
            raise SchedulerKeysMissing(f"invalid scheduler key: {path}") from exc
    if missing:
        raise SchedulerKeysMissing(
            "scheduler keys are not initialized; run `lara scheduler init-keys`"
        )
    return keys


def canonical_payload(kind: str, source: str, payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {"kind": kind, "source": source, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign(
    keys: Mapping[str, bytes],
    *,
    kind: str,
    source: str,
    payload: Mapping[str, Any],
) -> str:
    secret = keys.get(source)
    if secret is None:
        raise SchedulerKeysMissing(f"scheduler key for source {source!r} is missing")
    return hmac.new(
        secret, canonical_payload(kind, source, payload), hashlib.sha256
    ).hexdigest()


def verify(
    keys: Mapping[str, bytes],
    *,
    kind: str,
    source: str,
    payload: Mapping[str, Any],
    signature: str,
) -> bool:
    expected = sign(keys, kind=kind, source=source, payload=payload)
    return hmac.compare_digest(expected, signature)


def identify_source(
    keys: Mapping[str, bytes],
    *,
    kind: str,
    payload: Mapping[str, Any],
    signature: str,
) -> str | None:
    """Return the key owner that signed the payload, never the claimed source."""

    for source in SOURCES:
        if source in keys and verify(
            keys,
            kind=kind,
            source=source,
            payload=payload,
            signature=signature,
        ):
            return source
    return None
