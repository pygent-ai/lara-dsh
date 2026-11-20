"""lara_sdk.execution_host — Lara's composition point for the pygent runtime.

``LocalExecutionHost`` builds one ``PygentRuntimeService`` per workspace but
exposes no way to hand it host-side speak channels. :class:`LaraExecutionHost`
adds that seam: every channel receives every delivered ``speak`` — from the
turn a host submitted, from the finalizer round pygent appends to a
plain-text ending, from dsh-event turns, from fork replies — so a surface
(desktop bubble, TTS, chat bridge) sees the complete spoken stream without
subscribing to each turn individually.

Since pygent 0.6.3 a channel's ``deliver(text, attachments)`` receives the
spoken text and the workspace-resolved attachment paths as separate
arguments; surfaces consume them directly instead of parsing the payload.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
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


__all__ = ["LaraExecutionHost", "SpeakChannel"]
