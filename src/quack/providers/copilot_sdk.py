"""GitHub Copilot SDK provider.

Implements the same two-function contract as the other providers against
the GitHub Copilot SDK (``pip install github-copilot-sdk``, imported as
``from copilot import CopilotClient``).

The SDK is async and quack is sync, so every entry point drives the async
transport with :func:`asyncio.run`. Every failure mode - an auth failure,
an SDK exception, an asyncio error, a timeout, or an empty/None response -
is normalised to a single :class:`LLMUnavailable` with a short, readable
reason so callers can implement fail-open behaviour.

.. important::
	Auth comes from the Copilot CLI's stored OAuth login. Do NOT read or
	pass ``GITHUB_TOKEN`` here: an env token SHADOWS the working CLI login
	and causes an authorization failure. This provider deliberately never
	touches ``GITHUB_TOKEN``.
"""

from __future__ import annotations

import asyncio

from ..llmio import LLMUnavailable

try:  # pragma: no cover - exercised via monkeypatch in tests
	from copilot import CopilotClient
except ImportError:  # pragma: no cover - SDK is an optional runtime dep
	CopilotClient = None

# Realistic minimum timeout for this transport. The Copilot SDK must start a
# local runtime (~9s) before inference, so a short bound guarantees a timeout
# before the model is ever reached. This is a property of the TRANSPORT.
DEFAULT_TIMEOUT_S = 60.0

# Default model identifiers for this transport, split by USE. The Copilot SDK
# uses its own model naming (not GitHub Models' "owner/name"), so the defaults
# belong to the PROVIDER. They differ by capability requirement: Tier 2's
# single-shot review tolerates a cheaper model (haiku), but the agent's
# multi-step tool-using investigation needs a stronger one (sonnet) or it
# loops and fails.
DEFAULT_COMPLETION_MODEL = "claude-haiku-4.5"
DEFAULT_AGENT_MODEL = "claude-sonnet-4.5"


def check_availability() -> str | None:
	"""Return a readable reason this provider cannot run, or None if it can.

	Auth comes from the Copilot CLI's stored OAuth login, NOT from
	``GITHUB_TOKEN`` (an env token would shadow that login), so this
	provider is available regardless of ``GITHUB_TOKEN``. The only thing it
	genuinely needs is the SDK itself.
	"""
	if CopilotClient is None:
		return "copilot sdk not installed"
	return None


def _flatten(messages: list[dict]) -> str:
	"""Flatten OpenAI-style messages into a single delimited prompt string.

	System content is emitted first, then user (and any other) content,
	each under a clear header. Any "reply with only JSON" instruction that
	lives in the message content is preserved verbatim so downstream JSON
	parsing still works.
	"""
	system_parts: list[str] = []
	other_parts: list[str] = []
	for message in messages:
		content = (message.get("content") or "").strip()
		if not content:
			continue
		if message.get("role") == "system":
			system_parts.append(content)
		else:
			other_parts.append(content)

	sections: list[str] = []
	if system_parts:
		sections.append("[System]\n" + "\n\n".join(system_parts))
	if other_parts:
		sections.append("[User]\n" + "\n\n".join(other_parts))
	return "\n\n".join(sections)


async def _complete_async(prompt: str, model: str, timeout_s: float) -> str:
	"""Drive the async SDK for a single completion and return the text."""
	if CopilotClient is None:
		raise LLMUnavailable("copilot sdk not installed")

	client = CopilotClient()
	try:
		# NOTE: auth is the Copilot CLI's stored OAuth login. We must NOT
		# set or forward GITHUB_TOKEN; an env token shadows that login.
		await client.start()
		session = await client.create_session(model=model)
		resp = await asyncio.wait_for(
			session.send_and_wait(prompt), timeout=timeout_s
		)
		if resp is None or resp.data is None or not resp.data.content:
			raise LLMUnavailable("copilot sdk returned no content")
		return resp.data.content
	finally:
		# Always stop the client so no runtime process leaks.
		try:
			await client.stop()
		except Exception:
			pass


def complete(
	messages: list[dict],
	model: str,
	timeout_s: float = 6.0,
) -> str:
	"""Send ``messages`` as one flattened prompt and return the text.

	Raises :class:`LLMUnavailable` for every failure mode: auth failure,
	any SDK exception, an asyncio error, a timeout, or an empty response.
	"""
	prompt = _flatten(messages)
	try:
		return asyncio.run(_complete_async(prompt, model, timeout_s))
	except LLMUnavailable:
		raise
	except asyncio.TimeoutError:
		raise LLMUnavailable("copilot sdk timed out")
	except Exception as exc:
		msg = str(exc)[:200].replace("\n", " ")
		raise LLMUnavailable(f"copilot sdk error: {type(exc).__name__}: {msg}")


def chat(
	messages: list[dict],
	model: str,
	tools: list[dict] | None = None,
) -> dict:
	"""Tool calling is unsupported on this provider.

	The Copilot SDK exposes no OpenAI-style ``tool_calls`` surface, so the
	agent stays on the ``github_models`` provider. We do NOT emulate tool
	calling; we fail-open instead.
	"""
	raise LLMUnavailable("tool calling not supported on copilot_sdk provider")
