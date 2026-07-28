# QUACK — Capability Catalog & Showcase

> A presentation-ready guide to explain and demo **quack** — technically and
> feature-wise. Use this to pitch it to a team, a manager, or a security review.
> For hands-on commands and copy-paste demos, see [DEMO.md](DEMO.md).

**quack** `v0.1.0` · Python 3.11+ · Cross-platform (Windows / macOS / Linux) ·
MIT · https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK

---

## 1. The one-liner

> **quack is an AI-assisted git quality gate that stops bad commits before they
> happen — deterministically for secrets and mistakes, intelligently for
> regressions.**

## 2. The elevator pitch (30 seconds)

Every team has the same three problems: **secrets leak into commits**, **debug
code and merge markers slip through**, and **regressions get pushed**. quack
catches all three at the git level — so it works the same in the terminal, VS
Code, Visual Studio, or any git client. It's **fast and offline** for the
blocking checks, and **AI-powered** for the deeper review — but the AI can
*never* block you or crash your workflow. It's safe by design.

## 3. Why it matters (the problem → the value)

| Problem | Cost | How quack solves it |
|---------|------|---------------------|
| Secret committed to git history | Rotating keys, incident response, audit | Blocks the commit before the secret is ever recorded |
| Merge markers / debug code shipped | Broken builds, embarrassing prod bugs | Deterministic Tier 1 block |
| Regression pushed | Broken main, blocked teammates | Agent runs the tests and diagnoses root cause pre-push |
| Security tooling is flaky/slow | Devs disable it | Fail-open + offline core = never in the way |
| Editor-specific hooks | Inconsistent enforcement | Lives in git, not the editor — universal |

---

## 4. What quack IS (positioning)

quack is **three things in one**, layered by cost and confidence:

```
		┌─────────────────────────────────────────────────────────┐
		│  TIER 1  — deterministic, offline, BLOCKING              │
		│  secrets · merge markers · debug code · large files     │
		│  → milliseconds, no network, exit 1 stops the commit    │
		├─────────────────────────────────────────────────────────┤
		│  GITLEAKS POWER MODE — optional, layered, fail-open     │
		│  hundreds of tuned rules (Stripe, GCP, ...) if present  │
		├─────────────────────────────────────────────────────────┤
		│  TIER 2  — AI review, advisory, NON-blocking            │
		│  GitHub Models · redacted diff · never changes exit code│
		├─────────────────────────────────────────────────────────┤
		│  QUACK AGENT — agentic pre-push investigation loop      │
		│  reads code · runs tests · diagnoses root cause · coaches│
		└─────────────────────────────────────────────────────────┘
```

**Is it a tool, a hook, or an agent?** All three — deliberately:
- a **CLI tool** (`quack check/agent/install/model`),
- installed as a **pre-commit hook**,
- with an **agentic** pre-push mode.

---

## 5. Feature catalog (what to showcase)

### 5.1 Deterministic blocking checks (Tier 1)

| Feature | Detects | Demo value |
|---------|---------|-----------|
| Secret scanning | AWS keys, GitHub tokens, private keys, Azure Storage keys, Azure DevOps PATs, Slack tokens, hardcoded credentials | Fires 100% of the time — perfect for live demos |
| Merge-marker detection | `<<<<<<<`, `=======`, `>>>>>>>` | Catches botched merges |
| Debug-code detection | `console.log(`, `breakpoint()`, `pdb.set_trace()`, `Debugger.Break()`, debug `print(` | Stops "oops" commits |
| Large-file warning | Staged files > 512 KB | Keeps repos lean (warns, doesn't block) |
| Exact line numbers | Tracks diff hunk offsets | Reports `file.py:42`, not just "somewhere" |
| Loud alarm | `🐤 QUACK!!!! check line #42` | Impossible to miss |

### 5.2 gitleaks power mode
- Auto-installed during `quack install` (winget / brew).
- Adds hundreds of entropy-aware rules (Stripe, GCP, generic high-entropy).
- **Fail-open**: missing/broken gitleaks → quack still runs on built-ins.

### 5.3 Inline allowlist (one marker, both scanners)
- `# quack: allow` or `# pragma: allowlist secret` on a line.
- Suppresses **quack built-ins AND gitleaks** on that line — a clean escape
  hatch that keeps the gate honest (better than `--no-verify`).

### 5.4 AI review (Tier 2)
- Uses GitHub Models when `GITHUB_TOKEN` is set.
- Diff is **redacted** before it leaves the machine (secrets never sent).
- **Advisory only** — never blocks, never changes the exit code, fail-open.

### 5.5 Agentic pre-push (quack agent)
- Investigates the diff with three **read-only** tools: `read_file`, `list_dir`,
  `run_tests`.
- Runs the smallest relevant test set, diagnoses **root cause**, proposes a
  minimal patch and a missing test.
- **Coaching mode by default**; `--fly` reveals the ready patch.

### 5.6 Developer experience
- **Yellow duck celebration banner** on install 🦆
- Rich terminal panels, TTY/NO_COLOR-aware (clean CI logs).
- One-command setup: `quack install --local`.
- Works in **cmd, VS Code, Visual Studio, any git client**.

---

## 6. Engineering & trust story (for a technical audience)

This is what makes quack *safe to adopt*:

| Principle | What it means | Why it matters |
|-----------|---------------|----------------|
| **Fail-open everywhere** | gitleaks, AI, agent all degrade gracefully | A tooling outage never blocks your team |
| **Blocking is deterministic only** | Only offline Tier 1 can stop a commit | No AI flakiness in the critical path |
| **Functional core / imperative shell** | Pure logic (`tier1`, `delta`, `testmap`) separated from I/O (`gitio`, `llmio`, `runio`, `gitleaks`) | Fully unit-testable, easy to reason about |
| **Secrets never leave the machine** | Diff redacted before any model call | Privacy by construction |
| **Agent safety invariants** | Path containment, command whitelist, budgets, ground-truth reconciliation | The agent can't read outside the repo, run arbitrary commands, loop forever, or lie about test results |
| **91 passing tests** | Every check, adapter, and render path covered | Confidence to extend |
| **Cross-platform, zero shell=True** | Portable subprocess arg lists | Same behavior on every OS |

---

## 7. Live demo flow (what to click through)

> Full commands in [DEMO.md §9](DEMO.md). Presentation beats:

1. **Install** → yellow duck banner appears. "Setup is one command."
2. **Commit a secret** → BLOCKED panel + `🐤 QUACK!!!! check line #5`. "It stops
   the leak before git records it."
3. **Allowlist one line** → finding disappears. "Clean escape hatch for known
   false positives — both scanners honor it."
4. **Clean commit** → green *commit allowed*. "Out of your way when you're fine."
5. **Commit from VS Code / VS** → hook fires automatically. "Editor-agnostic."
6. **quack agent on a regression** → it reads the code, runs the test, diagnoses
   the root cause. "It investigates, it doesn't guess."
7. **quack agent --fly** → the ready-to-apply patch. "And it can hand you the fix."

---

## 8. Adoption story (rolling it out)

```
1. Publish quack  ──►  2. quack install (pinned)  ──►  3. commit config
		│                        │                            │
   v0.1.0 tagged        everyone same version          .pre-commit-config.yaml
		│                        │                            │
   4. teammates: pre-commit install  ──►  5. optional: GITHUB_TOKEN for AI
		│                                          │
   6. optional CI: pre-commit run --all-files  (closes the --no-verify gap)
```

- **Local mode** (`--local`): drop into any project instantly, no published repo.
- **Pinned mode** (default): whole team runs the same version via a git tag.

---

## 9. Objection handling (Q&A cheat-sheet)

| "But what about…" | Answer |
|-------------------|--------|
| "AI is flaky / slow" | AI is advisory and fail-open — it never blocks or crashes. Blocking is 100% deterministic and offline. |
| "Will it send our code to an API?" | Only Tier 2/agent, only if you set `GITHUB_TOKEN`, and the diff is **redacted** first. Tier 1 is fully offline. |
| "Devs will just `--no-verify`" | Prefer the inline allowlist (honest, per-line). Add `pre-commit run --all-files` in CI as the real gate. |
| "Does it work on Linux/Mac?" | Yes — pure Python, no `shell=True`, portable everywhere. gitleaks auto-installs on Win/Mac; one manual step on brew-less Linux. |
| "Can the agent do something dangerous?" | No — read-only tools, path containment, command whitelist, hard budgets. |
| "Is it maintained/tested?" | 91 passing tests; functional-core design makes it easy to extend. |

---

## 10. At-a-glance summary card

| | |
|--|--|
| **What** | AI-assisted git quality gate |
| **Blocks** | secrets, merge markers, debug code (deterministic) |
| **Advises** | AI code review + agentic regression check |
| **Speed** | milliseconds for the blocking core (offline) |
| **Safety** | fail-open; secrets redacted; agent sandboxed |
| **Setup** | `quack install --local` (one command) |
| **Works in** | cmd, VS Code, Visual Studio, any git client |
| **Platforms** | Windows, macOS, Linux |
| **Tests** | 91 passing |
| **License** | MIT |

---

*Pair this catalog with [DEMO.md](DEMO.md) for the live walkthrough and
[README.md](README.md) for install details.*
