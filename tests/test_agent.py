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



