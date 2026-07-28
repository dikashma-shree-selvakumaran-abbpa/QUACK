# quack

An AI-assisted git quality gate. quack inspects your **staged changes** at
commit time and decides whether they are safe -- fast, offline, and fail-safe.

## How it works

- **Tier 1** -- deterministic, offline checks (secrets, merge markers, debug
  code, large files, commit-to-test mapping). Always runs, target <1s. The
  ONLY tier that can block a commit.
- **Tier 2** -- one call to GitHub Models returning schema-validated JSON risk
  analysis. Skipped for trivial deltas; hard 6s timeout; fail-open.
- **quack agent** -- an agentic investigate-and-verify loop for pre-push.

> Design principle: *AI advises, deterministic code decides.* The LLM can never
> stop your commit -- only the offline Tier 1 checks can.

## Install

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # Windows (PowerShell)
# source .venv/bin/activate      # macOS/Linux
pip install -e ".[dev]"
```

Confirm:

```bash
quack --help
quack check
```

## Commands

```bash
quack check      # pre-commit hook entry (Tier 1 + Tier 2)
quack install    # wire quack into .pre-commit-config.yaml (--local for any project)
quack agent      # agentic pre-push investigation loop
quack model      # model/config utilities (stub)
```

## Add quack to a project

quack runs via the [pre-commit](https://pre-commit.com) framework. Once wired
in it runs on every `git commit` -- in VS Code, Visual Studio, the terminal, or
any git client, because the hook lives in git itself, not the editor.

### One-time setup

```bash
pipx install pre-commit          # one-time: the pre-commit framework
quack install --local            # writes config, installs hook, auto-installs gitleaks
```

`--local` wires quack via a `repo: local` stanza that runs the `quack` command
already on your PATH, so you can drop quack into any project on this machine
without needing a published quack repo or git tag. Omit `--local` to pin quack
to a published git repo/tag instead (see *Manual* below).

Run on demand without committing:

```bash
git add .
quack check
```

### Manual .pre-commit-config.yaml

Local mode (no published repo needed -- what `quack install --local` writes):

```yaml
repos:
  - repo: local
	hooks:
	  - id: quack
		name: quack
		entry: quack check
		language: system
		pass_filenames: false
		stages: [pre-commit]
```

Pinned to a published remote (what `quack install` writes):

```yaml
repos:
  - repo: https://github.com/your-org/quack   # replace with your remote
	rev: v0.1.0                                # a real git tag
	hooks:
	  - id: quack
```

### VS Code / Visual Studio

The hook fires when you commit from the Source Control / Git Changes panel. For
the full formatted report, commit from the built-in terminal with `git commit`.

### Bypass (use sparingly)

```bash
git commit --no-verify
```

Prefer the inline allowlist below over `--no-verify`.

## Tier 1 checks

| Check | Blocks? | Examples |
|-------|---------|----------|
| secrets | yes | AWS keys, GitHub/Azure DevOps/Slack tokens, private keys, hardcoded creds |
| merge_markers | yes | conflict markers |
| debug_code | warn | console.log, breakpoint(), pdb.set_trace(), focused .only tests |
| large_file | warn | files > 512 KB |

### Inline allowlist

```python
API_KEY = "AKIA...example..."  # quack: allow
API_KEY = "AKIA...example..."  # pragma: allowlist secret
```

That line is skipped for all Tier 1 checks.

## gitleaks power mode (auto-installed)

On top of the built-in patterns, quack uses
[gitleaks](https://github.com/gitleaks/gitleaks) -- hundreds of tuned,
entropy-aware rules catching Stripe, GCP, npm, JWT, and many more secret types.

`quack install` auto-installs gitleaks when possible (winget on Windows, brew
on macOS). Manual install:

```bash
winget install Gitleaks.Gitleaks   # Windows
brew install gitleaks              # macOS
```

Fully optional and fail-open: if gitleaks is missing, errors, or times out,
quack falls back to its built-in patterns. Disable it even when installed:

```bash
$env:QUACK_DISABLE_GITLEAKS=1     # PowerShell
export QUACK_DISABLE_GITLEAKS=1   # bash
```

## AI features (Tier 2 and the agent)

```bash
$env:GITHUB_TOKEN=your_token      # PowerShell
export GITHUB_TOKEN=your_token    # bash
```

Without a token, Tier 2 skips (fail-open). Override the model with `--model` or
the `AIGUARD_MODEL` env var.

## Development

```bash
pip install -e ".[dev]"
pytest
```
