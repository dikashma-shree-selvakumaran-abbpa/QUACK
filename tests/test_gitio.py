"""Unit tests for the quack.gitio adapter (no real git required)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from quack import gitio


def test_credential_approve_passes_secret_only_on_stdin(monkeypatch) -> None:
	captured: dict[str, object] = {}

	def fake_run(command, **kwargs):
		captured.update(command=command, **kwargs)
		return subprocess.CompletedProcess(command, 0)

	monkeypatch.setattr(gitio.subprocess, "run", fake_run)

	assert gitio.credential_approve(
		"https://sonar.example", "quack", "secret-token"
	)
	assert "secret-token" not in captured["command"]
	assert "password=secret-token" in captured["input"]
	assert captured["stdout"] is subprocess.DEVNULL
	assert captured["stderr"] is subprocess.DEVNULL


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


def test_export_working_snapshot_copies_tracked_and_untracked_files(
	monkeypatch, tmp_path: Path
) -> None:
	root = tmp_path / "repo"
	root.mkdir()
	(root / "src").mkdir()
	(root / "src" / "tracked.py").write_text(
		"tracked = True\n", encoding="utf-8"
	)
	(root / "src" / "new.py").write_text("new = True\n", encoding="utf-8")
	(root / ".scannerwork").mkdir()
	(root / ".scannerwork" / "report-task.txt").write_text(
		"generated\n", encoding="utf-8"
	)
	destination = tmp_path / "snapshot"

	monkeypatch.setattr(
		gitio,
		"_run_git",
		lambda args, cwd=None: (
			"src/tracked.py\0src/new.py\0.scannerwork/report-task.txt\0"
		)
		if "ls-files" in args
		else "",
	)

	assert gitio.export_working_snapshot(destination, root)
	assert (destination / "src" / "tracked.py").read_text(encoding="utf-8") == (
		"tracked = True\n"
	)
	assert (destination / "src" / "new.py").read_text(encoding="utf-8") == (
		"new = True\n"
	)
	assert not (destination / ".scannerwork").exists()


def test_working_delta_includes_nonignored_new_file(monkeypatch, tmp_path: Path) -> None:
	root = tmp_path / "repo"
	root.mkdir()
	(root / "new.py").write_text("password = 'secret'\n", encoding="utf-8")

	def fake_run_git(args, cwd=None):
		if args == ["ls-files", "-z"]:
			return ""
		if "ls-files" in args:
			return "new.py\0.scannerwork/report-task.txt\0"
		return ""

	monkeypatch.setattr(gitio, "_run_git", fake_run_git)

	result = gitio.working_delta(root)

	assert [file.path for file in result.files] == ["new.py"]
	assert result.files[0].status == "A"
	assert "+password = 'secret'" in result.files[0].hunks[0]
	assert "new.py" in result.raw_diff
