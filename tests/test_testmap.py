"""Unit tests for quack.testmap over fabricated repo trees."""

from __future__ import annotations

from pathlib import Path

from quack.delta import StagedDelta, StagedFile
from quack.testmap import TestMapConfig, build_plan


def _delta(*paths: str, status: str = "M") -> StagedDelta:
	files = [
		StagedFile(path=p, status=status, added=3, removed=0, hunks=[]) for p in paths
	]
	return StagedDelta(files=files, raw_diff="")


def _write(root: Path, rel: str, content: str = "") -> Path:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")
	return path


# ---------------------------------------------------------------------------
# C# layout
# ---------------------------------------------------------------------------


def _make_cs_repo(root: Path) -> None:
	_write(root, "src/Lib/Lib.csproj", "<Project />")
	_write(root, "src/Lib/Sub/Foo.cs", "namespace Lib.Sub; public class Foo {}")
	_write(root, "src/Lib/Bar.cs", "namespace Lib; public class Bar {}")
	_write(root, "src/Lib/Orphan.cs", "namespace Lib; public class Orphan {}")

	_write(root, "tests/Lib.Tests/Lib.Tests.csproj", "<Project />")
	_write(
		root,
		"tests/Lib.Tests/Sub/FooTests.cs",
		"public class FooTests { void T() { new Foo(); } }",
	)
	_write(
		root,
		"tests/Lib.Tests/BarTest.cs",
		"public class BarTest { void T() { new Bar(); } }",
	)


def test_cs_conventional_and_mirrored(tmp_path: Path) -> None:
	_make_cs_repo(tmp_path)
	plan = build_plan(_delta("src/Lib/Sub/Foo.cs"), root=tmp_path)

	assert len(plan.mappings) == 1
	mapping = plan.mappings[0]
	assert mapping.class_name == "Foo"
	assert mapping.tests == ["tests/Lib.Tests/Sub/FooTests.cs"]
	assert mapping.test_project == "tests/Lib.Tests/Lib.Tests.csproj"
	assert not plan.untested_sources


def test_cs_sibling_test_project_is_mapped(tmp_path: Path) -> None:
	_write(
		tmp_path,
		"packages/GraphicsModelEditor/FabricWasmHost/FabricWasmHost.csproj",
		"<Project />",
	)
	_write(
		tmp_path,
		"packages/GraphicsModelEditor/FabricWasmHost/SvgDrawingAdapter.cs",
		"public class SvgDrawingAdapter {}",
	)
	_write(
		tmp_path,
		"packages/GraphicsModelEditor/FabricWasmHost.Tests/FabricWasmHost.Tests.csproj",
		"<Project />",
	)
	_write(
		tmp_path,
		"packages/GraphicsModelEditor/FabricWasmHost.Tests/SvgDrawingAdapterTests.cs",
		"public class SvgDrawingAdapterTests {}",
	)

	plan = build_plan(
		_delta("packages/GraphicsModelEditor/FabricWasmHost/SvgDrawingAdapter.cs"),
		root=tmp_path,
	)

	mapping = plan.mappings[0]
	assert mapping.tests == [
		"packages/GraphicsModelEditor/FabricWasmHost.Tests/SvgDrawingAdapterTests.cs"
	]
	assert mapping.test_project == (
		"packages/GraphicsModelEditor/FabricWasmHost.Tests/"
		"FabricWasmHost.Tests.csproj"
	)
	assert plan.runner_commands == [
		'dotnet test packages/GraphicsModelEditor/FabricWasmHost.Tests/'
		'FabricWasmHost.Tests.csproj --no-build --filter '
		'"FullyQualifiedName~SvgDrawingAdapterTests"'
	]


def test_cs_runner_command_no_build_filter(tmp_path: Path) -> None:
	_make_cs_repo(tmp_path)
	plan = build_plan(_delta("src/Lib/Sub/Foo.cs", "src/Lib/Bar.cs"), root=tmp_path)

	assert plan.runner_command is not None
	assert len(plan.runner_commands) == 1
	cmd = plan.runner_commands[0]
	assert "dotnet test tests/Lib.Tests/Lib.Tests.csproj" in cmd
	assert "--no-build" in cmd
	# both classes deduped into a single project command, joined with |.
	assert 'FullyQualifiedName~' in cmd
	assert "FooTests" in cmd and "BarTests" in cmd
	assert "|" in cmd
	assert plan.dotnet_hint is True


def test_cs_no_tests_found(tmp_path: Path) -> None:
	_make_cs_repo(tmp_path)
	plan = build_plan(_delta("src/Lib/Orphan.cs"), root=tmp_path)

	assert plan.untested_sources == ["src/Lib/Orphan.cs"]
	assert plan.runner_commands == []


def test_cs_content_probe_when_name_differs(tmp_path: Path) -> None:
	_write(tmp_path, "src/App/App.csproj", "<Project />")
	_write(tmp_path, "src/App/Widget.cs", "public class Widget {}")
	_write(tmp_path, "tests/App.Tests/App.Tests.csproj", "<Project />")
	# No conventional WidgetTests.cs name; only a content usage of Widget.
	_write(
		tmp_path,
		"tests/App.Tests/MiscSpecs.cs",
		"public class MiscSpecs { void T() { var w = new Widget(); } }",
	)
	plan = build_plan(_delta("src/App/Widget.cs"), root=tmp_path)

	mapping = plan.mappings[0]
	assert mapping.tests == ["tests/App.Tests/MiscSpecs.cs"]
	assert mapping.test_project == "tests/App.Tests/App.Tests.csproj"


def test_cs_changed_test_recorded(tmp_path: Path) -> None:
	_make_cs_repo(tmp_path)
	plan = build_plan(_delta("tests/Lib.Tests/Sub/FooTests.cs"), root=tmp_path)

	assert plan.changed_tests == ["tests/Lib.Tests/Sub/FooTests.cs"]
	assert plan.mappings == []


# ---------------------------------------------------------------------------
# Python layout
# ---------------------------------------------------------------------------


def _make_py_repo(root: Path) -> None:
	_write(root, "src/pkg/foo.py", "def foo(): ...")
	_write(root, "src/pkg/bar.py", "def bar(): ...")
	_write(root, "src/pkg/orphan.py", "def orphan(): ...")
	_write(root, "tests/test_foo.py", "def test_foo(): ...")
	_write(root, "tests/bar_test.py", "def test_bar(): ...")


def test_py_conventional_names(tmp_path: Path) -> None:
	_make_py_repo(tmp_path)
	plan = build_plan(_delta("src/pkg/foo.py", "src/pkg/bar.py"), root=tmp_path)

	sources = {m.source: m for m in plan.mappings}
	assert sources["src/pkg/foo.py"].tests == ["tests/test_foo.py"]
	assert sources["src/pkg/bar.py"].tests == ["tests/bar_test.py"]

	assert plan.runner_command is not None
	cmd = plan.runner_commands[-1]
	assert cmd.startswith("pytest ")
	assert cmd.endswith(" -x")
	assert "tests/test_foo.py" in cmd and "tests/bar_test.py" in cmd


def test_py_no_tests_found(tmp_path: Path) -> None:
	_make_py_repo(tmp_path)
	plan = build_plan(_delta("src/pkg/orphan.py"), root=tmp_path)

	assert plan.untested_sources == ["src/pkg/orphan.py"]
	assert plan.runner_commands == []


def test_py_changed_test_recorded(tmp_path: Path) -> None:
	_make_py_repo(tmp_path)
	plan = build_plan(_delta("tests/test_foo.py"), root=tmp_path)

	assert plan.changed_tests == ["tests/test_foo.py"]
	assert plan.mappings == []


# ---------------------------------------------------------------------------
# Mixed / config
# ---------------------------------------------------------------------------


def test_mixed_repo_emits_both_commands(tmp_path: Path) -> None:
	_make_cs_repo(tmp_path)
	_make_py_repo(tmp_path)
	plan = build_plan(
		_delta("src/Lib/Sub/Foo.cs", "src/pkg/foo.py"), root=tmp_path
	)

	assert any(c.startswith("dotnet test") for c in plan.runner_commands)
	assert any(c.startswith("pytest") for c in plan.runner_commands)


def test_deleted_files_ignored(tmp_path: Path) -> None:
	_make_py_repo(tmp_path)
	plan = build_plan(_delta("src/pkg/orphan.py", status="D"), root=tmp_path)

	assert plan.mappings == []
	assert plan.untested_sources == []


def test_config_test_dir_treated_as_root(tmp_path: Path) -> None:
	_write(tmp_path, "src/App/App.csproj", "<Project />")
	_write(tmp_path, "src/App/Thing.cs", "public class Thing {}")
	# A test file without a *.Tests.csproj, discoverable only via config.
	_write(tmp_path, "spec/App/App.csproj", "<Project />")
	_write(tmp_path, "spec/App/ThingTests.cs", "public class ThingTests {}")

	cfg = TestMapConfig(test_dirs=("spec",))
	plan = build_plan(_delta("src/App/Thing.cs"), config=cfg, root=tmp_path)

	mapping = plan.mappings[0]
	assert mapping.tests == ["spec/App/ThingTests.cs"]
	assert mapping.test_project == "spec/App/App.csproj"
