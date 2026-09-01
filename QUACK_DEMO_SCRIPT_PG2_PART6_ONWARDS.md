# Quack PG2 Demo Script — Parts 6 Onward

This script continues from Part 6 using the real PG2 C# repository:

```text
C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
```

The earlier fresh `quack-demo` repository can be used for installation and deterministic secret demonstrations. PG2 is used here for real C# test mapping, AI review, watch mode, and pre-push analysis.

> Recording safety: use only a disposable demo branch. Do not run `git reset --hard` if PG2 contains work that must be preserved.

---

# PG2 preparation

Open a separate PowerShell window and inspect the repository:

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
git status --short
git branch --show-current
```

If the prepared demo branch already exists:

```powershell
git switch quack-demo-do-not-merge
git fetch origin
git reset --hard origin/main
git branch --unset-upstream
quack install --local
```

If the branch does not exist, create a disposable branch from `origin/main` instead:

```powershell
git fetch origin
git switch -C quack-demo-recording origin/main
quack install --local
```

The relevant PG2 files are:

```text
packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
packages/GraphicsModelEditor/GfxKernel.Tests/GfxKernel.Tests.csproj
```

The relevant test class is:

```text
KernelGraphicsAdapterTests
```

---

# Part 6 — Test guidance on real C# code

## Screen

Open:

```text
packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
```

Make or restore the prepared snap-to-grid change, then stage it:

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
```

Run:

```powershell
quack check
```

Expected test guidance should identify the separate test project and a focused test filter similar to:

```text
dotnet test packages/GraphicsModelEditor/GfxKernel.Tests/GfxKernel.Tests.csproj
--no-build --filter "FullyQualifiedName~KernelGraphicsAdapterTests"
```

## Narration

> “This is a real C# change in one package, but the relevant tests live in a different package. Quack searched the repository, found the matching test project and test class, and produced a focused dotnet test command.”

Point out the local sections in the panel:

- Changed file count
- Added and removed lines
- Test project
- Focused test filter
- Any untested source warning
- Advisory footer

If AI has not run yet, the panel should contain a message similar to:

```text
AI review: not reviewed yet - run `quack watch` to review in the background
```

Say:

> “The deterministic check is already complete. The AI review is separate and has not been run yet.”

Do not claim that this command made an AI request. `quack check` is local and only reads an existing review cache.

---

# Part 7 — One-shot AI review and cache

With the PG2 change still staged, run:

```powershell
quack watch --once
```

Expected output shape:

```text
reviewed 1 file(s) - risk: medium|high
```

## Narration

> “Watch mode reviewed the current change and stored the result in Quack’s local review cache.”

Run the local check again:

```powershell
quack check
```

The panel should now show the cached AI result, including some or all of:

- AI model
- Risk level
- One-line summary
- Review reasons
- Suggested tests
- Missing-test information
- Review age
- `advisory: commit allowed`

Say:

> “Commit time is still local. Quack does not call the provider here. It hashes the redacted diff, reads the matching cached result, and displays it if it is fresh.”

## Demonstrate a changed cache key

Make another small change without running watch again:

```powershell
Add-Content packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs `
	"    // intentional cache-miss demonstration"

git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
quack check
```

The check should report that the current change has not been reviewed yet.

Say:

> “Because the staged diff changed, the hash no longer matches the previous review. Quack does not reuse a verdict for a different diff, and it still does not make a network call at commit time.”

Refresh the review:

```powershell
quack watch --once
quack check
```

---

# Part 8 — Background watch mode in PG2

Use two PowerShell windows.

## Right window — watcher

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
quack watch --quiet-period 5
```

The normal quiet period is 30 seconds. Five seconds is used only to make the recording practical.

Say:

> “The watcher polls for file changes and reviews after the working tree has been quiet. I can leave it running while I continue working.”

## Left window — developer work

Open:

```text
packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
```

Make a small real edit and save it without staging immediately.

After the quiet period, the right window should report a result similar to:

```text
reviewed 1 file(s) - risk: medium|high
```

Stage the same change in the left window:

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
quack check
```

## Narration

> “The review happened while I was still working. Now that the same change is staged, the local commit check can display the cached verdict immediately.”

Stop the watcher:

```text
Ctrl+C
```

---

# Part 9 — A meaningful PG2 risk example

For a stronger AI-review frame, use a small behavior-sensitive change in `KernelGraphicsAdapter.cs`, such as the prepared validation-boundary change.

Stage the change:

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
quack watch --once
quack check
```

Pause on the AI panel.

## Narration

> “This is the kind of semantic change that may look small in a diff but changes which inputs are accepted. Quack’s deterministic risk rubric recognizes boundary and index-sensitive logic, while the AI review explains the possible downstream impact.”

If the output differs, do not force a specific risk level. Say:

> “The exact wording and risk level come from the current diff and model response. The important point is that Quack combines deterministic signals with an advisory explanation.”

---

# Part 10 — Pre-push review against staged PG2 changes

First verify the upstream state:

```powershell
git rev-parse --abbrev-ref --symbolic-full-name "@{u}"
```

If the command reports an upstream, continue with the unpushed-commit demonstration in Part 11.

If there is no upstream, demonstrate the agent against staged changes first:

```powershell
quack agent
```

## Narration

> “The pre-push path performs a Tier 2 review first, then starts the investigative agent.”

The agent can use only these tools:

- `read_file`
- `list_dir`
- `run_tests`

Say:

> “The agent cannot execute arbitrary shell commands. File paths must remain inside the repository, and test commands are restricted to approved pytest and dotnet test shapes.”

The investigation is bounded by:

- Eight iterations
- Two test runs
- A 180-second wall-clock limit

Say:

> “These limits are enforced by Quack, not left to the model.”

The command is advisory and should exit successfully even if the AI provider is unavailable.

---

# Part 11 — Pre-push review of unpushed PG2 commits

A first push of a new branch has no previous upstream range to analyze. Establish the upstream on a disposable demo branch first:

```powershell
git push -u origin quack-demo-do-not-merge
```

Then make a second real change in:

```text
packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
```

Stage and commit it:

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
git commit -m "demo: intentional PG2 change"
```

Push:

```powershell
git push
```

The pre-push hook should report a message similar to:

```text
analyzing 1 unpushed commit(s)
```

## Narration

> “Because this branch has an upstream now, Quack can compare the local branch with its upstream and review the unpushed commit range.”

During the push, show:

- The pre-push hook ID `quack-agent`
- The number of unpushed commits
- The Tier 2 risk review
- Investigation progress
- Any tests run
- The final advisory result

Say:

> “The AI result is advisory. A model failure, timeout, unavailable provider, or investigation problem does not block the push.”

If the agent cannot complete its investigation, say:

> “The investigation was unavailable, but the push still completed. That is the fail-open behavior.”

---

# Part 12 — Coaching mode and `--fly`

Run the agent in its default coaching mode:

```powershell
quack agent
```

If the agent returns a proposed patch, the default output should explain the issue without showing or applying the patch.

Say:

> “By default, Quack coaches the developer through the issue. It does not silently apply the proposed patch.”

Now run:

```powershell
quack agent --fly
```

If a proposed patch exists, Quack reveals:

- The issue explanation
- The proposed unified diff
- An apply hint
- A statement that the patch was not applied

Say:

> “Fly mode reveals the proposed patch and an apply hint, but it still does not modify the repository automatically.”

If no patch is proposed, say:

> “This investigation did not produce a proposed patch, so there is nothing to reveal. Fly mode only displays a patch when the agent returns one.”

---

# Part 13 — PG2 diagnostics and failure behavior

Run:

```powershell
quack model
```

Say:

> “This command is read-only. It reports the selected provider, model precedence, timeout, and reachable models without changing configuration.”

Demonstrate an invalid provider:

```powershell
$env:QUACK_PROVIDER = "invalid-provider"
quack model
Remove-Item Env:\QUACK_PROVIDER
```

Say:

> “Provider resolution errors are converted into readable diagnostics. A diagnostic problem does not become a new failure mode.”

Optional token-shadowing demonstration with a fake value:

```powershell
$env:GITHUB_TOKEN = "fake-demo-token"
quack model
Remove-Item Env:\GITHUB_TOKEN
```

Say:

> “The token value is not printed. Quack reports only that an ambient token is set and gives its length, because it can shadow the stored Copilot login.”

Demonstrate AI unavailability without changing PG2 files:

```powershell
$env:QUACK_PROVIDER = "invalid-provider"
quack watch --once
Remove-Item Env:\QUACK_PROVIDER
```

Say:

> “The AI review reports why it was unavailable. The deterministic local checks remain usable because the AI layer is fail-open.”

---

# Part 14 — PG2 metrics

Run:

```powershell
quack metrics
```

Expected fields include:

- Total runs
- Runs by command
- Number of blocks
- Most common findings
- Median duration
- Cache hit rate

Say:

> “Metrics are stored locally as aggregate events. They record counts, durations, command types, risk levels, and cache status—not source code, diff content, repository names, or file paths.”

Do not show personal cache paths or unrelated repository data on screen.

---

# Part 15 — Complete command surface

Run:

```powershell
quack --help
```

Say:

> “The complete command surface is six commands.”

Explain:

- `quack check` — local staged checks
- `quack watch` — background or one-shot cached AI review
- `quack agent` — pre-push review and investigation
- `quack install` — hook installation
- `quack model` — provider and model diagnostics
- `quack metrics` — local aggregate metrics

Also show:

```powershell
quack --version
```

---

# Closing narration

> “This real PG2 workflow shows the separation between deterministic enforcement and advisory intelligence.
>
> At commit time, Quack checks the staged C# change locally and produces focused test guidance. Watch mode can review the change in the background and cache the result. At push time, Quack can review unpushed commits and investigate relevant files and tests.
>
> The AI layer can fail, time out, or be unavailable without preventing the developer from committing or pushing. The local deterministic checks remain the authority for blocking behavior.”

---

# PG2 cleanup

Stop any watcher with Ctrl+C.

Uninstall the hooks before cleaning the repository:

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
pre-commit uninstall
pre-commit uninstall --hook-type pre-push
```

Restore the disposable demo branch:

```powershell
git reset --hard origin/main
git clean -fd
```

If the branch was pushed remotely:

```powershell
git push origin --delete quack-demo-do-not-merge
```

Return to the normal branch:

```powershell
git checkout main
git branch -D quack-demo-do-not-merge
```

Use this cleanup order:

1. Stop the watcher.
2. Uninstall both hooks.
3. Reset the disposable branch.
4. Remove generated files.
5. Delete the remote demo branch if applicable.
6. Return to `main`.
