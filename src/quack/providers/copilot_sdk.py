"""GitHub Copilot SDK provider (stub).

Implements the same two-function contract as the other providers against
the GitHub Copilot SDK. The real transport lands in a follow-up task; for
now both entry points fail-open by raising :class:`LLMUnavailable` so that
selecting this provider degrades gracefully instead of crashing.
"""

from __future__ import annotations

from ..llmio import LLMUnavailable

_NOT_IMPLEMENTED = "copilot sdk provider not implemented"


def complete(
	messages: list[dict],
	model: str,
	timeout_s: float = 6.0,
) -> str:
	"""Not yet implemented; always raises :class:`LLMUnavailable`."""
	raise LLMUnavailable(_NOT_IMPLEMENTED)


def chat(
	messages: list[dict],
	model: str,
	tools: list[dict] | None = None,
) -> dict:
	"""Not yet implemented; always raises :class:`LLMUnavailable`."""
	raise LLMUnavailable(_NOT_IMPLEMENTED)
