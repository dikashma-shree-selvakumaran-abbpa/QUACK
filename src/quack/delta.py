"""Pure logic for modelling staged git changes.

This module never touches git or the network. It takes raw git output
strings in and returns dataclasses out, so it is fully unit-testable.

The three inputs it understands are produced by:
	git diff --cached --name-status -M
	git diff --cached --numstat -M
	git diff --cached -M --unified=3
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

MAX_RAW_DIFF = 60_000
TRUNCATION_MARKER = "\n... [diff truncated at 60000 chars] ...\n"

# File extensions / paths considered documentation.
DOC_EXTENSIONS = (".md", ".markdown", ".rst", ".txt", ".adoc")
DOC_DIR_PREFIXES = ("docs/", "doc/")

# Default glob patterns for lockfiles / generated files. Matched against both
# the full post-image path and its basename.
DEFAULT_EXCLUDES = (
	"*.lock",
	"package-lock.json",
	"yarn.lock",
	"pnpm-lock.yaml",
	"poetry.lock",
	"Pipfile.lock",
	"Cargo.lock",
	"go.sum",
	"composer.lock",
	"*.min.js",
	"*.min.css",
	"*_pb2.py",
	"*.pb.go",
	"**/generated/**",
	"**/__generated__/**",
)

# Small-change threshold: fewer than this many changed lines is trivial.
TRIVIAL_LINE_THRESHOLD = 5


@dataclass
class StagedFile:
	"""A single staged file and its change summary."""

	path: str
	status: str
	added: int
	removed: int
	hunks: list[str] = field(default_factory=list)
	binary: bool = False


@dataclass
class StagedDelta:
	"""The complete set of staged changes."""

	files: list[StagedFile] = field(default_factory=list)
	raw_diff: str = ""

	@property
	def total_added(self) -> int:
		return sum(f.added for f in self.files)

	@property
	def total_removed(self) -> int:
		return sum(f.removed for f in self.files)

	def triviality(
		self, excludes: tuple[str, ...] | list[str] | None = None
	) -> tuple[bool, str]:
		"""Return (is_trivial, reason).

		Trivial when there are no changes, when every file is documentation,
		when the total changed line count is below the threshold, or when every
		file matches a lockfile/generated exclude pattern.
		"""
		patterns = tuple(excludes) if excludes is not None else DEFAULT_EXCLUDES

		if not self.files:
			return True, "no staged changes"

		if all(_is_doc(f.path) for f in self.files):
			return True, "docs-only change"

		total_changed = sum(f.added + f.removed for f in self.files if not f.binary)
		if total_changed < TRIVIAL_LINE_THRESHOLD and not any(
			f.binary for f in self.files
		):
			return True, f"small change (<{TRIVIAL_LINE_THRESHOLD} lines)"

		if all(_matches_any(f.path, patterns) for f in self.files):
			return True, "only lockfiles/generated files"

		return False, "non-trivial change"


def parse_staged_delta(
	name_status: str, numstat: str, unified_diff: str
) -> StagedDelta:
	"""Build a StagedDelta from the three raw git outputs."""
	statuses = _parse_name_status(name_status)
	counts = _parse_numstat(numstat)
	hunks_by_path, binary_paths = _parse_unified_diff(unified_diff)

	files: list[StagedFile] = []
	for path, status in statuses:
		added, removed, numstat_binary = counts.get(path, (0, 0, False))
		binary = numstat_binary or path in binary_paths
		hunks = [] if status == "D" or binary else hunks_by_path.get(path, [])
		files.append(
			StagedFile(
				path=path,
				status=status,
				added=added,
				removed=removed,
				hunks=hunks,
				binary=binary,
			)
		)

	raw_diff = unified_diff
	if len(raw_diff) > MAX_RAW_DIFF:
		raw_diff = raw_diff[:MAX_RAW_DIFF] + TRUNCATION_MARKER

	return StagedDelta(files=files, raw_diff=raw_diff)


def _parse_name_status(text: str) -> list[tuple[str, str]]:
	"""Parse `--name-status` output into (post_image_path, status_letter)."""
	result: list[tuple[str, str]] = []
	for line in text.splitlines():
		if not line.strip():
			continue
		parts = line.split("\t")
		token = parts[0].strip()
		status = token[0] if token else "M"
		if status in ("R", "C") and len(parts) >= 3:
			# Rename/copy: use the post-image path.
			path = parts[2]
		elif len(parts) >= 2:
			path = parts[1]
		else:
			continue
		result.append((path, status))
	return result


def _parse_numstat(text: str) -> dict[str, tuple[int, int, bool]]:
	"""Parse `--numstat` into {post_image_path: (added, removed, binary)}."""
	result: dict[str, tuple[int, int, bool]] = {}
	for line in text.splitlines():
		if not line.strip():
			continue
		parts = line.split("\t")
		if len(parts) < 3:
			continue
		added_raw, removed_raw, rest = parts[0], parts[1], "\t".join(parts[2:])
		path = _numstat_path(rest)
		binary = added_raw == "-" or removed_raw == "-"
		added = 0 if binary else int(added_raw)
		removed = 0 if binary else int(removed_raw)
		result[path] = (added, removed, binary)
	return result


def _numstat_path(rest: str) -> str:
	"""Normalise a numstat path (which may encode a rename) to the new path."""
	if "=>" not in rest:
		return rest.strip()
	if "{" in rest and "}" in rest:
		before = rest[: rest.index("{")]
		inside = rest[rest.index("{") + 1 : rest.index("}")]
		after = rest[rest.index("}") + 1 :]
		new = inside.split("=>", 1)[1].strip()
		return (before + new + after).replace("//", "/").strip()
	return rest.split("=>", 1)[1].strip()


def _parse_unified_diff(text: str) -> tuple[dict[str, list[str]], set[str]]:
	"""Parse a unified diff into {post_image_path: hunks} and binary paths."""
	hunks_by_path: dict[str, list[str]] = {}
	binary_paths: set[str] = set()

	for block in _split_diff_blocks(text):
		path = _block_path(block)
		if path is None:
			continue
		if any(line.startswith("Binary files") for line in block):
			binary_paths.add(path)
			continue
		hunks = _block_hunks(block)
		if hunks:
			hunks_by_path[path] = hunks
	return hunks_by_path, binary_paths


def _split_diff_blocks(text: str) -> list[list[str]]:
	blocks: list[list[str]] = []
	current: list[str] | None = None
	for line in text.splitlines():
		if line.startswith("diff --git "):
			if current is not None:
				blocks.append(current)
			current = [line]
		elif current is not None:
			current.append(line)
	if current is not None:
		blocks.append(current)
	return blocks


def _block_path(block: list[str]) -> str | None:
	post: str | None = None
	rename_to: str | None = None
	minus: str | None = None
	for line in block:
		if line.startswith("+++ b/"):
			post = line[len("+++ b/") :]
		elif line.startswith("rename to "):
			rename_to = line[len("rename to ") :]
		elif line.startswith("--- a/"):
			minus = line[len("--- a/") :]
	if post is not None:
		return post
	if rename_to is not None:
		return rename_to
	return minus


def _block_hunks(block: list[str]) -> list[str]:
	hunks: list[str] = []
	current: list[str] | None = None
	for line in block:
		if line.startswith("@@"):
			if current is not None:
				hunks.append("\n".join(current))
			current = [line]
		elif current is not None:
			current.append(line)
	if current is not None:
		hunks.append("\n".join(current))
	return hunks


def _is_doc(path: str) -> bool:
	lowered = path.lower()
	if lowered.endswith(DOC_EXTENSIONS):
		return True
	return any(lowered.startswith(prefix) for prefix in DOC_DIR_PREFIXES)


def _matches_any(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
	basename = path.rsplit("/", 1)[-1]
	for pattern in patterns:
		if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
			return True
	return False
