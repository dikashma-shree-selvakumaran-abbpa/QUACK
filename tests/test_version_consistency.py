"""Pins the version number carried by the package, pyproject and the VS Code client."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import quack

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_is_consistent_across_package_pyproject_and_extension() -> None:
	package_version = quack.__version__

	pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
	pyproject_version = pyproject["project"]["version"]
	assert package_version == pyproject_version, (
		f"version mismatch: quack.__version__={package_version!r} "
		f"pyproject.toml={pyproject_version!r}"
	)

	manifest = REPO_ROOT / "clients" / "vscode" / "package.json"
	if not manifest.exists():
		pytest.skip("clients/vscode/package.json is not checked out")

	extension_version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
	assert package_version == extension_version, (
		f"version mismatch: quack.__version__={package_version!r} "
		f"pyproject.toml={pyproject_version!r} "
		f"clients/vscode/package.json={extension_version!r}"
	)
