"""lara_sdk.agent — the complete Agent facade.

:class:`Lara` is the single external entry point: it composes the four lara
capabilities behind one lifecycle.

- runtime session: :class:`~lara_sdk.lara_rt.LaraSessionAgent` (main harness,
  durable history, eternal conversation, realtime interaction);
- eternal memory: the deployment master switch plus the session-level opt-in;
- realtime interaction: a ``lara-says`` session, whose runtime assembly adds
  the ``speak`` tool automatically;
- scheduler: an embedded, in-process asyncio poll loop that shares the same
  SQLite store as the standalone ``lara scheduler`` daemon. Both sides claim
  due tasks through expiring leases, so they may run side by side safely.

Agent-originated tasks are signed with the agent key and default to
``needs_review``; the caller approves them through :meth:`Lara.tasks`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lara_shell.config import load_run_config
from pygent.agent.pygent_v1.scheduler import (
    Run,
    SchedulerDaemon,
    SchedulerStore,
    Submission,
    Task,
    TaskResult,
    TaskRunner,
    build_task_message,
    init_keys,
    load_secret,
    parse_schedule,
    scheduler_db_path,
    sign,
    submission_payload,
)
from pygent.agent.pygent_v1.schema import RunConfig, SchedulerConfig
from pygent.agent.pygent_v1.graph.prompts import PromptRegistry
from pygent.agent.pygent_v1.scheduler.store import utc_now
from pygent.agent.pygent_v1.sessions import SessionManager

from .interfaces import AgentInput, AgentResult
from .lara_profile import lara_prompt_modules
from .lara_rt import REALTIME_SESSION_MODE, LaraSessionAgent


@dataclass(frozen=True)
class SchedulerSettings:
    """Embedded scheduler settings.

    ``session`` names the dedicated scheduler session (also the case id used
    when the facade creates that session for the first time).
    """

    enabled: bool = True
    poll_seconds: float = 10
    max_concurrent: int = 2
    agent_create_enabled: bool = False
    session: str = "scheduler"

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "poll_seconds", float(self.poll_seconds))
        object.__setattr__(self, "max_concurrent", int(self.max_concurrent))
        object.__setattr__(
            self, "agent_create_enabled", bool(self.agent_create_enabled)
        )
        if self.poll_seconds <= 0:
            raise ValueError("scheduler poll_seconds must be positive")
        if self.max_concurrent < 1:
            raise ValueError("scheduler max_concurrent must be positive")
        if not self.session.strip():
            raise ValueError("scheduler session must be non-empty")


@dataclass(frozen=True)
class LaraConfig:
    """Configuration for one :class:`Lara` facade.

    ``model_group`` selects a model group from the lara configuration. The
    default sentinel ``"default"`` keeps the configured agent's default group
    (or a group literally named ``default`` when one exists).
    """

    workspace_root: str | Path
    model_group: str = "default"
    eternal_memory: bool = False
    realtime: bool = True
    scheduler: SchedulerSettings = SchedulerSettings()
    # Prompt content is composition-side: the kernel registry ships empty.
    # ``system_prompt`` is the configured static base text; enable
    # ``lara_prompt_profile`` to register the stock Lara modules on top.
    system_prompt: str | None = None
    lara_prompt_profile: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_root",
            str(Path(self.workspace_root).expanduser().resolve()),
        )
        object.__setattr__(self, "model_group", str(self.model_group))
        object.__setattr__(self, "eternal_memory", bool(self.eternal_memory))
        object.__setattr__(self, "realtime", bool(self.realtime))
        object.__setattr__(
            self, "system_prompt", None if self.system_prompt is None else str(self.system_prompt)
        )
        object.__setattr__(self, "lara_prompt_profile", bool(self.lara_prompt_profile))
        if not isinstance(self.scheduler, SchedulerSettings):
            raise TypeError("scheduler must be a SchedulerSettings instance")

    def _prompt_registry(self) -> "PromptRegistry | None":
        from pygent.agent.pygent_v1.graph.prompts import PromptRegistry

        if not self.lara_prompt_profile:
            return None
        registry = PromptRegistry()
        for module in lara_prompt_modules():
            registry.register(module)
        return registry


class _InProcessTaskRunner:
    """Run scheduler tasks in the facade's event loop instead of a subprocess."""

    def __init__(
        self,
        run_task: Callable[[Task], Coroutine[Any, Any, str]],
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = 3600.0,
    ) -> None:
        self._run_task = run_task
        self._loop = loop
        self._timeout = timeout

    def run(self, task: Task) -> TaskResult:
        started = utc_now()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._run_task(task), self._loop
            )
            output = future.result(timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - a failed task becomes an audit run
            return TaskResult(
                exit_code=None,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started,
                finished_at=utc_now(),
            )
        return TaskResult(
            exit_code=0,
            output=str(output),
            started_at=started,
            finished_at=utc_now(),
        )


class _EmbeddedScheduler:
    """In-process asyncio poll loop over the shared scheduler SQLite store."""

    def __init__(
        self,
        config: RunConfig,
        *,
        runner: TaskRunner,
    ) -> None:
        self.store = SchedulerStore(scheduler_db_path(config.pygent_root))
        self._daemon = SchedulerDaemon(config, store=self.store, runner=runner)
        self._poll_seconds = float(config.scheduler.poll_seconds)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._loop_task: asyncio.Task[None] | None = None
        self.last_error: Exception | None = None

    async def start(self) -> None:
        # The first cycle runs synchronously so key/schema failures surface in
        # Lara.start() instead of an unobserved background task.
        await self._poll()
        self._loop_task = asyncio.create_task(
            self._run(), name="lara-sdk-scheduler"
        )

    def wake(self) -> None:
        self._wake.set()

    async def _poll(self) -> None:
        async with self._poll_lock:
            await asyncio.to_thread(self._daemon.run_once)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._poll_seconds
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop observable
                self.last_error = exc

    async def wait_processed(
        self, submission_id: str, *, timeout: float = 30.0
    ) -> Submission:
        """Wake the loop and wait until one submission leaves ``unverified``."""

        self.wake()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            submission = self.store.submission(submission_id)
            if submission is not None and submission.status != "unverified":
                return submission
            if asyncio.get_running_loop().time() >= deadline:
                detail = (
                    f" (scheduler loop error: {self.last_error})"
                    if self.last_error is not None
                    else ""
                )
                raise TimeoutError(
                    f"scheduler submission {submission_id!r} was not processed"
                    f" within {timeout}s{detail}"
                )
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task, self._loop_task = self._loop_task, None
        if task is not None:
            await task


class LaraTasks:
    """View and control the shared scheduler task store.

    Returned by :meth:`Lara.tasks`. Reads are synchronous SQLite snapshots;
    mutations submit signed ``user`` controls and wait for the embedded (or an
    external) scheduler to promote them.
    """

    def __init__(self, facade: "Lara") -> None:
        self._facade = facade

    @property
    def _store(self) -> SchedulerStore:
        store = self._facade._store
        if store is None:
            raise RuntimeError("scheduler is disabled")
        return store

    def list(self) -> tuple[Task, ...]:
        return tuple(self._store.list_tasks())

    def get(self, task_id: str) -> Task | None:
        return self._store.task(task_id)

    def runs(self, task_id: str | None = None) -> tuple[Run, ...]:
        return tuple(self._store.list_runs(task_id=task_id))

    async def approve(self, task_id: str) -> Task:
        return await self._facade._control_task("approve", task_id)

    async def reject(self, task_id: str) -> Task:
        return await self._facade._control_task("reject", task_id)

    async def pause(self, task_id: str) -> Task:
        return await self._facade._control_task("pause", task_id)

    async def resume(self, task_id: str) -> Task:
        return await self._facade._control_task("resume", task_id)

    async def delete(self, task_id: str) -> None:
        await self._facade._control("delete", task_id)

    def __iter__(self) -> Iterator[Task]:
        return iter(self.list())

    def __len__(self) -> int:
        return len(self.list())


class Lara:
    """Complete Agent facade: session runtime + scheduler in one lifecycle."""

    def __init__(self, config: LaraConfig) -> None:
        self.config = config
        self._run_config: RunConfig | None = None
        self._session_agent: LaraSessionAgent | None = None
        self._scheduler: _EmbeddedScheduler | None = None
        self._store: SchedulerStore | None = None
        self._scheduler_session_id: str | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._started = False

    # —— lifecycle ——

    async def start(self) -> "Lara":
        """Start the runtime session and the embedded scheduler loop."""

        if self._started:
            raise RuntimeError("Lara is already started")
        config = self._effective_config()
        manager = SessionManager(config)
        scheduler_session_id: str | None = None
        store: SchedulerStore | None = None
        if self.config.scheduler.enabled:
            init_keys(config.user_pygent_root)
            scheduler_session_id = self._ensure_scheduler_session(
                manager, self.config.scheduler.session
            )
            store = SchedulerStore(scheduler_db_path(config.pygent_root))

        mode = REALTIME_SESSION_MODE if self.config.realtime else "chat"
        session_id: str | None = None
        if self.config.realtime:
            existing = manager.list_sessions(mode=REALTIME_SESSION_MODE)
            if existing:
                session_id = str(existing[0]["session_id"])
        agent = LaraSessionAgent(
            config=config,
            session_id=session_id,
            mode=mode,
            eternal_memory=self.config.eternal_memory,
            system_prompt=self.config.system_prompt,
            prompt_registry=self.config._prompt_registry(),
        )
        await agent.__aenter__()
        if self.config.eternal_memory:
            agent.enable_eternal_memory()

        self._run_config = config
        self._session_agent = agent
        self._scheduler_session_id = scheduler_session_id
        self._store = store
        try:
            if self.config.scheduler.enabled:
                assert store is not None
                runner = _InProcessTaskRunner(
                    self._run_scheduled_task, asyncio.get_running_loop()
                )
                self._scheduler = _EmbeddedScheduler(config, runner=runner)
                await self._scheduler.start()
        except BaseException:
            self._scheduler = None
            self._store = None
            self._scheduler_session_id = None
            self._session_agent = None
            await agent.__aexit__(None, None, None)
            raise
        self._started = True
        return self

    async def stop(self) -> None:
        """Stop the scheduler loop and close the runtime session."""

        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            await scheduler.stop()
        agent, self._session_agent = self._session_agent, None
        if agent is not None:
            await agent.__aexit__(None, None, None)
        self._store = None
        self._scheduler_session_id = None
        self._run_config = None
        self._started = False

    # —— runtime session ——

    async def run(
        self, message: str, *, attachments: Sequence[str] = ()
    ) -> AgentResult:
        """Run one foreground conversation turn.

        ``attachments`` are workspace-relative paths delivered with this turn.
        """

        self._require_started()
        assert self._session_agent is not None
        return await self._session_agent.run(
            AgentInput(message=message, attachments=tuple(attachments))
        )

    def history(self) -> tuple[Mapping[str, Any], ...]:
        """The main session's durable conversation history."""

        self._require_started()
        assert self._session_agent is not None
        return self._session_agent.history()

    # —— scheduler ——

    async def schedule_task(
        self,
        name: str,
        schedule: str,
        prompt: str,
        instructions: str = "",
    ) -> Task:
        """Submit an agent-authored task (``needs_review`` unless auto-approved)."""

        self._require_scheduler()
        if not name.strip():
            raise ValueError("task name must be non-empty")
        if not prompt.strip():
            raise ValueError("task prompt must be non-empty")
        parse_schedule(schedule)
        store = self._require_store()
        payload = {
            "name": name,
            "schedule": schedule.strip(),
            "prompt": prompt,
            "instructions": instructions,
            "session": self._scheduler_session_id,
            "priority": "normal",
        }
        submission = self._submit("task", "agent", payload)
        processed = await self._processed(submission.id)
        if processed.status != "verified" or processed.task_id is None:
            raise RuntimeError(
                f"scheduler rejected task {name!r}: {processed.error or processed.status}"
            )
        task = store.task(processed.task_id)
        if task is None:
            raise RuntimeError(f"scheduler task {processed.task_id!r} disappeared")
        return task

    async def emit_event(
        self, name: str, payload: Mapping[str, Any] | None = None
    ) -> Submission:
        """Emit an agent-signed event that can trigger ``on <event>`` tasks."""

        self._require_scheduler()
        if not name.strip():
            raise ValueError("event name must be non-empty")
        submission = self._submit(
            "event", "agent", {"event": name, "data": dict(payload or {})}
        )
        processed = await self._processed(submission.id)
        if processed.status != "verified":
            raise RuntimeError(
                f"scheduler rejected event {name!r}: "
                f"{processed.error or processed.status}"
            )
        return processed

    def tasks(self) -> LaraTasks:
        """View and control scheduler tasks."""

        self._require_started()
        if self._store is None:
            raise RuntimeError("scheduler is disabled")
        return LaraTasks(self)

    # —— internal ——

    def _effective_config(self) -> RunConfig:
        config = load_run_config(workspace_root=self.config.workspace_root)
        requested = self.config.model_group
        native = config.model_config
        if requested and requested != "default":
            group = requested
        elif native is not None and "default" in native.model_groups:
            group = "default"
        else:
            group = None
        if group is not None:
            if native is None or group not in native.model_groups:
                raise ValueError(f"unknown model group {group!r}")
            if config.resolved_agent is None:
                raise ValueError(
                    "agent configuration is required to select a model group"
                )
            config.resolved_agent = replace(
                config.resolved_agent, default_model_group=group
            )
        settings = self.config.scheduler
        config.scheduler = SchedulerConfig(
            agent_create_enabled=settings.agent_create_enabled,
            session=settings.session,
            max_concurrent=settings.max_concurrent,
            poll_seconds=settings.poll_seconds,
        )
        config.eternal_conversation = replace(
            config.eternal_conversation, enabled=self.config.eternal_memory
        )
        config.realtime_interaction = replace(
            config.realtime_interaction, enabled=self.config.realtime
        )
        return config

    @staticmethod
    def _ensure_scheduler_session(manager: SessionManager, case_id: str) -> str:
        for record in manager.list_sessions(mode="scheduler"):
            if str(record.get("case_id") or "") == case_id:
                return str(record["session_id"])
        return manager.create(case_id, mode="scheduler").session_id

    async def _run_scheduled_task(self, task: Task) -> str:
        config = self._run_config
        if config is None:
            raise RuntimeError("Lara is not started")
        message = build_task_message(
            task_id=task.id,
            name=task.name,
            source=task.source,
            instructions=task.instructions,
            user_message=task.prompt,
            user_identity=config.user_identity,
        )
        lock = self._session_locks.setdefault(task.session, asyncio.Lock())
        async with lock:
            agent = LaraSessionAgent(
                config=config,
                session_id=task.session,
                eternal_memory=self.config.eternal_memory,
                system_prompt=self.config.system_prompt,
                prompt_registry=self.config._prompt_registry(),
            )
            await agent.__aenter__()
            try:
                if self.config.eternal_memory:
                    agent.enable_eternal_memory()
                result = await agent.run(message)
            finally:
                await agent.__aexit__(None, None, None)
        return result.text

    def _submit(
        self, kind: str, source: str, payload: Mapping[str, Any]
    ) -> Submission:
        assert self._run_config is not None
        assert self._store is not None
        secret = load_secret(self._run_config.user_pygent_root, source)
        return self._store.submit(
            kind=kind,
            source=source,
            payload=payload,
            signature=sign(submission_payload(kind, source, payload), secret),
        )

    async def _processed(self, submission_id: str) -> Submission:
        assert self._scheduler is not None
        return await self._scheduler.wait_processed(submission_id)

    async def _control(self, action: str, task_id: str) -> None:
        self._require_scheduler()
        store = self._require_store()
        if store.task(task_id) is None:
            raise ValueError(f"unknown scheduler task {task_id!r}")
        submission = self._submit(
            "control", "user", {"action": action, "task_id": task_id}
        )
        processed = await self._processed(submission.id)
        if processed.status != "verified":
            raise RuntimeError(
                f"scheduler rejected {action} for {task_id!r}: "
                f"{processed.error or processed.status}"
            )
        if action == "delete" and store.task(task_id) is not None:
            raise RuntimeError(f"scheduler task {task_id!r} was not deleted")

    async def _control_task(self, action: str, task_id: str) -> Task:
        await self._control(action, task_id)
        task = self._require_store().task(task_id)
        if task is None:
            raise RuntimeError(f"scheduler task {task_id!r} disappeared")
        return task

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Lara must be started before use")

    def _require_scheduler(self) -> None:
        self._require_started()
        if not self.config.scheduler.enabled or self._scheduler is None:
            raise RuntimeError("scheduler is disabled")

    def _require_store(self) -> SchedulerStore:
        self._require_scheduler()
        assert self._store is not None
        return self._store


__all__ = [
    "Lara",
    "LaraConfig",
    "LaraTasks",
    "SchedulerSettings",
]
