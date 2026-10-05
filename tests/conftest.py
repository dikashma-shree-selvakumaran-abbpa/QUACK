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
def _isolate_pre_commit_env(monkeypatch):
	"""Clear ambient pre-commit environment variables for all tests."""
	monkeypatch.delenv("PRE_COMMIT", raising=False)
	monkeypatch.delenv("PRE_COMMIT_FROM_REF", raising=False)
	monkeypatch.delenv("PRE_COMMIT_TO_REF", raising=False)
	monkeypatch.delenv("PRE_COMMIT_SOURCE", raising=False)
	monkeypatch.delenv("PRE_COMMIT_ORIGIN", raising=False)
	yield


@pytest.fixture(autouse=True)
def _isolate_sonar_env(monkeypatch):
	"""Clear ambient Sonar environment variables for all tests."""
	monkeypatch.delenv("QUACK_SONAR_AT_COMMIT", raising=False)
	monkeypatch.delenv("QUACK_SONAR_BRANCH", raising=False)
	monkeypatch.delenv("SONARQUBE_BRANCH", raising=False)
	monkeypatch.delenv("SONAR_BRANCH", raising=False)
	monkeypatch.delenv("QUACK_SONAR_PROJECT_KEY", raising=False)
	monkeypatch.delenv("SONARQUBE_PROJECT_KEY", raising=False)
	monkeypatch.delenv("SONAR_PROJECT_KEY", raising=False)
	monkeypatch.delenv("QUACK_SONAR_SOURCES", raising=False)
	monkeypatch.delenv("SONAR_SOURCES", raising=False)
	monkeypatch.delenv("QUACK_SONAR_TESTS", raising=False)
	monkeypatch.delenv("SONAR_TESTS", raising=False)
	monkeypatch.delenv("QUACK_SONAR_TIMEOUT_S", raising=False)
	monkeypatch.delenv("QUACK_SONAR_SCANNER", raising=False)
	monkeypatch.delenv("QUACK_TOOLS_DIR", raising=False)
	monkeypatch.delenv("SONAR_TOKEN", raising=False)
	yield


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

	monkeypatch.setattr(llmio, "complete", fail_complete)
	yield
