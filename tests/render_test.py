"""Output-shape tests for quack.render.

These verify the two environment-driven fallbacks required by the design:
NO_COLOR and non-TTY (piped) output must both emit plain text with no ANSI
escape codes, while a forced terminal still emits color (so the NO_COLOR test
actually proves something).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.console import Console

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
	assert "pytest -x" in out


def test_thinking_yields_and_returns_normally(capsys) -> None:
	with render.thinking("reviewing changes..."):
		result = "complete"

	out = capsys.readouterr().out
	assert result == "complete"
	assert "reviewing changes..." in out


def test_thinking_clears_on_exception_without_suppressing(capsys) -> None:
	with pytest.raises(RuntimeError, match="review failed"):
		with render.thinking("reviewing changes..."):
			raise RuntimeError("review failed")

	assert "reviewing changes..." in capsys.readouterr().out


def test_thinking_non_tty_has_no_ansi(capsys) -> None:
	with render.thinking("reviewing changes..."):
		pass

	out = capsys.readouterr().out
	assert ANSI not in out
	assert out == "reviewing changes...\n"


def test_thinking_no_color_has_no_ansi(monkeypatch, capsys) -> None:
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("NO_COLOR", "1")

	with render.thinking("reviewing changes..."):
		pass

	out = capsys.readouterr().out
	assert ANSI not in out
	assert out == "reviewing changes...\n"


def _agent_result_with_patch() -> SimpleNamespace:
	return SimpleNamespace(
		summary="1 test failing",
		tests_run=["pytest tests/test_transform.py"],
		failures=[
			{
				"test": "tests/test_transform.py::test_scale",
				"diagnosis": "off-by-one in the boundary check",
			}
		],
		proposed_patch="--- a/src/transform.py\n+++ b/src/transform.py\n@@\n-    if x > n:\n+    if x >= n:",
		proposed_new_tests=None,
	)


def test_agent_report_default_coaches_and_hides_patch(monkeypatch, capsys) -> None:
	monkeypatch.delenv("NO_COLOR", raising=False)
	monkeypatch.delenv("FORCE_COLOR", raising=False)

	render.agent_report(_agent_result_with_patch())

	out = capsys.readouterr().out
	# Understanding is always shown.
	assert "off-by-one in the boundary check" in out
	# Coaching hint appears, patch content does not.
	assert "run `quack agent --fly`" in out
	assert "if x >= n" not in out
	assert "PROPOSED -- not applied" not in out
	assert "git apply" not in out


def test_agent_report_fly_reveals_patch(monkeypatch, capsys) -> None:
	monkeypatch.delenv("NO_COLOR", raising=False)
	monkeypatch.delenv("FORCE_COLOR", raising=False)

	render.agent_report(_agent_result_with_patch(), fly=True)

	out = capsys.readouterr().out
	# Understanding still shown in fly mode.
	assert "off-by-one in the boundary check" in out
	# Patch and apply hint are revealed; coaching line is not.
	assert "if x >= n" in out
	assert "PROPOSED -- not applied" in out
	assert "git apply" in out
	assert "run `quack agent --fly`" not in out


def test_agent_report_no_patch_shows_no_patch_line(monkeypatch, capsys) -> None:
	monkeypatch.delenv("NO_COLOR", raising=False)
	monkeypatch.delenv("FORCE_COLOR", raising=False)

	result = SimpleNamespace(
		summary="all green",
		tests_run=["pytest tests/test_transform.py"],
		failures=[{"test": "tests/test_transform.py::test_scale", "diagnosis": "flaky ordering"}],
		proposed_patch=None,
		proposed_new_tests=None,
	)

	for fly in (False, True):
		render.agent_report(result, fly=fly)
		out = capsys.readouterr().out
		assert "flaky ordering" in out
		assert "run `quack agent --fly`" not in out
		assert "PROPOSED -- not applied" not in out


def test_quack_alarm_lists_blocking_lines(capsys) -> None:
	render.report(
		files=1,
		added=4,
		removed=0,
		findings=[_finding("error")],
		plan=None,
		ai=None,
		blocked=True,
	)
	out = capsys.readouterr().out
	assert "QUACK!!!!" in out
	assert "check line" not in out
	assert "#12" not in out


def test_long_command_folds_without_losing_content() -> None:
	command = "dotnet test tests/GfxKernel.Tests/GfxKernel.Tests.csproj --filter FullyQualifiedName~CriticalRenderingPath"
	console = Console(width=80, force_terminal=False, color_system=None, record=True)

	console.print(render._command_text(command))

	out = console.export_text()
	assert "\n" in out
	assert "".join(out.split()) == "".join(command.split())


def test_quack_alarm_absent_when_not_blocked(capsys) -> None:
	render.report(
		files=1,
		added=4,
		removed=0,
		findings=[_finding("warn")],
		plan=None,
		ai=None,
		blocked=False,
	)
	assert "QUACK!!!!" not in capsys.readouterr().out


def test_install_banner_prints_wordmark(capsys) -> None:
	render.install_banner()
	out = capsys.readouterr().out
	assert "QUACK" in out
	assert "quality gate" in out


def test_install_banner_no_ansi_under_no_color(monkeypatch, capsys) -> None:
	monkeypatch.setenv("FORCE_COLOR", "1")
	monkeypatch.setenv("NO_COLOR", "1")
	render.install_banner()
	out = capsys.readouterr().out
	assert ANSI not in out
	assert "QUACK" in out
