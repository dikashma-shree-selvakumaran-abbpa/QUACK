"""Lenient parsing of model-produced JSON.

Chat models frequently wrap their JSON in markdown code fences (```json ...
```` or bare ``` ... ```) or precede it with prose like "Here's my analysis:".
:func:`loads` hardens ``json.loads`` against exactly those failure modes:

1. Try to parse the text as-is.
2. Strip a leading ```json / ``` fence and a trailing ``` and retry.
3. Fall back to extracting the first balanced ``{...}`` object (respecting
   string literals and escapes) and parse just that.

Returns the parsed object, or ``None`` on any failure. Never raises.
"""

from __future__ import annotations

import json


def loads(raw: object) -> object | None:
	"""Parse ``raw`` as JSON, tolerating code fences and surrounding prose."""
	if not isinstance(raw, str):
		return None

	candidates = [raw, _strip_fences(raw)]
	for candidate in candidates:
		parsed = _try_loads(candidate)
		if parsed is not None:
			return parsed

	extracted = _first_json_object(raw)
	if extracted is not None:
		return _try_loads(extracted)
	return None


def _try_loads(text: str) -> object | None:
	try:
		return json.loads(text)
	except (ValueError, TypeError):
		return None


def _strip_fences(text: str) -> str:
	"""Remove a leading ```json/``` fence and trailing ``` if present."""
	stripped = text.strip()
	if not stripped.startswith("```"):
		return stripped
	# Drop the opening fence line (```json, ```JSON, or bare ```).
	newline = stripped.find("\n")
	if newline == -1:
		return stripped
	stripped = stripped[newline + 1 :]
	if stripped.rstrip().endswith("```"):
		stripped = stripped.rstrip()[: -len("```")]
	return stripped.strip()


def _first_json_object(text: str) -> str | None:
	"""Return the first balanced ``{...}`` substring, respecting strings."""
	start = text.find("{")
	if start == -1:
		return None

	depth = 0
	in_string = False
	escaped = False
	for index in range(start, len(text)):
		char = text[index]
		if in_string:
			if escaped:
				escaped = False
			elif char == "\\":
				escaped = True
			elif char == '"':
				in_string = False
			continue
		if char == '"':
			in_string = True
		elif char == "{":
			depth += 1
		elif char == "}":
			depth -= 1
			if depth == 0:
				return text[start : index + 1]
	return None
