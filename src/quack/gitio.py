"""Thin git adapter.

This is one of the few modules permitted to call subprocess directly.
Everything else should take data in and return data out.
"""

from __future__ import annotations

import subprocess


def staged_files() -> list[str]:
	"""Return the list of staged file paths (added/copied/modified/renamed).

	Returns an empty list if git is unavailable or this is not a repo.
	"""
	try:
		result = subprocess.run(
			["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
			capture_output=True,
			text=True,
			check=True,
		)
	except (subprocess.CalledProcessError, FileNotFoundError):
		return []
	return [line for line in result.stdout.splitlines() if line.strip()]
