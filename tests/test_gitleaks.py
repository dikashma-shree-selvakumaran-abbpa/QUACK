"""Unit tests for quack.gitleaks (report parsing + merge, no binary needed)."""

from __future__ import annotations

import json

from quack import gitleaks
from quack.tier1 import Finding


def test_parse_report_maps_fields() -> None:
	raw = json.dumps(
		[
			{
				"RuleID": "aws-access-token",
				"File": "src/config.py",
				"StartLine": 12,
				"Secret": "AKIA1234567890ABCDEF",
			}
		]
	)
	findings = gitleaks._parse_report(raw)
	assert len(findings) == 1
	finding = findings[0]
	assert finding.check == "secrets"
	assert finding.severity == "error"
	assert finding.path == "src/config.py"
	assert finding.line == 12
	assert finding.message == "gitleaks: aws-access-token"
	assert finding.match == "AKIA1234567890ABCDEF"


def test_parse_report_tolerates_junk() -> None:
	assert gitleaks._parse_report("") == []
	assert gitleaks._parse_report("not json") == []
	assert gitleaks._parse_report(json.dumps({"not": "a list"})) == []


def test_merge_dedupes_same_path_and_line() -> None:
	builtin = [Finding("secrets", "error", "a.py", 1, "AWS access key id")]
	external = [
		Finding("secrets", "error", "a.py", 1, "gitleaks: aws-access-token"),
		Finding("secrets", "error", "b.py", 5, "gitleaks: generic-api-key"),
	]
	merged = gitleaks.merge(builtin, external)
	# a.py:1 collapses to the built-in; b.py:5 is added.
	assert len(merged) == 2
	assert merged[0].message == "AWS access key id"
	assert merged[1].path == "b.py"


def test_merge_with_empty_external_is_identity() -> None:
	builtin = [Finding("secrets", "error", "a.py", 1, "AWS access key id")]
	assert gitleaks.merge(builtin, []) == builtin


def test_ensure_installed_noop_when_present(monkeypatch) -> None:
	monkeypatch.setattr(gitleaks, "available", lambda: True)
	installed, message = gitleaks.ensure_installed()
	assert installed is True
	assert "already installed" in message


def test_ensure_installed_reports_when_no_installer(monkeypatch) -> None:
	monkeypatch.setattr(gitleaks, "available", lambda: False)
	monkeypatch.setattr(gitleaks, "_pick_installer", lambda: None)
	installed, message = gitleaks.ensure_installed()
	assert installed is False
	assert "no supported package manager" in message

