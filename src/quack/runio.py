"""Thin test-runner subprocess adapter.

This is one of the few modules permitted to call subprocess directly. It knows
how to invoke exactly two whitelisted command shapes and nothing else:

* ``pytest <paths> -x --tb=short -q``
* ``dotnet test <project> --no-build --filter "<filter>" -v minimal``

The argument lists are always built here from already-validated inputs. No
shell is ever used (``shell=False``), so nothing constructible from tool input
can inject additional commands. Every failure mode is normalised to an
``(exit_code, output)`` tuple; this module never raises for a runner error.
"""

from __future__ import annotations

import subprocess

DEFAULT_TIMEOUT_S = 180


def run_pytest(
	paths: list[str], timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[int, str]:
	"""Run ``pytest <paths> -x --tb=short -q`` and return (exit_code, output)."""
	return _run(["pytest", *paths, "-x", "--tb=short", "-q"], timeout_s)


def run_dotnet_test(
	project: str, test_filter: str | None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[int, str]:
	"""Run ``dotnet test <project> --no-build [--filter ...] -v minimal``."""
	args = ["dotnet", "test", project, "--no-build"]
	if test_filter:
		args += ["--filter", test_filter]
	args += ["-v", "minimal"]
	return _run(args, timeout_s)


def _run(args: list[str], timeout_s: int) -> tuple[int, str]:
	"""Execute a fixed argument list without a shell. Never raises."""
	try:
		result = subprocess.run(
			args,
			capture_output=True,
			text=True,
			timeout=timeout_s,
		)
	except FileNotFoundError:
		return (127, f"<runner not found: {args[0]}>")
	except subprocess.TimeoutExpired:
		return (-1, f"<timed out after {timeout_s}s>")
	output = (result.stdout or "") + (result.stderr or "")
	return (result.returncode, output)
