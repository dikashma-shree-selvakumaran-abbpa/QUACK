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


def test_eviction_keeps_only_most_recent_entries(tmp_path) -> None:
	path = tmp_path / "review-cache.json"
	for index in range(reviewcache.MAX_ENTRIES + 5):
		reviewcache.write(
			"/repo",
			f"hash-{index}",
			_payload(),
			path=path,
			timestamp=float(index),
		)

	stored = json.loads(path.read_text(encoding="utf-8"))["entries"]
	assert len(stored) == reviewcache.MAX_ENTRIES
	assert {entry["diff_hash"] for entry in stored} == {
		f"hash-{index}" for index in range(5, 25)
	}
