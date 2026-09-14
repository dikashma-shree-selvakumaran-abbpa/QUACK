# QUACK 

Catch problems before they reach CI : a pre-commit quality hook with AI review at pre-push.

## The problem

For most teams, the first real quality gate is CI. You push, wait 15ΓÇô20 minutes, and learn that a secret was committed, debug code was left in, or a test was broken , after you have context-switched and after teammates are blocked. The cost of a defect grows with the distance from the keystroke that made it. quack moves the first check back to the commit and push that introduced the change.

## What it looks like

A staged Azure DevOps PAT blocks the commit; quack exits 1 and Git does not commit it.

```text
Γò¡ΓöÇ quack - 2 file(s) - +2/-1 - 0.8s ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓò«
Γöé Γ£ù  secrets  demo_secret.cs:1  Azure DevOps PAT                                                                                       Γöé
Γöé ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ Γöé
Γöé ≡ƒÉñ QUACK!!!! check line #1                                                                                                           Γöé
Γò░ΓöÇ ≡ƒÉñ BLOCKED - fix and re-stage ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓò»
exit code: 1
```

A normal commit gives an exact test command and shows the cached review from `quack watch`.

```text
Γò¡ΓöÇ quack - 1 file(s) - +1/-1 - 1.9s ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓò«
Γöé Test guidance                                                                   Γöé
Γöé dotnet test packages/GraphicsModelEditor/GfxKernel.Tests/GfxKernel.Tests.csproj Γöé
Γöé (first run: build once with dotnet build)                                       Γöé
Γöé ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ Γöé
Γöé AI - claude-haiku-4.5 - risk: MEDIUM                                            Γöé
Γöé Validation relaxed from exact to minimum length; confirm downstream array index Γöé
Γöé Validation boundary changed: from exact length equality (!=) to minimum length  Γöé
Γöé Relaxed constraint permits cases previously rejected; downstream BeginMove logi Γöé
Γöé boundary/index/limit logic touched                                              Γöé
Γöé (reviewed 2 min ago by quack watch)                                             Γöé
Γò░ΓöÇ advisory: commit allowed ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓò»
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
| `quack check` | Pre-commit | Checks secrets, merge markers, debug code, test guidance, and an optional local SonarQube analysis. It never calls AI. | No; no token required. SonarQube is local and advisory. |
| `quack watch` | Alongside development | Reviews changes while you work and caches a verdict for commit time. | Yes, when a provider is available. |
| `quack agent` | Pre-push | Reviews unpushed commits, then runs the provider's read-only investigation tools and can propose fixes. | Yes, when a provider is available. |
| `quack serve` | On demand | Serves the same checks over local HTTP so editors can drive them. | Only when the endpoint it is asked for uses AI. |
| VS Code extension | Alongside development | Runs check and the agent against a running `quack serve` from inside the editor. | Through the server. |

Only secrets and merge markers block. AI is advisory and fails open: no token, offline operation, a slow provider, or rate limiting never prevents a commit. A pre-push range review requires an upstream branch; without one, `quack agent` can still analyze staged changes but has no unpushed range to inspect.


## SonarQube integration

When the local SonarQube server and scanner are available, `quack check` exports
the staged Git index to a temporary directory and submits a scan to
SonarQube. Unstaged edits are not scanned, scanner output is not printed, and
SonarQube failures never block a commit.

Set a local analysis token before using the integration:

```powershell
$env:SONAR_TOKEN = "<TOKEN>"
```

Quack discovers `sonar-scanner` on `PATH` or under `tools\sonar-scanner-*\bin`.
Set `QUACK_DISABLE_SONAR=1` to disable the advisory scan.

### SonarQube MCP server

The repository also includes the generated MCP client configuration at
`.vscode\mcp.json` and the Python adapter at `src\quack\mcp\sonarqube.py`.
This is separate from the local pre-commit scanner: when Podman and a
SonarQube token are available, `quack agent` uses the server's read-only
tools natively through the Copilot SDK's tool-calling session during its
pre-push investigation. Tools explicitly marked as write-capable by the
server are not exposed to the agent.

```powershell
$env:SQ_TOKEN = "<TOKEN>"
quack agent
```

The adapter connects to `SONARQUBE_URL` (e.g. `https://codescan.abb.com`),
accepts `SONARQUBE_TOKEN` or `SQ_TOKEN`, and never stores the token in
source control. Use a SonarQube user token (not a project key or global
token). If the SonarQube project is outside the repository being reviewed,
provide its workspace and project key explicitly with
`quack sonar-mcp --project-path <folder>` or `--project-key <key>`.

For corporate TLS inspection, the SonarQube MCP container image needs your
organization's CA certificate imported into its Java trust store (the
container's own OS trust store is not sufficient for the JVM). See
`Dockerfile.sonarqube-abb` for a reference build that layers ABB's root and
intermediate CA certificates into the image.

Run `quack sonar-mcp` to start a Podman MCP session and list the available
SonarQube tools directly from Quack. The MCP path is an optional pre-push
agent capability; the staged local scanner remains the pre-commit check.
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
  serve    Start the quack FastAPI server.
  watch    Review changes in the background and cache the result for...
```

Important options:

| Option | Commands | Purpose |
|---|---|---|
| `--once` | `watch` | Run one review immediately and exit. |
| `--quiet-period SECONDS` | `watch` | Set the quiet period before a background review; default is 30 seconds. |
| `--model MODEL` | `model`, `agent` | Override the configured model for diagnostics or agent review. |
| `--list` | `model` | Print the reachable model ids, with the current completion and agent defaults marked. |
| `--fly` | `agent` | Reveal an unapplied proposed patch instead of showing coaching-only output. |
| `--local` | `install` | Install hooks using the installed `quack` command without requiring a published repository revision. |
| `--port` / `--host` | `serve` | Bind address for the local server; defaults to `127.0.0.1:8787`. |

`quack check` is intentionally fully local. It runs deterministic checks,
builds test guidance, and reads a matching cached AI review when one exists.
It never calls an AI provider and never falls back to a network request on a
cache miss. AI review is performed by `quack watch` or `quack agent`.

## In the editor

`quack serve` starts a local HTTP server (`127.0.0.1:8787` by default) that
exposes the same engine to editors:

| Endpoint | Purpose |
|---|---|
| `POST /check` | Deterministic checks, test guidance, and any cached review for a repository. |
| `POST /review` | Run one Tier 2 review now and cache it. |
| `POST /agent` | Start an agent job and return its id. |
| `GET /agent/{id}` | Poll a job for stage-by-stage results. |
| `DELETE /agent/{id}` | Cancel a running job. |
| `GET /models` | The reachable model catalog and the current default. |
| `GET /metrics` | The same local aggregate summary as `quack metrics`. |
| `GET /install/plan` | What `quack install` would do in a repository. |
| `POST /install` | Perform the install, step by step. |
| `GET /status` | Version, build stamp, cache path, and any provider problem. |

Every repository-scoped endpoint - `/check`, `/review`, `/agent`, and both
install endpoints - requires an explicit `repo_path` and returns 400 without
one: the server never falls back to its own working directory.

The VS Code extension in [clients/vscode](clients/vscode) drives that server and
adds three commands:

- **QUACK: Check staged changes** - findings land in the Problems panel.
- **QUACK: Run AI review and investigation** - streams the review and the
  agent's investigation into the QUACK output channel.
- **QUACK: Select model** - picks from the live catalog and stores the choice in
  workspace settings.

## Install

1. `pipx install git+https://github.com/ABB-AU-PCP/QUACK`
2. Run `copilot`, enter `/login`, complete the browser flow, then exit.
3. In your repository, run `quack install`.

Each tagged release also attaches a frozen `quack.exe` and the matching
`quack-abb-<version>.vsix`; the release build fails if the extension version and
the tag disagree.

See [SETUP.md](SETUP.md) for installation details and troubleshooting, and
[FEATURES.md](FEATURES.md) for a code-grounded description of what each feature
actually does.

## Status

The deterministic commit checks, background review cache, pre-push Tier 2 review,
SDK-native agent tools, metrics, hook installation, the server endpoints, and
fail-open behavior are covered by the current test suite. `copilot_sdk` is the only provider; it uses
the Copilot CLI's stored login for both advisory review and native tool calling.
AI availability and model responses are intentionally advisory.
