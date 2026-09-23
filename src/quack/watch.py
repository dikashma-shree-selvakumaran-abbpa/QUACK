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

from . import (
	gitio,
	instructions,
	llmio,
	metrics,
	render,
	reviewcache,
	sonar,
	sonar_debug,
	sonar_report,
	testmap,
	tier2,
)
from .sonar_report import SonarQubeReportResult
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
	sonar_scan: sonar.ScanResult | None = None
	sonar_report: SonarQubeReportResult | None = None


def review_once(
	repo_root: str | Path,
	model: str | None = None,
	*,
	debug: bool = False,
) -> WatchResult:
	"""Review the staged delta, or the complete working delta when changed."""
	started = time.perf_counter()
	with sonar_debug.scope(debug):
		result = _review_once(repo_root, model)
	try:
		event = {
			"ts": metrics.timestamp(),
			"command": "watch",
			"duration_ms": int((time.perf_counter() - started) * 1000),
			"files": result.files,
			"risk": result.risk,
			"failure": result.reason,
		}
		if result.sonar_report is not None:
			event.update(
				{
					"sonar_mcp_status": result.sonar_report.status,
					"sonar_mcp_duration_ms": int(
						result.sonar_report.duration_s * 1000
					),
					"sonar_mcp_failure": (
						result.sonar_report.reason
						if result.sonar_report.status != "passed"
						else None
					),
				}
			)
		if result.sonar_scan is not None:
			event.update(
				{
					"sonar_status": result.sonar_scan.status,
					"sonar_duration_ms": int(
						result.sonar_scan.duration_s * 1000
					),
					"sonar_failure": (
						result.sonar_scan.reason
						if result.sonar_scan.status != "passed"
						else None
					),
				}
			)
		metrics.log(event)
	except Exception:
		pass
	return result


def _review_once(repo_root: str | Path, model: str | None = None) -> WatchResult:
	root = Path(repo_root)
	sonar_scan: sonar.ScanResult | None = None
	sonar_result: SonarQubeReportResult | None = None
	try:
		# Watch must represent the working tree developers are looking at.
		# working_delta() includes staged, unstaged, and new non-ignored edits;
		# pre-commit path remains the only path that uses the exact index.
		delta = gitio.working_delta(root)
		if not delta.files:
			delta = gitio.staged_delta()
		if not delta.files:
			return WatchResult(files=0, reason="no changes")

		sonar_settings = sonar.configuration(root, staged=False)
		scan_started_at = time.time()
		sonar_scan = sonar.scan(
			delta,
			root,
			config=sonar_settings,
			snapshot_scope="working",
		)
		sonar_result = sonar_report.run(
			delta,
			root,
			source="watch",
			project_path=root,
			project_key=sonar_settings.project_key,
			branch=sonar_settings.branch,
			fresh=bool(sonar_scan and sonar_scan.status == "passed"),
			expected_analysis_id=(
				sonar_scan.analysis_id if sonar_scan else None
			),
			minimum_analysis_at=(
				scan_started_at
				if sonar_scan and sonar_scan.status == "passed"
				else None
			),
		)
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
			return WatchResult(
				files=len(delta.files),
				reason="no model configured",
				sonar_scan=sonar_scan,
				sonar_report=sonar_result,
			)
		availability = llmio.availability_error()
		if availability:
			return WatchResult(
				files=len(delta.files),
				reason=availability,
				sonar_scan=sonar_scan,
				sonar_report=sonar_result,
			)

		with render.thinking("reviewing changes..."):
			review, reason = tier2.review_with_reason(
				delta,
				findings,
				plan,
				model=resolved_model,
				project_instructions=project_instructions,
				timeout_s=llmio.default_timeout(),
			)
		if review is None:
			return WatchResult(
				files=len(delta.files),
				reason=reason or llmio.availability_error() or "AI analysis unavailable",
				sonar_scan=sonar_scan,
				sonar_report=sonar_result,
			)

		payload = asdict(review)
		payload["model"] = resolved_model
		reviewcache.write(
			root,
			reviewcache.diff_hash(redacted.raw_diff),
			payload,
		)
		return WatchResult(
			files=len(delta.files),
			risk=review.risk,
			sonar_scan=sonar_scan,
			sonar_report=sonar_result,
		)
	except Exception as exc:
		message = str(exc)[:160].replace("\n", " ")
		return WatchResult(
			files=0,
			reason=(
				f"{type(exc).__name__}: {message}"
				if message
				else type(exc).__name__
			),
			sonar_scan=sonar_scan,
			sonar_report=sonar_result,
		)


def run(
	repo_root: str | Path,
	quiet_period_s: float,
	on_review: Callable[[WatchResult], None],
	*,
	poll_interval_s: float = POLL_INTERVAL_S,
	debug: bool = False,
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
			on_review(review_once(root, debug=debug))
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
					relative = path.relative_to(root).as_posix()
					if sonar_report.is_report_file(relative):
						continue
					stat = path.stat()
					state[relative] = (
						stat.st_mtime_ns,
						stat.st_size,
					)
				except OSError:
					continue
	except OSError:
		return {}
	return state
