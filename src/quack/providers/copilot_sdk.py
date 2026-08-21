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
import json
import logging
import os
import re
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from .. import render
from ..llmio import LLMUnavailable

try:  # pragma: no cover - exercised via monkeypatch in tests
	from copilot import CopilotClient, ToolResult, define_tool
except ImportError:  # pragma: no cover - SDK is an optional runtime dep
	CopilotClient = None
	ToolResult = None
	define_tool = None

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
_NATIVE_FINAL_INSTRUCTION = (
	"After investigating the changes and gathering the relevant evidence, "
	"reply with ONLY the final JSON verdict: "
	"{summary, tests_run, failures, proposed_patch, proposed_new_tests}."
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


def _value(obj, name: str, default=None):
	"""Read SDK hook/event values across SDK versions and test doubles."""
	if isinstance(obj, dict):
		return obj.get(name, default)
	return getattr(obj, name, default)


def _tool_result(text: str, *, failure: bool = False):
	"""Build an SDK result without allowing handler failures to escape."""
	if ToolResult is None:
		return text
	return ToolResult(
		text_result_for_llm=text,
		result_type="failure" if failure else "success",
		error=text if failure else None,
	)


def _agent_tool_schema(name: str) -> dict:
	if name in ("read_file", "list_dir"):
		parameter = "path"
		description = "Repo-relative path."
	else:
		parameter = "project_or_paths"
		description = "csproj [--filter ...] OR .py paths."
	return {
		"type": "object",
		"properties": {parameter: {"type": "string", "description": description}},
		"required": [parameter],
		"additionalProperties": False,
	}


def _agent_args(input_data) -> dict:
	args = _value(input_data, "toolArgs", {})
	return args if isinstance(args, dict) else {}


def _validate_agent_call(root: Path, name: str, args: dict) -> str | None:
	"""Validate a native invocation before its handler is allowed to run."""
	from .. import agent

	if name in ("read_file", "list_dir"):
		path = args.get("path")
		if not isinstance(path, str) or agent._safe_path(root, path) is None:
			return "path is outside the repository or invalid"
		return None
	if name != "run_tests":
		return "unknown tool"
	spec = args.get("project_or_paths")
	if not isinstance(spec, str) or not spec.strip():
		return "no test target provided"
	tokens = spec.split()
	if any(token.endswith(".csproj") for token in tokens):
		if "--filter" in tokens:
			index = tokens.index("--filter")
			if len(tokens[:index]) != 1 or not tokens[index + 1 :]:
				return "expected exactly one .csproj path before --filter"
			value = " ".join(tokens[index + 1 :]).strip().strip('"')
			if not re.match(r'^[A-Za-z0-9_.~|&=!"\s-]+$', value):
				return "filter rejected (illegal characters)"
		elif len(tokens) != 1:
			return "expected exactly one .csproj path before --filter"
		if agent._safe_path(root, tokens[0]) is None:
			return "project is outside the repository"
		return None
	if not all(token.endswith(".py") for token in tokens):
		return "unrecognized target"
	if any(agent._safe_path(root, token) is None for token in tokens):
		return "path is outside the repository"
	return None


async def _run_agent_async(
	diff: str, repo_root: Path, model: str, timeout_s: float
) -> object:
	"""Run the investigation through SDK-native tools and one native turn."""
	from .. import agent

	if CopilotClient is None or define_tool is None:
		raise LLMUnavailable("copilot sdk not installed")
	root = Path(repo_root).resolve()
	started = asyncio.get_running_loop().time()
	deadline = started + min(max(timeout_s, 0.0), agent.WALL_CLOCK_S)
	invocations = 0
	run_tests_used = 0
	test_outputs: list[str] = []

	def pre_tool(input_data):
		nonlocal invocations, run_tests_used
		name = str(_value(input_data, "toolName", ""))
		args = _agent_args(input_data)
		# Enforcement moved from loop control to denial because the SDK owns
		# turn progression; every attempted invocation consumes an iteration.
		invocations += 1
		if invocations > agent.MAX_ITERATIONS:
			return {"permissionDecision": "deny", "permissionDecisionReason": "iteration budget exhausted"}
		if asyncio.get_running_loop().time() - started >= agent.WALL_CLOCK_S:
			return {"permissionDecision": "deny", "permissionDecisionReason": "wall-clock budget exhausted"}
		validation_error = _validate_agent_call(root, name, args)
		if validation_error:
			return {"permissionDecision": "deny", "permissionDecisionReason": validation_error}
		if name == "run_tests" and run_tests_used >= agent.MAX_RUN_TESTS:
			return {"permissionDecision": "deny", "permissionDecisionReason": "run_tests budget exhausted"}
		if name == "run_tests":
			run_tests_used += 1
		return {"permissionDecision": "allow"}

	def make_handler(name: str):
		def handler(args):
			try:
				if name == "read_file":
					result = agent._read_file(root, str(args.get("path", "")))
				elif name == "list_dir":
					result = agent._list_dir(root, str(args.get("path", "")))
				else:
					result = agent._run_tests(root, str(args.get("project_or_paths", "")))
					if result.startswith("exit_code="):
						test_outputs.append(result)
				return _tool_result(result, failure=result.startswith("error:"))
			except Exception:
				return _tool_result("error: tool execution failed", failure=True)
		return handler

	tools = []
	for name, description in (
		("read_file", "Return the first 300 lines of a repo file."),
		("list_dir", "List the entry names of a repo directory."),
		("run_tests", "Run only the permitted pytest or dotnet test target."),
	):
		tool = define_tool(name=name, description=description, handler=make_handler(name), skip_permission=True)
		tool.parameters = _agent_tool_schema(name)
		tools.append(tool)

	client = CopilotClient()
	try:
		await _within_budget(client.start(), deadline, "runtime startup")
		session = await _within_budget(
			client.create_session(
				model=model,
				tools=tools,
				# Permission approval is not the security boundary; pre_tool is
				# the authoritative validation and budget enforcement point.
				on_permission_request=lambda *_args, **_kwargs: {"kind": "approved"},
				hooks={"on_pre_tool_use": pre_tool},
			),
			deadline,
			"inference",
		)
		prompt = (
			agent.SYSTEM_PROMPT
			+ "\n\nAccumulated local changes (staged diff):\n\n"
			+ diff
			+ "\n\n"
			+ _NATIVE_FINAL_INSTRUCTION
		)
		response = await _within_budget(session.send_and_wait(prompt), deadline, "inference")
		content = _value(_value(response, "data"), "content")
		result = agent._parse_and_validate(content)
		if result is None:
			# Native turns have no OpenAI message history to retry; ask the same
			# session once for schema correction, preserving the one-retry contract.
			response = await _within_budget(session.send_and_wait(agent._RETRY_MESSAGE), deadline, "inference")
			content = _value(_value(response, "data"), "content")
			result = agent._parse_and_validate(content)
		return agent._finalize(result, "AI analysis unavailable", test_outputs)
	finally:
		try:
			await client.stop()
		except Exception:
			pass


def run_agent(diff: str, repo_root: Path, model: str, timeout_s: float = DEFAULT_TIMEOUT_S):
	"""Run the SDK-native agent, normalising every failure to LLMUnavailable."""
	try:
		_show_runtime_preparation_notice()
		with _silence_sdk_logging():
			return asyncio.run(_run_agent_async(diff, repo_root, model, timeout_s))
	except LLMUnavailable:
		raise
	except Exception as exc:
		raise LLMUnavailable(_error_reason(exc, context="agent unavailable")) from None


def chat(
	messages: list[dict],
	model: str,
	tools: list[dict] | None = None,
) -> dict:
	"""The SDK intentionally has no OpenAI-style chat tool-call surface."""
	raise LLMUnavailable("tool calling not supported on copilot_sdk provider")
