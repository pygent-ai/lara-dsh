"""Unit tests for scheduler schedule parsing and advancement."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lara.scheduler.schedule import (
    is_due,
    matches_event,
    next_run_at,
    parse_schedule,
)


def test_parse_every_units() -> None:
    assert parse_schedule("every 30m").every_seconds == 1800
    assert parse_schedule("every 90s").every_seconds == 90
    assert parse_schedule("every 2h").every_seconds == 7200
    assert parse_schedule("every 1d").every_seconds == 86400


def test_parse_at_daily_and_on() -> None:
    at = parse_schedule("at 2026-05-01T12:30:00+00:00")
    assert at.kind == "at"
    assert at.at == datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    daily = parse_schedule("daily 09:05")
    assert daily.kind == "daily"
    assert daily.daily_time is not None
    on = parse_schedule("on build.finished")
    assert on.kind == "on"
    assert on.event == "build.finished"
    assert matches_event(on, "build.finished")
    assert not matches_event(on, "build.started")


@pytest.mark.parametrize(
    "text",
    ["", "every 0m", "every soon", "at nope", "daily 24:00", "on", "cron * * * * *"],
)
def test_parse_rejects_invalid_schedules(text: str) -> None:
    with pytest.raises(ValueError):
        parse_schedule(text)


def test_next_run_at_is_deterministic() -> None:
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    every = parse_schedule("every 30m")
    assert next_run_at(every, now) == datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    daily = parse_schedule("daily 09:00")
    assert next_run_at(daily, now) == datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
    at = parse_schedule("at 2026-01-01T09:00:00+00:00")
    assert next_run_at(at, now) is None
    assert next_run_at(at, now, allow_past=True) == datetime(
        2026, 1, 1, 9, 0, tzinfo=timezone.utc
    )
    assert next_run_at(parse_schedule("on build"), now) is None
    assert is_due(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc), now)
    assert not is_due(None, now)
