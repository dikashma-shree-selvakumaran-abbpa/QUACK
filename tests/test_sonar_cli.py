"""Tests for deterministic scanner-correlated Sonar Web API reads."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from quack import sonar_cli
from quack.delta import StagedDelta, StagedFile


class _Response:
	def __init__(self, payload: dict, status: int = 200) -> None:
		self.payload = payload
		self.status = status

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False

	def read(self, limit: int) -> bytes:
		return json.dumps(self.payload).encode("utf-8")


def _delta() -> StagedDelta:
	return StagedDelta(
		files=[
			StagedFile("src/a.ts", "M", 1, 0, []),
			StagedFile("src/b.ts", "M", 1, 0, []),
		],
		raw_diff="+change",
	)


def test_direct_api_collects_and_scopes_every_changed_file_violation(
	monkeypatch, tmp_path: Path
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	seen: list[tuple[str, dict[str, list[str]]]] = []

	def open_sonar(request, *, timeout):
		parsed = urlsplit(request.full_url)
		seen.append((parsed.path, parse_qs(parsed.query)))
		if parsed.path == "/api/project_analyses/search":
			return _Response(
				{"analyses": [{"key": "analysis-1", "date": "2026-09-23T16:30:00+0000"}]}
			)
		if parsed.path == "/api/hotspots/search":
			return _Response(
				{
					"hotspots": [
						{
							"key": "hotspot-1",
							"ruleKey": "typescript:S1523",
							"component": "demo:src/a.ts",
							"line": 4,
							"message": "Review dynamic execution.",
						},
						{
							"key": "old-hotspot",
							"component": "demo:src/unchanged.ts",
						},
					],
					"paging": {"total": 2},
				}
			)
		if parsed.path == "/api/issues/search":
			return _Response(
				{
					"issues": [
						{
							"key": "issue-1",
							"rule": "typescript:S1234",
							"component": "demo:src/a.ts",
							"line": 7,
							"message": "First issue.",
						},
						{
							"key": "issue-2",
							"rule": "typescript:S5678",
							"component": "demo:src/b.ts",
							"line": 9,
							"message": "Second issue.",
						},
					],
					"paging": {"total": 2},
				}
			)
		if parsed.path == "/api/measures/component":
			return _Response({"component": {"measures": []}})
		raise AssertionError(parsed.path)

	monkeypatch.setattr(sonar_cli, "_open_sonar", open_sonar)

	result = sonar_cli.run(
		_delta(),
		tmp_path,
		source="watch",
		project_key="demo",
		host_url="https://sonar.example",
		branch="feature",
		expected_analysis_id="analysis-1",
	)

	assert result is not None
	assert result.status == "passed"
	assert result.correlated is True
	assert result.violation_count == 3
	assert result.blocks_commit is True
	assert {path for path, _ in seen} == {
		"/api/project_analyses/search",
		"/api/hotspots/search",
		"/api/issues/search",
		"/api/measures/component",
	}
	assert all(query.get("branch") == ["feature"] for _, query in seen)
	content = (tmp_path / "docs" / "SONARQUBE_REPORT.md").read_text(encoding="utf-8")
	assert "typescript:S1523" in content
	assert "typescript:S1234" in content
	assert "typescript:S5678" in content
	assert "old-hotspot" not in content


def test_debug_prints_cli_and_api_inputs_outputs_without_token(
	monkeypatch, tmp_path: Path, capsys
) -> None:
	monkeypatch.setenv("SQ_TOKEN", "secret-token")
	monkeypatch.setenv("QUACK_SONAR_DEBUG", "1")

	def open_sonar(request, *, timeout):
		path = urlsplit(request.full_url).path
		if path == "/api/project_analyses/search":
			return _Response({"analyses": [{"key": "analysis-1"}]})
		if path == "/api/hotspots/search":
			return _Response({"hotspots": [], "paging": {"total": 0}})
		if path == "/api/issues/search":
			return _Response({"issues": [], "paging": {"total": 0}})
		return _Response({"component": {"measures": []}})

	monkeypatch.setattr(sonar_cli, "_open_sonar", open_sonar)

	result = sonar_cli.run(
		_delta(),
		tmp_path,
		project_key="demo",
		host_url="https://sonar.example",
		expected_analysis_id="analysis-1",
	)

	assert result is not None
	assert result.violation_count == 0
	output = capsys.readouterr().err
	assert "[debug] SonarQube CLI API input" in output
	assert "[debug] SonarQube CLI API output" in output
	assert '"issues":[]' in output
	assert "secret-token" not in output
