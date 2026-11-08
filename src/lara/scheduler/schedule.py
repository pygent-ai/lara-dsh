"""Pure schedule parsing and advancement for the scheduler daemon.

Four syntaxes are supported: ``every <interval>``, ``at <ISO timestamp>``,
``daily HH:MM``, and ``on <event>``. All functions are deterministic for a
supplied ``now``; the daemon owns clock reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Literal

ScheduleKind = Literal["every", "at", "daily", "on"]

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$")
_ON_RE = re.compile(r"^on\s+(\S+)$")
_INTERVAL_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    raw: str
    kind: ScheduleKind
    every_seconds: int | None = None
    at: datetime | None = None
    daily_time: time | None = None
    event: str | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def parse_schedule(text: str) -> ScheduleSpec:
    raw = " ".join(str(text).split())
    if not raw:
        raise ValueError("schedule must be non-empty")
    match = _EVERY_RE.fullmatch(raw)
    if match:
        amount = int(match.group(1))
        if amount <= 0:
            raise ValueError("every interval must be positive")
        seconds = amount * _INTERVAL_SECONDS[match.group(2).lower()]
        return ScheduleSpec(raw=raw, kind="every", every_seconds=seconds)
    if raw.lower().startswith("at "):
        value = raw[3:].strip()
        if not value:
            raise ValueError("at schedule requires an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
        return ScheduleSpec(raw=raw, kind="at", at=_aware(parsed))
    match = _DAILY_RE.fullmatch(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("daily time must be HH:MM")
        return ScheduleSpec(raw=raw, kind="daily", daily_time=time(hour, minute))
    match = _ON_RE.fullmatch(raw)
    if match:
        return ScheduleSpec(raw=raw, kind="on", event=match.group(1))
    raise ValueError(
        "schedule must be 'every <n>[smhd]', 'at <ISO>', 'daily HH:MM', or 'on <event>'"
    )


def next_run_at(
    spec: ScheduleSpec, now: datetime, *, allow_past: bool = False
) -> datetime | None:
    """Return the next due instant, or ``None`` for event/expired schedules."""

    current = _aware(now)
    if spec.kind == "every":
        assert spec.every_seconds is not None
        return current + timedelta(seconds=spec.every_seconds)
    if spec.kind == "at":
        assert spec.at is not None
        if spec.at > current or allow_past:
            return spec.at
        return None
    if spec.kind == "daily":
        assert spec.daily_time is not None
        candidate = datetime.combine(
            current.date(), spec.daily_time, tzinfo=current.tzinfo
        )
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate
    return None


def is_due(next_run_at: datetime | None, now: datetime) -> bool:
    return next_run_at is not None and _aware(next_run_at) <= _aware(now)


def matches_event(spec: ScheduleSpec, event: str) -> bool:
    return spec.kind == "on" and spec.event == event
