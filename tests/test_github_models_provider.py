"""Unit tests for the quack.providers.github_models provider."""

from __future__ import annotations

from quack.providers import github_models


def test_check_availability_reports_missing_token(monkeypatch):
	monkeypatch.delenv("GITHUB_TOKEN", raising=False)
	reason = github_models.check_availability()
	assert reason == "no GITHUB_TOKEN"


def test_check_availability_ok_when_token_set(monkeypatch):
	monkeypatch.setenv("GITHUB_TOKEN", "t")
	assert github_models.check_availability() is None


def test_declares_fast_default_timeout():
	# A GitHub Models HTTP call is fast; the transport declares a short bound.
	assert github_models.DEFAULT_TIMEOUT_S == 6.0


def test_declares_github_models_default_models():
	# Split by use: single-shot review tolerates a cheap model; the agent's
	# multi-step tool-using investigation needs a stronger one.
	assert github_models.DEFAULT_COMPLETION_MODEL == "openai/gpt-4o-mini"
	assert github_models.DEFAULT_AGENT_MODEL == "openai/gpt-4.1"

