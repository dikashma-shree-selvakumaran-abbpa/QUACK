"""Unit tests for fail-open background review caching."""

from __future__ import annotations

import json

from quack import reviewcache


def _payload(risk: str = "low") -> dict:
	return {
		"risk": risk,
		"reasons": [],
		"tests_to_run": [],
		"missing_tests": [],
		"one_liner": "Looks safe.",
	}


def test_diff_hash_is_stable_and_content_sensitive() -> None:
	assert reviewcache.diff_hash("same diff") == reviewcache.diff_hash("same diff")
	assert reviewcache.diff_hash("same diff") != reviewcache.diff_hash("changed diff")


def test_cache_write_then_read_returns_review(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	digest = reviewcache.diff_hash("diff")
	reviewcache.write("/repo", digest, _payload(), path=path, timestamp=100)

	entry = reviewcache.read("/repo", digest, path=path, now=101)

	assert entry is not None
	assert entry.review_payload == _payload()
	assert entry.timestamp == 100


def test_cache_miss_returns_none(tmp_path) -> None:
	assert reviewcache.read("/repo", "missing", path=tmp_path / "missing.json") is None


def test_expired_entry_returns_none(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	reviewcache.write("/repo", "hash", _payload(), path=path, timestamp=10)

	assert reviewcache.read(
		"/repo", "hash", path=path, now=20, max_age_s=5
	) is None


def test_corrupt_cache_returns_none(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	path.write_text("{not json", encoding="utf-8")

	assert reviewcache.read("/repo", "hash", path=path) is None


def test_unreadable_cache_returns_none(monkeypatch, tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	path.write_text("{}", encoding="utf-8")

	def denied(*args, **kwargs):
		raise PermissionError("denied")

	monkeypatch.setattr(type(path), "read_text", denied)
	assert reviewcache.read("/repo", "hash", path=path) is None


def test_cache_write_retries_transient_replace_failure(monkeypatch, tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	replace = reviewcache.os.replace
	attempts = 0

	def transient_failure(source, destination):
		nonlocal attempts
		attempts += 1
		if attempts == 1:
			raise PermissionError("temporarily locked")
		replace(source, destination)

	monkeypatch.setattr(reviewcache.os, "replace", transient_failure)
	monkeypatch.setattr(reviewcache.time, "sleep", lambda _: None)

	reviewcache.write("/repo", "hash", _payload(), path=path, timestamp=10)

	assert attempts == 2
	assert reviewcache.read("/repo", "hash", path=path, now=11) is not None


def test_eviction_per_repo_keeps_only_most_recent_entries(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	reviewcache.write(
		"/other-repo",
		"other-hash",
		_payload(),
		path=path,
		timestamp=1.0,
	)
	for index in range(reviewcache.MAX_ENTRIES_PER_REPO + 5):
		reviewcache.write(
			"/repo",
			f"hash-{index}",
			_payload(),
			path=path,
			timestamp=float(index + 10),
		)

	stored = json.loads(path.read_text(encoding="utf-8"))["entries"]
	repo_entries = [
		e for e in stored if e["repo_root"] == reviewcache._repo_key("/repo")
	]
	other_entries = [
		e for e in stored if e["repo_root"] == reviewcache._repo_key("/other-repo")
	]

	assert len(repo_entries) == reviewcache.MAX_ENTRIES_PER_REPO
	assert {entry["diff_hash"] for entry in repo_entries} == {
		f"hash-{index}" for index in range(5, reviewcache.MAX_ENTRIES_PER_REPO + 5)
	}
	assert len(other_entries) == 1
	assert other_entries[0]["diff_hash"] == "other-hash"


def test_heavy_activity_in_other_repo_does_not_evict_recent_entry(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	digest_a = reviewcache.diff_hash("diff-a")
	reviewcache.write("/repo-a", digest_a, _payload(), path=path, timestamp=10.0)

	# Simulate 30 subsequent reviews in repo-b (exceeding old global cap of 20)
	for index in range(30):
		reviewcache.write(
			"/repo-b",
			f"hash-b-{index}",
			_payload(),
			path=path,
			timestamp=float(20 + index),
		)

	entry = reviewcache.read("/repo-a", digest_a, path=path, now=60.0)
	assert entry is not None
	assert entry.diff_hash == digest_a
	assert entry.review_payload == _payload()


def test_global_ceiling_bounds_total_entries_across_repos(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	total_repos = reviewcache.MAX_ENTRIES + 15
	for index in range(total_repos):
		reviewcache.write(
			f"/repo-{index}",
			f"hash-{index}",
			_payload(),
			path=path,
			timestamp=float(index),
		)

	stored = json.loads(path.read_text(encoding="utf-8"))["entries"]
	assert len(stored) == reviewcache.MAX_ENTRIES
	# Most recent MAX_ENTRIES repos should be retained
	expected_repos = {
		reviewcache._repo_key(f"/repo-{index}")
		for index in range(15, total_repos)
	}
	assert {entry["repo_root"] for entry in stored} == expected_repos


def test_byte_ceiling_preserves_valid_json_under_max_file_size(
	monkeypatch, tmp_path
) -> None:
	path = tmp_path / "review-cache.json"
	# Set a tiny byte limit to test bounding
	monkeypatch.setattr(reviewcache, "MAX_FILE_SIZE", 300)

	for index in range(10):
		reviewcache.write(
			f"/repo-{index}",
			f"hash-{index}",
			_payload(),
			path=path,
			timestamp=float(index),
		)

	assert path.stat().st_size <= 300
	data = json.loads(path.read_text(encoding="utf-8"))
	assert "entries" in data
	assert isinstance(data["entries"], list)


