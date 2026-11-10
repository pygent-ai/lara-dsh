"""Drive DeepSeek Harness (dsh) sessions through the official Python SDK.

One :class:`DshGateway` owns one dsh runtime subprocess (``dsh --profile
sdk``). Sessions created through it are durable inside the configured
``dsh_home``: the same session id can be resumed later from this gateway or
from any other DeepSeek Harness SDK client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, RunResult

DEFAULT_DSH_PROVIDER = "deepseek-official"
DEFAULT_DSH_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class DshGatewayConfig:
    dsh_home: Path
    cwd: Path
    provider: str = DEFAULT_DSH_PROVIDER
    model: str = DEFAULT_DSH_MODEL
    api_key: str | None = None
    base_url: str | None = None
    runtime_cwd: Path | None = None

    @classmethod
    def from_env(cls, dsh_home: str | Path, cwd: str | Path) -> "DshGatewayConfig":
        """Read provider credentials from the process environment.

        ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY`` select any
        OpenAI-compatible endpoint (the dsh runtime speaks the DeepSeek
        wire protocol against it); ``DSH_MODEL`` overrides the model id.
        """
        return cls(
            dsh_home=Path(dsh_home),
            cwd=Path(cwd),
            model=os.environ.get("DSH_MODEL") or DEFAULT_DSH_MODEL,
            api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
            runtime_cwd=os.environ.get("DSH_RUNTIME_CWD") or None,
        )


def ensure_primary_runtime() -> Path:
    """Download the bundled dsh runtime resources once; reuse the cache."""
    from deepseek_harness_runtime import download_primary_runtime

    return download_primary_runtime()


class DshGateway:
    """Own one dsh runtime subprocess and expose its session capabilities."""

    def __init__(self, config: DshGatewayConfig) -> None:
        self._config = config
        self._harness: DeepSeekHarness | None = None

    def __enter__(self) -> "DshGateway":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def start(self) -> None:
        if self._harness is not None:
            return
        # The compiled dsh runtime extracts its embedded node_modules to
        # "<runtime subprocess cwd drive>:\snapshot\node\..." and loads native
        # modules from there. Keep that extraction on a stable, always-
        # writable drive instead of the agent workspace's drive: with an
        # E:-drive workspace the extraction lands on E:\snapshot and every
        # turn fails with a koffi.node load error.
        runtime_cwd = self._config.runtime_cwd or (
            Path.home() / ".lara" / "dsh-runtime-cwd"
        )
        runtime_cwd.mkdir(parents=True, exist_ok=True)
        self._config.dsh_home.mkdir(parents=True, exist_ok=True)
        config = DeepSeekHarnessConfig(
            provider=self._config.provider,
            model=self._config.model,
            cwd=str(self._config.cwd),
            runtime_cwd=str(runtime_cwd),
            dsh_home=str(self._config.dsh_home),
            base_url=self._config.base_url,
            api_key=self._config.api_key,
        )
        self._harness = DeepSeekHarness(config)
        self._harness.start()

    def run(self, prompt: str, *, session_id: str | None = None) -> tuple[str, str]:
        """Run one prompt and return ``(final_response, session_id)``."""
        self.start()
        assert self._harness is not None
        result = self._harness.run(prompt, session_id=session_id)
        if self._is_bootstrap_race(result):
            # The first turn after a runtime boot can race the node_modules
            # snapshot extraction; rerunning on the same durable session
            # succeeds once the snapshot is in place.
            result = self._harness.run(prompt, session_id=result.session_id)
        return result.final_response, result.session_id

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        """Restore a persisted session and continue it with one prompt."""
        self.start()
        assert self._harness is not None
        session = self._harness.resume_session(session_id)
        result = session.run(prompt)
        if self._is_bootstrap_race(result):
            result = session.run(prompt)
        return result.final_response, result.session_id

    @staticmethod
    def _is_bootstrap_race(result: RunResult) -> bool:
        return result.finish_reason == "error" and not result.final_response

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None
