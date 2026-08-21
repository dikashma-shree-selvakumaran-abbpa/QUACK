# QUACK — Capability Catalog & Showcase

> An overview of **quack** v0.3.0. For the live,


**quack** `v0.3.0` · Python 3.11+ · Windows / Linux 
<https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK>

---

## The one-liner

> **quack is an AI-assisted git quality gate: deterministic local checks stop
> secrets and merge markers before commit, while advisory AI review helps catch
> regressions before push.**

## The value

| Problem | quack response |
|---|---|
| A secret is staged | Blocks the commit locally before Git records it. |
| A merge marker is staged | Blocks the commit with the affected file and line. |
| Debug code or a large file is staged | Shows a warning without blocking the developer. |
| A change may regress behavior | Maps likely tests and provides an advisory AI review. |
| An AI provider is slow or unavailable | Fails open; commits and pushes continue. |

## How it works

```text
Tier 1: local deterministic checks ──────────────── blocks secrets and merge markers
		   │
		   ├─ optional local gitleaks scan ──────── advisory, fail-open
		   │
quack watch: redacted-diff AI review ────────────── advisory cache for commit time
		   │
quack agent: pre-push AI review ─────────────────── advisory review of unpushed commits
		   │
		   └─ optional tool-calling investigation ─ github_models provider only
```

| Surface | When | Network | Authority |
|---|---|---|---|
| `quack check` | Pre-commit | No | Blocks only secrets and merge markers |
| `quack watch` | While working | Yes | Advisory cached review |
| `quack agent` | Pre-push | Yes | Advisory review and optional investigation |
| `quack model` | On demand | Model discovery only | Diagnostics |
| `quack metrics` | On demand | No | Local aggregate summary |

## What to showcase

### Deterministic commit checks

- Scans added lines for secrets, including Azure DevOps PATs, GitHub tokens,
  AWS keys, private keys, Azure Storage keys, Slack tokens, and long assigned
  credentials.
- Detects merge markers, warns on selected debug-code patterns, and warns on
  large staged files.
- Reports exact file and line locations.
- Supports `# quack: allow` or `# pragma: allowlist secret` to suppress both
  built-in and gitleaks secret findings on one intentional fixture line.

### Advisory review without commit latency

- `quack check` is fully local: it has no provider call, token check, or network
  fallback.
- `quack watch` reviews staged or tracked working changes after a 30-second
  quiet period by default and caches the redacted-diff result for up to 24 hours.
- A cache hit lets the commit hook render the review immediately; a miss simply
  says to run `quack watch`.
- At pre-push, `quack agent` reviews staged changes or, when the index is empty,
  the unpushed commit range.

### Provider and privacy story

- The default provider is `copilot_sdk`, which uses the Copilot CLI's stored
  OAuth login; no personal access token is needed for default advisory reviews.
- Ambient `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN` values can
  shadow that login and should be unset when using the Copilot SDK.
- `QUACK_PROVIDER=github_models` plus `GITHUB_TOKEN` enables the optional
  OpenAI-style tool-calling investigation loop.
- Detected secrets are redacted before a diff is sent to either AI provider.
- Metrics are stored locally as sanitized aggregates; they contain no code,
  paths, repository names, commit messages, or token values.

## Live-demo beats

1. Run `quack install --local` in a throwaway repository to show both hooks.
2. Commit a staged Azure DevOps PAT to show the deterministic block.
3. Add `// quack: allow` to show the per-line escape hatch.
4. Change real code and run `quack check` to show cross-package test guidance.
5. Run `quack watch --once`, then `quack check` to show the cached review.
6. Push an intentional regression to show the advisory pre-push review.
7. Run `quack model` and `quack metrics` for diagnostics and local evidence.

## Objection handling

| Question | Answer |
|---|---|
| Does it slow down commits? | The commit path is local only; AI runs via watch mode or at pre-push. |
| Does it block because AI is unavailable? | No. AI, gitleaks, and the agent are advisory and fail open. |
| Does code leave the machine at commit time? | No. At watch/pre-push, only the redacted diff is sent to the selected provider. |
| Can the agent run with the default provider? | The default Copilot SDK provides review. The optional tool-calling loop requires `github_models` and `GITHUB_TOKEN`. |
| Does it work in an IDE? | Yes. These are Git hooks, so they work with terminal and IDE Git clients. |

## Adoption

1. Install quack and sign in to the Copilot CLI.
2. Run `quack install` in a published repository, or `quack install --local` for
   a local checkout or demo.
3. Keep `quack watch` running during development when advisory feedback before
   commit is useful.
4. Use `quack model` to diagnose provider and login problems.
5. Use `quack metrics` to review local aggregate evidence.

For installation and troubleshooting, see [SETUP.md](SETUP.md). For the
implementation-verified v0.3.0 details, see [CURRENT_STATE.md](CURRENT_STATE.md).
