"""The ``quack agent`` pre-push loop.

A plain, inspectable tool-calling loop -- no agent frameworks. The model is
given three read-only tools (``read_file``, ``list_dir``, ``run_tests``) and
must INVESTIGATE the staged delta, then emit a single JSON verdict.

Safety invariants enforced here, not by the model:

* Path containment: ``read_file``/``list_dir`` only ever touch paths that
  resolve inside the repo root. Escapes via ``..`` or absolute paths return an
  error string; a tool never raises.
* Command whitelist: ``run_tests`` can only construct the two shapes in
  :mod:`quack.runio`. The C# ``--filter`` value is validated against a strict
  character class before any subprocess call, so shell metacharacters are
  rejected up front.
* Budgets: at most 8 iterations, at most 2 ``run_tests`` calls, and a 180s wall
  clock cap across the whole loop, then a final answer is forced from whatever
  evidence was gathered.

The loop never crashes on model misbehaviour: malformed final JSON is retried
once (the Tier 2 pattern) and otherwise degrades to a clear message.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import jsonparse, llmio, render, runio
from .llmio import LLMUnavailable

MAX_ITERATIONS = 8
MAX_RUN_TESTS = 2
WALL_CLOCK_S = 180.0
READ_FILE_MAX_LINES = 300
OUTPUT_TAIL_LINES = 80

# C# --filter values may only contain these characters (no shell metacharacters).
_FILTER_RE = re.compile(r'^[A-Za-z0-9_.~|&=!"\s-]+$')

SYSTEM_PROMPT = """You are a pre-push test agent operating in a developer's
repository. Goal: determine whether the accumulated local changes are
safe to push, by INVESTIGATING, not guessing.

You have tools: read_file, list_dir, run_tests. Method:
1. Form a hypothesis about what could break, from the diff.
2. Gather the minimum evidence: read the changed code and its most
   relevant caller or test. Do not read files without a stated reason.
3. Run the smallest test set that would confirm or refute the
   hypothesis. At most 2 run_tests calls.
4. If a test fails: diagnose the ROOT CAUSE (which line of the delta,
   why), not the symptom.
5. Produce the final JSON. If you propose a patch, it must be a
   minimal unified diff fixing the root cause, and if a changed
   behavior has no test, propose the missing test.

Hard rules: never request paths outside the repository; never propose
running anything except tests; if you cannot conclude within your
iteration budget, say what you verified, what remains unknown, and
what the developer should check manually. An honest "unverified" beats
a confident guess."""

_FINAL_INSTRUCTION = (
	"Stop investigating. Using only the evidence gathered so far, reply with "
	"ONLY the final JSON verdict: {summary, tests_run, failures, "
	"proposed_patch, proposed_new_tests}."
)

_RETRY_MESSAGE = (
	"Your previous reply was not valid JSON matching the schema "
	"{summary, tests_run, failures:[{test, diagnosis}], proposed_patch, "
	"proposed_new_tests}. Reply with ONLY the JSON, no other text."
)

TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "read_file",
			"description": "Return the first 300 lines of a repo file.",
			"parameters": {
				"type": "object",
				"properties": {
					"path": {
						"type": "string",
						"description": "Repo-relative path to read.",
					}
				},
				"required": ["path"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "list_dir",
			"description": "List the entry names of a repo directory.",
			"parameters": {
				"type": "object",
				"properties": {
					"path": {
						"type": "string",
						"description": "Repo-relative directory path.",
					}
				},
				"required": ["path"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "run_tests",
			"description": (
				"Run tests. For C#: a .csproj path plus optional "
				'--filter "<value>". For Python: one or more .py test '
				"file paths."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"project_or_paths": {
						"type": "string",
						"description": "csproj [--filter ...] OR .py paths.",
					}
				},
				"required": ["project_or_paths"],
			},
		},
	},
]


@dataclass
class AgentResult:
	"""The validated final verdict from the agent."""

	summary: str
	tests_run: list[str] = field(default_factory=list)
	failures: list[dict] = field(default_factory=list)
	proposed_patch: str | None = None
	proposed_new_tests: str | None = None


# ---------------------------------------------------------------------------
# The loop.
# ---------------------------------------------------------------------------


def run(
	diff: str,
	repo_root: Path,
	model: str,
	*,
	max_iterations: int = MAX_ITERATIONS,
	max_run_tests: int = MAX_RUN_TESTS,
	wall_clock_s: float = WALL_CLOCK_S,
) -> AgentResult:
	"""Investigate ``diff`` and return a final :class:`AgentResult`.

	Never raises; on any model failure it degrades to a clear message.
	"""
	root = Path(repo_root).resolve()
	messages: list[dict] = [
		{"role": "system", "content": SYSTEM_PROMPT},
		{
			"role": "user",
			"content": f"Accumulated local changes (staged diff):\n\n{diff}",
		},
	]

	run_tests_used = 0
	test_run_outputs: list[str] = []
	start = time.monotonic()

	for _ in range(max_iterations):
		if time.monotonic() - start > wall_clock_s:
			break
		try:
			message = llmio.chat(messages, model=model, tools=TOOLS)
		except LLMUnavailable as exc:
			return _unavailable(exc.reason)

		tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
		if not tool_calls:
			# The model chose to answer instead of calling a tool.
			result = _validate_with_retry(messages, message, model)
			return _finalize(result, "invalid final JSON", test_run_outputs)

		messages.append(message)
		for call in tool_calls:
			name, args = _parse_call(call)
			budget_left = max_run_tests - run_tests_used
			result_text, consumed = _dispatch(root, name, args, budget_left)
			run_tests_used += consumed
			if consumed:
				# Capture the tool's ground-truth output for later cross-check.
				test_run_outputs.append(result_text)
			messages.append(
				{
					"role": "tool",
					"tool_call_id": call.get("id", ""),
					"content": result_text,
				}
			)

	# Budget exhausted: force a final answer from the gathered evidence.
	messages.append({"role": "user", "content": _FINAL_INSTRUCTION})
	try:
		message = llmio.chat(messages, model=model, tools=None)
	except LLMUnavailable as exc:
		return _unavailable(exc.reason)
	result = _validate_with_retry(messages, message, model)
	return _finalize(result, "no conclusion reached", test_run_outputs)


_OVERRIDE_DIAGNOSIS = (
	"Non-zero exit code from run_tests; failure present in tool output but "
	"absent from the model's self-report."
)

# Matches ".NET" and pytest failure lines, e.g. "Failed Ns.Class.Method [12 ms]"
# or "FAILED tests/test_x.py::test_y".
_FAILED_TEST_RE = re.compile(r"(?:Failed|FAILED)\s+(\S+)")


def _finalize(
	result: AgentResult | None, fallback_reason: str, test_run_outputs: list[str]
) -> AgentResult:
	"""Resolve the final result and cross-check it against tool ground truth."""
	resolved = result if result is not None else _unavailable(fallback_reason)
	return _reconcile(resolved, test_run_outputs)


def _reconcile(result: AgentResult, test_run_outputs: list[str]) -> AgentResult:
	"""Override the verdict when tool exit codes contradict the model.

	The tool's exit_code is ground truth. If any run_tests call exited non-zero
	but the model reported no failures, synthesize failure entries from the
	tool output itself (deterministic, no extra LLM call) and annotate summary.
	"""
	failing_outputs = [
		output
		for output in test_run_outputs
		if (_tool_exit_code(output) or 0) != 0
	]
	if not failing_outputs or result.failures:
		return result

	failed_names: list[str] = []
	for output in failing_outputs:
		for name in _FAILED_TEST_RE.findall(output):
			if name not in failed_names:
				failed_names.append(name)
	if not failed_names:
		failed_names = ["<unknown test>"]

	result.failures = [
		{"test": name, "diagnosis": _OVERRIDE_DIAGNOSIS} for name in failed_names
	]
	note = (
		f"[verified] {len(failed_names)} test(s) failed per tool output, "
		f"overriding model's self-report."
	)
	result.summary = f"{note} {result.summary}".strip()
	return result


def _tool_exit_code(output: str) -> int | None:
	"""Parse the ``exit_code=N`` prefix emitted by :func:`_format_test_output`."""
	first_line = output.splitlines()[0] if output else ""
	match = re.match(r"exit_code=(-?\d+)", first_line.strip())
	return int(match.group(1)) if match else None


def _dispatch(
	root: Path, name: str, args: dict, run_tests_budget: int
) -> tuple[str, int]:
	"""Execute one tool call. Returns (result_text, run_tests_consumed)."""
	if name == "read_file":
		path = str(args.get("path", ""))
		render.metadata(f"-> read_file {path}")
		return _read_file(root, path), 0
	if name == "list_dir":
		path = str(args.get("path", ""))
		render.metadata(f"-> list_dir {path}")
		return _list_dir(root, path), 0
	if name == "run_tests":
		spec = str(args.get("project_or_paths", ""))
		render.metadata(f"-> run_tests {spec}")
		if run_tests_budget <= 0:
			return "error: run_tests budget exhausted (max 2 calls)", 0
		return _run_tests(root, spec), 1
	render.metadata(f"-> {name} (unknown)")
	return f"error: unknown tool {name!r}", 0


# ---------------------------------------------------------------------------
# Tools (containment + whitelist enforced here).
# ---------------------------------------------------------------------------


def _safe_path(root: Path, path: str) -> Path | None:
	"""Resolve ``path`` inside ``root`` or return None if it escapes."""
	try:
		candidate = (root / path).resolve()
		candidate.relative_to(root)
	except (ValueError, OSError):
		return None
	return candidate


def _read_file(root: Path, path: str) -> str:
	"""Return the first 300 lines of a contained file, or an error string."""
	target = _safe_path(root, path)
	if target is None:
		return f"error: path {path!r} is outside the repository"
	try:
		with target.open("r", encoding="utf-8", errors="replace") as handle:
			lines = []
			for index, line in enumerate(handle):
				if index >= READ_FILE_MAX_LINES:
					break
				lines.append(line.rstrip("\n"))
	except (OSError, ValueError) as exc:
		msg = str(exc)[:200].replace("\n", " ")
		return f"error: cannot read {path!r}: {type(exc).__name__}: {msg}"
	return "\n".join(lines)


def _list_dir(root: Path, path: str) -> str:
	"""Return sorted entry names of a contained directory, or an error string."""
	target = _safe_path(root, path)
	if target is None:
		return f"error: path {path!r} is outside the repository"
	try:
		names = sorted(entry.name for entry in target.iterdir())
	except (OSError, ValueError) as exc:
		msg = str(exc)[:200].replace("\n", " ")
		return f"error: cannot list {path!r}: {type(exc).__name__}: {msg}"
	return "\n".join(names)


def _run_tests(root: Path, spec: str) -> str:
	"""Parse ``spec`` into a whitelisted runner invocation and execute it."""
	tokens = spec.split()
	if not tokens:
		return "error: no test target provided"

	if any(token.endswith(".csproj") for token in tokens):
		return _run_dotnet(root, tokens)
	if all(token.endswith(".py") for token in tokens):
		return _run_pytest(root, tokens)
	return (
		"error: unrecognized target; expected a .csproj (with optional "
		"--filter) or .py test file paths"
	)


def _run_dotnet(root: Path, tokens: list[str]) -> str:
	"""Validate and run ``dotnet test`` for a contained .csproj."""
	if "--filter" in tokens:
		split = tokens.index("--filter")
		project_tokens = tokens[:split]
		test_filter = " ".join(tokens[split + 1 :]).strip().strip('"')
	else:
		project_tokens = tokens
		test_filter = None

	if len(project_tokens) != 1 or not project_tokens[0].endswith(".csproj"):
		return "error: expected exactly one .csproj path before --filter"

	if test_filter is not None and not _FILTER_RE.match(test_filter):
		return "error: filter rejected (illegal characters)"

	target = _safe_path(root, project_tokens[0])
	if target is None:
		return f"error: project {project_tokens[0]!r} is outside the repository"

	exit_code, output = runio.run_dotnet_test(str(target), test_filter)
	return _format_test_output(exit_code, output)


def _run_pytest(root: Path, tokens: list[str]) -> str:
	"""Validate and run ``pytest`` for contained .py test paths."""
	paths: list[str] = []
	for token in tokens:
		target = _safe_path(root, token)
		if target is None:
			return f"error: path {token!r} is outside the repository"
		paths.append(str(target))

	exit_code, output = runio.run_pytest(paths)
	return _format_test_output(exit_code, output)


def _format_test_output(exit_code: int, output: str) -> str:
	"""Exit code plus the last 80 lines of runner output."""
	tail = output.splitlines()[-OUTPUT_TAIL_LINES:]
	body = "\n".join(tail)
	if len(body) > 6000:
		body = body[-6000:]
	return f"exit_code={exit_code}\n{body}"


# ---------------------------------------------------------------------------
# Final answer validation (Tier 2 pattern: one retry, then degrade).
# ---------------------------------------------------------------------------


def _validate_with_retry(
	messages: list[dict], message: dict, model: str
) -> AgentResult | None:
	"""Validate ``message`` content; on failure retry once, then give up."""
	content = message.get("content") if isinstance(message, dict) else None
	result = _parse_and_validate(content)
	if result is not None:
		return result

	retry_messages = messages + [
		message if isinstance(message, dict) else {"role": "assistant", "content": ""},
		{"role": "user", "content": _RETRY_MESSAGE},
	]
	try:
		retry = llmio.chat(retry_messages, model=model, tools=None)
	except LLMUnavailable:
		return None
	retry_content = retry.get("content") if isinstance(retry, dict) else None
	return _parse_and_validate(retry_content)


def _parse_and_validate(raw: object) -> AgentResult | None:
	"""Parse ``raw`` JSON and validate the agent schema. Never raises."""
	if not isinstance(raw, str):
		return None
	data = jsonparse.loads(raw)
	if not isinstance(data, dict):
		return None

	summary = data.get("summary")
	if not isinstance(summary, str):
		return None

	tests_run = _normalize_tests_run(data.get("tests_run", []))
	if tests_run is None:
		return None

	failures_raw = data.get("failures", [])
	if not isinstance(failures_raw, list):
		return None
	failures: list[dict] = []
	for item in failures_raw:
		if not isinstance(item, dict):
			return None
		test = item.get("test")
		diagnosis = item.get("diagnosis")
		if not isinstance(test, str) or not isinstance(diagnosis, str):
			return None
		failures.append({"test": test, "diagnosis": diagnosis})

	patch = data.get("proposed_patch")
	if patch is not None and not isinstance(patch, str):
		return None

	new_tests = _normalize_new_tests(data.get("proposed_new_tests"))

	return AgentResult(
		summary=summary,
		tests_run=tests_run,
		failures=failures,
		proposed_patch=patch,
		proposed_new_tests=new_tests,
	)


def _normalize_tests_run(value: object) -> list[str] | None:
	"""Accept a list of strings OR dicts with a ``test``/``path`` key.

	Returns a flat list of strings, or ``None`` if the shape is unusable.
	"""
	if not isinstance(value, list):
		return None
	result: list[str] = []
	for item in value:
		if isinstance(item, str):
			result.append(item)
		elif isinstance(item, dict):
			label = item.get("test") or item.get("path") or item.get("name")
			if isinstance(label, str):
				result.append(label)
			else:
				return None
		else:
			return None
	return result


def _normalize_new_tests(value: object) -> str | None:
	"""Coerce proposed new tests into a single display string, or ``None``.

	Accepts ``None``, a string, or a list of strings / dicts with
	``description``/``code`` keys. Dict items render as ``"description: code"``;
	items are newline-separated. Never raises.
	"""
	if value is None:
		return None
	if isinstance(value, str):
		return value or None
	if not isinstance(value, list):
		return None

	parts: list[str] = []
	for item in value:
		if isinstance(item, str):
			parts.append(item)
		elif isinstance(item, dict):
			description = item.get("description")
			code = item.get("code")
			if isinstance(description, str) and isinstance(code, str):
				parts.append(f"{description}: {code}")
			elif isinstance(code, str):
				parts.append(code)
			elif isinstance(description, str):
				parts.append(description)
	return "\n".join(parts) if parts else None


def _parse_call(call: dict) -> tuple[str, dict]:
	"""Extract (name, arguments dict) from a tool_call, tolerating bad JSON."""
	function = call.get("function", {}) if isinstance(call, dict) else {}
	name = function.get("name", "")
	raw_args = function.get("arguments", "{}")
	try:
		args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
	except (ValueError, TypeError):
		args = {}
	if not isinstance(args, dict):
		args = {}
	return name, args


def _unavailable(reason: str) -> AgentResult:
	"""A graceful, honest fallback verdict when no conclusion was reached."""
	return AgentResult(
		summary=f"AI analysis unavailable: {reason}. "
		f"Verify the staged changes manually before pushing.",
	)
