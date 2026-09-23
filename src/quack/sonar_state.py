"""Fail-open state for completed Sonar checks.

Sonar state is intentionally separate from the Tier 2 review cache. The review
cache is a small shared LRU; a scan result must not evict or collide with an
AI review from another repository. Snapshot digests also include the effective
scanner/configuration identity supplied by the staged caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

MAX_AGE_S = 24 * 60 * 60
MAX_ENTRIES = 40
MAX_FILE_SIZE = 1_000_000
_FILENAME = "sonar-state.json"


@dataclass(frozen=True)
class State:
	"""A validated Sonar result tied to one exact source snapshot."""

	digest: str
	scope: str
	repo_root: str
	project_key: str
	host_url: str
	branch: str | None
	status: str
	reason: str
	violation_count: int
	timestamp: float
	scan_status: str | None = None
	task_id: str | None = None
	analysis_id: str | None = None


def snapshot_digest(
	delta,
	*,
	scope: str,
	repo_root: str | Path,
	project_key: str,
	host_url: str,
	branch: str | None = None,
	config_identity: object | None = None,
) -> str:
	"""Hash the source delta and complete effective Sonar identity."""
	files = []
	for item in getattr(delta, "files", ()) or ():
		files.append(
			{
				"path": str(getattr(item, "path", "")),
				"status": str(getattr(item, "status", "")),
				"added": int(getattr(item, "added", 0) or 0),
				"removed": int(getattr(item, "removed", 0) or 0),
			}
		)
	payload = {
		"scope": scope,
		"repo_root": _repo_key(repo_root),
		"project_key": project_key,
		"host_url": host_url,
		"branch": branch,
		"config_identity": config_identity,
		"files": files,
		"raw_diff": str(getattr(delta, "raw_diff", "")),
	}
	encoded = json.dumps(
		payload, sort_keys=True, separators=(",", ":"), default=str
	)
	return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def path() -> Path:
	"""Return the independent per-user Sonar state path."""
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
		root = Path(base) if base else Path.home() / "AppData" / "Local"
	else:
		base = os.environ.get("XDG_DATA_HOME")
		root = Path(base) if base else Path.home() / ".local" / "share"
	return root / "quack" / _FILENAME


def read(
	repo_root: str | Path,
	digest: str,
	*,
	scope: str,
	project_key: str,
	host_url: str,
	branch: str | None = None,
	path_override: Path | None = None,
	now: float | None = None,
	max_age_s: float = MAX_AGE_S,
) -> State | None:
	"""Read a matching fresh state, treating every error as a cache miss."""
	try:
		state_path = path_override or path()
		if state_path.stat().st_size > MAX_FILE_SIZE:
			return None
		data = json.loads(state_path.read_text(encoding="utf-8"))
		entries = data.get("entries")
		if not isinstance(entries, list):
			return None
		current = time.time() if now is None else now
		repo_key = _repo_key(repo_root)
		for raw in entries:
			if not isinstance(raw, dict):
				continue
			if (
				raw.get("digest") != digest
				or raw.get("scope") != scope
				or raw.get("repo_root") != repo_key
				or raw.get("project_key") != project_key
				or raw.get("host_url") != host_url
				or raw.get("branch") != branch
			):
				continue
			timestamp = raw.get("timestamp")
			if (
				not isinstance(timestamp, (int, float))
				or isinstance(timestamp, bool)
				or not math.isfinite(timestamp)
			):
				return None
			if current - timestamp > max_age_s:
				return None
			if timestamp > current:
				return None
			count = raw.get("violation_count", 0)
			if not isinstance(count, int) or count < 0:
				return None
			status = raw.get("status")
			reason = raw.get("reason", "")
			scan_status = raw.get("scan_status")
			if (
				not isinstance(raw.get("digest"), str)
				or not isinstance(raw.get("scope"), str)
				or not isinstance(raw.get("repo_root"), str)
				or not isinstance(raw.get("project_key"), str)
				or not isinstance(raw.get("host_url"), str)
				or not isinstance(status, str)
				or not isinstance(reason, str)
				or status != "passed"
			):
				return None
			if scan_status not in (None, "passed"):
				return None
			task_id = raw.get("task_id")
			analysis_id = raw.get("analysis_id")
			if task_id is not None and not isinstance(task_id, str):
				return None
			if analysis_id is not None and not isinstance(analysis_id, str):
				return None
			return State(
				digest=digest,
				scope=scope,
				repo_root=repo_key,
				project_key=project_key,
				host_url=host_url,
				branch=branch,
				status=status,
				reason=reason,
				violation_count=count,
				timestamp=timestamp,
				scan_status=scan_status,
				task_id=task_id,
				analysis_id=analysis_id,
			)
	except Exception:
		return None
	return None


def write(
	repo_root: str | Path,
	state: State,
	*,
	path_override: Path | None = None,
) -> None:
	"""Persist one state record atomically and fail open on all I/O errors."""
	try:
		state_path = path_override or path()
		entries = _read_entries(state_path)
		if entries is None:
			return
		encoded_state = {
			"digest": state.digest,
			"scope": state.scope,
			"repo_root": _repo_key(repo_root),
			"project_key": state.project_key,
			"host_url": state.host_url,
			"branch": state.branch,
			"status": state.status,
			"reason": state.reason[:600],
			"violation_count": state.violation_count,
			"timestamp": state.timestamp,
			"scan_status": state.scan_status,
			"task_id": state.task_id,
			"analysis_id": state.analysis_id,
		}
		entries = [
			item
			for item in entries
			if not (
				item.get("digest") == state.digest
				and item.get("scope") == state.scope
				and item.get("repo_root") == encoded_state["repo_root"]
			)
		]
		entries.insert(0, encoded_state)
		entries = sorted(
			entries, key=lambda item: float(item.get("timestamp", 0)), reverse=True
		)[:MAX_ENTRIES]
		encoded = json.dumps({"entries": entries}, separators=(",", ":"))
		if len(encoded.encode("utf-8")) > MAX_FILE_SIZE:
			return
		state_path.parent.mkdir(parents=True, exist_ok=True)
		with tempfile.NamedTemporaryFile(
			mode="w",
			encoding="utf-8",
			dir=state_path.parent,
			prefix=f".{state_path.name}.",
			delete=False,
		) as handle:
			handle.write(encoded)
			temp_path = Path(handle.name)
		try:
			os.replace(temp_path, state_path)
		finally:
			temp_path.unlink(missing_ok=True)
	except Exception:
		return


def _read_entries(state_path: Path) -> list[dict] | None:
	try:
		if not state_path.exists():
			return []
		if state_path.stat().st_size > MAX_FILE_SIZE:
			return None
		data = json.loads(state_path.read_text(encoding="utf-8"))
		entries = data.get("entries", [])
		return [item for item in entries if isinstance(item, dict)] if isinstance(
			entries, list
		) else None
	except Exception:
		return None


def _repo_key(value: str | Path) -> str:
	return os.path.normcase(os.path.abspath(os.fspath(value)))
