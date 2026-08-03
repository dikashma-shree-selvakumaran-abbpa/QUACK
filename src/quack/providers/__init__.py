"""LLM transport providers.

Each provider module exposes the same two functions with identical
contracts to :mod:`quack.llmio`::

	complete(messages, model, timeout_s=6.0) -> str
	chat(messages, model, tools=None) -> dict

Every provider MUST normalise all failure modes to
:class:`quack.llmio.LLMUnavailable`. No provider-specific exception may
escape a provider. :mod:`quack.llmio` selects and delegates to one of
these providers.
"""
