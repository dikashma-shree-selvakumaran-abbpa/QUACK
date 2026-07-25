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

DEFAULT_BASE_URL = "https://models.github.ai/inference"


class LLMUnavailable(Exception):
	"""Raised when the model could not be reached or returned an error.

	The ``reason`` is a short, human-readable string safe to surface to the
	user (e.g. ``"no GITHUB_TOKEN"``). It never contains the token or the
	response body.
	"""

	def __init__(self, reason: str) -> None:
		super().__init__(reason)
		self.reason = reason


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
