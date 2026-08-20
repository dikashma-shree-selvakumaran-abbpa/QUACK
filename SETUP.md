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

## Verify it works

Run:

```shell
quack model
```

A healthy result includes:

- `Provider: copilot_sdk` — quack selected the Copilot CLI provider.
- `Auth status: available` — your Copilot CLI login is usable.
- `Completion model:` and `Agent model:` — resolved model names are shown.

## Daily use

- `git commit` runs local checks automatically. It does not use the network or a token.
- `quack watch` runs while you work and reviews changes in the background, so the result is ready at commit time.
- `git push` runs the AI review on unpushed commits.
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

## What quack does NOT need

quack does not need a GitHub Models PAT, an API key, or a config file. The Copilot CLI login is the only credential.
