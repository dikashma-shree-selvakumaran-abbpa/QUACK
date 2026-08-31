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
| `quack check` | Pre-commit | Checks secrets, merge markers, debug code, test guidance, and optional local SonarQube analysis. | SonarQube is local and advisory; Tier 1 remains offline. |
| `quack watch` | Alongside development | Reviews changes while you work and caches a verdict for commit time. | Yes, when a provider is available. |
| `quack agent` | Pre-push | Reviews unpushed commits. Its optional investigative loop can run tests and propose fixes with `QUACK_PROVIDER=github_models`. | Yes, when a provider is available. |

Only secrets and merge markers block. AI and SonarQube are advisory and fail
open: no token, offline operation, a slow provider, or rate limiting never
prevents a commit.

## SonarQube integration

When the local SonarQube server and scanner are available, `quack check` exports
the staged Git index to a temporary directory and submits a Python analysis to
SonarQube. Unstaged edits are not scanned, scanner output is not printed, and
SonarQube failures never block a commit.

The default server is `http://127.0.0.1:9002`. Set a local analysis token before
using the integration:

```powershell
$env:SONAR_TOKEN = "<TOKEN>"
```

Quack discovers `sonar-scanner` on `PATH` or under `tools\sonar-scanner-*\bin`.
Use `QUACK_SONAR_HOST_URL`, `QUACK_SONAR_PROJECT_KEY`, and
`QUACK_SONAR_SCANNER` to override the defaults. Set `QUACK_SONAR=off` (or
`QUACK_DISABLE_SONAR=1`) to disable the advisory scan; the timeout is bounded
by `QUACK_SONAR_TIMEOUT_S` and capped at 120 seconds.

### SonarQube MCP server

The repository also includes the generated MCP client configuration at
`.vscode\mcp.json` and the Python adapter at
`src\quack\mcp\sonarqube.py`. This is separate from the local pre-commit
scanner: when the `github_models` provider, Podman, and a SonarQube token are
available, `quack agent` can use the server's read-only tools during its
pre-push investigation. Tools explicitly marked as write-capable by the
server are not exposed to the agent.

```powershell
$env:SQ_TOKEN = "<TOKEN>"
$env:QUACK_PROVIDER = "github_models"
quack agent
```

The adapter defaults to `https://codescan.abb.com` and port `64120`, accepts
`SONARQUBE_TOKEN` or `SQ_TOKEN`, and never stores the token in source control.
Use a SonarQube user token (not a project key or global token). Set
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
Quack. The MCP path is an optional pre-push agent capability; the staged local
scanner remains the pre-commit check.

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
  sonar-mcp Connect to the SonarQube MCP server through Podman.
  watch    Review changes in the background and cache the result for...
```

## Install

1. `pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK`
2. Run `copilot`, enter `/login`, complete the browser flow, then exit.
3. In your repository, run `quack install`.

See [SETUP.md](SETUP.md) for details and troubleshooting and [CURRENT_STATE.md](CURRENT_STATE.md) for the
code-grounded v0.3.0 architecture snapshot.

## Status

The deterministic commit checks, background review cache, pre-push Tier 2 review,
metrics, and hook installation are covered by the current test suite. The
default `copilot_sdk` provider uses the Copilot CLI's stored login for advisory
reviews. The agent's tool-calling loop currently requires
`QUACK_PROVIDER=github_models` and `GITHUB_TOKEN`; SDK-native tools are scoped
but not built. AI availability and model responses are intentionally advisory.
