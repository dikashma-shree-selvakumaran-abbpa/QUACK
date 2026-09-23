"""Tests for the optional staged SonarQube integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from quack import sonar


@pytest.fixture(autouse=True)
def _clear_ambient_sonar_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
	for name in (
		"QUACK_SONAR_BRANCH",
		"QUACK_SONAR_EXCLUSIONS",
		"QUACK_SONAR_HOST_URL",
		"QUACK_SONAR_INCLUSIONS",
		"QUACK_SONAR_PROJECT_KEY",
		"QUACK_SONAR_SCANNER",
		"QUACK_TOOLS_DIR",
		"QUACK_SONAR_SOURCES",
		"QUACK_SONAR_TESTS",
		"SONARQUBE_BRANCH",
		"SONARQUBE_PROJECT_KEY",
		"SONARQUBE_URL",
		"SONAR_BRANCH",
		"SONAR_HOST_URL",
		"SONAR_PROJECT_KEY",
		"SONAR_SOURCES",
		"SONAR_TESTS",
	):
		monkeypatch.delenv(name, raising=False)


def _delta() -> SimpleNamespace:
	return SimpleNamespace(files=["src/example.py"])


def test_scan_is_disabled_without_side_effects(monkeypatch, tmp_path) -> None:
	monkeypatch.setenv("QUACK_DISABLE_SONAR", "1")

	def boom(*args, **kwargs):  # pragma: no cover - regression guard
		raise AssertionError("disabled SonarQube must not probe or scan")

	monkeypatch.setattr(sonar, "_discover_scanner", boom)

	assert sonar.scan(_delta(), tmp_path) is None


def test_scan_skips_without_token(monkeypatch, tmp_path) -> None:
	monkeypatch.delenv("QUACK_DISABLE_SONAR", raising=False)
	monkeypatch.delenv("QUACK_SONAR", raising=False)
	monkeypatch.delenv("SONAR_TOKEN", raising=False)
	monkeypatch.delenv("SQ_TOKEN", raising=False)
	monkeypatch.delenv("SONARQUBE_TOKEN", raising=False)
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "skipped"
	assert result.reason == "Sonar token is not set"


def test_discover_scanner_falls_back_to_quack_tools_after_stale_override(
	monkeypatch, tmp_path
) -> None:
	stale = tmp_path / "missing" / "sonar-scanner.bat"
	tools = tmp_path / "quack-tools"
	scanner = tools / "sonar-scanner-8.1.0.6389-windows-x64" / "bin"
	scanner.mkdir(parents=True)
	executable = scanner / "sonar-scanner.bat"
	executable.write_text("@echo off\n", encoding="utf-8")
	monkeypatch.setenv("QUACK_SONAR_SCANNER", str(stale))
	monkeypatch.setenv("QUACK_TOOLS_DIR", str(tools))
	monkeypatch.setattr(sonar.shutil, "which", lambda command: None)

	assert sonar._discover_scanner(tmp_path / "alarms") == str(executable.resolve())


def test_scan_exports_index_and_runs_bounded_scanner(
	monkeypatch, tmp_path, capsys
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setenv("QUACK_SONAR_DEBUG", "1")
	monkeypatch.setenv("QUACK_SONAR_TIMEOUT_S", "30")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))

	captured: dict = {}

	def export(destination, root):
		captured["root"] = root
		Path(destination, "src").mkdir()
		Path(destination, "src", "example.py").write_text(
			"print('staged')\n", encoding="utf-8"
		)
		return True

	def run(scanner, properties, cwd, timeout_s):
		captured["scanner"] = scanner
		captured["properties"] = properties
		captured["cwd_exists_during_run"] = Path(cwd).exists()
		captured["timeout_s"] = timeout_s
		metadata = Path(cwd, ".scannerwork")
		metadata.mkdir()
		(metadata / "report-task.txt").write_text(
			"ceTaskId=task-1\n", encoding="utf-8"
		)
		return 0, "scanner output must not be rendered"

	monkeypatch.setattr(sonar.gitio, "export_staged_snapshot", export)
	monkeypatch.setattr(sonar.runio, "run_sonar_scanner", run)
	monkeypatch.setattr(
		sonar,
		"_wait_for_analysis",
		lambda host, token, task, timeout: (True, "analysis completed", "analysis-1"),
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "passed"
	assert result.reason == "analysis completed"
	assert result.confirmed is True
	assert result.task_id == "task-1"
	assert result.analysis_id == "analysis-1"
	assert result.dashboard_url == "https://codescan.abb.com/dashboard?id=quack-local"
	assert captured["root"] == tmp_path.resolve()
	assert captured["scanner"] == "scanner"
	assert captured["cwd_exists_during_run"] is True
	assert captured["timeout_s"] == 30
	assert "-Dsonar.scm.disabled=true" in captured["properties"]
	assert "-Dsonar.host.url=https://codescan.abb.com" in captured["properties"]
	assert any(
		property.startswith("-Dsonar.scanner.metadataFilePath=")
		for property in captured["properties"]
	)
	debug = capsys.readouterr().err
	assert "[debug] SonarQube CLI input" in debug
	assert "[debug] SonarQube CLI output" in debug
	assert "[debug] SonarQube CLI analysis output" in debug
	assert "snapshot_export_ms=" in debug
	assert "scanner_process_ms=" in debug
	assert "analysis_wait_ms=" in debug
	assert "total_scan_ms=" in debug
	assert "-Dsonar.projectKey=quack-local" in debug
	assert "local-token" not in debug


def test_scan_replaces_empty_sonar_token_with_configured_token(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "")
	monkeypatch.setenv("SQ_TOKEN", "configured-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))
	monkeypatch.setattr(
		sonar.gitio, "export_staged_snapshot", lambda destination, root: True
	)
	captured: dict = {}

	def run(scanner, properties, cwd, timeout_s):
		captured["token"] = os.environ.get("SONAR_TOKEN")
		metadata = Path(cwd, ".scannerwork")
		metadata.mkdir()
		(metadata / "report-task.txt").write_text(
			"ceTaskId=task-token\n", encoding="utf-8"
		)
		return 0, ""

	monkeypatch.setattr(sonar.runio, "run_sonar_scanner", run)
	monkeypatch.setattr(
		sonar,
		"_wait_for_analysis",
		lambda host, token, task, timeout: (True, "analysis completed", None),
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "passed"
	assert captured["token"] == "configured-token"
	assert os.environ.get("SONAR_TOKEN") == ""


def test_scan_working_scope_exports_worktree_instead_of_index(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))
	captured: dict = {}

	def export(destination, root):
		captured["root"] = root
		Path(destination, "src").mkdir()
		Path(destination, "src", "new.py").write_text(
			"new = True\n", encoding="utf-8"
		)
		return True

	def run(scanner, properties, cwd, timeout_s):
		captured["snapshot"] = Path(cwd, "src", "new.py").read_text(
			encoding="utf-8"
		)
		metadata = Path(cwd, ".scannerwork")
		metadata.mkdir()
		(metadata / "report-task.txt").write_text(
			"ceTaskId=task-working\n", encoding="utf-8"
		)
		return 0, ""

	monkeypatch.setattr(sonar.gitio, "export_working_snapshot", export)
	monkeypatch.setattr(sonar.runio, "run_sonar_scanner", run)
	monkeypatch.setattr(
		sonar,
		"_wait_for_analysis",
		lambda host, token, task, timeout: (
			True,
			"analysis completed",
			"analysis-working",
		),
	)

	result = sonar.scan(_delta(), tmp_path, snapshot_scope="working")

	assert result is not None
	assert result.status == "passed"
	assert result.analysis_id == "analysis-working"
	assert captured["root"] == tmp_path.resolve()
	assert captured["snapshot"] == "new = True\n"


def test_scan_surfaces_bounded_redacted_scanner_error(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "secret-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))
	monkeypatch.setattr(
		sonar.gitio, "export_staged_snapshot", lambda destination, root: True
	)
	monkeypatch.setattr(
		sonar.runio,
		"run_sonar_scanner",
		lambda scanner, properties, cwd, timeout_s: (
			1,
			"INFO scanner\nERROR authorization failed for secret-token\n"
			+ ("x" * 1000),
		),
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "failed"
	assert "authorization failed" in result.reason
	assert "secret-token" not in result.reason
	assert len(result.reason) <= 320


def test_scan_fails_open_on_timeout(monkeypatch, tmp_path) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))
	monkeypatch.setattr(
		sonar.gitio, "export_staged_snapshot", lambda destination, root: True
	)
	monkeypatch.setattr(
		sonar.runio,
		"run_sonar_scanner",
		lambda scanner, properties, cwd, timeout_s: (-1, ""),
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "failed"
	assert result.reason == "scanner timed out after 180s"


def test_scan_does_not_mark_upload_fresh_without_task_metadata(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(sonar, "_server_ready", lambda host: (True, ""))
	monkeypatch.setattr(
		sonar.gitio, "export_staged_snapshot", lambda destination, root: True
	)
	monkeypatch.setattr(
		sonar.runio,
		"run_sonar_scanner",
		lambda scanner, properties, cwd, timeout_s: (0, ""),
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "failed"
	assert result.confirmed is False
	assert result.reason == "scan completion metadata unavailable"


def test_scan_skips_when_server_is_not_ready(monkeypatch, tmp_path) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(
		sonar, "_server_ready", lambda host: (False, "server status starting")
	)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "skipped"
	assert result.reason == "server status starting"


def test_scan_lets_scanner_verify_when_health_probe_is_unavailable(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	monkeypatch.setattr(
		sonar, "_server_ready", lambda host: (False, "server unavailable")
	)
	monkeypatch.setattr(
		sonar.gitio, "export_staged_snapshot", lambda destination, root: True
	)
	captured = {}

	def run(scanner, properties, cwd, timeout_s):
		captured["scanner"] = scanner
		return 126, ""

	monkeypatch.setattr(sonar.runio, "run_sonar_scanner", run)

	result = sonar.scan(_delta(), tmp_path)

	assert captured["scanner"] == "scanner"
	assert result is not None
	assert result.status == "skipped"
	assert result.reason == "scanner flavor unavailable for this project"


def test_wait_for_analysis_retries_transient_transport_errors(monkeypatch) -> None:
	calls = 0

	class Response:
		def __enter__(self):
			return self

		def __exit__(self, *args):
			return False

		def read(self, limit):
			return json.dumps(
				{
					"task": {
						"status": "SUCCESS",
						"analysisId": "analysis-1",
					}
				}
			).encode()

	def open_sonar(request, *, timeout):
		nonlocal calls
		calls += 1
		if calls == 1:
			raise URLError("temporary network failure")
		return Response()

	monkeypatch.setattr(sonar, "_open_sonar", open_sonar)
	monkeypatch.setattr(sonar.time, "sleep", lambda seconds: None)

	result = sonar._wait_for_analysis(
		"http://sonar.example",
		"token",
		"task-1",
		30,
	)

	assert result == (True, "analysis completed", "analysis-1")
	assert calls == 2


def test_staged_configuration_ignores_worktree_sonar_and_ide_settings(
	monkeypatch, tmp_path
) -> None:
	(tmp_path / "sonar-project.properties").write_text(
		"sonar.projectKey=worktree-key\nsonar.sources=unstaged\n",
		encoding="utf-8",
	)
	settings_dir = tmp_path / ".vscode"
	settings_dir.mkdir()
	(settings_dir / "settings.json").write_text(
		'{"sonarlint.connectedMode.project": {"projectKey": "ide-key"}}',
		encoding="utf-8",
	)
	monkeypatch.setattr(
		sonar.gitio,
		"staged_file_text",
		lambda path, root: (
			"sonar.projectKey=index-key\n"
			"sonar.sources=FrontEnd\n"
			"sonar.exclusions=**/generated/**\n"
		),
	)
	monkeypatch.setattr(
		sonar.gitio,
		"staged_paths",
		lambda root: ["FrontEnd/src/app.ts", "tests/app.test.ts"],
	)

	settings = sonar.configuration(tmp_path, staged=True)

	assert settings.project_key == "index-key"
	assert settings.sources == "FrontEnd"
	assert settings.tests == "tests"
	assert settings.exclusions == "**/generated/**"
	assert settings.project_key != "worktree-key"
	assert settings.project_key != "ide-key"


def test_effective_identity_changes_with_scanner_and_settings(
	monkeypatch, tmp_path
) -> None:
	scanner = tmp_path / "sonar-scanner.bat"
	scanner.write_text("scanner-one", encoding="utf-8")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: str(scanner))

	first = sonar.effective_identity(tmp_path)
	monkeypatch.setenv("QUACK_SONAR_EXCLUSIONS", "**/generated/**")
	second = sonar.effective_identity(tmp_path)
	scanner.write_text("scanner-two", encoding="utf-8")
	third = sonar.effective_identity(tmp_path)

	assert first != second
	assert second != third


def test_invalid_explicit_staged_configuration_fails_open(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setenv("QUACK_SONAR_EXCLUSIONS", "bad\nproperty")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "skipped"
	assert "invalid Sonar configuration" in result.reason


def test_scan_fails_open_for_mixed_frontend_and_backend_changes(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")
	delta = SimpleNamespace(
		files=[
			SimpleNamespace(path="FrontEnd/src/app.ts"),
			SimpleNamespace(path="BackEnd/src/Alarm.cs"),
		]
	)

	result = sonar.scan(delta, tmp_path)

	assert result is not None
	assert result.status == "skipped"
	assert result.confirmed is False
	assert "mixed FrontEnd/BackEnd staged changes are unverified" in result.reason
