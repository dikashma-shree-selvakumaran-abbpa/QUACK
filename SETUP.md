# Set up quack

## Prerequisites

- Python 3.11+
- git
- GitHub Copilot CLI, signed in. This uses an OAuth login, not a personal access token.

## Install

1. Install quack:

   ```shell
   pipx install git+https://github.com/ABB-AU-PCP/QUACK
   ```

2. Sign in to Copilot. Run `copilot`, enter `/login`, complete the browser flow, then exit the CLI.

3. In the repository where you want quack enabled, run:

   ```shell
   quack install
   ```

   For a local checkout, unpublished fork, or a one-off demo repository, use
   `quack install --local`. It writes hooks that invoke the installed `quack`
   command directly.

4. Optional, for VS Code: download `quack-abb-<version>.vsix` from the GitHub
   release matching your quack version and install it with
   `code --install-extension quack-abb-<version>.vsix`. The extension needs a
   running `quack serve`; see "Using quack from VS Code".

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
  The `copilot_sdk` defaults are `claude-haiku-4.5` for review and
  `claude-sonnet-5` for the agent.

Use `quack model --list` to print just the reachable model ids, with the current
completion and agent defaults marked.

## Using quack from VS Code

1. Start the server for the repository you are working on:

   ```shell
   quack serve
   ```

   It listens on `http://127.0.0.1:8787` by default. `--host` and `--port`
   change that; the extension's `quack.serverUrl` setting must match.

2. **QUACK: Check staged changes** runs the local checks on the staged diff and
   reports findings in the Problems panel.

3. **QUACK: Run AI review and investigation** runs the full pre-push analysis.
   Results stream into the QUACK output channel, and the run can be cancelled
   from the progress notification.

4. **QUACK: Select model** picks from the reachable catalog. The choice is saved
   as `quack.model` in workspace settings; leaving it empty uses the server's
   default.

The status bar shows the connected server's version and build stamp, or
`quack offline` when no server is reachable.

## Daily use

- `git commit` runs local checks automatically. It does not use the network or a token.
- `quack watch` is a foreground process you keep running while you work. It
  reviews after 30 seconds without file changes by default, then caches the
  result for commit time. Use `quack watch --once` to review immediately.
- `git push` runs an advisory AI review on unpushed commits. The `copilot_sdk`
  provider uses the Copilot CLI's stored OAuth login.
- The Copilot SDK runs the advisory SDK-native investigation loop.
- `quack serve` is only needed while you drive quack from VS Code.
- `quack metrics` shows a local summary of what quack has caught.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `Authorization error, you may need to run /login` | The Copilot session expired, or an ambient `GITHUB_TOKEN` shadows the Copilot login. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot`, enter `/login`, and complete sign-in. |
| `copilot` exits immediately without opening | An ambient token is present. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot` and enter `/login`. |
| Unicode crash in a Windows terminal | The terminal is not using UTF-8 output. | Set `PYTHONIOENCODING=utf-8`. In PowerShell: `$env:PYTHONIOENCODING = "utf-8"`. |
| `where.exe quack` shows two paths | A stale duplicate installation exists. | Delete the `.local\bin` copy. |
| The first AI call is slow | The Copilot runtime may be initializing for the first request. | Wait for the first call to finish; later calls may be faster after runtime initialization. |
| `AI review: not reviewed yet` | Watch mode has not reviewed the current diff. | Run `quack watch`. |
| `AI review unavailable` | The provider could not authenticate or complete the advisory request. | The commit/push still proceeds. Run `quack model`, then sign in with `copilot` and `/login` if needed. |
| `gitleaks not installed` at `quack install` | Machine policy blocks `winget` (or no supported package manager was found). | Optional: gitleaks only adds breadth on top of quack's built-in secret patterns, which still block on their own. Set `QUACK_DISABLE_GITLEAKS=1` to silence the check, or install the `gitleaks` binary manually and re-run `quack install`. |
| `quack offline` in the VS Code status bar | No server is listening at `quack.serverUrl`. | Run `quack serve`, or correct `quack.serverUrl` to match the host and port you started it on. |
| A server call returns `400 repo_path is required` | The request carried no repository path. | The server never falls back to its own working directory. Open a folder in VS Code, or send an explicit `repo_path`. |

## What quack does NOT need

For advisory reviews and the SDK-native agent, quack does not need a
GitHub Models PAT, API key, or config file: the Copilot CLI login is sufficient.
The agent runs on `copilot_sdk` through the Copilot SDK's own session API,
authenticated by the Copilot CLI's stored OAuth login; no `GITHUB_TOKEN` is used.

For a code-grounded description of what each feature actually does, see
[FEATURES.md](FEATURES.md). Keep live recording scripts with the
demo materials so timing-dependent transcripts are not treated as product
behavior guarantees.
