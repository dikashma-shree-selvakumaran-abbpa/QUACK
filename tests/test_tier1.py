"""Unit tests for quack.tier1 using fabricated staged deltas."""

from __future__ import annotations

from quack import tier1
from quack.delta import StagedDelta, StagedFile
from quack.tier1 import Finding, Tier1Config


def _delta(path: str, hunk: str, raw_diff: str | None = None) -> StagedDelta:
	"""Build a single-file StagedDelta from one hunk string."""
	file = StagedFile(path=path, status="M", added=1, removed=0, hunks=[hunk])
	diff = raw_diff if raw_diff is not None else hunk
	return StagedDelta(files=[file], raw_diff=diff)


def _hunk(*added_lines: str, start: int = 1) -> str:
	body = "\n".join(f"+{line}" for line in added_lines)
	return f"@@ -0,0 +{start},{len(added_lines)} @@\n{body}"


# ---------------------------------------------------------------------------
# One fixture per check type
# ---------------------------------------------------------------------------


def test_secrets_azure_connection_string() -> None:
	secret = "AccountKey=" + "A" * 64 + ";"
	delta = _delta("src/config.py", _hunk(f'CONN = "{secret}"'))
	findings = tier1.run(delta)
	assert [f.check for f in findings] == ["secrets"]
	assert findings[0].severity == "error"
	assert findings[0].path == "src/config.py"
	assert findings[0].line == 1


def test_secrets_aws_akia_key() -> None:
	delta = _delta("src/aws.py", _hunk('KEY = "AKIA1234567890ABCDEF"'))
	findings = tier1.run(delta)
	assert any(f.check == "secrets" for f in findings)


def test_secrets_azure_devops_pat() -> None:
	# A bare 52-char base32 Azure DevOps PAT (no keyword nearby).
	pat = "a" * 52
	delta = _delta("src/ado.py", _hunk(f"pat = {pat}"))
	findings = tier1.run(delta)
	assert [f.check for f in findings] == ["secrets"]
	assert findings[0].message == "Azure DevOps PAT"


def test_inline_allowlist_suppresses_findings() -> None:
	# A real secret on a line carrying the allowlist marker is skipped.
	delta = _delta(
		"src/aws.py",
		_hunk('KEY = "AKIA1234567890ABCDEF"  # quack: allow'),
	)
	assert tier1.run(delta) == []


def test_inline_allowlist_pragma_convention() -> None:
	delta = _delta(
		"src/aws.py",
		_hunk('KEY = "AKIA1234567890ABCDEF"  # pragma: allowlist secret'),
	)
	assert tier1.run(delta) == []


def test_merge_markers() -> None:
	delta = _delta(
		"src/app.py",
		_hunk("<<<<<<< HEAD", "our change", "=======", "their change", ">>>>>>> branch"),
	)
	findings = tier1.run(delta)
	checks = [f.check for f in findings]
	assert checks.count("merge_markers") == 3
	assert all(f.severity == "error" for f in findings if f.check == "merge_markers")


def test_debug_code() -> None:
	delta = _delta("src/ui.js", _hunk("console.log('here')"))
	findings = tier1.run(delta)
	assert [f.check for f in findings] == ["debug_code"]
	assert findings[0].severity == "warn"


def test_large_file(tmp_path) -> None:
	big = tmp_path / "big.bin"
	big.write_bytes(b"0" * (600 * 1024))
	file = StagedFile(path=str(big), status="A", added=0, removed=0, hunks=[])
	delta = StagedDelta(files=[file], raw_diff="")
	findings = tier1.run(delta)
	assert [f.check for f in findings] == ["large_file"]
	assert findings[0].severity == "warn"


# ---------------------------------------------------------------------------
# Clean diff -> no findings
# ---------------------------------------------------------------------------


def test_clean_diff_has_no_findings() -> None:
	delta = _delta("src/app.py", _hunk("def add(a, b):", "    return a + b"))
	assert tier1.run(delta) == []


# ---------------------------------------------------------------------------
# CRITICAL: a secret only on a REMOVED line must never fire
# ---------------------------------------------------------------------------


def test_secret_on_removed_line_produces_no_findings() -> None:
	secret = "AKIA1234567890ABCDEF"
	hunk = "\n".join(
		[
			"@@ -1,2 +1,1 @@",
			f'-KEY = "{secret}"',
			" unchanged = 1",
		]
	)
	delta = _delta("src/aws.py", hunk)
	assert tier1.run(delta) == []


def test_diff_header_plus_line_is_not_treated_as_added() -> None:
	# The '+++' file header must never be scanned as an added line.
	hunk = "\n".join(
		[
			"@@ -0,0 +1,1 @@",
			"+++ this is context-looking but starts with +++",
		]
	)
	delta = _delta("src/app.py", hunk)
	assert tier1.run(delta) == []


# ---------------------------------------------------------------------------
# Blocking policy
# ---------------------------------------------------------------------------


def test_should_block_true_for_secrets() -> None:
	findings = [Finding("secrets", "error", "a.py", 1, "x")]
	assert tier1.should_block(findings, tier1.DEFAULT_BLOCK_ON) is True


def test_should_block_false_for_debug_only() -> None:
	findings = [Finding("debug_code", "warn", "a.py", 1, "x")]
	assert tier1.should_block(findings, tier1.DEFAULT_BLOCK_ON) is False


# ---------------------------------------------------------------------------
# Redaction removes the secret text
# ---------------------------------------------------------------------------


def test_redact_removes_secret_from_raw_diff() -> None:
	secret = "AKIA1234567890ABCDEF"
	hunk = _hunk(f'KEY = "{secret}"')
	raw = "diff --git a/src/aws.py b/src/aws.py\n" + hunk
	delta = _delta("src/aws.py", hunk, raw_diff=raw)

	findings = tier1.run(delta)
	redacted = tier1.redact(delta, findings)

	assert secret not in redacted.raw_diff
	assert "[REDACTED]" in redacted.raw_diff
	assert all(secret not in h for f in redacted.files for h in f.hunks)


def test_redact_noop_without_secret_findings() -> None:
	delta = _delta("src/ui.js", _hunk("console.log('here')"))
	findings = tier1.run(delta)
	assert tier1.redact(delta, findings) is delta
