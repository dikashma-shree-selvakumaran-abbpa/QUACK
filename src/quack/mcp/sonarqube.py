"""SonarQube MCP client used by Quack's optional advisory integrations.

The local staged SonarQube path remains in :mod:`quack.sonar` and uses the
scanner. This module is a separate, dependency-free MCP client for the Podman
stdio server described by ``.vscode/mcp.json``. It discovers the server's
tools and translates them to the OpenAI tool shape used by the agent and the
shared watch/pre-commit report without ever placing the token in a command
argument or log message.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from .. import runio

DEFAULT_URL = "https://codescan.abb.com"
DEFAULT_IDE_PORT = 64120
DEFAULT_IMAGE = "mcp/sonarqube"
CONTAINER_RUNTIME = "podman"
CONTAINER_WORKSPACE = "/app/mcp-workspace"
CONTAINER_CERTIFICATES = "/usr/local/share/ca-certificates"
DEFAULT_TIMEOUT_S = 90.0
MAX_TIMEOUT_S = 180.0
MAX_TOOL_OUTPUT = 12000
MCP_PROTOCOL_VERSION = "2024-11-05"

_DISABLED_VALUES = {"0", "false", "no", "off"}
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$")
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def _new_container_name() -> str:
	"""Return a unique name used to reap a one-shot MCP container."""
	return f"quack-sonarqube-mcp-{uuid.uuid4().hex[:12]}"


class SonarQubeMcpUnavailable(Exception):
	"""Raised when the optional SonarQube MCP server cannot answer."""

	def __init__(self, reason: str) -> None:
		self.reason = _short_reason(reason)
		super().__init__(self.reason)


@dataclass(frozen=True)
class SonarQubeMcpConfig:
	"""Validated configuration for one SonarQube MCP server invocation."""

	url: str = DEFAULT_URL
	token: str = field(default="", repr=False)
	ide_port: int = DEFAULT_IDE_PORT
	project_path: Path | None = None
	project_key: str | None = None
	toolsets: tuple[str, ...] = ()
	ca_dir: Path | None = None
	image: str = DEFAULT_IMAGE
	timeout_s: float = DEFAULT_TIMEOUT_S
	container_name: str = field(default_factory=_new_container_name, repr=False)

	@classmethod
	def from_environment(
		cls,
		*,
		project_path: str | Path | None = None,
		project_key: str | None = None,
		toolsets: Sequence[str] | str | None = None,
		ca_dir: str | Path | None = None,
		server_url: str | None = None,
		base_path: str | Path | None = None,
	) -> SonarQubeMcpConfig | None:
		"""Build configuration from the generated MCP environment contract."""
		if not enabled():
			return None
		token = _token_from_environment()
		if not token:
			return None
		url = _normalise_url(
			server_url or os.environ.get("SONARQUBE_URL", DEFAULT_URL)
		)
		if url is None:
			raise SonarQubeMcpUnavailable("invalid SonarQube URL")
		raw_project_path = (
			project_path
			if project_path is not None
			else os.environ.get("QUACK_SONAR_MCP_PROJECT_PATH")
			or os.environ.get("SONARQUBE_PROJECT_PATH")
		)
		resolved_project_path = _resolve_project_path(raw_project_path, base_path)
		raw_ca_dir = _configured_ca_dir(ca_dir)
		resolved_ca_dir = _resolve_ca_dir(raw_ca_dir, base_path)
		raw_project_key = (
			project_key
			or os.environ.get("SONARQUBE_PROJECT_KEY")
			or _discover_project_key(resolved_project_path)
		)
		if raw_project_key and not _PROJECT_KEY_RE.fullmatch(raw_project_key.strip()):
			raise SonarQubeMcpUnavailable("invalid SonarQube project key")
		resolved_toolsets = _normalise_toolsets(
			toolsets
			if toolsets is not None
			else os.environ.get("SONARQUBE_TOOLSETS")
		)
		image = os.environ.get("QUACK_SONAR_MCP_IMAGE", DEFAULT_IMAGE).strip()
		if not _IMAGE_RE.fullmatch(image) or image.startswith("-"):
			raise SonarQubeMcpUnavailable("invalid SonarQube MCP image")
		return cls(
			url=url,
			token=token,
			ide_port=_ide_port(),
			project_path=resolved_project_path,
			project_key=raw_project_key.strip() if raw_project_key else None,
			toolsets=resolved_toolsets,
			ca_dir=resolved_ca_dir,
			image=image,
			timeout_s=_timeout_seconds(),
		)

	def podman_command(self) -> list[str]:
		"""Return the fixed Podman stdio command from the supplied JSON."""
		command = [
			CONTAINER_RUNTIME,
			"run",
			"--init",
			"-i",
			"--rm",
			"--name",
			self.container_name,
			"-e",
			"SONARQUBE_TOKEN",
			"-e",
			"SONARQUBE_URL",
			"-e",
			"SONARQUBE_IDE_PORT",
			"-e",
			"SONARQUBE_MCP_IN_CONTAINER",
			"-e",
			"SONARQUBE_READ_ONLY",
		]
		if self.project_key:
			command += ["-e", "SONARQUBE_PROJECT_KEY"]
		if self.toolsets:
			command += ["-e", "SONARQUBE_TOOLSETS"]
		if self.project_path:
			command += [
				"-v",
				f"{self.project_path}:{CONTAINER_WORKSPACE}:ro",
			]
		if self.ca_dir:
			command += [
				"-v",
				f"{self.ca_dir}:{CONTAINER_CERTIFICATES}:ro",
			]
		command.append(self.image)
		return command

	def child_environment(self) -> dict[str, str]:
		"""Return the child environment without exposing credentials in argv."""
		environment = os.environ.copy()
		environment["SONARQUBE_TOKEN"] = self.token
		environment["SONARQUBE_URL"] = self.url
		environment["SONARQUBE_IDE_PORT"] = str(self.ide_port)
		environment["SONARQUBE_MCP_IN_CONTAINER"] = "true"
		environment["SONARQUBE_READ_ONLY"] = "true"
		if self.project_key:
			environment["SONARQUBE_PROJECT_KEY"] = self.project_key
		if self.toolsets:
			environment["SONARQUBE_TOOLSETS"] = ",".join(self.toolsets)
		return environment


class SonarQubeMcpClient:
	"""Small one-shot MCP stdio client for SonarQube tool discovery and calls."""

	def __init__(self, config: SonarQubeMcpConfig) -> None:
		self._config = config
		self._tools: list[dict[str, Any]] | None = None
		self._name_map: dict[str, str] = {}

	def tool_definitions(self) -> list[dict[str, Any]]:
		"""Return discovered SonarQube tools in the agent's tool format."""
		if self._tools is None:
			self._tools = self._list_tools()
		definitions: list[dict[str, Any]] = []
		used_names: set[str] = set()
		for tool in self._tools:
			if not _is_read_only(tool):
				continue
			name = tool.get("name")
			if not isinstance(name, str) or not name:
				continue
			openai_name = _openai_tool_name(name, used_names)
			used_names.add(openai_name)
			self._name_map[openai_name] = name
			description = tool.get("description")
			if not isinstance(description, str) or not description.strip():
				description = f"Call the SonarQube MCP tool {name}."
			schema = tool.get("inputSchema")
			if not isinstance(schema, dict):
				schema = {"type": "object", "properties": {}}
			definitions.append(
				{
					"type": "function",
					"function": {
						"name": openai_name,
						"description": description[:1000],
						"parameters": schema,
					},
				}
			)
		return definitions

	def has_tool(self, name: str) -> bool:
		"""Return whether ``name`` is one of this client's advertised tools."""
		return self.resolve_tool_name(name) is not None

	def resolve_tool_name(self, name: str) -> str | None:
		"""Resolve an OpenAI-prefixed or native MCP name to the advertised name."""
		if not isinstance(name, str) or not name.strip():
			return None
		self.tool_definitions()
		candidate = name.strip()
		if candidate in self._name_map:
			return candidate
		for openai_name, original_name in self._name_map.items():
			if original_name == candidate:
				return openai_name
		return None

	@property
	def project_key(self) -> str | None:
		"""Return the configured project scope without exposing credentials."""
		return self._config.project_key

	def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
		"""Call an advertised tool and return bounded model-readable content."""
		result = self._call_tool_result(name, arguments)
		return _format_tool_result(result)

	def call_tool_response(
		self, name: str, arguments: dict[str, Any]
	) -> dict[str, Any]:
		"""Call an advertised tool and return its complete MCP response."""
		return self._call_tool_result(name, arguments)

	def call_tool_data(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
		"""Call a tool and return its structured JSON result."""
		result = self._call_tool_result(name, arguments)
		if result.get("isError"):
			reason = _format_tool_result(result)
			raise SonarQubeMcpUnavailable(
				reason.removeprefix("error: ").strip() or "SonarQube MCP tool failed"
			)
		structured = result.get("structuredContent")
		if isinstance(structured, dict):
			return structured
		content = result.get("content")
		if isinstance(content, list):
			for item in content:
				if not isinstance(item, dict):
					continue
				text = item.get("text")
				if not isinstance(text, str):
					continue
				try:
					value = json.loads(text)
				except (TypeError, ValueError):
					continue
				if isinstance(value, dict):
					return value
		raise SonarQubeMcpUnavailable("SonarQube MCP returned an unreadable result")

	def _call_tool_result(
		self, name: str, arguments: dict[str, Any]
	) -> dict[str, Any]:
		openai_name = self.resolve_tool_name(name)
		if openai_name is None:
			raise SonarQubeMcpUnavailable("unknown SonarQube MCP tool")
		original_name = self._name_map[openai_name]
		if not isinstance(arguments, dict):
			raise SonarQubeMcpUnavailable("invalid SonarQube MCP arguments")
		return self._exchange(
			"tools/call",
			{"name": original_name, "arguments": arguments},
			request_id=2,
		)

	def _list_tools(self) -> list[dict[str, Any]]:
		"""Discover all pages of tools while bounding server-controlled data."""
		tools: list[dict[str, Any]] = []
		cursor: str | None = None
		for _ in range(8):
			params: dict[str, Any] = {}
			if cursor:
				params["cursor"] = cursor
			result = self._exchange("tools/list", params, request_id=2)
			page = result.get("tools")
			if isinstance(page, list):
				tools.extend(
					item
					for item in page[:100]
					if isinstance(item, dict)
					and isinstance(item.get("name"), str)
					and _is_read_only(item)
				)
			next_cursor = result.get("nextCursor")
			if not isinstance(next_cursor, str) or not next_cursor:
				break
			cursor = next_cursor
		return tools[:200]

	def _exchange(
		self,
		method: str,
		params: dict[str, Any],
		*,
		request_id: int,
	) -> dict[str, Any]:
		"""Run one initialize/request exchange over the Podman stdio server."""
		payloads = [
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "initialize",
				"params": {
					"protocolVersion": MCP_PROTOCOL_VERSION,
					"capabilities": {},
					"clientInfo": {"name": "quack", "version": "0.3.0"},
				},
			},
			{
				"jsonrpc": "2.0",
				"method": "notifications/initialized",
				"params": {},
			},
			{
				"jsonrpc": "2.0",
				"id": request_id,
				"method": method,
				"params": params,
			},
		]
		input_data = "\n".join(
			json.dumps(payload, separators=(",", ":")) for payload in payloads
		) + "\n"
		exit_code, output = runio.run_sonarqube_mcp(
			self._config.podman_command(),
			input_data,
			self._config.child_environment(),
			timeout_s=self._config.timeout_s,
		)
		if exit_code != 0:
			if exit_code == -1:
				diagnostic = _server_diagnostic(output, self._config.token)
				if diagnostic:
					raise SonarQubeMcpUnavailable(
						f"SonarQube MCP server failed: {diagnostic}"
					)
				raise SonarQubeMcpUnavailable("SonarQube MCP server timed out")
			if exit_code == 127:
				raise SonarQubeMcpUnavailable("podman not found on PATH")
			if exit_code == 126:
				raise SonarQubeMcpUnavailable(
					"Podman could not start the SonarQube MCP container"
				)
			detail = _safe_reason(output, self._config.token)
			if detail == "unavailable":
				raise SonarQubeMcpUnavailable(
					f"SonarQube MCP server exited with code {exit_code}"
				)
			raise SonarQubeMcpUnavailable(
				f"SonarQube MCP server exited: {detail}"
			)

		responses = _json_responses(output)
		_response_result(responses, 1, self._config.token)
		return _response_result(responses, request_id, self._config.token)


def enabled() -> bool:
	"""Return whether the optional MCP integration is not explicitly disabled."""
	value = os.environ.get("QUACK_SONAR_MCP", "auto").strip().lower()
	return value not in _DISABLED_VALUES


def client_from_environment(
	*,
	project_path: str | Path | None = None,
	project_key: str | None = None,
	toolsets: Sequence[str] | str | None = None,
	ca_dir: str | Path | None = None,
	server_url: str | None = None,
	base_path: str | Path | None = None,
) -> SonarQubeMcpClient | None:
	"""Create a client when the provider, Podman, and token are available."""
	client, _reason = connection_from_environment(
		require_provider=True,
		project_path=project_path,
		project_key=project_key,
		toolsets=toolsets,
		ca_dir=ca_dir,
		server_url=server_url,
		base_path=base_path,
	)
	return client


def connection_from_environment(
	*,
	require_provider: bool = False,
	project_path: str | Path | None = None,
	project_key: str | None = None,
	toolsets: Sequence[str] | str | None = None,
	ca_dir: str | Path | None = None,
	server_url: str | None = None,
	base_path: str | Path | None = None,
) -> tuple[SonarQubeMcpClient | None, str | None]:
	"""Return a client and an actionable reason when setup is incomplete."""
	if not enabled():
		return None, "disabled by QUACK_SONAR_MCP"
	if require_provider and os.environ.get(
		"QUACK_PROVIDER", "copilot_sdk"
	).strip().lower() != "github_models":
		return None, "requires QUACK_PROVIDER=github_models"
	if not _token_from_environment():
		return None, "set SQ_TOKEN or SONARQUBE_TOKEN"
	if shutil.which(CONTAINER_RUNTIME) is None:
		return None, "podman not found on PATH"
	try:
		config = SonarQubeMcpConfig.from_environment(
			project_path=project_path,
			project_key=project_key,
			toolsets=toolsets,
			ca_dir=ca_dir,
			server_url=server_url,
			base_path=base_path,
		)
	except SonarQubeMcpUnavailable as exc:
		return None, exc.reason
	if config is None:
		return None, "SonarQube MCP configuration unavailable"
	return SonarQubeMcpClient(config), None


def server_config() -> dict[str, Any]:
	"""Return a token-free client configuration matching the supplied JSON."""
	return {
		"command": CONTAINER_RUNTIME,
		"args": [
			"run",
			"--init",
			"-i",
			"--rm",
			"-e",
			"SONARQUBE_TOKEN",
			"-e",
			"SONARQUBE_URL",
			"-e",
			"SONARQUBE_IDE_PORT",
			"-e",
			"SONARQUBE_MCP_IN_CONTAINER",
			"-e",
			"SONARQUBE_READ_ONLY",
			DEFAULT_IMAGE,
		],
		"env": {
			"SONARQUBE_URL": DEFAULT_URL,
			"SONARQUBE_TOKEN": "${SQ_TOKEN}",
			"SONARQUBE_IDE_PORT": str(DEFAULT_IDE_PORT),
			"SONARQUBE_MCP_IN_CONTAINER": "true",
			"SONARQUBE_READ_ONLY": "true",
		},
	}


def _token_from_environment() -> str:
	return (
		os.environ.get("SONARQUBE_TOKEN", "").strip()
		or os.environ.get("SQ_TOKEN", "").strip()
		or os.environ.get("SONAR_TOKEN", "").strip()
	)


def _normalise_toolsets(
	raw_toolsets: Sequence[str] | str | None,
) -> tuple[str, ...]:
	"""Validate and de-duplicate arbitrary SonarQube toolset keys."""
	if raw_toolsets is None:
		return ()
	values = (
		[raw_toolsets]
		if isinstance(raw_toolsets, str)
		else list(raw_toolsets)
	)
	toolsets: list[str] = []
	for value in values:
		if not isinstance(value, str):
			raise SonarQubeMcpUnavailable("invalid SonarQube MCP toolset")
		for item in value.split(","):
			item = item.strip()
			if not item:
				continue
			if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", item):
				raise SonarQubeMcpUnavailable("invalid SonarQube MCP toolset")
			if item not in toolsets:
				toolsets.append(item)
	return tuple(toolsets)


def _resolve_project_path(
	raw_path: str | Path | None,
	base_path: str | Path | None,
) -> Path | None:
	"""Resolve an optional local workspace directory for MCP file tools."""
	if raw_path is None:
		return None
	try:
		path = Path(raw_path).expanduser()
		if not path.is_absolute():
			path = Path(base_path or Path.cwd()) / path
		path = path.resolve()
	except (OSError, RuntimeError, TypeError, ValueError):
		raise SonarQubeMcpUnavailable("invalid SonarQube project path")
	if not path.is_dir():
		raise SonarQubeMcpUnavailable("SonarQube project path is not a directory")
	return path


def _configured_ca_dir(
	explicit_path: str | Path | None,
) -> str | Path | None:
	"""Use an explicit or configured CA directory, then the Windows default."""
	if explicit_path is not None:
		return explicit_path
	configured = os.environ.get("QUACK_SONAR_MCP_CA_DIR")
	if configured is not None:
		return configured or None
	default_path = _default_ca_dir()
	return default_path if default_path.is_dir() else None


def _default_ca_dir() -> Path:
	"""Return the per-user CA directory used by the Windows installation."""
	if os.name == "nt":
		local_app_data = os.environ.get("LOCALAPPDATA")
		base = (
			Path(local_app_data)
			if local_app_data
			else Path.home() / "AppData" / "Local"
		)
	else:
		base = Path(
			os.environ.get(
				"XDG_DATA_HOME",
				str(Path.home() / ".local" / "share"),
			)
		)
	return base / "quack" / "sonarqube-mcp" / "certs"


def _resolve_ca_dir(
	raw_path: str | Path | None,
	base_path: str | Path | None,
) -> Path | None:
	"""Resolve a CA directory containing certificates supported by the image."""
	if raw_path is None:
		return None
	try:
		path = Path(raw_path).expanduser()
		if not path.is_absolute():
			path = Path(base_path or Path.cwd()) / path
		path = path.resolve()
		entries = list(path.iterdir())
	except (OSError, RuntimeError, TypeError, ValueError):
		raise SonarQubeMcpUnavailable("invalid SonarQube MCP CA directory")
	if not path.is_dir():
		raise SonarQubeMcpUnavailable("SonarQube MCP CA directory is not a directory")
	if not any(
		entry.is_file() and entry.suffix.lower() in {".crt", ".pem"}
		for entry in entries
	):
		raise SonarQubeMcpUnavailable(
			"SonarQube MCP CA directory has no .crt or .pem certificates"
		)
	return path


def _discover_project_key(project_path: Path | None) -> str | None:
	"""Read the connected-mode project key without scanning arbitrary files."""
	if project_path is None:
		return None
	settings_path = project_path / ".vscode" / "settings.json"
	try:
		settings = json.loads(settings_path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, ValueError):
		settings = None
	if isinstance(settings, dict):
		connected = settings.get("sonarlint.connectedMode.project")
		if isinstance(connected, dict):
			value = connected.get("projectKey")
			if isinstance(value, str) and _PROJECT_KEY_RE.fullmatch(value.strip()):
				return value.strip()

	properties_path = project_path / "sonar-project.properties"
	try:
		lines = properties_path.read_text(encoding="utf-8").splitlines()
	except (OSError, UnicodeError):
		lines = []
	for line in lines:
		key, separator, value = line.partition("=")
		if separator and key.strip() == "sonar.projectKey":
			value = value.strip()
			if _PROJECT_KEY_RE.fullmatch(value):
				return value
	return None


def _normalise_url(raw: str) -> str | None:
	try:
		parts = urlsplit(raw.strip())
		_ = parts.port
	except (AttributeError, ValueError):
		return None
	if (
		parts.scheme not in {"http", "https"}
		or not parts.netloc
		or parts.username
		or parts.password
		or not parts.hostname
	):
		return None
	return raw.strip().rstrip("/")


def _ide_port() -> int:
	raw = os.environ.get("SONARQUBE_IDE_PORT", str(DEFAULT_IDE_PORT)).strip()
	try:
		value = int(raw)
	except ValueError:
		return DEFAULT_IDE_PORT
	return value if 1 <= value <= 65535 else DEFAULT_IDE_PORT


def _timeout_seconds() -> float:
	raw = os.environ.get("QUACK_SONAR_MCP_TIMEOUT_S", "").strip()
	try:
		value = float(raw) if raw else DEFAULT_TIMEOUT_S
	except ValueError:
		value = DEFAULT_TIMEOUT_S
	if not math.isfinite(value):
		value = DEFAULT_TIMEOUT_S
	return max(1.0, min(MAX_TIMEOUT_S, value))


def _json_responses(output: str) -> list[dict[str, Any]]:
	responses: list[dict[str, Any]] = []
	for line in output.splitlines():
		try:
			value = json.loads(line)
		except (TypeError, ValueError):
			continue
		if isinstance(value, dict) and ("id" in value or "error" in value):
			responses.append(value)
	return responses


def _response_result(
	responses: list[dict[str, Any]], request_id: int, token: str
) -> dict[str, Any]:
	for response in responses:
		if response.get("id") != request_id:
			continue
		error = response.get("error")
		if isinstance(error, dict):
			message = error.get("message")
			raise SonarQubeMcpUnavailable(_safe_reason(message, token))
		result = response.get("result")
		if isinstance(result, dict):
			return result
		raise SonarQubeMcpUnavailable("malformed SonarQube MCP response")
	raise SonarQubeMcpUnavailable("SonarQube MCP response not received")


def _format_tool_result(result: dict[str, Any]) -> str:
	content = result.get("content")
	parts: list[str] = []
	if isinstance(content, list):
		for item in content[:100]:
			if not isinstance(item, dict):
				continue
			text = item.get("text")
			if isinstance(text, str):
				parts.append(text)
			elif item.get("type") == "resource":
				resource = item.get("resource")
				if isinstance(resource, dict) and isinstance(
					resource.get("text"), str
				):
					parts.append(resource["text"])
	if not parts:
		try:
			parts.append(json.dumps(result, sort_keys=True))
		except (TypeError, ValueError):
			parts.append("SonarQube MCP returned an unreadable result")
	text = "\n".join(parts).strip()
	if result.get("isError"):
		text = f"error: {text}"
	if len(text) > MAX_TOOL_OUTPUT:
		return text[: MAX_TOOL_OUTPUT - 20].rstrip() + "\n...[truncated]"
	return text


def format_tool_result(result: dict[str, Any]) -> str:
	"""Return bounded display text for a complete MCP tool response."""
	return _format_tool_result(result)


def _is_read_only(tool: dict[str, Any]) -> bool:
	annotations = tool.get("annotations")
	if isinstance(annotations, dict) and annotations.get("readOnlyHint") is False:
		return False
	return True


def _openai_tool_name(name: str, used_names: set[str]) -> str:
	base = _TOOL_NAME_RE.sub("_", name).strip("_") or "tool"
	candidate = f"sonarqube_{base}"[:64]
	if candidate not in used_names:
		return candidate
	digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
	return f"{candidate[:55]}_{digest}"


def _safe_reason(value: object, token: str) -> str:
	reason = str(value) if value is not None else "server error"
	if token:
		reason = reason.replace(token, "<redacted>")
	return _short_reason(reason)


def _server_diagnostic(output: str, token: str) -> str | None:
	"""Extract a useful one-line failure from server startup diagnostics."""
	for line in reversed(output.splitlines()):
		line = " ".join(line.split())
		if not line:
			continue
		match = re.search(
			r"(?:[A-Za-z_][A-Za-z0-9_.$]*)(?:Exception|Error):\s*(.+)$",
			line,
		)
		if match:
			return _safe_reason(match.group(1), token)
		lower_line = line.lower()
		if "not authorized" in lower_line or "unauthorized" in lower_line:
			return _safe_reason(line, token)
	return None


def _short_reason(value: str) -> str:
	reason = " ".join(value.split())
	if len(reason) > 160:
		return reason[:157].rstrip() + "..."
	return reason or "unavailable"
