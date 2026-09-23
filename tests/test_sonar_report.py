"""Tests for the shared SonarQube MCP snapshot and living report."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from quack import cli, sonar, sonar_report, watch
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
	assert "custom_metric" not in measure_keys

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


def test_optional_endpoint_404s_do_not_make_findings_snapshot_incomplete(
	monkeypatch, tmp_path: Path
) -> None:
	class OptionalEndpoint404Client(_CompleteClient):
		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name in {
				"sonarqube_get_duplications",
				"sonarqube_get_component_measures",
			}:
				raise sonar_report.sonarqube_mcp.SonarQubeMcpUnavailable(
					"An error occurred during the tool execution: "
					"SonarQube answered with Error 404 on "
					"https://codescan.abb.com/api/example"
				)
			return super().call_tool_data(name, arguments)

	client = OptionalEndpoint404Client()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.status == "partial"
	assert result.violation_count == 1
	assert result.blocks_commit is True
	assert "optional check gaps" in result.reason
	assert "HTTP 404" in result.reason
	assert any(
		outcome.status == "unavailable"
		for outcome in result.outcomes
		if outcome.name == "sonarqube_get_component_measures"
	)


def test_component_measure_request_uses_stable_metric_set(
	monkeypatch, tmp_path: Path
) -> None:
	class LargeMetricCatalogClient(_CompleteClient):
		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_search_metrics":
				return {
					"metrics": [
						{"key": f"custom_metric_{index}"}
						for index in range(240)
					]
				}
			return super().call_tool_data(name, arguments)

	client = LargeMetricCatalogClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	measure_calls = [
		arguments
		for name, arguments in client.calls
		if name == "sonarqube_get_component_measures"
	]
	assert len(measure_calls) == 1
	metric_keys = measure_calls[0]["metricKeys"]
	assert metric_keys == list(sonar_report.DEFAULT_METRIC_KEYS)


def test_confirmed_findings_can_block_when_another_finding_endpoint_is_unavailable(
	monkeypatch, tmp_path: Path
) -> None:
	class HotspotEndpoint404Client(_CompleteClient):
		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_search_security_hotspot":
				raise sonar_report.sonarqube_mcp.SonarQubeMcpUnavailable(
					"Error 404 on https://codescan.abb.com/api/hotspots/search"
				)
			return super().call_tool_data(name, arguments)

	client = HotspotEndpoint404Client()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.status == "partial"
	assert result.violation_count == 1
	assert result.blocks_commit is True
	assert "HTTP 404" in result.reason


def test_unset_branch_is_not_sent_as_a_null_mcp_argument(
	monkeypatch, tmp_path: Path
) -> None:
	class BranchAwareClient(_CompleteClient):
		def tool_definitions(self) -> list[dict]:
			tools = super().tool_definitions()
			for tool in tools:
				name = tool["function"]["name"]
				if name in {
					"sonarqube_search_security_hotspot",
					"sonarqube_search_sonar_issues_in_projects",
					"sonarqube_get_component_measures",
				}:
					tool["function"]["parameters"]["properties"]["branch"] = {}
			return tools

	client = BranchAwareClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.status == "passed"
	for name, arguments in client.calls:
		if name in {
			"sonarqube_search_security_hotspot",
			"sonarqube_search_sonar_issues_in_projects",
			"sonarqube_get_component_measures",
		}:
			assert "branch" not in arguments


def test_configured_branch_is_forwarded_and_reported(
	monkeypatch, tmp_path: Path
) -> None:
	class BranchAwareClient(_CompleteClient):
		branch = "validation"

		def tool_definitions(self) -> list[dict]:
			tools = super().tool_definitions()
			for tool in tools:
				name = tool["function"]["name"]
				if name in {
					"sonarqube_search_security_hotspot",
					"sonarqube_search_sonar_issues_in_projects",
					"sonarqube_get_component_measures",
				}:
					tool["function"]["parameters"]["properties"]["branch"] = {}
			return tools + [_tool("sonarqube_list_branches", {})]

		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_list_branches":
				return {"branches": [{"name": "validation"}]}
			return super().call_tool_data(name, arguments)

	client = BranchAwareClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="watch")

	assert result is not None
	assert result.status == "passed"
	assert result.branch == "validation"
	assert result.correlated is True
	for name, arguments in client.calls:
		if name in {
			"sonarqube_search_security_hotspot",
			"sonarqube_search_sonar_issues_in_projects",
			"sonarqube_get_component_measures",
		}:
			assert arguments["branch"] == "validation"
	content = (tmp_path / "docs" / "SONARQUBE_REPORT.md").read_text(
		encoding="utf-8"
	)
	assert "- Branch: `validation`" in content


def test_known_schema_omits_unsupported_optional_arguments(
	monkeypatch, tmp_path: Path
) -> None:
	class NoBranchClient(_CompleteClient):
		branch = "validation"

		def tool_definitions(self) -> list[dict]:
			return super().tool_definitions() + [
				_tool("sonarqube_list_branches", {})
			]

		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_list_branches":
				return {"branches": [{"name": "validation"}]}
			return super().call_tool_data(name, arguments)

	client = NoBranchClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="watch")

	assert result is not None
	assert result.status == "passed"
	assert result.correlated is True
	for name, arguments in client.calls:
		if name in {
			"sonarqube_search_security_hotspot",
			"sonarqube_search_sonar_issues_in_projects",
			"sonarqube_get_component_measures",
		}:
			assert "branch" not in arguments


def test_missing_branch_cannot_be_treated_as_current_sonar_evidence(
	monkeypatch, tmp_path: Path
) -> None:
	class MissingBranchClient(_CompleteClient):
		branch = "validation"

		def tool_definitions(self) -> list[dict]:
			return super().tool_definitions() + [
				_tool("sonarqube_list_branches", {})
			]

		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_list_branches":
				return {"branches": [{"name": "main"}]}
			return super().call_tool_data(name, arguments)

	client = MissingBranchClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="watch")

	assert result is not None
	assert result.status == "failed"
	assert result.violation_count == 0
	assert result.correlated is False
	assert result.blocks_commit is False
	assert "has no uploaded analysis" in result.reason


def test_cached_mcp_snapshot_is_unverified_without_analysis_correlation(
	monkeypatch, tmp_path: Path
) -> None:
	client = _CompleteClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(
		_delta(),
		tmp_path,
		source="pre-commit",
		project_key="quack-local",
		expected_analysis_id="analysis-1",
	)

	assert result is not None
	assert result.status == "passed"
	assert result.correlated is False
	assert result.blocks_commit is False
	assert "could not be correlated" in result.reason


def test_mcp_snapshot_is_unverified_when_local_watch_scan_is_unavailable(
	monkeypatch, tmp_path: Path
) -> None:
	client = _CompleteClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(
		_delta(),
		tmp_path,
		source="watch",
		project_key="quack-local",
		fresh=False,
	)

	assert result is not None
	assert result.fresh is False
	assert result.blocks_commit is False
	assert "MCP result is unverified" in result.reason


def test_cached_mcp_snapshot_blocks_when_analysis_correlation_matches(
	monkeypatch, tmp_path: Path
) -> None:
	class CorrelatedClient(_CompleteClient):
		def call_tool_data(self, name: str, arguments: dict) -> dict:
			data = super().call_tool_data(name, arguments)
			data["analysisId"] = "analysis-1"
			return data

	client = CorrelatedClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(
		_delta(),
		tmp_path,
		source="pre-commit",
		project_key="quack-local",
		expected_analysis_id="analysis-1",
	)

	assert result is not None
	assert result.analysis_id == "analysis-1"
	assert result.correlated is True
	assert result.blocks_commit is True


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


def test_run_explicit_project_path_overrides_external_environment(
	monkeypatch,
	tmp_path: Path,
) -> None:
	external_path = tmp_path / "external-project"
	external_path.mkdir()
	repository_path = tmp_path / "repository"
	repository_path.mkdir()
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

	sonar_report.run(
		_delta(),
		repository_path,
		source="pre-commit",
		project_path=repository_path,
	)

	assert captured["project_path"] == str(repository_path)


def test_run_scopes_issue_and_hotspot_counts_to_changed_components(
	monkeypatch, tmp_path: Path
) -> None:
	class ScopedClient(_CompleteClient):
		def tool_definitions(self) -> list[dict]:
			tools = super().tool_definitions()
			for tool in tools:
				name = tool["function"]["name"]
				if name in {
					"sonarqube_search_security_hotspot",
					"sonarqube_search_sonar_issues_in_projects",
				}:
					tool["function"]["parameters"]["properties"][
						"componentKeys"
					] = {}
			return tools

		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_search_security_hotspot":
				self.calls.append((name, arguments))
				return {
					"hotspots": [
						{
							"key": "HOTSPOT-CURRENT",
							"component": "quack-local:src/app.py",
							"status": "TO_REVIEW",
						},
						{
							"key": "HOTSPOT-OTHER",
							"component": "quack-local:src/other.py",
							"status": "TO_REVIEW",
						},
					],
					"paging": {"total": 2},
				}
			if name == "sonarqube_search_sonar_issues_in_projects":
				self.calls.append((name, arguments))
				return {
					"issues": [
						{
							"key": "ISSUE-CURRENT",
							"component": "quack-local:src/app.py",
							"status": "OPEN",
						},
						{
							"key": "ISSUE-OTHER",
							"component": "quack-local:src/other.py",
							"status": "OPEN",
						},
					],
					"paging": {"total": 2},
				}
			return super().call_tool_data(name, arguments)

	client = ScopedClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.violation_count == 2
	issue_call = next(
		args
		for name, args in client.calls
		if name == "sonarqube_search_sonar_issues_in_projects"
	)
	hotspot_call = next(
		args
		for name, args in client.calls
		if name == "sonarqube_search_security_hotspot"
	)
	assert issue_call["componentKeys"] == ["quack-local:src/app.py"]
	assert hotspot_call["componentKeys"] == ["quack-local:src/app.py"]


def test_run_paginates_unscoped_findings_before_filtering(
	monkeypatch, tmp_path: Path
) -> None:
	class PagedClient(_CompleteClient):
		def call_tool_data(self, name: str, arguments: dict) -> dict:
			if name == "sonarqube_search_sonar_issues_in_projects":
				self.calls.append((name, arguments))
				if arguments.get("p") == 1:
					items = [
						{
							"key": "ISSUE-OTHER",
							"component": "quack-local:src/other.py",
							"status": "OPEN",
						}
					]
				else:
					items = [
						{
							"key": "ISSUE-CURRENT",
							"component": "quack-local:src/app.py",
							"status": "OPEN",
						}
					]
				return {"issues": items, "paging": {"total": 2}}
			return super().call_tool_data(name, arguments)

	client = PagedClient()
	monkeypatch.setattr(sonar_report.sonarqube_mcp, "enabled", lambda: True)
	monkeypatch.setattr(
		sonar_report.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (client, None),
	)

	result = sonar_report.run(_delta(), tmp_path, source="test")

	assert result is not None
	assert result.violation_count == 1
	issue_calls = [
		args
		for name, args in client.calls
		if name == "sonarqube_search_sonar_issues_in_projects"
	]
	assert [call["p"] for call in issue_calls] == [1, 2]


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
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda current, root: sonar.ScanResult(status="passed"),
	)

	def capture_mcp(current, root, *, source, **kwargs):
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
	monkeypatch.setattr(
		cli.sonar,
		"scan",
		lambda current, root: sonar.ScanResult(status="passed"),
	)
	monkeypatch.setattr(
		cli.sonar_report,
		"run",
		lambda current, root, *, source, **kwargs: mcp_result,
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
	monkeypatch.setattr(
		watch.sonar,
		"configuration",
		lambda root, staged=False: sonar.ScanConfig(
			host_url="https://sonar.example",
			project_key="quack-local",
			project_name="quack-local",
			branch="validation",
			sources="src",
			tests="tests",
		),
	)
	scan_result = sonar.ScanResult(
		status="passed",
		reason="analysis completed",
		analysis_id="analysis-1",
	)
	order: list[str] = []

	def scan(*args, **kwargs):
		order.append("scan")
		return scan_result

	monkeypatch.setattr(watch.sonar, "scan", scan)
	monkeypatch.setattr(watch.gitio, "working_delta", lambda root=None: delta)
	monkeypatch.setattr(watch.gitio, "staged_delta", lambda: delta)

	def run_mcp(*args, **kwargs):
		order.append("mcp")
		captured_call.update(kwargs)
		return mcp_result

	monkeypatch.setattr(watch.sonar_report, "run", run_mcp)
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
	assert result.sonar_scan is scan_result
	assert order == ["scan", "mcp"]
	assert {
		key: captured_call[key]
		for key in ("source", "project_path", "project_key", "branch", "fresh", "expected_analysis_id")
	} == {
		"source": "watch",
		"project_path": tmp_path,
		"project_key": "quack-local",
		"branch": "validation",
		"fresh": True,
		"expected_analysis_id": "analysis-1",
	}
	assert isinstance(captured_call["minimum_analysis_at"], float)


def test_watch_uses_combined_staged_and_unstaged_working_delta(
	monkeypatch, tmp_path
) -> None:
	working = _delta()
	working.files.append(
		working.files[0].__class__(
			"src/unstaged.py",
			"M",
			1,
			0,
			["@ @ -1,1 +1,2 @ @\n value = 1\n+value = 2"],
		)
	)
	captured: dict = {}
	monkeypatch.setattr(
		watch.sonar,
		"configuration",
		lambda root, staged=False: sonar.ScanConfig(
			host_url="https://sonar.example",
			project_key="quack-local",
			project_name="quack-local",
			branch=None,
			sources="src",
			tests="tests",
		),
	)
	scan_result = sonar.ScanResult(
		status="skipped",
		reason="Sonar token is not set",
	)
	monkeypatch.setattr(watch.sonar, "scan", lambda *args, **kwargs: scan_result)
	monkeypatch.setattr(watch.gitio, "working_delta", lambda root=None: working)
	monkeypatch.setattr(
		watch.gitio,
		"staged_delta",
		lambda: (_ for _ in ()).throw(
			AssertionError("working delta should be preferred when non-empty")
		),
	)
	monkeypatch.setattr(
		watch.sonar_report,
		"run",
		lambda current, root, *, source, project_path, **kwargs: (
			captured.update(
				delta=current,
				source=source,
				project_path=project_path,
				sonar_kwargs=kwargs,
			)
			or sonar_report.SonarQubeReportResult(
				status="passed",
				reason="snapshot complete",
			)
		),
	)
	monkeypatch.setattr(watch.testmap, "build_plan", lambda *args, **kwargs: TestPlan())
	monkeypatch.setattr(watch.instructions, "load", lambda root: None)
	monkeypatch.setattr(watch.llmio, "default_model", lambda kind: None)

	result = watch._review_once(tmp_path)

	assert result.files == 2
	assert captured["delta"] is working
	assert captured["source"] == "watch"
	assert captured["project_path"] == tmp_path
	assert captured["sonar_kwargs"]["project_key"] == "quack-local"
	assert captured["sonar_kwargs"]["branch"] is None
	assert captured["sonar_kwargs"]["fresh"] is False
	assert result.sonar_scan is scan_result
	assert result.sonar_report is not None
