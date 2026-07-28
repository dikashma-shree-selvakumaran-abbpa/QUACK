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

> Honest note on the install banner: **there is no ASCII-art banner today.**
> `quack install` prints clean status lines (see §3). If you want a wordmark
> banner for the demo, that's a quick add — see §9.

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

```
quack: updated .pre-commit-config.yaml
pre-commit installed at .git\hooks\pre-commit
quack: pre-commit hook installed
quack: gitleaks already installed
```

If gitleaks isn't present, the last line becomes a best-effort install attempt.
This is all plain status text — **not** an ASCII banner.

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

## 8. quack agent (pre-push, optional advanced demo)

```powershell
$env:GITHUB_TOKEN = "<your token>"
quack agent            # investigates staged/pushed changes, runs tests, coaches
quack agent --fly      # also proposes a concrete patch
```

Requires a `GITHUB_TOKEN` and a git repo. Without a token it prints a clear
"needs GITHUB_TOKEN" message and exits cleanly.

---

## 9. Suggested demo script (5 minutes)

1. `quack install --local` in a sample repo — show the status lines.
2. Stage `demo_secrets.py` → `quack check` → **BLOCKED** panel + `🐤 QUACK!!!!`.
3. Add `# quack: allow` to one line → `quack check` → that finding disappears.
4. Fix the rest → `quack check` → green "commit allowed".
5. Try `git commit` normally → hook fires automatically (same result, no manual
   `quack check`).
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
