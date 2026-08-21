# quack - Current State

> A verified, code-grounded snapshot of this repository at `v0.3.0`.
> Every implementation claim below was checked against all 20 Python modules
> under `src/quack/` (including `providers/`), all 19 files under `tests/`,
> `pyproject.toml`, and `.pre-commit-hooks.yaml`. Source and executable results
> take precedence over earlier documentation and design descriptions.
>
> Validation on this checkout: **206 tests passed** (5.56s on the final warmed
> run; 11.34s on the first run). `HEAD`, the local
> `v0.3.0` tag, and the `v0.3.0` tag advertised by `origin` all resolve to
> `afc3fa69301116b018cdcd79d0c096cec825e2a2`.

---

## 1. At a glance

| Property | Verified value |
|---|---|
| Version | `0.3.0` in `pyproject.toml` and `quack.__version__` |
| Python | `>=3.11` |
| Build backend | `hatchling` |
| Console entry point | `quack = "quack.cli:main"` |
| Runtime dependencies | `click`, `pyyaml`, `httpx`, `rich`, `github-copilot-sdk` |
| Source modules | 20 Python files: 17 top-level files, the providers package initializer, and 2 provider implementations |
| Tests | **206 collected and passing** |
| Pre-commit hook | `quack check` |
| Pre-push hook | `quack agent`, with `verbose: true` |
| Default provider | `copilot_sdk` |
| Alternate provider | `github_models` via `QUACK_PROVIDER=github_models` |
| Commit-time AI/network | **None**; `quack check` is local |
| Commit blockers | Deterministic `secrets` and `merge_markers` findings only |

A local wheel build and isolated installation succeeded as
`quack-0.3.0-py3-none-any.whl` (59,343 bytes). This verifies that the current
source is buildable and installable as version `0.3.0`; the matching remote tag
verifies the repository release surface. It does not, by itself, prove a PyPI
publication.

---

## 2. Implemented module architecture

| Area | Modules | Role and I/O boundary |
|---|---|---|
| CLI/orchestration | `cli.py`, `watch.py`, `agent.py` | Commands and control flow. `cli.py` also invokes `pre-commit` during installation. |
| Git/delta | `gitio.py`, `delta.py` | `gitio` is the subprocess adapter; `delta` parses and classifies supplied text. |
| Deterministic review | `tier1.py`, `gitleaks.py`, `testmap.py` | Added-line checks, optional local gitleaks subprocess, and filesystem-only test discovery. |
| AI review | `tier2.py`, `jsonparse.py`, `instructions.py` | Prompt construction, structured-result validation, deterministic risk anchoring, and local instruction loading. |
| Provider seam | `llmio.py`, `providers/github_models.py`, `providers/copilot_sdk.py` | Provider selection and the only model transports. |
| Local persistence | `reviewcache.py`, `metrics.py` | Fail-open per-user JSON cache and JSONL metrics. |
| Test execution | `runio.py` | Fixed `pytest` and `dotnet test` subprocess shapes. |
| Output | `render.py` | Rich output for nearly all commands. |

Direct subprocess calls exist in `gitio.py`, `runio.py`, `gitleaks.py`, and the
installation path in `cli.py`. Network/model transport is confined to the two
provider modules. Filesystem reads and writes also occur where their feature
requires them: test mapping, instructions, cache, metrics, watch snapshots,
installation, and agent read/list tools.

The intended single-renderer rule has one current exception: `quack metrics`
uses `click.echo()` directly in `cli.py`. No built-in `print()` calls exist
outside Rich's `Console.print()` use in `render.py`.

---

## 3. Provider seam and model configuration

`llmio.py` selects a provider in this order:

1. `QUACK_PROVIDER`
2. `copilot_sdk` when the variable is unset

Known provider names are `copilot_sdk` and `github_models`. Unknown names,
provider import failures, and ordinary exceptions from `complete()` or `chat()`
are normalized to `LLMUnavailable`. `availability_error()` converts provider
availability failures to a readable value so command paths can fail open.

Provider-owned configuration is split by transport and use:

| Provider | Availability requirement | `DEFAULT_TIMEOUT_S` | Completion model | Agent model |
|---|---|---:|---|---|
| `copilot_sdk` | Copilot SDK importable; authentication is delegated to the Copilot CLI/runtime | 60s | `claude-haiku-4.5` | `claude-sonnet-4.5` |
| `github_models` | `GITHUB_TOKEN` present | 6s | `openai/gpt-4o-mini` | `openai/gpt-4.1` |

`copilot_sdk` deliberately does not read or forward `GITHUB_TOKEN`. Its adapter
flattens OpenAI-style messages, starts an async `CopilotClient`, sends one
prompt, and always attempts to stop the client. SDK logging is temporarily
suppressed and restored. Empty responses, timeouts, SDK failures, and discovery
failures become `LLMUnavailable`.

`github_models` posts to
`https://models.github.ai/inference/chat/completions`. The token is read from
the environment at call time and is not stored or logged. Completion requests
ask for a JSON object with `max_tokens=800` and `temperature=0.1`; chat requests
return the full assistant message for OpenAI-style tool calls.

`llmio.default_model(kind)` and `llmio.default_timeout()` ask the selected
provider. Model override order is `--model`, then `QUACK_MODEL`, then the
provider's use-specific default. The CLI retains `openai/gpt-4.1` only as an
agent fallback when no provider default can resolve.

---

## 4. CLI and hook surfaces

| Command | Current behavior |
|---|---|
| `quack check` | Local staged-delta checks, optional gitleaks, test guidance, and fail-open review-cache lookup. Exit 1 only for deterministic blockers. |
| `quack watch [--quiet-period 30] [--once]` | Polls the working tree and performs Tier 2 reviews after a quiet period, or performs one review immediately. |
| `quack agent [--model M] [--fly]` | Pre-push Tier 2 review followed by the tool-calling investigation loop; always exits 0. |
| `quack model [--model M]` | Read-only provider/auth/model/timeout diagnostic and, for Copilot SDK, reachable-model discovery. |
| `quack metrics` | Summarizes locally recorded events. |
| `quack install [--local]` | Writes both hook surfaces, installs pre-commit and pre-push hook types, and best-effort installs gitleaks. |

`.pre-commit-hooks.yaml` exposes two hooks:

- `quack`: `entry: quack check`, stage `pre-commit`, `pass_filenames: false`
- `quack-agent`: `entry: quack agent`, stage `pre-push`,
  `pass_filenames: false`, `verbose: true`

`quack install` writes both hook IDs. The published-repository stanza pins
`rev: v0.3.0`; local mode writes equivalent `language: system` hooks. Pre-push
installation failure warns and does not undo a successful pre-commit install.

---

## 5. `quack check`: fully local commit path

The live path is:

1. Read staged name/status, numstat, and unified diff through `gitio`.
2. Run built-in Tier 1 checks.
3. Unless `QUACK_DISABLE_GITLEAKS` is set, run local gitleaks, apply the shared
   inline allowlist, and merge findings.
4. Block immediately only when a finding's check name is `secrets` or
   `merge_markers`.
5. Build test guidance for an unblocked change.
6. Redact built-in secret matches, hash the redacted diff, and perform one
   fail-open local cache lookup.
7. Render a cached Tier 2 result with its age, or render the nudge
   `run quack watch` on a miss.
8. Append a check metrics event and exit 0.

There is no token check, provider call, or network fallback in `quack check`.
Tests replace Tier 2 with a function that raises and prove it is never called.
A cache hit is rehydrated locally and cannot alter the exit code.

Exit codes are:

- `1`: a deterministic `secrets` or `merge_markers` finding exists
- `0`: clean, advisory, cache hit/miss, unavailable local tooling, or no staged
  changes

The code does contain `StagedDelta.triviality()` for no-change, docs-only,
fewer-than-five-line, and generated/lockfile classification. **No production
caller uses it.** Because commit-time check performs no AI call anyway, this
has no check-path network consequence; watch and pre-push Tier 2 currently do
not skip trivial deltas.

---

## 6. Deterministic checks and diff handling

Tier 1 scans added lines only and derives new-file line numbers from hunk
headers. Removed lines do not fire checks or advance the new-file line counter.
Binary and configured generated/excluded files are skipped.

Implemented finding classes are:

| Check | Severity | Blocks | Examples |
|---|---|:---:|---|
| `secrets` | error | yes | AWS keys, private-key headers, Azure Storage keys, GitHub tokens, Azure DevOps PATs, Slack tokens, long assigned credentials |
| `merge_markers` | error | yes | seven-character conflict markers |
| `debug_code` | warning | no | `console.log`, breakpoints, focused tests, marked debug prints, selected C# console writes |
| `security_smell` | warning | no | `verify=False`, `shell=True` |
| `performance_smell` | warning | no | Python or .NET sleep calls |
| `large_file` | warning | no | on-disk staged path larger than 512 KiB |

`# quack: allow` and `# pragma: allowlist secret` suppress all built-in checks
on that added line and suppress a gitleaks result at the same `(path, line)`.
Secret-bearing lines are replaced by `[REDACTED]` in both `raw_diff` and parsed
hunks before a diff is handed to watch Tier 2 or the pre-push agent.

Unified diff text is truncated to 60,000 characters with an explicit marker in
`delta.parse_staged_delta()`. Rename/copy parsing uses the post-image path;
binary changes are represented without hunks.

This is useful large-delta hardening, but it occurs **after** `gitio` has captured
the complete Git stdout in memory. Git subprocesses have no timeout and no
streaming byte cap, so the implementation does not yet impose an end-to-end
memory or time bound for extremely large repository diffs.

---

## 7. gitleaks and test guidance

When present, gitleaks runs locally as:

`gitleaks protect --staged --no-banner --report-format json --report-path <tmp>`

Exit 0 and 1 are accepted. Missing binary, timeout, malformed report, OS error,
or another exit code produces no external findings. Results become blocking
`secrets` findings; built-in findings win duplicate `(path, line)` collisions.
The scan timeout is 10 seconds, so the optional tool can exceed the nominal
sub-second deterministic target even though it remains local and fail-open.

Test mapping builds one pruned filesystem index per call and supports Python
and C#:

- Python: `foo.py` maps to `test_foo.py` or `foo_test.py`; matched files are
  combined into `pytest ... -x`.
- C#: conventional `FooTests.cs`/`FooTest.cs` names are preferred, mirrored
  project-relative structure disambiguates matches, and a content probe is the
  fallback. Commands are grouped by owning test project.

The sibling-project regression is fixed: a source under a product project can
map to a conventionally named test in a sibling `*.Tests.csproj` or
`*.Test.csproj`. The generated command targets that sibling project, for
example `dotnet test <sibling.Tests.csproj> --no-build --filter ...`.

---

## 8. Watch mode and review cache

`quack watch` is a foreground, long-running polling process intended to run
alongside development. It is "background" relative to the commit hook; it does
not create a background thread or daemon itself.

The watcher:

- snapshots non-pruned paths every 2 seconds by default;
- starts dirty and waits for the configured quiet period (30 seconds by
  default);
- reviews the staged delta, falling back to tracked changes versus `HEAD`;
- runs Tier 1 redaction, test mapping, and repo-instruction loading;
- calls Tier 2 synchronously using the selected provider's completion model and
  timeout;
- stores a successful result under SHA-256 of the redacted diff.

Untracked files can change the filesystem snapshot but are absent from the Git
delta until tracked/staged. A review failure is reported and not cached.

The per-user cache is `%LOCALAPPDATA%/quack/review-cache.json` on Windows and
`$XDG_DATA_HOME/quack/review-cache.json` or
`~/.local/share/quack/review-cache.json` elsewhere. Reads and writes fail open;
writes use a temporary file plus `os.replace`, with short retries for transient
`PermissionError`. Entries expire after 24 hours, are capped at 20, and the
cache file is capped at 1 MB.

The cache stores normalized repository root plus the review payload. Review
payloads may contain source/test paths because they are developer-facing local
review results; this is distinct from the metrics file.

---

## 9. Tier 2 structured review

Tier 2 builds messages from changed-file metadata, test guidance, untested
sources, optional untrusted repo instructions, and the redacted diff. Repo
instructions are fenced and explicitly denied authority over the schema and
risk assessment.

A valid response must contain:

- `risk`: `low`, `medium`, or `high`
- `reasons`: at most three strings
- `tests_to_run`: list of strings
- `missing_tests`: list of strings
- `one_liner`: string

The parser tolerates JSON fences and surrounding prose, but the resulting
object must satisfy the schema. Invalid output receives one correction retry.
A second invalid response returns unavailable; raw model text is not rendered.
Transport failures normalize to a reason or `None`, depending on the wrapper.

A deterministic rubric scores untested behavior, public contracts,
state/concurrency, 200+ changed lines, behavior-sensitive paths, and boundary
logic. Final risk is the maximum of rubric and model risk, so repo instructions
or model output cannot lower the deterministic floor.

The old global "hard 6s Tier 2 timeout" description is no longer accurate.
`tier2.review()` defaults to 6 seconds, but live watch and pre-push paths pass
the provider timeout: 6 seconds for GitHub Models and 60 seconds for Copilot
SDK. An invalid response can cause a second provider call with the same
per-call timeout.

---

## 10. Metrics and `quack metrics`

`metrics.py` appends compact JSON lines beside the review cache:

- Windows: `%LOCALAPPDATA%/quack/metrics.jsonl`
- Linux/macOS default: `~/.local/share/quack/metrics.jsonl`
- `$XDG_DATA_HOME` is respected

The file rolls to `metrics.jsonl.1` when the current file is already larger
than 5 MiB. Logging and reading are fail-open; malformed individual lines are
ignored. `quack metrics` reports total runs, runs by command, blocks, finding
counts, median duration, and cache hit rate.

Live event producers are:

- `quack check`: durations, changed-file/line counts, finding counts, block,
  test counts, cache hit/miss, risk, and exit
- each `watch.review_once`: duration, file count, risk, and failure reason
- `quack agent`: duration, target enum, provider/model, Tier 2 outcome, agent
  outcome, and exit

The implementation does **not** log one line for every CLI invocation:
`model`, `metrics`, and `install` do not emit events, and a continuous watch
emits per review attempt rather than one event for process lifetime.

No diff, file-content, path, commit-message, or repository-name field is
written. `metrics.log()` applies a central allowlist of event keys, validates
numeric and risk values, and sanitizes failure strings by bounding their length
and redacting paths and token-like values. Tests also prove the check event
contains counts instead of changed paths or content.

---

## 11. `quack model`

`quack model` is implemented and read-only. It reports:

- selected provider and whether it came from `QUACK_PROVIDER` or the default;
- provider availability/auth status and a suggested fix;
- ambient `GITHUB_TOKEN`, `GH_TOKEN`, or `COPILOT_GITHUB_TOKEN` shadowing risk
  for the Copilot SDK, showing names and lengths but not values;
- separately resolved completion and agent models with configuration source;
- selected provider timeout;
- up to 15 reachable model IDs for an available Copilot SDK provider.

Model discovery is optional in `llmio`. `copilot_sdk` implements it by starting
the SDK client and calling `list_models()`. `github_models` does not implement
model discovery, and the CLI only attempts listing for `copilot_sdk`; therefore
"reachable model list" is not a provider-independent feature.

All diagnostic exceptions are caught and the command remains exit 0. Discovery
failure reasons are bounded to one line and known ambient token values are
redacted before rendering.

---

## 12. Pre-push Tier 2 and agent loop

`quack agent` prefers staged changes. If the index is empty, it resolves the
upstream and reviews the unpushed `<upstream>..HEAD` range. With neither target,
it exits 0.

It runs deterministic redaction once, then performs two AI surfaces:

1. a Tier 2 single-shot review using the provider's completion model;
2. the hand-written agent loop using the provider's agent model.

Tier 2 failure does not prevent the agent attempt. Both paths are advisory and
the command exits 0.

The agent loop uses OpenAI-style `tool_calls` through `llmio.chat`. It has three
read-only tools:

| Tool | Enforcement |
|---|---|
| `read_file` | Resolved path must remain in the repo; returns at most 300 lines. |
| `list_dir` | Resolved path must remain in the repo; returns sorted names. |
| `run_tests` | Accepts one contained `.csproj` plus optional validated filter, or contained `.py` paths; delegates only to fixed `dotnet test`/`pytest` adapters. |

Budgets are eight iterations, two test calls, and a 180-second loop clock.
Runner output is reduced to the last 80 lines and at most 6,000 characters.
Malformed final JSON is retried once. Non-zero test exit codes override a model
claim of no failures through deterministic reconciliation.

`--fly` affects rendering only: the default provides diagnosis/coaching and
hides a proposed patch; `--fly` reveals the proposed unified diff and an apply
hint. Quack never applies the patch.

### Provider requirement

`copilot_sdk.chat()` unconditionally raises
`LLMUnavailable("tool calling not supported on copilot_sdk provider")` because
it does not expose the OpenAI-style `tool_calls` contract used by this loop.
Therefore the **agentic tool loop currently requires
`QUACK_PROVIDER=github_models`**, even though the preceding Tier 2 review works
with the default Copilot SDK provider. Under the default provider, users receive
the Tier 2 result and then a graceful "AI analysis unavailable" agent result.

The source contains no SDK-native tool runtime integration and no verifiable
record of a completed SDK-native-tools investigation. Porting the loop to a
provider-specific runtime would be an architectural change; claims about the
exact budgets or reconciliation guarantees that would be lost cannot be
established from the current code and are not presented here as historical
fact.

---

## 13. Rendering and UX

Rich color semantics are fixed: red for blocking, yellow for warnings, green
for clean results, cyan for commands, and dim for metadata. Consoles are built
per call. `NO_COLOR` explicitly disables terminal styling, and non-TTY output
is plain; both paths are tested.

`quack check` renders findings as `path:line`, test commands as copy-pasteable
cyan lines, and a blocked alarm with exact line numbers. A clean no-change run
prints one line. A staged clean run uses a panel and may include test guidance
plus either cached AI output or the watch nudge, so the original "clean commits
print at most two lines" target is not enforced for every clean staged change.

The pre-push hook is verbose by metadata so its output remains visible when
pre-commit runs the hook during push.

---

## 14. Measured validation and performance

Measurements below were made on the development machine used to regenerate
this document. They are samples, not cross-machine latency guarantees.

| Measurement | Result |
|---|---:|
| Full pytest suite | 206 passed; 11.34s first run, 5.56s final warmed run |
| Seven end-to-end no-change `quack check` processes | median 1.700s; range 1.584-1.836s |
| Seven end-to-end staged Python checks in an isolated repo, gitleaks disabled | median 1.616s; range 1.557-2.021s |
| Local wheel build | succeeded; 59,343-byte wheel |

Both check timings include Python interpreter startup and imports. The staged
sample exercised Git delta parsing, Tier 1, Python test mapping, redaction,
cache lookup, rendering, and metrics, with output suppressed for timing.
It intentionally disabled gitleaks to isolate built-in quack work.

No current measurement establishes the old 3.61-second full path or a
sub-second Tier 1 guarantee on this checkout. If gitleaks is enabled, its own
configured timeout is 10 seconds. Copilot SDK review is explicitly outside the
commit path and has a 60-second per-call timeout.

---

## 15. Known gaps

### Agent compatibility and containment

- The default `copilot_sdk` provider cannot run the OpenAI-style agent tool
  loop; set `QUACK_PROVIDER=github_models` for the tool loop.
- Agent Python test targets are checked for `.py` suffix and repository
  containment, but they are **not** required to come from a precomputed
  allowlist of known test paths. The current implementation is narrower than
  arbitrary shell execution but does not meet a strict allowlist requirement.
- The agent also permits the fixed `dotnet test` shape, not pytest only.
- `_safe_path()` establishes lexical/resolved containment but does not require
  the selected test path to exist before handing it to the runner.

### Timeout and trivial-delta policy

- There is no single hard six-second Tier 2 budget across providers. Copilot SDK
  live calls receive 60 seconds each, and invalid JSON may trigger a second
  call.
- `StagedDelta.triviality()` is implemented and tested but unused by watch and
  pre-push review. Docs-only, config/generated-only, and tiny deltas are not
  skipped before those network/provider calls.

### Cache and metrics validation

- Cache rehydration catches missing/wrong top-level types but does not re-run
  Tier 2's strict schema validator. For example, it coerces several cached
  fields with `list()`/`str()` and does not revalidate risk membership.
- Metrics payload privacy uses a central allowlist and sanitizer, but the
  permitted provider/model strings and bounded failure reasons still need
  periodic review as telemetry evolves.
- Metrics are not emitted by every command, and `quack metrics` bypasses the
  single Rich renderer with `click.echo()`.

### Large repositories and latency

- Unified diff truncation happens after full Git stdout capture; Git calls have
  no timeout or streaming size limit.
- Watch snapshots walk the repository and test mapping rebuilds a filesystem
  index for each review/check. Pruning helps, but there is no measured bound for
  very large repositories in the current suite.
- Optional gitleaks has a 10-second timeout, so the complete deterministic hook
  path is not hard-bounded below five seconds.

### Model diagnostics and live integration

- Reachable-model listing is implemented only for Copilot SDK, not GitHub
  Models.
- Provider tests use fakes/mocks; the suite does not exercise a real Copilot
  login, GitHub Models request, gitleaks binary, or remote model inventory.
- The remote `v0.3.0` Git tag and local wheel build were verified. PyPI
  publication was not established and should not be inferred.

### UX and architecture drift

- Clean staged commits are not guaranteed to fit within two terminal lines.
- `cli.py` directly runs `pre-commit` for installation, so subprocess use is not
  confined exclusively to `gitio.py`, `llmio.py`, and `runio.py`; gitleaks also
  has its own subprocess adapter.
- The package dependency set includes `github-copilot-sdk`, beyond the original
  stdlib + Click + PyYAML + httpx + Rich list. There is still no LangChain or
  general agent framework; the loop remains inspectable project code.
