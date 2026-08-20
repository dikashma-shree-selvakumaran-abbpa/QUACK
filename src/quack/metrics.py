"""Fail-open, local metrics persistence.

Privacy is non-negotiable: metrics must never contain file contents, diff text,
file paths, commit messages, repository names, or code. Callers may record only
counts, durations, bounded enums, and sanitized reason strings. Do not widen
this payload without explicitly reconsidering those constraints.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import reviewcache

MAX_BYTES = 5 * 1024 * 1024
_FILENAME = "metrics.jsonl"


def metrics_path() -> Path:
	"""Return the metrics path beside the existing per-user review cache."""
	return reviewcache.cache_path().with_name(_FILENAME)


def timestamp() -> str:
	"""Return the current UTC time in ISO 8601 form."""
	return datetime.now(timezone.utc).isoformat()


def log(event: dict) -> None:
	"""Append one JSON line, silently doing nothing on every failure."""
	try:
		path = metrics_path()
		encoded = json.dumps(event, separators=(",", ":")) + "\n"
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
