# quack

An AI-assisted pre-commit quality hook.

quack runs two tiers at commit time:

- **Tier 1** — deterministic, offline checks (secrets, merge markers, debug
  code, large files, commit-to-test mapping). Always runs, target <1s.
- **Tier 2** — a single call to GitHub Models returning schema-validated JSON.
  Skipped for trivial deltas; hard 6s timeout; fail-open.

A separate `quack agent` command runs an agentic loop intended for pre-push.

## Install

```bash
pipx install -e .
```

## Usage

```bash
quack check      # the pre-commit hook entry
quack install    # wire quack into .pre-commit-config.yaml
quack agent      # agentic pre-push loop (stub)
quack model      # model/config utilities (stub)
```

## Use as a pre-commit hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/your-org/quack
	rev: v0.1.0
	hooks:
	  - id: quack
```
