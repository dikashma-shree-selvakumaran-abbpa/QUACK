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


def test_writing_more_than_max_entries_per_repo_evicts_oldest_for_that_repo(
	tmp_path,
) -> None:
	path = tmp_path / "sonar-state.json"
	# Write entries for repo-b first so we verify other repos are untouched
	state_b = sonar_state.State(
		digest="digest-b",
		scope="staged",
		repo_root="/repo-b",
		project_key="project-b",
		host_url="https://sonar.example",
		branch="validation",
		status="passed",
		reason="snapshot complete",
		violation_count=0,
		timestamp=50.0,
		scan_status="passed",
		task_id="task-b",
		analysis_id="analysis-b",
	)
	sonar_state.write("/repo-b", state_b, path_override=path)

	# Write 7 entries for repo-a with timestamps 101.0 through 107.0 (max_entries_per_repo=5)
	for i in range(1, 8):
		st = sonar_state.State(
			digest=f"digest-a-{i}",
			scope="staged",
			repo_root="/repo-a",
			project_key="project-a",
			host_url="https://sonar.example",
			branch="validation",
			status="passed",
			reason="snapshot complete",
			violation_count=0,
			timestamp=100.0 + i,
			scan_status="passed",
			task_id=f"task-a-{i}",
			analysis_id=f"analysis-a-{i}",
		)
		sonar_state.write(
			"/repo-a",
			st,
			path_override=path,
			max_entries_per_repo=5,
		)

	# Oldest entries (1 and 2) should be evicted for repo-a
	assert (
		sonar_state.read(
			"/repo-a",
			"digest-a-1",
			scope="staged",
			project_key="project-a",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=200.0,
		)
		is None
	)
	assert (
		sonar_state.read(
			"/repo-a",
			"digest-a-2",
			scope="staged",
			project_key="project-a",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=200.0,
		)
		is None
	)

	# Newest entries (3 through 7) should still exist
	for i in range(3, 8):
		res = sonar_state.read(
			"/repo-a",
			f"digest-a-{i}",
			scope="staged",
			project_key="project-a",
			host_url="https://sonar.example",
			branch="validation",
			path_override=path,
			now=200.0,
		)
		assert res is not None
		assert res.task_id == f"task-a-{i}"

	# repo-b entry should remain untouched
	res_b = sonar_state.read(
		"/repo-b",
		"digest-b",
		scope="staged",
		project_key="project-b",
		host_url="https://sonar.example",
		branch="validation",
		path_override=path,
		now=200.0,
	)
	assert res_b is not None
	assert res_b.task_id == "task-b"


def test_heavy_activity_in_other_repo_does_not_evict_recent_state(
	tmp_path,
) -> None:
	path = tmp_path / "sonar-state.json"
	state_a = sonar_state.State(
		digest="digest-a",
		scope="staged",
		repo_root="/repo-a",
		project_key="project-a",
		host_url="https://sonar.example",
		branch="validation",
		status="passed",
		reason="snapshot complete",
		violation_count=0,
		timestamp=10.0,
		scan_status="passed",
		task_id="task-a",
		analysis_id="analysis-a",
	)
	sonar_state.write("/repo-a", state_a, path_override=path)

	# Write 50 entries in repo-b (exceeding old global cap of 40)
	for i in range(50):
		st_b = sonar_state.State(
			digest=f"digest-b-{i}",
			scope="staged",
			repo_root="/repo-b",
			project_key="project-b",
			host_url="https://sonar.example",
			branch="validation",
			status="passed",
			reason="snapshot complete",
			violation_count=0,
			timestamp=float(20 + i),
			scan_status="passed",
			task_id=f"task-b-{i}",
			analysis_id=f"analysis-b-{i}",
		)
		sonar_state.write("/repo-b", st_b, path_override=path)

	res_a = sonar_state.read(
		"/repo-a",
		"digest-a",
		scope="staged",
		project_key="project-a",
		host_url="https://sonar.example",
		branch="validation",
		path_override=path,
		now=100.0,
	)
	assert res_a is not None
	assert res_a.task_id == "task-a"


def test_global_ceiling_bounds_total_entries_across_many_repos(tmp_path) -> None:
	path = tmp_path / "sonar-state.json"
	# Write 30 repos with 1 entry each, using max_entries=10
	for i in range(30):
		st = sonar_state.State(
			digest=f"digest-{i}",
			scope="staged",
			repo_root=f"/repo-{i}",
			project_key=f"project-{i}",
			host_url="https://sonar.example",
			branch="validation",
			status="passed",
			reason="snapshot complete",
			violation_count=0,
			timestamp=float(100 + i),
			scan_status="passed",
			task_id=f"task-{i}",
			analysis_id=f"analysis-{i}",
		)
		sonar_state.write(
			f"/repo-{i}",
			st,
			path_override=path,
			max_entries=10,
		)

	payload = json.loads(path.read_text(encoding="utf-8"))
	assert len(payload["entries"]) <= 10

