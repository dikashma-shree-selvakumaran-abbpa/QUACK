"""Unit tests for quack.instructions (repo-local instructions loader)."""

from __future__ import annotations

from pathlib import Path

from quack import instructions


def test_missing_file_returns_none(tmp_path: Path) -> None:
	assert instructions.load(tmp_path) is None


def test_reads_single_file(tmp_path: Path) -> None:
	(tmp_path / "instructions.md").write_text("be careful", encoding="utf-8")
	assert instructions.load(tmp_path) == "be careful"


def test_precedence_earlier_wins(tmp_path: Path) -> None:
	(tmp_path / ".quack").mkdir()
	(tmp_path / ".quack" / "instructions.md").write_text(
		"quack wins", encoding="utf-8"
	)
	(tmp_path / "instructions.md").write_text("root loses", encoding="utf-8")
	(tmp_path / ".github").mkdir()
	(tmp_path / ".github" / "copilot-instructions.md").write_text(
		"copilot loses", encoding="utf-8"
	)

	assert instructions.load(tmp_path) == "quack wins"


def test_precedence_second_when_first_absent(tmp_path: Path) -> None:
	(tmp_path / "instructions.md").write_text("root wins", encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("agents loses", encoding="utf-8")

	assert instructions.load(tmp_path) == "root wins"


def test_truncation_appends_marker(tmp_path: Path) -> None:
	(tmp_path / "instructions.md").write_text("x" * 5000, encoding="utf-8")

	result = instructions.load(tmp_path, max_chars=100)

	assert result is not None
	assert result.startswith("x" * 100)
	assert result.endswith("... [instructions truncated]")
	assert "x" * 101 not in result


def test_no_truncation_when_within_limit(tmp_path: Path) -> None:
	(tmp_path / "instructions.md").write_text("short", encoding="utf-8")

	result = instructions.load(tmp_path, max_chars=100)

	assert result == "short"
	assert "truncated" not in result


def test_unreadable_file_returns_none_without_raising(
	tmp_path: Path,
	monkeypatch,
) -> None:
	(tmp_path / "instructions.md").write_text("secret", encoding="utf-8")

	def boom(*_args, **_kwargs):
		raise PermissionError("nope")

	monkeypatch.setattr(Path, "read_text", boom)

	assert instructions.load(tmp_path) is None


def test_missing_directory_returns_none(tmp_path: Path) -> None:
	assert instructions.load(tmp_path / "does-not-exist") is None
