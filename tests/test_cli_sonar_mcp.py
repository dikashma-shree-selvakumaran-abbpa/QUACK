"""Tests for the explicit SonarQube MCP CLI command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from quack import cli


def test_sonar_mcp_reports_actionable_setup_error(monkeypatch) -> None:
	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda: (None, "set SQ_TOKEN or SONARQUBE_TOKEN"),
	)

	result = CliRunner().invoke(cli.main, ["sonar-mcp"])

	assert result.exit_code == 1
	assert "SonarQube MCP: connecting via Podman..." in result.output
	assert "set SQ_TOKEN or SONARQUBE_TOKEN" in result.output


def test_sonar_mcp_lists_connected_tools(monkeypatch) -> None:
	class FakeClient:
		project_key = "quack-local"

		def tool_definitions(self):
			return [
				{
					"function": {
						"name": "sonarqube_search_sonar_issues_in_projects"
					}
				},
				{"function": {"name": "sonarqube_measures"}},
			]

		def call_tool_data(self, name, arguments):
			return {"issues": [], "paging": {"total": 0}}

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (FakeClient(), None),
	)

	result = CliRunner().invoke(
		cli.main, ["sonar-mcp", "--project-key", "quack-local"]
	)

	assert result.exit_code == 0
	assert "SonarQube MCP: discovering available tools" in result.output
	assert "SonarQube MCP connected via podman (2 tool(s))" in result.output
	assert "no open security violations found for quack-local" in result.output


def test_sonar_mcp_reports_vulnerability_query_failure(monkeypatch) -> None:
	class FakeClient:
		project_key = "quack-local"

		def tool_definitions(self):
			return [
				{
					"function": {
						"name": "sonarqube_search_sonar_issues_in_projects"
					}
				}
			]

		def call_tool_data(self, name, arguments):
			raise cli.sonarqube_mcp.SonarQubeMcpUnavailable(
				"SonarQube returned HTTP 503"
			)

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (FakeClient(), None),
	)

	result = CliRunner().invoke(
		cli.main, ["sonar-mcp", "--project-key", "quack-local"]
	)

	assert result.exit_code == 1
	assert "SonarQube vulnerability report failed" in result.output
	assert "SonarQube returned HTTP 503" in result.output


def test_sonar_mcp_invokes_arbitrary_tool_with_json_arguments(monkeypatch) -> None:
	captured = {}
	connection = {}

	class FakeClient:
		project_key = None

		def tool_definitions(self):
			return [
				{
					"function": {
						"name": "sonarqube_get_component_measures"
					}
				}
			]

		def resolve_tool_name(self, name):
			return (
				"sonarqube_get_component_measures"
				if name in {
					"sonarqube_get_component_measures",
					"get_component_measures",
				}
				else None
			)

		def call_tool_response(self, name, arguments):
			captured["name"] = name
			captured["arguments"] = arguments
			return {
				"content": [{"type": "text", "text": "measures found"}],
				"structuredContent": {"component": {"key": "quack-local"}},
			}

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (connection.update(kwargs) or FakeClient(), None),
	)

	result = CliRunner().invoke(
		cli.main,
		[
			"sonar-mcp",
			"--toolset",
			"measures,coverage",
			"--tool",
			"get_component_measures",
			"--arguments",
			'{"component":"quack-local","metricKeys":["ncloc"]}',
		],
	)

	assert result.exit_code == 0
	assert connection["toolsets"] == ("measures", "coverage")
	assert captured == {
		"name": "sonarqube_get_component_measures",
		"arguments": {
			"component": "quack-local",
			"metricKeys": ["ncloc"],
		},
	}
	assert "SonarQube MCP tool completed: sonarqube_get_component_measures" in (
		result.output
	)
	assert "measures found" in result.output


def test_sonar_mcp_json_output_contains_only_tool_response(monkeypatch) -> None:
	class FakeClient:
		project_key = None

		def tool_definitions(self):
			return [{"function": {"name": "sonarqube_search_metrics"}}]

		def resolve_tool_name(self, name):
			return "sonarqube_search_metrics" if name == "search_metrics" else None

		def call_tool_response(self, name, arguments):
			return {
				"content": [{"type": "text", "text": "metrics"}],
				"structuredContent": {"metrics": []},
			}

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (FakeClient(), None),
	)

	result = CliRunner().invoke(
		cli.main,
		[
			"sonar-mcp",
			"--tool",
			"search_metrics",
			"--arguments",
			"{}",
			"--json",
		],
	)

	assert result.exit_code == 0
	assert result.output.startswith("{")
	assert result.output.endswith("}\n")
	assert '"metrics": []' in result.output


def test_sonar_mcp_rejects_non_object_tool_arguments(monkeypatch) -> None:
	class FakeClient:
		project_key = None

		def tool_definitions(self):
			return []

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		lambda **kwargs: (FakeClient(), None),
	)

	result = CliRunner().invoke(
		cli.main,
		["sonar-mcp", "--tool", "anything", "--arguments", "[]"],
	)

	assert result.exit_code == 2
	assert "--arguments must contain a JSON object" in result.output


def test_sonar_mcp_accepts_external_project_options(monkeypatch, tmp_path) -> None:
	captured = {}
	ca_dir = tmp_path / "certs"
	ca_dir.mkdir()
	(ca_dir / "corporate.pem").write_text("certificate", encoding="ascii")

	class FakeClient:
		project_key = "Operations.HMI.App.Alarms"

		def tool_definitions(self):
			return [
				{
					"function": {
						"name": "sonarqube_search_sonar_issues_in_projects"
					}
				}
			]

		def call_tool_data(self, name, arguments):
			return {"issues": [], "paging": {"total": 0}}

	def connect(**kwargs):
		captured.update(kwargs)
		return FakeClient(), None

	monkeypatch.setattr(
		cli.sonarqube_mcp,
		"connection_from_environment",
		connect,
	)

	result = CliRunner().invoke(
		cli.main,
		[
			"sonar-mcp",
			"--project-path",
			str(tmp_path),
			"--project-key",
			"Operations.HMI.App.Alarms",
			"--ca-dir",
			str(ca_dir),
			"--url",
			"https://codescan.abb.com/",
		],
	)

	assert result.exit_code == 0
	assert captured["project_path"] == Path(tmp_path)
	assert captured["project_key"] == "Operations.HMI.App.Alarms"
	assert captured["ca_dir"] == Path(ca_dir)
	assert captured["server_url"] == "https://codescan.abb.com/"
	assert "no open security violations found" in result.output
