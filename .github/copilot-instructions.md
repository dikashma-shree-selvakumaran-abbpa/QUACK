# Copilot Instructions

## Project Guidelines
- For ABB projects, instructions.md is typically the first source of project guidance and should be read early to improve navigation and targeted advice.
- Treat Tier 2 risk labels as non-deterministic/model-dependent advisory signals; explicitly document this as an open item and prioritize calibration metrics and a signed rubric/prompt review before treating HIGH as validated risk.
- For the "quack" project, implement a Python 3.11+ pre-commit hook with offline deterministic Tier 1 and fail-open, latency-bounded, schema-validated GitHub Models Tier 2; ensure strict adapter boundaries (gitio.py, llmio.py, runio.py), secure redaction/tooling, JSONL metrics, quiet Rich-only rendering, and limit dependencies to stdlib, click, pyyaml, httpx, rich.

## Reporting Guidelines
- Display hook durations in quack report titles with one decimal place while keeping the underlying duration value as a float.
- Enforce privacy at the metrics.log boundary with a strict event-key allowlist, sanitization of failure/reason text, type validation, and fail-open behavior so malformed metrics never reach callers as exceptions.

## SDK Integration Guidelines
- Suppress SDK logger output and child-log records during both model discovery and completion calls; restore logging state afterward.
- Surface only a one-line normalized/truncated reason with no traceback.
- For the Copilot SDK provider, provide actionable auth guidance for list_models and completion failures; tests should assert the actionable mapping rather than legacy generic wording.