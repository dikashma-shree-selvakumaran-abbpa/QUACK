"""Deterministic Sonar scanner result queries for Watch and pre-commit.

The scanner uploads the exact staged or working snapshot. This module then
reads findings directly from SonarQube's Web API and correlates them with the
scanner analysis ID. No MCP server or model call participates in the result.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from . import sonar, sonar_debug

REPORT_RELATIVE_PATH = Path("docs") / "SONARQUBE_REPORT.md"
MAX_API_BYTES = 2_000_000
MAX_FINDING_PAGES = 10
PAGE_SIZE = 500
MAX_REPORT_BYTES = 250_000
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")

MEASURE_KEYS = (
	"bugs",
	"code_smells",
	"cognitive_complexity",
	"coverage",
	"ncloc",
	"reliability_rating",
	"security_hotspots",
	"security_rating",
	"vulnerabilities",
)


@dataclass(frozen=True)
class SonarApiOutcome:
	"""One bounded direct Sonar Web API result."""

	name: str
	status: str
	description: str
	data: Any = None


@dataclass(frozen=True)
class SonarCliResult:
	"""Correlated scanner plus direct-API quality result."""

	status: str
	reason: str
	duration_s: float = 0.0
	project_key: str | None = None
	report_path: Path | None = None
	outcomes: tuple[SonarApiOutcome, ...] = ()
	violation_count: int = 0
	branch: str | None = None
	fresh: bool = True
	analysis_id: str | None = None
	correlated: bool = True

	@property
	def blocks_commit(self) -> bool:
		return (
			self.fresh
			and self.correlated
			and self.status in {"passed", "partial"}
			and self.violation_count > 0
		)


def report_path(repo_root: str | Path) -> Path:
	return Path(repo_root).resolve() / REPORT_RELATIVE_PATH


def is_report_file(path: str | Path) -> bool:
	normalised = str(path).replace("\\", "/").lstrip("./")
	return normalised.casefold() == REPORT_RELATIVE_PATH.as_posix().casefold()


def run(
	delta: Any,
	repo_root: str | Path,
	*,
	source: str = "unknown",
	project_key: str | None = None,
	host_url: str | None = None,
	branch: str | None = None,
	fresh: bool | None = None,
	expected_analysis_id: str | None = None,
	minimum_analysis_at: float | None = None,
) -> SonarCliResult | None:
	"""Read all current changed-file findings directly from SonarQube."""
	started = perf_counter()
	if not getattr(delta, "files", None):
		return None
	if fresh is False:
		return _result(
			"failed",
			"scanner did not complete; Sonar API was not queried",
			started,
			project_key=project_key,
			branch=branch,
			fresh=False,
		)
	root = Path(repo_root).resolve()
	settings = sonar.configuration(root, staged=source == "pre-commit")
	project = (project_key or settings.project_key).strip()
	host = (host_url or settings.host_url).rstrip("/")
	selected_branch = branch if branch is not None else settings.branch
	if not _PROJECT_KEY_RE.fullmatch(project):
		return _result("failed", "invalid SonarQube project key", started, fresh=fresh)
	token = _token()
	if not token:
		return _result(
			"skipped",
			"Sonar token is not set",
			started,
			project_key=project,
			branch=selected_branch,
			fresh=fresh,
		)

	components = _changed_components(delta, project)
	analysis = _latest_analysis(host, token, project, selected_branch)
	issues = _paged_findings(
		host,
		token,
		"issues",
		"/api/issues/search",
		{
			"projects": project,
			"issueStatuses": "OPEN",
			**({"branch": selected_branch} if selected_branch else {}),
		},
		"issues",
	)
	hotspots = _paged_findings(
		host,
		token,
		"hotspots",
		"/api/hotspots/search",
		{
			"projectKey": project,
			"status": "TO_REVIEW",
			**({"branch": selected_branch} if selected_branch else {}),
		},
		"hotspots",
	)
	issues = _scope_findings(issues, "issues", components)
	hotspots = _scope_findings(hotspots, "hotspots", components)
	measures = _get_json(
		host,
		token,
		"measures",
		"/api/measures/component",
		{
			"component": project,
			"metricKeys": ",".join(MEASURE_KEYS),
			**({"branch": selected_branch} if selected_branch else {}),
		},
		required=False,
	)
	outcomes = (analysis, hotspots, issues, measures)

	finding_outcomes = (hotspots, issues)
	finding_failures = [item for item in finding_outcomes if item.status != "passed"]
	if len(finding_failures) == len(finding_outcomes):
		status = "failed"
		reason = "Sonar CLI result unavailable: " + "; ".join(
			item.description for item in finding_failures
		)
	elif finding_failures:
		status = "partial"
		reason = "Sonar CLI result partial: " + "; ".join(
			item.description for item in finding_failures
		)
	else:
		status = "passed"
		reason = "Sonar CLI result complete: " + "; ".join(
			item.description for item in finding_outcomes
		)
	if measures.status not in {"passed", "skipped"}:
		status = "partial" if status == "passed" else status
		reason += "; optional measures unavailable: " + measures.description

	analysis_id = _analysis_id(analysis)
	correlated = _correlated(
		analysis,
		analysis_id,
		expected_analysis_id,
		minimum_analysis_at,
	)
	if not correlated:
		status = "failed"
		reason += "; result does not match the scanner analysis"

	violation_count = _finding_count(hotspots, "hotspots") + _finding_count(
		issues, "issues"
	)
	if violation_count:
		reason += f"; {violation_count} SonarQube violation(s) detected"
	else:
		reason += "; no changed-file SonarQube violations were found"

	result = SonarCliResult(
		status=status,
		reason=_safe_text(reason, 800),
		duration_s=perf_counter() - started,
		project_key=project,
		outcomes=outcomes,
		violation_count=violation_count,
		branch=selected_branch,
		fresh=True if fresh is None else fresh,
		analysis_id=analysis_id,
		correlated=correlated,
	)
	if source != "pre-commit":
		result = _with_report(result, root, source)
	return result


def _latest_analysis(
	host: str, token: str, project: str, branch: str | None
) -> SonarApiOutcome:
	return _get_json(
		host,
		token,
		"analysis",
		"/api/project_analyses/search",
		{
			"project": project,
			"ps": 1,
			**({"branch": branch} if branch else {}),
		},
	)


def _paged_findings(
	host: str,
	token: str,
	name: str,
	path: str,
	params: dict[str, object],
	item_key: str,
) -> SonarApiOutcome:
	items: list[dict[str, Any]] = []
	for page in range(1, MAX_FINDING_PAGES + 1):
		outcome = _get_json(
			host,
			token,
			name,
			path,
			{**params, "p": page, "ps": PAGE_SIZE},
		)
		if outcome.status != "passed" or not isinstance(outcome.data, dict):
			return outcome
		page_items = outcome.data.get(item_key)
		if not isinstance(page_items, list):
			return SonarApiOutcome(name, "failed", f"{name} response shape unavailable")
		items.extend(item for item in page_items if isinstance(item, dict))
		paging = outcome.data.get("paging")
		total = paging.get("total") if isinstance(paging, dict) else len(items)
		if not isinstance(total, int) or len(items) >= total or not page_items:
			return SonarApiOutcome(
				name,
				"passed",
				_describe_findings(name, len(items)),
				{item_key: items, "paging": {"total": len(items)}},
			)
	return SonarApiOutcome(
		name,
		"failed",
		f"{name} exceeded the bounded {MAX_FINDING_PAGES * PAGE_SIZE} finding limit",
	)


def _get_json(
	host: str,
	token: str,
	name: str,
	path: str,
	params: dict[str, object],
	*,
	required: bool = True,
) -> SonarApiOutcome:
	query = urlencode(params, doseq=True)
	url = f"{host}{path}?{query}"
	sonar_debug.emit(
		"SonarQube CLI API input",
		{"operation": name, "method": "GET", "url": url, "parameters": params},
		secrets=(token,),
	)
	auth = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
	request = Request(
		url,
		headers={"Accept": "application/json", "Authorization": f"Basic {auth}"},
	)
	try:
		with _open_sonar(request, timeout=_api_timeout()) as response:
			raw = response.read(MAX_API_BYTES + 1)
			status_code = getattr(response, "status", 200)
		if len(raw) > MAX_API_BYTES:
			raise ValueError("response exceeded bounded output limit")
		payload = json.loads(raw)
		if not isinstance(payload, dict):
			raise ValueError("response was not a JSON object")
		sonar_debug.emit(
			"SonarQube CLI API output",
			{"operation": name, "status_code": status_code, "response": payload},
			secrets=(token,),
		)
		return SonarApiOutcome(name, "passed", f"{name} query completed", payload)
	except HTTPError as exc:
		try:
			body = exc.read(64 * 1024).decode("utf-8", errors="replace")
		except OSError:
			body = ""
		sonar_debug.emit(
			"SonarQube CLI API output",
			{"operation": name, "status_code": exc.code, "response": body},
			secrets=(token,),
		)
		status = "failed" if required else "unavailable"
		return SonarApiOutcome(name, status, f"{name} query returned HTTP {exc.code}")
	except (URLError, HTTPException, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
		sonar_debug.emit(
			"SonarQube CLI API output",
			{"operation": name, "error": _safe_text(str(exc), 300)},
			secrets=(token,),
		)
		status = "failed" if required else "unavailable"
		return SonarApiOutcome(name, status, f"{name} query unavailable: {_safe_text(str(exc), 200)}")


def _scope_findings(
	outcome: SonarApiOutcome, item_key: str, components: set[str]
) -> SonarApiOutcome:
	if outcome.status != "passed" or not isinstance(outcome.data, dict):
		return outcome
	items = outcome.data.get(item_key)
	if not isinstance(items, list):
		return outcome
	filtered = [
		item
		for item in items
		if isinstance(item, dict) and str(item.get("component", "")) in components
	]
	return replace(
		outcome,
		description=_describe_findings(outcome.name, len(filtered)),
		data={item_key: filtered, "paging": {"total": len(filtered)}},
	)


def _describe_findings(name: str, count: int) -> str:
	kind = "security hotspot" if name == "hotspots" else "open issue"
	if count == 0:
		return f"no changed-file {kind}s were found"
	return f"{count} changed-file {kind}(s) found"


def _changed_components(delta: Any, project: str) -> set[str]:
	return {
		f"{project}:{str(getattr(item, 'path', '')).replace(chr(92), '/')}"
		for item in getattr(delta, "files", ())
		if getattr(item, "path", None)
	}


def _analysis_id(outcome: SonarApiOutcome) -> str | None:
	if not isinstance(outcome.data, dict):
		return None
	analyses = outcome.data.get("analyses")
	if not isinstance(analyses, list) or not analyses or not isinstance(analyses[0], dict):
		return None
	value = analyses[0].get("key")
	return value if isinstance(value, str) and value else None


def _correlated(
	analysis: SonarApiOutcome,
	analysis_id: str | None,
	expected_analysis_id: str | None,
	minimum_analysis_at: float | None,
) -> bool:
	if analysis.status != "passed":
		return False
	if expected_analysis_id is not None:
		return analysis_id == expected_analysis_id
	if minimum_analysis_at is None:
		return analysis_id is not None
	if not isinstance(analysis.data, dict):
		return False
	analyses = analysis.data.get("analyses")
	if not isinstance(analyses, list) or not analyses or not isinstance(analyses[0], dict):
		return False
	value = analyses[0].get("date")
	if not isinstance(value, str):
		return False
	try:
		stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
	except ValueError:
		return False
	return stamp >= minimum_analysis_at - 30


def _finding_count(outcome: SonarApiOutcome, item_key: str) -> int:
	if outcome.status != "passed" or not isinstance(outcome.data, dict):
		return 0
	items = outcome.data.get(item_key)
	return len(items) if isinstance(items, list) else 0


def _with_report(result: SonarCliResult, root: Path, source: str) -> SonarCliResult:
	path = root / REPORT_RELATIVE_PATH
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		content = _markdown(result, source)
		if len(content.encode("utf-8")) > MAX_REPORT_BYTES:
			content = content[: MAX_REPORT_BYTES - 80].rstrip() + "\n\n_Report truncated._\n"
		temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
		temp.write_text(content, encoding="utf-8")
		os.replace(temp, path)
		return replace(result, report_path=path)
	except (OSError, UnicodeError, ValueError):
		return result


def _markdown(result: SonarCliResult, source: str) -> str:
	lines = [
		"# SonarQube CLI report",
		"",
		"> Generated deterministically by the Sonar scanner CLI and direct Sonar Web API reads.",
		"",
		f"- Trigger: `{_safe_text(source, 80)}`",
		f"- Project: `{_safe_text(result.project_key or 'unknown', 256)}`",
		f"- Branch: `{_safe_text(result.branch or 'default', 256)}`",
		f"- Analysis: `{_safe_text(result.analysis_id or 'unavailable', 256)}`",
		f"- Status: **{result.status.upper()}**",
		f"- Violations: **{result.violation_count}**",
		"",
		"## Outcome",
		"",
		_safe_text(result.reason, 1600),
		"",
		"## Findings",
		"",
	]
	for kind, key in (("HOTSPOT", "hotspots"), ("ISSUE", "issues")):
		outcome = next((item for item in result.outcomes if item.name == key), None)
		items = outcome.data.get(key, []) if outcome and isinstance(outcome.data, dict) else []
		for item in items if isinstance(items, list) else []:
			if not isinstance(item, dict):
				continue
			rule = _safe_text(item.get("rule") or item.get("ruleKey") or "unknown rule", 160)
			severity = _safe_text(item.get("severity") or item.get("vulnerabilityProbability") or "unknown", 80)
			component = _safe_text(item.get("component") or "unknown component", 300)
			line = item.get("line") or (item.get("textRange") or {}).get("startLine") or "?"
			message = _safe_text(item.get("message") or "no message", 600)
			lines.append(f"- **{kind}** `{rule}` [{severity}] `{component}:{line}` - {message}")
	if result.violation_count == 0:
		lines.append("No changed-file violations were returned.")
	return "\n".join(lines).rstrip() + "\n"


def _result(
	status: str,
	reason: str,
	started: float,
	*,
	project_key: str | None = None,
	branch: str | None = None,
	fresh: bool | None = None,
) -> SonarCliResult:
	return SonarCliResult(
		status=status,
		reason=reason,
		duration_s=perf_counter() - started,
		project_key=project_key,
		branch=branch,
		fresh=True if fresh is None else fresh,
		correlated=False,
	)


def _open_sonar(request: Request, *, timeout: float):
	try:
		return build_opener(ProxyHandler({})).open(request, timeout=timeout)
	except HTTPError:
		raise
	except (URLError, TimeoutError, OSError):
		return urlopen(request, timeout=timeout)


def _api_timeout() -> float:
	raw = os.environ.get("QUACK_SONAR_API_TIMEOUT_S", "10")
	try:
		return max(2.0, min(60.0, float(raw)))
	except (TypeError, ValueError):
		return 10.0


def _token() -> str:
	return (
		os.environ.get("SONAR_TOKEN", "").strip()
		or os.environ.get("SQ_TOKEN", "").strip()
		or os.environ.get("SONARQUBE_TOKEN", "").strip()
	)


def _safe_text(value: object, limit: int) -> str:
	text = " ".join(str(value).replace("\x00", "").split())
	for token in (
		os.environ.get("SONAR_TOKEN", ""),
		os.environ.get("SQ_TOKEN", ""),
		os.environ.get("SONARQUBE_TOKEN", ""),
	):
		if token:
			text = text.replace(token, "<redacted>")
	return text[:limit].rstrip()
