"""Thin git adapter.

This is one of the few modules permitted to call subprocess directly.
Everything else should take data in and return data out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import delta
from .delta import StagedDelta

_GENERATED_WATCH_FILES = frozenset({"docs/sonarqube_report.md"})


def _run_git(args: list[str], *, cwd: str | Path | None = None) -> str:
	"""Run a git command and return stdout, or "" if git/repo is unavailable."""
	try:
		result = subprocess.run(
			["git", *args],
			cwd=str(cwd) if cwd else None,
			capture_output=True,
			encoding="utf-8",
			errors="replace",
			bufsize=1024 * 1024,
			check=True,
		)
	except (subprocess.CalledProcessError, OSError):
		return ""
	return result.stdout or ""


def repo_root() -> str:
	"""Absolute path to the repository top level, or "" if unavailable."""
	return _run_git(["rev-parse", "--show-toplevel"]).strip()


def current_branch(cwd: str | Path | None = None) -> str:
	"""Return the checked-out branch name, or "" for detached/unavailable Git."""
	return _run_git(["branch", "--show-current"], cwd=cwd).strip()


def config_get(key: str, cwd: str | Path | None = None) -> str:
	"""Read one repository-local Git setting, or "" when unavailable."""
	return _run_git(["config", "--get", key], cwd=cwd).strip()


def config_set(
	key: str,
	value: str,
	cwd: str | Path | None = None,
	*,
	worktree: bool = False,
) -> bool:
	"""Write one local or worktree-local Git setting without invoking a shell."""
	args = ["config"]
	if worktree:
		args.append("--worktree")
	args.extend([key, value])
	try:
		result = subprocess.run(
			["git", *args],
			cwd=str(cwd) if cwd else None,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return False
	return result.returncode == 0


def credential_password(
	url: str,
	username: str,
	cwd: str | Path | None = None,
) -> str:
	"""Read a password from Git's credential helper without interactive prompts."""
	if not url or not username or any(char in url + username for char in "\r\n"):
		return ""
	environment = os.environ.copy()
	environment["GIT_TERMINAL_PROMPT"] = "0"
	environment["GCM_INTERACTIVE"] = "Never"
	payload = f"url={url}\nusername={username}\n\n"
	try:
		result = subprocess.run(
			["git", "credential", "fill"],
			cwd=str(cwd) if cwd else None,
			input=payload,
			capture_output=True,
			encoding="utf-8",
			errors="replace",
			env=environment,
			timeout=5,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return ""
	if result.returncode != 0:
		return ""
	for line in result.stdout.splitlines():
		if line.startswith("password="):
			return line.removeprefix("password=").strip()
	return ""


def credential_approve(
	url: str,
	username: str,
	password: str,
	cwd: str | Path | None = None,
) -> bool:
	"""Store a secret through Git's configured credential helper via stdin."""
	if (
		not url
		or not username
		or not password
		or any(char in url + username + password for char in "\r\n")
	):
		return False
	payload = f"url={url}\nusername={username}\npassword={password}\n\n"
	try:
		result = subprocess.run(
			["git", "credential", "approve"],
			cwd=str(cwd) if cwd else None,
			input=payload,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			encoding="utf-8",
			timeout=5,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return False
	return result.returncode == 0


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


def staged_file_text(
	path: str, root: str | Path | None = None
) -> str | None:
	"""Read one text file from the index, never the working tree."""
	normalised = path.replace("\\", "/").strip("/")
	if (
		not normalised
		or normalised.startswith("-")
		or any(part in {"", ".", ".."} for part in normalised.split("/"))
	):
		return None
	try:
		result = subprocess.run(
			["git", "show", f":{normalised}"],
			cwd=str(root) if root else None,
			capture_output=True,
			encoding="utf-8",
			errors="replace",
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return None
	return result.stdout if result.returncode == 0 else None


def staged_paths(root: str | Path | None = None) -> list[str]:
	"""Return tracked paths exactly as represented by the current index."""
	output = _run_git(["ls-files", "-z"], cwd=root)
	return [item for item in output.split("\0") if item]


def export_staged_snapshot(
	destination: str | Path, root: str | Path | None = None
) -> bool:
	"""Export the index contents into an empty directory.

	The scanner must inspect exactly what is staged, not unstaged working-tree
	edits. ``git checkout-index`` copies the index snapshot without invoking a
	shell and without exposing repository contents to another process.
	"""
	destination_path = Path(destination)
	try:
		destination_path.mkdir(parents=True, exist_ok=True)
		prefix = f"--prefix={destination_path.resolve()}{os.sep}"
		result = subprocess.run(
			["git", "checkout-index", "--all", prefix],
			cwd=str(root) if root else None,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return False
	return result.returncode == 0


def working_paths(root: str | Path | None = None) -> list[str]:
	"""Return tracked and non-ignored untracked paths in the working tree."""
	output = _run_git(
		["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
		cwd=root,
	)
	return [item for item in output.split("\0") if item]


def export_working_snapshot(
	destination: str | Path, root: str | Path | None = None
) -> bool:
	"""Copy the current working tree into an empty scanner directory.

	Only Git-tracked and non-ignored untracked files are copied. Symlinks and
	paths escaping the repository are skipped so a scanner cannot follow a
	worktree link outside the requested project.
	"""
	root_path = Path(root).resolve() if root else Path.cwd().resolve()
	destination_path = Path(destination).resolve()
	try:
		destination_path.mkdir(parents=True, exist_ok=True)
		for relative_name in working_paths(root_path):
			relative = Path(relative_name)
			if relative.is_absolute() or ".." in relative.parts:
				continue
			if _is_generated_watch_path(relative.as_posix()):
				continue
			source = root_path / relative
			if source.is_symlink() or not source.is_file():
				continue
			resolved = source.resolve()
			if root_path != resolved and root_path not in resolved.parents:
				continue
			target = destination_path / relative
			target.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(source, target)
	except (OSError, RuntimeError):
		return False
	return True


def working_delta(root: str | Path | None = None) -> StagedDelta:
	"""Collect staged, unstaged, and non-ignored untracked changes vs HEAD."""
	cwd = root
	result = delta.parse_staged_delta(
		_run_git(["diff", "--name-status", "-M", "HEAD"], cwd=cwd),
		_run_git(["diff", "--numstat", "-M", "HEAD"], cwd=cwd),
		_run_git(["diff", "-M", "--unified=3", "HEAD"], cwd=cwd),
	)
	tracked = set(staged_paths(root))
	untracked = [
		path
		for path in working_paths(root)
		if not _is_generated_watch_path(path)
		and path not in tracked
	]
	for path in untracked:
		added_file = _untracked_file(path, root)
		if added_file is None:
			continue
		result.files.append(added_file)
		result.raw_diff += _untracked_diff(path, added_file)
	if len(result.raw_diff) > delta.MAX_RAW_DIFF:
		result.raw_diff = result.raw_diff[: delta.MAX_RAW_DIFF] + delta.TRUNCATION_MARKER
	return result


def _is_generated_watch_path(path: str) -> bool:
	normalised = path.replace("\\", "/").strip("/").casefold()
	return normalised in _GENERATED_WATCH_FILES or normalised.startswith(
		".scannerwork/"
	)


def _untracked_file(path: str, root: str | Path | None) -> delta.StagedFile | None:
	base = Path(root).resolve() if root else Path.cwd().resolve()
	source = base / Path(path)
	try:
		if source.is_symlink() or not source.is_file():
			return None
		data = source.read_bytes()
	except OSError:
		return None
	if b"\0" in data:
		return delta.StagedFile(path, "A", 0, 0, [], binary=True)
	text = data.decode("utf-8", errors="replace")
	lines = text.splitlines()
	hunk_lines = [f"@@ -0,0 +1,{len(lines)} @@"]
	hunk_lines.extend(f"+{line}" for line in lines)
	hunk = "\n".join(hunk_lines)
	if len(hunk) > delta.MAX_RAW_DIFF:
		hunk = hunk[: delta.MAX_RAW_DIFF] + delta.TRUNCATION_MARKER
	return delta.StagedFile(
		path,
		"A",
		len(lines),
		0,
		[hunk] if lines else [],
	)


def _untracked_diff(path: str, file: delta.StagedFile) -> str:
	"""Build the bounded diff text used by Tier 1/cache for a new file."""
	if file.binary:
		return (
			f"diff --git a/{path} b/{path}\n"
			f"new file mode 100644\nBinary files /dev/null and b/{path} differ\n"
		)
	return (
		f"diff --git a/{path} b/{path}\n"
		f"new file mode 100644\n--- /dev/null\n+++ b/{path}\n"
		+ (file.hunks[0] + "\n" if file.hunks else "")
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
