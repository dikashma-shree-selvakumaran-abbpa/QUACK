"""Tests for the bounded subprocess adapters."""

from __future__ import annotations

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


def test_run_sonarqube_mcp_stops_after_response_and_captures_stderr(
	monkeypatch,
) -> None:
	captured = {}

	class FakeStream:
		def __init__(self, lines):
			self._lines = iter(lines)

		def readline(self):
			return next(self._lines, "")

		def close(self):
			pass

	class FakeStdin:
		def write(self, data):
			captured["input"] = data

		def flush(self):
			pass

		def close(self):
			pass

	class FakeProcess:
		def __init__(self):
			self.stdin = FakeStdin()
			self.stdout = FakeStream(['{"jsonrpc":"2.0","id":2,"result":{}}\n'])
			self.stderr = FakeStream(["secret\n"])
			self.returncode = None

		def poll(self):
			return self.returncode

		def terminate(self):
			self.returncode = 0

		def wait(self, timeout=None):
			self.returncode = 0
			return self.returncode

		def kill(self):
			self.returncode = -9

	def fake_popen(args, **kwargs):
		captured["args"] = args
		captured["kwargs"] = kwargs
		return FakeProcess()

	monkeypatch.setattr(runio.subprocess, "Popen", fake_popen)

	result = runio.run_sonarqube_mcp(
		["podman", "run", "-i", "--rm", "mcp/sonarqube"],
		'{"jsonrpc":"2.0"}\n',
		{"SONARQUBE_TOKEN": "secret"},
		timeout_s=4.5,
	)

	assert result == (
		0,
		'{"jsonrpc":"2.0","id":2,"result":{}}\n\nsecret\n',
	)
	assert captured["args"][0:2] == ["podman", "run"]
	assert captured["kwargs"]["env"]["SONARQUBE_TOKEN"] == "secret"
	assert captured["kwargs"]["stdin"] is subprocess.PIPE
	assert captured["kwargs"]["stdout"] is subprocess.PIPE
	assert captured["kwargs"]["stderr"] is subprocess.PIPE
	assert captured["kwargs"]["text"] is True
	assert captured["input"] == '{"jsonrpc":"2.0"}\n'


def test_run_sonarqube_mcp_times_out_and_stops_server(monkeypatch) -> None:
	class FakeStream:
		def readline(self):
			return ""

		def close(self):
			pass

	class FakeStdin:
		def write(self, data):
			pass

		def flush(self):
			pass

		def close(self):
			pass

	class FakeProcess:
		def __init__(self):
			self.stdin = FakeStdin()
			self.stdout = FakeStream()
			self.stderr = FakeStream()
			self.returncode = None
			self.terminated = False

		def poll(self):
			return self.returncode

		def terminate(self):
			self.terminated = True
			self.returncode = 0

		def wait(self, timeout=None):
			self.returncode = 0
			return self.returncode

		def kill(self):
			self.returncode = -9

	process = FakeProcess()
	monkeypatch.setattr(runio.subprocess, "Popen", lambda *args, **kwargs: process)
	monkeypatch.setattr(runio, "_cleanup_mcp_container", lambda command: None)

	result = runio.run_sonarqube_mcp(
		["podman", "run", "--name", "quack-test", "mcp/sonarqube"],
		'{"jsonrpc":"2.0"}\n',
		{},
		timeout_s=0.01,
	)

	assert result == (-1, "")
	assert process.terminated is True


def test_mcp_output_preserves_responses_when_stderr_is_verbose() -> None:
	response = '{"jsonrpc":"2.0","id":2,"result":{}}\n'

	output = runio._merge_mcp_output([response], ["diagnostic\n" * 30000])

	assert response in output
	assert len(output) <= 24000
