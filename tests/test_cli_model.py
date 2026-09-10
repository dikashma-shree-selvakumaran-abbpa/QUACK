"""Tests for the read-only `quack model` diagnostic."""

from __future__ import annotations

import json

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
	monkeypatch.setenv("QUACK_PROVIDER", "not_a_provider")

	result = _invoke()

	assert result.exit_code == 0
	assert "Provider: not_a_provider" in result.output
	assert "selected by QUACK_PROVIDER environment variable" in result.output


def test_reports_availability_reason_and_suggested_fix(monkeypatch):
	monkeypatch.setenv("QUACK_PROVIDER", "not_a_provider")
	monkeypatch.setattr(cli.llmio, "availability_error", lambda: "no GITHUB_TOKEN")

	result = _invoke()

	assert result.exit_code == 0
	assert "Auth status: problem - no GITHUB_TOKEN" in result.output
	assert "Suggested fix: set GITHUB_TOKEN with models:read permission" in result.output


@pytest.mark.parametrize("token_name", ["GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"])
def test_warns_about_token_shadowing_only_for_copilot(monkeypatch, token_name):
	monkeypatch.setenv(token_name, "secret-value")

	copilot_result = _invoke()
	monkeypatch.setenv("QUACK_PROVIDER", "not_a_provider")
	other_provider_result = _invoke()

	assert copilot_result.exit_code == 0
	assert f"{token_name} is set (length 12)" in copilot_result.output
	assert "SHADOWS the Copilot CLI's stored login" in copilot_result.output
	assert "secret-value" not in copilot_result.output
	assert other_provider_result.exit_code == 0
	assert "SHADOWS" not in other_provider_result.output


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


def test_warns_when_a_resolved_model_is_not_in_the_catalog():
	# claude-sonnet-4.5 aged out of the Copilot catalog and the agent silently
	# stopped investigating, because the failure lived inside a fail-open path.
	# `quack model` already knows both the defaults and the catalog, so it is
	# the right place to notice.
	result = _invoke()

	assert result.exit_code == 0
	assert "default-completion is NOT in the provider's reachable list" in result.output
	assert "default-agent is NOT in the provider's reachable list" in result.output


def test_no_warning_when_resolved_models_are_reachable(monkeypatch):
	monkeypatch.setattr(
		cli.llmio, "list_models", lambda: ["default-completion", "default-agent"]
	)

	result = _invoke()

	assert result.exit_code == 0
	assert "NOT in the provider's reachable list" not in result.output


def test_list_prints_only_model_ids():
	result = _invoke("--list")

	assert result.exit_code == 0
	assert "model-a" in result.output
	assert "model-b" in result.output
	assert "Provider:" not in result.output
	assert "Auth status:" not in result.output
	assert "Timeout:" not in result.output


def test_list_marks_the_current_defaults(monkeypatch):
	monkeypatch.setattr(
		cli.llmio,
		"list_models",
		lambda: ["model-a", "default-completion", "default-agent"],
	)

	result = _invoke("--list")

	assert result.exit_code == 0
	assert "(completion default)" in result.output
	assert "(agent default)" in result.output


def test_list_marks_one_model_for_both_kinds(monkeypatch):
	monkeypatch.setattr(cli.llmio, "default_model", lambda kind="completion": "shared")
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["model-a", "shared"])

	result = _invoke("--list")

	assert result.exit_code == 0
	shared_lines = [line for line in result.output.splitlines() if "shared" in line]
	assert len(shared_lines) == 1
	assert "completion default" in shared_lines[0]
	assert "agent default" in shared_lines[0]


def test_list_is_fail_open_when_catalog_unavailable(monkeypatch):
	def fail():
		raise LLMUnavailable("model list unavailable: 401: token lacks permission")

	monkeypatch.setattr(cli.llmio, "list_models", fail)

	result = _invoke("--list")

	assert result.exit_code == 0
	assert "model catalog unavailable" in result.output
	assert "401: token lacks permission" in result.output


def test_json_wins_over_list():
	result = _invoke("--json", "--list")

	assert result.exit_code == 0
	payload = json.loads(result.output)
	assert payload["schemaVersion"] == 1
