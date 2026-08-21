# quack — demo runbook

The document to keep open in a second window while presenting. README covers
what quack is, SETUP.md covers installation, CURRENT_STATE.md covers
architecture. This is only what you need while running the demo.

---

## Pre-flight

Run these before you start. Every one must pass.

| Command | Expected |
|---|---|
| `quack --version` | `quack, version 0.3.0` |
| `where.exe quack` | exactly ONE path |
| `echo $env:PYTHONIOENCODING` | `utf-8` |
| `echo "[$env:GITHUB_TOKEN][$env:GH_TOKEN][$env:COPILOT_GITHUB_TOKEN]"` | `[][][]` — all empty |
| `quack model` | `Auth status: available` plus a model list |
| `copilot -p "say ok"` | `ok` |

If `where.exe quack` shows two paths:

```powershell
Remove-Item "$env:USERPROFILE\.local\bin\quack.exe"
```

If any token is set — an ambient token shadows the Copilot login in the SDK's
auth precedence order:

```powershell
Remove-Item Env:\GITHUB_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\COPILOT_GITHUB_TOKEN -ErrorAction SilentlyContinue
```

If auth is unavailable: run `copilot`, type `/login`, complete the browser
flow, exit.

Use **plain PowerShell**, not the VS Developer PowerShell.

---

## Demo repos

**Throwaway** — for acts 0 to 2. Create fresh each run:

```powershell
cd C:\ABB\AI-Champs
Remove-Item -Recurse -Force demo-repo -ErrorAction SilentlyContinue
mkdir demo-repo; cd demo-repo
git init
```

**PG2 clone** — for acts 3 to 5. Real ABB code:

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
git checkout quack-demo-do-not-merge
git reset --hard origin/main
git status
```

---

## Act 0 — install

> "One command wires quack into any repo."

```powershell
cd C:\ABB\AI-Champs\demo-repo
quack install --local
```

Shows the QUACK banner, then:

```text
quack: updated .pre-commit-config.yaml
pre-commit installed at .git\hooks\pre-commit
quack: pre-commit hook installed
pre-commit installed at .git\hooks\pre-push
quack: pre-push hook installed
```

> "Two hooks. One at commit, one at push."

---

## Act 1 — a blocked commit

> "This is a token I should never have committed."

```powershell
$pat = 'a' * 52
"public string Token = `"$pat`";" | Set-Content secret.cs -Encoding utf8
git add secret.cs
git commit -m "add config"
```

The commit does not happen. Panel shows the finding with a red border:

```text
✗  secrets  secret.cs:1  Azure DevOps PAT
🐤 QUACK!!!!
🐤 BLOCKED - fix and re-stage
```

> "Two seconds. Never left my machine. No network, no token, no AI — just
> pattern matching."

---

## Act 2 — the allowlist

> "Sometimes it's a fixture and you mean it."

```powershell
"public string Token = `"$pat`";  // quack: allow" | Set-Content secret.cs -Encoding utf8
git add secret.cs
git commit -m "add config"
```

Commit succeeds.

> "Inline allowlist, per line. It suppresses gitleaks on that line too."

Clean up:

```powershell
git reset --hard HEAD
Remove-Item secret.cs -ErrorAction SilentlyContinue
```

---

## Act 3 — test guidance on real code

Switch to PG2.

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
quack install --local
```

Open `packages\GraphicsModelEditor\FabricWasmHost\KernelGraphicsAdapter.cs`.
In `BeginMove`, change:

```csharp
if (modelIndexes == null || modelIndexes.Length != selectedItems.Length)
```

to use `<` instead of `!=`. Save.

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
quack check
```

Test guidance resolves across package boundaries:

```text
dotnet test packages/GraphicsModelEditor/GfxKernel.Tests/GfxKernel.Tests.csproj
--no-build --filter "FullyQualifiedName~KernelGraphicsAdapterTests"
```

> "The source is in FabricWasmHost. The test project is a sibling package.
> It found it, and gave me the exact command — before I commit."

Also shows `AI review: not reviewed yet - run \`quack watch\``.

---

## Act 4 — watch mode

> "The AI review is the slow part. So it doesn't run at commit time."

```powershell
quack watch --once
```

Takes 10-20 seconds. Prints `reviewed 1 file(s) - risk: medium`.

```powershell
quack check
```

Instant. Full review, with an age note:

```text
AI - claude-haiku-4.5 - risk: MEDIUM
Validation relaxed from exact to minimum length; confirm downstream array index
(reviewed 8 sec ago by quack watch)
```

> "Same review. The call happened in the background while I was still
> working. Commit time stayed local and fast."

In real use `quack watch` runs continuously and reviews after a quiet period.

---

## Act 5 — pre-push

> "The last gate before code leaves my machine."

Revert act 3's change, then make a more serious one — in `EndMove`, delete the
`RestoreMouseDelegates();` line from the `finally` block. Save.

```powershell
git add packages/GraphicsModelEditor/FabricWasmHost/KernelGraphicsAdapter.cs
git commit -m "demo: intentional regression for quack demonstration"
git push -u origin quack-demo-do-not-merge
```

**This takes 20-26 seconds.** Say what is happening while it runs: the Copilot
runtime is starting and reviewing the unpushed commit.

```text
quack-agent....Passed
- duration: 26.14s
analyzing 1 unpushed commit(s)
AI - claude-haiku-4.5 - risk: HIGH
Removing delegate cleanup risks stale mouse handlers
...
```

Then the push completes.

> "It reviewed the commit, flagged it high risk, and let the push through.
> Only secrets block. Everything else is advisory."

---

## Act 6 — diagnostics and evidence

```powershell
quack model
```

> "Which provider, whether auth works, which models resolve. This is the
> answer to 'why did AI skip?'"

```powershell
quack metrics
```

> "Every run is logged locally — counts and durations only, no code, no paths.
> That's the evidence base for measuring whether this actually reduces CI
> failures."

---

## Reset between runs

**PG2:**

```powershell
cd C:\ABB\AI-Champs\PCP.Operations.HMI.Engineering.Graphics
git reset --hard origin/main
git push origin --delete quack-demo-do-not-merge
Remove-Item .pre-commit-config.yaml -ErrorAction SilentlyContinue
```

**Throwaway:**

```powershell
cd C:\ABB\AI-Champs
Remove-Item -Recurse -Force demo-repo
```

---

## If something goes wrong

| Symptom | What to say | What to do |
|---|---|---|
| Push hangs for 20-26s | "That's the Copilot runtime starting up and reviewing the commit — it's why watch mode exists." | Wait. It completes. |
| `AI review unavailable (Copilot login expired...)` | "That's the fail-open design — the AI is advisory, so the commit and push still go through." | Carry on. Fix later with `copilot` + `/login`. |
| `AI review: not reviewed yet` | "Watch mode hasn't seen this diff yet." | Run `quack watch --once`. |
| Transient SDK failure | Same fail-open line. | Retry once; it usually succeeds. |
| Terminal shows garbled characters | — | `$env:PYTHONIOENCODING = "utf-8"` |

**The rule:** nothing that fails here blocks a commit or a push. Say that out
loud when it happens — the failure demonstrates the design.

---

## Questions you will get

**Does it slow down commits?**

About two seconds, all local. The AI review runs in the background via watch
mode, so it never sits on the commit path.

**Does my code leave the machine?**

At commit time, no — nothing network at all. At pre-push, a redacted diff goes
to the Copilot service over the ABB-approved transport. Tier 1 strips detected
secrets before anything is sent.

**What if the AI is wrong?**

It's advisory. Only secrets and merge markers block a commit. Everything the AI
says is a suggestion you can ignore.

**Why does the agent need a different provider?**

The Copilot SDK exposes no OpenAI-style tool-calling surface, so the agent's
investigate-and-run-tests loop currently needs `QUACK_PROVIDER=github_models`.
Porting it to SDK-native tools is scoped but not built.

**What about people who use Copilot in the IDE, not the CLI?**

The hooks work regardless — they're git hooks. Whether the SDK finds the IDE's
stored credentials isn't documented. Run `quack model`; if auth says available,
nothing more is needed. If not, one `copilot` + `/login`.
