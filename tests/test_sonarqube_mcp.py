"""Tests for the optional SonarQube MCP adapter."""

from __future__ import annotations

import json

import pytest

from quack.mcp import sonarqube


def test_server_config_matches_generated_contract() -> None:
	config = sonarqube.server_config()

	assert config["command"] == "podman"
	assert config["args"][-1] == "mcp/sonarqube"
	assert config["env"] == {
		"SONARQUBE_URL": "https://codescan.abb.com",
		"SONARQUBE_TOKEN": "${SQ_TOKEN}",
		"SONARQUBE_IDE_PORT": "64120",
		"SONARQUBE_MCP_IN_CONTAINER": "true",
		"SONARQUBE_READ_ONLY": "true",
	}


def test_environment_config_prefers_sonar_token_without_repr_leak(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.delenv("SONARQUBE_TOKEN", raising=False)

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.token == "secret-token"
	assert "secret-token" not in repr(config)
	assert config.child_environment()["SONARQUBE_TOKEN"] == "secret-token"
	assert config.podman_command()[-1] == "mcp/sonarqube"


def test_environment_config_discovers_and_mounts_project_workspace(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setattr(
		sonarqube,
		"_default_ca_dir",
		lambda: tmp_path / "missing-certs",
	)
	settings_dir = tmp_path / ".vscode"
	settings_dir.mkdir()
	(settings_dir / "settings.json").write_text(
		json.dumps(
			{
				"sonarlint.connectedMode.project": {
					"projectKey": "Operations.HMI.App.Alarms"
				}
			}
		),
		encoding="utf-8",
	)

	config = sonarqube.SonarQubeMcpConfig.from_environment(
		project_path=tmp_path,
	)

	assert config is not None
	assert config.project_path == tmp_path.resolve()
	assert config.project_key == "Operations.HMI.App.Alarms"
	command = config.podman_command()
	assert "--init" in command
	assert f"{tmp_path.resolve()}:/app/mcp-workspace:ro" in command
	assert config.child_environment()["SONARQUBE_PROJECT_KEY"] == (
		"Operations.HMI.App.Alarms"
	)


def test_environment_config_auto_mounts_default_ca_directory(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	ca_dir = tmp_path / "certs"
	ca_dir.mkdir()
	(ca_dir / "corporate.pem").write_text("certificate", encoding="ascii")
	monkeypatch.setattr(sonarqube, "_default_ca_dir", lambda: ca_dir)

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.ca_dir == ca_dir.resolve()
	assert f"{ca_dir.resolve()}:/usr/local/share/ca-certificates:ro" in (
		config.podman_command()
	)


def test_environment_config_passes_arbitrary_toolsets(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("SONARQUBE_TOOLSETS", "issues,quality-gates,issues")

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.toolsets == ("issues", "quality-gates")
	assert config.child_environment()["SONARQUBE_TOOLSETS"] == (
		"issues,quality-gates"
	)
	command = config.podman_command()
	assert command.count("SONARQUBE_TOOLSETS") == 1


def test_client_translates_tools_and_calls_original_name(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	client = sonarqube.SonarQubeMcpClient(config)
	calls: list[tuple[str, dict]] = []

	def fake_exchange(method, params, *, request_id):
		calls.append((method, params))
		if method == "tools/list":
			return {
				"tools": [
					{
						"name": "issues.search",
						"description": "Search issues",
						"inputSchema": {
							"type": "object",
							"properties": {"projectKey": {"type": "string"}},
						},
					}
				]
			}
		return {
			"content": [{"type": "text", "text": "one issue"}],
		}

	monkeypatch.setattr(client, "_exchange", fake_exchange)

	tools = client.tool_definitions()
	tool_name = tools[0]["function"]["name"]

	assert tool_name.startswith("sonarqube_issues_search")
	assert client.call_tool(tool_name, {"projectKey": "quack-local"}) == "one issue"
	assert calls[-1] == (
		"tools/call",
		{
			"name": "issues.search",
			"arguments": {"projectKey": "quack-local"},
		},
	)
	assert client.resolve_tool_name("issues.search") == tool_name


def test_client_uses_initialize_and_request_protocol(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	captured: dict = {}

	def fake_run(command, input_data, env, timeout_s):
		captured["command"] = command
		captured["input"] = input_data
		captured["env"] = env
		captured["timeout"] = timeout_s
		return (
			0,
			"\n".join(
				[
					json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
					json.dumps(
						{
							"jsonrpc": "2.0",
							"id": 2,
							"result": {"tools": []},
						}
					),
				]
			),
		)

	monkeypatch.setattr(sonarqube.runio, "run_sonarqube_mcp", fake_run)

	assert sonarqube.SonarQubeMcpClient(config).tool_definitions() == []
	payloads = [json.loads(line) for line in captured["input"].splitlines()]
	assert [payload["method"] for payload in payloads] == [
		"initialize",
		"notifications/initialized",
		"tools/list",
	]
	assert captured["env"]["SONARQUBE_TOKEN"] == "secret-token"
	assert "secret-token" not in captured["command"]


def test_client_surfaces_server_diagnostic_instead_of_timeout(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	client = sonarqube.SonarQubeMcpClient(config)
	monkeypatch.setattr(
		sonarqube.runio,
		"run_sonarqube_mcp",
		lambda *args, **kwargs: (
			-1,
			(
				'Exception in thread "main" '
				"org.sonarsource.UnauthorizedException: "
				"SonarQube answered with Not authorized. "
				"Please check server credentials.\n"
				"\tat server.Main.run(Main.java:1)\n"
			),
		),
	)

	with pytest.raises(sonarqube.SonarQubeMcpUnavailable) as excinfo:
		client._exchange("tools/list", {}, request_id=2)

	assert excinfo.value.reason == (
		"SonarQube MCP server failed: SonarQube answered with Not authorized. "
		"Please check server credentials."
	)


def test_read_only_annotation_filters_write_tool() -> None:
	config = sonarqube.SonarQubeMcpConfig(token="token")
	client = sonarqube.SonarQubeMcpClient(config)
	client._tools = [
		{
			"name": "safe",
			"annotations": {"readOnlyHint": True},
		},
		{
			"name": "write",
			"annotations": {"readOnlyHint": False},
		},
	]

	tools = client.tool_definitions()

	assert [tool["function"]["name"] for tool in tools] == ["sonarqube_safe"]


def test_disabled_or_missing_token_does_not_create_config(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("QUACK_SONAR_MCP", "off")
	monkeypatch.setenv("SQ_TOKEN", "token")
	assert sonarqube.SonarQubeMcpConfig.from_environment() is None

	monkeypatch.setenv("QUACK_SONAR_MCP", "on")
	monkeypatch.delenv("SQ_TOKEN", raising=False)
	monkeypatch.delenv("SONARQUBE_TOKEN", raising=False)
	assert sonarqube.SonarQubeMcpConfig.from_environment() is None


def test_connection_reports_missing_token(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("QUACK_SONAR_MCP", "on")
	monkeypatch.delenv("SQ_TOKEN", raising=False)
	monkeypatch.delenv("SONARQUBE_TOKEN", raising=False)

	client, reason = sonarqube.connection_from_environment()

	assert client is None
	assert reason == "set SQ_TOKEN or SONARQUBE_TOKEN"
