"""Repo-scoped Copilot artifacts installed by ``quack init``."""

from __future__ import annotations

SONAR_CHECK = """# Sonar check

Use the repository's read-only SonarQube MCP tools to inspect the project and
the current changed files. This check is deterministic: do not ask an LLM to
decide whether a Sonar response contains violations.

1. Identify the Sonar project key, server URL, and branch. If the analysis is
   stale, missing, or unavailable, report that explicitly.
2. Discover the available read-only issue, security-hotspot, measure, and
   duplication tools before calling them.
3. Scope issue queries to the changed components and open statuses. Return the
   issue key, rule, severity, file, line, and a short redacted message.
4. For `quack check`, project properties come from the staged index; do not use
   unstaged `sonar-project.properties` or IDE settings. The staged identity
   includes the scanner and effective settings.
5. Distinguish confirmed current findings from pre-existing or unverified
   server data. Cached scanner results must re-query MCP and require matching
   analysis correlation; otherwise fail open. Never treat unavailable
   infrastructure as a clean result.
6. Do not call write-capable tools, expose tokens, or modify files. Do not
   claim generic `sonar-scanner` analyzed BackEnd C# in BackEnd-only or mixed
   FrontEnd/BackEnd changes.
"""

SONAR_FIXES = """# Sonar fixes

Use this skill only after a Sonar check has produced a confirmed issue.

1. Read the affected source and the closest relevant test.
2. Explain the rule and propose the smallest behavior-preserving fix.
3. Prefer a real remediation over a blanket suppression; justify any
   unavoidable suppression and keep it narrowly scoped.
4. Apply no change until the developer accepts it, then run the smallest
   relevant test or build command.
5. Re-run the read-only Sonar check and report what remains.

Sonar findings and model-generated risk labels are separate evidence. Tier 2
risk labels are advisory, non-deterministic, and model-dependent rather than
validated gates; HIGH remains an open item pending calibration metrics and a
signed rubric/prompt review, and must not block a commit or merge by itself.
"""

SONAR_CODE_REVIEW_AGENT = """---
name: sonar-code-review
description: Review changed code with deterministic SonarQube MCP evidence and propose minimal fixes.
---

# Sonar code review

Review the current staged or working-tree diff. Use the `sonar-check` and
`sonar-fixes` skills and only read-only SonarQube MCP tools.

- Establish the exact project key, branch, and analysis freshness first.
- For staged checks, use only index-resolved Sonar configuration and validated
  explicit environment overrides.
- Inspect changed source and the closest tests before proposing a fix.
- Treat confirmed Sonar issues as evidence; treat Sonar unavailability,
  stale data, and model risk labels as advisory/unverified.
- Never expose credentials, call write-capable MCP tools, or make broad
  speculative refactors.
- Return: confirmed findings, evidence checked, minimal proposed fixes, tests
  run, and unresolved infrastructure or calibration limits.
"""


ARTIFACTS = {
	".github/skills/sonar-check/SKILL.md": SONAR_CHECK,
	".github/skills/sonar-fixes/SKILL.md": SONAR_FIXES,
	".github/agents/sonar-code-review.agent.md": SONAR_CODE_REVIEW_AGENT,
}
