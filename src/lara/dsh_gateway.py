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

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

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

    @classmethod
    def from_env(cls, dsh_home: str | Path, cwd: str | Path) -> "DshGatewayConfig":
        """Read provider credentials from the process environment."""
        return cls(
            dsh_home=Path(dsh_home),
            cwd=Path(cwd),
            api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or None,
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
        config = DeepSeekHarnessConfig(
            provider=self._config.provider,
            model=self._config.model,
            cwd=str(self._config.cwd),
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
        return result.final_response, result.session_id

    def resume_run(self, session_id: str, prompt: str) -> tuple[str, str]:
        """Restore a persisted session and continue it with one prompt."""
        self.start()
        assert self._harness is not None
        session = self._harness.resume_session(session_id)
        result = session.run(prompt)
        return result.final_response, result.session_id

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None
