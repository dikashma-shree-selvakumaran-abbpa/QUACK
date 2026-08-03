# quack — Current State

> A verified, code-grounded snapshot of what exists in this repository today.
> Every claim below was checked against source, not against documentation.
>
> Generated from a full read of `src/quack/*.py` (12 modules), `tests/` (91 tests),
> `pyproject.toml`, and `.pre-commit-hooks.yaml`.

---

## 1. At a glance

| Property | Value |
|---|---|
| Version | `0.1.0` (`pyproject.toml`) |
| Python | `>=3.11` |
| Build backend | `hatchling` |
| Entry point | `quack = "quack.cli:main"` |
| Runtime deps | `click>=8.1`, `pyyaml>=6.0`, `httpx>=0.27`, `rich>=13.0` |
| Dev deps | `pytest>=8.0` |
| Source modules | 12 |
| Tests | **91 collected, passing** |
| Hook stages wired | `pre-commit` only |
| LLM endpoint | `https://models.github.ai/inference` |
| Auth | `GITHUB_TOKEN` env var (`models:read`) |

Dependency rule (stdlib + click + pyyaml + httpx + rich, nothing else) is **held**.
No LangChain, no agent framework — the agent loop is hand-written.

---

## 2. Module inventory

| Module | LOC | Role | Touches I/O? |
|---|---:|---|---|
| `cli.py` | 304 | Click command group, orchestration | subprocess (`pre-commit install`) |
| `delta.py` | 264 | Parse raw git output → dataclasses | **no** — pure |
| `gitio.py` | 72 | Thin git adapter | subprocess (git) |
| `tier1.py` | 374 | Deterministic checks + redaction | `os.path.getsize` only |
| `gitleaks.py` | 212 | Optional gitleaks layer | subprocess (gitleaks) |
| `testmap.py` | 375 | Map changed sources → tests | filesystem reads |
| `tier2.py` | 310 | AI review orchestration + risk rubric | **no** — delegates to `llmio` |
| `llmio.py` | 138 | HTTP adapter for GitHub Models | network (httpx) |
| `runio.py` | 54 | Test-runner adapter | subprocess (pytest/dotnet) |
| `agent.py` | 548 | Agentic pre-push loop | delegates to `llmio`/`runio` |
| `jsonparse.py` | 86 | Lenient model-JSON parsing | **no** — pure |
| `render.py` | 369 | Single terminal output module | stdout/stderr |

**Rule #5 (testability) verified.** Only four modules call `subprocess`: `gitio`, `runio`, `gitleaks`, and `cli` (for `pre-commit install`). Only `llmio` touches the network.

**Rule #8 (single renderer) verified.** No `print()` calls outside `render.py`.

---

## 3. CLI surface

| Command | Status | Behavior |
|---|---|---|
| `quack check [--model M]` | ✅ Complete | Tier 1 + gitleaks + test plan + Tier 2 on staged diff |
| `quack agent [--model M] [--fly]` | ✅ Complete | Agentic investigation loop; **always exits 0** |
| `quack install [--local]` | ✅ Complete | Writes pre-commit config, installs hook, bootstraps gitleaks |
| `quack model` | ⚠️ **Stub** | Prints `"quack model: not implemented yet"`, exits 0 |

### Model resolution (two distinct defaults)

```
check:  --model  >  $QUACK_MODEL  >  "openai/gpt-4o-mini"
agent:  --model  >  $QUACK_MODEL  >  "openai/gpt-4.1"     ← stronger, for multi-step reasoning
```

### Environment variables honored

| Variable | Effect |
|---|---|
| `GITHUB_TOKEN` | Required for Tier 2 / agent. Read at call time, never stored. |
| `QUACK_MODEL` | Overrides default model for both commands. |
| `QUACK_DISABLE_GITLEAKS` | Skips the gitleaks layer entirely. |
| `NO_COLOR` | Strips all ANSI output (explicitly honored, not just delegated to rich). |

---

## 4. `quack check` — flow

```
gitio.staged_delta()
   ├─ git diff --cached --name-status -M
   ├─ git diff --cached --numstat -M
   └─ git diff --cached -M --unified=3
		↓ delta.parse_staged_delta()
   StagedDelta { files[], raw_diff }
		↓
tier1.run(delta, Tier1Config())            → list[Finding]
		↓
gitleaks.scan_staged() → filter_allowlisted() → merge()    [unless QUACK_DISABLE_GITLEAKS]
		↓
should_block(findings, ("secrets", "merge_markers"))
   ├─ True  → render.report(blocked=True) → exit 1        ← STOPS HERE
   └─ False ↓
testmap.build_plan(delta)                  → TestPlan
		↓
delta.triviality()
   ├─ trivial     → ai = ("skipped", reason)              ← no network call
   └─ non-trivial → tier2.review(...)      → ReviewResult | None
		↓
render.report(...) → exit 0
```

### Exit codes
- `0` — clean, advisory, or AI unavailable
- `1` — **only** when a `secrets` or `merge_markers` finding exists

---

## 5. Tier 1 — deterministic checks

Scans **added lines only** (`+`, excluding `+++`). Removed lines never fire and do not
advance the new-file line counter. Line numbers come from parsing `@@ -a,b +c,d @@` headers.

### Secret patterns (`_SECRET_PATTERNS`, ordered)

| Message | Pattern |
|---|---|
| AWS access key id | `AKIA[0-9A-Z]{16}` |
| private key block | `-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----` |
| Azure Storage account key | `AccountKey=[A-Za-z0-9+/=]{60,}` |
| GitHub token | `gh[posur]_[A-Za-z0-9]{20,}` \| `github_pat_[A-Za-z0-9_]{20,}` |
| Azure DevOps PAT | `\b[a-z2-7]{52}\b` |
| Slack token | `xox[baprs]-[A-Za-z0-9-]{10,}` |
| hardcoded credential | `(?i)(key\|secret\|token\|password)\s*[=:]\s*['"][^'"]{16,}['"]` |

### All checks

| Check | Severity | Blocks | Trigger |
|---|---|:---:|---|
| `secrets` | error | ✅ | patterns above |
| `merge_markers` | error | ✅ | `^(<{7}\|={7}\|>{7})` |
| `debug_code` | warn | ❌ | `console.log(`, `breakpoint()`, `pdb.set_trace()`, `it.only(`, `describe.only(`, `Debugger.Break()`, `print(` near `HERE`/`DEBUG`/`xxx`, `Console.WriteLine` in non-test/non-CLI `.cs` |
| `security_smell` | warn | ❌ | `verify\s*=\s*False`, `shell\s*=\s*True` |
| `performance_smell` | warn | ❌ | `time.sleep(`, `Thread.Sleep(` |
| `large_file` | warn | ❌ | on-disk size > 512 KB |

### Inline allowlist
`# quack: allow` or `# pragma: allowlist secret` on an added line suppresses **all**
checks on that line — and via `allowlisted_locations()` it suppresses **gitleaks too**.

### ⚠️ `should_block` matches on check name only

```python
def should_block(findings, block_on) -> bool:
	blockers = set(block_on)
	return any(f.check in blockers for f in findings)   # severity NOT consulted
```

In practice equivalent, since `secrets` and `merge_markers` are only ever constructed
with `severity="error"` — but the severity field is not a gate. Worth knowing before
adding any non-error finding under those check names.

### Redaction
`tier1.redact()` replaces any line containing matched secret text with `[REDACTED]`
(preserving the leading `+`/`-`/space), in **both** `raw_diff` and per-file `hunks`.
Applied by `tier2.build_messages()` before the diff reaches the model.

---

## 6. gitleaks power mode

- Invoked as `gitleaks protect --staged --no-banner --report-format json --report-path <tmp>`
- Exit `0` (clean) or `1` (leaks) accepted; **anything else treated as tool error → `[]`**
- 10s timeout; auto-installed at `quack install` via `winget` (Windows) / `brew` (macOS/Linux)
- Fully fail-open: missing binary, timeout, unparseable JSON → `[]`

**Findings are emitted as `check="secrets"`, `severity="error"` — so gitleaks results block commits.**

`merge()` de-duplicates by `(path, line)`; built-in findings win because their messages are curated.

---

## 7. Test mapping

### C# (priority order)
1. **Conventional name** — `Foo.cs` → `FooTests.cs` / `FooTest.cs` under any `*.Tests`/`*.Test` project
2. **Mirrored structure** — `src/Lib/Sub/Foo.cs` → `Lib.Tests/Sub/FooTests.cs`
3. **Content probe** — regex for `class FooTests` or the changed class name

Test project = nearest ancestor `.csproj` matching `\.Tests?\.csproj$`.
One `dotnet test <project> --no-build` per project.

### Python
`foo.py` → `test_foo.py` / `foo_test.py` anywhere, or `src/` → `tests/` mirror.
Single `pytest <files> -x --tb=short -q`.

Pruned dirs: `.git`, `.hg`, `.svn`, `bin`, `obj`, `node_modules`, `__pycache__`,
`.venv`, `venv`, `.tox`, `.mypy_cache`, `.pytest_cache`, `packages`.
Index built once per run.

---

## 8. Tier 2 — AI review

### Contract
- **One** call to `chat/completions`, `response_format: json_object`, `temperature 0.1`, `max_tokens 800`
- **6s hard timeout**
- Invalid JSON → retry once with `_RETRY_MESSAGE` → second failure returns `None`
- `LLMUnavailable` → `None`
- `cli._run_tier2` additionally wraps in bare `except Exception` — **Tier 2 can never crash the hook**

### Schema (strictly validated; any violation → `None`)
```json
{
  "risk": "low|medium|high",
  "reasons": ["≤3 strings"],
  "tests_to_run": ["..."],
  "missing_tests": ["..."],
  "one_liner": "..."
}
```

### Trivial skip (before any network call)
| Condition | Reason string |
|---|---|
| no files | `no staged changes` |
| all files are docs (`.md`, `.rst`, `.txt`, `.adoc`, `docs/`) | `docs-only change` |
| `< 5` changed lines and no binary | `small change (<5 lines)` |
| all files match lockfile/generated globs | `only lockfiles/generated files` |

### Deterministic risk rubric — `_deterministic_risk()`

Model-independent. Same diff → same baseline across runs and model swaps.

| Signal | Score |
|---|---:|
| `test_plan.untested_sources` non-empty | +2 |
| public contract changed (`class\|interface\|enum\|record\|def\|function`) | +2 |
| state/concurrency (`lock\|mutex\|semaphore\|thread\|async\|await\|delegate\|event\|callback\|state`) | +2 |
| ≥200 changed lines | +1 |
| path hints (`auth`, `login`, `token`, `payment`, `billing`, `order`, `checkout`, `wallet`, `render`, `draw`, `capture`, `transform`, `state`, …) | +1 |
| boundary (`<=`, `>=`, `==`, `!=`, `boundary\|index\|offset\|limit\|range\|count\|length\|size`) | +1 |

`score >= 4 → high`, `>= 2 → medium`, else `low`.

### Anchoring
`_anchor_result()` takes `max(rubric, model)`. **The model can raise risk but never lower it.**
`risk_basis` records `"rubric"` or `"max(rubric, model)"`; `model_risk` preserves the raw label.
This is also the defense against prompt injection via project instructions.

---

## 9. `quack agent` — flow

Guards (each exits **0**): no `GITHUB_TOKEN` → no repo root → nothing staged.

```
messages = [SYSTEM_PROMPT, "Accumulated local changes (staged diff):\n\n" + diff]

loop ≤ 8 iterations:
	elapsed > 180s → break
	llmio.chat(messages, tools=TOOLS)
	├─ LLMUnavailable      → _unavailable(reason) → RETURN
	├─ no tool_calls       → _validate_with_retry → _finalize("invalid final JSON") → RETURN
	└─ tool_calls          → _dispatch each, append {role: tool} results

budget exhausted:
	append _FINAL_INSTRUCTION
	llmio.chat(tools=None)  →  _validate_with_retry  →  _finalize("no conclusion reached")
```

### Tools

| Tool | Containment | Budget |
|---|---|---:|
| `read_file` | `_safe_path()` (`.resolve()` + `relative_to(root)`), first 300 lines | 0 |
| `list_dir` | `_safe_path()`, sorted entry names | 0 |
| `run_tests` | `.csproj` → `_run_dotnet`, all `.py` → `_run_pytest` | **1** (max 2) |

- C# `--filter` validated against `^[A-Za-z0-9_.~|&="\s-]+$` before any subprocess call
- Exactly one `.csproj` permitted before `--filter`
- All runner output normalized to `exit_code={N}\n` + last 80 lines, capped at 6000 chars
- **Every tool returns a string; no tool raises.** Errors become model-readable text.

### Budgets
`MAX_ITERATIONS = 8`, `MAX_RUN_TESTS = 2`, `WALL_CLOCK_S = 180.0`, `READ_FILE_MAX_LINES = 300`

### Ground-truth reconciliation
If any `run_tests` output has non-zero `exit_code` **and** the model reported no failures,
`_reconcile()` synthesizes failures from `(?:Failed|FAILED)\s+(\S+)` and prefixes the summary:

> `[verified] N test(s) failed per tool output, overriding model's self-report.`

Deterministic — **no extra LLM call**. Tool exit codes outrank model narrative.

### `--fly`
Affects **rendering only**. It is passed to `render.agent_report(result, fly=fly)` and never
reaches `agent.run()`. Both modes run identical prompts, tools, budgets, and produce an
identical `AgentResult`. Single divergence at `render.py:346`:

- **default** → dim hint: *"A proposed fix is available. Try fixing it yourself…"*
- **`--fly`** → `PROPOSED -- not applied` rich Panel + `apply with: git apply <<'EOF' ... EOF`

Never writes files in either mode. `sys.exit(0)` unconditionally.

---

## 10. Rendering

Fixed color semantics: `red`=blocking, `yellow`=warning, `green`=clean, `cyan`=command, `dim`=metadata.

`Console` is constructed **per call** so current `sys.stdout` and env are always honored.
`NO_COLOR` sets both `no_color=True` and `force_terminal=False` so bold/dim escapes are
suppressed too, not just colors.

| Function | Output |
|---|---|
| `clean` / `warning` / `blocking` / `command` / `metadata` / `info` | one styled line (`blocking` → stderr) |
| `install_banner()` | ASCII duck + QUACK wordmark panel |
| `report(...)` | full `quack check` verdict Panel |
| `agent_report(result, fly)` | agent verdict |

`report()` composes up to four sections separated by dim rules: findings table,
`🐤 QUACK!!!! check line #N` alarm (blocked only), test guidance, AI verdict.
Subtitle is either `🐤 BLOCKED - fix and re-stage` or `advisory: commit allowed`.

---

## 11. Test suite

**91 tests, all passing** (`python -m pytest -q`).

| File | Tests |
|---|---:|
| `test_agent.py` | 18 |
| `test_tier1.py` | 17 |
| `test_delta.py` | 15 |
| `render_test.py` | 11 |
| `test_testmap.py` | 11 |
| `test_gitleaks.py` | 8 |
| `test_tier2.py` | 8 |
| `test_cli_check.py` | 3 |

No network and no git required — `llmio` is monkeypatched, delta parsing is fed raw strings.

---

## 12. Design rule compliance

| # | Rule | Status |
|:--:|---|---|
| 1 | **Fail-open** | ✅ Tier 2 wrapped in `except Exception`; agent always exits 0; gitleaks returns `[]` on any error |
| 2 | **Latency** | ⚠️ 6s Tier 2 timeout ✅, trivial skip ✅ — but **hook duration is never measured** |
| 3 | **Structured output** | ✅ Schema validation + one retry + graceful degrade, in both Tier 2 and agent |
| 4 | **Security** | ⚠️ Token env-only ✅, whitelist ✅, path containment ✅ — but **agent path is unredacted** (§13) |
| 5 | **Testability** | ✅ Only 4 modules use subprocess; only `llmio` uses network |
| 6 | **Observability** | ❌ **Not implemented** — no metrics file, zero references in code |
| 7 | **UX** | ✅ Quiet default, `path:line` findings, copy-pasteable commands, exit 0/1 |
| 8 | **Dependencies** | ✅ Exactly the 4 allowed deps; single `render.py`; `NO_COLOR` + non-TTY honored |

---

## 13. Known gaps

### ❌ Not implemented

**Metrics (Rule #6).** No `~/.local/share/quack/metrics.jsonl`. Zero code references.
This is the stated evidence base for the pilot — currently there is no record of what
quack reported on any run.

**`quack model` body.** Stub only. This is the diagnostic for the most common question
("why did AI skip?"), and it doesn't exist.

**Hook duration.** `render.report()` already accepts `duration: float = 0.0` and renders it
into the title, but `cli.check()` never passes it. The `# TODO: thread the real hook
duration through to render.report` comment is still live. Title currently always shows `0.0s`.

### ⚠️ Plumbed but dead

**Project instructions.** `tier2.review()` accepts `project_instructions`, and
`build_messages()` renders it into the user message under *"Project instructions (follow
these local conventions first)"*. **Nothing ever populates it.** `cli._run_tier2()` calls
`tier2.review(delta, findings, plan, model=model)` with no instructions argument, and no
loader module exists. `.github/copilot-instructions.md` exists in this repo and is never read.

> This is a documentation discrepancy: FEATURES.md claims *"Project instructions loaded
> from instructions.md / .github/copilot-instructions.md etc. (up to 4 000 chars)"* — that
> feature does **not** ship. The prompt slot and parameter exist; the loader does not.

### ⚠️ Security gap

**Agent diff is not redacted.** `cli.agent()` passes `delta.raw_diff` straight to
`agent_mod.run()`. It never calls `tier1_run()`, so `tier1.redact()` is never applied.
Rule #4 requires redacting Tier-1-detected secrets from *any* diff sent to the LLM — this
holds for Tier 2 but **not** for the agent path. A staged AWS key would be transmitted verbatim.

Fix is small: run Tier 1, redact, then pass the redacted diff.

### 📄 Other documentation drift

**Diff cap.** FEATURES.md says *"16 KB diff cap"*. Actual value is
`MAX_RAW_DIFF = 60_000` chars in `delta.py`, with marker
`"... [diff truncated at 60000 chars] ..."`.

### 🔌 Integration gaps

- `.pre-commit-hooks.yaml` declares `stages: [pre-commit]` only — **consumers cannot opt into pre-push**, so the fully-built `quack agent` is unreachable from any hook
- No `.github/workflows/` — no CI usage
- No machine-readable output (`--format json` / `sarif`), so agents and IDEs can only read the exit code
- No range mode — `quack check` reads the staged index, so it verifies **nothing** in CI (fresh checkout has an empty index → `nothing staged` → exit 0)

---

## 14. Summary

**Solid and complete:** Tier 1 checks with redaction, gitleaks layering with shared allowlist,
test mapping for C#/Python/JS, Tier 2 with deterministic risk anchoring, the full agent loop
with enforced safety invariants and ground-truth reconciliation, and a disciplined single-renderer
output layer. 91 passing tests with no network or git dependency.

**The three real gaps:** metrics (no pilot evidence), the dead instructions-loader path
(the intended differentiator, ~70% built), and the unredacted agent diff (a Rule #4 violation).

**The integration ceiling:** everything is currently reachable only through `pre-commit` at
commit time, in human-readable form. Pre-push, CI, and machine-readable output are all
unbuilt, which caps how far the existing functionality can travel.
