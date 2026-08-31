"""Tests for quack.server endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quack import server


def test_server_endpoints(monkeypatch):
	client = TestClient(server.app)

	# GET /status
	res = client.get("/status")
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert "version" in data
	assert "availabilityError" in data
	assert "cachePath" in data

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
	res = client.post("/check", json={"repo_path": None})
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
	res = client.post("/review", json={"repo_path": "/tmp", "model": "gpt-4"})
	assert res.status_code == 200
	data = res.json()
	assert data["schemaVersion"] == 1
	assert data["status"] == "reviewed"
	assert data["diffHash"] == "hash123"
