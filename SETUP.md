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

- `git commit` runs local checks automatically. It also runs the optional local
  SonarQube scan and SonarQube MCP snapshot when their credentials and
  runtimes are available. Both are advisory and fail-open.
- `quack watch` is a foreground process you keep running while you work. It
  refreshes the SonarQube MCP report and reviews after 30 seconds without file
  changes by default, then caches the result for commit time. Use
  `quack watch --once` to review immediately.
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
`tools\sonar-scanner-*\bin` or a scanner on `PATH`. It scans a temporary export
of the staged index, so unstaged source changes are excluded. SonarQube
analysis is advisory and never changes Quack's blocking exit code. Use
`QUACK_SONAR=off` to disable it or `QUACK_SONAR_TIMEOUT_S` to set a bounded
timeout (maximum 120 seconds).

## SonarQube MCP server

For the optional SonarQube MCP snapshot and pre-push investigation tools,
Podman and a SonarQube token are required. The repository includes the client
configuration in `.vscode\mcp.json`; set the token in the shell rather than
editing that file:

```powershell
$env:SQ_TOKEN = "<TOKEN>"
$env:QUACK_PROVIDER = "github_models"
quack agent
```

The adapter in `src\quack\mcp\sonarqube.py` starts
`mcp/sonarqube` over stdio, filters tools explicitly marked as write-capable,
and bounds each request. It defaults to `https://codescan.abb.com` with IDE
port `64120`. `quack watch` and the staged `quack check` hook use this adapter
to collect a bounded snapshot and refresh
`docs\SONARQUBE_REPORT.md`.
`SONARQUBE_TOKEN` can be used instead of `SQ_TOKEN`. Use a SonarQube user
token. If the project workspace is outside the repository being reviewed,
configure it explicitly so Podman can mount it read-only:

```powershell
$env:QUACK_SONAR_MCP_PROJECT_PATH = "C:\Dev\Workspace\alarms\main\prestine\Operations.HMI.App.Alarms"
$env:SONARQUBE_PROJECT_KEY = "Operations.HMI.App.Alarms"
quack sonar-mcp
quack watch --once
git add .
git commit
```

Equivalent one-shot options are
`quack sonar-mcp --project-path <folder> --project-key <key>`. The adapter
mounts the selected folder at `/app/mcp-workspace`. For corporate TLS
inspection, place the public `.crt` or `.pem` CA certificate under
`%LOCALAPPDATA%\quack\sonarqube-mcp\certs`; Quack auto-mounts that directory at
`/usr/local/share/ca-certificates`. Use `--ca-dir <folder>` or
`QUACK_SONAR_MCP_CA_DIR` to select another directory. Set
`QUACK_SONAR_MCP=off` to disable the optional MCP tools. This MCP path is
independent of the local scanner used by `quack check`; the staged local
scanner remains an additional pre-commit check. The report is updated only
after a connected snapshot, and its generated file is ignored by
`quack watch` change detection so it cannot trigger an endless review loop.
The optional `quack agent` investigation can use the same read-only MCP
capability during pre-push analysis.

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

## What quack does NOT need

For default advisory reviews, quack does not need a GitHub Models PAT, API key,
or config file: the Copilot CLI login is sufficient. The separate, optional
tool-calling agent loop requires `QUACK_PROVIDER=github_models` and a
`GITHUB_TOKEN` with access to GitHub Models.

For a complete live walkthrough, see [DEMO.md](DEMO.md). For the full v0.3.0
implementation snapshot, see [CURRENT_STATE.md](CURRENT_STATE.md).
