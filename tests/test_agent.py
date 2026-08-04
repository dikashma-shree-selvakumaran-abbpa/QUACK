"""Unit tests for quack.agent with a mocked llmio.chat (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quack import agent


def _tool_call(name: str, arguments: dict, call_id: str = "c1") -> dict:
	return {
		"role": "assistant",
		"content": None,
		"tool_calls": [
			{
				"id": call_id,
				"type": "function",
				"function": {"name": name, "arguments": json.dumps(arguments)},
			}
		],
	}


def _final(payload: dict) -> dict:
	return {"role": "assistant", "content": json.dumps(payload)}


# ---------------------------------------------------------------------------
# Containment.
# ---------------------------------------------------------------------------


def test_read_file_rejects_path_escape(tmp_path: Path) -> None:
	result = agent._read_file(tmp_path, "../../../etc/passwd")
	assert result.startswith("error:")
	assert "outside the repository" in result


def test_list_dir_rejects_path_escape(tmp_path: Path) -> None:
	result = agent._list_dir(tmp_path, "../../../etc")
	assert result.startswith("error:")
	assert "outside the repository" in result


def test_read_file_rejects_absolute_outside(tmp_path: Path) -> None:
	assert agent._read_file(tmp_path, "/etc/passwd").startswith("error:")


# ---------------------------------------------------------------------------
# run_tests filter injection.
# ---------------------------------------------------------------------------


def test_run_tests_rejects_shell_metacharacters(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	def boom(*args, **kwargs):
		raise AssertionError("subprocess must not be reached")

	monkeypatch.setattr(agent.runio, "run_dotnet_test", boom)
	(tmp_path / "Proj.csproj").write_text("<Project/>", encoding="utf-8")

	result = agent._run_tests(tmp_path, 'Proj.csproj --filter "; rm -rf /"')

	assert result.startswith("error:")
	assert "filter rejected" in result


def test_run_tests_accepts_valid_filter(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	captured: dict = {}

	def fake_dotnet(project, test_filter, timeout_s=180):
		captured["project"] = project
		captured["filter"] = test_filter
		return (0, "Passed!")

	monkeypatch.setattr(agent.runio, "run_dotnet_test", fake_dotnet)
	(tmp_path / "Proj.csproj").write_text("<Project/>", encoding="utf-8")

	result = agent._run_tests(
		tmp_path, "Proj.csproj --filter FullyQualifiedName~RectTransformTests"
	)

	assert "exit_code=0" in result
	assert captured["filter"] == "FullyQualifiedName~RectTransformTests"


# ---------------------------------------------------------------------------
# Loop budgets.
# ---------------------------------------------------------------------------


def test_iteration_cap_forces_final_answer(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	tool_phase_calls = 0

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		nonlocal tool_phase_calls
		if tools is not None:
			tool_phase_calls += 1
		# Always keep asking for another tool call.
		return _tool_call("read_file", {"path": "src/Foo.cs"})

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	result = agent.run("diff", Path("."), "m")

	assert isinstance(result, agent.AgentResult)
	assert tool_phase_calls == agent.MAX_ITERATIONS
	assert "unavailable" in result.summary


def test_run_tests_call_cap(monkeypatch: pytest.MonkeyPatch) -> None:
	run_tests_calls = 0

	def fake_pytest(paths, timeout_s=180):
		nonlocal run_tests_calls
		run_tests_calls += 1
		return (0, "1 passed")

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		return _tool_call("run_tests", {"project_or_paths": "tests/test_x.py"})

	monkeypatch.setattr(agent.runio, "run_pytest", fake_pytest)
	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	# safe_path needs the file to resolve inside root; "." resolves to cwd and
	# any relative path is contained, so no real file is required here.
	agent.run("diff", Path("."), "m")

	assert run_tests_calls == agent.MAX_RUN_TESTS


def test_valid_final_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
	payload = {
		"summary": "RectTransform boundary regressed",
		"tests_run": ["RectTransformTests"],
		"failures": [
			{"test": "RectTransformTests.Boundary", "diagnosis": "< should be <="}
		],
		"proposed_patch": "--- a/RectTransform.cs\n+++ b/RectTransform.cs\n",
		"proposed_new_tests": None,
	}

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		return _final(payload)

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	result = agent.run("diff", Path("."), "m")

	assert result.summary == "RectTransform boundary regressed"
	assert result.failures[0]["diagnosis"] == "< should be <="
	assert result.proposed_patch is not None


def test_malformed_json_retries_then_degrades(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	calls = 0

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		nonlocal calls
		calls += 1
		return {"role": "assistant", "content": "not json"}

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	result = agent.run("diff", Path("."), "m")

	assert isinstance(result, agent.AgentResult)
	assert "unavailable" in result.summary
	# First answer + one retry.
	assert calls == 2


# ---------------------------------------------------------------------------
# Lenient final-JSON parsing (fences / surrounding prose).
# ---------------------------------------------------------------------------


def _agent_payload() -> dict:
	return {
		"summary": "ok",
		"tests_run": [],
		"failures": [],
		"proposed_patch": None,
		"proposed_new_tests": None,
	}


def _run_with_content(
	monkeypatch: pytest.MonkeyPatch, content: str
) -> agent.AgentResult:
	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		return {"role": "assistant", "content": content}

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)
	return agent.run("diff", Path("."), "m")


def test_final_json_in_json_fence(monkeypatch: pytest.MonkeyPatch) -> None:
	content = "```json\n" + json.dumps(_agent_payload()) + "\n```"
	result = _run_with_content(monkeypatch, content)
	assert result.summary == "ok"


def test_final_json_in_bare_fence(monkeypatch: pytest.MonkeyPatch) -> None:
	content = "```\n" + json.dumps(_agent_payload()) + "\n```"
	result = _run_with_content(monkeypatch, content)
	assert result.summary == "ok"


def test_final_json_with_leading_prose(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	content = "Here's my analysis: " + json.dumps(_agent_payload()) + " Done."
	result = _run_with_content(monkeypatch, content)
	assert result.summary == "ok"


def test_non_json_still_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
	calls = 0

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		nonlocal calls
		calls += 1
		return {"role": "assistant", "content": "no json here at all"}

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)
	result = agent.run("diff", Path("."), "m")

	assert isinstance(result, agent.AgentResult)
	assert "unavailable" in result.summary
	assert calls == 2


# ---------------------------------------------------------------------------
# Varied final-answer field shapes (real captured model outputs).
# ---------------------------------------------------------------------------


# Response 2: the exact captured output that previously FAILED validation
# because proposed_new_tests is a list of {description, code} objects.
_RESPONSE_2 = """{
  "summary": "The change modifies the boundary condition in the Contains method of the RectTransform class. This could affect whether points on the right edge of the rectangle are considered contained. Further investigation is needed to confirm if existing tests cover this behavior.",
  "tests_run": [],
  "failures": [],
  "proposed_patch": null,
  "proposed_new_tests": [
	{
	  "description": "Test for Contains method to check point on the right edge of the rectangle.",
	  "code": "public void TestContains_RightEdge() { ... }"
	}
  ]
}"""


def test_response2_proposed_new_tests_list_of_dicts(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	result = _run_with_content(monkeypatch, _RESPONSE_2)

	assert isinstance(result, agent.AgentResult)
	assert "boundary condition" in result.summary
	assert result.tests_run == []
	assert result.failures == []
	assert result.proposed_patch is None
	# Normalized to a single "description: code" display string.
	assert result.proposed_new_tests == (
		"Test for Contains method to check point on the right edge of the "
		"rectangle.: public void TestContains_RightEdge() { ... }"
	)


def test_response1_tool_call_is_not_final(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# Response 1 is a mid-loop tool-call response, not a final answer: the loop
	# must dispatch the tool and keep going, reaching the final JSON next turn.
	steps = [
		_tool_call("read_file", {"path": "src/RectTransform.cs"}),
		{"role": "assistant", "content": _RESPONSE_2},
	]

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		return steps.pop(0)

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)
	result = agent.run("diff", Path("."), "m")

	assert isinstance(result, agent.AgentResult)
	assert "boundary condition" in result.summary
	assert result.proposed_new_tests is not None
	assert steps == []  # tool-call turn + final turn both consumed


def test_proposed_new_tests_list_of_strings(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	payload = _agent_payload()
	payload["proposed_new_tests"] = ["test_a covers edge", "test_b covers null"]
	result = _run_with_content(monkeypatch, json.dumps(payload))

	assert result.proposed_new_tests == "test_a covers edge\ntest_b covers null"


def test_tests_run_list_of_dicts(monkeypatch: pytest.MonkeyPatch) -> None:
	payload = _agent_payload()
	payload["tests_run"] = [
		{"test": "RectTransformTests.Boundary"},
		{"path": "tests/test_x.py"},
	]
	result = _run_with_content(monkeypatch, json.dumps(payload))

	assert result.tests_run == [
		"RectTransformTests.Boundary",
		"tests/test_x.py",
	]


# ---------------------------------------------------------------------------
# Ground-truth override: tool exit code beats the model's self-report.
# ---------------------------------------------------------------------------


_FAILED_DOTNET_OUTPUT = (
	"Failed GraphicsEditor.Core.Tests.RectTransformTests."
	"Contains_PointOnRightEdge_ReturnsTrue [12 ms]\n"
	"  Expected: True\n"
	"  Actual: False\n"
	"Failed! - Failed: 1, Passed: 41, Skipped: 0, Total: 42"
)


def test_nonzero_exit_overrides_model_all_passed(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	# Tool reports a real failure (exit 1); model dishonestly claims all passed.
	def fake_pytest(paths, timeout_s=180):
		return (1, _FAILED_DOTNET_OUTPUT)

	monkeypatch.setattr(agent.runio, "run_pytest", fake_pytest)

	# The real .py file must exist so containment passes and the run happens.
	test_file = tmp_path / "tests"
	test_file.mkdir()
	(test_file / "test_rect.py").write_text("", encoding="utf-8")

	final = {
		"summary": "All tests passed; the change is safe to push.",
		"tests_run": ["tests/test_rect.py"],
		"failures": [],
		"proposed_patch": None,
		"proposed_new_tests": None,
	}
	steps = [
		_tool_call("run_tests", {"project_or_paths": "tests/test_rect.py"}),
		{"role": "assistant", "content": json.dumps(final)},
	]

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		return steps.pop(0)

	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	result = agent.run("diff", tmp_path, "m")

	# The override must fire regardless of what the model claimed.
	assert result.failures, "expected synthesized failures from tool output"
	assert any(
		"Contains_PointOnRightEdge_ReturnsTrue" in f["test"]
		for f in result.failures
	)
	assert "[verified]" in result.summary
	assert "overriding model's self-report" in result.summary


# ---------------------------------------------------------------------------
# Security (Rule #4): the agent path must redact secrets before the diff
# ever reaches the model, exactly like Tier 2 does.
# ---------------------------------------------------------------------------


def test_agent_redacts_secret_before_sending_to_model(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli
	from quack.delta import StagedDelta, StagedFile

	secret = "AKIA" + "A" * 16
	hunk = f'@@ -0,0 +1,1 @@\n+AWS_KEY = "{secret}"'
	delta = StagedDelta(
		files=[
			StagedFile(
				path="src/config.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)

	captured: dict = {}

	def fake_chat(messages, model, tools=None, timeout_s=180.0):
		# Record the messages the very first time the model is contacted.
		captured.setdefault("messages", messages)
		return {
			"role": "assistant",
			"content": json.dumps(
				{
					"summary": "ok",
					"tests_run": [],
					"failures": [],
					"proposed_patch": None,
					"proposed_new_tests": None,
				}
			),
		}

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda: delta)
	monkeypatch.setattr(agent.llmio, "chat", fake_chat)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	serialized = json.dumps(captured["messages"])
	assert secret not in serialized
	assert "[REDACTED]" in serialized


# ---------------------------------------------------------------------------
# Tier 2 pre-push wiring: the agent path runs Tier 2 as a fast, fail-open
# single completion BEFORE the slow agent loop, reusing the Tier 1 findings
# it already computed for redaction.
# ---------------------------------------------------------------------------


def _plain_delta():
	from quack.delta import StagedDelta, StagedFile

	hunk = "@@ -0,0 +1,1 @@\n+x = 1"
	return StagedDelta(
		files=[
			StagedFile(
				path="src/thing.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)


def _stub_agent_loop(monkeypatch):
	"""Make the agent loop a no-op success so tests focus on the Tier 2 pre-pass."""
	from quack import agent as agent_mod

	def fake_run(diff, root, model):
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)


def test_agent_renders_tier2_verdict_when_review_returns_result(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli, tier2

	verdict = tier2.ReviewResult(
		risk="high",
		reasons=["src/thing.py: touches money path"],
		one_liner="Risky change to payment logic",
	)

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (verdict, None)
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "Risky change to payment logic" in result.output
	assert "HIGH" in result.output


def test_agent_renders_nothing_and_still_runs_when_review_returns_none(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run(diff, root, model):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "model unavailable")
	)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True
	# No verdict header rendered for a None review, but the failure is made
	# visible at pre-push rather than being silent, using the real reason.
	assert "risk:" not in result.output
	assert "AI review unavailable (model unavailable)" in result.output


def test_agent_survives_tier2_exception_without_changing_exit_code(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def boom(*a, **k):
		raise RuntimeError("tier2 exploded")

	def fake_run(diff, root, model):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.tier2, "review_with_reason", boom)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True


def test_agent_loads_instructions_with_repo_root(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	seen = {}

	def fake_load(repo_root, max_chars=4000):
		seen["repo_root"] = repo_root
		return None

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: "/tmp/myrepo")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.instructions, "load", fake_load)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert seen["repo_root"] == Path("/tmp/myrepo")


def test_agent_computes_tier1_findings_once(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	calls = {"tier1": 0}
	captured = {}

	real_run = cli.tier1_run

	def counting_run(delta, config):
		calls["tier1"] += 1
		return real_run(delta, config)

	def capture_review(delta, findings, plan, **kwargs):
		captured["findings"] = findings
		return None, "x"

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli, "tier1_run", counting_run)
	monkeypatch.setattr(cli.tier2, "review_with_reason", capture_review)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	# Tier 1 is computed exactly once and the same findings feed Tier 2.
	assert calls["tier1"] == 1
	assert "findings" in captured


def test_agent_passes_provider_timeout_to_tier2(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	seen = {}

	def capture_review(delta, findings, plan, **kwargs):
		seen["timeout_s"] = kwargs.get("timeout_s")
		return None, "x"

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	# The slow-transport timeout must be forwarded, not tier2's 6.0 default.
	monkeypatch.setattr(cli.llmio, "default_timeout", lambda: 60.0)
	monkeypatch.setattr(cli.tier2, "review_with_reason", capture_review)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert seen["timeout_s"] == 60.0


def test_agent_renders_provider_reason_when_tier2_unavailable(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	# The agent's own startup guard passes (call 1 -> None), but Tier 2 then
	# fails and consults availability_error again (call 2 -> reason), which is
	# what should surface as the dim line.
	calls = {"n": 0}

	def availability():
		calls["n"] += 1
		return None if calls["n"] == 1 else "no GITHUB_TOKEN"

	monkeypatch.setattr(cli.llmio, "availability_error", availability)
	# review returns no specific reason, so the CLI falls back to the
	# provider-level availability reason.
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, None)
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "AI review unavailable (no GITHUB_TOKEN)" in result.output


def test_agent_renders_dim_reason_when_tier2_raises(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def boom(*a, **k):
		raise RuntimeError("tier2 exploded")

	def fake_run(diff, root, model):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.tier2, "review_with_reason", boom)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True
	assert "AI review unavailable" in result.output



# ---------------------------------------------------------------------------
# Provider-aware availability: the agent must ask the selected provider
# whether it can run, not hardcode a GITHUB_TOKEN check. Under copilot_sdk
# (which authenticates via the Copilot CLI login) it must run even with no
# GITHUB_TOKEN; when the provider is unavailable it fails open with the
# provider's readable reason.
# ---------------------------------------------------------------------------


def test_agent_runs_under_copilot_sdk_without_github_token(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run(diff, root, model):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	# copilot_sdk provider reports available regardless of GITHUB_TOKEN.
	monkeypatch.delenv("GITHUB_TOKEN", raising=False)
	monkeypatch.setattr(cli.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True


def test_agent_fails_open_with_provider_reason(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run(diff, root, model):
		ran["agent"] = True
		raise AssertionError("agent must not run when provider is unavailable")

	monkeypatch.setattr(cli.llmio, "availability_error", lambda: "no GITHUB_TOKEN")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is False
	assert "quack agent: no GITHUB_TOKEN" in result.output


# ---------------------------------------------------------------------------
# Model resolution: the default model is transport-specific AND use-specific,
# so it comes from the selected provider split by kind. The agent loop uses the
# provider's AGENT default; Tier 2's single-shot review uses the COMPLETION
# default. --model and QUACK_MODEL still win over both.
# ---------------------------------------------------------------------------


def test_resolve_agent_model_uses_provider_agent_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(
		cli.llmio,
		"default_model",
		lambda kind="completion": {
			"agent": "claude-sonnet-4.5",
			"completion": "claude-haiku-4.5",
		}[kind],
	)
	assert cli._resolve_agent_model(None) == "claude-sonnet-4.5"


def test_resolve_completion_model_uses_provider_completion_default(
	monkeypatch,
) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(
		cli.llmio,
		"default_model",
		lambda kind="completion": {
			"agent": "claude-sonnet-4.5",
			"completion": "claude-haiku-4.5",
		}[kind],
	)
	assert cli._resolve_completion_model(None) == "claude-haiku-4.5"


def test_resolve_agent_model_cli_option_wins(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "from-env")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_agent_model("explicit/model") == "explicit/model"


def test_resolve_completion_model_cli_option_wins(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "from-env")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_completion_model("explicit/model") == "explicit/model"


def test_resolve_agent_model_env_beats_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "openai/gpt-4o-mini")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_agent_model(None) == "openai/gpt-4o-mini"


def test_resolve_completion_model_env_beats_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "openai/gpt-4o-mini")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_completion_model(None) == "openai/gpt-4o-mini"


def test_resolve_agent_model_falls_back_when_no_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(cli.llmio, "default_model", lambda kind="completion": None)
	assert cli._resolve_agent_model(None) == cli.DEFAULT_AGENT_MODEL




