"""Shared test safeguards."""

from __future__ import annotations

import pytest

from quack import llmio


_PROVIDER_CALL_ERROR = (
	"unmocked network call in test: mock quack.llmio.complete/chat "
	"or explicitly opt in with @pytest.mark.allow_provider_calls"
)


def pytest_configure(config):
	config.addinivalue_line(
		"markers",
		"allow_provider_calls: explicitly allow high-level provider seam tests",
	)


@pytest.fixture(autouse=True)
def _block_unmocked_provider_calls(request, monkeypatch, tmp_path):
	"""Prevent tests from reaching a real provider unless explicitly opted in."""
	monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
	monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
	if request.node.get_closest_marker("allow_provider_calls") is not None:
		yield
		return

	def fail_complete(*args, **kwargs):
		raise AssertionError(_PROVIDER_CALL_ERROR)

	def fail_chat(*args, **kwargs):
		raise AssertionError(_PROVIDER_CALL_ERROR)

	monkeypatch.setattr(llmio, "complete", fail_complete)
	monkeypatch.setattr(llmio, "chat", fail_chat)
	yield
