# QUACK

Catch problems before they reach CI : a pre-commit quality hook with AI review at pre-push.

## The problem

For most teams, the first real quality gate is CI. You push, wait 15–20 minutes, and learn that a secret was committed, debug code was left in, or a test was broken , after you have context-switched and after teammates are blocked. The cost of a defect grows with the distance from the keystroke that made it. quack moves the first check back to the commit and push that introduced the change.

## What it looks like

A staged Azure DevOps PAT blocks the commit; quack exits 1 and Git does not commit it.

```text
╭─ quack - 2 file(s) - +2/-1 - 0.8s ───────────────────────────────────────────────────────────────────────────────────────────────────╮
│ ✗  secrets  demo_secret.cs:1  Azure DevOps PAT                                                                                       │
│ ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── │
│ 🐤 QUACK!!!! check line #1                                                                                                           │
╰─ 🐤 BLOCKED - fix and re-stage ──────────────────────────────────────────────────────────────────────────────────────────────────────╯
exit code: 1
```

A normal commit gives an exact test command and shows the cached review from `quack watch`.

```text
╭─ quack - 1 file(s) - +1/-1 - 1.9s ──────────────────────────────────────────────╮
│ Test guidance                                                                   │
│ dotnet test packages/GraphicsModelEditor/GfxKernel.Tests/GfxKernel.Tests.csproj │
│ (first run: build once with dotnet build)                                       │
│ ─────────────────────────────────────────────────────────────────────────────── │
│ AI - claude-haiku-4.5 - risk: MEDIUM                                            │
│ Validation relaxed from exact to minimum length; confirm downstream array index │
│ Validation boundary changed: from exact length equality (!=) to minimum length  │
│ Relaxed constraint permits cases previously rejected; downstream BeginMove logi │
│ boundary/index/limit logic touched                                              │
│ (reviewed 2 min ago by quack watch)                                             │
╰─ advisory: commit allowed ──────────────────────────────────────────────────────╯
```

On push, the pre-push hook reviews unpushed commits before the push completes.

```text
quack-agent..............................................................Passed
- hook id: quack-agent
- duration: 26.14s
analyzing 1 unpushed commit(s)
AI - claude-haiku-4.5 - risk: HIGH
Removing delegate cleanup risks stale mouse handlers; confirm this is intentional.
Deletion of RestoreMouseDelegates() call in move finalization (line 115)
Mouse event handler state may not be restored after move completes
No apparent replacement code restoring delegates elsewhere in this path
```

## How it works

| Surface | When it runs | What it does | Network |
|---|---|---|---|
| `quack check` | Pre-commit | Checks secrets, merge markers, debug code, and test guidance, then performs only a local review-cache lookup. It never calls AI. | No; no token required. |
| `quack watch` | Alongside development | Reviews changes while you work and caches a verdict for commit time. | Yes, when a provider is available. |
| `quack agent` | Pre-push | Reviews unpushed commits, then runs the provider's read-only investigation tools and can propose fixes. | Yes, when a provider is available. |

Only secrets and merge markers block. AI is advisory and fails open: no token, offline operation, a slow provider, or rate limiting never prevents a commit. A pre-push range review requires an upstream branch; without one, `quack agent` can still analyze staged changes but has no unpushed range to inspect.

## CLI surface

```text
PS> quack --help
Usage: quack [OPTIONS] COMMAND [ARGS]...

  quack: an AI-assisted pre-commit quality hook.

Options:
  --version   Show the version and exit.
  -h, --help  Show this message and exit.

Commands:
  agent    Run the agentic pre-push analysis loop.
  check    Run the pre-commit quality checks on staged changes.
  install  Add the quack stanza to .pre-commit-config.yaml and install...
  metrics  Summarize local metrics without network access.
  model    Report model configuration and connectivity without changing it.
  watch    Review changes in the background and cache the result for...
```

Important options:

| Option | Commands | Purpose |
|---|---|---|
| `--once` | `watch` | Run one review immediately and exit. |
| `--quiet-period SECONDS` | `watch` | Set the quiet period before a background review; default is 30 seconds. |
| `--model MODEL` | `model`, `agent` | Override the configured model for diagnostics or agent review. |
| `--fly` | `agent` | Reveal an unapplied proposed patch instead of showing coaching-only output. |
| `--local` | `install` | Install hooks using the installed `quack` command without requiring a published repository revision. |

`quack check` is intentionally fully local. It runs deterministic checks,
builds test guidance, and reads a matching cached AI review when one exists.
It never calls an AI provider and never falls back to a network request on a
cache miss. AI review is performed by `quack watch` or `quack agent`.

## Install

1. `pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK`
2. Run `copilot`, enter `/login`, complete the browser flow, then exit.
3. In your repository, run `quack install`.

See [SETUP.md](SETUP.md) for details and troubleshooting and [CURRENT_STATE.md](CURRENT_STATE.md) for the
code-grounded v0.3.0 architecture snapshot.

## Status

The deterministic commit checks, background review cache, pre-push Tier 2 review,
SDK-native agent tools, metrics, hook installation, and fail-open behavior are
covered by the current test suite. The default `copilot_sdk` provider uses the
Copilot CLI's stored login for both advisory review and native tool calling;
`github_models` remains reachable as the legacy OpenAI-style provider. AI
availability and model responses are intentionally advisory.
