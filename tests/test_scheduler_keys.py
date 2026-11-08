"""Unit tests for scheduler submission signing and source attribution."""

from __future__ import annotations

from pathlib import Path

import pytest

from lara.scheduler.keys import (
    SchedulerKeysMissing,
    identify_source,
    init_keys,
    load_keys,
    sign,
    verify,
)


def test_keys_round_trip_and_source_attribution(tmp_path: Path) -> None:
    paths = init_keys(tmp_path)
    assert set(paths) == {"user", "agent"}
    keys = load_keys(tmp_path)
    payload = {"name": "daily", "schedule": "daily 09:00"}
    user_signature = sign(keys, kind="task", source="user", payload=payload)
    agent_signature = sign(keys, kind="task", source="agent", payload=payload)

    assert verify(
        keys, kind="task", source="user", payload=payload, signature=user_signature
    )
    assert identify_source(
        keys, kind="task", payload=payload, signature=agent_signature
    ) == "agent"
    assert not verify(
        keys, kind="task", source="agent", payload=payload, signature=user_signature
    )


def test_tampering_and_forged_source_are_rejected(tmp_path: Path) -> None:
    init_keys(tmp_path)
    keys = load_keys(tmp_path)
    payload = {"name": "daily", "schedule": "daily 09:00"}
    signature = sign(keys, kind="task", source="user", payload=payload)

    assert not verify(
        keys,
        kind="task",
        source="user",
        payload={**payload, "prompt": "tampered"},
        signature=signature,
    )
    assert identify_source(
        keys,
        kind="task",
        payload=payload,
        signature=signature,
    ) == "user"
    assert identify_source(
        keys,
        kind="task",
        payload=payload,
        signature="not-a-signature",
    ) is None


def test_keys_must_be_initialized(tmp_path: Path) -> None:
    with pytest.raises(SchedulerKeysMissing):
        load_keys(tmp_path)


def test_init_keys_preserves_existing_keys(tmp_path: Path) -> None:
    init_keys(tmp_path)
    before = load_keys(tmp_path)
    init_keys(tmp_path)
    assert load_keys(tmp_path) == before
