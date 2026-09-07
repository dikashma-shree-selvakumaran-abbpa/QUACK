"""Tests for the shared SonarQube MCP snapshot and living report."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from quack import cli, sonar_report, watch
from quack.delta import StagedDelta, StagedFile
from quack.testmap import TestPlan
from quack.tier2 import ReviewResult


def _delta() -> StagedDelta:
	hunk = "@@ -1,1 +1,2 @@\n value = 1\n+value = 2"
	return StagedDelta(
		files=[StagedFile("src/app.py", "M", 1, 0, [hunk])],
		raw_diff=hunk,
	)


def _tool(name: str, properties: dict[str, dict]) -> dict:
	return {
		"type": "function",
		"function": {
			"name": name,
			"parameters": {
				"type": "object",
				"properties": properties,
			},
		},
	}


class _CompleteClient:
	project_key = "quack-local"

	def __init__(self) -> None:
		self.calls: list[tuple[str, dict]] = []

	def tool_definitions(self) -> list[dict]:
		return [
			_tool("sonarqube_search_metrics", {"ps": {}, "p": {}}),
			_tool("sonarqube_get_duplications", {"key": {}}),
			_tool(
				"sonarqube_search_security_hotspot",
				{"projectKey": {}, "status": {}, "ps": {}, "p": {}},
			),
			_tool(
				"sonarqube_search_sonar_issues_in_projects",
				{"projects": {}, "issueStatuses": {}, "ps": {}, "p": {}},
			),
			_tool(
				"sonarqube_get_component_measures",
				{"projectKey": {}, "metricKeys": {}},
			),
		]

	def call_tool_data(self, name: str, arguments: dict) -> dict:
		self.calls.append((name, arguments))
		if name == "sonarqube_search_metrics":
			return {"metrics": [{"key": "custom_metric"}]}
		if name == "sonarqube_get_duplications":
			return {"duplications": [{"blocks": [{}, {}]}]}
		if name == "sonarqube_search_security_hotspot":
			return {"hotspots": [], "paging": {"total": 0}}
		if name == "sonarqube_search_sonar_issues_in_projects":
			return {
				"issues": [
					{
						"key": "ISSUE-1",
						"severity": "MAJOR",
						"status": "OPEN",
						"component": "quack-local:src/app.py",
						"message": (
							"Investigate ghp_" + ("a" * 36)
						),
					}
				],
				"paging": {"total": 1},
			}
		if name == "sonarqube_get_component_measures":
			return {
				"metrics": [
					{
						"key": "custom_metric",
						"description": "Team-specific quality measure",
					}
				],
				"component": {
					"measures": [
						{"metric": "cognitive_complexity", "value": "12"},
						{"metric": "ncloc", "value": "100"},
						{"metric": "reliability_rating", "value": "1.0"},
						{"metric": "custom_metric", "value": "7"},
					]
				},
			}
		raise AssertionError(f"unexpected tool: {name}")


def test_run_collects_all_requested_tools_and_writes_metrics_report(
	monkeypatch,
	tmp_path: Path,
) -> None:
	client = _CompleteClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.status == "passed"
	assert result.project_key == "quack-local"
	assert result.violation_count == 1
	assert result.blocks_commit is True
	assert "1 SonarQube violation(s) detected" in result.reason
	assert result.report_path == tmp_path / "docs" / "SONARQUBE_REPORT.md"
	assert [name for name, _ in client.calls].count(
		"sonarqube_get_duplications"
	) == 1
	assert {
		name for name, _ in client.calls
	} >= {
		"sonarqube_search_metrics",
		"sonarqube_get_duplications",
		"sonarqube_search_security_hotspot",
		"sonarqube_search_sonar_issues_in_projects",
		"sonarqube_get_component_measures",
	}

	calls = dict(client.calls)
	assert calls["sonarqube_get_duplications"] == {"key": "quack-local:src/app.py"}
	assert calls["sonarqube_search_security_hotspot"]["projectKey"] == (
		"quack-local"
	)
	assert calls["sonarqube_search_security_hotspot"]["status"] == "TO_REVIEW"
	assert calls["sonarqube_search_sonar_issues_in_projects"]["projects"] == [
		"quack-local"
	]
	assert calls["sonarqube_search_sonar_issues_in_projects"]["issueStatuses"] == [
		"OPEN"
	]
	measure_keys = calls["sonarqube_get_component_measures"]["metricKeys"]
	assert set(sonar_report.REQUIRED_METRIC_KEYS).issubset(measure_keys)
	assert "custom_metric" in measure_keys

	content = result.report_path.read_text(encoding="utf-8")
	assert "sonarqube_get_duplications" in content
	assert "no security hotspots are awaiting review" in content
	assert "1 open SonarQube issue(s) were found" in content
	assert "`cognitive_complexity`" in content
	assert "`ncloc`" in content
	assert "`reliability_rating`" in content
	assert "`custom_metric`" in content
	assert "`bugs`" in content
	assert "Team-specific quality measure" in content
	assert "ghp_" not in content
	assert "<redacted>" in content
	assert list(result.report_path.parent.glob(".*.tmp")) == []


def test_run_is_fail_open_when_required_tools_are_missing(
	monkeypatch,
	tmp_path: Path,
) -> None:
	class MissingClient:
		project_key = "quack-local"

		def tool_definitions(self) -> list[dict]:
			return [_tool("sonarqube_search_metrics", {})]

		def call_tool_data(self, name: str, arguments: dict) -> dict:
			return {"metrics": []}

	client = MissingClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.status == "failed"
	assert "required MCP tool is unavailable" in result.reason
	assert result.report_path is not None
	content = result.report_path.read_text(encoding="utf-8")
	assert "Status: **FAILED**" in content
	assert "required MCP tool is unavailable" in content


def test_precommit_snapshot_does_not_modify_living_report(
	monkeypatch,
	tmp_path: Path,
) -> None:
	client = _CompleteClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="pre-commit")

	assert result is not None
	assert result.violation_count == 1
	assert result.report_path is None
	assert not (tmp_path / "docs" / "SONARQUBE_REPORT.md").exists()


def test_run_honors_external_mcp_project_path(
	monkeypatch,
	tmp_path: Path,
) -> None:
	external_path = tmp_path / "external-project"
	external_path.mkdir()
	client = _CompleteClient()
	captured: dict = {}
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)

	def connect(**kwargs):
		captured.update(kwargs)
		return client, None

	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		connect,
	)
	monkeypatch.setenv(
		"QUACK_SONAR_MCP_PROJECT_PATH",
		str(external_path),
	)

	sonar_report.run(_delta(), tmp_path / "repository", source="test")

	assert captured["project_path"] == str(external_path)
	assert captured["base_path"] == (tmp_path / "repository").resolve()


def test_watch_snapshot_excludes_living_report(tmp_path: Path) -> None:
	report = tmp_path / "docs" / "SONARQUBE_REPORT.md"
	report.parent.mkdir()
	report.write_text("report", encoding="utf-8")
	kept = tmp_path / "src" / "app.py"
	kept.parent.mkdir()
	kept.write_text("value = 1", encoding="utf-8")

	state = watch.snapshot(tmp_path)

	assert "docs/SONARQUBE_REPORT.md" not in state
	assert "src/app.py" in state


def test_check_passes_mcp_snapshot_to_rendering_and_metrics(
	monkeypatch,
	tmp_path: Path,
) -> None:
	delta = _delta()
	mcp_result = sonar_report.SonarQubeReportResult(
		status="passed",
		reason="snapshot complete",
		duration_s=0.25,
		report_path=tmp_path / "docs" / "SONARQUBE_REPORT.md",
	)
	captured_report: dict = {}
	captured_events: list[dict] = []
	captured_call: dict = {}

	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar, "scan", lambda current, root: None)

	def capture_mcp(current, root, *, source):
		captured_call.update(delta=current, root=root, source=source)
		return mcp_result

	monkeypatch.setattr(cli.sonar_report, "run", capture_mcp)
	monkeypatch.setattr(
		cli.render,
		"report",
		lambda **kwargs: captured_report.update(kwargs),
	)
	monkeypatch.setattr(
		cli.metrics_mod,
		"log",
		lambda event: captured_events.append(event),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 0
	assert captured_call == {
		"delta": delta,
		"root": str(tmp_path),
		"source": "pre-commit",
	}
	assert captured_report["sonar_mcp"] is mcp_result
	assert captured_events[0]["sonar_mcp_status"] == "passed"
	assert captured_events[0]["sonar_mcp_duration_ms"] == 250


def test_check_blocks_when_mcp_reports_violations(
	monkeypatch,
	tmp_path: Path,
) -> None:
	delta = _delta()
	mcp_result = sonar_report.SonarQubeReportResult(
		status="passed",
		reason="1 SonarQube violation(s) detected",
		duration_s=0.25,
		violation_count=1,
	)
	captured_report: dict = {}
	captured_events: list[dict] = []

	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: str(tmp_path))
	monkeypatch.setenv("QUACK_DISABLE_GITLEAKS", "1")
	monkeypatch.setattr(cli.sonar, "scan", lambda current, root: None)
	monkeypatch.setattr(
		cli.sonar_report,
		"run",
		lambda current, root, *, source: mcp_result,
	)
	monkeypatch.setattr(
		cli.render,
		"report",
		lambda **kwargs: captured_report.update(kwargs),
	)
	monkeypatch.setattr(
		cli.metrics_mod,
		"log",
		lambda event: captured_events.append(event),
	)

	result = CliRunner().invoke(cli.main, ["check"])

	assert result.exit_code == 1
	assert captured_report["blocked"] is True
	assert captured_report["sonar_mcp"] is mcp_result
	assert captured_events[0]["blocked"] is True
	assert captured_events[0]["exit"] == 1


def test_watch_passes_mcp_snapshot_to_review(
	monkeypatch,
	tmp_path: Path,
) -> None:
	delta = _delta()
	mcp_result = sonar_report.SonarQubeReportResult(
		status="passed",
		reason="snapshot complete",
		duration_s=0.1,
	)
	captured_call: dict = {}
	monkeypatch.setattr(watch.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(watch.sonar_report, "run", lambda *args, **kwargs: (
		captured_call.update(kwargs) or mcp_result
	))
	monkeypatch.setattr(watch.testmap, "build_plan", lambda *args, **kwargs: TestPlan())
	monkeypatch.setattr(watch.instructions, "load", lambda root: None)
	monkeypatch.setattr(watch.llmio, "default_model", lambda kind: "test-model")
	monkeypatch.setattr(watch.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(
		watch.tier2,
		"review_with_reason",
		lambda *args, **kwargs: (
			ReviewResult(risk="low", one_liner="Review complete."),
			None,
		),
	)

	result = watch._review_once(tmp_path)

	assert result.sonar_report is mcp_result
	assert captured_call == {"source": "watch"}
