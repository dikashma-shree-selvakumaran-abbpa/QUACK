"""SonarQube MCP quality snapshots and the living repository report.

The MCP server is optional and fail-open when it cannot be reached. A
completed snapshot reports open issues and security hotspots as blocking
violations. This module keeps the integration independent from the model
provider, runs the requested read-only tools for the current change, and
writes one bounded Markdown report atomically.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .mcp import sonarqube as sonarqube_mcp

REPORT_RELATIVE_PATH = Path("docs") / "SONARQUBE_REPORT.md"
MAX_REPORT_BYTES = 250_000
MAX_DUPLICATION_FILES = 20
MAX_DETAIL_ROWS = 20
MAX_METRICS = 500

_PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_TOKEN_RE = re.compile(
	r"(?:ghp_|github_pat_|gho_|ghs_|ghu_|xox|AKIA)[A-Za-z0-9_-]+"
	r"|[A-Za-z0-9_+/=-]{32,}"
)

# This is the offline fallback. When the server exposes search_metrics, its
# catalog is preferred so custom metrics are included as well.
DEFAULT_METRIC_KEYS = (
	"alert_status",
	"bugs",
	"branch_coverage",
	"classes",
	"code_smells",
	"comment_lines",
	"comment_lines_density",
	"complexity",
	"conditions_to_cover",
	"cognitive_complexity",
	"coverage",
	"critical_violations",
	"duplicated_blocks",
	"duplicated_files",
	"duplicated_lines",
	"duplicated_lines_density",
	"files",
	"functions",
	"info_violations",
	"line_coverage",
	"lines",
	"major_violations",
	"minor_violations",
	"ncloc",
	"new_bugs",
	"new_branch_coverage",
	"new_code_smells",
	"new_conditions_to_cover",
	"new_coverage",
	"new_duplicated_lines_density",
	"new_line_coverage",
	"new_lines_to_cover",
	"new_maintainability_rating",
	"new_reliability_rating",
	"new_security_hotspots",
	"new_security_rating",
	"new_uncovered_conditions",
	"new_uncovered_lines",
	"new_violations",
	"reliability_rating",
	"security_hotspots",
	"security_rating",
	"skipped_tests",
	"sqale_index",
	"sqale_rating",
	"statements",
	"test_errors",
	"test_execution_time",
	"test_failures",
	"tests",
	"uncovered_conditions",
	"uncovered_lines",
	"violations",
	"vulnerabilities",
)

REQUIRED_METRIC_KEYS = (
	"cognitive_complexity",
	"ncloc",
	"reliability_rating",
)

METRIC_DESCRIPTIONS = {
	"cognitive_complexity": (
		"Aggregate cognitive complexity; lower values generally indicate "
		"control flow that is easier to understand."
	),
	"ncloc": "Non-comment lines of code; a size measure, not a quality grade.",
	"reliability_rating": (
		"Reliability rating for bug risk; A is the best rating and E is the "
		"weakest."
	),
	"security_rating": (
		"Security rating for vulnerability risk; A is the best rating."
	),
	"sqale_rating": (
		"Maintainability rating based on remediation effort; A is the best "
		"rating."
	),
	"coverage": "Overall test coverage percentage.",
	"duplicated_lines_density": "Percentage of lines duplicated in the project.",
	"violations": "Total open issues reported by the analyzer.",
	"bugs": "Issues classified as bugs.",
	"vulnerabilities": "Issues classified as vulnerabilities.",
	"security_hotspots": "Security Hotspots requiring review.",
	"code_smells": "Issues classified as maintainability code smells.",
	"complexity": "Cyclomatic complexity.",
}

_TOOL_ALIASES = {
	"sonarqube_get_duplications": (
		"sonarqube_get_duplications",
		"get_duplications",
	),
	"sonarqube_search_security_hotspot": (
		"sonarqube_search_security_hotspot",
		"sonarqube_search_security_hotspots",
		"sonarqube_security_hotspot_search",
		"sonarqube_security_hotspots_search",
		"search_security_hotspot",
		"search_security_hotspots",
		"security_hotspot_search",
		"security_hotspots_search",
	),
	"sonarqube_search_sonar_issues_in_projects": (
		"sonarqube_search_sonar_issues_in_projects",
		"search_sonar_issues_in_projects",
	),
	"sonarqube_get_component_measures": (
		"sonarqube_get_component_measures",
		"get_component_measures",
	),
	"sonarqube_search_metrics": (
		"sonarqube_search_metrics",
		"search_metrics",
	),
}


@dataclass(frozen=True)
class ToolOutcome:
	"""Safe result for one requested MCP operation."""

	name: str
	tool_name: str | None
	status: str
	description: str
	data: Any = None


@dataclass(frozen=True)
class SonarQubeReportResult:
	"""Display-safe result from one MCP report attempt."""

	status: str
	reason: str
	duration_s: float = 0.0
	project_key: str | None = None
	report_path: Path | None = None
	outcomes: tuple[ToolOutcome, ...] = ()
	violation_count: int = 0

	@property
	def blocks_commit(self) -> bool:
		"""Return whether this completed snapshot contains open findings."""
		return self.status == "passed" and self.violation_count > 0


def report_path(repo_root: str | Path) -> Path:
	"""Return the single repository-local living report path."""
	return Path(repo_root).resolve() / REPORT_RELATIVE_PATH


def is_report_file(path: str | Path) -> bool:
	"""Return whether a repository-relative path is the generated report."""
	normalised = str(path).replace("\\", "/").lstrip("./")
	return normalised.casefold() == REPORT_RELATIVE_PATH.as_posix().casefold()


def run(
	delta: Any,
	repo_root: str | Path,
	*,
	source: str = "unknown",
) -> SonarQubeReportResult | None:
	"""Run a bounded MCP snapshot and refresh the living report.

	No report is attempted when there is no change. Missing credentials,
	Podman, or a disabled integration return an advisory result without
	raising. Once a client is available, individual tool failures are recorded
	and the remaining read-only tools still run.
	"""
	started = perf_counter()
	if not getattr(delta, "files", None):
		return None
	if not sonarqube_mcp.enabled():
		return _result(
			"skipped",
			"disabled by QUACK_SONAR_MCP",
			started,
		)

	try:
		root = Path(repo_root).resolve()
	except (OSError, RuntimeError, TypeError, ValueError):
		return _result("failed", "repository path unavailable", started)

	try:
		configured_project_path = (
			os.environ.get("QUACK_SONAR_MCP_PROJECT_PATH", "").strip()
			or os.environ.get("SONARQUBE_PROJECT_PATH", "").strip()
		)
		client, connection_reason = sonarqube_mcp.connection_from_environment(
			project_path=configured_project_path or root,
			base_path=root,
		)
	except Exception as exc:
		return _result("failed", _exception_reason(exc), started)
	if client is None:
		return _result(
			"skipped",
			connection_reason or "SonarQube MCP configuration unavailable",
			started,
		)

	try:
		tools = client.tool_definitions()
	except Exception as exc:
		result = _result("failed", _exception_reason(exc), started)
		return _with_report(result, root, source)

	project_key = _project_key(client)
	if project_key is None:
		result = _result(
			"failed",
			"SonarQube project key unavailable; set SONARQUBE_PROJECT_KEY",
			started,
		)
		return _with_report(result, root, source)

	try:
		result = _collect(client, tools, delta, project_key, started)
	except Exception as exc:
		result = _result("failed", _exception_reason(exc), started)
	return _with_report(result, root, source)


def _collect(
	client: Any,
	tools: list[dict[str, Any]],
	delta: Any,
	project_key: str,
	started: float,
) -> SonarQubeReportResult:
	"""Collect all requested operations while keeping failures independent."""
	tool_map = {
		key: _find_tool(tools, aliases)
		for key, aliases in _TOOL_ALIASES.items()
	}
	metric_keys = list(DEFAULT_METRIC_KEYS)
	outcomes: list[ToolOutcome] = []

	metrics_tool = tool_map["sonarqube_search_metrics"]
	if metrics_tool is not None:
		metrics_outcome = _call_tool(
			client,
			"sonarqube_search_metrics",
			metrics_tool,
			_page_arguments(metrics_tool, MAX_METRICS),
		)
		outcomes.append(metrics_outcome)
		if metrics_outcome.status == "passed":
			metric_keys = _merge_metric_keys(
				metric_keys,
				_extract_metric_keys(metrics_outcome.data),
			)

	duplication_tool = tool_map["sonarqube_get_duplications"]
	outcomes.append(
		_collect_duplications(
			client,
			duplication_tool,
			delta,
			project_key,
		)
	)

	hotspot_tool = tool_map["sonarqube_search_security_hotspot"]
	hotspot_args = {}
	if hotspot_tool is not None:
		_set_project_argument(hotspot_args, hotspot_tool, project_key)
		_set_argument(
			hotspot_args,
			hotspot_tool,
			("status",),
			"TO_REVIEW",
			default="status",
		)
		hotspot_args.update(_page_arguments(hotspot_tool, 100))
	outcomes.append(
		_call_or_missing(
			client,
			"sonarqube_search_security_hotspot",
			hotspot_tool,
			hotspot_args,
		)
	)

	issues_tool = tool_map["sonarqube_search_sonar_issues_in_projects"]
	issues_args = {}
	if issues_tool is not None:
		_set_project_argument(
			issues_args,
			issues_tool,
			project_key,
			plural=True,
		)
		_set_argument(
			issues_args,
			issues_tool,
			("issueStatuses",),
			["OPEN"],
			default="issueStatuses",
		)
		issues_args.update(_page_arguments(issues_tool, 100))
	outcomes.append(
		_call_or_missing(
			client,
			"sonarqube_search_sonar_issues_in_projects",
			issues_tool,
			issues_args,
		)
	)

	measures_tool = tool_map["sonarqube_get_component_measures"]
	measures_args = {"metricKeys": metric_keys}
	if measures_tool is not None:
		_set_project_argument(measures_args, measures_tool, project_key)
		# A configured server-side project key removes projectKey from the
		# generated schema; the helper leaves it out in that case.
	measure_outcome = _call_or_missing(
		client,
		"sonarqube_get_component_measures",
		measures_tool,
		measures_args,
	)
	if measure_outcome.status == "passed" and isinstance(
		measure_outcome.data, dict
	):
		measure_data = dict(measure_outcome.data)
		measure_data["requestedMetrics"] = metric_keys
		measure_outcome = ToolOutcome(
			name=measure_outcome.name,
			tool_name=measure_outcome.tool_name,
			status=measure_outcome.status,
			description=measure_outcome.description,
			data=measure_data,
		)
	outcomes.append(measure_outcome)

	required = [
		outcome
		for outcome in outcomes
		if outcome.name != "sonarqube_search_metrics"
	]
	failed = [outcome for outcome in required if outcome.status == "failed"]
	if failed:
		status = "failed"
		reason = (
			"SonarQube MCP snapshot incomplete: "
			+ "; ".join(outcome.description for outcome in failed)
		)
	else:
		status = "passed"
		reason = (
			"SonarQube MCP snapshot complete: "
			+ "; ".join(outcome.description for outcome in required)
		)
	violation_count = _violation_count(outcomes)
	if violation_count:
		reason += f"; {violation_count} SonarQube violation(s) detected"
	return SonarQubeReportResult(
		status=status,
		reason=_safe_text(reason, 600),
		duration_s=perf_counter() - started,
		project_key=project_key,
		outcomes=tuple(outcomes),
		violation_count=violation_count,
	)


def _collect_duplications(
	client: Any,
	tool: dict[str, Any] | None,
	delta: Any,
	project_key: str,
) -> ToolOutcome:
	"""Query duplications for each changed file and aggregate the results."""
	if tool is None:
		return _missing("sonarqube_get_duplications")

	components = _changed_components(delta, project_key)
	if not components:
		return ToolOutcome(
			name="sonarqube_get_duplications",
			tool_name=_tool_name(tool),
			status="skipped",
			description="no changed file component was available",
		)

	entries: list[dict[str, Any]] = []
	for component in components[:MAX_DUPLICATION_FILES]:
		outcome = _call_tool(
			client,
			"sonarqube_get_duplications",
			tool,
			{"key": component},
		)
		entries.append(
			{
				"key": component,
				"status": outcome.status,
				"reason": outcome.description,
				"result": outcome.data,
			}
		)
	truncated = len(components) > MAX_DUPLICATION_FILES
	data = {"files": entries, "truncated": truncated}
	if any(entry["status"] != "passed" for entry in entries):
		status = "failed"
	else:
		status = "passed"
	description = _describe_duplications(data, status)
	return ToolOutcome(
		name="sonarqube_get_duplications",
		tool_name=_tool_name(tool),
		status=status,
		description=description,
		data=data,
	)


def _call_or_missing(
	client: Any,
	name: str,
	tool: dict[str, Any] | None,
	arguments: dict[str, Any],
) -> ToolOutcome:
	if tool is None:
		return _missing(name)
	return _call_tool(client, name, tool, arguments)


def _call_tool(
	client: Any,
	name: str,
	tool: dict[str, Any],
	arguments: dict[str, Any],
) -> ToolOutcome:
	"""Call one tool and normalize its result into a report-safe object."""
	try:
		data = client.call_tool_data(_tool_name(tool), arguments)
	except Exception as exc:
		return ToolOutcome(
			name=name,
			tool_name=_tool_name(tool),
			status="failed",
			description=_exception_reason(exc),
		)
	if not isinstance(data, dict):
		return ToolOutcome(
			name=name,
			tool_name=_tool_name(tool),
			status="failed",
			description="returned a non-object response",
			data=data,
		)
	return ToolOutcome(
		name=name,
		tool_name=_tool_name(tool),
		status="passed",
		description=_describe(name, data),
		data=data,
	)


def _missing(name: str) -> ToolOutcome:
	return ToolOutcome(
		name=name,
		tool_name=None,
		status="failed",
		description="required MCP tool is unavailable",
	)


def _with_report(
	result: SonarQubeReportResult,
	root: Path,
	source: str,
) -> SonarQubeReportResult:
	"""Persist watch reports without mutating repositories during pre-commit."""
	if source == "pre-commit":
		return result
	try:
		path = _write_report(root, result, source)
	except (OSError, UnicodeError, ValueError):
		path = None
	if path is None:
		return result
	return SonarQubeReportResult(
		status=result.status,
		reason=result.reason,
		duration_s=result.duration_s,
		project_key=result.project_key,
		report_path=path,
		outcomes=result.outcomes,
		violation_count=result.violation_count,
	)


def _write_report(
	root: Path,
	result: SonarQubeReportResult,
	source: str,
) -> Path | None:
	path = report_path(root)
	path.parent.mkdir(parents=True, exist_ok=True)
	content = _markdown(result, source)
	if len(content.encode("utf-8")) > MAX_REPORT_BYTES:
		content = content[: MAX_REPORT_BYTES - 80].rstrip()
		content += "\n\n_Report truncated to keep hook output bounded._\n"
	temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		temp.write_text(content, encoding="utf-8")
		os.replace(temp, path)
	except (OSError, UnicodeError):
		try:
			temp.unlink(missing_ok=True)
		except OSError:
			pass
		return None
	return path


def _markdown(result: SonarQubeReportResult, source: str) -> str:
	updated = datetime.now(timezone.utc).isoformat()
	lines = [
		"# SonarQube MCP living report",
		"",
		"> This file is refreshed in place by `quack watch` and the staged "
		"`quack check` hook.",
		"",
		f"- Updated (UTC): `{updated}`",
		f"- Trigger: `{_safe_text(source, 80)}`",
		f"- Project: `{_safe_text(result.project_key or 'unknown', 256)}`",
		f"- Status: **{result.status.upper()}**",
		f"- Snapshot duration: `{result.duration_s:.1f}s`",
		"",
		"## Outcome",
		"",
		_safe_text(result.reason, 1200),
		"",
		"## Requested MCP checks",
		"",
	]
	for outcome in result.outcomes:
		lines.extend(
			[
				f"### `{outcome.name}`",
				"",
				f"- Server tool: `{_safe_text(outcome.tool_name or 'unavailable', 160)}`",
				f"- Status: **{outcome.status.upper()}**",
				f"- Outcome: {_safe_text(outcome.description, 1200)}",
				"",
			]
		)
		if outcome.name == "sonarqube_get_duplications":
			lines.extend(_duplication_markdown(outcome.data))
		elif outcome.name == "sonarqube_search_security_hotspot":
			lines.extend(_issue_markdown(outcome.data, hotspot=True))
		elif outcome.name == "sonarqube_search_sonar_issues_in_projects":
			lines.extend(_issue_markdown(outcome.data, hotspot=False))
		elif outcome.name == "sonarqube_get_component_measures":
			lines.extend(_measure_markdown(outcome.data))
		elif outcome.name == "sonarqube_search_metrics":
			lines.extend(_metric_catalog_markdown(outcome.data))
	return "\n".join(lines).rstrip() + "\n"


def _duplication_markdown(data: Any) -> list[str]:
	if not isinstance(data, dict):
		return []
	lines = ["| Changed component | Duplicate groups | Duplicate blocks |", "|---|---:|---:|"]
	for entry in data.get("files", [])[:MAX_DETAIL_ROWS]:
		if not isinstance(entry, dict):
			continue
		key = _safe_text(entry.get("key"), 240)
		result = entry.get("result")
		groups, blocks = _duplication_counts(result)
		lines.append(f"| `{_md(key)}` | {groups} | {blocks} |")
	if data.get("truncated"):
		lines.append("| _additional files_ | _not shown_ | _not shown_ |")
	return lines + [""]


def _issue_markdown(data: Any, *, hotspot: bool) -> list[str]:
	if not isinstance(data, dict):
		return []
	key = "hotspots" if hotspot else "issues"
	items = data.get(key)
	if not isinstance(items, list) or not items:
		return ["No individual findings were returned.", ""]
	if hotspot:
		lines = [
			"| Key | Status | Probability/category | Component | Message |",
			"|---|---|---|---|---|",
		]
		for item in items[:MAX_DETAIL_ROWS]:
			if not isinstance(item, dict):
				continue
			lines.append(
				"| "
				+ " | ".join(
					[
						_md(_safe_text(item.get("key"), 80)),
						_md(_safe_text(item.get("status"), 40)),
						_md(
							_safe_text(
								item.get("vulnerabilityProbability")
								or item.get("securityCategory"),
								80,
							)
						),
						_md(_safe_text(item.get("component"), 180)),
						_md(_safe_text(item.get("message"), 300)),
					]
				)
				+ " |"
			)
	else:
		lines = [
			"| Key | Severity | Status | Component | Message |",
			"|---|---|---|---|---|",
		]
		for item in items[:MAX_DETAIL_ROWS]:
			if not isinstance(item, dict):
				continue
			lines.append(
				"| "
				+ " | ".join(
					[
						_md(_safe_text(item.get("key"), 80)),
						_md(_safe_text(item.get("severity"), 40)),
						_md(_safe_text(item.get("status"), 40)),
						_md(_safe_text(item.get("component"), 180)),
						_md(_safe_text(item.get("message"), 300)),
					]
				)
				+ " |"
			)
	if len(items) > MAX_DETAIL_ROWS:
		lines.append(f"| _and {len(items) - MAX_DETAIL_ROWS} more_ | | | | |")
	return lines + [""]


def _measure_markdown(data: Any) -> list[str]:
	measures = _measure_values(data)
	lines = ["| Metric | Value | Description |", "|---|---:|---|"]
	for item in measures:
		lines.append(
			f"| `{_md(item['metric'])}` | `{_md(item['value'])}` | "
			f"{_md(item['description'])} |"
		)
	return lines + [""]


def _metric_catalog_markdown(data: Any) -> list[str]:
	keys = _extract_metric_keys(data)
	if not keys:
		return ["No metric catalog entries were returned.", ""]
	return [
		"Metric catalog used for the component-measures request: "
		+ ", ".join(f"`{_md(key)}`" for key in keys),
		"",
	]


def _describe(name: str, data: dict[str, Any]) -> str:
	if name == "sonarqube_get_duplications":
		return _describe_duplications(
			{"files": [{"status": "passed", "result": data}]},
			"passed",
		)
	if name == "sonarqube_search_security_hotspot":
		total = _result_total(data, ("hotspots", "securityHotspots"))
		if total == 0:
			return "no security hotspots are awaiting review"
		return f"{total} security hotspot(s) are awaiting review"
	if name == "sonarqube_search_sonar_issues_in_projects":
		total = _result_total(data, ("issues",))
		if total == 0:
			return "no open SonarQube issues were found"
		return f"{total} open SonarQube issue(s) were found"
	if name == "sonarqube_get_component_measures":
		values = _measure_values(data)
		visible = {
			item["metric"]: item["value"]
			for item in values
			if item["metric"] in REQUIRED_METRIC_KEYS
		}
		required = ", ".join(
			f"{key}={visible.get(key, 'not returned')}"
			for key in REQUIRED_METRIC_KEYS
		)
		return f"captured {len(values)} component measure(s); {required}"
	if name == "sonarqube_search_metrics":
		return f"catalogued {len(_extract_metric_keys(data))} available metric(s)"
	return "response received"


def _describe_duplications(data: Any, status: str) -> str:
	groups = 0
	blocks = 0
	file_count = 0
	if isinstance(data, dict):
		for entry in data.get("files", []):
			if not isinstance(entry, dict):
				continue
			file_count += 1
			file_groups, file_blocks = _duplication_counts(entry.get("result"))
			groups += file_groups
			blocks += file_blocks
	if status != "passed":
		return f"duplication query failed for at least one of {file_count} file(s)"
	if groups == 0:
		return f"no duplicate blocks were returned for {file_count} changed file(s)"
	return (
		f"{groups} duplicate group(s) and {blocks} duplicate block(s) "
		f"found across {file_count} changed file(s)"
	)


def _duplication_counts(data: Any) -> tuple[int, int]:
	if not isinstance(data, dict):
		return 0, 0
	duplications = data.get("duplications")
	if not isinstance(duplications, list):
		return 0, 0
	blocks = 0
	for duplication in duplications:
		if isinstance(duplication, dict) and isinstance(
			duplication.get("blocks"), list
		):
			blocks += len(duplication["blocks"])
	return len(duplications), blocks


def _result_total(data: dict[str, Any], item_keys: tuple[str, ...]) -> int:
	paging = data.get("paging")
	if isinstance(paging, dict):
		total = paging.get("total")
		if type(total) is int and total >= 0:
			return total
	for key in item_keys:
		items = data.get(key)
		if isinstance(items, list):
			return len(items)
	return 0


def _violation_count(outcomes: list[ToolOutcome]) -> int:
	"""Count open SonarQube issues and security hotspots in a snapshot."""
	count = 0
	for outcome in outcomes:
		if outcome.status != "passed" or not isinstance(outcome.data, dict):
			continue
		if outcome.name == "sonarqube_search_sonar_issues_in_projects":
			count += _result_total(outcome.data, ("issues",))
		elif outcome.name == "sonarqube_search_security_hotspot":
			count += _result_total(
				outcome.data,
				("hotspots", "securityHotspots"),
			)
	return count


def _measure_values(data: Any) -> list[dict[str, str]]:
	if not isinstance(data, dict):
		return []
	component = data.get("component")
	raw_measures = (
		component.get("measures")
		if isinstance(component, dict)
		else data.get("measures")
	)
	if not isinstance(raw_measures, list):
		raw_measures = data.get("measures")
	if not isinstance(raw_measures, list):
		raw_measures = []

	metadata: dict[str, dict[str, Any]] = {}
	raw_metrics = data.get("metrics")
	if isinstance(raw_metrics, list):
		for item in raw_metrics:
			if isinstance(item, dict):
				key = item.get("key")
				if isinstance(key, str):
					metadata[key] = item

	values: dict[str, dict[str, str]] = {}
	for item in raw_measures:
		if not isinstance(item, dict):
			continue
		metric = item.get("metric") or item.get("key")
		if not isinstance(metric, str) or not _METRIC_KEY_RE.fullmatch(metric):
			continue
		value = item.get("value")
		if value is None:
			period = item.get("period")
			if isinstance(period, dict):
				value = period.get("value")
		if value is None:
			value = "not returned"
		best = item.get("bestValue")
		value_text = _safe_text(value, 120)
		if best is True:
			value_text += " (best)"
		elif best is False:
			value_text += " (not best)"
		values[metric] = {
			"metric": metric,
			"value": value_text,
			"description": _metric_description(metric, metadata.get(metric)),
		}

	for metric, item in metadata.items():
		if metric not in values and _METRIC_KEY_RE.fullmatch(metric):
			values[metric] = {
				"metric": metric,
				"value": "not returned",
				"description": _metric_description(metric, item),
			}
	raw_requested = data.get("requestedMetrics")
	if isinstance(raw_requested, list):
		for metric in raw_requested:
			if (
				isinstance(metric, str)
				and _METRIC_KEY_RE.fullmatch(metric)
				and metric not in values
			):
				values[metric] = {
					"metric": metric,
					"value": "not returned",
					"description": _metric_description(metric, None),
				}
	for metric in REQUIRED_METRIC_KEYS:
		values.setdefault(
			metric,
			{
				"metric": metric,
				"value": "not returned",
				"description": _metric_description(metric, None),
			},
		)
	return [values[key] for key in sorted(values)]


def _metric_description(
	metric: str,
	metadata: dict[str, Any] | None,
) -> str:
	if metric in METRIC_DESCRIPTIONS:
		return METRIC_DESCRIPTIONS[metric]
	if metadata is not None:
		description = metadata.get("description") or metadata.get("name")
		if isinstance(description, str) and description.strip():
			return _safe_text(description, 300)
	return "SonarQube component measure returned for the project."


def _extract_metric_keys(data: Any) -> list[str]:
	if not isinstance(data, dict):
		return []
	raw_metrics = data.get("metrics")
	if not isinstance(raw_metrics, list):
		return []
	keys: list[str] = []
	for item in raw_metrics:
		if not isinstance(item, dict):
			continue
		key = item.get("key")
		if (
			isinstance(key, str)
			and _METRIC_KEY_RE.fullmatch(key)
			and key not in keys
		):
			keys.append(key)
	return keys[:MAX_METRICS]


def _merge_metric_keys(*groups: list[str]) -> list[str]:
	keys: list[str] = []
	for group in groups:
		for key in group:
			if (
				isinstance(key, str)
				and _METRIC_KEY_RE.fullmatch(key)
				and key not in keys
			):
				keys.append(key)
	return keys[:MAX_METRICS]


def _changed_components(delta: Any, project_key: str) -> list[str]:
	components: list[str] = []
	for item in getattr(delta, "files", []) or []:
		path = str(getattr(item, "path", "")).replace("\\", "/").lstrip("/")
		if (
			not path
			or is_report_file(path)
			or getattr(item, "binary", False)
			or getattr(item, "status", "") == "D"
		):
			continue
		component = f"{project_key}:{path}"
		if component not in components:
			components.append(component)
	return components


def _find_tool(
	tools: list[dict[str, Any]],
	aliases: tuple[str, ...],
) -> dict[str, Any] | None:
	for tool in tools:
		name = _tool_name(tool)
		if not name:
			continue
		lowered = name.casefold()
		for alias in aliases:
			variant = alias.casefold()
			short_variant = variant.removeprefix("sonarqube_")
			if lowered in {variant, short_variant} or lowered.endswith(
				"_" + short_variant
			):
				return tool
	return None


def _tool_name(tool: dict[str, Any]) -> str:
	function = tool.get("function")
	if isinstance(function, dict):
		name = function.get("name")
		if isinstance(name, str):
			return name
	name = tool.get("name")
	return name if isinstance(name, str) else ""


def _properties(tool: dict[str, Any]) -> dict[str, Any] | None:
	function = tool.get("function")
	parameters = function.get("parameters") if isinstance(function, dict) else None
	if not isinstance(parameters, dict):
		return None
	properties = parameters.get("properties")
	return properties if isinstance(properties, dict) else None


def _set_argument(
	arguments: dict[str, Any],
	tool: dict[str, Any],
	candidates: tuple[str, ...],
	value: Any,
	*,
	default: str,
) -> None:
	properties = _properties(tool)
	if properties is None:
		arguments[default.split("=", 1)[-1]] = value
		return
	for candidate in candidates:
		if candidate in properties:
			arguments[candidate] = value
			return
	arguments[default.split("=", 1)[-1]] = value


def _set_project_argument(
	arguments: dict[str, Any],
	tool: dict[str, Any],
	project_key: str,
	*,
	plural: bool = False,
) -> None:
	properties = _properties(tool)
	candidates = (
		("projects", "projectKeys", "projectKey")
		if plural
		else ("projectKey", "component")
	)
	if properties is None:
		arguments[candidates[0]] = [project_key] if plural else project_key
		return
	for candidate in candidates:
		if candidate in properties:
			arguments[candidate] = [project_key] if plural and candidate != "projectKey" else project_key
			return


def _page_arguments(tool: dict[str, Any], page_size: int) -> dict[str, int]:
	properties = _properties(tool)
	if properties is None:
		return {"ps": page_size}
	result: dict[str, int] = {}
	if "ps" in properties:
		result["ps"] = page_size
	elif "pageSize" in properties:
		result["pageSize"] = page_size
	if "p" in properties:
		result["p"] = 1
	elif "pageIndex" in properties:
		result["pageIndex"] = 1
	return result


def _project_key(client: Any) -> str | None:
	value = getattr(client, "project_key", None)
	if isinstance(value, str) and _PROJECT_KEY_RE.fullmatch(value.strip()):
		return value.strip()
	for name in ("SONARQUBE_PROJECT_KEY", "QUACK_SONAR_PROJECT_KEY"):
		value = os.environ.get(name, "").strip()
		if _PROJECT_KEY_RE.fullmatch(value):
			return value
	return None


def _result(
	status: str,
	reason: str,
	started: float,
) -> SonarQubeReportResult:
	return SonarQubeReportResult(
		status=status,
		reason=_safe_text(reason, 600),
		duration_s=perf_counter() - started,
	)


def _exception_reason(exc: Exception) -> str:
	return f"{type(exc).__name__}: {_safe_text(str(exc), 240)}"


def _safe_text(value: Any, limit: int = 240) -> str:
	if value is None:
		return ""
	if isinstance(value, bool):
		text = str(value).lower()
	elif isinstance(value, (int, float)) and not isinstance(value, bool):
		text = str(value)
		if isinstance(value, float) and not math.isfinite(value):
			text = "unavailable"
	else:
		text = str(value)
	text = " ".join(text.split())
	text = _TOKEN_RE.sub("<redacted>", text)
	if len(text) > limit:
		return text[: limit - 3].rstrip() + "..."
	return text


def _md(value: Any) -> str:
	return _safe_text(value, 500).replace("|", "\\|").replace("`", "'")
