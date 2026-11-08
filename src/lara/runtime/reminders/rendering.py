from __future__ import annotations

from datetime import datetime

from lara.core.prompt import Prompt

from .models import ReminderSection


def render_context_body(
    sections: list[ReminderSection],
    *,
    rendered_at: datetime | None = None,
) -> str | None:
    active = sorted(
        (section for section in sections if section.lines),
        key=lambda item: (item.order, item.source),
    )
    if not active:
        return None
    timestamp = rendered_at or datetime.now().astimezone()
    lines: list[str] = []
    if any(section.include_time for section in active):
        time_block = Prompt(
            f"  Current system time: {timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        ).wrap("time")
        lines.extend([*str(time_block).splitlines(), ""])
    for index, section in enumerate(active):
        if index:
            lines.append("")
        lines.extend(section.lines)
    return "\n".join(lines)
