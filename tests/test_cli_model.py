"""Tests for the read-only `quack model` diagnostic."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from quack import cli
from quack.llmio import LLMUnavailable


@pytest.fixture(autouse=True)
def _diagnostic_stubs(monkeypatch):
	for name in (
		"QUACK_PROVIDER",
		"QUACK_MODEL",
		"GITHUB_TOKEN",
		"GH_TOKEN",
		"COPILOT_GITHUB_TOKEN",
	):
		monkeypatch.delenv(name, raising=False)
	monkeypatch.setattr(cli.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(cli.llmio, "default_timeout", lambda: 60.0)
	monkeypatch.setattr(
		cli.llmio,
		"default_model",
		lambda kind="completion": {
			"completion": "default-completion",
			"agent": "default-agent",
		}[kind],
	)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["model-a", "model-b"])


def _invoke(*args: str):
	return CliRunner().invoke(cli.main, ["model", *args])


def test_reports_default_provider_when_env_is_unset():
	result = _invoke()

	assert result.exit_code == 0
	assert "Provider: copilot_sdk" in result.output
	assert "QUACK_PROVIDER unset; default is copilot_sdk" in result.output


def test_reports_provider_selected_by_env(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "github_models")

	result = _invoke()

	assert result.exit_code == 0
	assert "Provider: github_models" in result.output
	assert "selected by QUACK_PROVIDER environment variable" in result.output


def test_reports_availability_reason_and_suggested_fix(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "github_models")
	monkeypatch.setattr(cli.llmio, "availability_error", lambda: "no GITHUB_TOKEN")

	result = _invoke()

	assert result.exit_code == 0
	assert "Auth status: problem - no GITHUB_TOKEN" in result.output
	assert "Suggested fix: set GITHUB_TOKEN with models:read permission" in result.output


@pytest.mark.parametrize("token_name", ["GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"])
def test_warns_about_token_shadowing_only_for_copilot(monkeypatch, token_name):
	monkeypatch.setenv(token_name, "secret-value")

	copilot_result = _invoke()
	monkeypatch.setenv("QUACK_PROVIDER", "github_models")
	github_models_result = _invoke()

	assert copilot_result.exit_code == 0
	assert f"{token_name} is set (length 12)" in copilot_result.output
	assert "SHADOWS the Copilot CLI's stored login" in copilot_result.output
	assert "secret-value" not in copilot_result.output
	assert github_models_result.exit_code == 0
	assert "SHADOWS" not in github_models_result.output


def test_does_not_warn_when_no_ambient_token_is_set():
	result = _invoke()

	assert result.exit_code == 0
	assert "SHADOWS" not in result.output


def test_resolves_and_displays_both_provider_default_models():
	result = _invoke()

	assert result.exit_code == 0
	assert "Completion model: default-completion (source: provider default)" in result.output
	assert "Agent model: default-agent (source: provider default)" in result.output


def test_model_resolution_precedence(monkeypatch):
	monkeypatch.setenv("QUACK_MODEL", "env-model")
	from_env = _invoke()
	from_cli = _invoke("--model", "cli-model")

	assert "Completion model: env-model (source: QUACK_MODEL)" in from_env.output
	assert "Agent model: env-model (source: QUACK_MODEL)" in from_env.output
	assert "Completion model: cli-model (source: --model)" in from_cli.output
	assert "Agent model: cli-model (source: --model)" in from_cli.output
	assert from_env.exit_code == from_cli.exit_code == 0


def test_list_models_failure_is_fail_open(monkeypatch):
	def fail():
		raise LLMUnavailable(
			"model list unavailable: 401: token lacks Copilot Requests permission"
		)

	monkeypatch.setattr(cli.llmio, "list_models", fail)

	result = _invoke()

	assert result.exit_code == 0
	assert (
		"Reachable models unavailable: "
		"401: token lacks Copilot Requests permission"
	) in result.output
	assert "Traceback" not in result.output


def test_list_models_reason_is_one_line_truncated_and_token_redacted(monkeypatch):
	secret = "ambient-secret"
	monkeypatch.setenv("GITHUB_TOKEN", secret)

	def fail():
		raise RuntimeError(f"denied for {secret}\n" + "x" * 300)

	monkeypatch.setattr(cli.llmio, "list_models", fail)

	result = _invoke()

	assert result.exit_code == 0
	assert secret not in result.output
	assert "denied for [REDACTED] x" in result.output
	assert "..." in result.output


def test_caps_reachable_models_at_fifteen(monkeypatch):
	monkeypatch.setattr(cli.llmio, "list_models", lambda: [f"model-{i}" for i in range(20)])

	result = _invoke()

	assert result.exit_code == 0
	assert "model-14" in result.output
	assert "model-15" not in result.output
	assert "Showing first 15 of 20 models" in result.output


def test_unknown_provider_and_diagnostic_exception_still_exit_zero(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "unknown")
	monkeypatch.setattr(
		cli.llmio, "availability_error", lambda: "unknown provider: unknown"
	)
	unknown = _invoke()

	def explode():
		raise RuntimeError("private provider detail")

	monkeypatch.setattr(cli.llmio, "availability_error", explode)
	exploded = _invoke()

	assert unknown.exit_code == 0
	assert "Provider resolution: unavailable" in unknown.output
	assert exploded.exit_code == 0
	assert "diagnostic unavailable (RuntimeError)" in exploded.output
	assert "private provider detail" not in exploded.output
