"""Tests for the optional staged SonarQube integration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quack import sonar


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
	monkeypatch.setattr(sonar, "_discover_scanner", lambda root: "scanner")

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "skipped"
	assert result.reason == "SONAR_TOKEN is not set"


def test_scan_exports_index_and_runs_bounded_scanner(monkeypatch, tmp_path) -> None:
	monkeypatch.setenv("SONAR_TOKEN", "local-token")
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
		return 0, "scanner output must not be rendered"

	monkeypatch.setattr(sonar.gitio, "export_staged_snapshot", export)
	monkeypatch.setattr(sonar.runio, "run_sonar_scanner", run)

	result = sonar.scan(_delta(), tmp_path)

	assert result is not None
	assert result.status == "passed"
	assert result.reason == "analysis uploaded"
	assert result.dashboard_url == "http://127.0.0.1:9002/dashboard?id=quack-local"
	assert captured["root"] == tmp_path.resolve()
	assert captured["scanner"] == "scanner"
	assert captured["cwd_exists_during_run"] is True
	assert captured["timeout_s"] == 30
	assert "-Dsonar.scm.disabled=true" in captured["properties"]
	assert "-Dsonar.host.url=http://127.0.0.1:9002" in captured["properties"]


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
	assert result.reason == "scanner timed out after 60s"


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
