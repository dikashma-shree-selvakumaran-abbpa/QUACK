"""Thin git adapter.

This is one of the few modules permitted to call subprocess directly.
Everything else should take data in and return data out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

from . import delta
from .delta import StagedDelta


@dataclass(frozen=True)
class PushedRef:
	"""One ref being pushed, parsed from git's pre-push stdin line."""

	local_ref: str
	local_sha: str
	remote_ref: str
	remote_sha: str

	@property
	def is_delete(self) -> bool:
		return (
			self.local_ref == "(delete)"
			or self.local_sha == "0" * 40
			or (len(self.local_sha) >= 4 and set(self.local_sha) == {"0"})
		)

	@property
	def is_new_branch(self) -> bool:
		return (
			self.remote_sha == "0" * 40
			or (len(self.remote_sha) >= 4 and set(self.remote_sha) == {"0"})
		)


def _run_git(args: list[str], *, cwd: str | None = None) -> str:
	"""Run a git command and return stdout, or "" if git/repo is unavailable."""
	try:
		result = subprocess.run(
			["git", *args],
			capture_output=True,
			encoding="utf-8",
			errors="replace",
			bufsize=1024 * 1024,
			check=True,
			cwd=cwd,
		)
	except (subprocess.CalledProcessError, FileNotFoundError):
		return ""
	return result.stdout


def _is_valid_git_ref(ref: str, *, root: str | None = None) -> bool:
	"""Return True if ref resolves to a valid git commit/object in repo."""
	if not ref or not isinstance(ref, str):
		return False
	return bool(_run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root).strip())


def repo_root(*, root: str | None = None) -> str:
	"""Absolute path to the repository top level, or "" if unavailable."""
	return _run_git(["rev-parse", "--show-toplevel"], cwd=root).strip()


def staged_name_status(*, root: str | None = None) -> str:
	"""Raw `git diff --cached --name-status -M` output."""
	return _run_git(["diff", "--cached", "--name-status", "-M"], cwd=root)


def staged_numstat(*, root: str | None = None) -> str:
	"""Raw `git diff --cached --numstat -M` output."""
	return _run_git(["diff", "--cached", "--numstat", "-M"], cwd=root)


def staged_diff(*, root: str | None = None) -> str:
	"""Raw `git diff --cached -M --unified=3` output."""
	return _run_git(["diff", "--cached", "-M", "--unified=3"], cwd=root)


def staged_delta(*, root: str | None = None) -> StagedDelta:
	"""Collect the staged changes and parse them into a StagedDelta."""
	return delta.parse_staged_delta(
		staged_name_status(root=root),
		staged_numstat(root=root),
		staged_diff(root=root),
	)


def working_delta(*, root: str | None = None) -> StagedDelta:
	"""Collect tracked working-tree changes versus HEAD.

	This includes staged and unstaged changes. Untracked files are visible to
	the watcher's filesystem snapshot but have no Git diff until staged.
	"""
	return delta.parse_staged_delta(
		_run_git(["diff", "--name-status", "-M", "HEAD"], cwd=root),
		_run_git(["diff", "--numstat", "-M", "HEAD"], cwd=root),
		_run_git(["diff", "-M", "--unified=3", "HEAD"], cwd=root),
	)


def range_delta(base: str, head: str = "HEAD", *, root: str | None = None) -> StagedDelta:
	"""Collect the delta for a commit range and parse it into a StagedDelta.

	Mirrors staged_delta() but over base..head instead of the index, reusing
	the same three git invocations (with the range substituted for --cached)
	and the existing delta.parse_staged_delta() parser.
	"""
	rng = f"{base}..{head}"
	return delta.parse_staged_delta(
		_run_git(["diff", "--name-status", "-M", rng], cwd=root),
		_run_git(["diff", "--numstat", "-M", rng], cwd=root),
		_run_git(["diff", "-M", "--unified=3", rng], cwd=root),
	)


def upstream_ref(*, root: str | None = None) -> str | None:
	"""Return the tracking branch (e.g. ``origin/main``) or None.

	Resolves via ``git rev-parse --abbrev-ref --symbolic-full-name @{u}``.
	Returns None when there is no upstream (new branch, no remote). MUST NOT
	raise -- any git failure yields None.
	"""
	ref = _run_git(
		["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=root
	).strip()
	return ref or None


def remote_default_branch(
	remote: str = "origin", *, root: str | None = None
) -> str | None:
	"""Return the best remote default branch ref (e.g. 'origin/main'), or None."""
	candidates = [
		f"{remote}/HEAD",
		f"{remote}/main",
		f"{remote}/master",
	]
	if remote != "origin":
		candidates.extend(["origin/HEAD", "origin/main", "origin/master"])

	for cand in candidates:
		if _run_git(["rev-parse", "--verify", "--quiet", cand], cwd=root).strip():
			return cand

	remotes = [
		r.strip()
		for r in _run_git(["remote"], cwd=root).splitlines()
		if r.strip()
	]
	for r in remotes:
		if r not in (remote, "origin"):
			for cand in [f"{r}/HEAD", f"{r}/main", f"{r}/master"]:
				if _run_git(
					["rev-parse", "--verify", "--quiet", cand], cwd=root
				).strip():
					return cand

	for local_cand in ["main", "master"]:
		if _run_git(
			["rev-parse", "--verify", "--quiet", local_cand], cwd=root
		).strip():
			return local_cand

	return None


def range_commit_count(base: str, head: str = "HEAD", *, root: str | None = None) -> int:
	"""Number of commits in ``base..head``, or 0 on any git failure."""
	out = _run_git(["rev-list", "--count", f"{base}..{head}"], cwd=root).strip()
	try:
		return int(out)
	except ValueError:
		return 0


def staged_files(*, root: str | None = None) -> list[str]:
	"""Return the list of staged file paths (added/copied/modified/renamed).

	Returns an empty list if git is unavailable or this is not a repo.
	"""
	return [
		line
		for line in _run_git(
			["diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd=root
		).splitlines()
		if line.strip()
	]


def parse_prepush_stdin(text: str | None) -> list[PushedRef]:
	"""Parse lines in git pre-push stdin format:
	<local-ref> <local-sha1> <remote-ref> <remote-sha1>
	"""
	if not text:
		return []
	refs: list[PushedRef] = []
	for raw_line in text.splitlines():
		line = raw_line.strip()
		if not line:
			continue
		parts = line.split()
		if len(parts) == 4:
			refs.append(
				PushedRef(
					local_ref=parts[0],
					local_sha=parts[1],
					remote_ref=parts[2],
					remote_sha=parts[3],
				)
			)
	return refs


def read_prepush_stdin() -> str | None:
	"""Read git pre-push ref lines from sys.stdin if piped, or None if a tty."""
	try:
		if sys.stdin is not None and not sys.stdin.isatty():
			content = sys.stdin.read()
			return content if content.strip() else None
	except Exception:
		return None
	return None


def resolve_push_range(
	stdin_text: str | None = None,
	*,
	remote: str = "origin",
	root: str | None = None,
) -> tuple[str | None, str, str | None]:
	"""Determine the (base, head, description) to analyze for pre-push.

	Priority:
	1. Git stdin ref lines (<local-ref> <local-sha1> <remote-ref> <remote-sha1>)
	   - Existing branch update: remote_sha..local_sha
	   - New branch: remote_default_branch..local_sha
	2. Pre-commit environment variables (PRE_COMMIT_FROM_REF / PRE_COMMIT_TO_REF)
	3. Tracking branch @{u}..HEAD
	4. Remote default branch (e.g. origin/main)..HEAD
	"""
	pushed_refs = parse_prepush_stdin(stdin_text) if stdin_text else []
	if not pushed_refs and os.environ.get("PRE_COMMIT") == "1":
		from_ref = os.environ.get("PRE_COMMIT_FROM_REF")
		to_ref = os.environ.get("PRE_COMMIT_TO_REF")
		if (
			from_ref
			and to_ref
			and _is_valid_git_ref(from_ref, root=root)
			and _is_valid_git_ref(to_ref, root=root)
		):
			pushed_refs = [
				PushedRef(
					local_ref="HEAD",
					local_sha=to_ref,
					remote_ref="remote",
					remote_sha=from_ref,
				)
			]

	for ref in pushed_refs:
		if ref.is_delete:
			continue
		head = ref.local_sha
		if not ref.is_new_branch:
			return ref.remote_sha, head, f"{ref.remote_sha[:7]}..{head[:7]}"
		default_branch = remote_default_branch(remote=remote, root=root)
		if default_branch:
			return (
				default_branch,
				head,
				f"{default_branch}..{head[:7]} (new branch)",
			)
		return None, head, None

	upstream = upstream_ref(root=root)
	if upstream:
		return upstream, "HEAD", f"{upstream}..HEAD"

	default_branch = remote_default_branch(remote=remote, root=root)
	if default_branch:
		return (
			default_branch,
			"HEAD",
			f"{default_branch}..HEAD (no upstream tracking branch)",
		)

	return None, "HEAD", None

