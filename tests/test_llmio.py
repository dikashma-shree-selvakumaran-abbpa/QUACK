"""Unit tests for the quack.llmio provider seam (dispatcher)."""

from __future__ import annotations

import sys
import types

import pytest

from quack import llmio
from quack.llmio import LLMUnavailable


@pytest.fixture(autouse=True)
def _restore_provider_modules():
	"""Snapshot provider modules so stubs never leak between tests."""
	prefix = "quack.providers."
	saved = {k: v for k, v in sys.modules.items() if k.startswith(prefix)}
	yield
	for key in [k for k in sys.modules if k.startswith(prefix)]:
		if key not in saved:
			del sys.modules[key]
	sys.modules.update(saved)


def _stub_provider(name, complete_fn=None, chat_fn=None):
	"""Install a fake provider module at quack.providers.<name>."""
	module = types.ModuleType(f"quack.providers.{name}")
	if complete_fn is not None:
		module.complete = complete_fn
	if chat_fn is not None:
		module.chat = chat_fn
	sys.modules[f"quack.providers.{name}"] = module
	return module


def test_defaults_to_github_models_when_unset(monkeypatch):
	monkeypatch.delenv("QUACK_PROVIDER", raising=False)
	captured = {}

	def fake_complete(messages, model, timeout_s=6.0):
		captured["provider"] = "github_models"
		return "ok"

	_stub_provider("github_models", complete_fn=fake_complete)
	assert llmio.complete([], "m") == "ok"
	assert captured["provider"] == "github_models"


def test_honors_quack_provider_env(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	captured = {}

	def fake_chat(messages, model, tools=None):
		captured["provider"] = "copilot_sdk"
		return {"role": "assistant", "content": "hi"}

	_stub_provider("copilot_sdk", chat_fn=fake_chat)
	assert llmio.chat([], "m") == {"role": "assistant", "content": "hi"}
	assert captured["provider"] == "copilot_sdk"


def test_unknown_provider_raises_llmunavailable(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "does_not_exist")
	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.complete([], "m")
	assert "unknown provider" in excinfo.value.reason
	with pytest.raises(LLMUnavailable):
		llmio.chat([], "m")


def test_provider_arbitrary_exception_surfaces_as_llmunavailable(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "github_models")

	def boom_complete(messages, model, timeout_s=6.0):
		raise RuntimeError("kaboom")

	def boom_chat(messages, model, tools=None):
		raise ValueError("nope")

	_stub_provider("github_models", complete_fn=boom_complete, chat_fn=boom_chat)

	with pytest.raises(LLMUnavailable):
		llmio.complete([], "m")
	with pytest.raises(LLMUnavailable):
		llmio.chat([], "m")


def test_provider_llmunavailable_passes_through_unchanged(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")

	def fail_complete(messages, model, timeout_s=6.0):
		raise LLMUnavailable("specific reason")

	_stub_provider("copilot_sdk", complete_fn=fail_complete)
	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.complete([], "m")
	assert excinfo.value.reason == "specific reason"


def test_copilot_sdk_chat_is_unavailable(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	# Ensure the real provider module is loaded, not a leftover fake.
	sys.modules.pop("quack.providers.copilot_sdk", None)
	# Tool calling is unsupported on copilot_sdk; it must fail-open.
	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.chat([], "m")
	assert excinfo.value.reason == (
		"tool calling not supported on copilot_sdk provider"
	)
