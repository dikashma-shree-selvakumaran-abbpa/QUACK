"""Tests for the bounded subprocess adapters."""

from __future__ import annotations

import json
import subprocess

from quack import runio


def test_run_sonar_scanner_builds_non_shell_command(monkeypatch, tmp_path) -> None:
	captured = {}

	def fake_run(args, **kwargs):
		captured["args"] = args
		captured["kwargs"] = kwargs
		return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

	monkeypatch.setattr(runio.subprocess, "run", fake_run)

	result = runio.run_sonar_scanner(
		"C:\\tools\\sonar-scanner.bat",
		["-Dsonar.projectKey=quack-local"],
		tmp_path,
		timeout_s=12,
	)

	assert result == (0, "ok")
	assert captured["args"] == [
		"C:\\tools\\sonar-scanner.bat",
		"-Dsonar.projectKey=quack-local",
	]
	assert captured["kwargs"]["cwd"] == str(tmp_path)
	assert captured["kwargs"]["timeout"] == 12
	assert captured["kwargs"]["capture_output"] is True


def test_run_sonar_scanner_rejects_build_integrated_runner(monkeypatch, tmp_path) -> None:
	monkeypatch.setattr(
		runio.subprocess,
		"run",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			AssertionError("dotnet scanner must not start")
		),
	)

	assert runio.run_sonar_scanner(
		"dotnet-sonarscanner",
		["-Dsonar.projectKey=alarms"],
		tmp_path,
	) == (126, "<runner unavailable: dotnet-sonarscanner requires a build and is not supported by the staged snapshot runner>")


def test_run_sonar_scanner_rejects_unvalidated_properties(tmp_path) -> None:
	assert runio.run_sonar_scanner(
		"sonar-scanner",
		["--not-a-property"],
		tmp_path,
	) == (126, "<invalid SonarScanner property>")

