"""lara_sdk.execution_host — Lara's composition point for the pygent runtime.

``LocalExecutionHost`` builds one ``PygentRuntimeService`` per workspace but
exposes no way to hand it host-side speak channels. :class:`LaraExecutionHost`
adds that seam: every channel receives every delivered ``speak`` — from the
turn a host submitted, from the finalizer round pygent appends to a
plain-text ending, from dsh-event turns, from fork replies — so a surface
(desktop bubble, TTS, chat bridge) sees the complete spoken stream without
subscribing to each turn individually.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any

from pygent.agent.pygent_v1.orchestration import LocalExecutionHost
from pygent.agent.pygent_v1.runtime import SpeakChannel
from pygent.agent.pygent_v1.runtime.service import PygentRuntimeService
from pygent.agent.pygent_v1.schema import RunConfig


class LaraExecutionHost(LocalExecutionHost):
    """``LocalExecutionHost`` whose runtimes mirror speak into host channels."""

    def __init__(self, *, speak_channels: Sequence[SpeakChannel] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.speak_channels: tuple[SpeakChannel, ...] = tuple(speak_channels)

    def _build_runtime(self, config: RunConfig, **kwargs: Any) -> PygentRuntimeService:
        # Same wiring as the base class, plus the channels: the exchange hook
        # routes fork replies to the session lane (see LocalExecutionHost).
        return PygentRuntimeService(
            config,
            on_exchange_ready=partial(self._deliver_exchange, config),
            speak_channels=self.speak_channels,
            **kwargs,
        )


def split_speak_delivery(text: str, workspace_root: str | Path) -> tuple[str, list[str]]:
    """Recover ``(text, attachments)`` from one ``SpeakChannel.deliver`` payload.

    The runtime's speak executor hands channels a single string: the spoken
    text followed by one workspace-relative attachment path per line. Trailing
    lines that name an existing file under the workspace are attachments; the
    first line that does not ends the attachment block.
    """
    root = Path(workspace_root)
    lines = text.split("\n")
    attachments: list[str] = []
    while lines:
        candidate = lines[-1].strip()
        if not candidate or candidate != lines[-1] or Path(candidate).is_absolute():
            break
        if not (root / candidate).is_file():
            break
        attachments.append(candidate)
        lines.pop()
    attachments.reverse()
    return "\n".join(lines), attachments


__all__ = ["LaraExecutionHost", "SpeakChannel", "split_speak_delivery"]
