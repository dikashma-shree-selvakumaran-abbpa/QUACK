"""Thin test-runner subprocess adapter.

This is one of the few modules permitted to call subprocess directly. It knows
how to invoke exactly four whitelisted command shapes and nothing else:

* ``pytest <paths> -x --tb=short -q``
* ``dotnet test <project> --no-build --filter "<filter>" -v minimal``
* whitelisted JS/TS test commands such as ``npm test`` and ``npx jest``
* ``sonar-scanner <validated -D properties>``

The argument lists are always built here from already-validated inputs. No
shell is ever used (``shell=False``), so nothing constructible from tool input
can inject additional commands. Every failure mode is normalised to an
``(exit_code, output)`` tuple; this module never raises for a runner error.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 180
# FrontEnd scanners may download analyzers and embedded runtimes on the first
# run; keep the default bounded long enough for that startup path.
SONAR_TIMEOUT_S = 180


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


def run_js_test(
	args: list[str], cwd: str | None = None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> tuple[int, str]:
	"""Run a whitelisted JS/TS test command list."""
	return _run(args, timeout_s, cwd=cwd)


def run_sonar_scanner(
	scanner: str | Path,
	properties: list[str],
	cwd: str | Path,
	timeout_s: int = SONAR_TIMEOUT_S,
) -> tuple[int, str]:
	"""Run a validated SonarScanner argument list without a shell."""
	if _is_dotnet_sonarscanner(scanner):
		return (
			126,
			"<runner unavailable: dotnet-sonarscanner requires a build and is not "
			"supported by the staged snapshot runner>",
		)
	if any(not isinstance(item, str) or not item.startswith("-D") for item in properties):
		return (126, "<invalid SonarScanner property>")
	return _run([str(scanner), *properties], timeout_s, cwd=str(cwd))


def _is_dotnet_sonarscanner(scanner: str | Path) -> bool:
	name = Path(scanner).name.casefold()
	return "dotnet-sonarscanner" in name or "sonarscanner.msbuild" in name


def _run(
	args: list[str], timeout_s: int, *, cwd: str | None = None
) -> tuple[int, str]:
	"""Execute a fixed argument list without a shell. Never raises."""
	try:
		result = subprocess.run(
			args,
			cwd=cwd,
			capture_output=True,
			text=True,
			timeout=timeout_s,
		)
	except FileNotFoundError:
		return (127, f"<runner not found: {args[0]}>")
	except subprocess.TimeoutExpired:
		return (-1, f"<timed out after {timeout_s}s>")
	except OSError:
		return (126, "<runner unavailable>")
	output = (result.stdout or "") + (result.stderr or "")
	return (result.returncode, output)
