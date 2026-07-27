"""Riva AI companion: one `/v1/chat` endpoint with two entry paths.

An explicit slash-command is pattern-matched and dispatched straight to its
handler with no LLM call; free text goes to Claude with tool-calling enabled.
Both paths run the **same** handlers out of the `tools.REGISTRY`, so a tool is
defined once and reachable both ways.
"""
