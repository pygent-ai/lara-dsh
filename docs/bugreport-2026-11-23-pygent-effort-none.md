# Bug report: pygent 0.6.6 rejects DeepSeek's Anthropic `effort="none"` in fork replies

- Date: 2026-11-23
- Project: lara-dsh
- pygent version: 0.6.6
- Python: 3.13.13
- OS: Windows 11
- Severity: high — a message sent while a turn is running is not delivered

## Summary

The realtime fork reply uses `fork_reasoning_effort="none"` so DeepSeek's
Anthropic-compatible endpoint turns thinking off. The endpoint rejects a forced
`tool_choice` while thinking is on ("Thinking mode does not support this
tool_choice"), so `none` is required for the pet's one-line `speak` reply.

pygent 0.6.6 writes that value into the Anthropic request as
`output_config.effort`, but its Anthropic adapter validates effort against the
official Anthropic set, which omits `none`. The fork fails before the request is
sent:

```text
ModelGroupConfigurationError('output_config effort is invalid')
```

## Reproduction

1. Use DeepSeek's Anthropic-compatible route (`https://api.deepseek.com/anthropic`).
2. Configure realtime interaction with `fork_reasoning_effort="none"`.
3. Send a user message while a turn is running, so the runtime starts a fork reply.

Observed: the fork fails with `ModelGroupConfigurationError('output_config
effort is invalid')`.

Expected: the fork sends `output_config.effort="none"`, DeepSeek disables
thinking, and the forced `speak` tool call succeeds.

## Root cause

pygent's two modules disagree about the valid Anthropic effort vocabulary:

- `pygent/agent/pygent_v1/runtime/realtime/fork_reasoning.py:123` writes any
  configured effort into the fork model's provider options:
  ```python
  output_config["effort"] = reasoning_effort
  ```
- `pygent/llm/anthropic_messages.py:61` only accepts:
  ```python
  _EFFORT_VALUES = {"low", "medium", "high", "xhigh", "max"}
  ```
  and `_validate_provider_options` rejects `none` at
  `anthropic_messages.py:756`.

The fork profile is therefore constructible but not request-constructible.
This is an internal consistency bug in pygent, and a compatibility problem for
Anthropic Messages-compatible providers that expose non-Anthropic extension
values.

## Endpoint evidence

A direct request against the current DeepSeek account route, with a forced
`tool_choice` for `speak`:

| Effort | Result |
|---|---|
| `output_config.effort="none"` | 200, `stop_reason=tool_use` |
| `output_config.effort="low"` | 400, `Thinking mode does not support this tool_choice` |
| no `output_config` | 400, `Thinking mode does not support this tool_choice` |

So `none` is the value that makes the fork request valid on this route.

## Suggested upstream fix

Prefer normalizing the cross-protocol value in
`fork_reasoning._apply_reasoning_effort` for `anthropic_messages`:

```python
if reasoning_effort == "none":
    options["thinking"] = {"type": "disabled"}
else:
    output_config["effort"] = reasoning_effort
```

DeepSeek accepts `thinking: {"type": "disabled"}` with the same forced
`tool_choice` (verified: 200, `tool_use`), and this is also the standard
Anthropic way to disable thinking. Alternatively, allow provider-specific
effort values through the Anthropic adapter instead of validating only the
official set.

## Local workaround

`src/lara_pet/sidecar.py:75` adds `none` to pygent's `_EFFORT_VALUES` at import
time so the fork profile passes validation. A regression test at
`tests/test_pet_sidecar_routing.py:253` builds the fork profile and asserts the
request body contains `{"output_config": {"effort": "none"}}`.

The workaround reaches into a private pygent set and should be removed once the
upstream fix lands.
