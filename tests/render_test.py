"""Output-shape tests for quack.render.

These verify the two environment-driven fallbacks required by the design:
NO_COLOR and non-TTY (piped) output must both emit plain text with no ANSI
escape codes, while a forced terminal still emits color (so the NO_COLOR test
actually proves something).
"""

from __future__ import annotations

from types import SimpleNamespace

from quack import render

ANSI = "\x1b["


def _finding(severity: str = "error") -> SimpleNamespace:
	return SimpleNamespace(
		severity=severity,
		check="secrets",
		path="src/config.py",
		line=12,
		message="hardcoded secret",
	)


def _plan() -> SimpleNamespace:
	return SimpleNamespace(
		runner_commands=["pytest tests/test_config.py -x"],
		untested_sources=["src/config.py"],
		dotnet_hint=False,
	)


def _review() -> SimpleNamespace:
	return SimpleNamespace(
		risk="medium",
		one_liner="Looks mostly safe.",
		reasons=["boundary condition changed"],
		tests_to_run=["pytest tests/test_transform.py"],
		missing_tests=["src/transform.py"],
	)


def _emit_full_report() -> None:
	render.report(
		files=1,
		added=3,
		removed=1,
		findings=[_finding("warn")],
		plan=_plan(),
		ai=_review(),
		model="openai/gpt-4o-mini",
		blocked=False,
	)


def test_no_color_env_strips_ansi(monkeypatch, capsys) -> None:
	# Force a terminal so color WOULD be emitted, then prove NO_COLOR wins.
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("NO_COLOR", "1")

	_emit_full_report()

	out = capsys.readouterr().out
	assert ANSI not in out
	assert "quack" in out
	assert "Test guidance" in out


def test_non_tty_pipe_has_no_ansi(monkeypatch, capsys) -> None:
	# capsys replaces stdout with a non-TTY buffer (like `quack check | cat`).
	monkeypatch.delenv("FORCE_COLOR", raising=False)
	monkeypatch.delenv("NO_COLOR", raising=False)

	_emit_full_report()

	out = capsys.readouterr().out
	assert ANSI not in out
	assert "openai/gpt-4o-mini" in out


def test_forced_terminal_emits_ansi(monkeypatch, capsys) -> None:
	# Contrast case: with a forced terminal and no NO_COLOR, color is emitted,
	# proving the module does colorize (so the NO_COLOR assertion is meaningful).
	monkeypatch.delenv("NO_COLOR", raising=False)
	monkeypatch.setenv("FORCE_COLOR", "1")

	_emit_full_report()

	out = capsys.readouterr().out
	assert ANSI in out


def test_primitives_plain_under_no_color(monkeypatch, capsys) -> None:
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("NO_COLOR", "1")

	render.clean("all clean")
	render.command("pytest -x")
	render.metadata("meta")

	out = capsys.readouterr().out
	assert ANSI not in out
	assert "all clean" in out


def test_banner_install_skipped_when_piped(monkeypatch, capsys) -> None:
	# capsys stdout is a non-TTY buffer (like `quack install | tee log`).
	monkeypatch.delenv("FORCE_COLOR", raising=False)
	monkeypatch.delenv("NO_COLOR", raising=False)

	render.banner_install()

	assert capsys.readouterr().out == ""


def test_banner_install_plain_under_no_color(monkeypatch, capsys) -> None:
	# Forced terminal + NO_COLOR: banner still shows, but as plain text.
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("NO_COLOR", "1")

	render.banner_install()

	out = capsys.readouterr().out
	assert ANSI not in out
	assert "installed -- your commits are now protected." in out


def test_banner_install_colored_on_forced_terminal(monkeypatch, capsys) -> None:
	monkeypatch.delenv("NO_COLOR", raising=False)
	monkeypatch.setenv("FORCE_COLOR", "1")

	render.banner_install()

	out = capsys.readouterr().out
	assert ANSI in out
	assert "installed -- your commits are now protected." in out
