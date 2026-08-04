"""Thin HTTP adapter for the GitHub Models chat/completions endpoint.

This module contains NO business logic. It only knows how to POST a list of
messages and return the raw text content of the first choice. Every failure
mode is normalised to a single :class:`LLMUnavailable` exception so that
callers can implement fail-open behaviour without catching a zoo of httpx
errors.

The token is read from the ``GITHUB_TOKEN`` environment variable at call time.
It is never stored on an object and never logged.
"""

from __future__ import annotations

import os

import httpx

from ..llmio import LLMUnavailable

DEFAULT_BASE_URL = "https://models.github.ai/inference"

# Realistic minimum timeout for this transport. A GitHub Models HTTP call is
# fast, so a short bound is appropriate. This is a property of the TRANSPORT,
# not of any caller/tier.
DEFAULT_TIMEOUT_S = 6.0

# Default model identifiers for this transport, split by USE. Model ids are
# transport-specific (GitHub Models uses "owner/name"), so the defaults belong
# to the PROVIDER. They differ by capability requirement: Tier 2's single-shot
# review tolerates a cheap model, but the agent's multi-step tool-using
# investigation loops and fails on a weak model (gpt-4o-mini), so it needs a
# stronger one.
DEFAULT_COMPLETION_MODEL = "openai/gpt-4o-mini"
DEFAULT_AGENT_MODEL = "openai/gpt-4.1"


def check_availability() -> str | None:
	"""Return a readable reason this provider cannot run, or None if it can.

	This provider authenticates with a GitHub token read from the
	``GITHUB_TOKEN`` environment variable, so it is unavailable when that
	variable is unset. The credential requirement lives here, next to the
	code that actually reads it, not in the CLI.
	"""
	if not os.environ.get("GITHUB_TOKEN"):
		return "no GITHUB_TOKEN"
	return None


def complete(
	messages: list[dict],
	model: str,
	timeout_s: float = 6.0,
	base_url: str = DEFAULT_BASE_URL,
) -> str:
	"""POST ``messages`` to the chat/completions endpoint and return the text.

	Returns the raw text content of the first choice's message. Raises
	:class:`LLMUnavailable` when no token is set, on any httpx exception, on a
	non-2xx response, or on timeout. The total request time is bounded by
	``timeout_s``.
	"""
	token = os.environ.get("GITHUB_TOKEN")
	if not token:
		raise LLMUnavailable("no GITHUB_TOKEN")

	url = f"{base_url.rstrip('/')}/chat/completions"
	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
	}
	body = {
		"model": model,
		"messages": messages,
		"response_format": {"type": "json_object"},
		"max_tokens": 800,
		"temperature": 0.1,
	}

	try:
		with httpx.Client(timeout=timeout_s) as client:
			response = client.post(url, headers=headers, json=body)
	except httpx.TimeoutException:
		raise LLMUnavailable("request timed out")
	except httpx.HTTPError as exc:
		raise LLMUnavailable(f"request failed: {type(exc).__name__}")

	if not (200 <= response.status_code < 300):
		raise LLMUnavailable(f"HTTP {response.status_code}")

	try:
		payload = response.json()
		return payload["choices"][0]["message"]["content"]
	except (ValueError, KeyError, IndexError, TypeError):
		raise LLMUnavailable("malformed response envelope")


def chat(
	messages: list[dict],
	model: str,
	tools: list[dict] | None = None,
	timeout_s: float = 180.0,
	base_url: str = DEFAULT_BASE_URL,
) -> dict:
	"""POST ``messages`` and return the first choice's full message object.

	Unlike :func:`complete`, this returns the whole assistant message dict so
	callers can inspect ``tool_calls`` for OpenAI tool-calling loops. When
	``tools`` is provided they are advertised with ``tool_choice="auto"``; when
	it is ``None`` a JSON object response format is requested so the model
	emits a final structured answer.

	Raises :class:`LLMUnavailable` on missing token, any httpx error, a non-2xx
	response, or a malformed envelope. The total request time is bounded by
	``timeout_s``.
	"""
	token = os.environ.get("GITHUB_TOKEN")
	if not token:
		raise LLMUnavailable("no GITHUB_TOKEN")

	url = f"{base_url.rstrip('/')}/chat/completions"
	headers = {
		"Authorization": f"Bearer {token}",
		"Content-Type": "application/json",
	}
	body: dict = {
		"model": model,
		"messages": messages,
		"max_tokens": 1200,
		"temperature": 0.1,
	}
	if tools:
		body["tools"] = tools
		body["tool_choice"] = "auto"
	else:
		body["response_format"] = {"type": "json_object"}

	try:
		with httpx.Client(timeout=timeout_s) as client:
			response = client.post(url, headers=headers, json=body)
	except httpx.TimeoutException:
		raise LLMUnavailable("request timed out")
	except httpx.HTTPError as exc:
		raise LLMUnavailable(f"request failed: {type(exc).__name__}")

	if not (200 <= response.status_code < 300):
		raise LLMUnavailable(f"HTTP {response.status_code}")

	try:
		payload = response.json()
		return payload["choices"][0]["message"]
	except (ValueError, KeyError, IndexError, TypeError):
		raise LLMUnavailable("malformed response envelope")
