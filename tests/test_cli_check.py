"""Integration-style tests for the `quack check` CLI command.

These patch gitio.staged_delta with a fabricated StagedDelta so no real git
or subprocess is involved.
"""

from __future__ import annotations

import time

import pytest
from click.testing import CliRunner

from quack import cli, reviewcache, sonar, sonar_cli, sonar_state, tier2
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
	for name in (
		"QUACK_SONAR_BRANCH",
		"QUACK_SONAR_HOST_URL",
		"QUACK_SONAR_MCP_PROJECT_PATH",
		"QUACK_SONAR_PROJECT_KEY",
		"SONARQUBE_BRANCH",
		"SONARQUBE_PROJECT_KEY",
		"SONARQUBE_URL",
	):
		monkeypatch.delenv(name, raising=False)


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
	monkeypatch.setattr(cli.sonar, "scan", lambda current, root: None)
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


def test_check_passes_sonar_result_to_report(monkeypatch) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	sonar_result = object()
	monkeypatch.setattr(cli.sonar, "scan", lambda current, root: sonar_result)
	captured: dict = {}
	monkeypatch.setattr(cli.render, "report", lambda **kwargs: captured.update(kwargs))

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured["sonar"] is sonar_result


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


def test_check_reuses_matching_completed_sonar_state(monkeypatch, tmp_path) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	cached = sonar_state.State(
		digest="matching",
		scope="staged",
		repo_root=str(tmp_path),
		project_key="quack-local",
		host_url="http://127.0.0.1:9002",
		branch=None,
		status="passed",
		reason="complete",
		violation_count=0,
		timestamp=time.time(),
		scan_status="passed",
		analysis_id="analysis-1",
	)
	report_calls: list[dict] = []
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar_state, "read", lambda *args, **kwargs: cached)
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("matching Sonar state must skip the scanner")
		),
	)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, **kwargs: (
			report_calls.append(kwargs)
			or sonar_cli.SonarCliResult(
				status="passed",
				reason="current direct API read",
				project_key="quack-local",
				branch=None,
				analysis_id="analysis-1",
			)
		),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert report_calls[0]["expected_analysis_id"] == "analysis-1"
	assert report_calls[0]["host_url"] == "http://127.0.0.1:9002"


def test_check_ignores_mcp_url_and_uses_scanner_host(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	cached = sonar_state.State(
		digest="matching",
		scope="staged",
		repo_root=str(tmp_path),
		project_key="quack-local",
		host_url="http://127.0.0.1:9002",
		branch=None,
		status="passed",
		reason="complete",
		violation_count=0,
		timestamp=time.time(),
		scan_status="passed",
	)
	report_calls: list[dict] = []
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setenv("QUACK_SONAR_MCP_URL", "https://codescan.abb.com")
	monkeypatch.setattr(cli.sonar_state, "read", lambda *args, **kwargs: cached)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, **kwargs: (
			report_calls.append(kwargs)
			or sonar_cli.SonarCliResult(
				status="passed",
				reason="current MCP read",
				project_key="quack-local",
				branch=None,
			)
		),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert report_calls[0]["host_url"] == "http://127.0.0.1:9002"


def test_check_uses_custom_scanner_url_for_direct_api(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setenv("QUACK_SONAR_HOST_URL", "http://scanner.local:9000")
	captured: dict = {}
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda *args, **kwargs: sonar.ScanResult(
			status="skipped",
			confirmed=False,
		),
	)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, **kwargs: (
			captured.update(kwargs)
			or sonar_cli.SonarCliResult(
				status="skipped",
				reason="MCP unavailable",
			)
		),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured["host_url"] == "http://scanner.local:9000"


def test_check_rescans_when_cached_analysis_no_longer_correlates(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	cached = sonar_state.State(
		digest="matching",
		scope="staged",
		repo_root=str(tmp_path),
		project_key="quack-local",
		host_url="http://127.0.0.1:9002",
		branch=None,
		status="passed",
		reason="old violation",
		violation_count=1,
		timestamp=time.time(),
		scan_status="passed",
		task_id="task-1",
		analysis_id="analysis-1",
	)
	captured: dict = {}
	api_calls: list[dict] = []
	scans: list[bool] = []
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar_state, "read", lambda *args, **kwargs: cached)
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda *args, **kwargs: (
			scans.append(True)
			or sonar.ScanResult(
				status="passed",
				confirmed=True,
				task_id="task-2",
				analysis_id="analysis-2",
			)
		),
	)

	def api_result(current, root, **kwargs):
		api_calls.append(kwargs)
		return sonar_cli.SonarCliResult(
			status="passed",
			reason="current direct API result",
			project_key="quack-local",
			violation_count=0,
			branch=None,
			analysis_id=(
				"other-analysis"
				if "expected_analysis_id" in kwargs
				else "analysis-2"
			),
		)

	monkeypatch.setattr(cli.sonar_cli, "run", api_result)
	monkeypatch.setattr(
		cli.render, "report", lambda **kwargs: captured.update(kwargs)
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured["blocked"] is False
	assert scans == [True]
	assert len(api_calls) == 2


def test_check_blocks_revalidated_cached_violation_when_analysis_correlates(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	cached = sonar_state.State(
		digest="matching",
		scope="staged",
		repo_root=str(tmp_path),
		project_key="quack-local",
		host_url="http://127.0.0.1:9002",
		branch=None,
		status="passed",
		reason="old result",
		violation_count=0,
		timestamp=time.time(),
		scan_status="passed",
		task_id="task-1",
		analysis_id="analysis-1",
	)
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar_state, "read", lambda *args, **kwargs: cached)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, **kwargs: sonar_cli.SonarCliResult(
			status="passed",
			reason="current MCP finding",
			project_key="quack-local",
			violation_count=1,
			branch=None,
			analysis_id="analysis-1",
		),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 1


def test_check_writes_sonar_state_only_after_confirmed_scan(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	writes: list[sonar_state.State] = []
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar_state, "read", lambda *args, **kwargs: None)
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda *args, **kwargs: sonar.ScanResult(
			status="passed",
			task_id="task-1",
			analysis_id="analysis-1",
			confirmed=True,
		),
	)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, *, source, **kwargs: sonar_cli.SonarCliResult(
			status="passed",
			reason="current MCP read",
			fresh=True,
		),
	)
	monkeypatch.setattr(
		cli.sonar_state,
		"write",
		lambda root, state: writes.append(state),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert len(writes) == 1
	assert writes[0].task_id == "task-1"
	assert writes[0].analysis_id == "analysis-1"


def test_check_does_not_block_on_unverified_sonar_cli_result(
	monkeypatch, tmp_path
) -> None:
	delta = _delta("src/app.py", _hunk("x = 1"))
	captured: dict = {}
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda *args, **kwargs: sonar.ScanResult(status="skipped", confirmed=False),
	)
	monkeypatch.setattr(
		cli.sonar_cli,
		"run",
		lambda current, root, *, source, **kwargs: sonar_cli.SonarCliResult(
			status="passed",
			reason="stale MCP issue",
			violation_count=1,
			fresh=True,
		),
	)
	monkeypatch.setattr(cli.render, "report", lambda **kwargs: captured.update(kwargs))

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured["blocked"] is False

