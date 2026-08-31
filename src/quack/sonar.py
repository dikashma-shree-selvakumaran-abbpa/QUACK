"""Optional local SonarQube analysis for the pre-commit path.

The integration is advisory and fail-open. It only runs when a scanner,
``SONAR_TOKEN``, and a healthy SonarQube server are available. Analysis is
performed from a temporary export of the Git index so unstaged edits are never
included accidentally. Scanner output is retained only in memory and reduced
to a bounded, controlled reason before it reaches the terminal or metrics.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import gitio, runio

DEFAULT_HOST_URL = "http://127.0.0.1:9002"
DEFAULT_PROJECT_KEY = "quack-local"
DEFAULT_TIMEOUT_S = runio.SONAR_TIMEOUT_S
HEALTH_TIMEOUT_S = 2.0
MAX_TIMEOUT_S = 120
MIN_TIMEOUT_S = 5

_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
_PROJECT_KEY_CHARS = frozenset(
	"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-"
)


@dataclass(frozen=True)
class ScanResult:
	"""Safe summary of one SonarQube attempt."""

	status: str
	reason: str = ""
	duration_s: float = 0.0
	dashboard_url: str | None = None


def scan(delta, root: str | Path) -> ScanResult | None:
	"""Run one bounded staged snapshot analysis, or return ``None`` if disabled."""
	started = time.perf_counter()
	if _disabled():
		return None
	if not getattr(delta, "files", None):
		return None

	try:
		root_path = Path(root).resolve()
	except OSError:
		return _result("failed", "repository path unavailable", started)

	host_url = _host_url()
	if host_url is None:
		return _result("failed", "invalid server URL", started)

	project_key = os.environ.get("QUACK_SONAR_PROJECT_KEY", DEFAULT_PROJECT_KEY)
	if not _valid_project_key(project_key):
		return _result("failed", "invalid project key", started)

	scanner = _discover_scanner(root_path)
	if scanner is None:
		return _result("skipped", "scanner not found", started)

	if not os.environ.get("SONAR_TOKEN", "").strip():
		return _result("skipped", "SONAR_TOKEN is not set", started)

	ready, reason = _server_ready(host_url)
	if not ready:
		return _result("skipped", reason, started)

	timeout_s = _timeout_seconds()
	try:
		with tempfile.TemporaryDirectory(prefix="quack-sonar-") as temp:
			snapshot = Path(temp)
			if not gitio.export_staged_snapshot(snapshot, root_path):
				return _result("failed", "staged snapshot unavailable", started)

			properties = _scanner_properties(host_url, project_key)
			exit_code, _output = runio.run_sonar_scanner(
				scanner,
				properties,
				snapshot,
				timeout_s=timeout_s,
			)
	except OSError:
		return _result("failed", "temporary analysis directory unavailable", started)

	dashboard = f"{host_url}/dashboard?id={project_key}"
	if exit_code == 0:
		return _result("passed", "analysis uploaded", started, dashboard)
	if exit_code == -1:
		return _result(
			"failed", f"scanner timed out after {timeout_s}s", started, dashboard
		)
	if exit_code == 127:
		return _result("skipped", "scanner not found", started)
	return _result(
		"failed", f"scanner exited with code {exit_code}", started, dashboard
	)


def _disabled() -> bool:
	"""Return whether the explicit opt-out is active."""
	disable = os.environ.get("QUACK_DISABLE_SONAR", "").strip().lower()
	if disable in {"1", "true", "yes", "on"}:
		return True
	return os.environ.get("QUACK_SONAR", "auto").strip().lower() in _DISABLED_VALUES


def _host_url() -> str | None:
	"""Resolve and validate the configured SonarQube HTTP endpoint."""
	raw = (
		os.environ.get("QUACK_SONAR_HOST_URL")
		or os.environ.get("SONAR_HOST_URL")
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


def _valid_project_key(value: str) -> bool:
	return bool(value) and all(char in _PROJECT_KEY_CHARS for char in value)


def _discover_scanner(root: Path) -> str | None:
	"""Find an explicit, PATH, or repository-local SonarScanner executable."""
	configured = os.environ.get("QUACK_SONAR_SCANNER")
	if configured:
		try:
			path = Path(configured).expanduser()
			return str(path) if path.is_file() else None
		except OSError:
			return None

	for command in ("sonar-scanner", "sonar-scanner.bat"):
		found = shutil.which(command)
		if found:
			return found

	tools = root / "tools"
	try:
		candidates = sorted(tools.glob("sonar-scanner-*/bin/sonar-scanner*"))
	except OSError:
		return None
	for path in candidates:
		if path.name.lower() in {"sonar-scanner", "sonar-scanner.bat"} and path.is_file():
			return str(path)
	return None


def _server_ready(host_url: str) -> tuple[bool, str]:
	"""Check only the local server status endpoint within a short bound."""
	request = Request(
		f"{host_url}/api/system/status",
		headers={"Accept": "application/json"},
	)
	try:
		with urlopen(request, timeout=HEALTH_TIMEOUT_S) as response:
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


def _scanner_properties(host_url: str, project_key: str) -> list[str]:
	return [
		f"-Dsonar.projectKey={project_key}",
		"-Dsonar.projectName=QUACK",
		"-Dsonar.sources=src",
		"-Dsonar.tests=tests",
		"-Dsonar.python.version=3.11",
		"-Dsonar.sourceEncoding=UTF-8",
		"-Dsonar.scm.disabled=true",
		f"-Dsonar.host.url={host_url}",
		"-Dsonar.qualitygate.wait=false",
	]


def _result(
	status: str,
	reason: str,
	started: float,
	dashboard_url: str | None = None,
) -> ScanResult:
	return ScanResult(
		status=status,
		reason=reason,
		duration_s=time.perf_counter() - started,
		dashboard_url=dashboard_url,
	)
