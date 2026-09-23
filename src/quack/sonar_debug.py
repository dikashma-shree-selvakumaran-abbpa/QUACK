"""Bounded, redacted diagnostics for local Sonar and MCP calls."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from typing import Iterator

DEBUG_ENV = "QUACK_SONAR_DEBUG"
MAX_DEBUG_CHARS = 300_000
_TOKEN_PREFIX_RE = re.compile(
	r"(?:ghp_|github_pat_|gho_|ghs_|ghu_|xox|AKIA)[A-Za-z0-9_-]+"
)


def enabled() -> bool:
	"""Return whether verbose Sonar diagnostics are enabled."""
	return os.environ.get(DEBUG_ENV, "").strip().lower() not in {
		"",
		"0",
		"false",
		"no",
		"off",
	}


@contextmanager
def scope(active: bool) -> Iterator[None]:
	"""Enable diagnostics for one Watch call and restore the caller's state."""
	if not active:
		yield
		return
	previous = os.environ.get(DEBUG_ENV)
	os.environ[DEBUG_ENV] = "1"
	try:
		yield
	finally:
		if previous is None:
			os.environ.pop(DEBUG_ENV, None)
		else:
			os.environ[DEBUG_ENV] = previous


def emit(title: str, fields: dict[str, object], *, secrets=()) -> None:
	"""Write one bounded diagnostic record to stderr when debugging is enabled."""
	if not enabled():
		return
	secret_values = tuple(
		value
		for value in (
			*secrets,
			os.environ.get("SONARQUBE_TOKEN", ""),
			os.environ.get("SQ_TOKEN", ""),
			os.environ.get("SONAR_TOKEN", ""),
		)
		if isinstance(value, str) and value
	)
	lines = [f"[debug] {title}"]
	for key, value in fields.items():
		if isinstance(value, str):
			text = value
		else:
			try:
				text = json.dumps(
					value,
					ensure_ascii=False,
					separators=(",", ":"),
					sort_keys=True,
				)
			except (TypeError, ValueError):
				text = repr(value)
		lines.append(f"  {key}={redact(text, secret_values)}")
	sys.stderr.write("\n".join(lines) + "\n")


def redact(value: str, secrets=()) -> str:
	"""Redact configured tokens and recognizable token prefixes."""
	text = value
	for secret in sorted(set(secrets), key=len, reverse=True):
		text = text.replace(secret, "<redacted>")
	text = _TOKEN_PREFIX_RE.sub("<redacted>", text)
	if len(text) > MAX_DEBUG_CHARS:
		text = (
			text[:MAX_DEBUG_CHARS]
			+ f"\n<debug output truncated at {MAX_DEBUG_CHARS} characters>"
		)
	return text
