"""Unit tests for the quack.llmio provider seam (dispatcher)."""

from __future__ import annotations

import sys
import types

import pytest

from quack import llmio
from quack.llmio import LLMUnavailable

pytestmark = pytest.mark.allow_provider_calls


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


def _stub_provider(name, complete_fn=None):
	"""Install a fake provider module at quack.providers.<name>."""
	module = types.ModuleType(f"quack.providers.{name}")
	if complete_fn is not None:
		module.complete = complete_fn
	sys.modules[f"quack.providers.{name}"] = module
	return module


def test_defaults_to_copilot_sdk_when_unset(monkeypatch):
	# copilot_sdk is the approved (default) transport; others are opt-in.
	monkeypatch.delenv("QUACK_PROVIDER", raising=False)
	captured = {}

	def fake_complete(messages, model, timeout_s=6.0):
		captured["provider"] = "copilot_sdk"
		return "ok"

	_stub_provider("copilot_sdk", complete_fn=fake_complete)
	assert llmio.complete([], "m") == "ok"
	assert captured["provider"] == "copilot_sdk"


def test_unknown_provider_raises_llmunavailable(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "does_not_exist")
	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.complete([], "m")
	assert "unknown provider" in excinfo.value.reason


def test_provider_arbitrary_exception_surfaces_as_llmunavailable(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")

	def boom_complete(messages, model, timeout_s=6.0):
		raise RuntimeError("kaboom")

	_stub_provider("copilot_sdk", complete_fn=boom_complete)

	with pytest.raises(LLMUnavailable):
		llmio.complete([], "m")


def test_provider_llmunavailable_passes_through_unchanged(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")

	def fail_complete(messages, model, timeout_s=6.0):
		raise LLMUnavailable("specific reason")

	_stub_provider("copilot_sdk", complete_fn=fail_complete)
	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.complete([], "m")
	assert excinfo.value.reason == "specific reason"


def test_availability_error_delegates_to_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	_stub_provider("copilot_sdk").check_availability = lambda: "no login"
	assert llmio.availability_error() == "no login"


def test_availability_error_none_when_provider_available(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	_stub_provider("copilot_sdk").check_availability = lambda: None
	assert llmio.availability_error() is None


def test_availability_error_normalizes_unknown_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "does_not_exist")
	reason = llmio.availability_error()
	assert reason is not None
	assert "unknown provider" in reason


def test_availability_error_none_when_provider_lacks_check(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	# A provider without check_availability is treated as available.
	_stub_provider("copilot_sdk")
	assert llmio.availability_error() is None


def test_default_timeout_reflects_selected_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	stub = _stub_provider("copilot_sdk")
	stub.DEFAULT_TIMEOUT_S = 60.0
	assert llmio.default_timeout() == 60.0

	monkeypatch.setenv("QUACK_PROVIDER", "not_a_provider")
	stub2 = _stub_provider("not_a_provider")
	stub2.DEFAULT_TIMEOUT_S = 6.0
	assert llmio.default_timeout() == 6.0


def test_default_timeout_falls_back_on_unknown_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "does_not_exist")
	assert llmio.default_timeout() == 6.0


def test_default_model_reflects_selected_provider_and_kind(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	stub = _stub_provider("copilot_sdk")
	stub.DEFAULT_COMPLETION_MODEL = "claude-haiku-4.5"
	stub.DEFAULT_AGENT_MODEL = "claude-sonnet-4.5"
	assert llmio.default_model() == "claude-haiku-4.5"
	assert llmio.default_model(kind="completion") == "claude-haiku-4.5"
	assert llmio.default_model(kind="agent") == "claude-sonnet-4.5"


def test_default_model_none_on_unknown_kind(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "not_a_provider")
	stub = _stub_provider("not_a_provider")
	stub.DEFAULT_COMPLETION_MODEL = "openai/gpt-4o-mini"
	stub.DEFAULT_AGENT_MODEL = "openai/gpt-4.1"
	assert llmio.default_model(kind="nonsense") is None


def test_list_models_delegates_to_selected_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	_stub_provider("copilot_sdk").list_models = lambda: ["model-a", "model-b"]

	assert llmio.list_models() == ["model-a", "model-b"]


def test_list_models_reports_unavailable_when_provider_lacks_method(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "copilot_sdk")
	_stub_provider("copilot_sdk")

	with pytest.raises(LLMUnavailable) as excinfo:
		llmio.list_models()

	assert excinfo.value.reason == "model list unavailable"


def test_default_model_none_on_unknown_provider(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "does_not_exist")
	assert llmio.default_model(kind="completion") is None
	assert llmio.default_model(kind="agent") is None


