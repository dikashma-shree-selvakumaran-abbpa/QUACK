"""Integration-style tests for the `quack check` CLI command.

These patch gitio.staged_delta with a fabricated StagedDelta so no real git
or subprocess is involved.
"""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from quack import cli, reviewcache, tier2
from quack.delta import StagedDelta, StagedFile


def _hunk(*added_lines: str, start: int = 1) -> str:
	body = "\n".join(f"+{line}" for line in added_lines)
	return f"@@ -0,0 +{start},{len(added_lines)} @@\n{body}"


def _delta(path: str, hunk: str) -> StagedDelta:
	file = StagedFile(path=path, status="M", added=1, removed=0, hunks=[hunk])
	return StagedDelta(files=[file], raw_diff=hunk)


@pytest.fixture(autouse=True)
def _empty_review_cache(monkeypatch):
	monkeypatch.setattr(cli.reviewcache, "read", lambda *args, **kwargs: None)


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


def test_check_passes_nonzero_duration_to_report(monkeypatch) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	clock = iter((10.0, 10.25, 10.3))
	monkeypatch.setattr(cli.time, "perf_counter", lambda: next(clock))
	captured: dict = {}
	monkeypatch.setattr(cli.render, "report", lambda **kwargs: captured.update(kwargs))

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured["duration"] == pytest.approx(0.25)
	assert "BLOCKED" not in result.output


def test_check_is_fully_local_no_ai_section(monkeypatch) -> None:
	"""Commit time performs a cache lookup but never calls Tier 2."""
	delta = _delta(
		"src/app.py",
		_hunk("def add(a, b):", "    return a + b", "x = add(1, 2)"),
	)
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)

	# Any network/AI use would go through tier2.review; make it explode so a
	# regression that re-adds the commit-time call fails loudly.
	def _boom(*args, **kwargs):  # pragma: no cover - only hit on regression
		raise AssertionError("quack check must not call Tier 2 at commit time")

	monkeypatch.setattr(tier2, "review", _boom)
	lookups: list[tuple] = []
	monkeypatch.setattr(
		cli.reviewcache,
		"read",
		lambda *args, **kwargs: lookups.append(args) or None,
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert len(lookups) == 1
	assert "AI review: not reviewed yet" in result.output


def test_check_renders_cached_review_with_age_without_provider(monkeypatch) -> None:
	delta = _delta(
		"src/app.py",
		_hunk("def add(a, b):", "    return a + b", "x = add(1, 2)"),
	)
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: "/repo")
	entry = reviewcache.CacheEntry(
		diff_hash=reviewcache.diff_hash(delta.raw_diff),
		timestamp=time.time() - 125,
		repo_root="/repo",
		review_payload={
			"risk": "medium",
			"reasons": ["src/app.py: boundary changed"],
			"tests_to_run": ["pytest tests/test_app.py"],
			"missing_tests": [],
			"one_liner": "Review complete.",
			"model": "test-model",
		},
	)
	monkeypatch.setattr(cli.reviewcache, "read", lambda *args, **kwargs: entry)

	def _boom(*args, **kwargs):
		raise AssertionError("cache hits must not call Tier 2")

	monkeypatch.setattr(tier2, "review", _boom)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert "Review complete." in result.output
	assert "risk: MEDIUM" in result.output
	assert "reviewed 2 min ago by quack watch" in result.output


def test_check_cache_miss_renders_watch_nudge(monkeypatch) -> None:
	delta = _delta("src/app.py", _hunk("x = 1", "y = 2", "z = 3"))
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert "run `quack watch` to review in the background" in result.output


def test_check_has_no_model_option() -> None:
	"""The --model option was removed from check (commit time makes no AI call)."""
	result = CliRunner().invoke(cli.main, ["check", "--model", "openai/gpt-4o-mini"])

	assert result.exit_code != 0
	assert "no such option" in result.output.lower()


def test_check_nothing_staged(monkeypatch) -> None:
	monkeypatch.setattr(
		cli.gitio, "staged_delta", lambda: StagedDelta(files=[], raw_diff="")
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
