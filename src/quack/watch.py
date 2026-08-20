"""Background review orchestration for ``quack watch``.

Tier 2 currently runs at pre-push because the Copilot SDK costs roughly
9-15 seconds while commit time must stay under five seconds. Watch mode
dissolves that tradeoff: it notices working-tree changes, waits for a quiet
period, reviews while the developer is still working, and caches the result.
At commit time quack performs only a local hash comparison and file read, so
the already-computed review appears immediately without an LLM call.

A small standard-library polling loop is sufficient here: the debounce is 30
seconds by default, so event-level notifications would add a dependency
without improving developer-visible latency. The polling snapshot uses the
same directory prune list as test mapping.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import gitio, instructions, llmio, reviewcache, testmap, tier2
from .tier1 import Tier1Config
from .tier1 import redact as tier1_redact
from .tier1 import run as tier1_run

POLL_INTERVAL_S = 2.0


@dataclass(frozen=True)
class WatchResult:
	"""Display-safe outcome from one background review attempt."""

	files: int
	risk: str | None = None
	reason: str | None = None


def review_once(repo_root: str | Path, model: str | None = None) -> WatchResult:
	"""Review the staged delta, or tracked working changes when unstaged."""
	root = Path(repo_root)
	try:
		delta = gitio.staged_delta()
		if not delta.files:
			delta = gitio.working_delta()
		if not delta.files:
			return WatchResult(files=0, reason="no changes")

		findings = tier1_run(delta, Tier1Config())
		redacted = tier1_redact(delta, findings)
		plan = testmap.build_plan(delta, root=root)
		project_instructions = instructions.load(root)
		resolved_model = (
			model
			or os.environ.get("QUACK_MODEL")
			or llmio.default_model(kind="completion")
		)
		if not resolved_model:
			return WatchResult(files=len(delta.files), reason="no model configured")
		availability = llmio.availability_error()
		if availability:
			return WatchResult(files=len(delta.files), reason=availability)

		review = tier2.review(
			delta,
			findings,
			plan,
			model=resolved_model,
			project_instructions=project_instructions,
			timeout_s=llmio.default_timeout(),
		)
		if review is None:
			return WatchResult(files=len(delta.files), reason="AI analysis unavailable")

		payload = asdict(review)
		payload["model"] = resolved_model
		reviewcache.write(
			root,
			reviewcache.diff_hash(redacted.raw_diff),
			payload,
		)
		return WatchResult(files=len(delta.files), risk=review.risk)
	except Exception as exc:
		message = str(exc)[:160].replace("\n", " ")
		return WatchResult(
			files=0,
			reason=f"{type(exc).__name__}: {message}" if message else type(exc).__name__,
		)


def run(
	repo_root: str | Path,
	quiet_period_s: float,
	on_review: Callable[[WatchResult], None],
	*,
	poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
	"""Poll until interrupted, reviewing after each filesystem quiet period."""
	root = Path(repo_root)
	previous = snapshot(root)
	dirty = True
	last_change = time.monotonic()
	while True:
		time.sleep(poll_interval_s)
		current = snapshot(root)
		if current != previous:
			previous = current
			dirty = True
			last_change = time.monotonic()
		if dirty and time.monotonic() - last_change >= quiet_period_s:
			on_review(review_once(root))
			dirty = False


def snapshot(repo_root: str | Path) -> dict[str, tuple[int, int]]:
	"""Return a fail-open path-to-(mtime_ns,size) working-tree snapshot."""
	root = Path(repo_root)
	state: dict[str, tuple[int, int]] = {}
	try:
		for dirpath, dirnames, filenames in os.walk(root):
			dirnames[:] = [name for name in dirnames if name not in testmap.PRUNE_DIRS]
			for filename in filenames:
				path = Path(dirpath) / filename
				try:
					stat = path.stat()
					state[path.relative_to(root).as_posix()] = (
						stat.st_mtime_ns,
						stat.st_size,
					)
				except OSError:
					continue
	except OSError:
		return {}
	return state
