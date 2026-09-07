"""Unit tests for quack.agent tools and the agent CLI path (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quack import agent


# ---------------------------------------------------------------------------
# Containment.
# ---------------------------------------------------------------------------


def test_read_file_rejects_path_escape(tmp_path: Path) -> None:
	result = agent._read_file(tmp_path, "../../../etc/passwd")
	assert result.startswith("error:")
	assert "outside the repository" in result


def test_list_dir_rejects_path_escape(tmp_path: Path) -> None:
	result = agent._list_dir(tmp_path, "../../../etc")
	assert result.startswith("error:")
	assert "outside the repository" in result


def test_read_file_rejects_absolute_outside(tmp_path: Path) -> None:
	assert agent._read_file(tmp_path, "/etc/passwd").startswith("error:")


# ---------------------------------------------------------------------------
# run_tests filter injection.
# ---------------------------------------------------------------------------


def test_run_tests_rejects_shell_metacharacters(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	def boom(*args, **kwargs):
		raise AssertionError("subprocess must not be reached")

	monkeypatch.setattr(agent.runio, "run_dotnet_test", boom)
	(tmp_path / "Proj.csproj").write_text("<Project/>", encoding="utf-8")

	result = agent._run_tests(tmp_path, 'Proj.csproj --filter "; rm -rf /"')

	assert result.startswith("error:")
	assert "filter rejected" in result


def test_run_tests_accepts_valid_filter(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	captured: dict = {}

	def fake_dotnet(project, test_filter, timeout_s=180):
		captured["project"] = project
		captured["filter"] = test_filter
		return (0, "Passed!")

	monkeypatch.setattr(agent.runio, "run_dotnet_test", fake_dotnet)
	(tmp_path / "Proj.csproj").write_text("<Project/>", encoding="utf-8")

	result = agent._run_tests(
		tmp_path, "Proj.csproj --filter FullyQualifiedName~RectTransformTests"
	)

	assert "exit_code=0" in result
	assert captured["filter"] == "FullyQualifiedName~RectTransformTests"


@pytest.mark.parametrize(
	"cmd",
	[
		"npm test",
		"npm run test",
		"npx jest",
		"npx vitest",
		"yarn test",
		"yarn jest",
		"vitest",
		"jest",
	],
)
def test_run_tests_accepts_js_test_prefixes(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
	def fake_js_test(args, cwd=None, timeout_s=180):
		return (0, "PASS")

	monkeypatch.setattr(agent.runio, "run_js_test", fake_js_test)

	result = agent._run_tests(tmp_path, cmd)
	assert not result.startswith("error:")
	assert "exit_code=0" in result


def test_run_tests_rejects_unapproved_command(tmp_path: Path) -> None:
	result = agent._run_tests(tmp_path, "node_modules/.bin/evil")
	assert result.startswith("error:")
	assert "unrecognized target" in result


# ---------------------------------------------------------------------------
# Security (Rule #4): a staged secret must never reach the model. Tier 1 now
# blocks the push before any call, so the diff is never transmitted at all.
# ---------------------------------------------------------------------------


def test_agent_never_sends_a_secret_to_the_model(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli
	from quack.delta import StagedDelta, StagedFile

	secret = "AKIA" + "A" * 16
	hunk = f'@@ -0,0 +1,1 @@\n+AWS_KEY = "{secret}"'
	delta = StagedDelta(
		files=[
			StagedFile(
				path="src/config.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)

	captured: dict = {}

	def fake_review(*args, **kwargs):
		# Record the very first time anything is handed to the model layer.
		captured.setdefault("args", args)
		return (None, "test stub")

	def fake_agent_run(diff, root, model):
		captured.setdefault("args", diff)
		return agent.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda *, root=None: delta)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.tier2, "review_with_reason", fake_review)
	monkeypatch.setattr("quack.cli.agent_mod.run", fake_agent_run)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 1
	assert "args" not in captured


# ---------------------------------------------------------------------------
# Tier 2 pre-push wiring: the agent path runs Tier 2 as a fast, fail-open
# single completion BEFORE the slow agent loop, reusing the Tier 1 findings
# it already computed for redaction.
# ---------------------------------------------------------------------------


def _plain_delta(*, root=None):
	from quack.delta import StagedDelta, StagedFile

	hunk = "@@ -0,0 +1,1 @@\n+x = 1"
	return StagedDelta(
		files=[
			StagedFile(
				path="src/thing.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)


def _stub_agent_loop(monkeypatch):
	"""Make the agent loop a no-op success so tests focus on the Tier 2 pre-pass."""
	from quack import agent as agent_mod, cli

	def fake_run_agent(diff, root, model, timeout_s=None):
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)


def test_agent_renders_tier2_verdict_when_review_returns_result(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli, tier2

	verdict = tier2.ReviewResult(
		risk="high",
		reasons=["src/thing.py: touches money path"],
		one_liner="Risky change to payment logic",
	)

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (verdict, None)
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "Risky change to payment logic" in result.output
	assert "HIGH" in result.output


def test_agent_renders_nothing_and_still_runs_when_review_returns_none(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run_agent(diff, root, model, timeout_s=None):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "model unavailable")
	)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True
	# No verdict header rendered for a None review, but the failure is made
	# visible at pre-push rather than being silent, using the real reason.
	assert "risk:" not in result.output
	assert "AI review unavailable (model unavailable)" in result.output


def test_agent_survives_tier2_exception_without_changing_exit_code(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def boom(*a, **k):
		raise RuntimeError("tier2 exploded")

	def fake_run_agent(diff, root, model, timeout_s=None):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.tier2, "review_with_reason", boom)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True


def test_agent_loads_instructions_with_repo_root(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	seen = {}

	def fake_load(repo_root, max_chars=4000):
		seen["repo_root"] = repo_root
		return None

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: "/tmp/myrepo")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.instructions, "load", fake_load)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert seen["repo_root"] == Path("/tmp/myrepo")


def test_agent_computes_tier1_findings_once(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	calls = {"tier1": 0}
	captured = {}

	real_run = cli.tier1_run

	def counting_run(delta, config):
		calls["tier1"] += 1
		return real_run(delta, config)

	def capture_review(delta, findings, plan, **kwargs):
		captured["findings"] = findings
		return None, "x"

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli, "tier1_run", counting_run)
	monkeypatch.setattr(cli.tier2, "review_with_reason", capture_review)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	# Tier 1 is computed exactly once and the same findings feed Tier 2.
	assert calls["tier1"] == 1
	assert "findings" in captured


def test_agent_passes_provider_timeout_to_tier2(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	seen = {}

	def capture_review(delta, findings, plan, **kwargs):
		seen["timeout_s"] = kwargs.get("timeout_s")
		return None, "x"

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	# The slow-transport timeout must be forwarded, not tier2's 6.0 default.
	monkeypatch.setattr(cli.llmio, "default_timeout", lambda: 60.0)
	monkeypatch.setattr(cli.tier2, "review_with_reason", capture_review)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert seen["timeout_s"] == 60.0


def test_agent_renders_provider_reason_when_tier2_unavailable(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	# The agent's own startup guard passes (call 1 -> None), but Tier 2 then
	# fails and consults availability_error again (call 2 -> reason), which is
	# what should surface as the dim line.
	calls = {"n": 0}

	def availability():
		calls["n"] += 1
		return None if calls["n"] == 1 else "no GITHUB_TOKEN"

	monkeypatch.setattr(cli.llmio, "availability_error", availability)
	# review returns no specific reason, so the CLI falls back to the
	# provider-level availability reason.
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, None)
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "AI review unavailable (no GITHUB_TOKEN)" in result.output


def test_agent_renders_dim_reason_when_tier2_raises(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def boom(*a, **k):
		raise RuntimeError("tier2 exploded")

	def fake_run_agent(diff, root, model, timeout_s=None):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.tier2, "review_with_reason", boom)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True
	assert "AI review unavailable" in result.output



# ---------------------------------------------------------------------------
# Provider-aware availability: the agent must ask the selected provider
# whether it can run, not hardcode a GITHUB_TOKEN check. Under copilot_sdk
# (which authenticates via the Copilot CLI login) it must run even with no
# GITHUB_TOKEN; when the provider is unavailable it fails open with the
# provider's readable reason.
# ---------------------------------------------------------------------------


def test_agent_runs_under_copilot_sdk_without_github_token(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run_agent(diff, root, model, timeout_s=None):
		from quack import agent as agent_mod

		ran["agent"] = True
		return agent_mod.AgentResult(summary="ok")

	# copilot_sdk provider reports available regardless of GITHUB_TOKEN.
	monkeypatch.delenv("GITHUB_TOKEN", raising=False)
	monkeypatch.setattr(cli.llmio, "availability_error", lambda: None)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is True


def test_agent_fails_open_with_provider_reason(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	ran = {"agent": False}

	def fake_run_agent(diff, root, model, timeout_s=None):
		ran["agent"] = True
		raise AssertionError("agent must not run when provider is unavailable")

	monkeypatch.setattr(cli.llmio, "availability_error", lambda: "no GITHUB_TOKEN")
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert ran["agent"] is False
	assert "quack agent: no GITHUB_TOKEN" in result.output


# ---------------------------------------------------------------------------
# Model resolution: the default model is transport-specific AND use-specific,
# so it comes from the selected provider split by kind. The agent loop uses the
# provider's AGENT default; Tier 2's single-shot review uses the COMPLETION
# default. --model and QUACK_MODEL still win over both.
# ---------------------------------------------------------------------------


def test_resolve_agent_model_uses_provider_agent_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(
		cli.llmio,
		"default_model",
		lambda kind="completion": {
			"agent": "claude-sonnet-4.5",
			"completion": "claude-haiku-4.5",
		}[kind],
	)
	assert cli._resolve_agent_model(None) == "claude-sonnet-4.5"


def test_resolve_completion_model_uses_provider_completion_default(
	monkeypatch,
) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(
		cli.llmio,
		"default_model",
		lambda kind="completion": {
			"agent": "claude-sonnet-4.5",
			"completion": "claude-haiku-4.5",
		}[kind],
	)
	assert cli._resolve_completion_model(None) == "claude-haiku-4.5"


def test_resolve_agent_model_cli_option_wins(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "from-env")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_agent_model("explicit/model") == "explicit/model"


def test_resolve_completion_model_cli_option_wins(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "from-env")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_completion_model("explicit/model") == "explicit/model"


def test_resolve_agent_model_env_beats_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "openai/gpt-4o-mini")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_agent_model(None) == "openai/gpt-4o-mini"


def test_resolve_completion_model_env_beats_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.setenv("QUACK_MODEL", "openai/gpt-4o-mini")
	monkeypatch.setattr(
		cli.llmio, "default_model", lambda kind="completion": "provider-default"
	)
	assert cli._resolve_completion_model(None) == "openai/gpt-4o-mini"


def test_resolve_agent_model_falls_back_when_no_provider_default(monkeypatch) -> None:
	from quack import cli

	monkeypatch.delenv("QUACK_MODEL", raising=False)
	monkeypatch.setattr(cli.llmio, "default_model", lambda kind="completion": None)
	assert cli._resolve_agent_model(None) is None


# ---------------------------------------------------------------------------
# Target selection (pre-push vs manual): quack agent prefers staged changes,
# else falls back to the unpushed range @{u}..HEAD, else exits cleanly.
# ---------------------------------------------------------------------------


def _empty_delta(*, root=None):
	from quack.delta import StagedDelta

	return StagedDelta(files=[], raw_diff="")


def test_agent_prefers_staged_when_staged_exists(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	def no_range(*a, **k):
		raise AssertionError("range_delta must not run when staged exists")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(cli.gitio, "range_delta", no_range)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "analyzing staged changes" in result.output


def test_agent_falls_back_to_unpushed_range(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	captured: dict = {}

	def fake_range_delta(base, head="HEAD", *, root=None):
		captured["base"] = base
		return _plain_delta()

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _empty_delta)
	monkeypatch.setattr(cli.gitio, "upstream_ref", lambda *, root=None: "origin/main")
	monkeypatch.setattr(cli.gitio, "range_delta", fake_range_delta)
	monkeypatch.setattr(cli.gitio, "range_commit_count", lambda *a, **k: 3)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert captured["base"] == "origin/main"
	assert "analyzing 3 unpushed commit(s)" in result.output


def test_agent_exits_cleanly_when_nothing_staged_or_unpushed(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli

	def no_agent(*a, **k):
		raise AssertionError("agent must not run when there is nothing to analyze")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _empty_delta)
	monkeypatch.setattr(cli.gitio, "upstream_ref", lambda *, root=None: None)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr("quack.cli.agent_mod.run", no_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "nothing to analyze" in result.output


def test_agent_range_path_blocks_a_secret_before_transmission(monkeypatch) -> None:
	from click.testing import CliRunner

	from quack import cli
	from quack.delta import StagedDelta, StagedFile

	secret = "AKIA" + "A" * 16
	hunk = f'@@ -0,0 +1,1 @@\n+AWS_KEY = "{secret}"'
	range_delta = StagedDelta(
		files=[
			StagedFile(
				path="src/config.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)

	captured: dict = {}

	def fake_review(*args, **kwargs):
		captured.setdefault("args", args)
		return (None, "test stub")

	def fake_run_agent(diff, root, model, timeout_s=None):
		captured.setdefault("args", diff)
		return agent.AgentResult(summary="ok")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _empty_delta)
	monkeypatch.setattr(cli.gitio, "upstream_ref", lambda *, root=None: "origin/main")
	monkeypatch.setattr(cli.gitio, "range_delta", lambda *a, **k: range_delta)
	monkeypatch.setattr(cli.gitio, "range_commit_count", lambda *a, **k: 1)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.tier2, "review_with_reason", fake_review)
	monkeypatch.setattr("quack.providers.copilot_sdk.run_agent", fake_run_agent)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 1
	assert "args" not in captured


def test_agent_blocks_push_when_tier1_finds_a_secret(monkeypatch) -> None:
	# Tier 1 is deterministic, so it gates. The AI verdict stays advisory.
	from click.testing import CliRunner

	from quack import cli
	from quack.delta import StagedDelta, StagedFile

	secret = "AKIA" + "A" * 16
	hunk = f'@@ -0,0 +1,1 @@\n+AWS_KEY = "{secret}"'
	delta = StagedDelta(
		files=[
			StagedFile(
				path="src/config.py",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)

	def no_model(*a, **k):
		raise AssertionError("must not call the model on a blocked push")

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda *, root=None: delta)
	monkeypatch.setattr(cli.llmio, "list_models", lambda: ["claude-sonnet-5"])
	monkeypatch.setattr(cli.tier2, "review_with_reason", no_model)
	monkeypatch.setattr("quack.cli.agent_mod.run", no_model)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 1


def test_agent_still_exits_zero_when_tier1_is_clean(monkeypatch) -> None:
	# The AI verdict must not change hook success -- only Tier 1 does.
	from click.testing import CliRunner

	from quack import cli

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", _plain_delta)
	monkeypatch.setattr(
		cli.tier2, "review_with_reason", lambda *a, **k: (None, "x")
	)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0


def test_agent_does_not_block_on_warn_level_findings(monkeypatch) -> None:
	# Only error-level Tier 1 checks gate. debug_code and the other warn types
	# must still reach the model, since they are advice rather than facts about
	# secrets leaving the machine.
	from click.testing import CliRunner

	from quack import cli
	from quack.delta import StagedDelta, StagedFile

	hunk = "@@ -0,0 +1,1 @@\n+console.log('debugging')"
	delta = StagedDelta(
		files=[
			StagedFile(
				path="src/thing.js",
				status="M",
				added=1,
				removed=0,
				hunks=[hunk],
			)
		],
		raw_diff=hunk,
	)

	seen = {}

	def capture_review(delta_arg, findings, plan, **kwargs):
		seen["findings"] = [f.check for f in findings]
		return None, "x"

	monkeypatch.setenv("GITHUB_TOKEN", "t")
	monkeypatch.setattr(cli.gitio, "repo_root", lambda *, root=None: ".")
	monkeypatch.setattr(cli.gitio, "staged_delta", lambda *, root=None: delta)
	monkeypatch.setattr(cli.tier2, "review_with_reason", capture_review)
	_stub_agent_loop(monkeypatch)

	result = CliRunner().invoke(cli.main, ["agent"])

	assert result.exit_code == 0
	assert "debug_code" in seen["findings"]


# ---------------------------------------------------------------------------
# _reconcile exit-code handling.
# ---------------------------------------------------------------------------


def test_reconcile_ignores_no_tests_collected() -> None:
	# Exit 5 means pytest collected nothing, not that a test failed.
	result = agent.AgentResult(summary="ok")
	output = f"exit_code={agent.NO_TESTS_COLLECTED_EXIT}\nno tests ran in 0.01s"

	reconciled = agent._reconcile(result, [output])

	assert reconciled.failures == []
	assert reconciled.summary == "ok"


def test_reconcile_notes_unnamed_failure_without_fabricating_one() -> None:
	result = agent.AgentResult(summary="ok")
	output = "exit_code=1\nERROR: file or directory not found: tests/missing.py"

	reconciled = agent._reconcile(result, [output])

	assert reconciled.failures == []
	assert "[verified]" in reconciled.summary
	assert "no individual test name could be identified" in reconciled.summary
	assert reconciled.summary.endswith("ok")


def test_reconcile_still_records_named_failures() -> None:
	result = agent.AgentResult(summary="ok")
	output = "exit_code=1\nFAILED tests/test_x.py::test_y - AssertionError"

	reconciled = agent._reconcile(result, [output])

	assert [f["test"] for f in reconciled.failures] == ["tests/test_x.py::test_y"]
	assert "[verified]" in reconciled.summary


# ---------------------------------------------------------------------------
# _timeout_hint.
# ---------------------------------------------------------------------------


def test_timeout_hint_names_the_model() -> None:
	reason = "Copilot inference timed out. Raw reason: inference timeout."

	hint = agent._timeout_hint(reason, "gpt-4o-mini")

	assert reason in hint
	assert "gpt-4o-mini" in hint


def test_timeout_hint_is_case_insensitive() -> None:
	reason = "Copilot inference Timed Out. Raw reason: inference timeout."

	hint = agent._timeout_hint(reason, "gpt-4o-mini")

	assert hint != reason
	assert "gpt-4o-mini" in hint


def test_timeout_hint_leaves_other_failures_alone() -> None:
	# Suggesting a different model for an auth failure would misdirect the user.
	reason = "no Copilot login found"

	assert agent._timeout_hint(reason, "gpt-4o-mini") == reason