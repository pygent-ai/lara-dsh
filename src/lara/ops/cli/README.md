# Ops CLI

Thin command-line adapter over Lara application services. This package is the
human/operations entry layer: it may depend on the agent core and runtime
hosting, but neither may import `lara.ops`.

- `main.py`: root parser, command dispatch, JSON rendering, and CLI errors.
- `sessions.py`: session creation/inspection, one-shot turns, interactive chat, and collaboration commands.
- `credentials.py`: credential subcommands.
- `projects.py`: project inspection commands.
- `scheduler.py`: scheduler daemon, submissions, approvals, and autostart commands.

Business logic belongs in the corresponding feature package, not in command handlers.
