from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from lara_shell.config import load_run_config
from lara_shell.credentials import build_credential_environment
from pygent.agent.pygent_v1.core.io import plain_object
from pygent.agent.pygent_v1.orchestration import (
    LocalExecutionHost,
    ManagedSessionTurn,
    UserDelivery,
)
from pygent.agent.pygent_v1.schema import RunConfig
from pygent.agent.pygent_v1.sessions import SessionManager


def register_session_parser(subparsers: Any) -> None:
    session = subparsers.add_parser("session", help="Create, inspect, and run sessions")
    commands = session.add_subparsers(dest="session_command", required=True)

    create = commands.add_parser("create", help="Create a session")
    create.add_argument("--case", required=True, dest="case_id")
    create.add_argument("--mode", default="e2e")
    create.set_defaults(handler=_session_create)

    list_command = commands.add_parser("list", help="List sessions")
    list_command.add_argument("--mode", default=None, help="Filter by session mode")
    list_command.set_defaults(handler=_session_list)

    show = commands.add_parser("show", help="Show a session")
    show.add_argument("session_id")
    show.set_defaults(handler=_session_show)

    run = commands.add_parser("run", help="Run one agent turn and return JSON")
    _add_session_target(run)
    run.add_argument("-m", "--message", required=True, help="Message for the agent")
    _add_turn_attachments(run)
    run.set_defaults(handler=_session_run)

    chat = commands.add_parser("chat", help="Open an interactive agent chat")
    _add_session_target(chat)
    _add_turn_attachments(chat)
    chat.set_defaults(handler=_session_chat)


def _add_session_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", nargs="?", help="Existing session ID")
    parser.add_argument("--new", action="store_true", help="Create a new chat session")


def _add_turn_attachments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-a",
        "--attachment",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Workspace-relative file to attach to the submitted turn"
            " (repeatable)"
        ),
    )


def _validate_session_target(args: argparse.Namespace) -> None:
    if args.new and args.session_id:
        raise ValueError("session_id and --new cannot be used together")
    if not args.new and not args.session_id:
        raise ValueError("session_id is required unless --new is used")


def _session_create(args: argparse.Namespace) -> dict[str, Any]:
    return _manager(args).create(args.case_id, mode=args.mode).to_dict()


def _session_list(args: argparse.Namespace) -> dict[str, Any]:
    return {"sessions": _manager(args).list_sessions(mode=args.mode)}


def _session_show(args: argparse.Namespace) -> dict[str, Any]:
    return _manager(args).show(args.session_id)


def _session_run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_session_target(args)
    return asyncio.run(_run_session_turn(args))


async def _run_session_turn(args: argparse.Namespace) -> dict[str, Any]:
    config, manager = _config_and_manager(args, session_id=args.session_id)
    async with _execution_host(config) as host:
        turn = _require_turn(
            await host.deliver_user_message(
                config=config,
                manager=manager,
                session_id=args.session_id,
                message=args.message,
                input_id="turn-0001",
                interactive_approvals=False,
                attachments=tuple(args.attachment or ()),
            )
        )
        output, _ = await turn.result()
        return _turn_result_payload(turn, output)


def _session_chat(args: argparse.Namespace) -> None:
    _validate_session_target(args)
    return asyncio.run(_interactive_session_chat(args))


async def _interactive_session_chat(args: argparse.Namespace) -> None:
    config, manager = _config_and_manager(args, session_id=args.session_id)
    session_id = args.session_id
    if session_id is None:
        session_id = manager.create("chat", mode="chat").session_id
    else:
        manager.load(session_id)

    async with _execution_host(config) as host:
        print(f"lara session chat: {session_id}")
        print("Type /exit or /quit to end.")
        turn_index = 1
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if user_input.strip() in {"/exit", "/quit"}:
                break
            if not user_input.strip():
                continue
            turn = _require_turn(
                await host.deliver_user_message(
                    config=config,
                    manager=manager,
                    session_id=session_id,
                    message=user_input,
                    input_id=f"turn-{turn_index:04d}",
                    interactive_approvals=True,
                    attachments=tuple(args.attachment or ()),
                )
            )
            await turn.wait_ready()
            streamed = await _render_turn_events(turn)
            output, _ = await turn.result()
            result = plain_object(plain_object(output.data).get("result"))
            if streamed:
                print()
            elif result.get("final_answer"):
                print(result["final_answer"])
            if result.get("error"):
                print(f"agent error: {result['error']}", file=sys.stderr)
                break
            turn_index += 1


def _require_turn(delivery: UserDelivery) -> ManagedSessionTurn:
    # CLI sessions are plain chat sessions, so the unified entry always
    # starts a turn; an interrupt delivery means a realtime session was
    # targeted, which the CLI does not drive.
    if delivery.turn is None:
        raise RuntimeError(
            f"session delivered via {delivery.mode!r}; the CLI only runs turns"
        )
    return delivery.turn


async def _render_turn_events(turn: ManagedSessionTurn) -> bool:
    if turn.execution_handle is None:
        return False
    streamed = False
    async with turn.subscribe() as execution_events:
        async for event in execution_events:
            data = plain_object(event.data)
            if (
                event.kind == "model.text.delta"
                and ".foreground.react.model.model" in event.module_path
            ):
                chunk = str(data.get("text") or "")
                if chunk:
                    streamed = True
                    print(chunk, end="", flush=True)
            elif event.kind == "pygent.v1.approval.requested":
                answer = await asyncio.to_thread(
                    input,
                    f"Approve {data.get('tool_name')} {data.get('arguments')}? [y/N] ",
                )
                await turn.runtime_service.deliver_approval(
                    str(data["approval_id"]),
                    approved=answer.strip().lower() in {"y", "yes"},
                    comment="interactive CLI decision",
                )
    return streamed


def _turn_result_payload(turn: ManagedSessionTurn, output: Any) -> dict[str, Any]:
    run_ref = turn.run_ref
    if run_ref is None:
        raise RuntimeError("session turn has no case run")
    result = plain_object(plain_object(output.data).get("result"))
    payload = {
        "session_id": run_ref.session_id,
        "case_run_id": run_ref.case_run_id,
        "execution_id": turn.execution_id,
        "status": str(result.get("status") or turn.status),
        "final_answer": str(result.get("final_answer") or ""),
        "run_dir": str(Path(run_ref.run_dir).resolve()),
    }
    if result.get("error"):
        payload["error"] = str(result["error"])
    return payload


def _execution_host(config: RunConfig) -> LocalExecutionHost:
    """Compose the CLI execution host with shell-owned config and credentials."""

    return LocalExecutionHost(
        credential_environ=build_credential_environment(config.user_pygent_root),
        run_config_loader=load_run_config,
    )


def _config_and_manager(
    args: argparse.Namespace,
    *,
    session_id: str | None = None,
) -> tuple[RunConfig, SessionManager]:
    config = load_run_config(
        workspace_root=args.workspace_root,
        session_id=session_id,
        agent_alias=args.agent_alias,
        max_steps=args.max_steps,
    )
    return config, SessionManager(config)


def _manager(args: argparse.Namespace) -> SessionManager:
    return SessionManager(
        load_run_config(
            workspace_root=args.workspace_root,
            session_id=getattr(args, "session_id", None),
            agent_alias=args.agent_alias,
            max_steps=args.max_steps,
        )
    )


__all__ = ["register_session_parser"]
