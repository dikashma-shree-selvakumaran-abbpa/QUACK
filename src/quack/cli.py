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
from pathlib import Path

import click
import yaml

from . import (
	__version__,
	agent as agent_mod,
	gitio,
	gitleaks,
	instructions,
	llmio,
	render,
	testmap,
	tier2,
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
	delta = gitio.staged_delta()
	if not delta.files:
		render.clean("nothing staged")
		sys.exit(0)

	# Tier 1: deterministic checks (offline, always run).
	findings = tier1_run(delta, Tier1Config())

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
		)
		sys.exit(1)

	# Test guidance (only worth computing on an unblocked commit).
	plan = testmap.build_plan(delta)

	# Commit time is fully local: no Tier 2, no network, no token. The AI
	# section is simply absent (ai=None). AI analysis runs at pre-push via
	# `quack agent`.
	# TODO: thread the real hook duration through to render.report.
	render.report(
		files=len(delta.files),
		added=delta.total_added,
		removed=delta.total_removed,
		findings=findings,
		plan=plan,
		ai=None,
		blocked=False,
	)
	sys.exit(0)


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
	# Whether the agent can authenticate is a PROVIDER concern, not a CLI
	# one: github_models needs GITHUB_TOKEN, but copilot_sdk authenticates
	# via the Copilot CLI's stored OAuth login and never reads it. Ask the
	# selected provider (via llmio) rather than hardcoding a token check.
	reason = llmio.availability_error()
	if reason:
		render.metadata(f"quack agent: {reason}")
		sys.exit(0)

	root = gitio.repo_root()
	if not root:
		render.metadata("quack agent: not a git repository")
		sys.exit(0)

	delta = gitio.staged_delta()
	# Choose the analysis target. quack agent runs both manually (where
	# staged changes are the right target) and as a pre-push hook (where the
	# index is empty and the real target is the unpushed range @{u}..HEAD).
	# Prefer staged changes to preserve the manual/demo flow; otherwise fall
	# back to the unpushed range.
	if delta.files:
		render.metadata("analyzing staged changes")
	else:
		upstream = gitio.upstream_ref()
		unpushed = gitio.range_delta(upstream) if upstream else None
		if unpushed and unpushed.files:
			delta = unpushed
			count = gitio.range_commit_count(upstream)
			render.metadata(f"analyzing {count} unpushed commit(s)")
		else:
			render.clean(
				"nothing to analyze: no staged changes and nothing unpushed"
			)
			sys.exit(0)

	# Run Tier 1 and redact any detected secrets before the diff ever leaves
	# the machine. The agent path must honour the same guarantee as Tier 2:
	# a staged secret is never transmitted verbatim to the model.
	findings = tier1_run(delta, Tier1Config())
	redacted = tier1_redact(delta, findings)

	resolved_model = _resolve_agent_model(model)
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
		tier2_failure = f"{type(exc).__name__}"
	if review is not None:
		render.review(review, model=tier2_model)
	elif tier2_failure is not None:
		# At pre-push, AI review is the point: silence would hide that it was
		# even attempted, so surface a single dim line stating why.
		render.metadata(f"AI review unavailable ({tier2_failure})")

	result = agent_mod.run(redacted.raw_diff, Path(root), resolved_model)
	render.agent_report(result, fly=fly)
	sys.exit(0)


@main.command()
def model() -> None:
	"""Inspect model configuration and connectivity (stub)."""
	render.metadata("quack model: not implemented yet")
	sys.exit(0)


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
	installed, message = gitleaks.ensure_installed()
	if installed:
		render.clean(f"quack: {message}")
	else:
		render.warning(f"quack: gitleaks power mode unavailable - {message}")
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
		"hooks": [{"id": "quack"}, {"id": "quack-agent"}],
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

