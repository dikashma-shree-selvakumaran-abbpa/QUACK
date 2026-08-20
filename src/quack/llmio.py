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
2. default ``"copilot_sdk"``

Architectural invariant: every failure mode - a missing token, a transport
error, an unknown provider name, or any arbitrary exception raised inside a
provider - is normalised to a single :class:`LLMUnavailable`. No
provider-specific exception may escape this module, so callers implement
fail-open by catching exactly one exception type.
"""

from __future__ import annotations

import importlib
import os

# The Copilot SDK is the approved transport at ABB; GitHub Models via a PAT is
# not. The compliant path must therefore be the DEFAULT rather than something a
# developer has to opt into. github_models remains available via
# QUACK_PROVIDER=github_models because it is currently the only provider that
# supports tool calling, which the agent's multi-step loop requires.
DEFAULT_PROVIDER = "copilot_sdk"
KNOWN_PROVIDERS = ("github_models", "copilot_sdk")

# Used only when no provider can be selected (unknown name, import failure).
# A provider that loads always supplies its own ``DEFAULT_TIMEOUT_S``.
_FALLBACK_TIMEOUT_S = 6.0


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
		msg = str(exc)[:200].replace("\n", " ")
		raise LLMUnavailable(f"provider unavailable: {type(exc).__name__}: {msg}")


def availability_error() -> str | None:
	"""Return a readable reason the selected provider cannot run, or None.

	The credential/runtime requirement is a *provider* concern, so this
	simply selects the provider and asks it via ``check_availability()``.
	Any failure - including an unknown provider name or an import failure
	surfaced as :class:`LLMUnavailable` - is normalised to a readable string
	rather than raised, so callers can fail-open on a plain value.
	"""
	try:
		provider = _select_provider()
	except LLMUnavailable as exc:
		return exc.reason
	except Exception as exc:
		msg = str(exc)[:200].replace("\n", " ")
		return f"provider unavailable: {type(exc).__name__}: {msg}"

	check = getattr(provider, "check_availability", None)
	if check is None:
		return None
	try:
		return check()
	except LLMUnavailable as exc:
		return exc.reason
	except Exception as exc:
		msg = str(exc)[:200].replace("\n", " ")
		return f"provider unavailable: {type(exc).__name__}: {msg}"


def default_timeout() -> float:
	"""Return the selected provider's realistic minimum timeout in seconds.

	The minimum viable timeout is a property of the *transport* (an HTTP call
	is fast; an SDK that spins up a local runtime is slow), so this asks the
	provider via its ``DEFAULT_TIMEOUT_S`` constant. Any failure to select a
	provider falls back to :data:`_FALLBACK_TIMEOUT_S` rather than raising, so
	callers get a usable number unconditionally.
	"""
	try:
		provider = _select_provider()
	except Exception:
		return _FALLBACK_TIMEOUT_S
	return float(getattr(provider, "DEFAULT_TIMEOUT_S", _FALLBACK_TIMEOUT_S))


def default_model(kind: str = "completion") -> str | None:
	"""Return the selected provider's default model id for ``kind``, or None.

	Model ids are transport-specific (GitHub Models uses ``owner/name``; the
	Copilot SDK uses its own naming), so the default model belongs to the
	*provider*. The default is further split by USE: ``kind="completion"`` for
	Tier 2's single-shot review (tolerates a cheap model) and ``kind="agent"``
	for the multi-step tool-using investigation (needs a stronger model or it
	loops and fails). Any failure to select a provider - or an unknown kind -
	returns None rather than raising, so callers can layer their own fallback.
	"""
	attr = {
		"completion": "DEFAULT_COMPLETION_MODEL",
		"agent": "DEFAULT_AGENT_MODEL",
	}.get(kind)
	if attr is None:
		return None
	try:
		provider = _select_provider()
	except Exception:
		return None
	return getattr(provider, attr, None)


def list_models() -> list[str]:
	"""Return model ids reachable through the selected provider.

	Model discovery is optional. Providers that do not expose it, and every
	provider-specific failure, are normalised to :class:`LLMUnavailable` so a
	diagnostic caller can remain fully fail-open.
	"""
	provider = _select_provider()
	discover = getattr(provider, "list_models", None)
	if discover is None:
		raise LLMUnavailable("model list unavailable")
	try:
		return list(discover())
	except LLMUnavailable:
		raise
	except Exception as exc:
		msg = str(exc)[:200].replace("\n", " ")
		raise LLMUnavailable(
			f"model list unavailable: {type(exc).__name__}: {msg}"
		)


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
		msg = str(exc)[:200].replace("\n", " ")
		raise LLMUnavailable(f"provider error: {type(exc).__name__}: {msg}")


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
		msg = str(exc)[:200].replace("\n", " ")
		raise LLMUnavailable(f"provider error: {type(exc).__name__}: {msg}")
