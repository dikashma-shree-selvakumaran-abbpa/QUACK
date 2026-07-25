"""Single terminal output module for quack.

All terminal output MUST go through this module. No print() calls elsewhere.

Fixed color semantics:
	red    = blocking / error
	yellow = warning
	green  = clean
	cyan   = suggested command (copy-pasteable)
	dim    = metadata / info

Two shapes of output:

* The six primitive helpers (:func:`clean`, :func:`blocking`, :func:`warning`,
  :func:`command`, :func:`metadata`, :func:`info`) print a single styled line.
  Signatures are stable; simple callers (nothing-staged, install, agent, model)
  keep using them unchanged.
* :func:`report` renders the full ``quack check`` verdict as one rounded
  ``rich`` Panel: Tier 1 findings, test guidance and the AI verdict, separated
  by dim rules, with an advisory / BLOCKED footer.

``rich`` handles NO_COLOR and non-TTY output automatically: when stdout is a
pipe (or NO_COLOR is set) no ANSI escape codes are emitted, so CI logs and
``quack check | cat`` stay readable. Consoles are built per call so the current
``sys.stdout`` and environment are always honored.
"""

from __future__ import annotations

import os

from rich.box import ROUNDED
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# Fixed color semantics.
_BLOCK = "red"
_WARN = "yellow"
_CLEAN = "green"
_CMD = "cyan"
_META = "dim"

# Per-severity symbol + color for a Tier 1 finding row.
_SYMBOLS: dict[str, tuple[str, str]] = {
	"error": ("\u2717", _BLOCK),  # x
	"warn": ("\u26a0", _WARN),  # warning triangle
	"warning": ("\u26a0", _WARN),
	"ok": ("\u2713", _CLEAN),  # check mark
	"info": ("\u2713", _CLEAN),
}

# Risk level -> color.
_RISK_STYLES: dict[str, str] = {
	"low": _CLEAN,
	"medium": _WARN,
	"high": _BLOCK,
	"critical": _BLOCK,
}


def _console(stderr: bool = False) -> Console:
	"""Build a Console honoring the current stdout, NO_COLOR and TTY state.

	NO_COLOR is honored explicitly (not just delegated to rich) so it wins even
	when a terminal is forced -- a piped or NO_COLOR run emits no ANSI codes
	(rich's ``no_color`` only strips colors, so we also drop terminal mode to
	suppress bold/dim attribute escapes).
	"""
	no_color = bool(os.environ.get("NO_COLOR"))
	return Console(
		stderr=stderr,
		highlight=False,
		soft_wrap=True,
		emoji=False,
		no_color=no_color,
		force_terminal=False if no_color else None,
	)


# ---------------------------------------------------------------------------
# Primitive line helpers (stable signatures).
# ---------------------------------------------------------------------------


def clean(message: str) -> None:
	"""Print a clean/success line (green)."""
	_console().print(message, style=_CLEAN)


def warning(message: str) -> None:
	"""Print a warning line (yellow)."""
	_console().print(message, style=_WARN)


def blocking(message: str) -> None:
	"""Print a blocking line (red) to stderr."""
	_console(stderr=True).print(message, style=_BLOCK)


def command(message: str) -> None:
	"""Print a copy-pasteable suggested command (cyan)."""
	_console().print(message, style=_CMD)


def metadata(message: str) -> None:
	"""Print dim metadata."""
	_console().print(message, style=_META)


def info(message: str) -> None:
	"""Print a plain informational line."""
	_console().print(message)


# ---------------------------------------------------------------------------
# The quack check verdict panel.
# ---------------------------------------------------------------------------


def report(
	*,
	files: int,
	added: int,
	removed: int,
	findings,
	plan,
	ai,
	model: str = "",
	blocked: bool = False,
	duration: float = 0.0,
) -> None:
	"""Render the full ``quack check`` verdict as one rounded Panel.

	Parameters are duck-typed data (no imports of quack domain modules):

	* ``findings``  -- iterable of Tier 1 findings (``.severity``, ``.check``,
	  ``.path``, ``.line``, ``.message``).
	* ``plan``      -- test plan (``.runner_commands``, ``.untested_sources``,
	  ``.dotnet_hint``) or ``None``.
	* ``ai``        -- ``None`` (no AI section), ``("skipped", reason)``, or a
	  review result (``.risk``, ``.one_liner``, ``.reasons``,
	  ``.tests_to_run``, ``.missing_tests``).

	``duration`` is a placeholder for now.
	TODO: thread the real hook duration through from cli.check.
	"""
	sections: list[RenderableType] = []
	for section in (
		_findings_table(findings),
		_guidance_group(plan),
		_ai_group(ai, model),
	):
		if section is not None:
			sections.append(section)

	body: list[RenderableType] = []
	for index, section in enumerate(sections):
		if index:
			body.append(Rule(style=_META))
		body.append(section)
	if not body:
		body.append(Text("no findings", style=_META))

	title = f"quack - {files} file(s) - +{added}/-{removed} - {duration}s"
	if blocked:
		# The duck emoji is permitted ONLY on the blocked banner.
		subtitle = Text("\U0001f986 BLOCKED - fix and re-stage", style="bold red")
	else:
		subtitle = Text("advisory: commit allowed", style=_META)

	panel = Panel(
		Group(*body),
		box=ROUNDED,
		title=title,
		title_align="left",
		subtitle=subtitle,
		subtitle_align="left",
		padding=(0, 1),
	)
	_console().print(panel)


def _findings_table(findings) -> RenderableType | None:
	"""Aligned rows: symbol + check name + path:line + message."""
	rows = list(findings or [])
	if not rows:
		return None
	table = Table.grid(padding=(0, 2))
	table.add_column()  # symbol
	table.add_column()  # check name
	table.add_column()  # path:line
	table.add_column()  # message
	for finding in rows:
		symbol, style = _SYMBOLS.get(finding.severity, _SYMBOLS["warn"])
		table.add_row(
			Text(symbol, style=style),
			Text(finding.check, style=style),
			Text(f"{finding.path}:{finding.line}", style=_META),
			Text(finding.message),
		)
	return table


def _guidance_group(plan) -> RenderableType | None:
	"""Test guidance: runner commands (cyan) and NO TESTS FOUND (red)."""
	if plan is None:
		return None
	commands = list(getattr(plan, "runner_commands", []) or [])
	untested = list(getattr(plan, "untested_sources", []) or [])
	if not commands and not untested:
		return None
	lines: list[RenderableType] = [Text("Test guidance", style="bold")]
	# Commands sit on their own line with no leading decoration so they stay
	# triple-click copy-pasteable.
	for cmd in commands:
		lines.append(Text(cmd, style=_CMD))
	if getattr(plan, "dotnet_hint", False):
		lines.append(Text("(first run: build once with dotnet build)", style=_META))
	for source in untested:
		lines.append(Text(f"{source}: NO TESTS FOUND", style=_BLOCK))
	return Group(*lines)


def _ai_group(ai, model: str) -> RenderableType | None:
	"""AI verdict section, a skipped dim line, or nothing."""
	if ai is None:
		return None
	if isinstance(ai, tuple) and ai and ai[0] == "skipped":
		reason = ai[1] if len(ai) > 1 else "unknown"
		# Not a separate titled section -- just one dim line.
		return Text(f"AI: skipped ({reason})", style=_META)

	risk = str(getattr(ai, "risk", "unknown"))
	risk_style = _RISK_STYLES.get(risk, _WARN)
	header = Text.assemble(
		(f"AI - {model} - risk: ", "bold"),
		(risk.upper(), f"bold {risk_style}"),
	)
	lines: list[RenderableType] = [header]
	one_liner = getattr(ai, "one_liner", "")
	if one_liner:
		lines.append(Text(one_liner))
	for reason in getattr(ai, "reasons", []) or []:
		lines.append(Text(reason))
	for test in getattr(ai, "tests_to_run", []) or []:
		lines.append(Text(test, style=_CMD))
	for source in getattr(ai, "missing_tests", []) or []:
		lines.append(Text(f"{source}: no test covers this change", style=_WARN))
	return Group(*lines)


# ---------------------------------------------------------------------------
# The quack agent verdict.
# ---------------------------------------------------------------------------


def agent_report(result, fly: bool = False) -> None:
	"""Render a ``quack agent`` verdict.

	``result`` is duck-typed (``.summary``, ``.tests_run``, ``.failures`` with
	``test``/``diagnosis``, ``.proposed_patch``, ``.proposed_new_tests``).

	The Duck Way: coaching over crutches -- understanding is the default
	(waddle), the patch is opt-in via ``--fly``. By default the summary and
	failure diagnoses are shown, but the ready-to-paste patch is withheld
	behind a dim hint. With ``fly=True`` the full ``PROPOSED -- not applied``
	diff panel and ``git apply`` hint are revealed after the diagnosis.
	"""
	console = _console()

	console.print(Text(getattr(result, "summary", ""), style=_CLEAN))

	tests_run = list(getattr(result, "tests_run", []) or [])
	if tests_run:
		console.print(Text("tests run:", style=_META))
		for test in tests_run:
			console.print(Text(f"  {test}", style=_CMD))

	for failure in list(getattr(result, "failures", []) or []):
		test = failure.get("test", "?")
		diagnosis = failure.get("diagnosis", "")
		console.print(Text(f"FAIL {test}", style=_BLOCK))
		if diagnosis:
			console.print(Text(f"  {diagnosis}"))

	patch = getattr(result, "proposed_patch", None)
	if patch:
		if fly:
			syntax = Syntax(patch, "diff", theme="ansi_dark", word_wrap=True)
			console.print(
				Panel(
					syntax,
					box=ROUNDED,
					title="PROPOSED -- not applied",
					title_align="left",
					padding=(0, 1),
				)
			)
			console.print(Text("apply with: git apply <<'EOF' ... EOF", style=_META))
		else:
			console.print(
				Text(
					"A proposed fix is available. Try fixing it yourself from the "
					"diagnosis above, or run `quack agent --fly` to see the patch.",
					style=_META,
				)
			)

	new_tests = getattr(result, "proposed_new_tests", None)
	if new_tests:
		console.print(Text("proposed new test(s):", style=_WARN))
		console.print(Text(new_tests))