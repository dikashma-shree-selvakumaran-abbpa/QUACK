"""Unit tests for quack.tier2 with a mocked llmio.complete (no network)."""

from __future__ import annotations

import json

import pytest

from quack import testmap, tier2
from quack.delta import StagedDelta, StagedFile
from quack.llmio import LLMUnavailable


def _delta() -> StagedDelta:
	file = StagedFile(
		path="src/pay.py",
		status="M",
		added=3,
		removed=1,
		hunks=["@@ -1,2 +1,3 @@\n+amount = total * rate"],
	)
	return StagedDelta(files=[file], raw_diff="+amount = total * rate")


def _plan() -> testmap.TestPlan:
	return testmap.TestPlan(
		untested_sources=["src/pay.py"],
		runner_commands=["pytest tests/test_pay.py -x"],
	)


def _valid_json() -> str:
	return json.dumps(
		{
			"risk": "high",
			"reasons": ["src/pay.py:1 money calculation changed"],
			"tests_to_run": ["tests/test_pay.py"],
			"missing_tests": ["src/pay.py"],
			"one_liner": "Money path changed without a test.",
		}
	)


def test_valid_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list = []

	def fake_complete(messages, model, timeout_s=6.0):
		calls.append(messages)
		return _valid_json()

	monkeypatch.setattr(tier2.llmio, "complete", fake_complete)

	result = tier2.review(_delta(), [], _plan(), model="m")

	assert result is not None
	assert result.risk == "high"
	assert result.tests_to_run == ["tests/test_pay.py"]
	assert result.missing_tests == ["src/pay.py"]
	assert result.one_liner == "Money path changed without a test."
	assert len(calls) == 1


def test_invalid_then_valid_retries(monkeypatch: pytest.MonkeyPatch) -> None:
	outputs = ["not json at all", _valid_json()]

	def fake_complete(messages, model, timeout_s=6.0):
		return outputs.pop(0)

	monkeypatch.setattr(tier2.llmio, "complete", fake_complete)

	result = tier2.review(_delta(), [], _plan(), model="m")

	assert result is not None
	assert result.risk == "high"
	assert outputs == []  # both responses consumed -> retry happened


def test_invalid_twice_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list = []

	def fake_complete(messages, model, timeout_s=6.0):
		calls.append(messages)
		return "still not json"

	monkeypatch.setattr(tier2.llmio, "complete", fake_complete)

	result = tier2.review(_delta(), [], _plan(), model="m")

	assert result is None
	assert len(calls) == 2  # original + one retry, then give up


def test_llm_unavailable_returns_none(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	def fake_complete(messages, model, timeout_s=6.0):
		raise LLMUnavailable("no GITHUB_TOKEN")

	monkeypatch.setattr(tier2.llmio, "complete", fake_complete)

	result = tier2.review(_delta(), [], _plan(), model="m")

	assert result is None


def test_schema_violation_returns_none(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	bad = json.dumps({"risk": "critical", "one_liner": "x"})

	def fake_complete(messages, model, timeout_s=6.0):
		return bad

	monkeypatch.setattr(tier2.llmio, "complete", fake_complete)

	result = tier2.review(_delta(), [], _plan(), model="m")

	assert result is None
