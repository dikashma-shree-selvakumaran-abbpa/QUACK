"""Tier 1 deterministic checks.

Pure logic: takes a :class:`~quack.delta.StagedDelta` plus a
:class:`Tier1Config` in, returns a list of :class:`Finding` out. It never
touches git or the network, so it is fully unit-testable.

Every check scans ADDED lines only. A secret (or any pattern) that appears on
a REMOVED line ('-') must never fire -- only lines starting with '+' (and not
the '+++' diff header) are considered added. Line numbers are tracked by
parsing each hunk header for the new-file starting line and advancing through
context and added lines.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace

from .delta import DEFAULT_EXCLUDES, StagedDelta, StagedFile, _matches_any

# ---------------------------------------------------------------------------
# Configuration and result types
# ---------------------------------------------------------------------------

DEFAULT_BLOCK_ON: tuple[str, ...] = ("secrets", "merge_markers")

# A staged file larger than this on disk is flagged (warn).
LARGE_FILE_BYTES = 512 * 1024


@dataclass
class Tier1Config:
	"""Configuration for the Tier 1 checks."""

	excludes: tuple[str, ...] | list[str] = DEFAULT_EXCLUDES
	block_on: tuple[str, ...] | list[str] = DEFAULT_BLOCK_ON


@dataclass
class Finding:
	"""A single Tier 1 finding."""

	check: str
	severity: str
	path: str
	line: int
	message: str
	# The exact secret text matched, used only by redact(); never rendered.
	match: str = field(default="", repr=False)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Ordered list of (message, compiled pattern) for secret detection.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
	("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
	(
		"private key block",
		re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----"),
	),
	(
		"Azure Storage account key",
		re.compile(r"AccountKey=[A-Za-z0-9+/=]{60,}"),
	),
	(
		"GitHub token",
		re.compile(r"(?:gh[posur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
	),
	(
		# Azure DevOps PATs are 52-char base32 (lowercase a-z + digits 2-7).
		# Word boundaries keep false positives low against source identifiers.
		"Azure DevOps PAT",
		re.compile(r"\b[a-z2-7]{52}\b"),
	),
	("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
	(
		"hardcoded credential",
		re.compile(
			r"""(?i)(?:key|secret|token|password)\s*[=:]\s*['"][^'"]{16,}['"]""",
		),
	),
]

_MERGE_MARKER = re.compile(r"^(?:<{7}|={7}|>{7})")

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Inline allowlist: a line carrying one of these markers is skipped entirely,
# giving developers a clean escape hatch for a known false positive instead
# of resorting to `git commit --no-verify`. Compatible with the common
# detect-secrets `pragma: allowlist secret` convention.
_ALLOW_MARKER = re.compile(
	r"quack:\s*allow|pragma:\s*allowlist secret",
	re.IGNORECASE,
)

_SECURITY_SMELLS: list[tuple[str, re.Pattern[str]]] = [
	("TLS verification disabled (verify=False)", re.compile(r"\bverify\s*=\s*False\b")),
	(
		"subprocess shell=True introduced",
		re.compile(r"\bshell\s*=\s*True\b"),
	),
]

_PERFORMANCE_SMELLS: list[tuple[str, re.Pattern[str]]] = [
	(
		"sleep call introduced (potential latency/perf impact)",
		re.compile(r"\b(?:time\.sleep|Thread\.Sleep)\s*\("),
	),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(delta: StagedDelta, config: Tier1Config | None = None) -> list[Finding]:
	"""Run all Tier 1 checks over a staged delta and return the findings."""
	cfg = config or Tier1Config()
	findings: list[Finding] = []

	for file in delta.files:
		if file.binary:
			continue
		if _matches_any(file.path, cfg.excludes):
			continue

		findings.extend(_large_file(file))

		for hunk in file.hunks:
			findings.extend(_scan_hunk(file.path, hunk))

	return findings


def allowlisted_locations(delta: StagedDelta) -> set[tuple[str, int]]:
	"""Return the ``(path, line)`` of every added line carrying an allow marker.

	This is the single source of truth for the inline allowlist. Both quack's
	built-in checks and the optional gitleaks layer consult it, so one
	``# quack: allow`` (or ``pragma: allowlist secret``) comment suppresses a
	finding on that line regardless of which scanner produced it.
	"""
	locations: set[tuple[str, int]] = set()
	for file in delta.files:
		if file.binary:
			continue
		for hunk in file.hunks:
			lines = hunk.split("\n")
			header = _HUNK_HEADER.match(lines[0]) if lines else None
			if header is None:
				continue
			current = int(header.group(1))
			for line in lines[1:]:
				if line.startswith("+++") or line.startswith("---"):
					continue
				if line.startswith("+"):
					if _ALLOW_MARKER.search(line[1:]):
						locations.add((file.path, current))
					current += 1
				elif line.startswith("-") or line.startswith("\\"):
					continue
				else:
					current += 1
	return locations


def should_block(
	findings: list[Finding], block_on: tuple[str, ...] | list[str]
) -> bool:
	"""Return True if any finding's check name is in ``block_on``."""
	blockers = set(block_on)
	return any(f.check in blockers for f in findings)


def redact(delta: StagedDelta, findings: list[Finding]) -> StagedDelta:
	"""Return a copy of ``delta`` with secret lines replaced by ``[REDACTED]``.

	Any line whose content matched a ``secrets`` finding has its content blanked
	in both ``raw_diff`` and the owning file's hunks. The real secret text never
	appears in the returned delta, so it is safe to hand to Tier 2.
	"""
	secret_texts = [f.match for f in findings if f.check == "secrets" and f.match]
	if not secret_texts:
		return delta

	new_files = [
		replace(
			file,
			hunks=[_redact_text(hunk, secret_texts) for hunk in file.hunks],
		)
		for file in delta.files
	]
	return StagedDelta(
		files=new_files,
		raw_diff=_redact_text(delta.raw_diff, secret_texts),
	)


# ---------------------------------------------------------------------------
# Hunk scanning
# ---------------------------------------------------------------------------


def _scan_hunk(path: str, hunk: str) -> list[Finding]:
	lines = hunk.split("\n")
	header = _HUNK_HEADER.match(lines[0]) if lines else None
	if header is None:
		return []

	findings: list[Finding] = []
	current = int(header.group(1))

	for line in lines[1:]:
		if line.startswith("+++") or line.startswith("---"):
			continue
		if line.startswith("+"):
			content = line[1:]
			findings.extend(_scan_added_line(path, current, content))
			current += 1
		elif line.startswith("-"):
			# Removed line: never scanned, does not advance the new-file counter.
			continue
		elif line.startswith("\\"):
			# "\ No newline at end of file" marker.
			continue
		else:
			# Context line.
			current += 1

	return findings


def _scan_added_line(path: str, line: int, content: str) -> list[Finding]:
	findings: list[Finding] = []

	# An explicit inline allowlist marker suppresses every check on this line.
	if _ALLOW_MARKER.search(content):
		return findings

	secret = _match_secret(content)
	if secret is not None:
		message, text = secret
		findings.append(
			Finding("secrets", "error", path, line, message, match=text)
		)

	if _MERGE_MARKER.match(content):
		findings.append(
			Finding("merge_markers", "error", path, line, "merge conflict marker")
		)

	debug = _match_debug(path, content)
	if debug is not None:
		findings.append(Finding("debug_code", "warn", path, line, debug))

	security_smell = _match_security_smell(content)
	if security_smell is not None:
		findings.append(Finding("security_smell", "warn", path, line, security_smell))

	performance_smell = _match_performance_smell(content)
	if performance_smell is not None:
		findings.append(
			Finding("performance_smell", "warn", path, line, performance_smell)
		)

	return findings


def _match_secret(content: str) -> tuple[str, str] | None:
	for message, pattern in _SECRET_PATTERNS:
		match = pattern.search(content)
		if match:
			return message, match.group(0)
	return None


def _match_debug(path: str, content: str) -> str | None:
	if "console.log(" in content:
		return "console.log left in"
	if "breakpoint()" in content:
		return "breakpoint() left in"
	if "pdb.set_trace()" in content:
		return "pdb.set_trace() left in"
	if "it.only(" in content or "describe.only(" in content:
		return "focused test (.only) left in"
	if "Debugger.Break()" in content:
		return "Debugger.Break() left in"
	if "print(" in content and _has_debug_marker(content):
		return "debug print statement left in"
	if (
		path.lower().endswith(".cs")
		and "Console.WriteLine" in content
		and not _is_test_or_cli(path)
	):
		return "Console.WriteLine left in"
	return None


def _match_security_smell(content: str) -> str | None:
	for message, pattern in _SECURITY_SMELLS:
		if pattern.search(content):
			return message
	return None


def _match_performance_smell(content: str) -> str | None:
	for message, pattern in _PERFORMANCE_SMELLS:
		if pattern.search(content):
			return message
	return None


def _has_debug_marker(content: str) -> bool:
	return "HERE" in content or "DEBUG" in content or "xxx" in content.lower()


def _is_test_or_cli(path: str) -> bool:
	lowered = path.lower()
	if "test" in lowered:
		return True
	base = lowered.rsplit("/", 1)[-1]
	return "cli" in base or base in ("program.cs", "main.cs")


# ---------------------------------------------------------------------------
# Whole-file checks
# ---------------------------------------------------------------------------


def _large_file(file: StagedFile) -> list[Finding]:
	try:
		size = os.path.getsize(file.path)
	except OSError:
		return []
	if size > LARGE_FILE_BYTES:
		kb = size // 1024
		return [
			Finding(
				"large_file",
				"warn",
				file.path,
				0,
				f"large file ({kb} KB > 512 KB)",
			)
		]
	return []


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact_text(text: str, secret_texts: list[str]) -> str:
	if not text:
		return text
	out: list[str] = []
	for line in text.split("\n"):
		out.append(_redact_line(line, secret_texts))
	return "\n".join(out)


def _redact_line(line: str, secret_texts: list[str]) -> str:
	for secret in secret_texts:
		if secret and secret in line:
			prefix = line[:1] if line[:1] in "+- " else ""
			return prefix + "[REDACTED]"
	return line
