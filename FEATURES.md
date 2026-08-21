# quack — Technical Feature Reference

> An AI-assisted **pre-commit / pre-push quality gate** for git.
> Deterministic where it must block, AI-assisted where it can help, and
> **fail-open** everywhere the AI is involved.

This document describes what quack does, how each layer is implemented, the
exact safety invariants, and how quack differs from a general "code review
agent." Everything here is derived from the source, not aspiration.

---

## 1. Architecture at a glance

quack follows a **functional core / imperative shell** design.

| Layer | Modules | Responsibility |
|---|---|---|
| Pure logic (no I/O) | `delta`, `tier1`, `testmap`, `jsonparse` | Parse diffs, scan for issues, plan tests, parse model JSON. Fully unit-testable. |
| I/O adapters | `gitio`, `llmio`, `runio`, `gitleaks` | Talk to git, model providers, test runners, and the gitleaks binary. |
| Orchestration | `cli`, `tier2`, `agent` | Wire the pieces together; own exit codes and UX. |
| Presentation | `render` | The single module that writes to the terminal. |

**Design invariant:** only Tier 1 governs the exit code. gitleaks and the agent
are advisory / fail-open and can never turn a passing commit into a failing one
due to their own unavailability. **Commit time is fully local** — `quack check`
makes no network calls and sends no code anywhere. AI runs in `quack watch`
while the developer works or at pre-push via `quack agent`.

---

## 2. CLI surface

**Two hook surfaces.** quack installs two pre-commit-framework hooks:

- **pre-commit (`quack`)** — local checks only (Tier 1 + gitleaks). No network,
  no code leaves the machine.
- **pre-push (`quack-agent`)** — AI review, plus the investigative agent where
  the provider supports tool calling.

`quack install` writes both entries into `.pre-commit-config.yaml` and installs
both hook types (the default pre-commit hook and
`pre-commit install --hook-type pre-push`). The pre-push install is handled
independently: if it fails, install warns but does not abort, leaving the
already-installed pre-commit checks active.

| Command | Purpose | Exit-code authority |
|---|---|---|
| `quack check` | Full pre-commit gate on **staged** changes (what the hook runs). Fully local: **no network calls, sends no code anywhere.** | Tier 1 only |
| `quack watch [--quiet-period 30] [--once]` | Review staged or tracked working changes and cache an advisory result for commit time. | Advisory (informational) |
| `quack agent [--fly] [--model M]` | Pre-push Tier 2 review plus an investigative loop over staged changes or, when the index is empty, unpushed commits. `--fly` allows a proposed patch. | Advisory (informational) |
| `quack install [--local]` | Write `.pre-commit-config.yaml` (both `quack` + `quack-agent` hooks), install both hook types, and best-effort bootstrap gitleaks. `--local` targets any repo without a published remote. | n/a |
| `quack model [--model M]` | Report provider, availability, model resolution, timeout, and Copilot SDK model discovery. | n/a |
| `quack metrics` | Summarize locally recorded aggregate metrics. | n/a |

**Model resolution order:** `--model` flag → `QUACK_MODEL` env var →
provider-specific default. `copilot_sdk` defaults to `claude-haiku-4.5` for
single-shot review and `claude-sonnet-4.5` for the agent; `github_models`
defaults to `openai/gpt-4o-mini` and `openai/gpt-4.1` respectively. `check`
uses **no model** — it makes no AI calls.

**Relevant env vars:** `QUACK_PROVIDER` (LLM transport — see below),
`GITHUB_TOKEN` (required by the `github_models` provider), `QUACK_MODEL`
(model override), `QUACK_DISABLE_GITLEAKS` (skip gitleaks), and `NO_COLOR`
(plain output). `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN` should
be unset when using `copilot_sdk`, because ambient tokens can shadow the
Copilot CLI login.

**LLM provider:** `QUACK_PROVIDER` selects the transport, defaulting to
**`copilot_sdk`**. The Copilot SDK is the approved transport at ABB; GitHub
Models via a PAT is not, so the compliant path is the default rather than an
opt-in. `github_models` remains available via `QUACK_PROVIDER=github_models`
because it is currently the **only** provider that supports tool calling.
**The `agent` command therefore requires `QUACK_PROVIDER=github_models` today**
— the Copilot SDK does not yet implement tool calling, so the agent loop cannot
run under the default provider until SDK tool calling is implemented.

---

## 3. Tier 1 — deterministic blocking checks

**Module:** `tier1.py` · **Cost:** offline, milliseconds · **Authority:** blocks the commit.

### 3.1 Scanning model
- Scans **added lines only** (`+` lines, excluding the `+++` header). A secret
  on a removed (`-`) line never fires.
- Line numbers are tracked by parsing each hunk header
  (`@@ -a,b +c,d @@`) and advancing through context/added lines, so every
  finding reports an **exact** file + line number.
- Binary files and excluded paths (`DEFAULT_EXCLUDES`) are skipped.

### 3.2 Secret patterns (regex, ordered)

| Secret | Pattern (essence) |
|---|---|
| AWS access key id | `AKIA[0-9A-Z]{16}` |
| Private key block | `-----BEGIN … PRIVATE KEY-----` |
| Azure Storage account key | `AccountKey=[A-Za-z0-9+/=]{60,}` |
| GitHub token | `gh[posur]_…{20,}` or `github_pat_…{20,}` |
| Azure DevOps PAT | `\b[a-z2-7]{52}\b` (52-char base32) |
| Slack token | `xox[baprs]-…{10,}` |
| Hardcoded credential | `(key|secret|token|password) = "…16+ chars…"` |

### 3.3 Other checks

| Check | Trigger | Severity |
|---|---|---|
| `merge_markers` | Lines starting `<<<<<<<`, `=======`, `>>>>>>>` | **error (blocks)** |
| `debug_code` | `print(...)` near `HERE`/`DEBUG`/`xxx`, or `Debugger.Break()` | warn |
| `large_file` | Staged file > 512 KB on disk | warn |

### 3.4 What actually blocks
`DEFAULT_BLOCK_ON = ("secrets", "merge_markers")`. Everything else is a
**warning** and does not change the exit code. `should_block()` decides the
`sys.exit(1)`.

### 3.5 Inline allowlist (escape hatch)
A line carrying `# quack: allow` **or** `# pragma: allowlist secret`
(detect-secrets convention) is skipped entirely. `allowlisted_locations()` is
the single source of truth, and it also suppresses **gitleaks** on that same
line — so one marker covers both engines. This is a deliberate alternative to
`git commit --no-verify`.

---

## 4. gitleaks power mode (optional)

**Module:** `gitleaks.py` · **Fully fail-open.**

- Auto-installed during `quack install` — winget (Windows) or brew (macOS/Linux);
  silently skipped if neither is available.
- On `check`, runs `gitleaks protect --staged`, parses the JSON report into
  `Finding` objects, and `merge()`s them with Tier 1 (deduped).
- Respects the same inline allowlist via `filter_allowlisted()`.
- Disabled with `QUACK_DISABLE_GITLEAKS`. Returns `[]` on any error — never
  raises into the hook.

**Why both?** Tier 1 is a tight, high-precision, dependency-free set for the
demo-critical secrets and merge markers. gitleaks adds broad, entropy-aware
coverage as an optional upgrade.

---

## 5. Tier 2 — advisory AI review (watch mode and pre-push)

**Modules:** `tier2.py` + `llmio.py` · **Fail-open. Never changes the exit code.**

> **Not run at commit time.** The Copilot/model setup cost (~9–15s cold) and
> per-call credit cost are incompatible with a <6s commit budget and per-commit
> team economics, so `quack check` makes **no** Tier 2 call. `quack watch`
> performs the review after a quiet period and caches it; `quack agent` also
> runs the same single-shot review at pre-push before any investigation loop.
- **Privacy:** the diff is **redacted before it leaves the machine.**
  `tier1.redact()` replaces every detected secret with `[REDACTED]`, and Tier 2
  builds its prompt from that redacted delta (`"Staged diff (redacted):"`).
- **Transport:** `llmio.complete()` selects `copilot_sdk` by default, using the
  Copilot CLI's stored OAuth login, or `github_models` when explicitly selected
  with `QUACK_PROVIDER=github_models` and `GITHUB_TOKEN`. All transport failures
  normalize to `LLMUnavailable` — no login, token, network error, or bad
  response ever crashes the hook; Tier 2 reports that review is unavailable.
- **Project instructions:** repo-local guidance is loaded by
  `instructions.load()` from the first file that exists — `.quack/instructions.md`
  → `instructions.md` → `.github/copilot-instructions.md` → `AGENTS.md` — and
  passed to the reviewer as **context** (truncated to 4 000 chars). Loading is
  **fail-open**: a missing/unreadable file returns `None` and the review runs
  without it. The file is **untrusted input**: `build_messages()` fences it as
  repo-provided context (not authority), and the deterministic rubric floor in
  `_anchor_result()` means a prompt-injection instructions file can **never
  lower** a risk verdict — it can only ever raise it.

---

## 6. The agent — pre-push investigation

**Module:** `agent.py` · A plain, inspectable **tool-calling loop** — no agent
framework.

### 6.1 Tools (all read-only except test execution)

| Tool | Behavior |
|---|---|
| `read_file(path)` | Returns first **300 lines** of a repo file. |
| `list_dir(path)` | Lists directory entry names. |
| `run_tests(project_or_paths)` | Runs a **whitelisted** test invocation only. |

### 6.2 Supported test runners (`runio.py`)
`run_tests` can construct **only** these fixed, no-shell command shapes:

| Language | Invocation |
|---|---|
| Python | `pytest <paths> -x --tb=short -q` |
| C# | `dotnet test <project.csproj> --no-build [--filter "…"] -v minimal` |
| JavaScript / TypeScript | `npx --no-install jest <paths> --silent --runInBand` |

JS/TS targets are recognized by `.test`/`.spec` suffixes across
`.js/.jsx/.ts/.tsx/.mjs/.cjs`. `--no-install` guarantees jest is never fetched
from the network; the project's local jest is used.

### 6.3 Method (from the system prompt)
1. Form a hypothesis about what could break, from the diff.
2. Gather **minimum** evidence — read the changed code and its most relevant
   caller/test (no reading without a stated reason).
3. Run the **smallest** test set to confirm/refute (≤ 2 runs).
4. On failure, diagnose the **root cause** (which line of the delta, why) — not
   the symptom.
5. Emit a strict JSON verdict:
   `{summary, tests_run, failures:[{test, diagnosis}], proposed_patch, proposed_new_tests}`.
   With `--fly`, `proposed_patch` may contain a minimal unified diff.

### 6.4 Hard safety invariants

| Invariant | Value / mechanism |
|---|---|
| Iteration budget | `MAX_ITERATIONS = 8` |
| Test-run budget | `MAX_RUN_TESTS = 2` |
| Wall-clock cap | `WALL_CLOCK_S = 180.0` |
| File read cap | `READ_FILE_MAX_LINES = 300` |
| Path containment | `_safe_path()` rejects anything outside the repo root |
| No shell | `subprocess` with `shell=False`; executables resolved via `shutil.which` |
| Filter sanitization | C# `--filter` must match `^[A-Za-z0-9_.~|&=!"\s-]+$` (no shell metacharacters) |
| Ground-truth reconciliation | A non-zero test exit code **overrides** an over-optimistic model self-report |
| JSON discipline | One retry on malformed JSON, then graceful degradation |

---

## 7. UX / onboarding

- **Install banner:** yellow ASCII duck + `QUACK` wordmark on `quack install`.
- **Blocked-line alarm:** loud `QUACK!!!! check line #…` callout with exact
  line numbers.
- **Output discipline:** all terminal output flows through `render.py` and is
  TTY / `NO_COLOR`-aware (ANSI dropped when piped).
- **Cross-platform:** Windows / macOS / Linux; works in Visual Studio, VS Code,
  cmd/PowerShell, and Copilot CLI.

---

## 8. How quack differs from a "code review agent"

quack is **not** a general LLM code reviewer. The distinction is architectural,
not cosmetic.

| Dimension | Typical code-review agent | quack |
|---|---|---|
| **Primary gate** | LLM opinion decides pass/fail | **Deterministic Tier 1** decides pass/fail; the LLM is never in the blocking path |
| **Determinism** | Same diff can yield different verdicts run to run | Blocking checks are pure regex/logic — identical every time |
| **When it runs** | Usually post-push, in CI, on a PR | **Locally, before the commit/push even leaves your machine** |
| **Failure mode** | If the model/API is down, review is blocked or skipped opaquely | **Fail-open**: no token/API means quack still enforces Tier 1 and reports AI as `skipped` |
| **Privacy** | Often sends the raw diff to a third party | Diff is **redacted** (`[REDACTED]`) before any network call; Tier 1 is 100% offline |
| **Scope of "AI"** | Broad, subjective style/logic opinions | Two bounded roles: a one-shot advisory reviewer, and a **budgeted investigator that runs real tests** |
| **Guessing vs. verifying** | Reasons over text; rarely executes anything | Agent **runs the actual test suite** (pytest / dotnet / jest) and treats the exit code as ground truth |
| **Safety** | Frequently has broad tool/file/shell access | Read-only tools, strict path containment, no shell, hard iteration/time/test budgets |
| **Cost & speed** | Every review is an API round-trip | Tier 1 is local and offline; AI runs through watch mode or at pre-push, never in the commit path |
| **Authority** | The agent's judgment is the verdict | The agent is **advisory**; a human still decides — it points at root cause and proposes a *minimal* patch |

**In one line:** a code-review agent asks a model _"does this look okay?"_;
quack **deterministically blocks the objectively-bad things offline**, then —
optionally and fail-open — uses AI to _redact-and-advise_ and to _investigate
by actually running your tests_. The LLM assists; it never holds the gate.

---

## 9. Guarantees summary

- [x] Secrets and merge markers are caught **offline and deterministically**.
- [x] Detected secrets are **redacted** before any AI call.
- [x] AI/gitleaks unavailability **never** breaks your workflow (fail-open).
- [x] The agent can only **read** files and run **whitelisted** tests, within the
  repo, under strict budgets, with no shell.
- [x] Only Tier 1 owns the exit code.
