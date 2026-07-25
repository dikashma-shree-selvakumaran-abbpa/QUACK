"""Integration-style tests for the `quack check` CLI command.

These patch gitio.staged_delta with a fabricated StagedDelta so no real git
or subprocess is involved.
"""

from __future__ import annotations

from click.testing import CliRunner

from quack import cli
from quack.delta import StagedDelta, StagedFile


def _hunk(*added_lines: str, start: int = 1) -> str:
	body = "\n".join(f"+{line}" for line in added_lines)
	return f"@@ -0,0 +{start},{len(added_lines)} @@\n{body}"


def _delta(path: str, hunk: str) -> StagedDelta:
	file = StagedFile(path=path, status="M", added=1, removed=0, hunks=[hunk])
	return StagedDelta(files=[file], raw_diff=hunk)


def test_check_blocks_on_secret(monkeypatch) -> None:
	secret = "AKIA" + "A" * 16
	delta = _delta("src/config.py", _hunk(f'AWS_KEY = "{secret}"'))
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 1
	assert "BLOCKED" in result.output


def test_check_passes_on_clean_delta(monkeypatch) -> None:
	delta = _delta(
		"src/app.py",
		_hunk("def add(a, b):", "    return a + b", "x = add(1, 2)"),
	)
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert "BLOCKED" not in result.output


def test_check_nothing_staged(monkeypatch) -> None:
	monkeypatch.setattr(
		cli.gitio, "staged_delta", lambda: StagedDelta(files=[], raw_diff="")
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
