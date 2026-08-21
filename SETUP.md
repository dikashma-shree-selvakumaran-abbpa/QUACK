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

- `git commit` runs local checks automatically. It does not use the network or a token.
- `quack watch` is a foreground process you keep running while you work. It
  reviews after 30 seconds without file changes by default, then caches the
  result for commit time. Use `quack watch --once` to review immediately.
- `git push` runs an advisory AI review on unpushed commits. The default
  `copilot_sdk` provider uses the Copilot CLI's stored OAuth login.
- The optional tool-calling investigation loop requires
  `QUACK_PROVIDER=github_models` and `GITHUB_TOKEN`; it is advisory as well.
- `quack metrics` shows a local summary of what quack has caught.

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `Authorization error, you may need to run /login` | The Copilot session expired, or an ambient `GITHUB_TOKEN` shadows the Copilot login. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot`, enter `/login`, and complete sign-in. |
| `copilot` exits immediately without opening | An ambient token is present. | Run `quack model` to diagnose. Unset `GITHUB_TOKEN`, `GH_TOKEN`, and `COPILOT_GITHUB_TOKEN`; then run `copilot` and enter `/login`. |
| Unicode crash in a Windows terminal | The terminal is not using UTF-8 output. | Set `PYTHONIOENCODING=utf-8`. In PowerShell: `$env:PYTHONIOENCODING = "utf-8"`. |
| `where.exe quack` shows two paths | A stale duplicate installation exists. | Delete the `.local\bin` copy. |
| The first AI call is slow (about 25 seconds) | Copilot is extracting its runtime once. | Wait for the first call to finish; later calls use the extracted runtime. |
| `AI review: not reviewed yet` | Watch mode has not reviewed the current diff. | Run `quack watch`. |
| `AI review unavailable` | The provider could not authenticate or complete the advisory request. | The commit/push still proceeds. Run `quack model`, then sign in with `copilot` and `/login` if needed. |

## What quack does NOT need

For default advisory reviews, quack does not need a GitHub Models PAT, API key,
or config file: the Copilot CLI login is sufficient. The separate, optional
tool-calling agent loop requires `QUACK_PROVIDER=github_models` and a
`GITHUB_TOKEN` with access to GitHub Models.

For a complete live walkthrough, see [DEMO.md](DEMO.md). For the full v0.3.0
implementation snapshot, see [CURRENT_STATE.md](CURRENT_STATE.md).
