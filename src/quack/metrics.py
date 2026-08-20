"""Fail-open, local metrics persistence.

Privacy is non-negotiable: metrics must never contain file contents, diff text,
file paths, commit messages, repository names, or code. Callers may record only
counts, durations, bounded enums, and sanitized reason strings. Do not widen
this payload without explicitly reconsidering those constraints.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import reviewcache

MAX_BYTES = 5 * 1024 * 1024
_FILENAME = "metrics.jsonl"
_ALLOWED_KEYS = frozenset(
	{
		"ts",
		"command",
		"duration_ms",
		"files",
		"lines_added",
		"lines_removed",
		"tier1_findings",
		"blocked",
		"tests_mapped",
		"untested_sources",
		"review_cache",
		"risk",
		"exit",
		"target",
		"provider",
		"model",
		"tier2_risk",
		"tier2_failure",
		"agent_ran",
		"agent_failure",
		"failure",
	}
)
# Widening this allowlist requires explicitly reconsidering the metrics privacy constraint.
_NUMERIC_KEYS = frozenset(
	{
		"duration_ms",
		"files",
		"lines_added",
		"lines_removed",
		"tests_mapped",
		"untested_sources",
		"exit",
	}
)
_FAILURE_KEYS = frozenset({"tier2_failure", "agent_failure", "failure"})
_RISKS = frozenset({"low", "medium", "high"})
_PATH_RE = re.compile(
	r"(?:\b[A-Za-z]:[\\/][^\s]*|\\\\[^\s\\]+\\[^\s\\]+(?:\\[^\s]*)?|(?<![\w:/])\/[^\s]+)"
)
_TOKEN_RE = re.compile(
	r"(?:ghp_|github_pat_|gho_|ghs_|ghu_|xox|AKIA)[A-Za-z0-9_-]+|[A-Za-z0-9_+/=-]{32,}"
)


def metrics_path() -> Path:
	"""Return the metrics path beside the existing per-user review cache."""
	return reviewcache.cache_path().with_name(_FILENAME)


def timestamp() -> str:
	"""Return the current UTC time in ISO 8601 form."""
	return datetime.now(timezone.utc).isoformat()


def _sanitize_failure(value: Any) -> str | None:
	if value is None:
		return None
	if not isinstance(value, str):
		raise TypeError("failure reason must be a string or null")
	value = " ".join(value[:200].splitlines())
	value = _PATH_RE.sub("<path>", value)
	return _TOKEN_RE.sub("<redacted>", value)


def _sanitize_event(event: dict) -> dict[str, Any]:
	sanitized: dict[str, Any] = {}
	for key, value in event.items():
		if key not in _ALLOWED_KEYS:
			continue
		if key in _NUMERIC_KEYS:
			if type(value) is int:
				sanitized[key] = value
			continue
		if key == "tier1_findings":
			if not isinstance(value, dict):
				continue
			sanitized[key] = {
				name: count
				for name, count in value.items()
				if isinstance(name, str) and type(count) is int
			}
			continue
		if key == "risk":
			if value is None or value in _RISKS:
				sanitized[key] = value
			continue
		if key in _FAILURE_KEYS:
			try:
				sanitized[key] = _sanitize_failure(value)
			except Exception:
				continue
			continue
		sanitized[key] = value
	return sanitized


def log(event: dict) -> None:
	"""Append one JSON line, silently doing nothing on every failure."""
	try:
		path = metrics_path()
		encoded = json.dumps(_sanitize_event(event), separators=(",", ":")) + "\n"
		path.parent.mkdir(parents=True, exist_ok=True)
		if path.exists() and path.stat().st_size > MAX_BYTES:
			os.replace(path, path.with_name(f"{path.name}.1"))
		with path.open("a", encoding="utf-8") as stream:
			stream.write(encoded)
	except Exception:
		return


def read(*, path: Path | None = None) -> list[dict[str, Any]] | None:
	"""Return valid local events, or ``None`` when the file is unreadable."""
	try:
		events: list[dict[str, Any]] = []
		with (path or metrics_path()).open(encoding="utf-8") as stream:
			for line in stream:
				try:
					event = json.loads(line)
					if isinstance(event, dict):
						events.append(event)
				except Exception:
					continue
		return events
	except Exception:
		return None
