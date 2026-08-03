"""Project-instructions loader.

Reads repo-local guidance (coding conventions, review focus) so Tier 2 can
render it into the prompt as *context*. The content is untrusted input that
lives in the repository, so this module only reads and truncates it; it never
grants it authority. The deterministic rubric floor in :mod:`quack.tier2`
guarantees such a file can never lower a risk verdict.

FAIL-OPEN contract: this runs inside a commit hook, so :func:`load` never
raises. Any missing file, missing directory, permission error, or decode
problem returns ``None``.
"""

from __future__ import annotations

from pathlib import Path

# Precedence order: the first file that exists wins.
_CANDIDATES = (
	".quack/instructions.md",
	"instructions.md",
	".github/copilot-instructions.md",
	"AGENTS.md",
)

_TRUNCATION_MARKER = "\n\n... [instructions truncated]"


def load(repo_root: Path, max_chars: int = 4000) -> str | None:
	"""Return repo-local instructions text, or ``None`` if none is available.

	Reads the first existing file from the precedence list relative to
	``repo_root``, truncated to ``max_chars`` (with a clear marker appended
	when truncation occurs). Never raises: any OSError, decode error, or
	missing directory yields ``None``.
	"""
	for candidate in _CANDIDATES:
		path = repo_root / candidate
		try:
			if not path.is_file():
				continue
			text = path.read_text(encoding="utf-8", errors="replace")
		except OSError:
			return None

		if len(text) > max_chars:
			return text[:max_chars] + _TRUNCATION_MARKER
		return text

	return None
