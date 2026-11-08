"""Lara scheduler — single-agent-bound task scheduling with a standalone daemon.

Layers: store (two-tier SQLite) / schedule (trigger syntax) / keys (per-source
signing) / daemon (poll, promote, execute via subprocess) / tools (agent-facing).
"""
