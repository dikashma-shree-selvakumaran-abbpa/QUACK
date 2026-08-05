"""Unit tests for the quack.gitio adapter (no real git required)."""

from __future__ import annotations

from quack import gitio


def test_range_delta_parses_range_via_existing_parser(monkeypatch) -> None:
	name_status = "M\tsrc/thing.py\n"
	numstat = "3\t1\tsrc/thing.py\n"
	unified = (
		"diff --git a/src/thing.py b/src/thing.py\n"
		"--- a/src/thing.py\n"
		"+++ b/src/thing.py\n"
		"@@ -1,1 +1,3 @@\n"
		"-x = 1\n"
		"+x = 1\n"
		"+y = 2\n"
		"+z = 3\n"
	)

	calls: list[list[str]] = []

	def fake_run_git(args: list[str]) -> str:
		calls.append(args)
		if "--name-status" in args:
			return name_status
		if "--numstat" in args:
			return numstat
		return unified

	monkeypatch.setattr(gitio, "_run_git", fake_run_git)

	delta = gitio.range_delta("origin/main")

	# The range is substituted for --cached in all three invocations.
	assert all("origin/main..HEAD" in args for args in calls)
	assert not any("--cached" in args for args in calls)
	assert [f.path for f in delta.files] == ["src/thing.py"]
	assert delta.files[0].added == 3
	assert delta.files[0].removed == 1


def test_upstream_ref_returns_none_without_upstream(monkeypatch) -> None:
	# _run_git already returns "" on any git failure (no upstream configured).
	monkeypatch.setattr(gitio, "_run_git", lambda args: "")
	assert gitio.upstream_ref() is None


def test_upstream_ref_does_not_raise_on_git_failure(monkeypatch) -> None:
	def boom(args: list[str]) -> str:  # pragma: no cover - guarded below
		raise RuntimeError("git exploded")

	# Even if the underlying runner raised, upstream_ref must not propagate.
	# _run_git itself swallows subprocess errors, but assert the contract.
	monkeypatch.setattr(gitio, "_run_git", lambda args: "")
	assert gitio.upstream_ref() is None


def test_upstream_ref_returns_tracking_branch(monkeypatch) -> None:
	monkeypatch.setattr(gitio, "_run_git", lambda args: "origin/main\n")
	assert gitio.upstream_ref() == "origin/main"
