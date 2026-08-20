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
import logging
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout

from .. import render
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

_AUTH_MESSAGE = (
	"Copilot login expired or unavailable — run `copilot` then `/login`. "
	"(Also check: an ambient GITHUB_TOKEN shadows your Copilot login — "
	"run `quack model` to diagnose.)"
)
_COPILOT_REQUESTS_MESSAGE = (
	"Copilot access denied — your PAT needs the `Copilot Requests` permission."
)


class _CopilotTimeout(TimeoutError):
	"""Internal timeout carrying the SDK lifecycle stage that exhausted budget."""

	def __init__(self, stage: str) -> None:
		super().__init__(f"{stage} timeout")
		self.stage = stage


@contextmanager
def _silence_sdk_logging():
	"""Suppress all Copilot SDK terminal output for one adapter operation."""
	parent = logging.getLogger("copilot")
	parent_state = (
		list(parent.handlers),
		parent.level,
		parent.propagate,
		parent.disabled,
	)
	children = [
		(logger, logger.disabled)
		for name, logger in logging.Logger.manager.loggerDict.items()
		if name.startswith("copilot.") and isinstance(logger, logging.Logger)
	]
	parent.handlers = [logging.NullHandler()]
	parent.propagate = False
	parent.disabled = False
	for logger, _ in children:
		logger.disabled = True
	try:
		with open(os.devnull, "w", encoding="utf-8") as sink:
			with redirect_stdout(sink), redirect_stderr(sink):
				yield
	finally:
		parent.handlers, parent.level, parent.propagate, parent.disabled = parent_state
		for logger, disabled in children:
			logger.disabled = disabled


def _short_error(exc: Exception, limit: int = 160) -> str:
	"""Return a single-line bounded SDK error without traceback content."""
	message = " ".join(str(exc).split()) or type(exc).__name__
	if len(message) > limit:
		return message[: limit - 3].rstrip() + "..."
	return message


def _is_auth_error(exc: Exception) -> bool:
	"""Return whether an SDK failure requires credentials to be repaired."""
	message = _short_error(exc).lower()
	return any(
		signature in message
		for signature in (
			"authorization",
			"authentication",
			"unauthorized",
			"401",
			"run /login",
			"copilot requests",
		)
	)


def _error_reason(exc: Exception, *, context: str = "copilot sdk error") -> str:
	"""Map known SDK failures to actionable text while retaining the reason."""
	raw_reason = _short_error(exc)
	lower_reason = raw_reason.lower()
	if "copilot requests" in lower_reason:
		message = _COPILOT_REQUESTS_MESSAGE
	elif _is_auth_error(exc):
		message = _AUTH_MESSAGE
	elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
		stage = getattr(exc, "stage", "inference")
		message = f"Copilot {stage} timed out."
	else:
		message = context
	return f"{message} Raw reason: {raw_reason}"


async def _within_budget(awaitable, deadline: float, stage: str):
	"""Await one SDK operation without crossing the caller's deadline."""
	remaining = deadline - asyncio.get_running_loop().time()
	if remaining <= 0:
		close = getattr(awaitable, "close", None)
		if close is not None:
			close()
		raise _CopilotTimeout(stage)
	try:
		return await asyncio.wait_for(awaitable, timeout=remaining)
	except asyncio.TimeoutError as exc:
		raise _CopilotTimeout(stage) from exc


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


def _runtime_needs_preparation() -> bool:
	"""Return whether the SDK reliably reports no cached bundled runtime."""
	if (
		CopilotClient is None
		or os.environ.get("COPILOT_CLI_PATH")
		or os.environ.get("COPILOT_SKIP_CLI_DOWNLOAD", "").lower()
		in ("1", "true", "yes")
	):
		return False
	try:
		from copilot._cli_download import get_cached_cli_path
	except ImportError:
		return False
	try:
		return get_cached_cli_path() is None
	except Exception:
		return False


def _show_runtime_preparation_notice() -> None:
	"""Explain the SDK's confirmed first-use runtime preparation delay."""
	if _runtime_needs_preparation():
		render.metadata(
			"first run: preparing the Copilot runtime, this happens once"
		)


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


def _model_id(model) -> str | None:
	"""Extract a stable model id from SDK objects across supported versions."""
	if isinstance(model, str):
		return model or None
	if isinstance(model, dict):
		value = model.get("id") or model.get("name")
	else:
		value = getattr(model, "id", None) or getattr(model, "name", None)
	return str(value) if value else None


async def _list_models_async() -> list[str]:
	"""Start the SDK runtime and return model ids reachable by this login."""
	if CopilotClient is None:
		raise LLMUnavailable("copilot sdk not installed")

	client = CopilotClient()
	discover = getattr(client, "list_models", None)
	if discover is None:
		raise LLMUnavailable("model list unavailable")
	try:
		await client.start()
		models = await discover()
		return [model_id for model in models if (model_id := _model_id(model))]
	finally:
		try:
			await client.stop()
		except Exception:
			pass


def list_models() -> list[str]:
	"""Return reachable Copilot model ids, normalising every SDK failure."""
	try:
		_show_runtime_preparation_notice()
		with _silence_sdk_logging():
			return asyncio.run(_list_models_async())
	except LLMUnavailable:
		raise
	except Exception as exc:
		raise LLMUnavailable(_error_reason(exc, context="model list unavailable"))


async def _complete_async(prompt: str, model: str, timeout_s: float) -> str:
	"""Drive the async SDK for a single completion and return the text."""
	if CopilotClient is None:
		raise LLMUnavailable("copilot sdk not installed")

	deadline = asyncio.get_running_loop().time() + max(timeout_s, 0.0)
	client = CopilotClient()
	try:
		# NOTE: auth is the Copilot CLI's stored OAuth login. We must NOT
		# set or forward GITHUB_TOKEN; an env token shadows that login.
		await _within_budget(client.start(), deadline, "runtime startup")
		session = await _within_budget(
			client.create_session(model=model), deadline, "inference"
		)
		for attempt in range(2):
			try:
				resp = await _within_budget(
					session.send_and_wait(prompt), deadline, "inference"
				)
				break
			except Exception as exc:
				if attempt == 1 or _is_auth_error(exc):
					raise
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
		_show_runtime_preparation_notice()
		with _silence_sdk_logging():
			return asyncio.run(_complete_async(prompt, model, timeout_s))
	except LLMUnavailable:
		raise
	except (asyncio.TimeoutError, TimeoutError) as exc:
		raise LLMUnavailable(_error_reason(exc))
	except Exception as exc:
		raise LLMUnavailable(_error_reason(exc))


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
