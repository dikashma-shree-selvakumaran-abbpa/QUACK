"""Unit tests for the quack.providers.copilot_sdk provider.

These tests never hit the network: the async Copilot SDK client is
replaced with in-memory fakes via monkeypatch.
"""

from __future__ import annotations

import asyncio
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


def test_chat_raises_llm_unavailable():
	with pytest.raises(LLMUnavailable):
		copilot_sdk.chat(
			[{"role": "user", "content": "hi"}], "claude-haiku-4.5"
		)
