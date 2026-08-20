"""Fail-open persistence for background Tier 2 review results.

Cache access runs in the commit hook, so every filesystem, decoding, and
validation failure is treated as a miss. Writes are atomic and best-effort.
No cache operation may block a commit by raising an exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_ENTRIES = 20
MAX_AGE_S = 24 * 60 * 60
MAX_FILE_SIZE = 1_000_000
REPLACE_ATTEMPTS = 3
REPLACE_RETRY_DELAY_S = 0.01
_CACHE_FILENAME = "review-cache.json"


@dataclass(frozen=True)
class CacheEntry:
	"""One fresh, validated cached review."""

	diff_hash: str
	timestamp: float
	repo_root: str
	review_payload: dict


def diff_hash(redacted_diff: str) -> str:
	"""Return the stable SHA-256 key for redacted diff text."""
	return hashlib.sha256(redacted_diff.encode("utf-8")).hexdigest()


def cache_path() -> Path:
	"""Return the platform-appropriate per-user review cache path."""
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
		root = Path(base) if base else Path.home() / "AppData" / "Local"
	else:
		base = os.environ.get("XDG_DATA_HOME")
		root = Path(base) if base else Path.home() / ".local" / "share"
	return root / "quack" / _CACHE_FILENAME


def read(
	repo_root: str | Path,
	digest: str,
	*,
	path: Path | None = None,
	now: float | None = None,
	max_age_s: float = MAX_AGE_S,
) -> CacheEntry | None:
	"""Return a matching fresh entry, or ``None`` on a miss or any error."""
	try:
		cache_file = path or cache_path()
		if cache_file.stat().st_size > MAX_FILE_SIZE:
			return None
		data = json.loads(cache_file.read_text(encoding="utf-8"))
		entries = data.get("entries")
		if not isinstance(entries, list):
			return None
		repo_key = _repo_key(repo_root)
		current_time = time.time() if now is None else now
		for raw in entries:
			if not isinstance(raw, dict):
				continue
			if raw.get("repo_root") != repo_key or raw.get("diff_hash") != digest:
				continue
			timestamp = float(raw["timestamp"])
			payload = raw.get("review_payload")
			if current_time - timestamp > max_age_s or not isinstance(payload, dict):
				return None
			return CacheEntry(digest, timestamp, repo_key, payload)
	except Exception:
		return None
	return None


def write(
	repo_root: str | Path,
	digest: str,
	review_payload: dict,
	*,
	path: Path | None = None,
	timestamp: float | None = None,
	max_entries: int = MAX_ENTRIES,
) -> None:
	"""Store a review atomically, silently doing nothing on any error."""
	try:
		cache_file = path or cache_path()
		repo_key = _repo_key(repo_root)
		stored_at = time.time() if timestamp is None else timestamp
		entries = _read_entries_for_write(cache_file)
		if entries is None:
			return
		entries = [
			entry
			for entry in entries
			if not (
				entry.get("repo_root") == repo_key
				and entry.get("diff_hash") == digest
			)
		]
		entries.append(
			{
				"diff_hash": digest,
				"timestamp": stored_at,
				"repo_root": repo_key,
				"review_payload": review_payload,
			}
		)
		entries = sorted(
			entries, key=lambda entry: float(entry.get("timestamp", 0)), reverse=True
		)[:max_entries]
		encoded = _encode_bounded(entries)
		if encoded is None:
			return
		cache_file.parent.mkdir(parents=True, exist_ok=True)
		with tempfile.NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=cache_file.parent,
			prefix=f".{cache_file.name}.",
			delete=False,
		) as temporary:
			temporary.write(encoded)
			temporary_path = Path(temporary.name)
		try:
			_replace_with_retry(temporary_path, cache_file)
		finally:
			try:
				temporary_path.unlink(missing_ok=True)
			except Exception:
				pass
	except Exception:
		return


def _repo_key(repo_root: str | Path) -> str:
	return os.path.normcase(os.path.abspath(os.fspath(repo_root)))


def _replace_with_retry(source: Path, destination: Path) -> None:
	for attempt in range(REPLACE_ATTEMPTS):
		try:
			os.replace(source, destination)
			return
		except PermissionError:
			if attempt == REPLACE_ATTEMPTS - 1:
				raise
			time.sleep(REPLACE_RETRY_DELAY_S)


def _read_entries_for_write(path: Path) -> list[dict] | None:
	try:
		if path.stat().st_size > MAX_FILE_SIZE:
			return None
		data = json.loads(path.read_text(encoding="utf-8"))
		entries = data.get("entries", [])
		if isinstance(entries, list):
			return [entry for entry in entries if isinstance(entry, dict)]
		return None
	except FileNotFoundError:
		return []
	except Exception:
		return None


def _encode_bounded(entries: list[dict]) -> str | None:
	remaining = list(entries)
	while remaining:
		encoded = json.dumps({"entries": remaining}, separators=(",", ":"))
		if len(encoded.encode("utf-8")) <= MAX_FILE_SIZE:
			return encoded
		remaining.pop()
	return json.dumps({"entries": []})
