# Scheduler

Independent, single-agent scheduler daemon. It has no in-process coupling to
an agent session: `lara scheduler run` owns its own process, polls SQLite, and
invokes `lara session run <scheduler-session> -m <prompt>` as a child process.

## Layers

- `store.py`: one SQLite file (`<lara_root>/scheduler/lara-scheduler.sqlite3`)
  with append-only signed `submissions` and daemon-owned `tasks` / `runs`.
  WAL, busy timeout, and lease-checked atomic claims are always enabled.
- `schedule.py`: deterministic `every`, `at`, `daily`, and `on` parsing and
  advancement.
- `keys.py`: per-source HMAC-SHA256 keys under the user Lara directory. The
  two-key format is intentionally replaceable by Ed25519 later.
- `daemon.py`: verification, promotion, `needs_review`/approval gates, due and
  missed execution, prompt-hash rechecks, audit runs, and failure alerts.
- `message.py`: builds the `<task-context>` execution message with the core
  Prompt API and task-specific instructions.
- `tools.py`: `schedule_task` / `emit_event`; they sign and append submissions,
  never write tasks directly.
- `../cli/scheduler.py`: `run`, `add`, `list`, `show`, approval/control,
  `init-keys`, and Windows autostart commands.

## Trust boundary

CLI and agent writers can only append `submissions`. The daemon verifies the
signature and claimed source, then alone writes `tasks` and `runs`. User
submissions default to `approved`; agent submissions default to
`needs_review` unless `scheduler.agent_create_enabled` is enabled.
