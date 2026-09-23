# How to use Quack Sonar with the Alarms worktrees

This walkthrough builds Quack from source, initializes the two retained Alarms
validation worktrees, runs the deterministic Sonar check with `quack watch`,
uses the generated Sonar skills with the read-only SonarQube MCP tools, and
verifies the pre-commit gate.

The worktrees used by this walkthrough are:

```powershell
$clean = "C:\Dev\Workspace\alarms\quack-sonar-validation\clean"
$violations = "C:\Dev\Workspace\alarms\quack-sonar-validation\violation"
```

Do not run initialization in the original dirty checkout:

```text
C:\Dev\Workspace\alarms\main\prestine\Operations.HMI.App.Alarms
```

## 1. Build and install Quack

Open PowerShell and create an isolated Python environment in the Quack
checkout:

```powershell
Set-Location "C:\Dev\Workspace\AI-Champs\QUACK"

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --editable ".[dev]"
```

Verify the source checkout before using it from another repository:

```powershell
python -m pytest -q
quack --help
pre-commit --version
podman --version
```

The Quack test suite should pass. Podman is required for the Sonar MCP
adapter, while `pre-commit` is required for the Git hook. The optional AI
review also requires a signed-in Copilot CLI:

```text
copilot
/login
```

Quack's deterministic Sonar check does not require an LLM.

When installing from Git instead of an editable checkout, remember that pip
installs the selected committed revision. It does not include uncommitted
changes in another working tree. After the Sonar changes have been pushed,
install the branch explicitly:

```powershell
python -m pip install --upgrade `
  "git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK.git@feat/sonar-mcp-integration"
```

Use `pip install --editable .` while testing local changes, or push and
reinstall the branch after committing them. Verify the installed source and
CLI before running the worktree checks:

```powershell
python -c "import quack; print(quack.__file__)"
quack sonar-mcp --help
```

## 2. Initialize both Alarms worktrees

`quack init` updates the local pre-commit configuration, installs the
pre-commit and pre-push hooks when `pre-commit` is available, and creates
these repo-scoped Copilot artifacts:

```text
.github/skills/sonar-check/SKILL.md
.github/skills/sonar-fixes/SKILL.md
.github/agents/sonar-code-review.agent.md
```

Initialize the clean and violation worktrees separately:

```powershell
$clean = "C:\Dev\Workspace\alarms\quack-sonar-validation\clean"
$violations = "C:\Dev\Workspace\alarms\quack-sonar-validation\violation"

foreach ($repo in @($clean, $violations)) {
    Push-Location $repo
    quack init --local
    Pop-Location
}
```

The command is idempotent. Existing skill, agent, and unrelated hook content
is preserved rather than overwritten. Verify the generated files and staged
fixtures:

```powershell
Get-ChildItem "$clean\.github\skills\sonar-check", "$clean\.github\skills\sonar-fixes", "$clean\.github\agents"
Get-ChildItem "$violations\.github\skills\sonar-check", "$violations\.github\skills\sonar-fixes", "$violations\.github\agents"

git -C $clean status --short
git -C $violations status --short
```

The clean fixture is:

```text
FrontEnd/packages/alarms/src/quack-sonar-clean.ts
```

The deliberate violation fixture contains unsafe `innerHTML`, `eval`, and
loose equality:

```text
FrontEnd/packages/alarms/src/quack-sonar-violation.ts
```

## 3. Configure SonarQube and Podman

Use a SonarQube user token. Do not put the token in `.vscode\mcp.json`,
`.pre-commit-config.yaml`, source files, or a committed PowerShell script.
The following variables live only in the current PowerShell process:

```powershell
$env:SONARQUBE_URL = "https://codescan.abb.com"
$env:QUACK_SONAR_HOST_URL = "https://codescan.abb.com"
$env:SONARQUBE_PROJECT_KEY = "Operations.HMI.App.Alarms"
$env:QUACK_SONAR_PROJECT_KEY = "Operations.HMI.App.Alarms"
$env:SQ_TOKEN = "<TOKEN>"
$env:QUACK_SONAR_MCP = "auto"
$env:QUACK_SONAR_TIMEOUT_S = "180"
```

`SONAR_TOKEN`, `SQ_TOKEN`, and `SONARQUBE_TOKEN` are accepted token names.
Prefer one name and never echo it.
The scanner timeout is bounded to 300 seconds; 180 seconds is the default so
the first FrontEnd run has time to download Sonar analyzers and runtimes.

Quack uses the generic `sonar-scanner` path for the Alarms FrontEnd. It first
checks `PATH`, the target repository's `tools\sonar-scanner-*\bin`, and Quack's
own `tools\sonar-scanner-*\bin`. If a different installation is required, set
an explicit path:

```powershell
$env:QUACK_SONAR_SCANNER = "C:\Tools\sonar-scanner\bin\sonar-scanner.bat"
```

If Quack is installed from a wheel or the tools directory is elsewhere, point
discovery at the directory containing `sonar-scanner-*` folders:

```powershell
$env:QUACK_TOOLS_DIR = "C:\Dev\Workspace\AI-Champs\QUACK\tools"
```

An invalid explicit scanner path falls back to these discovered locations.

The scanner must produce `.scannerwork\report-task.txt` and a completed
server-side task before Quack records a fresh staged result. Watch and check
then query the Sonar Web API directly; Podman and MCP are not used by these
automatic checks. The standalone `quack sonar-mcp` command still requires
Podman when you explicitly use it:

```powershell
podman machine start
podman pull mcp/sonarqube
```

The MCP adapter mounts the selected repository read-only and exposes only
read-only Sonar tools. If Codescan supports project branches, run the two
worktrees serially with distinct branch names:

```powershell
$env:QUACK_SONAR_BRANCH = "quack-sonar-clean"
$env:SONARQUBE_BRANCH = "quack-sonar-clean"
$env:QUACK_SONAR_MCP_PROJECT_PATH = $clean
```

For the violation worktree, use `quack-sonar-violation` instead. If branch
analysis is not supported, use the project's documented branch and run the
worktrees one at a time; do not run competing analyses concurrently.
When switching worktrees in one PowerShell session, set both branch variables
again because `QUACK_SONAR_BRANCH` takes precedence over
`SONARQUBE_BRANCH`. `quack watch` and `quack check` scan the current worktree
snapshot and query the configured Sonar host directly. `--project-path` and
`QUACK_SONAR_MCP_PROJECT_PATH` apply only to the standalone `quack sonar-mcp`
command.

## 4. Run Sonar with `quack watch`

`quack watch` uses the complete working delta, including staged, unstaged, and
non-ignored new files. It exports that snapshot to a temporary directory and
runs the local Sonar scanner before the optional AI review, so a new file is
actually analyzed rather than compared with an older server-side result.
After the bounded scanner task completes, Watch queries the Sonar Web API
against the configured project/branch and requires the latest analysis ID (or
post-scan analysis timestamp) to match the scanner result. It then reads every
open issue and review-required hotspot, filters them to changed files, and
prints every returned violation. If the scan or API result is unavailable,
Watch reports the result as unverified and will not treat an empty
issue list as clean. The generated `docs\SONARQUBE_REPORT.md` is excluded from
both the working snapshot and watch change detection.

Run a one-shot check in the clean worktree:

```powershell
Set-Location $clean
$env:QUACK_SONAR_BRANCH = "quack-sonar-clean"
$env:SONARQUBE_BRANCH = "quack-sonar-clean"
$env:QUACK_SONAR_MCP_PROJECT_PATH = $clean
quack watch --once
```

Run the continuous watcher when developing:

```powershell
quack watch
```

Use `Ctrl+C` to stop it. `quack watch --once` reports the Sonar status and
prints each returned violation with its rule, severity, component, line, and
message. It also refreshes `docs\SONARQUBE_REPORT.md`. The report is generated
output; review it locally and do not stage it as application source.

For troubleshooting, add `--debug`:

```powershell
quack watch --once --debug
```

Debug mode prints the exact scanner CLI properties and bounded scanner output,
followed by each direct Sonar API input and complete bounded JSON output,
including the selected analysis/task result. Tokens and known token-shaped
values are redacted; normal Watch output does not print these payloads.

### Sonar timing and performance

`quack watch --once --debug` now prints integer-millisecond timings for the
snapshot export, scanner process, server analysis wait, every direct API
request, the total API phase, and the total Watch call. A validation run of
the Alarms violation worktree measured:

| Phase | Time | Share of 146.5 s Watch total |
|---|---:|---:|
| Working snapshot export | 1.9 s | 1.3% |
| `sonar-scanner` process | 88.4 s | 60.3% |
| Compute Engine analysis wait | 48.0 s | 32.7% |
| Four direct Web API reads | 6.1 s | 4.2% |
| Other Watch/report work | 1.0 s | 0.7% |

The scanner plus server analysis accounts for about 93% of the runtime. The
direct Web API integration is not the main bottleneck. Exact times vary with
Codescan queue load, network/proxy latency, analyzer cache warmth, and project
size.

The staged pre-commit cache has the largest safe impact. In the same worktree,
a changed/stale staged result required a new scan and took 118.9 s; the next
unchanged `quack check` reused that completed analysis, re-queried current
findings for safety, and took 6.4 s (about 95% faster). Changing the index,
scanner configuration, project, host, or branch intentionally invalidates the
cache.

To reduce elapsed time without weakening correctness:

1. Use continuous `quack watch` while editing. It waits for a filesystem
   change instead of repeatedly rescanning an unchanged tree. Every separate
   `quack watch --once` invocation intentionally performs a fresh working
   snapshot scan.
2. Leave the staged index unchanged when rerunning pre-commit so Quack can use
   its scope-specific staged cache. Do not clear Quack's Git-local state
   between runs.
3. Keep the scanner/analyzer download caches warm and use a stable scanner
   installation. A clean machine or newly downloaded analyzer is slower.
4. Set `QUACK_PROVIDER=disabled` when measuring Sonar alone. This removes the
   optional AI review from the remainder, but does not shorten the scanner or
   Codescan processing time.
5. Use the direct CLI/API path for Watch and pre-commit. Standalone
   `quack sonar-mcp` is intended for explicit agent/user queries, not the
   automatic gate. A measured `list_branches` invocation took 19.4 s wall
   time: the tool exchange was 9.6 s and the remaining time was primarily the
   separate safe tool-discovery/container exchange. `--debug` now prints the
   tool exchange `duration_ms`.
6. Set `QUACK_SONAR_TIMEOUT_S` high enough for observed server latency (for
   this project, 300 seconds is safer than 180). Raising it prevents false
   timeouts; it does not make successful scans faster.

Do not silently restrict `sonar.inclusions` to changed files or reuse a
working `.scannerwork` directory merely for speed. Those approaches can
publish a partial branch analysis, distort project measures, or mix snapshot
state. A future working-snapshot digest cache could safely accelerate repeated
`--once` calls, but it should be added only with the same freshness and
configuration identity guarantees as the staged cache.

The separate `AI review (advisory): ... risk: ...` line is a model-dependent
Tier 2 signal, not a Sonar finding or a commit decision. A `medium` or `high`
AI label can appear even when Sonar reports no open issues. To exercise only
the deterministic Sonar path, set `QUACK_PROVIDER=disabled`; watch will still
run Sonar and will report `review unavailable (no model configured)`.

Run the violation worktree serially:

```powershell
Set-Location $violations
$env:QUACK_SONAR_BRANCH = "quack-sonar-violation"
$env:SONARQUBE_BRANCH = "quack-sonar-violation"
$env:QUACK_SONAR_MCP_PROJECT_PATH = $violations
quack watch --once
```

Expected behavior for the deliberate fixture is a Sonar message similar to:

```text
SonarQube CLI: BLOCKED - <n> violation(s) detected
[HOTSPOT] <rule> [<severity>] <component>:<line> - <message>
review unavailable (no model configured)
```

The watcher exits successfully because watch is a reporting surface. The
blocking decision is enforced by `quack check` during pre-commit.

## 5. Use the Sonar MCP skill to find issues

`quack init` installs the `sonar-check` skill. Start Copilot from the target
worktree so the repository-scoped `.github` artifacts are loaded:

```powershell
Set-Location $violations
copilot
```

If Copilot is running from another directory, use `/add-dir` to add the
worktree before asking for a review. Ask Copilot to use the generated skill:

```text
Use the repository-scoped sonar-check skill. For project
Operations.HMI.App.Alarms and branch quack-sonar-violation, use only the
read-only SonarQube MCP tools. Check the current changed components, verify
analysis freshness and correlation, and report each confirmed open issue or
security hotspot with its key, rule, severity, file, and line. Do not modify
files and do not expose credentials.
```

The skill must distinguish a current correlated analysis from stale,
unavailable, or unverified server data. It must not ask an LLM to decide
whether a Sonar response contains a violation.

For direct MCP discovery outside Copilot, use Quack's read-only adapter:

```powershell
quack sonar-mcp `
  --project-path $violations `
  --project-key "Operations.HMI.App.Alarms"
```

This lists the advertised MCP tools. A specific read-only tool can be invoked
with JSON arguments:

```powershell
quack sonar-mcp `
  --project-path $violations `
  --project-key "Operations.HMI.App.Alarms" `
  --tool list_branches `
  --arguments '{}' `
  --json

quack sonar-mcp `
  --project-path $violations `
  --project-key "Operations.HMI.App.Alarms" `
  --tool search_sonar_issues_in_projects `
  --arguments '{"projects":["Operations.HMI.App.Alarms"],"branch":"quack-sonar-violation","issueStatuses":["OPEN"]}' `
  --json
```

Run `list_branches` first and use a branch that appears in its `branches`
array. SonarQube can return HTTP 200 with an empty `issues` array for a branch
that has never been analyzed; that is not evidence that the source is clean.
The deliberate `quack-sonar-violation` worktree must be scanned and uploaded
before its branch can return findings. The current Alarms project has `main`
and other analyzed branches, but a local Git worktree name does not create a
Codescan branch.

Tool names may be the native MCP name or the displayed `sonarqube_*` name.
Use the advertised `projects` field for the issue-search tool; Quack also
normalizes `projectKeys` to `projects` when Codescan advertises that schema.
The value passed to `--arguments` must be JSON: object keys and string values
need double quotes. For example, `{projectKeys:["..."]}` is not valid JSON;
use `{"projectKeys":["..."]}` as shown above. PowerShell's outer single quotes
only protect the JSON from the shell and are not part of the JSON payload.
PowerShell normally removes the single quotes around the JSON, but Quack
accepts the same payload if a launcher retains those outer quote characters or
escapes the inner quotes. If the shell still rewrites inline arguments, let
PowerShell generate the JSON as one native argument:

```powershell
$sonarArguments = [ordered]@{
    projects = @("Operations.HMI.App.Alarms")
    branch = "quack-sonar-violation"
    issueStatuses = @("OPEN")
} | ConvertTo-Json -Compress

quack sonar-mcp `
  --project-path $violations `
  --project-key "Operations.HMI.App.Alarms" `
  --tool search_sonar_issues_in_projects `
  --arguments $sonarArguments `
  --json
```

Windows PowerShell 5.1 can strip the inner JSON quotes before a native
launcher reaches Python. Quack accepts the resulting simple, quote-stripped
object only after safely reconstructing and validating it as JSON. Add
`--debug` to print the exact received argument, normalized request, and
redacted MCP response; diagnostics go to stderr so `--json` stdout remains
machine-readable.

Write-capable tools are rejected and are not exposed to the generated skill or
agent.

With `--json`, Quack prints the complete MCP response even when the tool
returns `isError: true`; the process exits `1` for that tool error. A
successful request exits `0` whether the returned issue list is empty or
contains findings, so scripts must inspect `isError`, `structuredContent`, and
`paging.total` rather than using the exit code as the finding count.

## 6. Use the Sonar fixes skill to remediate issues

After the check identifies a confirmed issue, ask Copilot to use the generated
`sonar-fixes` skill:

```text
Use the repository-scoped sonar-fixes skill for confirmed issue <ISSUE_KEY>.
Read the affected source and nearest relevant test, explain the Sonar rule,
and propose the smallest behavior-preserving fix. Do not use a blanket
suppression. Wait for approval before editing, then run the smallest relevant
test and rerun the read-only Sonar check.
```

The safe fix loop is:

1. Review the finding and affected source.
2. Accept or reject the proposed minimal change.
3. Run the closest unit test, lint, or build.
4. Run `quack watch --once` again.
5. Stage the fix and run the pre-commit gate.

The generated `sonar-code-review` agent can combine the staged or working
diff with these skills. In Copilot, use `/agent` and select
`sonar-code-review`, then request a review of the current worktree. Sonar
findings are evidence; model-generated risk labels remain advisory and cannot
block a commit by themselves.

## 7. Verify the pre-commit Sonar gate

Pre-commit analyzes only the exact Git index. Unstaged source and unstaged
Sonar configuration cannot contaminate the staged scanner snapshot. A
matching staged scanner result is reused locally, but direct Sonar API findings
are re-queried and must correlate to the recorded analysis before they can block.

Verify the clean worktree:

```powershell
Set-Location $clean
git add FrontEnd/packages/alarms/src/quack-sonar-clean.ts
pre-commit run quack --hook-stage pre-commit -v
```

Expected result:

```text
quack.................................................................Passed
advisory: commit allowed
```

Verify the violation worktree:

```powershell
Set-Location $violations
git add FrontEnd/packages/alarms/src/quack-sonar-violation.ts
pre-commit run quack --hook-stage pre-commit -v
```

Expected result for a current correlated Sonar finding:

```text
quack.................................................................Failed
SonarQube CLI: BLOCKED - <n> violation(s) detected
BLOCKED - fix and re-stage
```

VS Code and Visual Studio do not inherit environment variables added in a
PowerShell window after the IDE was started. Run `quack init --local` once
from a terminal where the Sonar project, branch, and token are configured.
Quack saves the non-secret settings in worktree-local Git config and stores
the token through Git Credential Manager. The hook can then retrieve the same
configuration non-interactively from terminal, VS Code, and Visual Studio
without committing or printing the token. Each validation worktree keeps its
own branch setting.

The command exits with code `1`. A normal `git commit` invokes the same hook
and is stopped before the commit is created:

```powershell
git commit -m "Validate Sonar pre-commit gate"
```

Do not use `git commit --no-verify` when validating the gate. After fixing the
issue, stage the fix and rerun the hook:

```powershell
git add <fixed-file>
pre-commit run quack --hook-stage pre-commit -v
```

Quack blocks the commit that would reach a pull request; it is not a
server-side pull-request policy by itself. Use a CI Sonar quality gate as the
authoritative server-side PR rule if the repository requires enforcement
after a push.

## 8. Understand fail-open and backend behavior

Quack blocks only a confirmed current Sonar issue or security hotspot in the
exact staged snapshot. It fails open when credentials, the scanner, the
server/API, task completion, or analysis correlation are unavailable. An
unavailable result is not treated as a clean result.

The generic `sonar-scanner` path is valid for the Alarms FrontEnd. BackEnd-only
and mixed FrontEnd/BackEnd staged changes are explicitly unverified because
generic `sonar-scanner` does not perform the required C# MSBuild analysis.
Use the Alarms CI `dotnet-sonarscanner` begin/build/end flow for BackEnd
coverage; do not interpret a Quack fail-open result as proof that BackEnd is
clean.

Common diagnostics:

| Symptom | Action |
|---|---|
| `Not authorized` | Create a valid SonarQube user token and set `SQ_TOKEN` or `SONARQUBE_TOKEN` in the current process. |
| `SonarQube answered with Error 404` or `snapshot incomplete` for hotspots, measures, or duplications | Codescan cannot resolve the configured project/branch/component, the token cannot see the project, or that endpoint is unavailable for the server edition. Set `SONARQUBE_PROJECT_KEY` to the exact Codescan key, unset `SONARQUBE_BRANCH` unless that branch already exists, and verify the same project with `quack sonar-mcp --project-key ...`. Quack treats missing optional duplication/measure data as a diagnostic gap and does not call it a clean result; missing issue/hotspot data remains fail-open. |
| `SonarQube MCP response not received` | Run the exact read-only call with `quack sonar-mcp --debug --json` and inspect the returned JSON. Quack invokes the fixed `podman run ... mcp/sonarqube` command and never creates a Sonar response. Large valid responses are preserved within a bounded 256 KB budget; a larger response is reported explicitly as `response exceeded the bounded output limit` rather than being truncated or treated as clean. |
| `scanner not found` | Install `sonar-scanner` or set `QUACK_SONAR_SCANNER` to its executable. |
| `Podman could not start` | Start the Podman machine and verify `podman image exists mcp/sonarqube`. |
| `analysis unverified` | Check project/branch settings and confirm the MCP response includes the same analysis identifier as the scanner task. |
| `review unavailable (no model configured)` | This is expected for the deterministic Sonar-only path; Sonar still runs. |

Finally, verify that the original dirty checkout was not modified:

```powershell
git -C "C:\Dev\Workspace\alarms\main\prestine\Operations.HMI.App.Alarms" status --short
```

