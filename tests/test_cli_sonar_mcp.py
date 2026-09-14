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
