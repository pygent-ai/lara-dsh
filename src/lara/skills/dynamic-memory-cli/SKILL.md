---
name: dynamic-memory-cli
description: "Per-session dynamic memory CLI for eternal conversation: search published memory records, inspect state, and manage entries journaled from session raw history."
---

# Dynamic Memory CLI

Single-file stdlib CLI mounted at `scripts/dynamic_memory_cli.py`. The eternal-conversation harness runs it as `"<python>" "<script>" --root "<session memory root>"` and publishes the exact search command in the `memory-access-instruction` envelope after the first memory publication. Do not invent a root or command: use the `memory_cli_command` from that envelope.

## Commands

- `search <keyword-or-key-phrase>` — keyword search over published memory records. Run when the Snapshot is insufficient or the task depends on prior decisions, constraints, commitments, preferences, or detailed history.
- `list [--full]` / `show <id>` — inspect published records.
- `add --file <file> [--force]` / `update <id> --file <file>` / `retire <id> [--reason]` — manage records (the harness's extractor fork publishes through these).
- `check-conflicts --file <file>` — conflict check before writing.
- `get-state` — formal memory state, including `covered_through`.
- `init --path <root>` — initialize the memory root (harness calls this before the first extraction).
