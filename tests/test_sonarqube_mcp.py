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
	assert config.ide_port is None
	assert "secret-token" not in repr(config)
	assert config.child_environment()["SONARQUBE_TOKEN"] == "secret-token"
	assert "SONARQUBE_IDE_PORT" not in config.child_environment()
	assert "SONARQUBE_IDE_PORT" not in config.podman_command()
	assert config.podman_command()[-1] == "mcp/sonarqube"


def test_explicit_ide_port_is_forwarded(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("SONARQUBE_IDE_PORT", "64120")

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.ide_port == 64120
	assert config.child_environment()["SONARQUBE_IDE_PORT"] == "64120"
	assert config.podman_command().count("SONARQUBE_IDE_PORT") == 1


def test_environment_config_prefers_explicit_mcp_url(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("SONARQUBE_URL", "https://scanner.example")
	monkeypatch.setenv("QUACK_SONAR_MCP_URL", "https://codescan.abb.com")

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.url == "https://codescan.abb.com"


def test_environment_config_prefers_quack_branch_override(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("SONARQUBE_BRANCH", "quack-sonar-clean")
	monkeypatch.setenv("QUACK_SONAR_BRANCH", "quack-sonar-violation")

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert config.branch == "quack-sonar-violation"


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


def test_environment_config_forwards_configured_proxy_to_container(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

	config = sonarqube.SonarQubeMcpConfig.from_environment()

	assert config is not None
	assert "HTTPS_PROXY" in config.podman_command()
	assert config.child_environment()["HTTPS_PROXY"] == "http://proxy.example:8080"


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
						"annotations": {"readOnlyHint": True},
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


def test_client_parses_structured_sonar_issue_response(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(
		token="secret-token",
		project_key="Operations.HMI.App.Alarms",
		branch="quack-sonar-violation",
	)
	client = sonarqube.SonarQubeMcpClient(config)
	requests: list[dict] = []
	issue_data = {
		"issues": [
			{
				"key": "ISSUE-1",
				"rule": "typescript:S innerHTML",
				"severity": "CRITICAL",
				"status": "OPEN",
				"component": (
					"Operations.HMI.App.Alarms:"
					"FrontEnd/packages/alarms/src/quack-sonar-violation.ts"
				),
				"textRange": {"startLine": 2, "endLine": 2},
			}
		],
		"paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
	}

	def fake_run(command, input_data, env, timeout_s):
		payloads = [json.loads(line) for line in input_data.splitlines()]
		request = payloads[-1]
		requests.append(request)
		if request["method"] == "tools/list":
			result = {
				"tools": [
					{
						"name": "search_sonar_issues_in_projects",
						"annotations": {"readOnlyHint": True},
						"inputSchema": {
							"type": "object",
							"properties": {
								"projects": {"type": "array"},
								"branch": {"type": "string"},
								"issueStatuses": {"type": "array"},
							},
						},
					}
				]
			}
		else:
			assert request["method"] == "tools/call"
			assert request["params"] == {
				"name": "search_sonar_issues_in_projects",
				"arguments": {
					"projects": ["Operations.HMI.App.Alarms"],
					"branch": "quack-sonar-violation",
					"issueStatuses": ["OPEN"],
				},
			}
			result = {
				"content": [
					{"type": "text", "text": json.dumps(issue_data)}
				],
				"isError": False,
				"structuredContent": issue_data,
			}
		return (
			0,
			"\n".join(
				[
					json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
					json.dumps(
						{"jsonrpc": "2.0", "id": 2, "result": result}
					),
				]
			),
		)

	monkeypatch.setattr(sonarqube.runio, "run_sonarqube_mcp", fake_run)

	data = client.call_tool_data(
		"search_sonar_issues_in_projects",
		{
			"projects": ["Operations.HMI.App.Alarms"],
			"branch": "quack-sonar-violation",
			"issueStatuses": ["OPEN"],
		},
	)

	assert data == issue_data
	assert requests[0]["method"] == "tools/list"
	assert requests[-1]["params"]["name"] == (
		"search_sonar_issues_in_projects"
	)
	assert requests[-1]["params"]["arguments"]["branch"] == (
		"quack-sonar-violation"
	)


def test_zero_process_exit_does_not_hide_mcp_error(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	client = sonarqube.SonarQubeMcpClient(config)

	def fake_run(command, input_data, env, timeout_s):
		payloads = [json.loads(line) for line in input_data.splitlines()]
		result = (
			{"tools": [{"name": "search_metrics"}]}
			if payloads[-1]["method"] == "tools/list"
			else {
				"content": [{"type": "text", "text": "Sonar rejected request"}],
				"isError": True,
				"structuredContent": {"issues": []},
			}
		)
		return (
			0,
			"\n".join(
				[
					json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
					json.dumps(
						{"jsonrpc": "2.0", "id": 2, "result": result}
					),
				]
			),
		)

	monkeypatch.setattr(sonarqube.runio, "run_sonarqube_mcp", fake_run)

	with pytest.raises(sonarqube.SonarQubeMcpUnavailable) as excinfo:
		client.call_tool_data("search_metrics", {})

	assert excinfo.value.reason == "Sonar rejected request"


def test_malformed_json_rpc_result_is_not_treated_as_sonar_data(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	client = sonarqube.SonarQubeMcpClient(config)

	def fake_run(command, input_data, env, timeout_s):
		payloads = [json.loads(line) for line in input_data.splitlines()]
		result = (
			{"tools": [{"name": "search_metrics"}]}
			if payloads[-1]["method"] == "tools/list"
			else {"structuredContent": []}
		)
		return (
			0,
			"\n".join(
				[
					json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
					json.dumps(
						{"jsonrpc": "2.0", "id": 2, "result": result}
					),
				]
			),
		)

	monkeypatch.setattr(sonarqube.runio, "run_sonarqube_mcp", fake_run)

	with pytest.raises(sonarqube.SonarQubeMcpUnavailable) as excinfo:
		client.call_tool_data("search_metrics", {})

	assert excinfo.value.reason == "SonarQube MCP returned an unreadable result"


@pytest.mark.parametrize(
	("variable", "value"),
	[
		("SONARQUBE_URL", "file://not-sonar"),
		("SONARQUBE_PROJECT_KEY", "bad project key"),
		("SONARQUBE_BRANCH", "branch\ninjection"),
		("SONARQUBE_BRANCH", "x" * 257),
		("SONARQUBE_IDE_PORT", "not-a-port"),
		("QUACK_SONAR_MCP_IMAGE", "mcp/sonarqube image"),
	],
)
def test_environment_input_validation_rejects_unsafe_values(
	monkeypatch: pytest.MonkeyPatch,
	variable: str,
	value: str,
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	for name in (
		"QUACK_SONAR_MCP_URL",
		"SONARQUBE_URL",
		"QUACK_SONAR_PROJECT_KEY",
		"SONARQUBE_PROJECT_KEY",
		"QUACK_SONAR_BRANCH",
		"SONARQUBE_BRANCH",
		"QUACK_SONAR_MCP_IMAGE",
	):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setenv(variable, value)

	with pytest.raises(sonarqube.SonarQubeMcpUnavailable):
		sonarqube.SonarQubeMcpConfig.from_environment()


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


def test_client_surfaces_oversized_response_without_fake_sonar_data(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="secret-token")
	client = sonarqube.SonarQubeMcpClient(config)
	monkeypatch.setattr(
		sonarqube.runio,
		"run_sonarqube_mcp",
		lambda *args, **kwargs: (
			sonarqube.runio.MCP_RESPONSE_TOO_LARGE,
			"<MCP response exceeded the bounded output limit>",
		),
	)

	with pytest.raises(sonarqube.SonarQubeMcpUnavailable) as excinfo:
		client._exchange("tools/list", {}, request_id=2)

	assert excinfo.value.reason == (
		"SonarQube MCP response exceeded the bounded output limit"
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


@pytest.mark.parametrize(
	("tool", "expected"),
	[
		(
			{"name": "unannotated", "annotations": {"readOnlyHint": True}},
			True,
		),
		(
			{"name": "unannotated", "annotations": {"readOnlyHint": False}},
			False,
		),
		({"name": "unannotated"}, False),
		({"name": "search_metrics"}, True),
		({"name": "list_branches"}, True),
		({"name": "get_component_measures", "annotations": {}}, True),
	],
)
def test_read_only_boundary_requires_annotation_or_known_tool(
	tool: dict, expected: bool
) -> None:
	assert sonarqube._is_read_only(tool) is expected


def test_tool_discovery_does_not_expose_write_capable_tools(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	config = sonarqube.SonarQubeMcpConfig(token="token")
	client = sonarqube.SonarQubeMcpClient(config)

	def fake_exchange(method, params, *, request_id):
		assert method == "tools/list"
		return {
			"tools": [
				{"name": "search_sonar_issues_in_projects"},
				{
					"name": "delete_project",
					"annotations": {"readOnlyHint": False},
				},
				{"name": "create_project"},
				{
					"name": "annotated_safe",
					"annotations": {"readOnlyHint": True},
				},
			]
		}

	monkeypatch.setattr(client, "_exchange", fake_exchange)

	tools = client.tool_definitions()
	names = [tool["function"]["name"] for tool in tools]

	assert names == [
		"sonarqube_search_sonar_issues_in_projects",
		"sonarqube_annotated_safe",
	]
	assert client.resolve_tool_name("delete_project") is None
	assert client.resolve_tool_name("create_project") is None


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
