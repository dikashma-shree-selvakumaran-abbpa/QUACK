# quack

An AI-assisted git quality gate. quack inspects your **staged changes** at
commit time and decides whether they are safe -- fast, offline, and fail-safe.

## How it works

- **Tier 1** -- deterministic, offline checks (secrets, merge markers, debug
  code, large files, commit-to-test mapping). Always runs, target <1s. The
  ONLY tier that can block a commit.
- **Commit time is fully local.** `quack check` performs **no network calls and
  sends no code anywhere** -- Tier 1 (plus gitleaks when installed) and test
  guidance only. No token, no AI, no quota.
- **quack agent** -- an agentic investigate-and-verify loop for pre-push. **All
  AI runs here**, never at commit time.

> Design principle: *AI advises, deterministic code decides.* The LLM can never
> stop your commit -- only the offline Tier 1 checks can.

## Install

quack is pure Python and runs anywhere Python 3.11+ does. Pick your platform.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### macOS / Linux (bash/zsh)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Platform support

quack is cross-platform: no shell-outs with `shell=True`, no hardcoded paths,
and every subprocess call uses portable argument lists.

| Feature | Windows | macOS | Linux |
|---------|---------|-------|-------|
| Tier 1 checks (secrets, merge markers, debug code) | ? | ? | ? |
| pre-commit hook | ? | ? | ? |
| quack agent AI review (pre-push) | ? | ? | ? |
| quack agent | ? | ? | ? |
| gitleaks **auto**-install | via `winget` | via `brew` | via `brew` if present |

On Linux without Homebrew, `quack install` can't auto-install gitleaks, so
install it once by hand — quack still runs fully on its built-in patterns
regardless (fail-open):

```bash
# Debian/Ubuntu example (see gitleaks releases for other distros)
curl -sSfL https://github.com/gitleaks/gitleaks/releases/latest/download/gitleaks_linux_x64.tar.gz \
  | sudo tar -xz -C /usr/local/bin gitleaks
gitleaks version
```

Confirm:

```bash
quack --help
quack check
```

## Commands

```bash
quack check      # pre-commit hook entry (Tier 1 + gitleaks, fully local, no network)
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

### Two surfaces: pre-commit and pre-push

quack wires **two** hooks:

- **pre-commit (`quack`)** — local checks only (Tier 1 + gitleaks). No network,
  no code leaves the machine.
- **pre-push (`quack-agent`)** — AI review, plus the investigative agent where
  the provider supports tool calling.

`quack install` writes both entries and installs both hook types (the default
pre-commit hook and `pre-commit install --hook-type pre-push`). If the pre-push
hook-type install fails, it warns but does not abort — your pre-commit checks
stay active.

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
	  - id: quack-agent
		name: quack-agent
		entry: quack agent
		language: system
		pass_filenames: false
		stages: [pre-push]
```

Pinned to a published remote (what `quack install` writes):

```yaml
repos:
  - repo: https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK
	rev: v0.1.0                                # a real git tag
	hooks:
	  - id: quack
	  - id: quack-agent
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

That line is skipped for **all** scanners -- quack's built-in Tier 1 checks
*and* the gitleaks power mode below. One marker, one line, both silenced.

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

## AI features (the pre-push agent)

`quack check` makes no network calls and needs no token. AI runs only at
pre-push via `quack agent`.

### LLM provider

`QUACK_PROVIDER` selects the LLM transport and defaults to **`copilot_sdk`**.
The Copilot SDK is the approved transport at ABB; GitHub Models via a personal
access token is not, so the compliant path is the default rather than something
you opt into.

**The `agent` command currently requires `QUACK_PROVIDER=github_models`.** The
Copilot SDK does not yet implement tool calling, which the agent's multi-step
loop needs, so until SDK tool calling is implemented the agent must run under
`github_models`:

```bash
$env:QUACK_PROVIDER="github_models"   # PowerShell
export QUACK_PROVIDER=github_models    # bash
```

The `github_models` provider reads `GITHUB_TOKEN`:

```bash
$env:GITHUB_TOKEN=your_token      # PowerShell
export GITHUB_TOKEN=your_token    # bash
```

Without an available provider, `quack agent` skips cleanly (fail-open). Override
the model with `--model` or the `QUACK_MODEL` env var.

## Development

```bash
pip install -e ".[dev]"
pytest
```
