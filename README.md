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
| `quack check` | Pre-commit | Checks secrets, merge markers, debug code, test guidance, and the exact staged SonarQube snapshot. | Tier 1 is offline; a confirmed current SonarQube MCP result can block on changed-file issues or hotspots. Unavailable Sonar infrastructure fails open. |
| `quack watch` | Alongside development | Checks the complete tracked working delta with SonarQube, then optionally reviews it with AI and caches that AI verdict. | SonarQube and AI are used only when configured; the deterministic Sonar check does not require an LLM. |
| `quack agent` | Pre-push | Reviews unpushed commits. Its optional investigative loop can run tests and propose fixes with `QUACK_PROVIDER=github_models`. | Yes, when a provider is available. |

Secrets, merge markers, and a confirmed current staged SonarQube snapshot with
open issues or security hotspots block. AI and the local scanner remain
advisory; an unavailable Sonar connection, stale analysis, offline operation,
a slow provider, or rate limiting never prevents a commit.

## SonarQube integration

When configured, `quack check` exports the exact staged Git index to a temporary
directory and submits the project-specific analysis to SonarQube. Unstaged
source and Sonar configuration are never included: project properties are read
from the index, IDE settings are ignored, and only validated explicit
environment overrides are accepted. The staged identity includes the scanner
path/content fingerprint and the complete effective scanner settings,
including source/test roots, exclusions, project, host, and branch. Any index
or configuration change forces a new local scan.

The default server is `http://127.0.0.1:9002`. Set a local analysis token before
using the integration:

```powershell
$env:SONAR_TOKEN = "<TOKEN>" # SONAR_TOKEN, SQ_TOKEN, or SONARQUBE_TOKEN
```

Quack discovers `sonar-scanner` on `PATH`, under the target repository's
`tools\sonar-scanner-*\bin`, or under Quack's own `tools\sonar-scanner-*\bin`.
An invalid `QUACK_SONAR_SCANNER` override no longer prevents that fallback.
Use `QUACK_SONAR_HOST_URL`, `QUACK_SONAR_PROJECT_KEY`,
`QUACK_SONAR_BRANCH`, `QUACK_SONAR_SOURCES`, `QUACK_SONAR_TESTS`,
`QUACK_SONAR_EXCLUSIONS`, and `QUACK_SONAR_SCANNER` for explicit overrides.
The generic scanner is used for FrontEnd-style sources. BackEnd-only and mixed
FrontEnd/BackEnd staged changes remain unverified because generic
`sonar-scanner` does not analyze C#; they fail open rather than claiming
coverage. A build-integrated `dotnet-sonarscanner` begin/build/end flow is
outside this staged snapshot path.
Set `QUACK_SONAR=off` (or `QUACK_DISABLE_SONAR=1`) to disable the scan; the
timeout is bounded by `QUACK_SONAR_TIMEOUT_S` and capped at 300 seconds. The default is 180 seconds
because the first FrontEnd scan may download language analyzers and runtimes.

### SonarQube MCP server

The repository also includes the generated MCP client configuration at
`.vscode\mcp.json` and the Python adapter at
`src\quack\mcp\sonarqube.py`. When Podman and a SonarQube token are available,
both `quack watch` and the staged `quack check` hook collect a bounded,
read-only snapshot. `quack watch` writes the snapshot to the living report at
`docs\SONARQUBE_REPORT.md`; the pre-commit hook renders its result directly
without modifying the target repository. Watch report updates are atomic and
the generated file is excluded from watch change detection.

Each snapshot calls the requested duplication, security-hotspot, open-issue,
and component-measure operations. Issue and hotspot results are filtered to
the changed Sonar components, and component arguments are sent when the MCP
schema advertises them. Component measures include every metric advertised by
the server when metric discovery is available, plus the required
`cognitive_complexity`, `ncloc`, and `reliability_rating` fields. A matching
local scanner result still re-runs the MCP finding query; its old violation
count is never trusted. If the server does not return a matching analysis
identifier, that cached MCP result is explicitly unverified and fails open.
Only a current correlated snapshot with open issues or security hotspots blocks
`quack check`; missing credentials, Podman, unavailable tools, timeouts, stale
reports, and malformed responses do not block the commit.

```powershell
$env:SQ_TOKEN = "<TOKEN>"
$env:QUACK_PROVIDER = "github_models"
quack agent
```

The adapter defaults to `https://codescan.abb.com`, accepts
`SONARQUBE_TOKEN` or `SQ_TOKEN`, and never stores the token in source control.
It does not use an IDE proxy by default; set `SONARQUBE_IDE_PORT` explicitly
only for an IDE-connected SonarQube deployment. Use a SonarQube user token
(not a project key or global token). Set
`QUACK_SONAR_MCP=off` to disable the optional MCP tools. If the SonarQube
project is outside the repository being reviewed, provide its workspace and
project key explicitly:

```powershell
$env:QUACK_SONAR_MCP_PROJECT_PATH = "C:\Dev\Workspace\alarms\main\prestine\Operations.HMI.App.Alarms"
$env:SONARQUBE_PROJECT_KEY = "Operations.HMI.App.Alarms"
quack sonar-mcp
quack agent
```

The path can also be supplied with `quack sonar-mcp --project-path <folder>`;
the adapter mounts it read-only at `/app/mcp-workspace`. For corporate TLS
inspection, place the public `.crt` or `.pem` CA certificate under
`%LOCALAPPDATA%\quack\sonarqube-mcp\certs`; Quack auto-mounts that directory at
`/usr/local/share/ca-certificates`. Use `--ca-dir <folder>` or
`QUACK_SONAR_MCP_CA_DIR` to select another directory. Run `quack sonar-mcp` to
start a Podman MCP session and list the available SonarQube tools directly from
Quack. Use `--toolset` (repeatable or comma-separated), or set the native
`SONARQUBE_TOOLSETS` environment variable, to enable any SonarQube MCP
toolsets. Use `--tool` with a JSON object to invoke any advertised read-only
tool:

```powershell
quack sonar-mcp --toolset coverage --tool search_files_by_coverage `
  --arguments '{"projectKey":"Operations.HMI.App.Alarms","branch":"main"}'
quack sonar-mcp --toolset sources,measures --tool get_component_measures `
  --arguments '{"component":"Operations.HMI.App.Alarms","metricKeys":["ncloc"]}' --json
```

Tool names may be the displayed `sonarqube_*` name or the native MCP name.
`--json` prints the complete successful MCP response for scripting. Quack
keeps the container in read-only mode, so write-capable tools are not exposed.
The MCP snapshot is used by watch and pre-commit; the staged local scanner
remains an additional pre-commit check. Watch reviews staged plus unstaged
tracked changes, while pre-commit uses only the exact index. The optional
`quack agent`
investigation can use the same read-only MCP capability during pre-push
analysis.

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
  init     Initialize hooks and repo-scoped Sonar Copilot artifacts.
  install  Add the quack stanza to .pre-commit-config.yaml and install...
  metrics  Summarize local metrics without network access.
  model    Report model configuration and connectivity without changing it.
  sonar-mcp Connect to, inspect, and invoke read-only SonarQube MCP tools.
  watch    Review changes in the background and cache the result for...
```

## Install

1. `pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK`
2. Run `copilot`, enter `/login`, complete the browser flow, then exit.
3. In your repository, run `quack init` to install hooks and create the
   repo-scoped Sonar check/fix skills and code-review agent. Existing artifact
   files are preserved; `quack install` remains available when only hooks are
   needed.

See [SETUP.md](SETUP.md) for details and troubleshooting, [HOW-TO-USE-SONAR.md](HOW-TO-USE-SONAR.md)
for the complete Alarms Sonar walkthrough, and [CURRENT_STATE.md](CURRENT_STATE.md) for the
code-grounded v0.3.0 architecture snapshot.

## Status

The deterministic commit checks, background review cache, pre-push Tier 2 review,
metrics, and hook installation are covered by the current test suite. The
default `copilot_sdk` provider uses the Copilot CLI's stored login for advisory
reviews. The agent's tool-calling loop currently requires
`QUACK_PROVIDER=github_models` and `GITHUB_TOKEN`; SDK-native tools are scoped
but not built. AI availability and model responses are intentionally advisory.
