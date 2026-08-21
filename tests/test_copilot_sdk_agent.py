"""No-network tests for the Copilot SDK-native agent path."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from quack import agent, render
from quack.llmio import LLMUnavailable
from quack.providers import copilot_sdk


_VALID_RESULT = {
	"summary": "success",
	"tests_run": [],
	"failures": [],
	"proposed_patch": None,
	"proposed_new_tests": None,
}


class _NativeSession:
	def __init__(self, actions=(), response=None, logger_name=None):
		self.actions = list(actions)
		self.response = response or _VALID_RESULT
		self.logger_name = logger_name
		self.prompt = None
		self.tools = []
		self.hooks = None
		self.denied = []

	async def send_and_wait(self, prompt):
		self.prompt = prompt
		if self.logger_name:
			logging.getLogger(self.logger_name).error("sdk session log")
		for name, args in self.actions:
			decision = self.hooks["on_pre_tool_use"](
				SimpleNamespace(toolName=name, toolArgs=args)
			)
			if decision["permissionDecision"] != "allow":
				self.denied.append((name, decision["permissionDecisionReason"]))
				continue
			tool = next(tool for tool in self.tools if tool.name == name)
			await tool.handler(SimpleNamespace(arguments=args))
		return SimpleNamespace(
			data=SimpleNamespace(
				content=__import__("json").dumps(self.response)
			)
		)


class _ToolResult:
	def __init__(self, text_result_for_llm="", result_type="success", error=None):
		self.text_result_for_llm = text_result_for_llm
		self.result_type = result_type
		self.error = error


class _Tool:
	def __init__(self, name, handler):
		self.name = name
		self.handler = handler
		self.parameters = None


def _define_tool(*, name, handler, **_kwargs):
	async def wrapped(invocation):
		return await _maybe_await(handler(invocation.arguments))

	return _Tool(name, wrapped)


async def _maybe_await(value):
	if hasattr(value, "__await__"):
		return await value
	return value


class _NativeClient:
	def __init__(self, session, start_error=None, prints=False):
		self.session = session
		self.start_error = start_error
		self.prints = prints
		self.stopped = False

	async def start(self):
		if self.prints:
			print("sdk stdout")
			print("sdk stderr", file=sys.stderr)
		if self.start_error:
			raise self.start_error

	async def create_session(self, **kwargs):
		self.session.tools = kwargs["tools"]
		self.session.hooks = kwargs["hooks"]
		return self.session

	async def stop(self):
		if self.prints:
			print("stop stdout")
			print("stop stderr", file=sys.stderr)
		self.stopped = True


def _install(monkeypatch, session, **kwargs):
	client = _NativeClient(session, **kwargs)
	monkeypatch.setattr(copilot_sdk, "CopilotClient", lambda: client)
	monkeypatch.setattr(copilot_sdk, "ToolResult", _ToolResult, raising=False)
	monkeypatch.setattr(copilot_sdk, "define_tool", _define_tool, raising=False)
	monkeypatch.setattr(copilot_sdk, "_runtime_needs_preparation", lambda: False)
	return client


def _run(monkeypatch, tmp_path, actions=(), response=None, **kwargs):
	session = _NativeSession(actions, response=response)
	client = _install(monkeypatch, session, **kwargs)
	result = copilot_sdk.run_agent("redacted diff", tmp_path, "model", timeout_s=5)
	return result, client, session


def test_read_file_outside_repo_returns_error_tool_result(monkeypatch, tmp_path):
	_, _, session = _run(monkeypatch, tmp_path, [("read_file", {"path": "../outside"})])
	assert session.denied == [("read_file", "path is outside the repository or invalid")]
	result = asyncio.run(
		session.tools[0].handler(SimpleNamespace(arguments={"path": "../outside"}))
	)
	assert result.result_type == "failure"
	assert "outside the repository" in result.text_result_for_llm


def test_list_dir_outside_repo_returns_error_tool_result(monkeypatch, tmp_path):
	_, _, session = _run(monkeypatch, tmp_path, [("list_dir", {"path": "../outside"})])
	assert session.denied == [("list_dir", "path is outside the repository or invalid")]
	result = asyncio.run(
		session.tools[1].handler(SimpleNamespace(arguments={"path": "../outside"}))
	)
	assert result.result_type == "failure"
	assert "outside the repository" in result.text_result_for_llm


def test_run_tests_rejects_non_whitelisted_target(monkeypatch, tmp_path):
	_, _, session = _run(
		monkeypatch,
		tmp_path,
		[("run_tests", {"project_or_paths": "run.sh"})],
	)
	assert session.denied and "unrecognized" in session.denied[0][1]


def test_run_tests_rejects_filter_shell_metacharacters(monkeypatch, tmp_path):
	_, _, session = _run(
		monkeypatch,
		tmp_path,
		[("run_tests", {"project_or_paths": 'Project.csproj --filter "x; rm -rf /"'})],
	)
	assert session.denied and "filter rejected" in session.denied[0][1]


def test_third_run_tests_invocation_is_denied_by_budget(monkeypatch, tmp_path):
	(tmp_path / "test_x.py").write_text("", encoding="utf-8")
	actions = [("run_tests", {"project_or_paths": "test_x.py"})] * 3
	_, _, session = _run(monkeypatch, tmp_path, actions)
	assert len(session.denied) == 1
	assert "run_tests budget" in session.denied[0][1]


def test_invocations_beyond_max_iterations_are_denied(monkeypatch, tmp_path):
	actions = [("list_dir", {"path": "."})] * (agent.MAX_ITERATIONS + 1)
	_, _, session = _run(monkeypatch, tmp_path, actions)
	assert len(session.denied) == 1
	assert "iteration budget" in session.denied[0][1]


def test_nonzero_test_output_reconciles_success_to_failure(monkeypatch, tmp_path):
	(tmp_path / "test_x.py").write_text("", encoding="utf-8")
	monkeypatch.setattr(agent.runio, "run_pytest", lambda *a, **k: (1, "FAILED test_x.py::test_bad"))
	result, _, _ = _run(
		monkeypatch,
		tmp_path,
		[("run_tests", {"project_or_paths": "test_x.py"})],
	)
	assert result.failures
	assert "verified" in result.summary


def test_sdk_output_and_logging_are_suppressed(monkeypatch, tmp_path, capsys):
	logger = logging.getLogger("copilot.native-agent")
	stream = io.StringIO()
	handler = logging.StreamHandler(stream)
	logger.addHandler(handler)
	logger.setLevel(logging.ERROR)
	try:
		_install(monkeypatch, _NativeSession(logger_name="copilot.native-agent"), prints=True)
		copilot_sdk.run_agent("diff", tmp_path, "model", timeout_s=5)
		assert capsys.readouterr() == ("", "")
		assert stream.getvalue() == ""
	finally:
		logger.removeHandler(handler)


def test_sdk_exception_normalizes_to_llm_unavailable(monkeypatch, tmp_path):
	with pytest.raises(LLMUnavailable):
		_install(monkeypatch, _NativeSession(), start_error=RuntimeError("sdk failed"))
		copilot_sdk.run_agent("diff", tmp_path, "model", timeout_s=5)


def test_redacted_diff_reaches_session_not_raw_diff(monkeypatch, tmp_path):
	session = _NativeSession()
	_install(monkeypatch, session)
	raw = "AWS_KEY=AKIA1234567890123456"
	redacted = "AWS_KEY=[REDACTED]"
	copilot_sdk.run_agent(redacted, tmp_path, "model", timeout_s=5)
	assert redacted in session.prompt
	assert raw not in session.prompt


def test_native_opening_prompt_does_not_stop_investigation(monkeypatch, tmp_path):
	session = _NativeSession()
	_install(monkeypatch, session)
	copilot_sdk.run_agent("diff", tmp_path, "model", timeout_s=5)
	assert "Stop investigating" not in session.prompt


def test_fly_reveals_patch_but_default_hides_it(monkeypatch, tmp_path, capsys):
	patch = "--- a/x.py\n+++ b/x.py\n"
	result, _, _ = _run(
		monkeypatch,
		tmp_path,
		response={**_VALID_RESULT, "proposed_patch": patch},
	)
	render.agent_report(result)
	assert patch not in capsys.readouterr().out
	render.agent_report(result, fly=True)
	output = capsys.readouterr().out
	assert "--- a/x.py" in output
	assert "+++ b/x.py" in output
