"""FastAPI server for quack service endpoints."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from statistics import median
import subprocess
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import quack
from . import (
	agent,
	cli,
	gitio,
	gitleaks,
	instructions,
	llmio,
	metrics,
	reviewcache,
	testmap,
	tier1,
	tier2,
	watch,
)
from .providers import copilot_sdk

from quack import __version__
app = FastAPI(title="quack", version=__version__)


class CheckRequest(BaseModel):
	repo_path: str | None = None


class ReviewRequest(BaseModel):
	repo_path: str | None = None
	model: str | None = None


class AgentRequest(BaseModel):
	repo_path: str | None = None
	model: str | None = None


class InstallRequest(BaseModel):
	repo_path: str | None = None
	use_local: bool = False
	quack_path: str | None = None
	install_gitleaks: bool = False
	consent_husky: bool = False


# Agent jobs live only in memory: `quack agent` takes 60-90s, so clients start
# a job and poll it. Stages hold rendered results only -- never raw diff text.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Finished jobs are kept briefly so a client that reloads can still
# collect its result, then dropped so the daemon does not grow forever.
JOB_RETENTION_S = 1800.0


def _sweep_jobs() -> None:
	"""Drop finished jobs older than JOB_RETENTION_S - call with _jobs_lock held."""
	cutoff = time.time() - JOB_RETENTION_S
	for job_id in [
		jid
		for jid, job in _jobs.items()
		if job["status"] != "running" and job["createdAt"] < cutoff
	]:
		del _jobs[job_id]


def _resolve_repo_path(repo_path: str | None) -> tuple[str | None, str | None]:
	"""Return (resolved repo root, error) - never falls back to the process cwd."""
	if not repo_path:
		return None, "repo_path is required"
	root = gitio.repo_root(root=repo_path)
	if not root:
		return None, f"not a git repository: {repo_path}"
	return root, None


@app.post("/check")
def check(req: CheckRequest | None = None) -> dict:
	req = req or CheckRequest()
	root, error = _resolve_repo_path(req.repo_path)
	if root is None:
		raise HTTPException(status_code=400, detail=error)
	delta = gitio.staged_delta(root=root)
	if not delta.files:
		return {
			"schemaVersion": 1,
			"repository": root,
			"blocked": False,
			"findings": [],
			"testGuidance": [],
			"aiReview": None,
			"aiError": None,
		}

	redaction_findings = tier1.run(delta, tier1.Tier1Config())
	findings = redaction_findings
	if not os.environ.get("QUACK_DISABLE_GITLEAKS"):
		external = gitleaks.scan_staged(root)
		external = gitleaks.filter_allowlisted(
			external, tier1.allowlisted_locations(delta)
		)
		findings = gitleaks.merge(findings, external)

	blocked = tier1.should_block(findings, block_on=("secrets", "merge_markers"))
	json_findings = [
		{
			"severity": f.severity,
			"source": "gitleaks" if f.message.startswith("gitleaks:") else getattr(f, "source", "quack"),
			"rule": f.message.removeprefix("gitleaks: ") if f.message.startswith("gitleaks:") else f.check,
			"file": f.path,
			"line": f.line,
			"message": f.message,
		}
		for f in findings
	]

	if blocked:
		return {
			"schemaVersion": 1,
			"repository": root,
			"blocked": True,
			"findings": json_findings,
			"testGuidance": [],
			"aiReview": None,
			"aiError": None,
		}

	plan = testmap.build_plan(delta, root=Path(root))
	json_test_guidance = []
	if plan is not None:
		for mapping in getattr(plan, "mappings", []):
			if mapping.tests:
				json_test_guidance.append(
					{
						"sourceFile": mapping.source,
						"testOrCommand": ", ".join(mapping.tests),
						"status": "mapped",
						"recommendation": "run mapped tests",
					}
				)
		for source in getattr(plan, "untested_sources", []):
			json_test_guidance.append(
				{
					"sourceFile": source,
					"testOrCommand": "",
					"status": "untested",
					"recommendation": "no tests found",
				}
			)

	redacted = tier1.redact(delta, redaction_findings)
	entry = reviewcache.read(root, reviewcache.diff_hash(redacted.raw_diff))
	cached_review = None
	cached_model = ""
	if entry is not None:
		cached_review, cached_model = cli._cached_review(entry.review_payload)

	ai_review = None
	ai_error = None
	if cached_review is not None and entry is not None:
		ai_review = {
			"available": True,
			"model": cached_model,
			"risk": cached_review.risk,
			"summary": cached_review.one_liner,
			"reasons": cached_review.reasons,
			"testsToRun": cached_review.tests_to_run,
			"missingTests": cached_review.missing_tests,
			"reviewedAtUtc": datetime.fromtimestamp(entry.timestamp, tz=timezone.utc).isoformat(),
		}
	else:
		ai_error = "AI review: not reviewed yet - run `quack watch` to review in the background"

	return {
		"schemaVersion": 1,
		"repository": root,
		"blocked": False,
		"findings": json_findings,
		"testGuidance": json_test_guidance,
		"aiReview": ai_review,
		"aiError": ai_error,
	}


@app.post("/review")
def review(req: ReviewRequest | None = None) -> dict:
	req = req or ReviewRequest()
	root, error = _resolve_repo_path(req.repo_path)
	if root is None:
		raise HTTPException(status_code=400, detail=error)
	res = watch.review_once(root, model=req.model, quiet=True)
	if res.diff_hash:
		return {
			"schemaVersion": 1,
			"status": "reviewed",
			"diffHash": res.diff_hash,
		}
	if res.reason == "no changes":
		return {
			"schemaVersion": 1,
			"status": "skipped",
			"reason": "nothing to review",
		}
	return {
		"schemaVersion": 1,
		"status": "error",
		"reason": res.reason or "unknown error",
	}


@app.get("/models")
def models() -> dict:
	try:
		discovered = llmio.list_models()
	except Exception:
		discovered = []
	default_mod = llmio.default_model() or ""
	provider = os.environ.get("QUACK_PROVIDER") or llmio.DEFAULT_PROVIDER
	return {
		"schemaVersion": 1,
		"defaultModel": default_mod,
		"models": [
			{
				"id": m,
				"displayName": m,
				"provider": provider,
				"available": True,
			}
			for m in discovered
		],
	}


@app.get("/metrics")
def metrics_report() -> dict:
	# Metrics are per-user (user profile), so there is no repo_path here.
	events = metrics.read()
	if events is None:
		return {"schemaVersion": 1, "available": False, "totalRuns": 0}

	commands = Counter(str(event.get("command")) for event in events if event.get("command"))
	findings: Counter[str] = Counter()
	durations: list[int] = []
	cache_hits = 0
	cache_lookups = 0
	blocks = 0
	for event in events:
		blocks += int(event.get("blocked") is True)
		duration = event.get("duration_ms")
		if isinstance(duration, (int, float)) and not isinstance(duration, bool):
			durations.append(int(duration))
		raw_findings = event.get("tier1_findings")
		if isinstance(raw_findings, dict):
			for name, count in raw_findings.items():
				if isinstance(name, str) and isinstance(count, int) and count > 0:
					findings[name] += count
		cache = event.get("review_cache")
		if cache in {"hit", "miss"}:
			cache_lookups += 1
			cache_hits += int(cache == "hit")

	return {
		"schemaVersion": 1,
		"available": True,
		"totalRuns": len(events),
		"runsByCommand": dict(sorted(commands.items())),
		"blocks": blocks,
		"findings": dict(findings.most_common()),
		"medianDurationMs": median(durations) if durations else None,
		"cacheHitRate": cache_hits / cache_lookups if cache_lookups else None,
	}


def _bounded(text: str) -> str:
	"""One-line, length-capped text - never a stack trace."""
	return text.replace("\n", " ").replace("\r", " ")[:200]


def _finding_json(f) -> dict:
	return {
		"severity": f.severity,
		"source": getattr(f, "source", "quack"),
		"rule": f.check,
		"file": f.path,
		"line": f.line,
		"message": f.message,
	}


def _job_json(job: dict) -> dict:
	return {
		"schemaVersion": 1,
		"jobId": job["id"],
		"status": job["status"],
		"repoPath": job["repoPath"],
		"stages": dict(job["stages"]),
		"error": job["error"],
	}


def _job_cancelled(job_id: str) -> bool:
	with _jobs_lock:
		job = _jobs.get(job_id)
		return job is None or job["status"] == "cancelled"


def _set_stage(job_id: str, name: str, value: dict) -> None:
	with _jobs_lock:
		job = _jobs.get(job_id)
		if job is None or job["status"] == "cancelled":
			return
		job["stages"][name] = value


def _finish_job(job_id: str, status: str, error: str | None = None) -> None:
	with _jobs_lock:
		job = _jobs.get(job_id)
		if job is None or job["status"] == "cancelled":
			return
		job["status"] = status
		job["error"] = error


def _run_agent_job(job_id: str, root: str, model: str | None) -> None:
	"""Mirror `quack agent` (see cli.agent), recording each stage as it lands."""
	try:
		resolved_model = cli._resolve_agent_model(model)
		reason = llmio.availability_error()
		if reason:
			_finish_job(job_id, "error", reason)
			return

		delta = gitio.staged_delta(root=root)
		if not delta.files:
			upstream = gitio.upstream_ref(root=root)
			unpushed = gitio.range_delta(upstream, root=root) if upstream else None
			if unpushed is not None and unpushed.files:
				delta = unpushed
			else:
				_set_stage(
					job_id,
					"tier1",
					{
						"blocked": False,
						"findings": [],
						"note": "nothing to analyze: no staged changes and nothing unpushed",
					},
				)
				_finish_job(job_id, "done")
				return

		findings = tier1.run(delta, tier1.Tier1Config())
		redacted = tier1.redact(delta, findings)
		json_findings = [_finding_json(f) for f in findings]

		# Tier 1 gates. A staged secret is never transmitted: return before
		# any model call.
		if tier1.should_block(findings, block_on=("secrets", "merge_markers")):
			_set_stage(
				job_id, "tier1", {"blocked": True, "findings": json_findings}
			)
			_finish_job(job_id, "done")
			return

		_set_stage(job_id, "tier1", {"blocked": False, "findings": json_findings})
		if _job_cancelled(job_id):
			return

		tier2_model = cli._resolve_completion_model(model)
		try:
			plan = testmap.build_plan(delta, root=Path(root))
			review, tier2_reason = tier2.review_with_reason(
				delta,
				findings,
				plan,
				model=tier2_model,
				project_instructions=instructions.load(Path(root)),
				timeout_s=llmio.default_timeout(),
			)
		except Exception as exc:
			review = None
			tier2_reason = f"{type(exc).__name__}: {_bounded(str(exc))}"
		if review is not None:
			_set_stage(
				job_id,
				"tier2",
				{
					"available": True,
					"model": tier2_model or "",
					"risk": review.risk,
					"summary": review.one_liner,
					"reasons": review.reasons,
					"testsToRun": review.tests_to_run,
					"missingTests": review.missing_tests,
				},
			)
		else:
			_set_stage(
				job_id,
				"tier2",
				{
					"available": False,
					"error": tier2_reason
					or llmio.availability_error()
					or "unavailable",
				},
			)
		if _job_cancelled(job_id):
			return

		try:
			result = copilot_sdk.run_agent(
				redacted.raw_diff,
				Path(root),
				resolved_model,
				timeout_s=agent.WALL_CLOCK_S,
			)
		except llmio.LLMUnavailable as exc:
			result = agent._unavailable(
				agent._timeout_hint(exc.reason, resolved_model)
			)
		_set_stage(
			job_id,
			"investigation",
			{
				"summary": result.summary,
				"testsRun": result.tests_run,
				"failures": result.failures,
				"proposedPatch": result.proposed_patch,
				"proposedNewTests": result.proposed_new_tests,
			},
		)
		_finish_job(job_id, "done")
	except Exception as exc:
		_finish_job(job_id, "error", f"{type(exc).__name__}: {_bounded(str(exc))}")


@app.post("/agent")
def start_agent(req: AgentRequest | None = None) -> dict:
	req = req or AgentRequest()
	root, error = _resolve_repo_path(req.repo_path)
	if root is None:
		raise HTTPException(status_code=400, detail=error)

	with _jobs_lock:
		_sweep_jobs()
		for job in _jobs.values():
			if job["repoPath"] == root and job["status"] == "running":
				return {
					"schemaVersion": 1,
					"jobId": job["id"],
					"status": "running",
				}
		job_id = uuid.uuid4().hex
		_jobs[job_id] = {
			"id": job_id,
			"repoPath": root,
			"status": "running",
			"createdAt": time.time(),
			"stages": {"tier1": None, "tier2": None, "investigation": None},
			"error": None,
		}

	threading.Thread(
		target=_run_agent_job,
		args=(job_id, root, req.model),
		daemon=True,
	).start()
	return {"schemaVersion": 1, "jobId": job_id, "status": "running"}


@app.get("/agent/{job_id}")
def agent_job(job_id: str) -> dict:
	with _jobs_lock:
		job = _jobs.get(job_id)
		if job is None:
			raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
		return _job_json(job)


@app.delete("/agent/{job_id}")
def cancel_agent_job(job_id: str) -> dict:
	with _jobs_lock:
		job = _jobs.get(job_id)
		if job is None:
			raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
		job["status"] = "cancelled"
	return {"schemaVersion": 1, "jobId": job_id, "status": "cancelled"}


def _install_strategy(root: str) -> tuple[str, str | None]:
	"""Return (strategy, hooksPath) for the repository at ``root``."""
	hooks_path = cli._hooks_path(root=root)
	if not hooks_path:
		return "precommit", None
	if cli._is_husky(hooks_path):
		return "husky", hooks_path
	return "unsupported_hooks_path", hooks_path


def _tracked(root: str, relative: str) -> bool:
	try:
		result = subprocess.run(
			["git", "ls-files", "--error-unmatch", relative],
			capture_output=True,
			text=True,
			check=False,
			cwd=root,
		)
	except OSError:
		return False
	return result.returncode == 0


@app.get("/install/plan")
def install_plan(repo_path: str | None = Query(default=None)) -> dict:
	root, error = _resolve_repo_path(repo_path)
	if root is None:
		raise HTTPException(status_code=400, detail=error)

	strategy, hooks_path = _install_strategy(root)
	husky_tracked = strategy == "husky" and (
		_tracked(root, ".husky/pre-commit") or _tracked(root, ".husky/pre-push")
	)
	return {
		"schemaVersion": 1,
		"repository": root,
		"strategy": strategy,
		"hooksPath": hooks_path,
		"huskyFilesTracked": husky_tracked,
		"preCommitAvailable": shutil.which("pre-commit") is not None,
		"gitleaksAvailable": gitleaks.available(),
	}


def _step(name: str, ok: bool, detail: str) -> dict:
	return {"name": name, "ok": ok, "detail": detail}


@app.post("/install")
def install(req: InstallRequest | None = None) -> dict:
	req = req or InstallRequest()
	root, error = _resolve_repo_path(req.repo_path)
	if root is None:
		raise HTTPException(status_code=400, detail=error)

	strategy, hooks_path = _install_strategy(root)
	if strategy == "unsupported_hooks_path":
		raise HTTPException(
			status_code=409,
			detail=(
				f"core.hooksPath is set to {hooks_path}; quack cannot install "
				f"hooks automatically"
			),
		)
	if strategy == "husky" and req.consent_husky is not True:
		raise HTTPException(
			status_code=409,
			detail=(
				"husky hooks are tracked in git: installing quack affects everyone "
				"on this branch, so explicit consent is required"
			),
		)

	# Quote: Windows paths have spaces.
	quack_cmd = f'"{req.quack_path}"' if req.quack_path else "quack"
	steps: list[dict] = []

	if strategy == "husky":
		husky_dir = Path(root) / ".husky"
		husky_dir.mkdir(exist_ok=True)
		for hook, command in (
			("pre-commit", f"{quack_cmd} check"),
			("pre-push", f"{quack_cmd} agent"),
		):
			try:
				action = cli._install_into_husky(husky_dir / hook, command)
				steps.append(
					_step(f"husky:{hook}", True, f"{action} quack block in .husky/{hook}")
				)
			except OSError as exc:
				steps.append(_step(f"husky:{hook}", False, _bounded(str(exc))))
	else:
		config_path = Path(root) / ".pre-commit-config.yaml"
		try:
			if req.use_local:
				cli._upsert_local_stanza(config_path, req.quack_path)
			else:
				cli._upsert_precommit_stanza(config_path)
			steps.append(_step("config", True, f"updated {config_path}"))
		except Exception as exc:
			steps.append(_step("config", False, _bounded(str(exc))))

		if shutil.which("pre-commit"):
			# Independent runs: a pre-push failure must not undo a successful
			# pre-commit install.
			for name, args in (
				("pre-commit-hook", ["pre-commit", "install"]),
				(
					"pre-push-hook",
					["pre-commit", "install", "--hook-type", "pre-push"],
				),
			):
				try:
					result = subprocess.run(
						args,
						capture_output=True,
						text=True,
						check=False,
						cwd=root,
					)
				except OSError as exc:
					steps.append(_step(name, False, _bounded(str(exc))))
					continue
				ok = result.returncode == 0
				steps.append(
					_step(
						name,
						ok,
						"installed"
						if ok
						else f"`{' '.join(args)}` failed ({result.returncode})",
					)
				)
		else:
			steps.append(
				_step(
					"pre-commit-hook",
					False,
					"`pre-commit` not found; install it with: pipx install pre-commit",
				)
			)

	if req.install_gitleaks:
		installed, message = gitleaks.ensure_installed()
		steps.append(_step("gitleaks", installed, message))
	else:
		steps.append(
			_step(
				"gitleaks",
				gitleaks.available(),
				"gitleaks available" if gitleaks.available() else "gitleaks not installed (skipped)",
			)
		)

	return {
		"schemaVersion": 1,
		"repository": root,
		"strategy": strategy,
		"steps": steps,
	}


@app.get("/status")
def status() -> dict:
	try:
		from quack._build_info import BUILD_COMMIT, BUILD_DATE
	except ImportError:
		BUILD_COMMIT = None
		BUILD_DATE = None
	return {
		"schemaVersion": 1,
		"version": quack.__version__,
		"availabilityError": llmio.availability_error(),
		"cachePath": str(reviewcache.cache_path()),
		"buildCommit": BUILD_COMMIT,
		"buildDate": BUILD_DATE,
	}

