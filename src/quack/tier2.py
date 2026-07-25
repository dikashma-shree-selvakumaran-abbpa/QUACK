"""Tier 2 orchestration: build the prompt, call the model, validate output.

This module is pure orchestration. The only I/O it performs is delegated to
:mod:`quack.llmio`. It takes data in (a redacted delta, findings, a test plan)
and returns a :class:`ReviewResult` or ``None`` out, so it is unit-testable
without git or the network by monkeypatching ``llmio.complete``.

FAIL-OPEN contract: :func:`review` never raises. On any model error, invalid
JSON (twice), or schema mismatch, it returns ``None`` and the caller must
treat that as "skip the AI section, do not crash".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import llmio, testmap, tier1
from .delta import StagedDelta
from .llmio import LLMUnavailable
from .tier1 import Finding

REVIEWER_SYSTEM_PROMPT = """You are a pre-commit reviewer embedded in a git hook. You see a
staged diff, the list of changed files, and a heuristic test plan.
Your ONLY output is a JSON object with this exact schema:
{"risk": "low"|"medium"|"high",
 "reasons": [up to 3 short strings, each naming a concrete location],
 "tests_to_run": [test file paths, minimal set, ordered by relevance],
 "missing_tests": [source paths that change behavior but have no test],
 "one_liner": one sentence a developer reads in 2 seconds}

Judgment rules:
- You are judging THIS DELTA ONLY, not the codebase. Do not comment on
  pre-existing code visible in context lines.
- risk=high only for: behavior changes on error/money/auth/data paths,
  concurrency changes, off-by-one/boundary edits, deleted validations,
  or changed public contracts. risk=low for renames, comments, logging,
  formatting, test-only changes.
- reasons must be specific (name a concrete location) — never generic
  advice like "consider adding error handling".
- tests_to_run: start from the provided test plan; add or reorder only
  with a concrete reason; keep it minimal.
- If the delta is genuinely fine, say so: risk low, empty reasons,
  one_liner "Looks safe: <why in five words>". Do not invent findings
  to appear useful.
- Never include the diff content, secrets, or file bodies in output."""

_RETRY_MESSAGE = (
	"Your previous reply was not valid JSON matching the schema. "
	"Reply with ONLY the JSON, no other text."
)


@dataclass
class ReviewResult:
	"""The validated Tier 2 verdict."""

	risk: str
	reasons: list[str] = field(default_factory=list)
	tests_to_run: list[str] = field(default_factory=list)
	missing_tests: list[str] = field(default_factory=list)
	one_liner: str = ""


def build_messages(
	delta: StagedDelta,
	findings: list[Finding],
	test_plan: testmap.TestPlan,
) -> list[dict]:
	"""Build the chat messages from a REDACTED delta, files, and test plan."""
	redacted = tier1.redact(delta, findings)

	file_list = "\n".join(
		f"- {f.path} ({f.status}, +{f.added}/-{f.removed})"
		for f in redacted.files
	)
	runner_commands = "\n".join(f"- {cmd}" for cmd in test_plan.runner_commands)
	untested = "\n".join(f"- {src}" for src in test_plan.untested_sources)

	user_content = (
		"Changed files:\n"
		f"{file_list or '(none)'}\n\n"
		"Heuristic test plan — runner commands:\n"
		f"{runner_commands or '(none)'}\n\n"
		"Heuristic test plan — sources with no test found:\n"
		f"{untested or '(none)'}\n\n"
		"Staged diff (redacted):\n"
		f"{redacted.raw_diff}"
	)

	return [
		{"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
		{"role": "user", "content": user_content},
	]


def review(
	delta: StagedDelta,
	findings: list[Finding],
	test_plan: testmap.TestPlan,
	model: str,
	timeout_s: float = 6.0,
) -> ReviewResult | None:
	"""Run the Tier 2 review. Returns a ReviewResult or None (fail-open).

	Calls the model, parses and validates JSON. On invalid output it retries
	once with an error-correction message. On a second failure, or on
	:class:`LLMUnavailable`, returns ``None``.
	"""
	messages = build_messages(delta, findings, test_plan)

	try:
		raw = llmio.complete(messages, model=model, timeout_s=timeout_s)
	except LLMUnavailable:
		return None

	result = _parse_and_validate(raw)
	if result is not None:
		return result

	retry_messages = messages + [
		{"role": "assistant", "content": raw},
		{"role": "user", "content": _RETRY_MESSAGE},
	]
	try:
		raw_retry = llmio.complete(
			retry_messages, model=model, timeout_s=timeout_s
		)
	except LLMUnavailable:
		return None

	return _parse_and_validate(raw_retry)


def _parse_and_validate(raw: str) -> ReviewResult | None:
	"""Parse ``raw`` as JSON and validate it against the schema.

	Returns a :class:`ReviewResult` on success, ``None`` on any parse or
	schema failure. Never raises.
	"""
	try:
		data = json.loads(raw)
	except (ValueError, TypeError):
		return None

	if not isinstance(data, dict):
		return None

	risk = data.get("risk")
	if risk not in ("low", "medium", "high"):
		return None

	reasons = data.get("reasons")
	if not _is_str_list(reasons) or len(reasons) > 3:
		return None

	tests_to_run = data.get("tests_to_run")
	if not _is_str_list(tests_to_run):
		return None

	missing_tests = data.get("missing_tests")
	if not _is_str_list(missing_tests):
		return None

	one_liner = data.get("one_liner")
	if not isinstance(one_liner, str):
		return None

	return ReviewResult(
		risk=risk,
		reasons=reasons,
		tests_to_run=tests_to_run,
		missing_tests=missing_tests,
		one_liner=one_liner,
	)


def _is_str_list(value: object) -> bool:
	return isinstance(value, list) and all(isinstance(x, str) for x in value)
