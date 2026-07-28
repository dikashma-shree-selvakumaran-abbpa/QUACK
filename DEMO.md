# QUACK — Demo & Adoption Walkthrough

A step-by-step guide to demoing quack and rolling it out on a real company
project or GitHub repo. Every command and example below was run against the
current build, so the outputs are what you'll actually see.

---

## 0. What quack is (30-second pitch)

quack is a **git quality gate** that inspects your **staged changes** before a
commit lands.

- **Tier 1 — deterministic, offline, BLOCKING.** Secrets, merge markers, and
  leftover debug code. No network, no AI, no flakiness. Exit code `1` stops the
  commit.
- **gitleaks power mode — optional, layered on top.** Hundreds of tuned rules
  when the `gitleaks` binary is present. Fail-open: if it's missing, quack still
  works.
- **Tier 2 — AI review, advisory, NON-blocking.** Uses GitHub Models when
  `GITHUB_TOKEN` is set. Fail-open: never changes the exit code.
- **quack agent — agentic pre-push loop.** Investigates, runs tests, and coaches
  (patch is opt-in via `--fly`).

> `quack install` opens with a **celebration ASCII banner** (duck + `QUACK`
> wordmark), then clean status lines (see §3). It's TTY/NO_COLOR-aware, so CI
> logs stay plain.

---

## 1. Prerequisites (one-time, per machine)

```powershell
pipx install pre-commit          # the hook framework quack rides on
pipx install git+https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK   # or: pip install -e . from a clone
quack --help                     # confirm it's on PATH
```

gitleaks is **auto-installed** by `quack install` (via winget on Windows,
brew on macOS/Linux). On Linux without Homebrew, install gitleaks once by hand
(see the README "Platform support" section) — quack still runs on its built-in
patterns regardless. The `pipx`/`quack` commands above are identical on Windows,
macOS, and Linux.

---

## 2. Add quack to a company project / GitHub repo

### Option A — Local mode (fastest, no published repo dependency)

```powershell
cd C:\path\to\your-project
quack install --local
git add .pre-commit-config.yaml
git commit -m "chore: add quack pre-commit gate"
```

Writes this stanza (runs the `quack` already on your PATH):

```yaml
repos:
  - repo: local
	hooks:
	  - id: quack
		name: quack
		entry: quack check
		language: system
		pass_filenames: false
		stages: [pre-commit]
```

### Option B — Pinned mode (best for teams; everyone gets the same version)

```powershell
cd C:\path\to\your-project
quack install                    # writes a repo+rev stanza
```

```yaml
repos:
  - repo: https://github.com/dikashma-shree-selvakumaran-abbpa/QUACK
	rev: v0.1.0
	hooks:
	  - id: quack
```

Teammates then just run `pre-commit install` after cloning — the hook fires on
every commit, in **cmd, VS Code, Visual Studio, or any git client**, because it
lives in git itself, not the editor.

---

## 3. What `quack install` prints (live, verified)

First the celebration banner:

```
╭──────────────────── QUACK ────────────────────╮
│    _      _      ___  _   _   _    ___ _  __    │
│   ( `-.  ( `-.  / _ \| | | | / \  / __| |/ /    │
│    `-. \  `-. \| |_| | |_| |/ _ \| |  | ' <     │
│   __.-'/__.-'/  \__\_\\___//_/ \_\\__||_|\_\    │
│  (__.-'(__.-'    Q  U  A  C  K   -   installed  │
╰──────── your commits just got a quality gate ──╯
```

Then the status lines:

```
quack: updated .pre-commit-config.yaml
pre-commit installed at .git\hooks\pre-commit
quack: pre-commit hook installed
quack: gitleaks already installed
```

The banner is TTY/NO_COLOR-aware (rich drops ANSI when piped or `NO_COLOR` is
set). If gitleaks isn't present, the last line becomes a best-effort install.

---

## 4. Capability showcase — reliable live examples

These use quack's **deterministic built-in patterns**, so they block the same
way every time (great for a live demo — no dependency on gitleaks entropy).

### Demo file: `demo_secrets.py`

```python
import os


def connect():
	aws_key = "AKIA1234567890ABCDEF"            # AWS access key id
	gh_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # GitHub token
	password = "s3cr3t_p@ssw0rd_value_here"     # hardcoded credential
	return aws_key
```

Run it:

```powershell
git add demo_secrets.py
quack check
```

Verified output (blocking panel):

```
╭─ quack - 1 file(s) - +5/-0 - 0.0s ───────────────────────────────╮
│ ✗  secrets  demo_secrets.py:5  AWS access key id                  │
│ ✗  secrets  demo_secrets.py:6  GitHub token                       │
│ ✗  secrets  demo_secrets.py:7  hardcoded credential               │
│ ──────────────────────────────────────────────────────────────── │
│ 🐤 QUACK!!!! check line #5, #6, #7                                 │
╰─ 🐤 BLOCKED - fix and re-stage ──────────────────────────────────╯
```

Exit code `1` — the commit is stopped.

### Full built-in detection matrix (all deterministic)

| Category        | Example that triggers it                                  |
|-----------------|-----------------------------------------------------------|
| AWS key         | `AKIA` + 16 uppercase/digits                              |
| GitHub token    | `ghp_...`, `gho_...`, `github_pat_...`                    |
| Private key     | `-----BEGIN RSA PRIVATE KEY-----`                         |
| Azure Storage   | `AccountKey=<60+ base64 chars>`                           |
| Azure DevOps PAT| a bare 52-char base32 string (`[a-z2-7]{52}`)             |
| Slack token     | `xoxb-...`, `xoxp-...`                                    |
| Hardcoded cred  | `password = "….16+ chars…"` (key/secret/token/password)  |
| Merge marker    | a line starting with `<<<<<<<`, `=======`, or `>>>>>>>`  |
| Debug code      | `console.log(`, `breakpoint()`, `pdb.set_trace()`, `Debugger.Break()`, or `print(` near a debug marker |
| Large file      | staged file > 512 KB (warning, not blocking)             |

### gitleaks power-mode bonus (needs a real secret shape + a HEAD commit)

With gitleaks installed, quack additionally catches **Stripe, GCP, generic
high-entropy keys**, etc. that the built-ins don't know. Findings show as
`gitleaks: <rule-id>`.

---

## 5. The "buried in a big file" showcase

Drop one secret on line 42 of an otherwise normal 60-line file. quack reports
the **exact line number** — it scans added diff lines, tracking hunk offsets:

```
│ ✗  secrets  services/payments.py:42  AWS access key id            │
│ 🐤 QUACK!!!! check line #42                                        │
```

This is the strongest "it actually works" moment — it's not grepping the whole
file, it's precisely locating the added line.

---

## 6. Edge cases to show (and why they behave that way)

| Edge case | Behavior | Why |
|-----------|----------|-----|
| Secret on a **removed** (`-`) line | **Not** flagged | quack only scans *added* lines — you're deleting the leak, not adding it. |
| Secret you *intend* to keep (fixture, example) | Add `# quack: allow` or `# pragma: allowlist secret` on that line | Suppresses **both** quack built-ins and gitleaks on that line. |
| `git commit --no-verify` | Bypasses quack entirely | It's a git escape hatch. Prefer the inline allowlist so the gate stays honest. Consider a server-side check for the real gate. |
| gitleaks not installed | quack still runs on built-ins | Fully fail-open. |
| No `GITHUB_TOKEN` | Tier 2 AI review skips silently | Fail-open; exit code unaffected. |
| Fresh repo with **no commits** | gitleaks may find nothing | `gitleaks protect --staged` compares against HEAD; make a first commit for full power mode. |
| Small change (< 5 lines) | AI review skips | Not worth a model call; quack says "AI: skipped (small change)". |
| Fake/low-entropy "secret" | gitleaks ignores it | It's entropy/checksum-aware. Use quack's built-ins for guaranteed demo hits. |

### Inline allowlist demo

```python
API_KEY = "AKIA1234567890ABCDEF"  # quack: allow
```

```powershell
git add config.py
quack check      # passes — that line is exempt for quack AND gitleaks
```

---

## 7. The clean-pass experience

Stage something harmless:

```powershell
"def add(a, b):`n    return a + b" | Set-Content math_utils.py
git add math_utils.py
quack check
```

You get a green advisory panel ("advisory: commit allowed"), exit code `0`, and
the commit proceeds.

---

## 8. quack agent — the agentic pre-push brain (full catalog)

`quack check` is the fast, deterministic commit gate. `quack agent` is the
**investigative pre-push loop**: instead of pattern-matching, it *reasons about
your diff, reads the relevant code, runs the right tests, and diagnoses root
causes* — then either coaches you or hands you a ready patch.

### 8.1 What it actually does (the loop)

```
diff → hypothesis → read code → run smallest test set → diagnose → JSON verdict
```

It is a **plain, inspectable tool-calling loop** (no agent framework). The model
gets exactly three **read-only** tools and must investigate, not guess:

| Tool | What it does | Guardrail |
|------|--------------|-----------|
| `read_file(path)` | Returns first 300 lines of a repo file | Path must resolve **inside** the repo — `..`/absolute paths are rejected |
| `list_dir(path)` | Lists entry names of a repo directory | Same path containment |
| `run_tests(project_or_paths)` | Runs pytest files **or** a C# `.csproj [--filter ...]` | Only the two whitelisted shapes; C# `--filter` is validated against a strict charset — no shell metacharacters |

### 8.2 Hard safety invariants (great to show a security-minded audience)

- **Path containment** — the agent can never read outside the repo root. Escapes
  return an error string; a tool never raises.
- **Command whitelist** — the *only* thing it can execute is tests, in exactly
  two shapes (`quack.runio`). No arbitrary commands, ever.
- **Budgets** — at most **8 iterations**, at most **2 `run_tests` calls**, and a
  **180-second** wall-clock cap. When the budget runs out, a final answer is
  *forced* from the evidence already gathered.
- **Never crashes** — malformed final JSON is retried once, then degrades to a
  clear message. An honest "unverified" beats a confident guess.
- **Ground-truth reconciliation** — the model's verdict is cross-checked against
  the *actual* test exit codes, so it can't claim "tests pass" if they failed.

### 8.3 Two modes: coach (default) vs. fly

```powershell
$env:GITHUB_TOKEN = "<your github models token>"

quack agent          # THE DUCK WAY: diagnosis + understanding, patch withheld
quack agent --fly    # SKIP AHEAD: also reveals the ready-to-apply patch
```

- **Default (coaching)** — shows the summary, which tests ran, and a root-cause
  diagnosis of any failure, but *withholds the patch* so you learn the fix.
- **`--fly`** — reveals a minimal unified-diff patch fixing the root cause, plus
  a proposed missing test when a changed behavior has none.

### 8.4 The verdict schema (what it always returns)

```json
{
  "summary": "one-line risk assessment",
  "tests_run": ["tests/test_payments.py"],
  "failures": [{"test": "test_refund", "diagnosis": "root cause + which diff line"}],
  "proposed_patch": "unified diff (shown only with --fly)",
  "proposed_new_tests": "test source for uncovered behavior"
}
```

### 8.5 Real-life agent scenarios to demo

| Scenario | What the agent does | What you see |
|----------|--------------------|--------------|
| **You changed a function with existing tests** | Reads the function + its test, runs that test file | "verified: tests/test_x.py passes" — safe to push |
| **You introduced a regression** | Runs the test, it fails, agent traces it to the exact changed line | root-cause diagnosis; `--fly` gives the fix patch |
| **You added new behavior with NO test** | Notices the coverage gap | proposes a concrete new test in `proposed_new_tests` |
| **C# project change** | Runs `dotnet test <csproj> --filter <TestName>` | scoped test run, no full-suite wait |
| **Ambiguous / big diff** | Investigates within budget, then stops | honest "verified X, unknown Y, check Z manually" |
| **No `GITHUB_TOKEN`** | Skips cleanly | "quack agent: needs GITHUB_TOKEN, none found", exit 0 |
| **Not a git repo / nothing staged** | Exits gracefully | clear metadata line, exit 0 |

### 8.6 Agent edge cases (for the catalog)

| Edge case | Behavior | Why |
|-----------|----------|-----|
| Model asks to read `../../etc/passwd` | Tool returns an error string, loop continues | Path containment invariant |
| Model tries to "run" a non-test command | Not possible — only `run_tests` exists | Command whitelist |
| Model loops without concluding | Forced final answer after 8 iters / 180s | Budget cap |
| Model claims "all pass" but a test failed | Verdict reconciled against real exit codes | Ground-truth cross-check |
| Model returns junk instead of JSON | Retried once, then clean degrade | Tier 2 retry pattern |
| Test runner itself errors | Returns `(exit_code, output)`, never raises | `runio` is failure-tolerant |

### 8.7 Where check vs. agent fit

| | `quack check` | `quack agent` |
|--|--------------|----------------|
| **When** | every commit (pre-commit) | before push (manual / pre-push) |
| **Speed** | milliseconds, offline | seconds, needs a token |
| **Blocking?** | Yes (Tier 1) | No — advisory |
| **Method** | deterministic patterns | reasoning + running tests |
| **Best at** | secrets, merge markers, debug code | regressions, missing tests, root causes |

---

## 9. Suggested demo script (7 minutes)

1. `quack install --local` in a sample repo — show the **celebration banner** +
   status lines.
2. Stage `demo_secrets.py` → `quack check` → **BLOCKED** panel + `🐤 QUACK!!!!`.
3. Add `# quack: allow` to one line → `quack check` → that finding disappears.
4. Fix the rest → `quack check` → green "commit allowed".
5. Try `git commit` normally → hook fires automatically (no manual `quack check`).
6. Introduce a regression in a tested function → `quack agent` → watch it read
   the code, run the test, and diagnose the root cause (coaching mode).
7. `quack agent --fly` → reveal the ready-to-apply patch + proposed test.
6. (Optional) bury a secret on line 42 of a big file → show precise line report.

---

## 10. Rollout checklist for a real team

- [ ] Publish quack (done: `v0.1.0` tag on the QUACK repo).
- [ ] Choose **pinned mode** (`quack install`) so everyone runs the same version.
- [ ] Commit `.pre-commit-config.yaml` to the repo.
- [ ] Add a README line: "run `pre-commit install` after cloning."
- [ ] Decide policy on `--no-verify` (education + optional server-side scan).
- [ ] Set `GITHUB_TOKEN` in dev environments if you want Tier 2 AI review.
- [ ] Optionally add quack to CI as `pre-commit run --all-files` for a safety net.

---

## Appendix — one-liner CI gate

```yaml
# .github/workflows/quack.yml (concept)
- run: pipx install pre-commit
- run: pre-commit run --all-files
```

This re-runs the same hooks server-side, closing the `--no-verify` gap.
