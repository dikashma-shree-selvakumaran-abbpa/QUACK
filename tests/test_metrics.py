"""Tests for privacy-safe local metrics and their CLI summary."""

from __future__ import annotations

import json

from click.testing import CliRunner

from quack import cli, metrics, reviewcache
from quack.delta import StagedDelta, StagedFile
from quack.testmap import TestPlan


def test_log_writes_one_parseable_json_line_per_call(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log({"command": "check", "duration_ms": 1})
	metrics.log({"command": "watch", "duration_ms": 2})

	lines = path.read_text(encoding="utf-8").splitlines()
	assert len(lines) == 2
	assert [json.loads(line)["command"] for line in lines] == ["check", "watch"]


def test_log_never_raises_for_unwritable_path(tmp_path, monkeypatch) -> None:
	unwritable = tmp_path / "metrics.jsonl"
	unwritable.mkdir()
	monkeypatch.setattr(metrics, "metrics_path", lambda: unwritable)

	metrics.log({"command": "check"})
	metrics.log({"not_serializable": object()})


def test_log_rolls_over_file_past_max_bytes(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	path.write_text("old metrics", encoding="utf-8")
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)
	monkeypatch.setattr(metrics, "MAX_BYTES", 5)

	metrics.log({"command": "check"})

	assert path.with_name("metrics.jsonl.1").read_text(encoding="utf-8") == "old metrics"
	assert json.loads(path.read_text(encoding="utf-8"))["command"] == "check"


def test_metrics_path_reuses_review_cache_user_data_directory() -> None:
	assert metrics.metrics_path().parent == reviewcache.cache_path().parent


def test_check_metrics_exclude_delta_paths_and_contents(monkeypatch) -> None:
	path_sentinel = "private/customer/secret_source.py"
	content_sentinel = "SUPER_PRIVATE_SOURCE_CONTENT"
	hunk = f"@@ -0,0 +1,1 @@\n+print('{content_sentinel}')"
	delta = StagedDelta(
		files=[StagedFile(path_sentinel, "M", 1, 0, [hunk])],
		raw_diff=hunk,
	)
	captured: list[dict] = []
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(
		cli.testmap,
		"build_plan",
		lambda *args, **kwargs: TestPlan(untested_sources=[path_sentinel]),
	)
	monkeypatch.setattr(cli.reviewcache, "read", lambda *args, **kwargs: None)
	monkeypatch.setattr(cli.metrics_mod, "log", captured.append)
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	serialized = json.dumps(captured)
	assert path_sentinel not in serialized
	assert content_sentinel not in serialized
	assert captured[0]["files"] == 1
	assert captured[0]["untested_sources"] == 1


def test_metrics_command_summarizes_known_file(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)
	metrics.log(
		{
			"command": "check",
			"duration_ms": 10,
			"blocked": True,
			"tier1_findings": {"secrets": 2},
			"review_cache": "hit",
		}
	)
	metrics.log(
		{
			"command": "check",
			"duration_ms": 20,
			"blocked": False,
			"tier1_findings": {"debug_code": 1},
			"review_cache": "miss",
		}
	)
	metrics.log({"command": "agent", "duration_ms": 100})

	result = CliRunner().invoke(cli.main, ["metrics"])

	assert result.exit_code == 0
	assert "Total runs: 3" in result.output
	assert "Runs by command: agent=1, check=2" in result.output
	assert "Blocks: 1" in result.output
	assert "secrets=2" in result.output
	assert "debug_code=1" in result.output
	assert "Median duration: 20 ms" in result.output
	assert "Cache hit rate: 50.0%" in result.output


def test_metrics_command_handles_missing_file_cleanly(tmp_path, monkeypatch) -> None:
	monkeypatch.setattr(metrics, "metrics_path", lambda: tmp_path / "missing.jsonl")

	result = CliRunner().invoke(cli.main, ["metrics"])

	assert result.exit_code == 0
	assert "missing or unreadable" in result.output
