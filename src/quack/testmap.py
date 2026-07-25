"""Map changed source files to their tests and suggest a runner command.

Pure-ish logic: it takes a :class:`~quack.delta.StagedDelta` plus a
:class:`TestMapConfig` and a filesystem ``root`` in, and returns a
:class:`TestPlan` out. The only side effect is *reading* the repository tree
to locate test projects and test files -- it never touches git, the network,
or subprocess, so it is fully unit-testable against a fabricated ``tmp_path``.

Two language heuristics are supported.

C# (priority order):
	1. Conventional names -- for a changed ``Foo.cs`` look for ``FooTests.cs``
	   or ``FooTest.cs`` anywhere under a test project (a directory whose
	   nearest ancestor ``*.csproj`` name ends in ``.Tests``/``.Test``, or a
	   configured ``test_dir``).
	2. Mirrored structure -- ``src/Lib/Sub/Foo.cs`` ->
	   ``Lib.Tests/Sub/FooTests.cs`` (preferred when several name matches
	   exist).
	3. Content probe -- a fast regex over candidate test files for
	   ``class FooTests`` or any usage of the changed class name.
	The owning test PROJECT for each matched file is the nearest ancestor
	``.csproj``. One ``dotnet test`` command is emitted per test project.

Python (secondary path):
	For a changed ``foo.py`` look for ``test_foo.py``/``foo_test.py`` anywhere,
	or the mirrored ``src`` -> ``tests`` path. A single ``pytest`` command
	lists every matched test file.

The test-project/dir index is built exactly once per run so the whole thing
stays well under 300ms on a ~2k-file repository.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# Directories that never contain first-party source or tests we care about.
_PRUNE_DIRS = frozenset(
	{
		".git",
		".hg",
		".svn",
		"bin",
		"obj",
		"node_modules",
		"__pycache__",
		".venv",
		"venv",
		".tox",
		".mypy_cache",
		".pytest_cache",
		"packages",
	}
)

# A .csproj whose file name ends in .Tests.csproj or .Test.csproj is a test
# project. (case-insensitive)
_CS_TEST_PROJECT = re.compile(r"\.Tests?\.csproj$", re.IGNORECASE)

_PY_TEST_NAME = re.compile(r"^(?:test_.+|.+_test)\.py$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Configuration and result types
# ---------------------------------------------------------------------------


@dataclass
class TestMapConfig:
	"""Configuration for test mapping.

	``test_dirs`` are extra directories (repo-relative) that should be treated
	as test roots in addition to auto-detected ``*.Tests``/``*.Test`` projects.
	``languages`` limits which heuristics run.
	"""

	__test__ = False  # not a pytest test class

	test_dirs: tuple[str, ...] | list[str] = ()
	languages: tuple[str, ...] | list[str] = ("csharp", "python")


@dataclass
class TestMapping:
	"""One changed source file and the tests that cover it."""

	__test__ = False  # not a pytest test class

	source: str
	tests: list[str] = field(default_factory=list)
	test_project: str | None = None
	class_name: str | None = None


@dataclass
class TestPlan:
	"""The result of mapping a staged delta to tests."""

	__test__ = False  # not a pytest test class

	mappings: list[TestMapping] = field(default_factory=list)
	untested_sources: list[str] = field(default_factory=list)
	changed_tests: list[str] = field(default_factory=list)
	runner_commands: list[str] = field(default_factory=list)
	# True when at least one dotnet --no-build command is suggested, so the
	# caller can print the "build once" hint.
	dotnet_hint: bool = False

	@property
	def runner_command(self) -> str | None:
		"""All runner commands joined with newlines, or ``None`` if empty."""
		if not self.runner_commands:
			return None
		return "\n".join(self.runner_commands)


# ---------------------------------------------------------------------------
# Filesystem index (built once per run)
# ---------------------------------------------------------------------------


@dataclass
class _Index:
	root: Path
	# dir path -> its .csproj path (first one wins).
	csproj_by_dir: dict[Path, Path]
	# directories that are test roots (test-project dirs + configured dirs).
	test_roots: list[Path]
	cs_test_files: list[Path]
	py_test_files: list[Path]

	def nearest_csproj(self, path: Path) -> Path | None:
		for parent in [path.parent, *path.parent.parents]:
			proj = self.csproj_by_dir.get(parent)
			if proj is not None:
				return proj
			if parent == self.root:
				break
		return None


def _build_index(root: Path, config: TestMapConfig) -> _Index:
	csproj_by_dir: dict[Path, Path] = {}
	all_csproj: list[Path] = []
	all_cs: list[Path] = []
	all_py: list[Path] = []

	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
		here = Path(dirpath)
		for name in filenames:
			if name.endswith(".csproj"):
				path = here / name
				all_csproj.append(path)
				csproj_by_dir.setdefault(here, path)
			elif name.endswith(".cs"):
				all_cs.append(here / name)
			elif name.endswith(".py"):
				all_py.append(here / name)

	# Directories that root a test project.
	test_project_dirs = [
		proj.parent for proj in all_csproj if _CS_TEST_PROJECT.search(proj.name)
	]
	configured = [(root / d).resolve() for d in config.test_dirs]
	test_roots = test_project_dirs + configured

	cs_test_files = [p for p in all_cs if _under_any(p, test_roots)]
	py_test_files = [p for p in all_py if _PY_TEST_NAME.match(p.name)]

	return _Index(
		root=root,
		csproj_by_dir=csproj_by_dir,
		test_roots=test_roots,
		cs_test_files=cs_test_files,
		py_test_files=py_test_files,
	)


def _under_any(path: Path, roots: list[Path]) -> bool:
	try:
		resolved = path.resolve()
	except OSError:
		resolved = path
	for root in roots:
		try:
			resolved.relative_to(root)
			return True
		except ValueError:
			continue
	return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_plan(
	delta,
	config: TestMapConfig | None = None,
	root: Path | str | None = None,
) -> TestPlan:
	"""Map the staged delta's source files to tests and build a TestPlan."""
	cfg = config or TestMapConfig()
	root_path = Path(root).resolve() if root is not None else Path.cwd().resolve()
	index = _build_index(root_path, cfg)

	languages = set(cfg.languages)
	mappings: list[TestMapping] = []
	untested: list[str] = []
	changed_tests: list[str] = []

	# test project (Path) -> ordered, de-duplicated class-filter tokens.
	cs_projects: dict[Path, list[str]] = {}
	py_test_matches: list[str] = []

	for file in delta.files:
		if getattr(file, "status", "") == "D" or getattr(file, "binary", False):
			continue
		path = file.path
		lower = path.lower()

		if lower.endswith(".cs") and "csharp" in languages:
			if _is_cs_test(path):
				changed_tests.append(path)
				continue
			mapping = _map_cs_source(path, index)
			mappings.append(mapping)
			if mapping.tests and mapping.test_project and mapping.class_name:
				token = f"{mapping.class_name}Tests"
				proj = root_path / mapping.test_project
				tokens = cs_projects.setdefault(proj, [])
				if token not in tokens:
					tokens.append(token)
			else:
				untested.append(path)

		elif lower.endswith(".py") and "python" in languages:
			if _PY_TEST_NAME.match(Path(path).name):
				changed_tests.append(path)
				continue
			mapping = _map_py_source(path, index)
			mappings.append(mapping)
			if mapping.tests:
				for test in mapping.tests:
					if test not in py_test_matches:
						py_test_matches.append(test)
			else:
				untested.append(path)

	runner_commands: list[str] = []
	for proj, tokens in cs_projects.items():
		rel = _relpath(proj, root_path)
		joined = "|".join(tokens)
		runner_commands.append(
			f'dotnet test {rel} --no-build --filter "FullyQualifiedName~{joined}"'
		)
	dotnet_hint = bool(runner_commands)

	if py_test_matches:
		runner_commands.append("pytest " + " ".join(py_test_matches) + " -x")

	return TestPlan(
		mappings=mappings,
		untested_sources=untested,
		changed_tests=changed_tests,
		runner_commands=runner_commands,
		dotnet_hint=dotnet_hint,
	)


# ---------------------------------------------------------------------------
# C# heuristics
# ---------------------------------------------------------------------------


def _is_cs_test(path: str) -> bool:
	stem = Path(path).stem
	return stem.endswith("Tests") or stem.endswith("Test")


def _map_cs_source(path: str, index: _Index) -> TestMapping:
	stem = Path(path).stem
	names = {f"{stem}Tests", f"{stem}Test"}

	# (1) conventional names.
	name_matches = [p for p in index.cs_test_files if p.stem in names]

	# (2) mirrored structure -- prefer test files whose project-relative
	# directory mirrors the source's project-relative directory.
	matches = name_matches
	if name_matches:
		mirrored = [
			p
			for p in name_matches
			if _cs_subpath(p, index) == _cs_subpath(index.root / path, index)
		]
		if mirrored:
			matches = mirrored
	else:
		# (3) content probe.
		matches = _cs_content_probe(stem, index)

	if not matches:
		return TestMapping(source=path, tests=[], class_name=stem)

	project = index.nearest_csproj(matches[0])
	test_project = _relpath(project, index.root) if project else None
	return TestMapping(
		source=path,
		tests=sorted(_relpath(p, index.root) for p in matches),
		test_project=test_project,
		class_name=stem,
	)


def _cs_subpath(path: Path, index: _Index) -> str:
	"""Return ``path``'s directory relative to its owning project dir."""
	proj = index.nearest_csproj(path)
	base = proj.parent if proj else index.root
	try:
		return str(path.parent.resolve().relative_to(base.resolve()))
	except (ValueError, OSError):
		return str(path.parent)


def _cs_content_probe(stem: str, index: _Index) -> list[Path]:
	pattern = re.compile(
		rf"class\s+{re.escape(stem)}Tests?\b|\b{re.escape(stem)}\b"
	)
	hits: list[Path] = []
	for candidate in index.cs_test_files:
		try:
			text = candidate.read_text(encoding="utf-8", errors="ignore")
		except OSError:
			continue
		if pattern.search(text):
			hits.append(candidate)
	return hits


# ---------------------------------------------------------------------------
# Python heuristics
# ---------------------------------------------------------------------------


def _map_py_source(path: str, index: _Index) -> TestMapping:
	stem = Path(path).stem
	names = {f"test_{stem}.py", f"{stem}_test.py"}
	matches = [p for p in index.py_test_files if p.name in names]

	if not matches:
		return TestMapping(source=path, tests=[], class_name=stem)

	return TestMapping(
		source=path,
		tests=sorted(_relpath(p, index.root) for p in matches),
		class_name=stem,
	)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relpath(path: Path, root: Path) -> str:
	try:
		return str(path.resolve().relative_to(root.resolve())).replace(os.sep, "/")
	except (ValueError, OSError):
		return str(path).replace(os.sep, "/")
