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
| I/O adapters | `gitio`, `llmio`, `runio`, `gitleaks` | Talk to git, GitHub Models, test runners, and the gitleaks binary. |
| Orchestration | `cli`, `tier2`, `agent` | Wire the pieces together; own exit codes and UX. |
| Presentation | `render` | The single module that writes to the terminal. |

**Design invariant:** only Tier 1 governs the exit code. gitleaks, Tier 2 AI,
and the agent are all advisory / fail-open and can never turn a passing commit
into a failing one due to their own unavailability.

---

## 2. CLI surface

| Command | Purpose | Exit-code authority |
|---|---|---|
| `quack check [--model M]` | Full pre-commit gate on **staged** changes (what the hook runs). | Tier 1 only |
| `quack agent [--fly] [--model M]` | Pre-push investigative agent over the **staged** diff. `--fly` allows a proposed patch. | Advisory (informational) |
| `quack install [--local]` | Write `.pre-commit-config.yaml`, install the hook, bootstrap gitleaks, banner, optional token setup. `--local` targets any repo without a published remote. | n/a |
| `quack model` | Print the resolved model id. | n/a |

**Model resolution order:** `--model` flag → `QUACK_MODEL` env var → default.
- `check` default: `openai/gpt-4o-mini` (one-shot review, cheap/fast).
- `agent` default: `openai/gpt-4.1` (multi-step tool use needs more reasoning).

**Relevant env vars:** `GITHUB_TOKEN` (AI features), `QUACK_MODEL` (model
override), `QUACK_DISABLE_GITLEAKS` (skip gitleaks), `NO_COLOR` (plain output).

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

## 5. Tier 2 — advisory AI review

**Modules:** `tier2.py` + `llmio.py` · **Fail-open. Never changes the exit code.**

- Only runs on an **unblocked** commit, and only when the change is non-trivial
  (`delta.triviality()` short-circuits tiny/whitespace-only diffs).
- **Privacy:** the diff is **redacted before it leaves the machine.**
  `tier1.redact()` replaces every detected secret with `[REDACTED]`, and Tier 2
  builds its prompt from that redacted delta (`"Staged diff (redacted):"`).
- **Transport:** `llmio.complete()` POSTs to the GitHub Models endpoint
  (`https://models.github.ai/inference/chat/completions`) using `GITHUB_TOKEN`,
  read at call time. All transport failures normalize to `LLMUnavailable` — no
  token, network error, or bad response ever crashes the hook; Tier 2 just
  reports `skipped`.
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
- **Token onboarding:** after install, quack optionally prompts (hidden input)
  for a `GITHUB_TOKEN`, persists it via `setx` on Windows, and skips cleanly
  when non-interactive or already set. Opt-in and never fatal.
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
| **Cost & speed** | Every review is an API round-trip | Tier 1 is milliseconds and offline; AI only fires when it adds value (non-trivial diffs) |
| **Authority** | The agent's judgment is the verdict | The agent is **advisory**; a human still decides — it points at root cause and proposes a *minimal* patch |

**In one line:** a code-review agent asks a model _"does this look okay?"_;
quack **deterministically blocks the objectively-bad things offline**, then —
optionally and fail-open — uses AI to _redact-and-advise_ and to _investigate
by actually running your tests_. The LLM assists; it never holds the gate.

---

## 9. Guarantees summary

- ✅ Secrets and merge markers are caught **offline and deterministically**.
- ✅ Detected secrets are **redacted** before any AI call.
- ✅ AI/gitleaks unavailability **never** breaks your workflow (fail-open).
- ✅ The agent can only **read** files and run **whitelisted** tests, within the
  repo, under strict budgets, with no shell.
- ✅ Only Tier 1 owns the exit code.
