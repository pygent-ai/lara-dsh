"""Realtime conversation-exchange envelope formatting tests."""

from __future__ import annotations

from lara.runtime.realtime.envelope import (
    format_user_asked,
    render_exchange_envelope,
)


def test_merged_user_asked_format_is_numbered() -> None:
    envelope = render_exchange_envelope(
        user_asked=["first", "second"], response="answer"
    )
    assert "<user-asked>\n1. first\n2. second\n</user-asked>" in envelope
    assert "<your-response-in-another-session>\nanswer\n</your-response-in-another-session>" in envelope
    assert format_user_asked("single") == "single"
