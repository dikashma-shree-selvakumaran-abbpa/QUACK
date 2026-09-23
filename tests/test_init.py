from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from quack import cli, templates


def test_init_creates_sonar_artifacts_without_overwriting_existing_files(
	monkeypatch,
) -> None:
	monkeypatch.setattr(cli, "_install_hooks", lambda use_local: None)
	runner = CliRunner()

	with runner.isolated_filesystem():
		result = runner.invoke(cli.main, ["init"])
		assert result.exit_code == 0
		for relative_path, content in templates.ARTIFACTS.items():
			assert open(relative_path, encoding="utf-8").read() == content

		existing = ".github/skills/sonar-check/SKILL.md"
		with open(existing, "w", encoding="utf-8") as handle:
			handle.write("developer-owned")

		result = runner.invoke(cli.main, ["init"])
		assert result.exit_code == 0
		assert open(existing, encoding="utf-8").read() == "developer-owned"
		assert "preserving existing" in result.output


def test_local_hook_setup_replaces_duplicate_quack_stanza(tmp_path: Path) -> None:
	config = tmp_path / ".pre-commit-config.yaml"
	config.write_text(
		"repos:\n"
		"- repo: https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK\n"
		"  rev: v0.3.0\n"
		"  hooks:\n"
		"  - id: quack\n"
		"- repo: local\n"
		"  hooks:\n"
		"  - id: custom\n"
		"    entry: custom\n",
		encoding="utf-8",
	)

	cli._upsert_local_stanza(config)

	text = config.read_text(encoding="utf-8")
	assert text.count("repo: local") == 1
	assert "repo: https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK" not in text
	assert text.count("- id: quack\n") == 1
	assert text.count("- id: quack-agent\n") == 1
	assert "id: custom" in text
