"""Single terminal output module for quack.

All terminal output MUST go through this module. No print() calls elsewhere.

Fixed color semantics:
	red    = blocking
	yellow = warning
	green  = clean
	cyan   = suggested command
	dim    = metadata

Respects the NO_COLOR env var and non-TTY output (plain fallback) so CI logs
stay readable.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console

_no_color = bool(os.environ.get("NO_COLOR"))
_console = Console(
	no_color=_no_color,
	highlight=False,
	soft_wrap=True,
)
_err_console = Console(
	stderr=True,
	no_color=_no_color,
	highlight=False,
	soft_wrap=True,
)


def _style(text: str, style: str) -> str:
	if _no_color or not _console.is_terminal:
		return text
	return f"[{style}]{text}[/{style}]"


def clean(message: str) -> None:
	"""Print a clean/success line (green)."""
	_console.print(_style(message, "green"))


def warning(message: str) -> None:
	"""Print a warning line (yellow)."""
	_console.print(_style(message, "yellow"))


def blocking(message: str) -> None:
	"""Print a blocking line (red) to stderr."""
	_err_console.print(_style(message, "red"))


def command(message: str) -> None:
	"""Print a copy-pasteable suggested command (cyan)."""
	_console.print(_style(message, "cyan"))


def metadata(message: str) -> None:
	"""Print dim metadata."""
	_console.print(_style(message, "dim"))


def info(message: str) -> None:
	"""Print a plain informational line."""
	_console.print(message)
