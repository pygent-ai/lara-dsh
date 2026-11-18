"""Stream ``speak`` tool-call text as user-visible delta events (D-RT10).

pygent reduces provider streams into ``model.tool_call.delta`` events that
carry raw JSON-argument fragments. The desktop pet renders
``lara.realtime.speak`` bubbles with a typewriter; this bridge turns a
streaming ``speak`` call into ``lara.realtime.speak.delta`` events carrying
decoded text suffixes, so the frontend shows the words as the model writes
them instead of replaying a finished text.

The final ``lara.realtime.speak`` event (emitted by the speak executor after
the tool call completes) stays authoritative: the delta stream is a preview
and may stop short if a retry resets the output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_SPEAK_TOOL_NAME = "speak"
_HEX = "0123456789abcdefABCDEF"
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

# The "text" key of a JSON object: preceded by "{" or "," and followed by a
# string value. String values may legitimately contain the sequence too; the
# final speak event remains authoritative, so a rare false positive only
# affects the preview stream.
_TEXT_KEY = re.compile(r'[{,]\s*"text"\s*:\s*"')


def _decode_json_string_prefix(raw: str) -> str | None:
    """Decode the value of the first ``"text": "..."`` field in a partial JSON
    object. Returns the decoded prefix, or ``None`` while the key has not
    appeared yet. Stops conservatively before incomplete escapes so the
    decoded output is always prefix-monotonic and dump-safe."""
    match = _TEXT_KEY.search(raw)
    if match is None:
        return None
    out: list[str] = []
    i = match.end()
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            break  # value closed
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break  # dangling backslash at the tail
        esc = raw[i + 1]
        simple = _SIMPLE_ESCAPES.get(esc)
        if simple is not None:
            out.append(simple)
            i += 2
            continue
        if esc != "u":
            break  # unknown escape: stop before it
        hex4 = raw[i + 2 : i + 6]
        if len(hex4) < 4 or any(c not in _HEX for c in hex4):
            break  # incomplete \\uXXXX
        code = int(hex4, 16)
        if 0xD800 <= code <= 0xDBFF:
            # High surrogate: only emit when the low surrogate already arrived.
            if raw[i + 6 : i + 8] != "\\u":
                break
            hex4b = raw[i + 8 : i + 12]
            if len(hex4b) < 4 or any(c not in _HEX for c in hex4b):
                break
            low = int(hex4b, 16)
            if not 0xDC00 <= low <= 0xDFFF:
                break
            out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
            i += 12
            continue
        if 0xDC00 <= code <= 0xDFFF:
            break  # stray low surrogate: stop before it
        out.append(chr(code))
        i += 6
    return "".join(out)


class SpeakStreamBridge:
    """Tracks streaming tool calls; yields new speak text suffixes per event.

    One bridge per model execution. The tool call ``name`` may trail its
    first argument fragments on some providers, so arguments are buffered
    until the name resolves and only ``speak`` calls are decoded.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, object]] = {}

    def reset(self) -> None:
        """Forget all in-flight calls (``model.output.reset`` on retry)."""
        self._calls.clear()

    def feed(self, kind: str, data: Mapping[str, object]) -> list[str]:
        """Consume one model event; return new decoded speak text suffixes."""

        if kind == "model.output.reset":
            self.reset()
            return []
        if kind == "model.tool_call.started":
            index = self._index(data)
            self._calls[index] = {
                "name": self._string(data.get("name")),
                "raw": "",
                "emitted": 0,
            }
            return []
        if kind == "model.tool_call.delta":
            index = self._index(data)
            entry = self._calls.get(index)
            if entry is None:
                return []
            name_delta = self._string(data.get("name_delta"))
            if name_delta:
                entry["name"] = self._string(entry.get("name")) + name_delta
            args_delta = self._string(data.get("arguments_delta"))
            if args_delta:
                entry["raw"] = self._string(entry.get("raw")) + args_delta
            return self._extract(index, entry)
        if kind == "model.tool_call.completed":
            self._calls.pop(self._index(data), None)
            return []
        return []

    @staticmethod
    def _index(data: Mapping[str, object]) -> int:
        index = data.get("index", 0)
        return index if isinstance(index, int) and not isinstance(index, bool) else 0

    @staticmethod
    def _string(value: object) -> str:
        return value if isinstance(value, str) else ""

    def _extract(self, index: int, entry: dict[str, object]) -> list[str]:
        name = self._string(entry.get("name"))
        if not name:
            return []  # name not resolved yet: keep buffering
        if name != _SPEAK_TOOL_NAME:
            self._calls.pop(index, None)
            return []
        decoded = _decode_json_string_prefix(self._string(entry.get("raw")))
        if decoded is None:
            return []
        emitted = entry.get("emitted")
        emitted = emitted if isinstance(emitted, int) else 0
        if len(decoded) <= emitted:
            return []
        entry["emitted"] = len(decoded)
        return [decoded[emitted:]]


__all__ = ["SpeakStreamBridge"]
