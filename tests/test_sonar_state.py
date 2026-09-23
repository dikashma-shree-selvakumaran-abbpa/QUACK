from __future__ import annotations

import json
from types import SimpleNamespace

from quack import sonar_state


def _delta(text: str = "x = 1") -> SimpleNamespace:
	return SimpleNamespace(
		files=[
			SimpleNamespace(
				path="src/app.py",
				status="M",
				added=1,
				removed=0,
			)
		],
		raw_diff=text,
	)


def _state(digest: str) -> sonar_state.State:
	return sonar_state.State(
		digest=digest,
		scope="staged",
		repo_root="/repo",
		project_key="project",
		host_url="https://sonar.example",
		branch="validation",
		status="passed",
		reason="snapshot complete",
		violation_count=0,
		timestamp=100.0,
		scan_status="passed",
		task_id="task-1",
		analysis_id="analysis-1",
	)


def test_state_round_trips_only_for_matching_snapshot(tmp_path) -> None:
	identity = {
		"scanner": {"path": "scanner", "sha256": "one"},
		"settings": {"sources": "src", "exclusions": ""},
	}
	digest = sonar_state.snapshot_digest(
		_delta(),
		scope="staged",
		repo_root="/repo",
		project_key="project",
		host_url="https://sonar.example",
		branch="validation",
		config_identity=identity,
	)
	path = tmp_path / "sonar-state.json"
	sonar_state.write("/repo", _state(digest), path_override=path)

	result = sonar_state.read(
		"/repo",
		digest,
		scope="staged",
		project_key="project",
		host_url="https://sonar.example",
		branch="validation",
		path_override=path,
		now=101.0,
	)

	assert result is not None
	assert result.task_id == "task-1"
	assert result.analysis_id == "analysis-1"
	assert (
		sonar_state.snapshot_digest(
			_delta("x = 2"),
			scope="staged",
			repo_root="/repo",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			config_identity={
				"scanner": {"path": "scanner", "sha256": "two"},
				"settings": {"sources": "src", "exclusions": ""},
			},
		)
		!= digest
	)


def test_snapshot_digest_includes_effective_configuration() -> None:
	base = {
		"scanner": {"path": "scanner", "version": "1"},
		"settings": {"sources": "src", "tests": "tests"},
	}
	changed = {
		"scanner": {"path": "scanner", "version": "1"},
		"settings": {"sources": "src", "tests": "tests", "exclusions": "gen"},
	}

	first = sonar_state.snapshot_digest(
		_delta(),
		scope="staged",
		repo_root="/repo",
		project_key="project",
		host_url="https://sonar.example",
		config_identity=base,
	)
	second = sonar_state.snapshot_digest(
		_delta(),
		scope="staged",
		repo_root="/repo",
		project_key="project",
		host_url="https://sonar.example",
		config_identity=changed,
	)

	assert first != second


def test_state_expiry_and_malformed_json_are_cache_misses(tmp_path) -> None:
	path = tmp_path / "sonar-state.json"
	digest = "digest"
	sonar_state.write("/repo", _state(digest), path_override=path)
	assert (
		sonar_state.read(
			"/repo",
			digest,
			scope="staged",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=101.0 + sonar_state.MAX_AGE_S,
		)
		is None
	)

	payload = {"entries": [{"digest": digest, "timestamp": "not-a-time"}]}
	path.write_text(json.dumps(payload), encoding="utf-8")
	assert (
		sonar_state.read(
			"/repo",
			digest,
			scope="staged",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
		)
		is None
	)

	payload = {
		"entries": [
			{
				"digest": digest,
				"scope": "staged",
				"repo_root": "/repo",
				"project_key": "project",
				"host_url": "https://sonar.example",
				"branch": "validation",
				"status": "failed",
				"reason": "not reusable",
				"violation_count": 0,
				"timestamp": 100.0,
				"scan_status": "failed",
			}
		]
	}
	path.write_text(json.dumps(payload), encoding="utf-8")
	assert (
		sonar_state.read(
			"/repo",
			digest,
			scope="staged",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=101.0,
		)
		is None
	)

	payload["entries"][0]["status"] = "passed"
	payload["entries"][0]["scan_status"] = "passed"
	payload["entries"][0]["timestamp"] = 102.0
	path.write_text(json.dumps(payload), encoding="utf-8")
	assert (
		sonar_state.read(
			"/repo",
			digest,
			scope="staged",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=101.0,
		)
		is None
	)

	path.write_text("{not-json", encoding="utf-8")
	assert (
		sonar_state.read(
			"/repo",
			digest,
			scope="staged",
			project_key="project",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
		)
		is None
	)


def test_state_write_never_exceeds_bounded_entry_shape(tmp_path) -> None:
	path = tmp_path / "sonar-state.json"
	digest = "digest"
	state = _state(digest)
	sonar_state.write("/repo", state, path_override=path)
	payload = json.loads(path.read_text(encoding="utf-8"))

	assert list(payload) == ["entries"]
	assert len(payload["entries"]) == 1
	assert payload["entries"][0]["scope"] == "staged"
