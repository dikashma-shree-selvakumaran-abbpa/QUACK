# QUACK

AI-assisted code review inside VS Code, backed by the quack engine.

## Requirements

QUACK talks to a running quack server. Start one first:

    quack serve

The server listens on `http://127.0.0.1:8787` by default. Point the extension
elsewhere with the `quack.serverUrl` setting.

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