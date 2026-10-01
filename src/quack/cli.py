"""quack command-line interface.

Subcommands:
	check   the hook entry (Tier 1 + Tier 2 orchestration)
	agent   agentic pre-push loop (stub)
	model   model/config utilities (stub)
	install wire quack into .pre-commit-config.yaml and run `pre-commit install`
	init    install hooks and scaffold repo-scoped Sonar Copilot skills/agent
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from statistics import median

import click
import yaml

from . import (
	__version__,
	agent as agent_mod,
	gitio,
	gitleaks,
	instructions,
	llmio,
	metrics as metrics_mod,
	render,
	reviewcache,
	sonar,
	sonar_cli,
	sonar_state,
	templates,
	testmap,
	tier2,
	watch as watch_mod,
)
from .tier1 import Tier1Config
from .tier1 import allowlisted_locations
from .tier1 import redact as tier1_redact
from .tier1 import run as tier1_run
from .tier1 import should_block

QUACK_REPO_URL = "https://github.com/ABB-AU-PCP/QUACK"
_LEGACY_QUACK_REPO_URL = (
	"https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK"
)


def _version_string() -> str:
	"""Version plus build provenance, so a stale frozen exe is identifiable."""
	try:
		from ._build_info import BUILD_COMMIT, BUILD_DATE
	except ImportError:
		return __version__
	if BUILD_COMMIT == "source":
		return f"{__version__} (source)"
	date = BUILD_DATE.split("T")[0] if BUILD_DATE else "unknown"
	return f"{__version__} (build {BUILD_COMMIT}, {date})"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(_version_string(), prog_name="quack")
def main() -> None:
	"""quack: an AI-assisted pre-commit quality hook."""


@main.command()
@click.option(
	"--json",
	"as_json",
	is_flag=True,
	default=False,
	help="Emit machine-readable verdict as JSON and suppress terminal rendering.",
)
def check(as_json: bool) -> None:
	"""Run the pre-commit quality checks on staged changes.

	Commit time runs local Tier 1 checks (plus gitleaks when installed), test
	guidance, and an optional local SonarQube analysis with HTTPS Web API
	verification. Unresolved SonarQube quality gates block the commit when
	verified against the current analysis; unavailable SonarQube integrations
	remain fail-open. All AI analysis runs at pre-push via ``quack agent``.
	"""
	# Measures in-process work only, excluding Python interpreter startup.
	started = time.perf_counter()
	delta = gitio.staged_delta()
	root = gitio.repo_root() or os.getcwd()
	if not delta.files:
		if as_json:
			payload = {
				"schemaVersion": 1,
				"repository": root,
				"blocked": False,
				"findings": [],
				"testGuidance": [],
				"aiReview": None,
				"aiError": None,
			}
			click.echo(json.dumps(payload, separators=(",", ":")))
		else:
			render.clean("nothing staged")
		_log_check_metrics(started, delta, exit_code=0)
		sys.exit(0)

	# Tier 1: deterministic checks (offline, always run).
	redaction_findings = tier1_run(delta, Tier1Config())
	findings = redaction_findings

	# Optional power mode: layer gitleaks' rules on top when it is installed.
	# Fully fail-open -- returns [] when gitleaks is unavailable or errors.
	if not os.environ.get("QUACK_DISABLE_GITLEAKS"):
		external = gitleaks.scan_staged(os.getcwd())
		# Honour the same inline allowlist quack's built-ins use, so one
		# `# quack: allow` marker suppresses gitleaks on that line too.
		external = gitleaks.filter_allowlisted(
			external, allowlisted_locations(delta)
		)
		findings = gitleaks.merge(findings, external)

	blocked = should_block(findings, block_on=("secrets", "merge_markers"))

	json_findings = [
		{
			"severity": f.severity,
			"source": "gitleaks" if f.message.startswith("gitleaks:") else getattr(f, "source", "quack"),
			"rule": f.message.removeprefix("gitleaks: ") if f.message.startswith("gitleaks:") else f.check,
			"file": f.path,
			"line": f.line,
			"message": f.message,
		}
		for f in findings
	]

	if blocked:
		# Only Tier 1 governs the exit code. Show findings + BLOCKED banner.
		if as_json:
			payload = {
				"schemaVersion": 1,
				"repository": root,
				"blocked": True,
				"findings": json_findings,
				"testGuidance": [],
				"aiReview": None,
				"aiError": None,
			}
			click.echo(json.dumps(payload, separators=(",", ":")))
		else:
			render.report(
				files=len(delta.files),
				added=delta.total_added,
				removed=delta.total_removed,
				findings=findings,
				plan=None,
				ai=None,
				blocked=True,
				duration=time.perf_counter() - started,
			)
		_log_check_metrics(started, delta, findings=findings, blocked=True, exit_code=1)
		sys.exit(1)

	root = gitio.repo_root() or os.getcwd()

	sonar_result, sonar_cli_result, _sonar_cache_hit = _run_staged_sonar_check(
		delta, root
	)
	blocked = blocked or bool(
		getattr(sonar_cli_result, "blocks_commit", False)
	)

	# Test guidance (only worth computing on an unblocked commit).
	plan = testmap.build_plan(delta)
	json_test_guidance = []
	if plan is not None:
		for mapping in getattr(plan, "mappings", []):
			if mapping.tests:
				json_test_guidance.append(
					{
						"sourceFile": mapping.source,
						"testOrCommand": ", ".join(mapping.tests),
						"status": "mapped",
						"recommendation": "run mapped tests",
					}
				)
		for source in getattr(plan, "untested_sources", []):
			json_test_guidance.append(
				{
					"sourceFile": source,
					"testOrCommand": "",
					"status": "untested",
					"recommendation": "no tests found",
				}
			)

	# Commit time is fully local: hash the same deterministically redacted diff
	# used by watch mode and perform one fail-open cache-file lookup. A miss must
	# never fall back to a provider call.
	redacted = tier1_redact(delta, redaction_findings)
	entry = reviewcache.read(root, reviewcache.diff_hash(redacted.raw_diff))
	cached_review = None
	cached_model = ""
	cache_note = None
	if entry is not None:
		cached_review, cached_model = _cached_review(entry.review_payload)
		if cached_review is not None:
			cache_note = (
				f"(reviewed {_format_age(entry.timestamp)} ago by quack watch)"
			)
	if cached_review is None:
		ai = (
			"skipped",
			"AI review: not reviewed yet - run `quack watch` to review in the background",
		)
	else:
		ai = cached_review

	if as_json:
		ai_review = None
		ai_error = None
		if cached_review is not None and entry is not None:
			ai_review = {
				"available": True,
				"model": cached_model,
				"risk": cached_review.risk,
				"summary": cached_review.one_liner,
				"reasons": cached_review.reasons,
				"testsToRun": cached_review.tests_to_run,
				"missingTests": cached_review.missing_tests,
				"reviewedAtUtc": datetime.fromtimestamp(entry.timestamp, tz=timezone.utc).isoformat(),
			}
		else:
			ai_error = ai[1] if isinstance(ai, tuple) and len(ai) > 1 else "AI analysis unavailable"

		payload = {
			"schemaVersion": 1,
			"repository": root,
			"blocked": blocked,
			"findings": json_findings,
			"testGuidance": json_test_guidance,
			"aiReview": ai_review,
			"aiError": ai_error,
		}
		click.echo(json.dumps(payload, separators=(",", ":")))
	else:
		render.report(
			files=len(delta.files),
			added=delta.total_added,
			removed=delta.total_removed,
			findings=findings,
			plan=plan,
			ai=ai,
			sonar=sonar_result,
			sonar_cli=sonar_cli_result,
			model=cached_model,
			ai_note=cache_note,
			blocked=blocked,
			duration=time.perf_counter() - started,
		)
	_log_check_metrics(
		started,
		delta,
		findings=findings,
		plan=plan,
		cache_hit=cached_review is not None,
		risk=cached_review.risk if cached_review is not None else None,
		sonar_result=sonar_result,
		sonar_cli_result=sonar_cli_result,
		blocked=blocked,
		exit_code=1 if blocked else 0,
	)
	sys.exit(1 if blocked else 0)


def _run_staged_sonar_check(delta, root):
	"""Check the exact index and revalidate any locally cached server result."""
	settings = sonar.configuration(root, staged=True)
	identity = sonar.effective_identity(root, settings=settings)
	digest = sonar_state.snapshot_digest(
		delta,
		scope="staged",
		repo_root=root,
		project_key=settings.project_key,
		host_url=settings.host_url,
		branch=settings.branch,
		config_identity=identity,
	)
	cached = sonar_state.read(
		root,
		digest,
		scope="staged",
		project_key=settings.project_key,
		host_url=settings.host_url,
		branch=settings.branch,
	)
	if cached is not None:
		scan_result = sonar.ScanResult(
			status="passed",
			reason="reused completed staged analysis",
			duration_s=0.0,
			dashboard_url=(
				f"{settings.host_url}/dashboard?id={settings.project_key}"
			),
			project_key=settings.project_key,
			branch=settings.branch,
			task_id=cached.task_id,
			analysis_id=cached.analysis_id,
			confirmed=True,
		)
		report = sonar_cli.run(
			delta,
			root,
			source="pre-commit",
			project_key=settings.project_key,
			host_url=settings.host_url,
			branch=settings.branch,
			expected_analysis_id=cached.analysis_id,
		)
		correlated = (
			cached.analysis_id is not None
			and report is not None
			and getattr(report, "status", None) == "passed"
			and getattr(report, "project_key", None) == settings.project_key
			and getattr(report, "branch", None) == settings.branch
			and getattr(report, "analysis_id", None) == cached.analysis_id
		)
		report = _mark_sonar_cache_result(report, correlated)
		if correlated:
			return scan_result, report, True
		# Watch or another branch upload may have replaced the latest analysis.
		# Re-scan the exact index instead of trusting or failing open on stale data.

	scan_result = sonar.scan(delta, root)
	scan_uploaded = bool(getattr(scan_result, "uploaded", False))
	scan_ok = (
		(
			getattr(scan_result, "status", None) == "passed"
			and getattr(scan_result, "confirmed", False)
		)
		or scan_uploaded
	)
	analysis_floor = None
	if scan_ok and scan_result is not None:
		analysis_floor = (
			time.time() - max(float(getattr(scan_result, "duration_s", 0.0)), 0.0) - 30
		)
	report = sonar_cli.run(
		delta,
		root,
		source="pre-commit",
		project_key=settings.project_key,
		host_url=settings.host_url,
		branch=settings.branch,
		minimum_analysis_at=analysis_floor,
	)
	report = _mark_sonar_fresh(report, scan_ok)
	if (
		scan_ok
		and report is not None
		and report.status == "passed"
	):
		sonar_state.write(
			root,
			sonar_state.State(
				digest=digest,
				scope="staged",
				repo_root=str(root),
				project_key=settings.project_key,
				host_url=settings.host_url,
				branch=settings.branch,
				status=report.status,
				reason=report.reason,
				violation_count=report.violation_count,
				timestamp=time.time(),
				scan_status=getattr(scan_result, "status", None),
				task_id=getattr(scan_result, "task_id", None),
				analysis_id=getattr(scan_result, "analysis_id", None),
			),
		)
	return scan_result, report, False


def _mark_sonar_cache_result(result, correlated: bool):
	"""Never let an old cached violation count become a commit blocker."""
	if result is None:
		return None
	if hasattr(result, "fresh"):
		updated = replace(result, fresh=correlated)
		if hasattr(updated, "correlated"):
			updated = replace(updated, correlated=correlated)
		if not correlated and getattr(updated, "reason", ""):
			reason = (
				f"{updated.reason}; current Sonar API result is unverified for the "
				"cached staged analysis"
			)
			updated = replace(updated, reason=reason[:600])
		return updated
	return result


def _mark_sonar_fresh(result, fresh: bool):
	if result is None:
		return None
	if hasattr(result, "fresh"):
		if result.fresh == fresh:
			return result
		if fresh:
			return replace(result, fresh=True)
		reason = getattr(result, "reason", "")
		if "unverified" not in reason.casefold():
			reason = (
				f"{reason}; current local Sonar scan is unavailable; "
				"Sonar API result is unverified"
			)
		return replace(result, fresh=False, reason=reason[:600])
	return result


def _log_check_metrics(
	started: float,
	delta,
	*,
	findings=(),
	plan=None,
	blocked: bool = False,
	cache_hit: bool = False,
	risk: str | None = None,
	sonar_result=None,
	sonar_cli_result=None,
	exit_code: int,
) -> None:
	try:
		event = {
			"ts": metrics_mod.timestamp(),
			"command": "check",
			"duration_ms": int((time.perf_counter() - started) * 1000),
			"files": len(delta.files),
			"lines_added": delta.total_added,
			"lines_removed": delta.total_removed,
			"tier1_findings": dict(Counter(item.check for item in findings)),
			"blocked": blocked,
			"tests_mapped": len(plan.runner_commands) if plan is not None else 0,
			"untested_sources": len(plan.untested_sources) if plan is not None else 0,
			"review_cache": "hit" if cache_hit else "miss",
			"risk": risk,
			"exit": exit_code,
		}
		if sonar_result is not None:
			event.update(
				{
					"sonar_status": sonar_result.status,
					"sonar_duration_ms": int(sonar_result.duration_s * 1000),
				}
			)
			if sonar_result.status != "passed":
				event["sonar_failure"] = sonar_result.reason
		if sonar_cli_result is not None:
			event.update(
				{
					"sonar_cli_status": sonar_cli_result.status,
					"sonar_cli_duration_ms": int(
						sonar_cli_result.duration_s * 1000
					),
				}
			)
			if sonar_cli_result.status != "passed":
				event["sonar_cli_failure"] = sonar_cli_result.reason
		metrics_mod.log(event)
	except Exception:
		pass


def _cached_review(payload: dict) -> tuple[tier2.ReviewResult | None, str]:
	"""Rehydrate a validated review payload without trusting cache contents."""
	try:
		return (
			tier2.ReviewResult(
				risk=payload["risk"],
				reasons=list(payload.get("reasons", [])),
				tests_to_run=list(payload.get("tests_to_run", [])),
				missing_tests=list(payload.get("missing_tests", [])),
				one_liner=str(payload.get("one_liner", "")),
				model_risk=str(payload.get("model_risk", "")),
				risk_basis=str(payload.get("risk_basis", "")),
			),
			str(payload.get("model", "")),
		)
	except (KeyError, TypeError, ValueError):
		return None, ""


def _format_age(timestamp: float) -> str:
	seconds = max(0, int(time.time() - timestamp))
	if seconds < 60:
		return f"{seconds} sec"
	if seconds < 3600:
		return f"{seconds // 60} min"
	return f"{seconds // 3600} hr"


@main.command()
@click.option(
	"--quiet-period",
	type=click.FloatRange(min=0.0),
	default=30.0,
	show_default=True,
	help="Seconds without file changes before reviewing.",
)
@click.option("--once", is_flag=True, help="Run one review immediately and exit.")
@click.option(
	"--json",
	"as_json",
	is_flag=True,
	default=False,
	help="Emit machine-readable review result as JSON (requires --once).",
)
@click.option(
	"--debug",
	is_flag=True,
	help="Print redacted Sonar CLI/API inputs and complete bounded outputs.",
)
def watch(quiet_period: float, once: bool, as_json: bool, debug: bool) -> None:
	"""Review changes in the background and cache the result for commits."""
	root = gitio.repo_root()
	if not root:
		if once and as_json:
			click.echo(
				json.dumps(
					{"schemaVersion": 1, "status": "error", "reason": "not a git repository"},
					separators=(",", ":"),
				)
			)
			return
		render.metadata("quack watch: not a git repository")
		return
	if once:
		res = watch_mod.review_once(root, quiet=as_json, debug=debug)
		if as_json:
			_emit_watch_json(res)
		else:
			_render_watch_result(res)
		return
	try:
		watch_mod.run(
			root,
			quiet_period,
			_render_watch_result,
			debug=debug,
		)
	except KeyboardInterrupt:
		return


def _render_watch_result(result: watch_mod.WatchResult) -> None:
	if result.sonar_scan is not None:
		render.sonar(result.sonar_scan)
	if result.sonar_cli is not None:
		render.sonar_cli(result.sonar_cli)
	if result.risk is not None:
		render.metadata(
			f"AI review (advisory): reviewed {result.files} file(s) "
			f"- risk: {result.risk}"
		)
	else:
		render.metadata(f"review unavailable ({result.reason or 'unknown reason'})")


def _emit_watch_json(result: watch_mod.WatchResult) -> None:
	if result.diff_hash:
		payload = {
			"schemaVersion": 1,
			"status": "reviewed",
			"diffHash": result.diff_hash,
		}
	elif result.reason == "no changes":
		payload = {
			"schemaVersion": 1,
			"status": "skipped",
			"reason": "nothing to review",
		}
	else:
		payload = {
			"schemaVersion": 1,
			"status": "error",
			"reason": result.reason or "unknown error",
		}
	click.echo(json.dumps(payload, separators=(",", ":")))


def _resolve_agent_model(cli_model: str | None) -> str:
	"""--model option > QUACK_MODEL env var > provider AGENT default.

	The default model is transport-specific AND use-specific, so it comes from
	the selected provider's *agent* default (via llmio). The agent runs a
	multi-step tool-using investigation that needs a stronger model than Tier
	2's single-shot review. An explicit --model or QUACK_MODEL always wins.
	"""
	return (
		cli_model
		or os.environ.get("QUACK_MODEL")
		or llmio.default_model(kind="agent")
	)


def _resolve_completion_model(cli_model: str | None) -> str | None:
	"""--model option > QUACK_MODEL env var > provider COMPLETION default.

	Used for Tier 2 at pre-push: a single-shot review tolerates a cheaper
	model than the agent's investigation, so it uses the provider's
	*completion* default. Same override order; returns None only if no
	provider resolves and nothing was specified (Tier 2 is fail-open).
	"""
	return (
		cli_model
		or os.environ.get("QUACK_MODEL")
		or llmio.default_model(kind="completion")
	)


@main.command()
@click.option(
	"--model",
	default=None,
	help="Model id for the agent (overrides QUACK_MODEL env var).",
)
@click.option(
	"--fly",
	is_flag=True,
	default=False,
	help="Skip ahead: reveal the ready-to-apply patch instead of coaching.",
)
def agent(model: str | None, fly: bool) -> None:
	"""Run the agentic pre-push analysis loop."""
	started = time.perf_counter()
	resolved_model = _resolve_agent_model(model)
	provider = os.environ.get("QUACK_PROVIDER") or llmio.DEFAULT_PROVIDER
	try:
		models = llmio.list_models()
	except Exception:
		pass
	else:
		if resolved_model and resolved_model not in models:
			render.warning(
				f"Agent model {resolved_model} is not in the provider catalog and "
				"will fail at runtime. Run `quack model --list` to see available models."
			)
	# Whether the agent can authenticate is a PROVIDER concern, not a CLI
	# one: copilot_sdk authenticates via the Copilot CLI's stored OAuth
	# login. Ask the selected provider (via llmio) rather than hardcoding
	# a token check.
	reason = llmio.availability_error()
	if reason:
		render.metadata(f"quack agent: {reason}")
		_log_agent_metrics(
			started,
			provider,
			resolved_model,
			tier2_failure=reason,
			agent_failure=reason,
		)
		sys.exit(0)

	root = gitio.repo_root()
	if not root:
		render.metadata("quack agent: not a git repository")
		_log_agent_metrics(
			started,
			provider,
			resolved_model,
			agent_failure="not a git repository",
		)
		sys.exit(0)

	delta = gitio.staged_delta()
	target = "staged"
	# Choose the analysis target. quack agent runs both manually (where
	# staged changes are the right target) and as a pre-push hook (where the
	# index is empty and the real target is the unpushed range @{u}..HEAD).
	# Prefer staged changes to preserve the manual/demo flow; otherwise fall
	# back to the unpushed range.
	if delta.files:
		render.metadata("analyzing staged changes")
	else:
		target = "range"
		upstream = gitio.upstream_ref()
		unpushed = gitio.range_delta(upstream) if upstream else None
		if unpushed and unpushed.files:
			delta = unpushed
			count = gitio.range_commit_count(upstream)
			render.metadata(f"analyzing {count} unpushed commit(s)")
		else:
			target = "none"
			render.clean(
				"nothing to analyze: no staged changes and nothing unpushed"
			)
			_log_agent_metrics(
				started,
				provider,
				resolved_model,
				target=target,
				agent_failure="no changes",
			)
			sys.exit(0)

	# Run Tier 1 and redact any detected secrets before the diff ever leaves
	# the machine. The agent path must honour the same guarantee as Tier 2:
	# a staged secret is never transmitted verbatim to the model.
	findings = tier1_run(delta, Tier1Config())
	redacted = tier1_redact(delta, findings)

	# Tier 1 gates; the AI advises. These checks are deterministic pattern
	# matches, so a hit is a fact rather than a judgement -- unlike the model's
	# verdict. A range diff compares endpoints, so a secret added and then
	# removed within the pushed range never appears here.
	if should_block(findings, block_on=("secrets", "merge_markers")):
		render.report(
			files=len(delta.files),
			added=delta.total_added,
			removed=delta.total_removed,
			findings=findings,
			plan=None,
			ai=None,
			blocked=True,
			duration=time.perf_counter() - started,
		)
		_log_agent_metrics(
			started,
			provider,
			resolved_model,
			target=target,
			agent_failure="tier1 blocked",
		)
		sys.exit(1)

	# Tier 2's single-shot review tolerates a cheaper model than the agent's
	# multi-step investigation, so it uses the provider's COMPLETION default
	# (an explicit --model/QUACK_MODEL still overrides both surfaces).
	tier2_model = _resolve_completion_model(model)

	# Tier 2 AI review on the pre-push path. The copilot_sdk provider supports
	# complete() but not chat()/tool-calling, so Tier 2 (a single completion)
	# runs on the approved transport even where the agent's tool loop cannot;
	# Tier 2 therefore provides useful AI review regardless of provider
	# capability. Run it FIRST: it is the fast single call, so the developer
	# gets a verdict quickly even if the slow agent loop later degrades.
	#
	# Reuse the findings already computed for redaction -- do NOT recompute
	# Tier 1. Fully fail-open and the whole point of this guard: ANY failure
	# (LLMUnavailable, timeout, or validation returning None) must leave the
	# agent path completely unaffected.
	#
	# Timeout is a TRANSPORT property, not a tier one: tier2.review()'s own
	# 6.0s default is tuned for the fast commit-time HTTP call, but the
	# copilot_sdk provider needs ~9s just to start its runtime. Pass the
	# provider-appropriate timeout so pre-push review does not always time out.
	tier2_timeout = llmio.default_timeout()
	tier2_failure: str | None = None
	with render.thinking("reviewing changes..."):
		try:
			plan = testmap.build_plan(delta)
			project_instructions = instructions.load(Path(root))
			review, reason = tier2.review_with_reason(
				delta,
				findings,
				plan,
				model=tier2_model,
				project_instructions=project_instructions,
				timeout_s=tier2_timeout,
			)
			if review is None:
				# tier2 surfaces the actual normalised reason (e.g. the model was
				# unavailable). Only fall back to a provider-level cause, and never
				# invent "timeout": a wrong reason is worse than a generic one.
				tier2_failure = (
					reason or llmio.availability_error() or "unavailable"
				)
		except Exception as exc:
			# Provider exceptions are already normalised to LLMUnavailable inside
			# llmio, but guard here too so no SDK stack trace ever reaches the
			# terminal. Report the real exception, not a misleading "timeout".
			review = None
			msg = str(exc)[:200].replace("\n", " ")
			tier2_failure = f"{type(exc).__name__}: {msg}"
	if review is not None:
		render.review(review, model=tier2_model)
	elif tier2_failure is not None:
		# At pre-push, AI review is the point: silence would hide that it was
		# even attempted, so surface a single dim line stating why.
		render.metadata(f"AI review unavailable ({tier2_failure})")

	with render.thinking("investigating changes..."):
		try:
			from .providers import copilot_sdk

			result = copilot_sdk.run_agent(
				redacted.raw_diff,
				Path(root),
				resolved_model,
				timeout_s=agent_mod.WALL_CLOCK_S,
			)
		except llmio.LLMUnavailable as exc:
			# The pre-push agent is advisory and must never change hook success.
			result = agent_mod._unavailable(
				agent_mod._timeout_hint(exc.reason, resolved_model)
			)
	render.agent_report(result, fly=fly)
	_log_agent_metrics(
		started,
		provider,
		resolved_model,
		target=target,
		tier2_risk=review.risk if review is not None else None,
		tier2_failure=tier2_failure,
		agent_ran=True,
		agent_failure=(
			"analysis unavailable"
			if result.summary.startswith("AI analysis unavailable:")
			else None
		),
	)
	sys.exit(0)


def _log_agent_metrics(
	started: float,
	provider: str,
	model: str,
	*,
	target: str = "none",
	tier2_risk: str | None = None,
	tier2_failure: str | None = None,
	agent_ran: bool = False,
	agent_failure: str | None = None,
) -> None:
	try:
		metrics_mod.log(
			{
				"ts": metrics_mod.timestamp(),
				"command": "agent",
				"duration_ms": int((time.perf_counter() - started) * 1000),
				"target": target,
				"provider": provider,
				"model": model,
				"tier2_risk": tier2_risk,
				"tier2_failure": tier2_failure,
				"agent_ran": agent_ran,
				"agent_failure": agent_failure,
				"exit": 0,
			}
		)
	except Exception:
		pass


@main.command(name="metrics")
def metrics_summary() -> None:
	"""Summarize local metrics without network access."""
	events = metrics_mod.read()
	if events is None:
		click.echo("No metrics available (file missing or unreadable).")
		return

	commands = Counter(str(event.get("command")) for event in events if event.get("command"))
	findings: Counter[str] = Counter()
	durations: list[int] = []
	cache_hits = 0
	cache_lookups = 0
	blocks = 0
	for event in events:
		blocks += int(event.get("blocked") is True)
		duration = event.get("duration_ms")
		if isinstance(duration, (int, float)) and not isinstance(duration, bool):
			durations.append(int(duration))
		raw_findings = event.get("tier1_findings")
		if isinstance(raw_findings, dict):
			for name, count in raw_findings.items():
				if isinstance(name, str) and isinstance(count, int) and count > 0:
					findings[name] += count
		cache = event.get("review_cache")
		if cache in {"hit", "miss"}:
			cache_lookups += 1
			cache_hits += int(cache == "hit")

	click.echo(f"Total runs: {len(events)}")
	click.echo(
		"Runs by command: "
		+ (", ".join(f"{name}={count}" for name, count in sorted(commands.items())) or "none")
	)
	click.echo(f"Blocks: {blocks}")
	click.echo(
		"Most common findings: "
		+ (", ".join(f"{name}={count}" for name, count in findings.most_common()) or "none")
	)
	click.echo(f"Median duration: {median(durations):g} ms" if durations else "Median duration: n/a")
	click.echo(
		f"Cache hit rate: {cache_hits / cache_lookups:.1%}"
		if cache_lookups
		else "Cache hit rate: n/a"
	)


def _diagnostic_model(kind: str, cli_model: str | None) -> tuple[str | None, str]:
	"""Resolve one model surface and identify the winning configuration layer."""
	if cli_model:
		return cli_model, "--model"
	env_model = os.environ.get("QUACK_MODEL")
	if env_model:
		return env_model, "QUACK_MODEL"
	provider_default = llmio.default_model(kind=kind)
	if provider_default:
		return provider_default, "provider default"
	return None, "unresolved"


def _availability_hint(reason: str) -> str:
	if "GITHUB_TOKEN" in reason:
		return "set GITHUB_TOKEN with models:read permission"
	if "not installed" in reason:
		return "install the selected provider runtime"
	if "unknown provider" in reason:
		return "set QUACK_PROVIDER to copilot_sdk"
	return "verify the selected provider's credentials and runtime"


def _model_list_failure_reason(exc: Exception, limit: int = 160) -> str:
	"""Return a safe, bounded, single-line reason for model discovery failure."""
	reason = getattr(exc, "reason", None) or str(exc)
	reason = " ".join(reason.split())
	prefix = "model list unavailable:"
	if reason.lower().startswith(prefix):
		reason = reason[len(prefix) :].strip()
	for name in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
		token = os.environ.get(name)
		if token:
			reason = reason.replace(token, "[REDACTED]")
	if not reason:
		return "unknown reason"
	if len(reason) > limit:
		return reason[: limit - 3].rstrip() + "..."
	return reason


def _render_model_diagnostic(cli_model: str | None) -> None:
	configured_provider = os.environ.get("QUACK_PROVIDER")
	provider = configured_provider or llmio.DEFAULT_PROVIDER
	selection = (
		"QUACK_PROVIDER environment variable"
		if configured_provider
		else f"default (QUACK_PROVIDER unset; default is {llmio.DEFAULT_PROVIDER})"
	)
	render.info(f"Provider: {provider} - selected by {selection}")

	reason = llmio.availability_error()
	if reason:
		render.warning(f"Auth status: problem - {reason}")
		if "unknown provider" in reason or "provider unavailable" in reason:
			render.warning(f"Provider resolution: unavailable - {reason}")
		render.metadata(f"Suggested fix: {_availability_hint(reason)}")
	else:
		render.info("Auth status: available")

	ambient_tokens = [
		(name, len(os.environ[name]))
		for name in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN")
		if name in os.environ
	]
	if provider == "copilot_sdk" and ambient_tokens:
		details = ", ".join(
			f"{name} is set (length {length})" for name, length in ambient_tokens
		)
		render.warning(
			f"WARNING: {details}. An ambient token SHADOWS the Copilot CLI's "
			"stored login in the SDK auth precedence order and can cause "
			'"Authorization error" failures.'
		)
		render.metadata("Suggested fix: unset the ambient token before running quack")

	for kind, label in (("completion", "Completion"), ("agent", "Agent")):
		resolved, source = _diagnostic_model(kind, cli_model)
		value = resolved or "unresolved"
		render.info(f"{label} model: {value} (source: {source})")

	render.info(f"Timeout: {llmio.default_timeout():g}s")

	if provider == "copilot_sdk" and reason is None:
		try:
			models = llmio.list_models()
		except Exception as exc:
			reason = _model_list_failure_reason(exc)
			render.warning(f"Reachable models unavailable: {reason}")
		else:
			visible = models[:15]
			render.info(
				"Reachable models: " + (", ".join(visible) if visible else "none reported")
			)
			if len(models) > len(visible):
				render.metadata(f"Showing first {len(visible)} of {len(models)} models")
			# A default that has aged out of the provider's catalog fails only
			# at runtime, inside a fail-open path: the agent stops investigating
			# and nothing says why. claude-sonnet-4.5 did exactly this. Compare
			# here, where both the defaults and the catalog are already known.
			for kind, label in (("completion", "Completion"), ("agent", "Agent")):
				resolved, _ = _diagnostic_model(kind, cli_model)
				if resolved and models and resolved not in models:
					render.warning(
						f"{label} model {resolved} is NOT in the provider's "
						f"reachable list - it will fail at runtime"
					)


def _echo_model_list(cli_model: str | None) -> None:
	"""Print reachable model ids one per line, marking the current defaults."""
	try:
		models = llmio.list_models()
	except Exception as exc:
		# Read-only convenience: never a failure mode.
		click.echo(f"model catalog unavailable: {_model_list_failure_reason(exc)}")
		return

	markers: dict[str, list[str]] = {}
	for kind, label in (("completion", "completion default"), ("agent", "agent default")):
		resolved, _ = _diagnostic_model(kind, cli_model)
		if resolved:
			markers.setdefault(resolved, []).append(label)

	width = max((len(m) for m in models if m in markers), default=0)
	for m in models:
		labels = markers.get(m)
		click.echo(f"{m.ljust(width)}  ({', '.join(labels)})" if labels else m)


@main.command()
@click.option(
	"--model",
	default=None,
	help="Model id to diagnose (overrides QUACK_MODEL and provider defaults).",
)
@click.option(
	"--json",
	"as_json",
	is_flag=True,
	default=False,
	help="Emit machine-readable model diagnostic payload as JSON and suppress terminal rendering.",
)
@click.option(
	"--list",
	"as_list",
	is_flag=True,
	default=False,
	help="List the reachable model ids and exit.",
)
def model(model: str | None, as_json: bool, as_list: bool) -> None:
	"""Report model configuration and connectivity without changing it."""
	# This command is intentionally read-only; it reports defaults but never sets them.
	if as_json:
		current_model = (
			model
			or os.environ.get("QUACK_MODEL")
			or llmio.default_model(kind="completion")
			or ""
		)
		default_mod = llmio.default_model(kind="completion") or ""
		provider = os.environ.get("QUACK_PROVIDER") or llmio.DEFAULT_PROVIDER
		try:
			discovered = llmio.list_models()
		except Exception:
			discovered = []
		payload = {
			"schemaVersion": 1,
			"currentModel": current_model,
			"defaultModel": default_mod,
			"models": [
				{
					"id": m,
					"displayName": m,
					"provider": provider,
					"available": True,
				}
				for m in discovered
			],
		}
		click.echo(json.dumps(payload, separators=(",", ":")))
		return

	if as_list:
		_echo_model_list(model)
		return

	try:
		_render_model_diagnostic(model)
	except Exception as exc:
		# Diagnostics must never become a new failure mode or expose exception data.
		render.warning(f"quack model: diagnostic unavailable ({type(exc).__name__})")


@main.command()
@click.option("--port", default=8787)
@click.option("--host", default="127.0.0.1")
def serve(port: int, host: str) -> None:
	"""Start the quack FastAPI server."""
	import uvicorn

	from . import server

	uvicorn.run(server.app, host=host, port=port)


@main.command()
@click.option(
	"--local",
	"use_local",
	is_flag=True,
	default=False,
	help="Wire quack via a `repo: local` stanza using the installed `quack` "
	"command (works without a published quack repo).",
)
@click.option(
	"--yes",
	"assume_yes",
	is_flag=True,
	default=False,
	help="Answer yes to prompts (for non-interactive callers such as the IDE "
	"extension).",
)
@click.option(
	"--quack-path",
	"quack_path",
	default=None,
	help="Absolute path to the quack executable to write into hooks. Used by "
	"IDE extensions, where quack is not on PATH.",
)
def install(use_local: bool, assume_yes: bool, quack_path: str | None) -> None:
	"""Add the quack stanza to .pre-commit-config.yaml and install the hook."""
	_install_hooks(
		use_local,
		assume_yes=assume_yes,
		quack_path=quack_path,
	)
	sys.exit(0)


def _configure_sonar_for_git_clients(root: Path) -> None:
	"""Persist local Sonar settings so GUI Git clients match the terminal."""
	explicit_project = (
		os.environ.get("QUACK_SONAR_PROJECT_KEY", "").strip()
		or os.environ.get("SONARQUBE_PROJECT_KEY", "").strip()
		or os.environ.get("SONAR_PROJECT_KEY", "").strip()
		or gitio.config_get("quack.sonar.projectKey", root)
	)
	if not explicit_project and not (root / "sonar-project.properties").is_file():
		return

	settings = sonar.configuration(root)
	branch = gitio.current_branch(root) or settings.branch
	if not settings.valid or not branch:
		return
	if not gitio.config_set("extensions.worktreeConfig", "true", root):
		return

	configured = all(
		(
			gitio.config_set(
				"quack.sonar.hostUrl", settings.host_url, root, worktree=True
			),
			gitio.config_set(
				"quack.sonar.projectKey", settings.project_key, root, worktree=True
			),
			gitio.config_set("quack.sonar.branch", branch, root, worktree=True),
		)
	)
	if not configured:
		render.warning("quack: could not save worktree-local Sonar settings")
		return
	render.clean("quack: Sonar settings saved for terminal and IDE commits")

	token = sonar.environment_token()
	if not token:
		return
	username = "quack"
	if gitio.credential_approve(settings.host_url, username, token, root):
		gitio.config_set(
			"quack.sonar.credentialUsername", username, root, worktree=True
		)
		render.clean("quack: Sonar token saved in the Git credential helper")
	else:
		render.warning(
			"quack: could not save the Sonar token for IDE commits; "
			"configure a Git credential helper and rerun `quack init`"
		)


def _install_hooks(
	use_local: bool,
	*,
	assume_yes: bool = False,
	quack_path: str | None = None,
) -> None:
	"""Write hook configuration and best-effort install both hook types."""
	# Quote: Windows paths have spaces.
	quack_cmd = f'"{quack_path}"' if quack_path else "quack"
	# Detect husky (or any core.hooksPath) BEFORE writing config: when git reads
	# hooks from elsewhere, `pre-commit install` cannot work and writing a config
	# file that nothing executes would leave a misleading artifact behind.
	hooks_path = _hooks_path()
	if hooks_path:
		if not _is_husky(hooks_path):
			render.warning(
				f"quack: core.hooksPath is set to {hooks_path}; quack cannot "
				f"install hooks automatically"
			)
			render.metadata("  add this line to your pre-commit hook:")
			render.metadata(f"    {quack_cmd} check")
			render.metadata("  and this to your pre-push hook:")
			render.metadata(f"    {quack_cmd} agent")
			sys.exit(1)

		husky_dir = Path(".husky")
		render.metadata(f"quack: husky detected (core.hooksPath = {hooks_path})")
		render.metadata(
			"  quack can add itself to .husky/pre-commit and .husky/pre-push"
		)
		render.warning(
			"  these files are tracked in git: this affects everyone on this branch"
		)

		if not assume_yes and not click.confirm("  add quack to the husky hooks?"):
			render.metadata("quack: nothing changed. To wire quack up manually, add:")
			render.metadata(f"    {quack_cmd} check      # to .husky/pre-commit")
			render.metadata(f"    {quack_cmd} agent      # to .husky/pre-push")
			sys.exit(1)

		husky_dir.mkdir(exist_ok=True)
		render.install_banner()
		commit_action = _install_into_husky(
			husky_dir / "pre-commit", f"{quack_cmd} check"
		)
		push_action = _install_into_husky(
			husky_dir / "pre-push", f"{quack_cmd} agent"
		)
		render.clean(f"quack: {commit_action} quack check in .husky/pre-commit")
		render.clean(f"quack: {push_action} quack agent in .husky/pre-push")
		render.metadata("  commit these files so your team gets the same hooks")
		return

	config_path = Path(".pre-commit-config.yaml")
	render.install_banner()
	if use_local:
		_upsert_local_stanza(config_path, quack_path)
	else:
		_upsert_precommit_stanza(config_path)
	render.clean(f"quack: updated {config_path}")

	_configure_sonar_for_git_clients(Path.cwd())

	if shutil.which("pre-commit"):
		try:
			subprocess.run(["pre-commit", "install"], check=True)
			render.clean("quack: pre-commit hook installed")
		except subprocess.CalledProcessError as exc:
			render.warning(f"quack: `pre-commit install` failed ({exc.returncode})")

		# All AI now lives at pre-push, so the pre-push hook type must be
		# installed too. Do this INDEPENDENTLY: a failure here must warn but
		# never abort, and must not undo the pre-commit install that already
		# succeeded.
		try:
			subprocess.run(
				["pre-commit", "install", "--hook-type", "pre-push"], check=True
			)
			render.clean("quack: pre-push hook installed")
		except subprocess.CalledProcessError as exc:
			render.warning(
				f"quack: `pre-commit install --hook-type pre-push` failed "
				f"({exc.returncode}); pre-commit checks still active"
			)
	else:
		render.warning("quack: `pre-commit` not found; skipping hook install")
		render.metadata("  install it with: pipx install pre-commit")

	# One-time power-mode bootstrap: get gitleaks on this machine so every
	# later commit benefits automatically. Best-effort and never fatal.
	# gitleaks is optional -- the built-in Tier 1 patterns still cover the
	# blocking checks on their own, so a failed bootstrap here is a benign
	# notice, not an error. Only surface the underlying reason when it is
	# actionable (i.e. not the generic "no supported package manager" case);
	# the raw exit code is dropped from the user-facing line and kept in
	# metrics instead.
	installed, message = gitleaks.ensure_installed()
	if installed:
		render.clean(f"quack: {message}")
	else:
		render.warning(
			"quack: gitleaks not installed (optional - built-in secret "
			"patterns still active)"
		)
		if "no supported package manager" not in message:
			render.metadata(f"  reason: {message}")
		render.metadata("  set QUACK_DISABLE_GITLEAKS=1 to silence this check")
		metrics_mod.log(
			{
				"ts": metrics_mod.timestamp(),
				"command": "install",
				"failure": f"gitleaks bootstrap: {message}",
			}
		)


@main.command()
@click.option(
	"--local",
	"use_local",
	is_flag=True,
	default=True,
	help="Use the installed `quack` command in a local pre-commit stanza.",
)
def init(use_local: bool) -> None:
	"""Initialize hooks and repo-scoped Sonar Copilot artifacts."""
	_install_hooks(use_local)
	for relative_path, content in templates.ARTIFACTS.items():
		path = Path(relative_path)
		if path.exists():
			render.warning(f"quack init: preserving existing {path}")
			continue
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
		render.clean(f"quack init: created {path}")
	sys.exit(0)


# Marker pair for quack's block inside a husky hook. String search rather than
# parsing: append, update and remove all stay deterministic even if a developer
# edits around the block.
QUACK_HOOK_START = "# >>> quack managed block >>>"
QUACK_HOOK_END = "# <<< quack managed block <<<"


def _hooks_path(*, root: str | None = None) -> str | None:
	"""Return git's configured core.hooksPath, or None when unset.

	When this is set, git ignores .git/hooks entirely -- so `pre-commit install`
	writes a hook that git will never execute. pre-commit knows this and refuses
	outright, which is why quack must detect the case rather than treating the
	failure as incidental.
	"""
	try:
		result = subprocess.run(
			["git", "config", "core.hooksPath"],
			capture_output=True,
			text=True,
			check=False,
			cwd=root,
		)
	except OSError:
		return None
	value = result.stdout.strip()
	return value or None


def _is_husky(hooks_path: str) -> bool:
	"""True when core.hooksPath looks like husky's generated hook directory."""
	return "husky" in hooks_path.replace("\\", "/").lower()


def _husky_hook_block(command: str) -> str:
	return (
		f"\n{QUACK_HOOK_START}\n"
		f"# Added by `quack install`. Remove this block to disable quack.\n"
		f"{command} || exit $?\n"
		f"{QUACK_HOOK_END}\n"
	)


def _install_into_husky(hook_file: Path, command: str) -> str:
	"""Append (or refresh) quack's block in a husky hook. Returns what happened.

	The hook file is tracked in git, so this is only ever called after explicit
	consent: appending here changes the hook for everyone on the branch, not
	just the developer who ran install.
	"""
	if hook_file.exists():
		existing = hook_file.read_text(encoding="utf-8")
	else:
		existing = "#!/usr/bin/env sh\n"

	block = _husky_hook_block(command)

	if QUACK_HOOK_START in existing:
		start = existing.index(QUACK_HOOK_START)
		end = existing.index(QUACK_HOOK_END) + len(QUACK_HOOK_END)
		updated = existing[:start].rstrip("\n") + block + existing[end:].lstrip("\n")
		action = "updated"
	else:
		updated = existing.rstrip("\n") + "\n" + block
		action = "added"

	hook_file.write_text(updated, encoding="utf-8")
	return action


def _upsert_local_stanza(config_path: Path, quack_path: str | None = None) -> None:
	"""Insert or update a `repo: local` quack stanza.

	Uses the `quack` command already on PATH (``language: system``), so no
	published quack repository or git tag is required -- ideal for trying quack
	in any project on this machine.
	"""
	if config_path.exists():
		data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
	else:
		data = {}

	repos = data.setdefault("repos", [])
	if not isinstance(repos, list):
		repos = []
		data["repos"] = repos
	# Quote: Windows paths have spaces.
	quack_cmd = f'"{quack_path}"' if quack_path else "quack"
	# Two surfaces: pre-commit runs local checks only; pre-push runs AI review
	# (and the agent where the provider supports tool calling).
	hooks_to_add = [
		{
			"id": "quack",
			"name": "quack",
			"entry": f"{quack_cmd} check",
			"language": "system",
			"pass_filenames": False,
			"stages": ["pre-commit"],
		},
		{
			"id": "quack-agent",
			"name": "quack-agent",
			"entry": f"{quack_cmd} agent",
			"language": "system",
			"pass_filenames": False,
			"stages": ["pre-push"],
			"verbose": True,
		},
	]

	target = next(
		(
			repo
			for repo in repos
			if isinstance(repo, dict) and repo.get("repo") == "local"
		),
		None,
	)
	anchor = next(
		(
			index
			for index, repo in enumerate(repos)
			if isinstance(repo, dict)
			and repo.get("repo")
			in {"local", QUACK_REPO_URL, _LEGACY_QUACK_REPO_URL}
		),
		None,
	)
	if target is None:
		target = {"repo": "local", "hooks": []}
	existing_hooks = target.get("hooks", [])
	if not isinstance(existing_hooks, list):
		existing_hooks = []
	preserved_hooks = [
		hook
		for hook in existing_hooks
		if not (
			isinstance(hook, dict)
			and hook.get("id") in {"quack", "quack-agent"}
		)
	]
	target["repo"] = "local"
	target["hooks"] = preserved_hooks + hooks_to_add

	filtered_repos: list[object] = []
	for index, repo in enumerate(repos):
		if anchor == index:
			filtered_repos.append(target)
		if isinstance(repo, dict) and repo.get("repo") in {
			"local",
			QUACK_REPO_URL,
			_LEGACY_QUACK_REPO_URL,
		}:
			continue
		filtered_repos.append(repo)
	if anchor is None:
		filtered_repos.append(target)
	data["repos"] = filtered_repos

	config_path.write_text(
		yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
		encoding="utf-8",
	)


def _upsert_precommit_stanza(config_path: Path) -> None:
	"""Insert or update the quack repo stanza in a pre-commit config file."""
	if config_path.exists():
		data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
	else:
		data = {}

	repos = data.setdefault("repos", [])
	# Both hooks come from the same quack repo: `quack` (pre-commit checks) and
	# `quack-agent` (pre-push AI review). Their stages are declared in
	# .pre-commit-hooks.yaml, so listing the ids here is enough.
	stanza = {
		"repo": QUACK_REPO_URL,
		"rev": f"v{__version__}",
		"hooks": [{"id": "quack"}, {"id": "quack-agent", "verbose": True}],
	}

	for repo in repos:
		if isinstance(repo, dict) and repo.get("repo") == QUACK_REPO_URL:
			repo["rev"] = stanza["rev"]
			repo["hooks"] = stanza["hooks"]
			break
	else:
		repos.append(stanza)

	config_path.write_text(
		yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
		encoding="utf-8",
	)


if __name__ == "__main__":
	main()
