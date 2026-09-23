"""quack command-line interface.

Subcommands:
	check   the hook entry (Tier 1 + Tier 2 orchestration)
	agent   agentic pre-push loop (stub)
	model   model/config utilities (stub)
	install wire quack into .pre-commit-config.yaml and run `pre-commit install`
	init    install hooks and scaffold repo-scoped Sonar Copilot skills/agent
	sonar-mcp connect to, inspect, and invoke read-only SonarQube MCP tools
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
	sonar_report,
	sonar_state,
	templates,
	testmap,
	tier2,
	watch as watch_mod,
)
from .mcp import sonarqube as sonarqube_mcp
from .tier1 import Tier1Config
from .tier1 import allowlisted_locations
from .tier1 import redact as tier1_redact
from .tier1 import run as tier1_run
from .tier1 import should_block

QUACK_REPO_URL = "https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK"

# Last-resort agent model, used ONLY when no provider resolves (e.g. an
# unknown QUACK_PROVIDER) so llmio.default_model("agent") returns None. In the
# normal path each provider supplies its own split defaults. Kept because that
# genuine no-provider case still needs a non-None model to hand the agent.
DEFAULT_AGENT_MODEL = "openai/gpt-4.1"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="quack")
def main() -> None:
	"""quack: an AI-assisted pre-commit quality hook."""


@main.command()
def check() -> None:
	"""Run the pre-commit quality checks on staged changes.

	Commit time runs local Tier 1 checks (plus gitleaks when installed), test
	guidance, an optional local SonarQube analysis, and an optional SonarQube
	MCP snapshot. A completed MCP snapshot blocks when it reports open SonarQube
	issues or security hotspots; unavailable SonarQube integrations remain
	fail-open. All AI analysis runs at pre-push via ``quack agent``.
	"""
	# Measures in-process work only, excluding Python interpreter startup.
	started = time.perf_counter()
	delta = gitio.staged_delta()
	if not delta.files:
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

	if blocked:
		# Only Tier 1 governs the exit code. Show findings + BLOCKED banner.
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

	sonar_result, sonar_mcp_result, _sonar_cache_hit = _run_staged_sonar_check(
		delta, root
	)
	blocked = blocked or bool(
		getattr(sonar_mcp_result, "blocks_commit", False)
	)

	# Test guidance (only worth computing on an unblocked commit).
	plan = testmap.build_plan(delta)

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

	render.report(
		files=len(delta.files),
		added=delta.total_added,
		removed=delta.total_removed,
		findings=findings,
		plan=plan,
		ai=ai,
		sonar=sonar_result,
		sonar_mcp=sonar_mcp_result,
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
		sonar_mcp_result=sonar_mcp_result,
		blocked=blocked,
		exit_code=1 if blocked else 0,
	)
	sys.exit(1 if blocked else 0)


def _run_staged_sonar_check(delta, root):
	"""Check the exact index and revalidate any locally cached server result."""
	settings = sonar.configuration(root, staged=True)
	mcp_server_url = _mcp_server_url()
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
		report = sonar_report.run(
			delta,
			root,
			source="pre-commit",
			project_path=root,
			project_key=settings.project_key,
			server_url=mcp_server_url,
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
		return scan_result, report, True

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
	report = sonar_report.run(
		delta,
		root,
		source="pre-commit",
		project_path=root,
		project_key=settings.project_key,
		server_url=mcp_server_url,
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


def _mcp_server_url() -> str | None:
	"""Resolve only explicit MCP settings, never scanner project settings."""
	explicit = (
		os.environ.get("QUACK_SONAR_MCP_URL", "").strip()
		or os.environ.get("SONARQUBE_URL", "").strip()
	)
	return explicit or None


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
				f"{updated.reason}; current MCP result is unverified for the "
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
		return replace(result, fresh=fresh)
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
	sonar_mcp_result=None,
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
		if sonar_mcp_result is not None:
			event.update(
				{
					"sonar_mcp_status": sonar_mcp_result.status,
					"sonar_mcp_duration_ms": int(
						sonar_mcp_result.duration_s * 1000
					),
				}
			)
			if sonar_mcp_result.status != "passed":
				event["sonar_mcp_failure"] = sonar_mcp_result.reason
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
def watch(quiet_period: float, once: bool) -> None:
	"""Review changes in the background and cache the result for commits."""
	root = gitio.repo_root()
	if not root:
		render.metadata("quack watch: not a git repository")
		return
	if once:
		_render_watch_result(watch_mod.review_once(root))
		return
	try:
		watch_mod.run(root, quiet_period, _render_watch_result)
	except KeyboardInterrupt:
		return


def _render_watch_result(result: watch_mod.WatchResult) -> None:
	if result.sonar_report is not None:
		render.sonar_mcp(result.sonar_report)
	if result.risk is not None:
		render.metadata(
			f"AI review (advisory): reviewed {result.files} file(s) "
			f"- risk: {result.risk}"
		)
	else:
		render.metadata(f"review unavailable ({result.reason or 'unknown reason'})")


def _resolve_agent_model(cli_model: str | None) -> str:
	"""--model option > QUACK_MODEL env var > provider AGENT default.

	The default model is transport-specific AND use-specific, so it comes from
	the selected provider's *agent* default (via llmio). The agent runs a
	multi-step tool-using investigation that needs a stronger model than Tier
	2's single-shot review. An explicit --model or QUACK_MODEL always wins.
	DEFAULT_AGENT_MODEL is only a last resort if no provider resolves.
	"""
	return (
		cli_model
		or os.environ.get("QUACK_MODEL")
		or llmio.default_model(kind="agent")
		or DEFAULT_AGENT_MODEL
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
	# Whether the agent can authenticate is a PROVIDER concern, not a CLI
	# one: github_models needs GITHUB_TOKEN, but copilot_sdk authenticates
	# via the Copilot CLI's stored OAuth login and never reads it. Ask the
	# selected provider (via llmio) rather than hardcoding a token check.
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
		result = agent_mod.run(redacted.raw_diff, Path(root), resolved_model)
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
	if kind == "agent":
		return DEFAULT_AGENT_MODEL, "fallback (provider unresolved)"
	return None, "unresolved"


def _availability_hint(reason: str) -> str:
	if "GITHUB_TOKEN" in reason:
		return "set GITHUB_TOKEN with models:read permission"
	if "not installed" in reason:
		return "install the selected provider runtime"
	if "unknown provider" in reason:
		return "set QUACK_PROVIDER to github_models or copilot_sdk"
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


@main.command()
@click.option(
	"--model",
	default=None,
	help="Model id to diagnose (overrides QUACK_MODEL and provider defaults).",
)
def model(model: str | None) -> None:
	"""Report model configuration and connectivity without changing it."""
	# This command is intentionally read-only; it reports defaults but never sets them.
	try:
		_render_model_diagnostic(model)
	except Exception as exc:
		# Diagnostics must never become a new failure mode or expose exception data.
		render.warning(f"quack model: diagnostic unavailable ({type(exc).__name__})")


@main.command()
@click.option(
	"--local",
	"use_local",
	is_flag=True,
	default=False,
	help="Wire quack via a `repo: local` stanza using the installed `quack` "
	"command (works without a published quack repo).",
)
def install(use_local: bool) -> None:
	"""Add the quack stanza to .pre-commit-config.yaml and install the hook."""
	_install_hooks(use_local)
	sys.exit(0)


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


def _install_hooks(use_local: bool) -> None:
	"""Write hook configuration and best-effort install both hook types."""
	render.install_banner()
	config_path = Path(".pre-commit-config.yaml")
	if use_local:
		_upsert_local_stanza(config_path)
	else:
		_upsert_precommit_stanza(config_path)
	render.clean(f"quack: updated {config_path}")

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
	installed, message = gitleaks.ensure_installed()
	if installed:
		render.clean(f"quack: {message}")
	else:
		render.warning(f"quack: gitleaks power mode unavailable - {message}")


@main.command("sonar-mcp")
@click.option(
	"--project-path",
	type=click.Path(
		exists=True, file_okay=False, dir_okay=True, path_type=Path
	),
	default=None,
	help="Local SonarQube workspace to mount read-only in the MCP container.",
)
@click.option(
	"--project-key",
	default=None,
	help="Default SonarQube project key for the MCP server.",
)
@click.option(
	"--toolset",
	"--toolsets",
	"toolsets",
	multiple=True,
	help="SonarQube MCP toolset to enable; repeat the option or use commas.",
)
@click.option(
	"--tool",
	"tool_name",
	default=None,
	help="Read-only MCP tool to invoke after discovery.",
)
@click.option(
	"--arguments",
	"tool_arguments",
	default="{}",
	show_default=True,
	help="JSON object passed to --tool.",
)
@click.option(
	"--json",
	"json_output",
	is_flag=True,
	help="Print the complete MCP response as JSON when --tool is used.",
)
@click.option(
	"--debug",
	"debug_output",
	is_flag=True,
	help="Print the resolved MCP invocation and response diagnostics.",
)
@click.option(
	"--ca-dir",
	type=click.Path(
		exists=True, file_okay=False, dir_okay=True, path_type=Path
	),
	default=None,
	help="Directory containing the corporate .crt or .pem CA certificate.",
)
@click.option(
	"--url",
	"server_url",
	default=None,
	help="SonarQube server URL (overrides SONARQUBE_URL).",
)
def sonar_mcp(
	project_path: Path | None,
	project_key: str | None,
	toolsets: tuple[str, ...],
	tool_name: str | None,
	tool_arguments: str,
	json_output: bool,
	debug_output: bool,
	ca_dir: Path | None,
	server_url: str | None,
) -> None:
	"""Connect to SonarQube MCP, list tools, or invoke one read-only tool."""
	if json_output and tool_name is None:
		raise click.UsageError("--json requires --tool")
	json_tool_mode = json_output and tool_name is not None
	if not json_tool_mode:
		render.info("SonarQube MCP: connecting via Podman...")
	normalised_toolsets = _normalise_cli_toolsets(toolsets)
	if (
		project_path is None
		and project_key is None
		and not normalised_toolsets
		and ca_dir is None
		and server_url is None
	):
		client, reason = sonarqube_mcp.connection_from_environment()
	else:
		client, reason = sonarqube_mcp.connection_from_environment(
			project_path=project_path,
			project_key=project_key,
			toolsets=normalised_toolsets or None,
			ca_dir=ca_dir,
			server_url=server_url,
			base_path=Path.cwd(),
		)
	if client is None:
		render.warning(f"quack sonar-mcp: {reason or 'unavailable'}")
		sys.exit(1)
	try:
		if json_tool_mode:
			tools = client.tool_definitions()
		else:
			with render.thinking("SonarQube MCP: discovering available tools"):
				tools = client.tool_definitions()
	except sonarqube_mcp.SonarQubeMcpUnavailable as exc:
		render.warning(f"quack sonar-mcp: {exc.reason}")
		sys.exit(1)
	if not json_tool_mode:
		render.clean(f"SonarQube MCP connected via podman ({len(tools)} tool(s))")
		for tool in tools:
			name = tool.get("function", {}).get("name")
			if isinstance(name, str):
				render.metadata(f"  {name}")
	if tool_name is not None:
		_invoke_sonar_mcp_tool(
			client,
			tool_name,
			tool_arguments,
			json_output,
			debug_output,
		)
		sys.exit(0)
	issue_tool = next(
		(
			name
			for tool in tools
			if isinstance((name := tool.get("function", {}).get("name")), str)
			and "search_sonar_issues_in_projects" in name
		),
		None,
	)
	if issue_tool is None:
		render.warning(
			"SonarQube vulnerability report failed: "
			"security issue search tool is unavailable"
		)
		sys.exit(1)
	project_key = client.project_key
	if not project_key:
		render.warning(
			"SonarQube vulnerability report failed: no project key was supplied; "
			"use --project-key or --project-path"
		)
		sys.exit(1)
	try:
		with render.thinking(
			"SonarQube MCP: checking open security violations"
		):
			report = client.call_tool_data(
				issue_tool,
				{
					"projects": [project_key],
					"impactSoftwareQualities": ["SECURITY"],
					"issueStatuses": ["OPEN"],
					"ps": 100,
				},
			)
	except sonarqube_mcp.SonarQubeMcpUnavailable as exc:
		render.warning(f"SonarQube vulnerability report failed: {exc.reason}")
		sys.exit(1)
	issues = report.get("issues")
	paging = report.get("paging")
	if not isinstance(issues, list) or not isinstance(paging, dict):
		render.warning(
			"SonarQube vulnerability report failed: malformed issue response"
		)
		sys.exit(1)
	total = paging.get("total")
	if not isinstance(total, int) or total < 0:
		render.warning(
			"SonarQube vulnerability report failed: issue count unavailable"
		)
		sys.exit(1)
	if total == 0:
		render.clean(
			f"SonarQube vulnerability report: no open security violations "
			f"found for {project_key}"
		)
		sys.exit(0)
	render.warning(
		f"SonarQube vulnerability report: {total} open security "
		f"violation(s) found for {project_key}"
	)
	for issue in issues[:10]:
		if not isinstance(issue, dict):
			continue
		key = _sonar_issue_value(issue, "key", "unknown")
		severity = _sonar_issue_value(issue, "severity", "unknown")
		message = _sonar_issue_value(issue, "message", "no message")
		render.metadata(f"  {key} [{severity}] {message}")
	if total > len(issues):
		render.metadata(f"  ...and {total - len(issues)} more")
	sys.exit(0)


def _normalise_cli_toolsets(values: tuple[str, ...]) -> tuple[str, ...]:
	"""Return repeatable and comma-separated CLI toolsets without duplicates."""
	normalised: list[str] = []
	for value in values:
		for item in value.split(","):
			item = item.strip()
			if item and item not in normalised:
				normalised.append(item)
	return tuple(normalised)


def _invoke_sonar_mcp_tool(
	client: sonarqube_mcp.SonarQubeMcpClient,
	tool_name: str,
	tool_arguments: str,
	json_output: bool,
	debug_output: bool,
) -> None:
	"""Invoke one advertised read-only MCP tool from the explicit CLI."""
	try:
		parsed_arguments = _parse_sonar_mcp_arguments(tool_arguments)
	except (TypeError, ValueError) as exc:
		_emit_sonar_mcp_debug(
			client,
			tool_name,
			arguments=None,
			response=None,
			raw_arguments=tool_arguments,
			force=True,
		)
		raise click.UsageError(
			"--arguments must contain valid JSON; quote object keys and "
			'string values, for example \'{"projectKeys":["Operations.HMI.App.Alarms"],'
			'"branch":"quack-sonar-violation","issueStatuses":["OPEN"]}\''
		) from exc
	if not isinstance(parsed_arguments, dict):
		raise click.UsageError("--arguments must contain a JSON object")
	arguments = parsed_arguments
	resolved_name = client.resolve_tool_name(tool_name)
	if resolved_name is None:
		render.warning(
			f"quack sonar-mcp: unknown or unavailable read-only MCP tool "
			f"{tool_name!r}"
		)
		sys.exit(1)
	arguments = _normalise_sonar_mcp_arguments(
		client, resolved_name, arguments
	)
	_emit_sonar_mcp_debug(
		client,
		resolved_name,
		arguments=arguments,
		response=None,
		raw_arguments=tool_arguments,
		force=debug_output,
	)
	try:
		if json_output:
			response = client.call_tool_response(resolved_name, arguments)
		else:
			with render.thinking(f"SonarQube MCP: invoking {resolved_name}"):
				response = client.call_tool_response(resolved_name, arguments)
	except sonarqube_mcp.SonarQubeMcpUnavailable as exc:
		_emit_sonar_mcp_debug(
			client,
			resolved_name,
			arguments=arguments,
			response={"error": exc.reason},
			raw_arguments=tool_arguments,
			force=debug_output,
		)
		render.warning(f"quack sonar-mcp: {exc.reason}")
		sys.exit(1)
	_emit_sonar_mcp_debug(
		client,
		resolved_name,
		arguments=arguments,
		response=response,
		raw_arguments=tool_arguments,
		force=debug_output,
	)
	if response.get("isError"):
		if json_output:
			click.echo(json.dumps(response, indent=2, sort_keys=True))
		else:
			render.warning(
				"quack sonar-mcp: tool returned an error: "
				+ sonarqube_mcp.format_tool_result(response)
			)
		sys.exit(1)
	if json_output:
		click.echo(json.dumps(response, indent=2, sort_keys=True))
	else:
		render.clean(f"SonarQube MCP tool completed: {resolved_name}")
		render.metadata(sonarqube_mcp.format_tool_result(response))


def _emit_sonar_mcp_debug(
	client: sonarqube_mcp.SonarQubeMcpClient,
	tool_name: str,
	*,
	arguments: dict[str, object] | None,
	response: dict[str, object] | None,
	raw_arguments: str,
	force: bool,
) -> None:
	"""Print safe diagnostics for one explicit MCP invocation."""
	if not force:
		return
	config = getattr(client, "_config", None)
	server_url = getattr(config, "url", "<unknown>")
	project_path = getattr(config, "project_path", None)
	project_key = getattr(client, "project_key", None) or "<unset>"
	branch = getattr(client, "branch", None) or "<unset>"
	command_builder = getattr(config, "podman_command", None)
	invocation = (
		json.dumps(command_builder(), separators=(",", ":"))
		if callable(command_builder)
		else "<unavailable>"
	)
	raw = _redact_sonar_debug_text(repr(raw_arguments))
	lines = [
		"[debug] SonarQube MCP invocation",
		f"  server_url={server_url}",
		f"  project_path={project_path or '<unset>'}",
		f"  project_key={project_key}",
		f"  configured_branch={branch}",
		f"  invocation_argv={_redact_sonar_debug_text(invocation)}",
		f"  tool={tool_name}",
		f"  raw_arguments={raw}",
	]
	if arguments is None:
		lines.append("  request=<not sent; argument parsing failed>")
	else:
		lines.append(
			"  request_arguments="
			+ _redact_sonar_debug_text(
				json.dumps(arguments, separators=(",", ":"), sort_keys=True)
			)
		)
	if response is None:
		lines.append("  response=<none>")
	else:
		lines.append(
			"  response="
			+ _redact_sonar_debug_text(
				json.dumps(response, separators=(",", ":"), sort_keys=True)
			)
		)
	click.echo("\n".join(lines), err=True)


def _redact_sonar_debug_text(value: str) -> str:
	"""Redact environment tokens before writing diagnostics to a terminal."""
	redacted = value
	for name in ("SONARQUBE_TOKEN", "SQ_TOKEN", "SONAR_TOKEN"):
		token = os.environ.get(name, "")
		if token:
			redacted = redacted.replace(token, "<redacted>")
	return redacted


def _parse_sonar_mcp_arguments(raw: str) -> object:
	"""Parse JSON passed by native shells and launcher wrappers."""
	text = raw.strip().lstrip("\ufeff")
	candidates = [text]
	if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
		candidates.append(text[1:-1])
	for candidate in tuple(candidates):
		if '\\"' in candidate:
			candidates.append(candidate.replace('\\"', '"'))
	for candidate in candidates:
		try:
			value = json.loads(candidate)
		except (TypeError, ValueError):
			continue
		if isinstance(value, str):
			nested = value.strip().lstrip("\ufeff")
			if nested.startswith(("{", "[")):
				try:
					return json.loads(nested)
				except (TypeError, ValueError):
					pass
		return value
	for candidate in candidates:
		restored = _restore_powershell_json_quotes(candidate)
		if restored == candidate:
			continue
		try:
			return json.loads(restored)
		except (TypeError, ValueError):
			continue
	raise ValueError("--arguments must contain valid JSON")


def _restore_powershell_json_quotes(text: str) -> str:
	"""Restore simple quotes stripped by Windows PowerShell 5.1 argv rules."""
	restored = re.sub(
		r"([,{]\s*)([A-Za-z_][A-Za-z0-9_.-]*)(\s*:)",
		r'\1"\2"\3',
		text,
	)

	def quote_scalar(match: re.Match[str]) -> str:
		prefix, token = match.group(1), match.group(2)
		if token in {"true", "false", "null"}:
			return prefix + token
		return prefix + json.dumps(token)

	return re.sub(
		r"([:\[,]\s*)([A-Za-z_][A-Za-z0-9_.:/\\@+-]*)(?=\s*[,}\]])",
		quote_scalar,
		restored,
	)


def _normalise_sonar_mcp_arguments(
	client: sonarqube_mcp.SonarQubeMcpClient,
	resolved_name: str,
	arguments: dict[str, object],
) -> dict[str, object]:
	"""Match common project-key spelling to the advertised MCP schema."""
	try:
		tools = client.tool_definitions()
	except sonarqube_mcp.SonarQubeMcpUnavailable:
		return arguments
	properties: dict[str, object] | None = None
	for tool in tools:
		function = tool.get("function")
		if not isinstance(function, dict):
			continue
		if function.get("name") != resolved_name:
			continue
		parameters = function.get("parameters")
		if isinstance(parameters, dict) and isinstance(
			parameters.get("properties"), dict
		):
			properties = parameters["properties"]
		break
	if (
		properties is not None
		and "projectKeys" in arguments
		and "projects" in properties
		and "projects" not in arguments
	):
		normalised = dict(arguments)
		normalised["projects"] = normalised.pop("projectKeys")
		return normalised
	return arguments


def _sonar_issue_value(
	issue: dict[str, object], key: str, default: str
) -> str:
	value = issue.get(key)
	if not isinstance(value, str):
		return default
	value = " ".join(value.split())
	return value[:240] or default


def _upsert_local_stanza(config_path: Path) -> None:
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
	# Two surfaces: pre-commit runs local checks only; pre-push runs AI review
	# (and the agent where the provider supports tool calling).
	hooks_to_add = [
		{
			"id": "quack",
			"name": "quack",
			"entry": "quack check",
			"language": "system",
			"pass_filenames": False,
			"stages": ["pre-commit"],
		},
		{
			"id": "quack-agent",
			"name": "quack-agent",
			"entry": "quack agent",
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
			and repo.get("repo") in {"local", QUACK_REPO_URL}
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
