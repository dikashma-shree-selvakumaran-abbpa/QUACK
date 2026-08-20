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


def test_log_replaces_paths_in_failure_strings(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log(
		{
			"failure": (
				"failed at C:\\private\\source.py, /home/user/source.py, "
				"and \\\\server\\share\\source.py"
			)
		}
	)

	failure = json.loads(path.read_text(encoding="utf-8"))["failure"]
	assert failure.count("<path>") == 3
	assert "source.py" not in failure


def test_log_redacts_token_shaped_strings(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log(
		{
			"failure": (
				"tokens ghp_abcdefghijklmnopqrstuvwxyz123456 "
				"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456"
			)
		}
	)

	failure = json.loads(path.read_text(encoding="utf-8"))["failure"]
	assert failure == "tokens <redacted> <redacted>"


def test_log_drops_unexpected_keys(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log({"command": "check", "private_path": "/private/source.py"})

	assert json.loads(path.read_text(encoding="utf-8")) == {"command": "check"}


def test_log_drops_malformed_tier1_findings_entries(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log(
		{
			"tier1_findings": {
				"secrets": 1,
				"debug_code": "one",
				3: 2,
				"nested": {"merge_markers": 1},
			}
		}
	)

	assert json.loads(path.read_text(encoding="utf-8")) == {
		"tier1_findings": {"secrets": 1}
	}


def test_log_sanitization_of_pathological_input_never_raises(
	tmp_path, monkeypatch
) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)

	metrics.log({"failure": object(), "tier1_findings": object()})

	assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_log_normal_event_round_trips_unchanged(tmp_path, monkeypatch) -> None:
	path = tmp_path / "metrics.jsonl"
	monkeypatch.setattr(metrics, "metrics_path", lambda: path)
	event = {
		"ts": "2026-01-02T03:04:05+00:00",
		"command": "check",
		"duration_ms": 12,
		"files": 2,
		"lines_added": 4,
		"lines_removed": 1,
		"tier1_findings": {"debug_code": 1},
		"blocked": False,
		"tests_mapped": 1,
		"untested_sources": 0,
		"review_cache": "hit",
		"risk": "low",
		"exit": 0,
	}

	metrics.log(event)

	assert json.loads(path.read_text(encoding="utf-8")) == event


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
