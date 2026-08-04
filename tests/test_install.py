"""Tests for `quack install`: it must wire BOTH hook surfaces.

pre-commit = local checks only (`quack`); pre-push = AI review / agent
(`quack-agent`). The config it writes must declare both hook ids with the
correct stages, and it must install both hook types — with the pre-push
install degrading gracefully if it fails.
"""

from __future__ import annotations

import subprocess

import yaml
from click.testing import CliRunner

from quack import cli


def _run_install(monkeypatch, tmp_path, args, run_side_effect):
	# Keep the one-time gitleaks bootstrap out of these tests.
	monkeypatch.setattr(cli.gitleaks, "ensure_installed", lambda: (True, "ok"))
	monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/pre-commit")

	calls = []

	def fake_run(cmd, *a, **kw):
		calls.append(cmd)
		result = run_side_effect(cmd)
		if result is not None:
			raise result
		return subprocess.CompletedProcess(cmd, 0)

	monkeypatch.setattr(cli.subprocess, "run", fake_run)

	runner = CliRunner()
	with runner.isolated_filesystem(temp_dir=tmp_path):
		result = runner.invoke(cli.main, ["install", *args])
		config = yaml.safe_load(
			open(".pre-commit-config.yaml", encoding="utf-8").read()
		)
	return result, config, calls


def _hook_ids_with_stages(config):
	"""Map hook id -> stages across every repo/hook in the config."""
	ids = {}
	for repo in config.get("repos", []):
		for hook in repo.get("hooks", []):
			ids[hook["id"]] = hook.get("stages")
	return ids


def test_local_install_writes_both_hooks_with_correct_stages(monkeypatch, tmp_path):
	result, config, _ = _run_install(
		monkeypatch, tmp_path, ["--local"], lambda cmd: None
	)

	assert result.exit_code == 0
	ids = _hook_ids_with_stages(config)
	assert ids["quack"] == ["pre-commit"]
	assert ids["quack-agent"] == ["pre-push"]


def test_precommit_install_writes_both_hook_ids(monkeypatch, tmp_path):
	result, config, _ = _run_install(
		monkeypatch, tmp_path, [], lambda cmd: None
	)

	assert result.exit_code == 0
	hook_ids = {
		hook["id"]
		for repo in config["repos"]
		for hook in repo.get("hooks", [])
	}
	assert hook_ids == {"quack", "quack-agent"}


def test_install_installs_both_hook_types(monkeypatch, tmp_path):
	result, _, calls = _run_install(
		monkeypatch, tmp_path, ["--local"], lambda cmd: None
	)

	assert result.exit_code == 0
	assert ["pre-commit", "install"] in calls
	assert ["pre-commit", "install", "--hook-type", "pre-push"] in calls


def test_pre_push_install_failure_degrades_gracefully(monkeypatch, tmp_path):
	def side_effect(cmd):
		if "--hook-type" in cmd:
			return subprocess.CalledProcessError(1, cmd)
		return None

	result, _, calls = _run_install(
		monkeypatch, tmp_path, ["--local"], side_effect
	)

	# The pre-push failure must warn but not abort, and must not undo the
	# pre-commit install that already succeeded.
	assert result.exit_code == 0
	assert ["pre-commit", "install"] in calls
	assert ["pre-commit", "install", "--hook-type", "pre-push"] in calls
	assert "pre-push" in result.output
