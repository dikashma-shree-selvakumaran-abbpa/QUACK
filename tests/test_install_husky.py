"""Tests for `quack install` when git reads hooks from elsewhere.

husky sets core.hooksPath, which makes git ignore .git/hooks entirely --
`pre-commit install` refuses, and quack previously reported success while
installing nothing. quack must detect this, append to the husky hook instead,
and never touch a tracked file without consent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from quack import cli


def _install(monkeypatch, tmp_path, hooks_path, args, existing_hook=None):
	monkeypatch.setattr(cli.gitleaks, "ensure_installed", lambda: (True, "ok"))
	monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/pre-commit")

	def fake_run(cmd, *a, **kw):
		if cmd[:3] == ["git", "config", "core.hooksPath"]:
			return subprocess.CompletedProcess(cmd, 0, stdout=hooks_path)
		return subprocess.CompletedProcess(cmd, 0, stdout="")

	monkeypatch.setattr(cli.subprocess, "run", fake_run)

	runner = CliRunner()
	with runner.isolated_filesystem(temp_dir=tmp_path):
		if existing_hook is not None:
			Path(".husky").mkdir()
			Path(".husky/pre-commit").write_text(existing_hook, encoding="utf-8")
		result = runner.invoke(cli.main, ["install", *args])
		hook = Path(".husky/pre-commit")
		content = hook.read_text(encoding="utf-8") if hook.exists() else None
		config_written = Path(".pre-commit-config.yaml").exists()
	return result, content, config_written


HUSKY_HOOK = "#!/usr/bin/env sh\npnpm run lint-staged\n"


def test_husky_detected_appends_quack_and_skips_precommit_config(
	monkeypatch, tmp_path
):
	result, content, config_written = _install(
		monkeypatch, tmp_path, ".husky/_", ["--local", "--yes"], HUSKY_HOOK
	)

	assert result.exit_code == 0
	assert "quack check" in content
	# Writing a pre-commit config here would leave an artifact that nothing
	# ever executes, which is what made the old failure so misleading.
	assert config_written is False


def test_existing_husky_content_is_preserved(monkeypatch, tmp_path):
	_, content, _ = _install(
		monkeypatch, tmp_path, ".husky/_", ["--local", "--yes"], HUSKY_HOOK
	)

	assert "pnpm run lint-staged" in content
	assert content.index("pnpm run lint-staged") < content.index("quack check")


def test_running_twice_does_not_duplicate_the_block(monkeypatch, tmp_path):
	_, once, _ = _install(
		monkeypatch, tmp_path, ".husky/_", ["--local", "--yes"], HUSKY_HOOK
	)
	# Feed the already-installed hook back in, as a second run would find it.
	_, twice, _ = _install(
		monkeypatch, tmp_path, ".husky/_", ["--local", "--yes"], once
	)

	assert twice.count(cli.QUACK_HOOK_START) == 1
	assert twice.count("quack check") == 1


def test_declining_the_prompt_writes_nothing(monkeypatch, tmp_path):
	# No --yes, and CliRunner sends empty stdin, so confirm() takes the
	# default (N). Nothing tracked may change without explicit consent.
	result, content, config_written = _install(
		monkeypatch, tmp_path, ".husky/_", ["--local"], HUSKY_HOOK
	)

	assert result.exit_code == 1
	assert content == HUSKY_HOOK
	assert config_written is False


def test_unknown_hooks_path_refuses_with_manual_instructions(monkeypatch, tmp_path):
	result, _, config_written = _install(
		monkeypatch, tmp_path, "/some/custom/hooks", ["--local", "--yes"]
	)

	assert result.exit_code == 1
	assert "quack check" in result.output
	assert config_written is False


def test_banner_not_shown_when_nothing_installed(monkeypatch, tmp_path):
	result, _, _ = _install(
		monkeypatch, tmp_path, "/some/custom/hooks", ["--local", "--yes"]
	)

	# The banner claims "your commits just got a quality gate" -- it must not
	# appear when no hook was installed.
	assert "quality gate" not in result.output
