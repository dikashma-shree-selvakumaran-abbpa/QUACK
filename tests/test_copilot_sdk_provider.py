"""Unit tests for the quack.providers.copilot_sdk provider.

These tests never hit the network: the async Copilot SDK client is
replaced with in-memory fakes via monkeypatch.
"""

from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace

import pytest

from quack.llmio import LLMUnavailable
from quack.providers import copilot_sdk


def _resp(content):
	"""Build a fake SessionEvent-shaped response object."""
	return SimpleNamespace(data=SimpleNamespace(content=content))


class _FakeSession:
	def __init__(self, resp=None, delay=0.0):
		self._resp = resp
		self._delay = delay

	async def send_and_wait(self, prompt):
		if self._delay:
			await asyncio.sleep(self._delay)
		return self._resp


class _FakeClient:
	"""Records lifecycle calls and returns a preconfigured session."""

	def __init__(self, resp=None, delay=0.0, start_error=None):
		self._resp = resp
		self._delay = delay
		self._start_error = start_error
		self.stopped = False

	async def start(self):
		if self._start_error is not None:
			raise self._start_error

	async def create_session(self, model):
		assert model == "claude-haiku-4.5"
		return _FakeSession(self._resp, self._delay)

	async def stop(self):
		self.stopped = True


class _FakeModelClient(_FakeClient):
	def __init__(self, models=None, list_error=None):
		super().__init__()
		self._models = models or []
		self._list_error = list_error

	async def list_models(self):
		if self._list_error is not None:
			raise self._list_error
		return self._models


class _LoggingSession:
	async def send_and_wait(self, prompt):
		try:
			raise RuntimeError("session authorization failed")
		except RuntimeError:
			logging.getLogger("copilot.session").exception(
				"CopilotSession.send_and_wait failed"
			)
			raise


class _LoggingClient(_FakeClient):
	async def create_session(self, model):
		return _LoggingSession()

	async def list_models(self):
		try:
			raise RuntimeError("401: token lacks Copilot Requests permission")
		except RuntimeError:
			logging.getLogger("copilot._jsonrpc").exception("JsonRpcError")
			raise


def _install_client(monkeypatch, client):
	monkeypatch.setattr(copilot_sdk, "CopilotClient", lambda: client)
	return client


def test_complete_returns_text(monkeypatch):
	client = _install_client(monkeypatch, _FakeClient(resp=_resp("hello")))
	messages = [
		{"role": "system", "content": "reply with only JSON"},
		{"role": "user", "content": "hi"},
	]
	assert copilot_sdk.complete(messages, "claude-haiku-4.5") == "hello"
	assert client.stopped is True


def test_auth_failure_raises_llm_unavailable(monkeypatch):
	err = RuntimeError("authorization failed")
	_install_client(monkeypatch, _FakeClient(start_error=err))
	with pytest.raises(LLMUnavailable):
		copilot_sdk.complete(
			[{"role": "user", "content": "hi"}], "claude-haiku-4.5"
		)


def test_timeout_raises_llm_unavailable(monkeypatch):
	client = _install_client(
		monkeypatch, _FakeClient(resp=_resp("late"), delay=1.0)
	)
	with pytest.raises(LLMUnavailable):
		copilot_sdk.complete(
			[{"role": "user", "content": "hi"}],
			"claude-haiku-4.5",
			timeout_s=0.01,
		)
	assert client.stopped is True


def test_none_response_raises_llm_unavailable(monkeypatch):
	client = _install_client(monkeypatch, _FakeClient(resp=None))
	with pytest.raises(LLMUnavailable):
		copilot_sdk.complete(
			[{"role": "user", "content": "hi"}], "claude-haiku-4.5"
		)
	assert client.stopped is True


def test_list_models_returns_normalized_ids_and_stops_client(monkeypatch):
	models = [
		SimpleNamespace(id="model-a", name="A"),
		{"id": "model-b"},
		"model-c",
	]
	client = _install_client(monkeypatch, _FakeModelClient(models=models))

	assert copilot_sdk.list_models() == ["model-a", "model-b", "model-c"]
	assert client.stopped is True


def test_list_models_failure_is_normalized_and_stops_client(monkeypatch):
	client = _install_client(
		monkeypatch, _FakeModelClient(list_error=RuntimeError("authorization failed"))
	)

	with pytest.raises(LLMUnavailable) as excinfo:
		copilot_sdk.list_models()

	assert "model list unavailable" in excinfo.value.reason
	assert client.stopped is True


@pytest.mark.parametrize(
	("operation", "logger_name", "leaked_message"),
	[
		("list_models", "copilot._jsonrpc", "JsonRpcError"),
		("complete", "copilot.session", "CopilotSession.send_and_wait failed"),
	],
)
def test_sdk_error_logging_is_suppressed_and_restored(
	monkeypatch, operation, logger_name, leaked_message
):
	client = _install_client(monkeypatch, _LoggingClient())
	logger = logging.getLogger(logger_name)
	stream = io.StringIO()
	handler = logging.StreamHandler(stream)
	state = (logger.level, logger.propagate, logger.disabled)
	logger.addHandler(handler)
	logger.setLevel(logging.ERROR)
	logger.propagate = False
	logger.disabled = False
	try:
		with pytest.raises(LLMUnavailable):
			if operation == "list_models":
				copilot_sdk.list_models()
			else:
				copilot_sdk.complete(
					[{"role": "user", "content": "hi"}], "claude-haiku-4.5"
				)

		assert stream.getvalue() == ""
		assert logger.disabled is False
		logger.error("logging restored")
		assert "logging restored" in stream.getvalue()
		assert leaked_message not in stream.getvalue()
		assert client.stopped is True
	finally:
		logger.removeHandler(handler)
		logger.level, logger.propagate, logger.disabled = state


def test_chat_raises_llm_unavailable():
	with pytest.raises(LLMUnavailable):
		copilot_sdk.chat(
			[{"role": "user", "content": "hi"}], "claude-haiku-4.5"
		)


def test_check_availability_ignores_github_token(monkeypatch):
	# copilot_sdk authenticates via the Copilot CLI's stored OAuth login,
	# never via GITHUB_TOKEN, so availability must not depend on it.
	monkeypatch.setattr(copilot_sdk, "CopilotClient", object())
	monkeypatch.delenv("GITHUB_TOKEN", raising=False)
	assert copilot_sdk.check_availability() is None
	monkeypatch.setenv("GITHUB_TOKEN", "irrelevant")
	assert copilot_sdk.check_availability() is None


def test_check_availability_reports_missing_sdk(monkeypatch):
	monkeypatch.setattr(copilot_sdk, "CopilotClient", None)
	reason = copilot_sdk.check_availability()
	assert reason is not None
	assert "not installed" in reason


def test_declares_slow_default_timeout():
	# The SDK starts a local runtime (~9s) before inference, so the transport
	# declares a much larger timeout than the fast HTTP provider.
	assert copilot_sdk.DEFAULT_TIMEOUT_S == 60.0


def test_declares_copilot_default_models():
	# The Copilot SDK uses its own model naming, NOT GitHub Models' ids.
	# Split by use: cheaper haiku for single-shot review, stronger sonnet for
	# the agent's multi-step tool-using investigation.
	assert copilot_sdk.DEFAULT_COMPLETION_MODEL == "claude-haiku-4.5"
	assert copilot_sdk.DEFAULT_AGENT_MODEL == "claude-sonnet-4.5"

