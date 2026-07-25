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

from . import __version__, gitio, render, testmap
from .tier1 import Finding, Tier1Config
from .tier1 import run as tier1_run
from .tier1 import should_block

QUACK_REPO_URL = "https://github.com/your-org/quack"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="quack")
def main() -> None:
	"""quack: an AI-assisted pre-commit quality hook."""


@main.command()
def check() -> None:
	"""Run the pre-commit quality checks on staged changes."""
	delta = gitio.staged_delta()
	if not delta.files:
		render.clean("nothing staged")
		sys.exit(0)

	# Tier 1 / Tier 2 orchestration will live here. Skeleton for now.
	render.metadata(
		f"quack: {len(delta.files)} file(s) staged, "
		f"+{delta.total_added}/-{delta.total_removed} lines"
	)

	# Tier 1: deterministic checks (offline, always run).
	findings = tier1_run(delta, Tier1Config())
	_render_findings(findings)
	if should_block(findings, block_on=("secrets", "merge_markers")):
		render.blocking("BLOCKED â€” fix and re-stage")
		sys.exit(1)

	# Test guidance (only worth computing on an unblocked commit).
	plan = testmap.build_plan(delta)
	_render_test_guidance(plan)

	# Tier 2 (AI analysis) slot â€” future milestone M4.

	sys.exit(0)


def _render_findings(findings: list[Finding]) -> None:
	"""Render each Tier 1 finding: 'severity  check  path:line  message'."""
	for f in findings:
		line = f"{f.severity}  {f.check}  {f.path}:{f.line}  {f.message}"
		if f.severity == "error":
			render.blocking(line)
		else:
			render.warning(line)


def _render_test_guidance(plan: testmap.TestPlan) -> None:
	"""Print the Test guidance section: runner commands and untested sources."""
	if not plan.runner_commands and not plan.untested_sources:
		return

	render.info("Test guidance:")

	for cmd in plan.runner_commands:
		render.command(f"  {cmd}")
	if plan.dotnet_hint:
		render.metadata("  (first run: build once with dotnet build)")

	for source in plan.untested_sources:
		render.warning(f"  {source}: NO TESTS FOUND")


@main.command()
def agent() -> None:
	"""Run the agentic pre-push analysis loop (stub)."""
	render.metadata("quack agent: not implemented yet")
	sys.exit(0)


@main.command()
def model() -> None:
	"""Inspect model configuration and connectivity (stub)."""
	render.metadata("quack model: not implemented yet")
	sys.exit(0)


@main.command()
def install() -> None:
	"""Add the quack stanza to .pre-commit-config.yaml and install the hook."""
	config_path = Path(".pre-commit-config.yaml")
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
	sys.exit(0)


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

