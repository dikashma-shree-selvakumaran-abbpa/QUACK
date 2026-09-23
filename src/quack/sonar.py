"""Optional local SonarQube analysis for the pre-commit path.

The integration is advisory and fail-open. It only runs when a scanner,
``SONAR_TOKEN``, and a healthy SonarQube server are available. Analysis is
performed from a temporary export of the Git index so unstaged edits are never
included accidentally. Scanner output is retained only in memory and reduced
to a bounded, controlled reason before it reaches the terminal or metrics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from . import gitio, runio, sonar_debug

DEFAULT_HOST_URL = "https://codescan.abb.com"
DEFAULT_PROJECT_KEY = "quack-local"
DEFAULT_TIMEOUT_S = runio.SONAR_TIMEOUT_S
HEALTH_TIMEOUT_S = 2.0
MAX_TIMEOUT_S = 300
MIN_TIMEOUT_S = 5

_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
_PROJECT_KEY_CHARS = frozenset(
	"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_PROPERTY_KEY_RE = re.compile(r"^sonar\.[A-Za-z0-9_.-]{1,160}$")
_MAX_PROPERTY_VALUE = 4096
_SENSITIVE_PROPERTIES = frozenset(
	{"sonar.login", "sonar.password", "sonar.token", "sonar.apiKey"}
)


@dataclass(frozen=True)
class ScanResult:
	"""Safe summary of one SonarQube attempt."""

	status: str
	reason: str = ""
	duration_s: float = 0.0
	dashboard_url: str | None = None
	project_key: str | None = None
	branch: str | None = None
	task_id: str | None = None
	analysis_id: str | None = None
	confirmed: bool = True
	uploaded: bool = False


@dataclass(frozen=True)
class ScanConfig:
	"""Validated project settings shared by scanner and MCP callers."""

	host_url: str
	project_key: str
	project_name: str
	branch: str | None
	sources: str
	tests: str
	exclusions: str = ""
	inclusions: str = ""
	test_inclusions: str = ""
	coverage_exclusions: str = ""
	source_encoding: str = "UTF-8"
	python_version: str = "3.11"
	scm_disabled: str = "true"
	qualitygate_wait: str = "false"
	properties: tuple[tuple[str, str], ...] = ()
	valid: bool = True
	invalid_reason: str | None = None


def configuration(
	root: str | Path | None = None,
	*,
	staged: bool = False,
) -> ScanConfig:
	"""Resolve settings from the index for staged scans.

	Only explicit environment overrides and files in the Git index are used
	when ``staged`` is true. Working-tree Sonar or IDE settings are ignored.
	"""
	base = Path(root).resolve() if root else Path.cwd().resolve()
	if staged:
		properties = _project_properties_text(
			gitio.staged_file_text("sonar-project.properties", base)
		)
		index_paths = gitio.staged_paths(base)
		source_default = (
			"FrontEnd"
			if any(path.casefold().startswith("frontend/") for path in index_paths)
			else "src"
		)
		tests_default = (
			"tests"
			if any(path.casefold().startswith("tests/") for path in index_paths)
			else ""
		)
	else:
		properties = _project_properties(base)
		source_default = "FrontEnd" if (base / "FrontEnd").is_dir() else "src"
		tests_default = "tests" if (base / "tests").is_dir() else ""

	invalid: list[str] = []
	host_url = _host_url(properties)
	if host_url is None:
		invalid.append("invalid server URL")
		host_url = DEFAULT_HOST_URL
	project_key = (
		os.environ.get("QUACK_SONAR_PROJECT_KEY")
		or os.environ.get("SONARQUBE_PROJECT_KEY")
		or os.environ.get("SONAR_PROJECT_KEY")
		or properties.get("sonar.projectKey")
		or DEFAULT_PROJECT_KEY
	).strip()
	sources = (
		os.environ.get("QUACK_SONAR_SOURCES")
		or os.environ.get("SONAR_SOURCES")
		or properties.get("sonar.sources")
		or source_default
	).strip()
	tests = (
		os.environ.get("QUACK_SONAR_TESTS")
		or os.environ.get("SONAR_TESTS")
		or properties.get("sonar.tests")
		or tests_default
	).strip()
	project_name = _validated_setting(
		os.environ.get("QUACK_SONAR_PROJECT_NAME")
		or os.environ.get("SONAR_PROJECT_NAME")
		or properties.get("sonar.projectName")
		or project_key,
		"project name",
		invalid,
	)
	branch_value = _validated_setting(
		os.environ.get("QUACK_SONAR_BRANCH")
		or os.environ.get("SONARQUBE_BRANCH")
		or os.environ.get("SONAR_BRANCH")
		or properties.get("sonar.branch.name"),
		"branch",
		invalid,
	)
	sources = _validated_setting(sources, "sources", invalid)
	tests = _validated_setting(tests, "tests", invalid)
	exclusions = _setting(
		("QUACK_SONAR_EXCLUSIONS", "SONAR_EXCLUSIONS"),
		properties,
		"sonar.exclusions",
		"",
		invalid,
	)
	inclusions = _setting(
		("QUACK_SONAR_INCLUSIONS", "SONAR_INCLUSIONS"),
		properties,
		"sonar.inclusions",
		"",
		invalid,
	)
	test_inclusions = _setting(
		("QUACK_SONAR_TEST_INCLUSIONS", "SONAR_TEST_INCLUSIONS"),
		properties,
		"sonar.test.inclusions",
		"",
		invalid,
	)
	coverage_exclusions = _setting(
		("QUACK_SONAR_COVERAGE_EXCLUSIONS", "SONAR_COVERAGE_EXCLUSIONS"),
		properties,
		"sonar.coverage.exclusions",
		"",
		invalid,
	)
	source_encoding = _setting(
		("QUACK_SONAR_SOURCE_ENCODING", "SONAR_SOURCE_ENCODING"),
		properties,
		"sonar.sourceEncoding",
		"UTF-8",
		invalid,
	)
	python_version = _setting(
		("QUACK_SONAR_PYTHON_VERSION", "SONAR_PYTHON_VERSION"),
		properties,
		"sonar.python.version",
		"3.11",
		invalid,
	)
	scm_disabled = _setting(
		(),
		properties,
		"sonar.scm.disabled",
		"true",
		invalid,
	)
	qualitygate_wait = _setting(
		(),
		properties,
		"sonar.qualitygate.wait",
		"false",
		invalid,
	)
	effective_properties = {
		key: value
		for key, value in properties.items()
		if _PROPERTY_KEY_RE.fullmatch(key)
		and key not in _SENSITIVE_PROPERTIES
		and key
		not in {"sonar.projectBaseDir", "sonar.scanner.metadataFilePath"}
	}
	effective_properties.update(
		{
			"sonar.host.url": host_url,
			"sonar.projectKey": project_key,
			"sonar.projectName": project_name,
			"sonar.sources": sources,
			"sonar.tests": tests,
			"sonar.exclusions": exclusions,
			"sonar.inclusions": inclusions,
			"sonar.test.inclusions": test_inclusions,
			"sonar.coverage.exclusions": coverage_exclusions,
			"sonar.sourceEncoding": source_encoding,
			"sonar.python.version": python_version,
			"sonar.scm.disabled": scm_disabled,
			"sonar.qualitygate.wait": qualitygate_wait,
		}
	)
	if branch_value:
		effective_properties["sonar.branch.name"] = branch_value
	return ScanConfig(
		host_url=host_url or DEFAULT_HOST_URL,
		project_key=project_key,
		project_name=project_name,
		branch=branch_value or None,
		sources=sources,
		tests=tests,
		exclusions=exclusions,
		inclusions=inclusions,
		test_inclusions=test_inclusions,
		coverage_exclusions=coverage_exclusions,
		source_encoding=source_encoding,
		python_version=python_version,
		scm_disabled=scm_disabled,
		qualitygate_wait=qualitygate_wait,
		properties=tuple(sorted(effective_properties.items())),
		valid=not invalid,
		invalid_reason="; ".join(dict.fromkeys(invalid)) or None,
	)


def scan(
	delta,
	root: str | Path,
	*,
	config: ScanConfig | None = None,
	snapshot_scope: str = "staged",
) -> ScanResult | None:
	"""Run one bounded staged or working-tree analysis.

	Staged scans export only the Git index for pre-commit. Working scans copy
	the complete current project, including non-ignored new files, for watch.
	"""
	started = time.perf_counter()
	if _disabled():
		return None
	if not getattr(delta, "files", None):
		return None
	if snapshot_scope not in {"staged", "working"}:
		return _result("failed", "invalid snapshot scope", started)

	try:
		root_path = Path(root).resolve()
	except OSError:
		return _result("failed", "repository path unavailable", started)

	settings = config or configuration(
		root_path, staged=snapshot_scope == "staged"
	)
	if not settings.valid:
		return _result(
			"skipped",
			f"invalid Sonar configuration: {settings.invalid_reason or 'unavailable'}",
			started,
			project_key=settings.project_key,
			branch=settings.branch,
		)
	host_url = settings.host_url
	if host_url is None:
		return _result("failed", "invalid server URL", started)

	project_key = settings.project_key
	if not _valid_project_key(project_key):
		return _result("failed", "invalid project key", started)

	scanner = _discover_scanner(root_path)
	if scanner is None:
		return _result("skipped", "scanner not found", started)

	token = _token()
	if not token:
		return _result("skipped", "Sonar token is not set", started)

	if _contains_backend_changes(delta):
		mixed = _contains_frontend_changes(delta)
		scope_label = "staged" if snapshot_scope == "staged" else "working-tree"
		reason = (
			f"mixed FrontEnd/BackEnd {scope_label} changes are unverified; generic "
			"sonar-scanner does not analyze Backend content"
			if mixed
			else (
				f"BackEnd {scope_label} changes are unverified; generic "
				"sonar-scanner requires dotnet-sonarscanner and a build"
			)
		)
		return _result(
			"skipped",
			reason,
			started,
			project_key=project_key,
			branch=settings.branch,
		)

	ready, reason = _server_ready(host_url)
	if not ready and reason != "server unavailable":
		return _result("skipped", reason, started)

	timeout_s = _timeout_seconds()
	try:
		with tempfile.TemporaryDirectory(prefix="quack-sonar-") as temp:
			snapshot = Path(temp)
			exporter = (
				gitio.export_staged_snapshot
				if snapshot_scope == "staged"
				else gitio.export_working_snapshot
			)
			if not exporter(snapshot, root_path):
				return _result(
					"failed",
					f"{snapshot_scope} snapshot unavailable",
					started,
				)

			properties = _scanner_properties(settings, snapshot)
			sonar_debug.emit(
				"SonarQube CLI input",
				{
					"scanner": scanner,
					"snapshot_scope": snapshot_scope,
					"snapshot_path": snapshot,
					"project_key": settings.project_key,
					"branch": settings.branch or "<unset>",
					"properties": properties,
					"changed_files": [
						getattr(item, "path", "")
						for item in getattr(delta, "files", [])
					],
					"timeout_s": timeout_s,
				},
				secrets=(token,),
			)
			had_sonar_token = "SONAR_TOKEN" in os.environ
			previous_token = os.environ.get("SONAR_TOKEN")
			if not previous_token:
				os.environ["SONAR_TOKEN"] = token
			try:
				exit_code, scanner_output = runio.run_sonar_scanner(
					scanner,
					properties,
					snapshot,
					timeout_s=timeout_s,
				)
			finally:
				if had_sonar_token:
					os.environ["SONAR_TOKEN"] = previous_token or ""
				else:
					os.environ.pop("SONAR_TOKEN", None)
			task_id, analysis_id = _analysis_metadata(snapshot)
			sonar_debug.emit(
				"SonarQube CLI output",
				{
					"exit_code": exit_code,
					"output": scanner_output,
					"task_id": task_id,
					"analysis_id": analysis_id,
				},
				secrets=(token,),
			)
	except OSError:
		return _result(
			"failed",
			"temporary analysis directory unavailable",
			started,
			project_key=project_key,
			branch=settings.branch,
		)

	dashboard = f"{host_url}/dashboard?id={project_key}"
	if exit_code == 0:
		if not task_id:
			return _result(
				"failed",
				"scan completion metadata unavailable",
				started,
				dashboard,
				project_key=project_key,
				branch=settings.branch,
			)
		completed, completion_reason, completed_analysis_id = _wait_for_analysis(
			host_url,
			token,
			task_id,
			timeout_s,
		)
		analysis_id = completed_analysis_id or analysis_id
		sonar_debug.emit(
			"SonarQube CLI analysis output",
			{
				"status": "passed" if completed else "pending",
				"project_key": project_key,
				"branch": settings.branch or "<unset>",
				"task_id": task_id,
				"analysis_id": analysis_id,
				"completion_reason": completion_reason,
				"dashboard_url": dashboard,
			},
			secrets=(token,),
		)
		if not completed:
			return _result(
				"pending",
				f"analysis uploaded; {completion_reason}",
				started,
				dashboard,
				project_key=project_key,
				branch=settings.branch,
				task_id=task_id,
				analysis_id=analysis_id,
				uploaded=True,
			)
		return _result(
			"passed",
			"analysis completed",
			started,
			dashboard,
			project_key=project_key,
			branch=settings.branch,
			task_id=task_id,
			analysis_id=analysis_id,
			confirmed=True,
		)
	if exit_code == -1:
		return _result(
			"failed",
			f"scanner timed out after {timeout_s}s",
			started,
			dashboard,
			project_key=project_key,
			branch=settings.branch,
		)
	if exit_code == 127:
		return _result(
			"skipped",
			"scanner not found",
			started,
			project_key=project_key,
			branch=settings.branch,
		)
	if exit_code == 126:
		return _result(
			"skipped",
			"scanner flavor unavailable for this project",
			started,
			dashboard,
			project_key=project_key,
			branch=settings.branch,
		)
	return _result(
		"failed",
		_scanner_failure_reason(exit_code, scanner_output),
		started,
		dashboard,
		project_key=project_key,
		branch=settings.branch,
	)


def _scanner_failure_reason(exit_code: int, output: str) -> str:
	"""Return a bounded scanner error without leaking credentials."""
	detail = ""
	for line in reversed((output or "").splitlines()):
		if "error" in line.casefold() or "failure" in line.casefold():
			detail = " ".join(line.split())
			break
	if not detail:
		detail = "scanner returned no diagnostic output"
	for secret in (_token(),):
		if secret:
			detail = detail.replace(secret, "<redacted>")
	return f"scanner exited with code {exit_code}: {detail[:240]}"


def _disabled() -> bool:
	"""Return whether the explicit opt-out is active."""
	disable = os.environ.get("QUACK_DISABLE_SONAR", "").strip().lower()
	if disable in {"1", "true", "yes", "on"}:
		return True
	return os.environ.get("QUACK_SONAR", "auto").strip().lower() in _DISABLED_VALUES


def _host_url(properties: dict[str, str] | None = None) -> str | None:
	"""Resolve and validate the configured SonarQube HTTP endpoint."""
	raw = (
		os.environ.get("QUACK_SONAR_HOST_URL")
		or os.environ.get("SONAR_HOST_URL")
		or os.environ.get("SONARQUBE_URL")
		or (properties or {}).get("sonar.host.url")
		or DEFAULT_HOST_URL
	).strip()
	try:
		parts = urlsplit(raw)
	except ValueError:
		return None
	if parts.scheme not in {"http", "https"} or not parts.netloc:
		return None
	if parts.username or parts.password:
		return None
	return raw.rstrip("/")


def _validated_setting(
	value: str | None,
	name: str,
	invalid: list[str],
) -> str:
	"""Keep scanner properties bounded and free of control characters."""
	text = (value or "").strip()
	if len(text) > _MAX_PROPERTY_VALUE or any(
		ord(char) < 32 or ord(char) == 127 for char in text
	):
		invalid.append(f"invalid {name}")
		return ""
	return text


def _setting(
	environment_names: tuple[str, ...],
	properties: dict[str, str],
	property_name: str,
	default: str,
	invalid: list[str],
) -> str:
	value = next(
		(os.environ.get(name) for name in environment_names if os.environ.get(name)),
		None,
	)
	if value is None:
		value = properties.get(property_name, default)
	return _validated_setting(value, property_name, invalid)


def _valid_project_key(value: str) -> bool:
	return bool(value) and all(char in _PROJECT_KEY_CHARS for char in value)


def effective_identity(
	root: str | Path,
	*,
	settings: ScanConfig | None = None,
) -> dict[str, object]:
	"""Return the stable scanner/config identity used by staged freshness."""
	base = Path(root).resolve()
	resolved = settings or configuration(base, staged=True)
	return {
		"scanner": _scanner_identity(_discover_scanner(base)),
		"settings": {
			"host_url": resolved.host_url,
			"project_key": resolved.project_key,
			"project_name": resolved.project_name,
			"branch": resolved.branch,
			"sources": resolved.sources,
			"tests": resolved.tests,
			"exclusions": resolved.exclusions,
			"inclusions": resolved.inclusions,
			"test_inclusions": resolved.test_inclusions,
			"coverage_exclusions": resolved.coverage_exclusions,
			"source_encoding": resolved.source_encoding,
			"python_version": resolved.python_version,
			"scm_disabled": resolved.scm_disabled,
			"qualitygate_wait": resolved.qualitygate_wait,
			"properties": list(resolved.properties),
			"valid": resolved.valid,
			"invalid_reason": resolved.invalid_reason,
		},
	}


def _scanner_identity(scanner: str | None) -> dict[str, object]:
	"""Describe the selected executable without invoking an untrusted command."""
	if not scanner:
		return {"path": None}
	try:
		path = Path(scanner).expanduser().resolve()
		stat = path.stat()
	except (OSError, RuntimeError, TypeError, ValueError):
		return {"path": str(scanner), "unavailable": True}
	identity: dict[str, object] = {
		"path": os.path.normcase(str(path)),
		"size": stat.st_size,
		"mtime_ns": stat.st_mtime_ns,
		"mode": stat.st_mode,
	}
	if stat.st_size <= 64 * 1024 * 1024 and path.is_file():
		try:
			digest = hashlib.sha256()
			with path.open("rb") as handle:
				for chunk in iter(lambda: handle.read(1024 * 1024), b""):
					digest.update(chunk)
			identity["sha256"] = digest.hexdigest()
		except OSError:
			identity["unavailable"] = True
	return identity


def _discover_scanner(root: Path) -> str | None:
	"""Find an explicit, PATH, or available Quack/repository scanner."""
	candidates: list[Path] = []
	configured = os.environ.get("QUACK_SONAR_SCANNER")
	if configured:
		try:
			configured_path = Path(configured.strip().strip('"')).expanduser()
			if configured_path.is_dir():
				candidates.extend(
					(
						configured_path / "bin" / "sonar-scanner.bat",
						configured_path / "bin" / "sonar-scanner",
					)
				)
			else:
				candidates.append(configured_path)
		except OSError:
			pass

	for command in ("sonar-scanner", "sonar-scanner.bat"):
		found = shutil.which(command)
		if found:
			candidates.append(Path(found))

	tool_directories = [root / "tools"]
	configured_tools = os.environ.get("QUACK_TOOLS_DIR")
	if configured_tools:
		try:
			tool_directories.append(Path(configured_tools.strip().strip('"')).expanduser())
		except OSError:
			pass
	try:
		quack_root = Path(__file__).resolve().parents[2]
	except (OSError, RuntimeError):
		quack_root = None
	if quack_root is not None:
		tool_directories.append(quack_root / "tools")
	for tools in tool_directories:
		try:
			candidates.extend(sorted(tools.glob("sonar-scanner-*/bin/sonar-scanner*")))
		except OSError:
			continue

	seen: set[str] = set()
	for path in candidates:
		try:
			resolved = path.expanduser().resolve()
			identity = os.path.normcase(str(resolved))
		except (OSError, RuntimeError):
			continue
		if identity in seen:
			continue
		seen.add(identity)
		if path.name.lower() in {"sonar-scanner", "sonar-scanner.bat"} and path.is_file():
			return str(resolved)
	return None


def _server_ready(host_url: str) -> tuple[bool, str]:
	"""Check only the local server status endpoint within a short bound."""
	request = Request(
		f"{host_url}/api/system/status",
		headers={"Accept": "application/json"},
	)
	try:
		with _open_sonar(request, timeout=HEALTH_TIMEOUT_S) as response:
			payload = json.loads(response.read(64 * 1024))
	except (
		HTTPError,
		URLError,
		HTTPException,
		TimeoutError,
		OSError,
		ValueError,
	):
		return False, "server unavailable"

	if not isinstance(payload, dict):
		return False, "server status unavailable"
	status = payload.get("status")
	if status == "UP":
		return True, ""
	if isinstance(status, str) and status:
		return False, f"server status {status.lower()}"
	return False, "server not ready"


def _timeout_seconds() -> int:
	raw = os.environ.get("QUACK_SONAR_TIMEOUT_S", "")
	try:
		value = float(raw) if raw else float(DEFAULT_TIMEOUT_S)
	except (TypeError, ValueError):
		value = float(DEFAULT_TIMEOUT_S)
	if not math.isfinite(value):
		value = float(DEFAULT_TIMEOUT_S)
	return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, int(value)))


def _scanner_properties(settings: ScanConfig, snapshot: Path) -> list[str]:
	properties = [
		f"-Dsonar.projectBaseDir={snapshot}",
		f"-Dsonar.projectKey={settings.project_key}",
		f"-Dsonar.projectName={settings.project_name}",
		f"-Dsonar.sources={settings.sources}",
	]
	if settings.tests:
		properties.append(f"-Dsonar.tests={settings.tests}")
	properties += [
		f"-Dsonar.python.version={settings.python_version}",
		f"-Dsonar.sourceEncoding={settings.source_encoding}",
		f"-Dsonar.scm.disabled={settings.scm_disabled}",
		f"-Dsonar.host.url={settings.host_url}",
		f"-Dsonar.qualitygate.wait={settings.qualitygate_wait}",
		f"-Dsonar.scanner.metadataFilePath={snapshot / '.scannerwork' / 'report-task.txt'}",
	]
	if settings.branch:
		properties.append(f"-Dsonar.branch.name={settings.branch}")
	fixed = {
		"sonar.projectKey",
		"sonar.projectName",
		"sonar.sources",
		"sonar.tests",
		"sonar.python.version",
		"sonar.sourceEncoding",
		"sonar.scm.disabled",
		"sonar.host.url",
		"sonar.qualitygate.wait",
		"sonar.branch.name",
	}
	for key, value in settings.properties:
		if key not in fixed and value:
			properties.append(f"-D{key}={value}")
	return properties


def _result(
	status: str,
	reason: str,
	started: float,
	dashboard_url: str | None = None,
	*,
	project_key: str | None = None,
	branch: str | None = None,
	task_id: str | None = None,
	analysis_id: str | None = None,
	confirmed: bool = False,
	uploaded: bool = False,
) -> ScanResult:
	return ScanResult(
		status=status,
		reason=reason,
		duration_s=time.perf_counter() - started,
		dashboard_url=dashboard_url,
		project_key=project_key,
		branch=branch,
		task_id=task_id,
		analysis_id=analysis_id,
		confirmed=confirmed,
		uploaded=uploaded,
	)


def _analysis_metadata(snapshot: Path) -> tuple[str | None, str | None]:
	"""Read the scanner task metadata from the isolated snapshot."""
	metadata_path = snapshot / ".scannerwork" / "report-task.txt"
	try:
		lines = metadata_path.read_text(encoding="utf-8").splitlines()
	except (OSError, UnicodeError):
		return None, None
	values: dict[str, str] = {}
	for line in lines:
		key, separator, value = line.partition("=")
		if separator and key.strip():
			values[key.strip()] = value.strip()
	task_id = values.get("ceTaskId")
	if not task_id or not _TASK_ID_RE.fullmatch(task_id):
		return None, None
	analysis_id = values.get("analysisId")
	if analysis_id and not _TASK_ID_RE.fullmatch(analysis_id):
		analysis_id = None
	return task_id, analysis_id


def _wait_for_analysis(
	host_url: str,
	token: str,
	task_id: str,
	timeout_s: int,
) -> tuple[bool, str, str | None]:
	"""Wait for the exact scanner task to complete, bounded and fail-open."""
	deadline = time.monotonic() + min(
		max(timeout_s, MIN_TIMEOUT_S),
		MAX_TIMEOUT_S,
	)
	auth = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
	url = f"{host_url}/api/ce/task?id={task_id}"
	while True:
		request = Request(
			url,
			headers={
				"Accept": "application/json",
				"Authorization": f"Basic {auth}",
			},
		)
		try:
			with _open_sonar(request, timeout=HEALTH_TIMEOUT_S) as response:
				payload = json.loads(response.read(64 * 1024))
		except HTTPError as exc:
			if exc.code in {401, 403}:
				return False, "analysis completion unavailable", None
			if time.monotonic() >= deadline:
				return False, "analysis completion timed out", None
			time.sleep(0.5)
			continue
		except (URLError, HTTPException, TimeoutError, OSError, ValueError):
			if time.monotonic() >= deadline:
				return False, "analysis completion timed out", None
			time.sleep(0.5)
			continue
		task = payload.get("task") if isinstance(payload, dict) else None
		if not isinstance(task, dict):
			return False, "analysis completion unavailable", None
		status = task.get("status")
		analysis_id = task.get("analysisId")
		if analysis_id is not None and not isinstance(analysis_id, str):
			analysis_id = None
		if status == "SUCCESS":
			return True, "analysis completed", analysis_id
		if status in {"FAILED", "CANCELED"}:
			return False, f"analysis task {str(status).lower()}", analysis_id
		if time.monotonic() >= deadline:
			return False, "analysis completion timed out", analysis_id
		time.sleep(0.5)


def _open_sonar(request: Request, *, timeout: float):
	"""Open Sonar directly before falling back to the process proxy.

	Some enterprise proxy configurations intercept generic Python HTTPS
	traffic while the scanner and MCP can reach the Sonar host directly.
	Prefer the direct route for the authenticated scanner lifecycle, but keep
	the configured proxy as a bounded fallback for environments that require it.
	"""
	try:
		return build_opener(ProxyHandler({})).open(request, timeout=timeout)
	except HTTPError:
		raise
	except (URLError, TimeoutError, OSError):
		return urlopen(request, timeout=timeout)


def _token() -> str:
	return (
		os.environ.get("SONAR_TOKEN", "").strip()
		or os.environ.get("SQ_TOKEN", "").strip()
		or os.environ.get("SONARQUBE_TOKEN", "").strip()
	)


def _project_properties(root: Path) -> dict[str, str]:
	try:
		text = (root / "sonar-project.properties").read_text(encoding="utf-8")
	except (OSError, UnicodeError):
		return {}
	return _project_properties_text(text)


def _project_properties_text(text: str | None) -> dict[str, str]:
	if not isinstance(text, str):
		return {}
	result: dict[str, str] = {}
	for line in text.splitlines():
		key, separator, value = line.partition("=")
		if separator and key.strip() and not key.lstrip().startswith("#"):
			result[key.strip()] = value.strip()
	return result


def _contains_backend_changes(delta) -> bool:
	paths = [str(getattr(item, "path", "")).casefold() for item in delta.files]
	return any(
		path.endswith(
			(".cs", ".csproj", ".sln", ".fs", ".fsproj", ".vb", ".vbproj")
		)
		for path in paths
	)


def _contains_frontend_changes(delta) -> bool:
	paths = [str(getattr(item, "path", "")).replace("\\", "/").casefold() for item in delta.files]
	return any(path.startswith("frontend/") for path in paths)
