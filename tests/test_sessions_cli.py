"""Parser-level tests for the ``lara session`` CLI."""

from __future__ import annotations

from lara_shell.ops.cli.main import build_parser


def test_run_accepts_repeatable_attachments() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "session",
            "run",
            "session-1",
            "-m",
            "hi",
            "-a",
            "notes.txt",
            "--attachment",
            "docs/spec.md",
        ]
    )
    assert args.attachment == ["notes.txt", "docs/spec.md"]


def test_run_attachments_default_empty() -> None:
    parser = build_parser()
    args = parser.parse_args(["session", "run", "--new", "-m", "hi"])
    assert args.attachment is None


def test_chat_accepts_attachments() -> None:
    parser = build_parser()
    args = parser.parse_args(["session", "chat", "--new", "-a", "notes.txt"])
    assert args.attachment == ["notes.txt"]
