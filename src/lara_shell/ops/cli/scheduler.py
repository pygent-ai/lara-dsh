"""CLI adapter for the independent Lara scheduler daemon."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from functools import partial
from typing import Any
from xml.sax.saxutils import escape

from lara_shell.ops.cli._base import config_from_args
from lara_shell.config import load_run_config
from pygent.agent.pygent_v1.scheduler.daemon import SchedulerDaemon
from pygent.agent.pygent_v1.scheduler.keys import init_keys, load_secret, sign
from pygent.agent.pygent_v1.scheduler.schedule import parse_schedule
from pygent.agent.pygent_v1.scheduler.store import (
    SchedulerStore,
    scheduler_db_path,
    scheduler_keys_dir,
    submission_payload,
)
from pygent.agent.pygent_v1.schema import RunConfig

AUTOSTART_TASK_NAME = "LaraScheduler"


def register_scheduler_parser(subparsers: Any) -> None:
    scheduler = subparsers.add_parser(
        "scheduler", help="Manage the independent Lara scheduler daemon"
    )
    commands = scheduler.add_subparsers(dest="scheduler_command", required=True)

    run = commands.add_parser("run", help="Run the scheduler daemon")
    run.add_argument("--home", default=None, help="dsh home containing Lara's state")
    run.add_argument(
        "--once",
        action="store_true",
        help="Process one poll cycle and exit (useful for diagnostics).",
    )
    run.set_defaults(handler=_scheduler_run)

    add = commands.add_parser("add", help="Submit a signed task")
    add.add_argument("--name", required=True)
    add.add_argument("--schedule", required=True)
    add.add_argument("--prompt", required=True)
    add.add_argument("--instructions", default="")
    add.add_argument("--session", default=None)
    add.add_argument("--priority", choices=["high", "normal", "low"], default="normal")
    add.add_argument(
        "--delivery",
        choices=["sync-reply", "speak", "fire-and-forget"],
        default="fire-and-forget",
    )
    add.set_defaults(handler=_scheduler_add)

    list_command = commands.add_parser("list", help="List promoted tasks")
    list_command.set_defaults(handler=_scheduler_list)

    show = commands.add_parser("show", help="Show one task and its runs")
    show.add_argument("task_id")
    show.set_defaults(handler=_scheduler_show)

    for action in ("approve", "reject", "pause", "resume", "delete"):
        command = commands.add_parser(action, help=f"{action.title()} a task")
        command.add_argument("task_id")
        command.set_defaults(handler=_scheduler_control, action=action)

    init = commands.add_parser("init-keys", help="Generate user and agent signing keys")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=_scheduler_init_keys)

    install = commands.add_parser(
        "install-autostart", help="Install Windows logon autostart with failure restart"
    )
    install.set_defaults(handler=_scheduler_install_autostart)

    uninstall = commands.add_parser(
        "uninstall-autostart", help="Remove the Windows autostart task"
    )
    uninstall.set_defaults(handler=_scheduler_uninstall_autostart)


def _config(args: argparse.Namespace) -> RunConfig:
    return config_from_args(
        args, loader=partial(load_run_config, dsh_home=getattr(args, "home", None))
    )


def _store(config: RunConfig) -> SchedulerStore:
    return SchedulerStore(scheduler_db_path(config.pygent_root))


def _scheduler_run(args: argparse.Namespace) -> dict[str, Any] | None:
    config = _config(args)
    daemon = SchedulerDaemon(config)
    if args.once:
        return daemon.run_once(startup=True)
    daemon.run_forever()
    return {"status": "stopped"}


def _scheduler_add(args: argparse.Namespace) -> dict[str, Any]:
    config = _config(args)
    payload = {
        "name": args.name,
        "schedule": args.schedule.strip(),
        "prompt": args.prompt,
        "instructions": args.instructions or "",
        "session": args.session or config.scheduler.session,
        "priority": getattr(args, "priority", None) or "normal",
        "delivery": getattr(args, "delivery", None) or "fire-and-forget",
    }
    parse_schedule(payload["schedule"])
    signature = sign(
        submission_payload("task", "user", payload),
        load_secret(config.user_pygent_root, "user"),
    )
    return _store(config).submit(
        kind="task",
        source="user",
        payload=payload,
        signature=signature,
    ).to_dict()


def _scheduler_list(args: argparse.Namespace) -> dict[str, Any]:
    config = _config(args)
    return {"tasks": [task.to_dict() for task in _store(config).list_tasks()]}


def _scheduler_show(args: argparse.Namespace) -> dict[str, Any]:
    config = _config(args)
    store = _store(config)
    task = store.task(args.task_id)
    if task is None:
        raise ValueError(f"unknown scheduler task: {args.task_id!r}")
    return {
        "task": task.to_dict(),
        "runs": [run.to_dict() for run in store.list_runs(task_id=task.id)],
    }


def _scheduler_control(args: argparse.Namespace) -> dict[str, Any]:
    config = _config(args)
    payload = {"action": args.action, "task_id": args.task_id}
    signature = sign(
        submission_payload("control", "user", payload),
        load_secret(config.user_pygent_root, "user"),
    )
    return _store(config).submit(
        kind="control",
        source="user",
        payload=payload,
        signature=signature,
    ).to_dict()


def _scheduler_init_keys(args: argparse.Namespace) -> dict[str, Any]:
    config = _config(args)
    init_keys(config.user_pygent_root, force=bool(args.force))
    key_dir = scheduler_keys_dir(config.user_pygent_root)
    return {
        "status": "ready",
        "keys": {
            source: str(key_dir / f"{source}.key")
            for source in ("user", "agent")
        },
    }


def _lara_executable() -> Path:
    candidate = Path(sys.executable).with_name(
        "lara.exe" if os.name == "nt" else "lara"
    )
    if candidate.is_file():
        return candidate
    found = shutil.which("lara")
    if found:
        return Path(found)
    raise RuntimeError("cannot locate the lara executable for autostart")


def _autostart_xml(executable: Path, config: RunConfig) -> str:
    command = escape(str(executable))
    workspace = escape(config.workspace_root)
    arguments = escape(subprocess.list2cmdline([
        "--workspace-root", config.workspace_root,
        "scheduler", "run", "--home", str(Path(config.user_pygent_root).parent),
    ]))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workspace}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _scheduler_install_autostart(args: argparse.Namespace) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("scheduler autostart is only supported on Windows")
    executable = _lara_executable()
    config = _config(args)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".xml", encoding="utf-16", delete=False
    ) as handle:
        handle.write(_autostart_xml(executable, config))
        xml_path = Path(handle.name)
    try:
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                AUTOSTART_TASK_NAME,
                "/XML",
                str(xml_path),
                "/F",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        xml_path.unlink(missing_ok=True)
    return {
        "status": "installed",
        "task": AUTOSTART_TASK_NAME,
        "executable": str(executable),
    }


def _scheduler_uninstall_autostart(args: argparse.Namespace) -> dict[str, Any]:
    del args
    if os.name != "nt":
        raise RuntimeError("scheduler autostart is only supported on Windows")
    subprocess.run(
        ["schtasks", "/Delete", "/TN", AUTOSTART_TASK_NAME, "/F"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {"status": "removed", "task": AUTOSTART_TASK_NAME}


__all__ = ["AUTOSTART_TASK_NAME", "register_scheduler_parser"]
