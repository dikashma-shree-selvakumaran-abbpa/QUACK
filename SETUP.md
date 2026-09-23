# Set up quack

## Prerequisites

- Python 3.11+
- git
- GitHub Copilot CLI, signed in. This uses an OAuth login, not a personal access token.

## Install

1. Install quack:

   ```shell
   pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK
   ```

2. Sign in to Copilot. Run `copilot`, enter `/login`, complete the browser flow, then exit the CLI.

3. In the repository where you want quack enabled, run:

   ```shell
   quack install
   ```

   For a local checkout, unpublished fork, or a one-off demo repository, use
   `quack install --local`. It writes hooks that invoke the installed `quack`
   command directly.

## Verify it works

Run:

```shell
quack model
```

A healthy result includes:

- `Provider: copilot_sdk` — quack selected the Copilot CLI provider.
- `Auth status: available` — the Copilot SDK runtime is available. A reachable
  model list confirms that the current Copilot login can be used.
- `Completion model:` and `Agent model:` — resolved model names are shown.

## Daily use

- `git commit` runs deterministic checks automatically. When configured, it
  scans the exact staged snapshot and queries SonarQube's Web API directly;
  a confirmed current issue or hotspot on a changed component blocks. Missing
  or stale Sonar infrastructure fails open.
- `quack watch` is a foreground process you keep running while you work. It
  scans the combined staged, unstaged, and non-ignored new-file delta with
  SonarQube before optionally reviewing it with AI, refreshes the report after
  30 seconds without file changes by default, and caches the AI result for
  commit time.
  Use `quack watch --once` to review immediately; the Sonar check itself does
  not require an LLM. The generated Sonar report is excluded from the working
  snapshot so it cannot retrigger watch.
- `git push` runs an advisory AI review on unpushed commits. The default
  `copilot_sdk` provider uses the Copilot CLI's stored OAuth login.
- The optional tool-calling investigation loop requires
  `QUACK_PROVIDER=github_models` and `GITHUB_TOKEN`; it is advisory as well.
- `quack metrics` shows a local summary of what quack has caught.

## Local SonarQube

With SonarQube listening on the default local port, create an analysis token in
the SonarQube account UI and set it for the shell that runs Git hooks:

```powershell
$env:SONAR_TOKEN = "<TOKEN>"
$env:QUACK_SONAR_HOST_URL = "http://127.0.0.1:9002"
```

Quack automatically finds a repository-local scanner under
the target repository's or Quack's `tools\sonar-scanner-*\bin`, or a scanner on
`PATH`. It scans a temporary export
of the exact staged index, so unstaged source and Sonar configuration changes
are excluded. `sonar-project.properties` is read from the index; `.vscode`
settings and other working-tree discovery cannot change `quack check`. Use
validated explicit environment overrides when necessary. The scanner task must
produce completion metadata before Quack records a fresh staged result. The
staged identity includes the scanner fingerprint and effective source/test
roots, exclusions, project, host, branch, and related settings. Use
`QUACK_SONAR_HOST_URL`, `QUACK_SONAR_PROJECT_KEY`, `QUACK_SONAR_BRANCH`,
`QUACK_SONAR_SOURCES`, `QUACK_SONAR_TESTS`, `QUACK_SONAR_EXCLUSIONS`, and
`QUACK_SONAR_SCANNER` for project-specific settings. Tokens may be supplied as
`SONAR_TOKEN`, `SQ_TOKEN`, or `SONARQUBE_TOKEN`.

The generic `sonar-scanner` path is suitable for the Alarms FrontEnd analysis.
BackEnd-only and mixed FrontEnd/BackEnd changes are explicitly unverified;
Quack fails open rather than falsely claiming that a generic scan analyzed C#.
The build-integrated `dotnet-sonarscanner` begin/build/end flow is outside this
staged snapshot path.
SonarQube failures and unavailable infrastructure are fail-open. Use
`QUACK_SONAR=off` to disable it or `QUACK_SONAR_TIMEOUT_S` to set a bounded
timeout (maximum 300 seconds; the default is 180 seconds).

## Deterministic Sonar CLI integration

`quack watch` and `quack check` run `sonar-scanner`, wait for the exact task,
and query SonarQube's Web API directly. They do not use MCP or Podman. The
latest API analysis must match the scanner analysis before changed-file issues
or review-required hotspots can block. Watch prints every returned violation
and writes `docs\SONARQUBE_REPORT.md`; pre-commit keeps its result in memory.
Use `quack watch --once --debug` to print redacted scanner CLI inputs/outputs
and complete bounded direct API request/response payloads.

An unchanged staged index reuses the local scanner result, but `quack check`
still re-queries the direct API. A cached finding count is never trusted by
itself; a mismatched analysis remains unverified and fail-open.

## Standalone SonarQube MCP command

`quack sonar-mcp` and optional agent/skill workflows still use the read-only
Podman MCP adapter. This explicit command is independent of automatic Watch
and pre-commit Sonar checks.

```powershell
$env:QUACK_SONAR_MCP_PROJECT_PATH = "C:\Dev\Workspace\alarms\main\prestine\Operations.HMI.App.Alarms"
$env:SONARQUBE_PROJECT_KEY = "Operations.HMI.App.Alarms"
quack sonar-mcp
```

Equivalent one-shot options are
`quack sonar-mcp --project-path <folder> --project-key <key>`. The adapter
mounts the selected folder at `/app/mcp-workspace`. For corporate TLS
inspection, place the public `.crt` or `.pem` CA certificate under
`%LOCALAPPDATA%\quack\sonarqube-mcp\certs`; Quack auto-mounts that directory at
`/usr/local/share/ca-certificates`. Use `--ca-dir <folder>` or
`QUACK_SONAR_MCP_CA_DIR` to select another directory. Set
`QUACK_SONAR_MCP=off` to disable the optional MCP tools. This MCP path is
independent of the scanner/API integration used by `quack watch` and
`quack check`. The optional `quack agent` investigation can use the same
read-only MCP capability during pre-push analysis.

To enable any SonarQube MCP toolsets, repeat `--toolset` or provide a
comma-separated value, or set the native `SONARQUBE_TOOLSETS` environment
variable. Invoke any advertised read-only tool with `--tool` and a JSON object
in `--arguments`; use the displayed `sonarqube_*` name or the native MCP name:

```powershell
quack sonar-mcp --toolset coverage --tool search_files_by_coverage `
  --arguments '{"projectKey":"Operations.HMI.App.Alarms","branch":"main"}'
quack sonar-mcp --toolset sources,measures --tool get_component_measures `
  --arguments '{"component":"Operations.HMI.App.Alarms","metricKeys":["ncloc"]}' --json
```

`--json` emits the complete successful MCP response for scripting. Quack
always starts the container with `SONARQUBE_READ_ONLY=true`, so write-capable
tools remain unavailable.

## Repository initialization

Run:

```powershell
quack init
```

This installs the existing Quack hooks and creates:

- `.github\skills\sonar-check\SKILL.md`
- `.github\skills\sonar-fixes\SKILL.md`
- `.github\agents\sonar-code-review.agent.md`

The command is idempotent and preserves an existing file instead of
overwriting it. Use `quack install` when hook installation is needed without
the repo-scoped Sonar artifacts.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `Authorization error, you may need to run /login` | The Copilot session expired, or an ambient `GITHUB_TOKEN` shadows the Copilot login. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot`, enter `/login`, and complete sign-in. |
| `copilot` exits immediately without opening | An ambient token is present. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot` and enter `/login`. |
| `UnicodeEncodeError` or `charmap` failure in a Windows hook | An older published hook writes Rich output through the Windows `cp1252` stream. | Use a hook revision containing the UTF-8-safe renderer, or test the current checkout with `pre-commit try-repo <path-to-quack> quack --all-files`. |
| `where.exe quack` shows two paths | A stale duplicate installation exists. | Delete the `.local\bin` copy. |
| The first AI call is slow (about 25 seconds) | Copilot is extracting its runtime once. | Wait for the first call to finish; later calls use the extracted runtime. |
| `AI review: not reviewed yet` | Watch mode has not reviewed the current diff. | Run `quack watch`. |
| `AI review unavailable` | The provider could not authenticate or complete the advisory request. | The commit/push still proceeds. Run `quack model`, then sign in with `copilot` and `/login` if needed. |
| `SSLHandshakeException` or `certificate_unknown` | The corporate proxy/CA is trusted by Windows but not by Java inside the container. | Copy the public CA as `.crt`/`.pem` under `%LOCALAPPDATA%\quack\sonarqube-mcp\certs` or pass `--ca-dir <folder>`, then retry. |
| `quack sonar-mcp: SonarQube MCP server timed out` | Podman was waiting for a long-lived MCP process to exit, or the server cannot reach SonarQube. | If the message says `Not authorized`, create a new SonarQube user token and set `SQ_TOKEN` or `SONARQUBE_TOKEN`. Otherwise update Quack with `python -m pip install --editable . --upgrade`, verify `podman image exists mcp/sonarqube`, and retry `quack sonar-mcp --project-path <folder>`. Increase `QUACK_SONAR_MCP_TIMEOUT_S` only up to 180 seconds. |
| `SonarQube answered with Error 404` for hotspots, measures, or duplications | The project key, branch, or changed component is not visible on Codescan, or the endpoint is unavailable for that server edition. | Set `SONARQUBE_PROJECT_KEY` exactly, unset `SONARQUBE_BRANCH` unless the branch already exists, and run `quack sonar-mcp --project-key <key>` to validate the same scope. Quack reports optional duplication/measure 404s as diagnostic gaps and keeps missing issue/hotspot evidence fail-open. |

## What quack does NOT need

For default advisory reviews, quack does not need a GitHub Models PAT, API key,
or config file: the Copilot CLI login is sufficient. The separate, optional
tool-calling agent loop requires `QUACK_PROVIDER=github_models` and a
`GITHUB_TOKEN` with access to GitHub Models.

For a complete live walkthrough, see [DEMO.md](DEMO.md). For the Alarms Sonar
workflow, see [HOW-TO-USE-SONAR.md](HOW-TO-USE-SONAR.md). For the full v0.3.0
implementation snapshot, see [CURRENT_STATE.md](CURRENT_STATE.md).
