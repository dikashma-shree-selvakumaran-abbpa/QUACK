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
			encoding="utf-8",
			errors="replace",
			bufsize=1024 * 1024,
			check=True,
		)
	except (subprocess.CalledProcessError, FileNotFoundError):
		return ""
	return result.stdout


def repo_root() -> str:
	"""Absolute path to the repository top level, or "" if unavailable."""
	return _run_git(["rev-parse", "--show-toplevel"]).strip()


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


def working_delta() -> StagedDelta:
	"""Collect tracked working-tree changes versus HEAD.

	This includes staged and unstaged changes. Untracked files are visible to
	the watcher's filesystem snapshot but have no Git diff until staged.
	"""
	return delta.parse_staged_delta(
		_run_git(["diff", "--name-status", "-M", "HEAD"]),
		_run_git(["diff", "--numstat", "-M", "HEAD"]),
		_run_git(["diff", "-M", "--unified=3", "HEAD"]),
	)


def range_delta(base: str, head: str = "HEAD") -> StagedDelta:
	"""Collect the delta for a commit range and parse it into a StagedDelta.

	Mirrors staged_delta() but over base..head instead of the index, reusing
	the same three git invocations (with the range substituted for --cached)
	and the existing delta.parse_staged_delta() parser.
	"""
	rng = f"{base}..{head}"
	return delta.parse_staged_delta(
		_run_git(["diff", "--name-status", "-M", rng]),
		_run_git(["diff", "--numstat", "-M", rng]),
		_run_git(["diff", "-M", "--unified=3", rng]),
	)


def upstream_ref() -> str | None:
	"""Return the tracking branch (e.g. ``origin/main``) or None.

	Resolves via ``git rev-parse --abbrev-ref --symbolic-full-name @{u}``.
	Returns None when there is no upstream (new branch, no remote). MUST NOT
	raise -- any git failure yields None.
	"""
	ref = _run_git(
		["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
	).strip()
	return ref or None


def range_commit_count(base: str, head: str = "HEAD") -> int:
	"""Number of commits in ``base..head``, or 0 on any git failure."""
	out = _run_git(["rev-list", "--count", f"{base}..{head}"]).strip()
	try:
		return int(out)
	except ValueError:
		return 0


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
