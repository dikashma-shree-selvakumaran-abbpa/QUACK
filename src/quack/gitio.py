"""Thin git adapter.

This is one of the few modules permitted to call subprocess directly.
Everything else should take data in and return data out.
"""

from __future__ import annotations

import subprocess

from . import delta
from .delta import StagedDelta


def _run_git(args: list[str]) -> str:
	"""Run a git command and return stdout, or "" if git/repo is unavailable."""
	try:
		result = subprocess.run(
			["git", *args],
			capture_output=True,
			text=True,
			check=True,
		)
	except (subprocess.CalledProcessError, FileNotFoundError):
		return ""
	return result.stdout


def staged_name_status() -> str:
	"""Raw `git diff --cached --name-status -M` output."""
	return _run_git(["diff", "--cached", "--name-status", "-M"])


def staged_numstat() -> str:
	"""Raw `git diff --cached --numstat -M` output."""
	return _run_git(["diff", "--cached", "--numstat", "-M"])


def staged_diff() -> str:
	"""Raw `git diff --cached -M --unified=3` output."""
	return _run_git(["diff", "--cached", "-M", "--unified=3"])


def staged_delta() -> StagedDelta:
	"""Collect the staged changes and parse them into a StagedDelta."""
	return delta.parse_staged_delta(
		staged_name_status(),
		staged_numstat(),
		staged_diff(),
	)


def staged_files() -> list[str]:
	"""Return the list of staged file paths (added/copied/modified/renamed).

	Returns an empty list if git is unavailable or this is not a repo.
	"""
	try:
		result = subprocess.run(
			["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
			capture_output=True,
			text=True,
			check=True,
		)
	except (subprocess.CalledProcessError, FileNotFoundError):
		return []
	return [line for line in result.stdout.splitlines() if line.strip()]
