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
	render,
	testmap,
)
from .tier1 import Tier1Config
from .tier1 import allowlisted_locations
from .tier1 import redact as tier1_redact
from .tier1 import run as tier1_run
from .tier1 import should_block

QUACK_REPO_URL = "https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK"

# Agent uses a stronger model
# more reasoning capability than check's one-shot review.
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
	"""--model option > QUACK_MODEL env var > agent default."""
	return cli_model or os.environ.get("QUACK_MODEL") or DEFAULT_AGENT_MODEL


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
	if not os.environ.get("GITHUB_TOKEN"):
		render.metadata("quack agent: needs GITHUB_TOKEN, none found")
		sys.exit(0)

	root = gitio.repo_root()
	if not root:
		render.metadata("quack agent: not a git repository")
		sys.exit(0)

	delta = gitio.staged_delta()
	if not delta.files:
		render.clean("nothing staged")
		sys.exit(0)

	# Run Tier 1 and redact any detected secrets before the diff ever leaves
	# the machine. The agent path must honour the same guarantee as Tier 2:
	# a staged secret is never transmitted verbatim to the model.
	findings = tier1_run(delta, Tier1Config())
	redacted = tier1_redact(delta, findings)

	resolved_model = _resolve_agent_model(model)
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
	hook = {
		"id": "quack",
		"name": "quack",
		"entry": "quack check",
		"language": "system",
		"pass_filenames": False,
		"stages": ["pre-commit"],
	}

	for repo in repos:
		if isinstance(repo, dict) and repo.get("repo") == "local":
			hooks = repo.setdefault("hooks", [])
			if not any(
				isinstance(h, dict) and h.get("id") == "quack" for h in hooks
			):
				hooks.append(hook)
			break
	else:
		repos.append({"repo": "local", "hooks": [hook]})

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
	stanza = {
		"repo": QUACK_REPO_URL,
		"rev": f"v{__version__}",
		"hooks": [{"id": "quack"}],
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

