"""Thin test-runner subprocess adapter.

This is one of the few modules permitted to call subprocess directly. It knows
how to invoke exactly four whitelisted command shapes and nothing else:

* ``pytest <paths> -x --tb=short -q``
* ``dotnet test <project> --no-build --filter "<filter>" -v minimal``
* ``sonar-scanner <validated -D properties>``
* ``podman run -i --rm ... mcp/sonarqube`` over stdin/stdout

The argument lists are always built here from already-validated inputs. No
shell is ever used (``shell=False``), so nothing constructible from tool input
can inject additional commands. Every failure mode is normalised to an
``(exit_code, output)`` tuple; this module never raises for a runner error.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 180
SONAR_TIMEOUT_S = 60
MCP_TIMEOUT_S = 90.0
_MCP_OUTPUT_LIMIT = 24000
_MCP_RESPONSE_LIMIT = 128000
_MCP_CONTAINER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def run_pytest(
	paths: list[str], timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[int, str]:
	"""Run ``pytest <paths> -x --tb=short -q`` and return (exit_code, output)."""
	return _run(["pytest", *paths, "-x", "--tb=short", "-q"], timeout_s)


def run_dotnet_test(
	project: str, test_filter: str | None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[int, str]:
	"""Run ``dotnet test <project> --no-build [--filter ...] -v minimal``."""
	args = ["dotnet", "test", project, "--no-build"]
	if test_filter:
		args += ["--filter", test_filter]
	args += ["-v", "minimal"]
	return _run(args, timeout_s)


def run_sonar_scanner(
	scanner: str | Path,
	properties: list[str],
	cwd: str | Path,
	timeout_s: int = SONAR_TIMEOUT_S,
) -> tuple[int, str]:
	"""Run a validated SonarScanner argument list without a shell."""
	return _run([str(scanner), *properties], timeout_s, cwd=str(cwd))


def run_sonarqube_mcp(
	command: list[str],
	input_data: str,
	env: dict[str, str],
	timeout_s: float = MCP_TIMEOUT_S,
	expected_id: int = 2,
) -> tuple[int, str]:
	"""Run one MCP request and stop after its JSON-RPC response arrives.

	MCP stdio servers stay alive waiting for more requests, so waiting for the
	process to exit would turn every successful response into a timeout.
	"""
	try:
		process = subprocess.Popen(
			command,
			env=env,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
	except FileNotFoundError:
		return (127, "")
	except OSError:
		return (126, "")

	events: queue.Queue[tuple[str, str | None]] = queue.Queue()
	threads = [
		threading.Thread(
			target=_read_mcp_stream,
			args=(process.stdout, "stdout", events),
			daemon=True,
		),
		threading.Thread(
			target=_read_mcp_stream,
			args=(process.stderr, "stderr", events),
			daemon=True,
		),
	]
	for thread in threads:
		thread.start()

	stdout_lines: list[str] = []
	stderr_lines: list[str] = []
	response_lines: list[str] = []
	stdout_closed = False
	stderr_closed = False
	exit_code = 0
	try:
		if process.stdin is None:
			exit_code = 126
		else:
			deadline = time.monotonic() + max(timeout_s, 0.0)
			response_received = False

			def wait_for_response(response_id: object) -> bool:
				nonlocal stdout_closed, stderr_closed
				while True:
					remaining = deadline - time.monotonic()
					if remaining <= 0:
						return False
					try:
						stream, line = events.get(timeout=min(remaining, 0.25))
					except queue.Empty:
						if process.poll() is not None and stdout_closed and stderr_closed:
							return False
						continue

					if line is None:
						if stream == "stdout":
							stdout_closed = True
						else:
							stderr_closed = True
						if process.poll() is not None and stdout_closed and stderr_closed:
							return False
						continue

					if stream == "stdout":
						if _is_jsonrpc_response(line):
							_append_bounded(
								response_lines,
								line,
								limit=_MCP_RESPONSE_LIMIT,
							)
						else:
							_append_bounded(stdout_lines, line)
						if _is_expected_response(line, response_id):
							return True
					else:
						_append_bounded(stderr_lines, line)

			for line in input_data.splitlines(keepends=True):
				if not line.strip():
					continue
				try:
					payload = json.loads(line)
				except (TypeError, ValueError):
					payload = None
				try:
					process.stdin.write(line)
					process.stdin.flush()
				except (BrokenPipeError, OSError):
					exit_code = 126
					_stop_process(process)
					break

				if isinstance(payload, dict) and "id" in payload:
					response_id = payload["id"]
					response_received = wait_for_response(response_id)
					if not response_received:
						exit_code = -1
						break

			if exit_code == 0 and not response_received:
				response_received = wait_for_response(expected_id)
				if not response_received:
					exit_code = -1

			try:
				process.stdin.close()
			except (BrokenPipeError, OSError):
				pass

			if exit_code == 0:
				exit_code = process.poll() if process.poll() is not None else 0
	finally:
		_stop_process(process)
		for thread in threads:
			thread.join(timeout=1.0)
		_drain_mcp_events(
			events,
			stdout_lines,
			stderr_lines,
			response_lines,
		)
		_cleanup_mcp_container(command)
	return exit_code, _merge_mcp_output(
		stdout_lines,
		stderr_lines,
		response_lines,
	)


def _read_mcp_stream(stream, name: str, events) -> None:
	"""Forward one MCP pipe to the bounded event queue."""
	if stream is None:
		events.put((name, None))
		return
	try:
		for line in iter(stream.readline, ""):
			events.put((name, line))
	finally:
		stream.close()
		events.put((name, None))


def _append_bounded(
	lines: list[str],
	line: str,
	*,
	limit: int = _MCP_OUTPUT_LIMIT,
) -> None:
	"""Keep enough subprocess output for diagnostics without unbounded growth."""
	used = sum(len(item) for item in lines)
	if used < limit:
		lines.append(line[: limit - used])


def _drain_mcp_events(
	events: queue.Queue[tuple[str, str | None]],
	stdout_lines: list[str],
	stderr_lines: list[str],
	response_lines: list[str],
) -> None:
	"""Collect pipe lines that arrived while the response loop was stopping."""
	while True:
		try:
			stream, line = events.get_nowait()
		except queue.Empty:
			return
		if line is None:
			continue
		if stream == "stdout" and _is_jsonrpc_response(line):
			_append_bounded(
				response_lines,
				line,
				limit=_MCP_RESPONSE_LIMIT,
			)
		else:
			_append_bounded(
				stdout_lines if stream == "stdout" else stderr_lines,
				line,
			)


def _merge_mcp_output(
	stdout_lines: list[str],
	stderr_lines: list[str],
	response_lines: list[str] | None = None,
) -> str:
	"""Return bounded output while preserving MCP responses ahead of diagnostics."""
	responses = "".join(response_lines or [])
	output = "".join(stdout_lines)
	stderr = "".join(stderr_lines)
	if len(output) > _MCP_OUTPUT_LIMIT:
		output = output[-_MCP_OUTPUT_LIMIT:]
	remaining = _MCP_OUTPUT_LIMIT - len(output)
	if stderr and remaining > 1:
		output += ("\n" if output else "") + stderr[-(remaining - 1) :]
	if responses and output:
		return responses + "\n" + output
	return responses or output


def _is_jsonrpc_response(line: str) -> bool:
	"""Return whether a line contains any JSON-RPC response."""
	try:
		payload = json.loads(line)
	except (TypeError, ValueError):
		return False
	return (
		isinstance(payload, dict)
		and "id" in payload
		and ("result" in payload or "error" in payload)
	)


def _is_expected_response(line: str, expected_id: object) -> bool:
	"""Return whether a line is the requested JSON-RPC response."""
	if not _is_jsonrpc_response(line):
		return False
	payload = json.loads(line)
	return isinstance(payload, dict) and payload.get("id") == expected_id


def _stop_process(process: subprocess.Popen) -> None:
	"""Stop the local Podman client without waiting on the MCP server forever."""
	if process.poll() is not None:
		return
	try:
		process.terminate()
		process.wait(timeout=2)
	except subprocess.TimeoutExpired:
		try:
			process.kill()
			process.wait(timeout=2)
		except (OSError, subprocess.TimeoutExpired):
			pass
	except OSError:
		pass


def _cleanup_mcp_container(command: list[str]) -> None:
	"""Force-remove a named container left behind by a timed-out Podman call."""
	name = None
	for index, argument in enumerate(command[:-1]):
		if argument == "--name":
			name = command[index + 1]
			break
	if not name or not _MCP_CONTAINER_RE.fullmatch(name):
		return
	try:
		subprocess.run(
			[command[0], "rm", "-f", name],
			capture_output=True,
			text=True,
			timeout=5,
			check=False,
		)
	except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
		pass


def _run(
	args: list[str], timeout_s: int, *, cwd: str | None = None
) -> tuple[int, str]:
	"""Execute a fixed argument list without a shell. Never raises."""
	try:
		result = subprocess.run(
			args,
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=timeout_s,
		)
	except FileNotFoundError:
		return (127, f"<runner not found: {args[0]}>")
	except subprocess.TimeoutExpired:
		return (-1, f"<timed out after {timeout_s}s>")
	except OSError:
		return (126, "<runner unavailable>")
	output = (result.stdout or "") + (result.stderr or "")
	return (result.returncode, output)
