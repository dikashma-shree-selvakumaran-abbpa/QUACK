"""FastAPI server for quack service endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import quack
from . import cli, gitio, gitleaks, llmio, reviewcache, testmap, tier1, watch

from quack import __version__
app = FastAPI(title="quack", version=__version__)


class CheckRequest(BaseModel):
	repo_path: str | None = None


class ReviewRequest(BaseModel):
	repo_path: str | None = None
	model: str | None = None


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

