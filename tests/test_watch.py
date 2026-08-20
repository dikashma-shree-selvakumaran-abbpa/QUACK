"""Tests for one-shot background review orchestration."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from quack import cli, watch
from quack.delta import StagedDelta, StagedFile
from quack.testmap import TestPlan
from quack.tier2 import ReviewResult


def _delta() -> StagedDelta:
	hunk = "@@ -1,1 +1,2 @@\n value = 1\n+value = 2"
	return StagedDelta(
		files=[StagedFile("src/app.py", "M", 1, 0, [hunk])],
		raw_diff=hunk,
	)


def test_watch_once_reviews_and_writes_cache(monkeypatch) -> None:
	delta = _delta()
	written: dict = {}
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: "/repo")
	monkeypatch.setattr(watch.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(watch.gitio, "working_delta", lambda: StagedDelta())
	monkeypatch.setattr(watch.testmap, "build_plan", lambda *args, **kwargs: TestPlan())
	monkeypatch.setattr(watch.instructions, "load", lambda root: "local guidance")
	monkeypatch.setattr(watch.llmio, "default_model", lambda kind: "test-model")
	monkeypatch.setattr(watch.llmio, "default_timeout", lambda: 12.0)
	monkeypatch.setattr(watch.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(
		watch.tier2,
		"review",
		lambda *args, **kwargs: ReviewResult(
			risk="medium", one_liner="Review complete."
		),
	)

	def capture(repo_root, digest, payload, **kwargs):
		written.update(repo_root=str(repo_root), digest=digest, payload=payload)

	monkeypatch.setattr(watch.reviewcache, "write", capture)

	result = CliRunner().invoke(cli.main, ["watch", "--once"])

	assert result.exit_code == 0
	assert "reviewed 1 file(s) - risk: medium" in result.output
	assert Path(written["repo_root"]) == Path("/repo")
	assert written["digest"] == watch.reviewcache.diff_hash(delta.raw_diff)
	assert written["payload"]["risk"] == "medium"
	assert written["payload"]["model"] == "test-model"
