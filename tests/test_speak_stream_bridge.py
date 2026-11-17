"""SpeakStreamBridge: streaming speak tool-call text → delta suffixes."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pet" / "sidecar"))

from speak_stream import SpeakStreamBridge  # noqa: E402


def _event(kind: str, data: dict[str, object]) -> tuple[str, dict[str, object]]:
    return (kind, data)


def started(index=0, name="speak"):
    return _event("model.tool_call.started", {"index": index, "name": name})


def delta(index, *, name="", arguments=""):
    return _event(
        "model.tool_call.delta",
        {"index": index, "name_delta": name, "arguments_delta": arguments},
    )


def completed(index=0):
    return _event("model.tool_call.completed", {"index": index})


def feed_all(bridge, events):
    out: list[str] = []
    for kind, data in events:
        out.extend(bridge.feed(kind, data))
    return out


def test_speak_text_streams_as_decoded_suffixes():
    bridge = SpeakStreamBridge()
    raw = json.dumps({"text": "你好，兔兔！", "attachments": []})
    # Fragment the raw arguments mid-escape and mid-char to exercise the
    # partial-decode paths (ensure_ascii escapes the CJK as \uXXXX).
    cuts = [0, 5, 12, 20, len(raw)]
    events = [started()]
    for a, b in zip(cuts, cuts[1:]):
        events.append(delta(0, arguments=raw[a:b]))
    suffixes = feed_all(bridge, events)
    assert "".join(suffixes) == "你好，兔兔！"


def test_text_with_escapes_and_surrogate_pair():
    bridge = SpeakStreamBridge()
    # \n escape, \" escape, and a surrogate pair (🐰 = U+1F430).
    text = '第一行\n他说"好" 🐰'
    raw = json.dumps({"text": text})
    events = [started()]
    step = max(1, len(raw) // 8)
    for i in range(0, len(raw), step):
        events.append(delta(0, arguments=raw[i : i + step]))
    suffixes = feed_all(bridge, events)
    assert "".join(suffixes) == text
    assert "\n" in "".join(suffixes)
    assert "🐰" in "".join(suffixes)


def test_incomplete_escape_holds_back_then_completes():
    bridge = SpeakStreamBridge()
    raw = json.dumps({"text": "a\nb"})
    # Split inside the \n escape: '{"text": "a\' + '\\'
    events = [
        started(),
        delta(0, arguments=raw[:10]),
        delta(0, arguments=raw[10:]),
    ]
    suffixes = feed_all(bridge, events)
    assert "".join(suffixes) == "a\nb"


def test_non_speak_tools_emit_nothing():
    bridge = SpeakStreamBridge()
    raw = json.dumps({"text": "should not stream", "file_path": "x"})
    events = [started(name="read")]
    for i in range(0, len(raw), 7):
        events.append(delta(0, arguments=raw[i : i + 7]))
    assert feed_all(bridge, events) == []


def test_name_trailing_arguments_buffer_until_resolved():
    bridge = SpeakStreamBridge()
    raw = json.dumps({"text": "hi"})
    # Arguments start before the name is known (started without a name).
    assert bridge.feed(*started(name="")) == []
    first = delta(0, arguments=raw[:9])
    assert bridge.feed(*first) == []  # still buffering
    suffixes = feed_all(
        bridge,
        [delta(0, name="speak"), delta(0, arguments=raw[9:])],
    )
    assert "".join(suffixes) == "hi"


def test_completed_call_is_forgotten():
    bridge = SpeakStreamBridge()
    feed_all(bridge, [started(), completed()])
    assert bridge.feed(*delta(0, arguments='{"text": "late"}')) == []


def test_reset_clears_inflight_calls():
    bridge = SpeakStreamBridge()
    feed_all(bridge, [started(), delta(0, arguments='{"text": "par')])
    bridge.reset()
    assert bridge.feed(*delta(0, arguments='tial"}')) == []


def test_two_calls_stream_independently():
    bridge = SpeakStreamBridge()
    events = [
        started(index=0),
        started(index=1, name="read"),
        delta(0, arguments='{"text": "AB'),
        delta(1, arguments='{"file_path": "x"}'),
        delta(0, arguments='C"}'),
    ]
    suffixes = feed_all(bridge, events)
    assert "".join(suffixes) == "ABC"


def test_output_reset_clears_state():
    bridge = SpeakStreamBridge()
    feed_all(bridge, [started(), delta(0, arguments='{"text": "old"}')])
    assert bridge.feed("model.output.reset", {}) == []
    feed_all(bridge, [started()])
    suffixes = feed_all(bridge, [delta(0, arguments='{"text": "new"}')])
    assert "".join(suffixes) == "new"
