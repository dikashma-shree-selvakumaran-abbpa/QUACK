"""Provider seam for the LLM transport.

This module is a thin dispatcher: it selects an LLM transport *provider*
and delegates the two public functions to it. The transport itself lives
in :mod:`quack.providers` so it can be swapped without touching any caller.

The two public functions keep their exact contract::

	complete(messages, model, timeout_s=6.0) -> str
	chat(messages, model, tools=None) -> dict

Provider selection order:

1. ``QUACK_PROVIDER`` environment variable (``"github_models"`` |
   ``"copilot_sdk"``)
2. default ``"github_models"``

Architectural invariant: every failure mode - a missing token, a transport
error, an unknown provider name, or any arbitrary exception raised inside a
provider - is normalised to a single :class:`LLMUnavailable`. No
provider-specific exception may escape this module, so callers implement
fail-open by catching exactly one exception type.
"""

from __future__ import annotations

import importlib
import os

DEFAULT_PROVIDER = "github_models"
KNOWN_PROVIDERS = ("github_models", "copilot_sdk")


class LLMUnavailable(Exception):
	"""Raised when the model could not be reached or returned an error.

	The ``reason`` is a short, human-readable string safe to surface to the
	user (e.g. ``"no GITHUB_TOKEN"``). It never contains the token or the
	response body.
	"""

	def __init__(self, reason: str) -> None:
		super().__init__(reason)
		self.reason = reason


def _select_provider():
	"""Return the selected provider module.

	Honours ``QUACK_PROVIDER`` and falls back to :data:`DEFAULT_PROVIDER`.
	An unknown name or an import failure is normalised to
	:class:`LLMUnavailable` rather than crashing the caller.
	"""
	name = os.environ.get("QUACK_PROVIDER") or DEFAULT_PROVIDER
	if name not in KNOWN_PROVIDERS:
		raise LLMUnavailable(f"unknown provider: {name}")
	try:
		return importlib.import_module(f".providers.{name}", __package__)
	except LLMUnavailable:
		raise
	except Exception as exc:
		raise LLMUnavailable(f"provider unavailable: {type(exc).__name__}")


def complete(
	messages: list[dict],
	model: str,
	timeout_s: float = 6.0,
) -> str:
	"""Return the text of the first choice via the selected provider.

	Raises :class:`LLMUnavailable` on every failure mode. Any exception the
	provider raises that is not already :class:`LLMUnavailable` is normalised
	so callers only ever see this one type.
	"""
	provider = _select_provider()
	try:
		return provider.complete(messages, model, timeout_s=timeout_s)
	except LLMUnavailable:
		raise
	except Exception as exc:
		raise LLMUnavailable(f"provider error: {type(exc).__name__}")


def chat(
	messages: list[dict],
	model: str,
	tools: list[dict] | None = None,
) -> dict:
	"""Return the first choice's full message via the selected provider.

	Raises :class:`LLMUnavailable` on every failure mode. Any exception the
	provider raises that is not already :class:`LLMUnavailable` is normalised
	so callers only ever see this one type.
	"""
	provider = _select_provider()
	try:
		return provider.chat(messages, model, tools=tools)
	except LLMUnavailable:
		raise
	except Exception as exc:
		raise LLMUnavailable(f"provider error: {type(exc).__name__}")
