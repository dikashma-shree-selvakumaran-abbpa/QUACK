"""quack command-line interface.

Subcommands:
	check   the hook entry (Tier 1 + Tier 2 orchestration)
	agent   agentic pre-push loop (stub)
	model   model/config utilities (stub)
	install wire quack into .pre-commit-config.yaml and run `pre-commit install`
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections import Counter
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
	testmap,
	tier2,
	watch as watch_mod,
)
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

	Commit time is fully local: Tier 1 deterministic checks (plus gitleaks when
	installed) and test guidance only. No network calls, no token, no AI. All
	AI analysis runs at pre-push via ``quack agent``.
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

	# Test guidance (only worth computing on an unblocked commit).
	plan = testmap.build_plan(delta)

	# Commit time is fully local: hash the same deterministically redacted diff
	# used by watch mode and perform one fail-open cache-file lookup. A miss must
	# never fall back to a provider call.
	redacted = tier1_redact(delta, redaction_findings)
	root = gitio.repo_root() or os.getcwd()
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
		model=cached_model,
		ai_note=cache_note,
		blocked=False,
		duration=time.perf_counter() - started,
	)
	_log_check_metrics(
		started,
		delta,
		findings=findings,
		plan=plan,
		cache_hit=cached_review is not None,
		risk=cached_review.risk if cached_review is not None else None,
		exit_code=0,
	)
	sys.exit(0)


def _log_check_metrics(
	started: float,
	delta,
	*,
	findings=(),
	plan=None,
	blocked: bool = False,
	cache_hit: bool = False,
	risk: str | None = None,
	exit_code: int,
) -> None:
	try:
		metrics_mod.log(
			{
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
		)
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
	if result.risk is not None:
		render.metadata(f"reviewed {result.files} file(s) - risk: {result.risk}")
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
	sys.exit(0)


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

	for repo in repos:
		if isinstance(repo, dict) and repo.get("repo") == "local":
			hooks = repo.setdefault("hooks", [])
			for hook in hooks_to_add:
				if not any(
					isinstance(h, dict) and h.get("id") == hook["id"]
					for h in hooks
				):
					hooks.append(hook)
			break
	else:
		repos.append({"repo": "local", "hooks": hooks_to_add})

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

