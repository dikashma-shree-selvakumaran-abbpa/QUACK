# QUACK

AI-assisted code review inside VS Code, backed by the quack engine.

## Requirements

When VS Code opens, the extension starts `quack serve` automatically. No
terminal is required.

The server listens on `http://127.0.0.1:8787` by default. Point the extension
elsewhere with the `quack.serverUrl` setting.

## Prerequisites

The extension requires `quack.exe` to be installed. If you have the quack CLI,
run `quack install` in any repo and the binary will be placed automatically.
If you are starting from the VS Code extension only, download `quack.exe` from
the GitHub releases page and place it at:
`%LOCALAPPDATA%\quack\quack.exe`

## First-time setup

When you open a repo without quack hooks installed, a notification offers to
set them up. Click **Set up** and the extension installs the pre-commit and
pre-push hooks automatically.

## The sidebar

The QUACK panel in the Activity Bar shows server status, the selected model,
actions to check staged changes, run the agent, or select a model, findings from
the last check, and a warning when hooks are missing.

## The model picker

**QUACK: Select model** opens a list of reachable models. Selecting one
persists it per workspace. Larger models cost more per run.

## Commands

- **QUACK: Check staged changes** - runs local checks on the staged diff and
  reports findings in the Problems panel. Secrets and merge markers are errors;
  everything else is advisory.
- **QUACK: Run AI review and investigation** - runs the full pre-push analysis:
  local checks, an AI review, then an agent that reads the repo and runs tests.
  Results stream into the QUACK output channel. Takes 60-90 seconds.

The status bar shows the connected server's version, or `quack offline` when no
server is reachable.

## What does not leave your machine

Local checks are entirely offline. If a staged secret is detected, the analysis
stops before any model is contacted and the diff is never transmitted.