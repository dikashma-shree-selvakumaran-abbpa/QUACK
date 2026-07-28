"""Optional gitleaks integration.

quack ships a small, high-precision set of built-in secret patterns in
:mod:`quack.tier1`. When the `gitleaks <https://github.com/gitleaks/gitleaks>`_
binary is available on ``PATH``, this adapter layers its hundreds of tuned,
entropy-aware rules on top -- turning Tier 1 into a best-in-class scanner
without adding a hard dependency.

This is one of the few modules permitted to call subprocess directly. It is
fully fail-open: if gitleaks is missing, errors, times out, or emits output we
cannot parse, :func:`scan_staged` returns ``[]`` and quack proceeds on its
built-in patterns alone. It never raises.

gitleaks is invoked as::

	gitleaks protect --staged --no-banner --report-format json \
		--report-path <tmpfile>

which scans the *staged* diff only -- exactly the surface quack cares about.
An exit code of 0 (clean) or 1 (leaks found) is expected; anything else is
treated as a tool error and ignored.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .tier1 import Finding

GITLEAKS_TIMEOUT_S = 10
INSTALL_TIMEOUT_S = 300


def available() -> bool:
	"""Return True if the gitleaks binary is discoverable on ``PATH``."""
	return shutil.which("gitleaks") is not None


def ensure_installed() -> tuple[bool, str]:
	"""Best-effort install of gitleaks via the platform package manager.

	Returns ``(installed, message)``. Never raises. Intended to be called once
	during ``quack install`` so that every later commit gets the power mode for
	free. If gitleaks is already present, or no supported installer is found,
	it reports that and leaves the system untouched.
	"""
	if available():
		return True, "gitleaks already installed"

	installer = _pick_installer()
	if installer is None:
		return False, "no supported package manager found (install gitleaks manually)"

	name, args = installer
	try:
		result = subprocess.run(
			args,
			capture_output=True,
			text=True,
			timeout=INSTALL_TIMEOUT_S,
		)
	except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
		return False, f"{name} install did not complete"

	if result.returncode != 0:
		return False, f"{name} install failed (exit {result.returncode})"

	# PATH may not refresh in the current process; re-probe via shutil which
	# re-reads the environment, but success is best-confirmed by the exit code.
	return True, f"gitleaks installed via {name}"


def _pick_installer() -> tuple[str, list[str]] | None:
	"""Choose an available OS package manager and its install command."""
	if sys.platform.startswith("win") and shutil.which("winget"):
		return (
			"winget",
			[
				"winget",
				"install",
				"--id",
				"Gitleaks.Gitleaks",
				"-e",
				"--accept-source-agreements",
				"--accept-package-agreements",
			],
		)
	if sys.platform == "darwin" and shutil.which("brew"):
		return ("brew", ["brew", "install", "gitleaks"])
	# Linux: prefer brew if present (distro packages vary too much to assume).
	if shutil.which("brew"):
		return ("brew", ["brew", "install", "gitleaks"])
	return None


def scan_staged(repo_root: str | Path) -> list[Finding]:
	"""Scan the staged diff with gitleaks and return :class:`Finding` objects.

	Returns an empty list when gitleaks is unavailable or on any error, so the
	caller can always merge the result unconditionally (fail-open).
	"""
	if not available():
		return []

	with tempfile.TemporaryDirectory() as tmp:
		report_path = Path(tmp) / "gitleaks.json"
		args = [
			"gitleaks",
			"protect",
			"--staged",
			"--no-banner",
			"--report-format",
			"json",
			"--report-path",
			str(report_path),
		]
		try:
			result = subprocess.run(
				args,
				cwd=str(repo_root),
				capture_output=True,
				text=True,
				timeout=GITLEAKS_TIMEOUT_S,
			)
		except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
			return []

		# 0 = clean, 1 = leaks found. Anything else is a tool error.
		if result.returncode not in (0, 1):
			return []

		try:
			raw = report_path.read_text(encoding="utf-8")
		except OSError:
			return []

	return _parse_report(raw)


def _parse_report(raw: str) -> list[Finding]:
	"""Convert gitleaks JSON report text into a list of Findings. Never raises."""
	if not raw.strip():
		return []
	try:
		data = json.loads(raw)
	except (ValueError, TypeError):
		return []
	if not isinstance(data, list):
		return []

	findings: list[Finding] = []
	for entry in data:
		if not isinstance(entry, dict):
			continue
		path = entry.get("File") or entry.get("file") or ""
		if not path:
			continue
		line = entry.get("StartLine") or entry.get("startLine") or 0
		rule = entry.get("RuleID") or entry.get("Description") or "secret"
		secret = entry.get("Secret") or entry.get("Match") or ""
		findings.append(
			Finding(
				check="secrets",
				severity="error",
				path=str(path),
				line=int(line) if isinstance(line, int) else 0,
				message=f"gitleaks: {rule}",
				match=str(secret),
			)
		)
	return findings


def merge(
	builtin: list[Finding], external: list[Finding]
) -> list[Finding]:
	"""Merge built-in and gitleaks findings, de-duplicating by (path, line).

	A built-in secret finding and a gitleaks finding on the same line describe
	the same leak; keep only one so the report is not noisy. Built-in findings
	win because their messages are curated.
	"""
	seen: set[tuple[str, int]] = {
		(f.path, f.line) for f in builtin if f.check == "secrets"
	}
	merged = list(builtin)
	for finding in external:
		key = (finding.path, finding.line)
		if key in seen:
			continue
		seen.add(key)
		merged.append(finding)
	return merged
