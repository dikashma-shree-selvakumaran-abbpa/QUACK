# quack

Catch problems before they reach CI — a pre-commit quality hook with AI review at pre-push.

## The problem

For most teams, the first real quality gate is CI. You push, wait 15–20 minutes, and learn that a secret was committed, debug code was left in, or a test was broken — after you have context-switched and after teammates are blocked. The cost of a defect grows with the distance from the keystroke that made it. quack moves the first check back to the commit and push that introduced the change.

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
| `quack check` | Pre-commit | Checks secrets, merge markers, debug code, and test guidance. | No; no token required. |
| `quack watch` | In the background | Reviews changes while you work and caches a verdict for commit time. | Yes, when a provider is available. |
| `quack agent` | Pre-push | Reviews unpushed commits; its investigative agent can run tests and propose fixes. | Yes, when a provider is available. |

Only secrets and merge markers block. AI is advisory and fails open: no token, offline operation, a slow provider, or rate limiting never prevents a commit.

## Install

1. `pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK`
2. Run `copilot`, enter `/login`, complete the browser flow, then exit.
3. In your repository, run `quack install`.

See [SETUP.md](SETUP.md) for details and troubleshooting.

## Status

The deterministic commit checks, background review cache, pre-push Tier 2 review, metrics, and hook installation are covered by the current test suite. The agent’s tool-calling loop currently requires the `github_models` provider; SDK-native tools are scoped but not built. AI availability and model responses depend on the signed-in provider and are intentionally advisory.
