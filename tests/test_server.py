"""Tests for quack.server endpoints."""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from quack import agent, server

# Assembled from fragments so quack's own secrets check does not fire on this
# source file. The file written into the temp repo still contains the full
# key, which is what the test needs tier1 to catch.
_FAKE_AWS_KEY = "AKIA" + "1234567890ABCDEF"


@pytest.fixture
def git_repo(tmp_path) -> str:
	"""A real (empty) git repository on disk."""
	repo = tmp_path / "repo"
	repo.mkdir()
	subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
	return str(repo)


def test_server_endpoints(monkeypatch, git_repo):
	client = TestClient(server.app)

	# GET /status
	res = client.get("/status")
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert "version" in data
	assert "availabilityError" in data
	assert "cachePath" in data
	assert data["buildCommit"] is None or isinstance(data["buildCommit"], str)
	assert data["buildDate"] is None or isinstance(data["buildDate"], str)

	# GET /models
	monkeypatch.setattr(server.llmio, "list_models", lambda: ["model-a", "model-b"])
	monkeypatch.setattr(server.llmio, "default_model", lambda: "model-a")
	res = client.get("/models")
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert data["defaultModel"] == "model-a"
	assert len(data["models"]) == 2
	assert data["models"][0]["id"] == "model-a"

	# POST /check
	res = client.post("/check", json={"repo_path": git_repo})
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert "blocked" in data
	assert "findings" in data
	assert "testGuidance" in data

	# POST /review
	monkeypatch.setattr(
		server.watch,
		"review_once",
		lambda repo_root, model=None, quiet=True: server.watch.WatchResult(
			files=1, diff_hash="hash123"
		),
	)
	res = client.post("/review", json={"repo_path": git_repo, "model": "gpt-4"})
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert data["status"] == "reviewed"
	assert data["diffHash"] == "hash123"


def test_check_requires_repo_path():
	res = TestClient(server.app).post("/check", json={})
	assert res.status_code == 400


def test_review_requires_repo_path():
	res = TestClient(server.app).post("/review", json={"model": "gpt-4"})
	assert res.status_code == 400


def test_check_rejects_non_repository(tmp_path):
	res = TestClient(server.app).post("/check", json={"repo_path": str(tmp_path)})
	assert res.status_code == 400


def test_review_rejects_non_repository(tmp_path):
	res = TestClient(server.app).post(
		"/review", json={"repo_path": str(tmp_path), "model": "gpt-4"}
	)
	assert res.status_code == 400


# ---------------------------------------------------------------------------
# /agent
# ---------------------------------------------------------------------------


def _stage(repo: str, name: str, content: str) -> None:
	(Path(repo) / name).write_text(content, encoding="utf-8")
	subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)


def _settled(client, job_id: str, timeout_s: float = 15.0) -> dict:
	"""Poll until the job leaves "running" - fail the test if it never does."""
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		res = client.get(f"/agent/{job_id}")
		assert res.status_code == 200
		data = res.json()
		if data["status"] != "running":
			return data
		time.sleep(0.02)
	pytest.fail(f"job {job_id} never settled")


def _explode(*args, **kwargs):
	raise AssertionError("a model boundary was called")


def test_agent_tier1_block_never_calls_a_model(monkeypatch, git_repo):
	_stage(git_repo, "aws.py", f'KEY = "{_FAKE_AWS_KEY}"\n')
	monkeypatch.setattr(server.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(server.tier2, "review_with_reason", _explode)
	monkeypatch.setattr(server.copilot_sdk, "run_agent", _explode)

	client = TestClient(server.app)
	res = client.post("/agent", json={"repo_path": git_repo, "model": "stub-model"})
	assert res.status_code == 200
	job = _settled(client, res.json()["jobId"])

	assert job["status"] == "done"
	assert job["stages"]["tier1"]["blocked"] is True
	assert any(f["rule"] == "secrets" for f in job["stages"]["tier1"]["findings"])
	assert job["stages"]["tier2"] is None
	assert job["stages"]["investigation"] is None


def test_agent_runs_all_stages(monkeypatch, git_repo):
	_stage(git_repo, "calc.py", "def add(a, b):\n\treturn a + b\n")
	review = server.tier2.ReviewResult(
		risk="LOW",
		reasons=["small change"],
		tests_to_run=["tests/test_calc.py"],
		missing_tests=[],
		one_liner="adds a helper",
	)
	result = agent.AgentResult(
		summary="investigated the change",
		tests_run=["tests/test_calc.py"],
		failures=[],
		proposed_patch="--- a/calc.py\n+++ b/calc.py\n",
	)
	monkeypatch.setattr(server.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(
		server.tier2, "review_with_reason", lambda *a, **k: (review, None)
	)
	monkeypatch.setattr(server.copilot_sdk, "run_agent", lambda *a, **k: result)

	client = TestClient(server.app)
	res = client.post("/agent", json={"repo_path": git_repo, "model": "stub-model"})
	job = _settled(client, res.json()["jobId"])

	assert job["status"] == "done"
	assert job["stages"]["tier1"]["blocked"] is False
	assert job["stages"]["tier2"]["risk"] == "LOW"
	assert job["stages"]["tier2"]["summary"] == "adds a helper"
	investigation = job["stages"]["investigation"]
	assert investigation["summary"] == "investigated the change"
	assert investigation["testsRun"] == ["tests/test_calc.py"]
	assert investigation["failures"] == []
	assert investigation["proposedPatch"] == "--- a/calc.py\n+++ b/calc.py\n"


def test_agent_returns_existing_job_for_same_repo(monkeypatch, git_repo):
	_stage(git_repo, "calc.py", "def add(a, b):\n\treturn a + b\n")
	release = threading.Event()
	monkeypatch.setattr(server.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(
		server.tier2, "review_with_reason", lambda *a, **k: (None, "stubbed out")
	)
	monkeypatch.setattr(
		server.copilot_sdk,
		"run_agent",
		lambda *a, **k: (release.wait(15), agent.AgentResult(summary="done"))[1],
	)

	client = TestClient(server.app)
	body = {"repo_path": git_repo, "model": "stub-model"}
	try:
		first = client.post("/agent", json=body).json()
		second = client.post("/agent", json=body).json()
		assert first["jobId"] == second["jobId"]
		assert second["status"] == "running"
	finally:
		release.set()


def test_agent_unknown_job_is_404():
	client = TestClient(server.app)
	assert client.get("/agent/does-not-exist").status_code == 404
	assert client.delete("/agent/does-not-exist").status_code == 404


def test_agent_cancel_marks_job_cancelled(monkeypatch, git_repo):
	_stage(git_repo, "calc.py", "def add(a, b):\n\treturn a + b\n")
	release = threading.Event()
	monkeypatch.setattr(server.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(
		server.tier2, "review_with_reason", lambda *a, **k: (None, "stubbed out")
	)
	monkeypatch.setattr(
		server.copilot_sdk,
		"run_agent",
		lambda *a, **k: (release.wait(15), agent.AgentResult(summary="done"))[1],
	)

	client = TestClient(server.app)
	try:
		job_id = client.post(
			"/agent", json={"repo_path": git_repo, "model": "stub-model"}
		).json()["jobId"]
		cancelled = client.delete(f"/agent/{job_id}")
		assert cancelled.status_code == 200
		assert cancelled.json()["status"] == "cancelled"
		assert client.get(f"/agent/{job_id}").json()["status"] == "cancelled"
	finally:
		release.set()


# ---------------------------------------------------------------------------
# /metrics - per-user, so there is no repo_path and no real metrics file
# ---------------------------------------------------------------------------


def _metrics(monkeypatch, events) -> dict:
	monkeypatch.setattr(server.metrics, "read", lambda: events)
	res = TestClient(server.app).get("/metrics")
	assert res.status_code == 200
	return res.json()


def test_metrics_unavailable_when_unreadable(monkeypatch):
	assert _metrics(monkeypatch, None) == {
		"schemaVersion": 1,
		"available": False,
		"totalRuns": 0,
	}


def test_metrics_aggregates_events(monkeypatch):
	data = _metrics(
		monkeypatch,
		[
			{
				"command": "check",
				"blocked": True,
				"duration_ms": 100,
				"tier1_findings": {"secrets": 2},
				"review_cache": "hit",
			},
			{
				"command": "check",
				"blocked": False,
				"duration_ms": 300,
				"tier1_findings": {"secrets": 1, "merge_markers": 2},
				"review_cache": "miss",
			},
			{"command": "agent", "duration_ms": 200, "review_cache": "hit"},
			{"command": "agent"},
		],
	)

	assert data["available"] is True
	assert data["totalRuns"] == 4
	assert data["runsByCommand"] == {"agent": 2, "check": 2}
	assert data["blocks"] == 1
	assert data["findings"] == {"secrets": 3, "merge_markers": 2}
	assert data["medianDurationMs"] == 200
	assert data["cacheHitRate"] == pytest.approx(2 / 3)


def test_metrics_ignores_malformed_values(monkeypatch):
	data = _metrics(
		monkeypatch,
		[
			{
				"command": "check",
				# a bool is not a duration, even though bool subclasses int
				"duration_ms": True,
				"tier1_findings": {
					"secrets": 0,
					"merge_markers": -1,
					"todos": "many",
					"large_files": 2,
				},
				"review_cache": "stale",
			},
			{"command": "check", "duration_ms": 50, "review_cache": "hit"},
		],
	)

	assert data["totalRuns"] == 2
	assert data["blocks"] == 0
	assert data["findings"] == {"large_files": 2}
	assert data["medianDurationMs"] == 50
	assert data["cacheHitRate"] == 1.0


def test_metrics_null_when_no_data(monkeypatch):
	data = _metrics(
		monkeypatch,
		[{"command": "check"}, {"command": "check", "blocked": True}],
	)

	assert data["totalRuns"] == 2
	assert data["blocks"] == 1
	# null means "no data", which is not the same as a real 0
	assert data["medianDurationMs"] is None
	assert data["cacheHitRate"] is None


# ---------------------------------------------------------------------------
# /install/plan and /install
# ---------------------------------------------------------------------------


def _set_hooks_path(repo: str, value: str) -> None:
	subprocess.run(
		["git", "config", "core.hooksPath", value],
		cwd=repo,
		check=True,
		capture_output=True,
	)


def _plan(repo: str) -> dict:
	res = TestClient(server.app).get("/install/plan", params={"repo_path": repo})
	assert res.status_code == 200
	return res.json()


def test_install_plan_reports_precommit_strategy(git_repo):
	data = _plan(git_repo)
	assert data["strategy"] == "precommit"
	assert data["hooksPath"] is None
	assert data["huskyFilesTracked"] is False
	assert "preCommitAvailable" in data
	assert "gitleaksAvailable" in data


def test_install_plan_hooks_installed_when_config_contains_quack(git_repo):
	(Path(git_repo) / ".pre-commit-config.yaml").write_text("quack\n", encoding="utf-8")
	assert _plan(git_repo)["hooksInstalled"] is True


def test_install_plan_hooks_not_installed_when_no_config(git_repo):
	assert _plan(git_repo)["hooksInstalled"] is False


def test_install_plan_detects_husky(git_repo):
	_set_hooks_path(git_repo, ".husky")
	data = _plan(git_repo)
	assert data["strategy"] == "husky"
	assert data["hooksPath"] == ".husky"


def test_install_plan_flags_unsupported_hooks_path(git_repo):
	_set_hooks_path(git_repo, ".myhooks")
	assert _plan(git_repo)["strategy"] == "unsupported_hooks_path"


def test_install_requires_husky_consent(git_repo):
	_set_hooks_path(git_repo, ".husky")
	res = TestClient(server.app).post("/install", json={"repo_path": git_repo})
	assert res.status_code == 409
	# The refusal must be total: husky hooks are tracked, so touching them
	# would change the hook for everyone on the branch.
	assert not (Path(git_repo) / ".husky").exists()


def test_install_rejects_unsupported_hooks_path(git_repo):
	_set_hooks_path(git_repo, ".myhooks")
	res = TestClient(server.app).post("/install", json={"repo_path": git_repo})
	assert res.status_code == 409


def test_install_writes_local_stanza(monkeypatch, git_repo):
	monkeypatch.setattr(server.shutil, "which", lambda cmd: None)
	res = TestClient(server.app).post(
		"/install",
		json={"repo_path": git_repo, "use_local": True, "install_gitleaks": False},
	)
	assert res.status_code == 200
	data = res.json()
	assert data["strategy"] == "precommit"

	config = Path(git_repo) / ".pre-commit-config.yaml"
	assert config.exists()
	text = config.read_text(encoding="utf-8")
	assert "id: quack" in text
	assert "id: quack-agent" in text

	config_step = next(s for s in data["steps"] if s["name"] == "config")
	assert config_step["ok"] is True


def test_install_does_not_bootstrap_gitleaks_unless_asked(monkeypatch, git_repo):
	monkeypatch.setattr(server.shutil, "which", lambda cmd: None)
	monkeypatch.setattr(server.gitleaks, "ensure_installed", _explode)
	res = TestClient(server.app).post(
		"/install",
		json={"repo_path": git_repo, "use_local": True, "install_gitleaks": False},
	)
	assert res.status_code == 200
	assert any(s["name"] == "gitleaks" for s in res.json()["steps"])


def test_install_requires_repo_path():
	client = TestClient(server.app)
	assert client.post("/install", json={}).status_code == 400
	assert client.get("/install/plan").status_code in {400, 422}



