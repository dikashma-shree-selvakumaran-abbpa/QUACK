"""Unit tests for quack.delta using fabricated git output strings."""

from __future__ import annotations

from quack import delta
from quack.delta import StagedDelta, StagedFile, parse_staged_delta


NAME_STATUS = "\n".join(
	[
		"M\tsrc/app.py",
		"A\tsrc/new.py",
		"D\tsrc/old.py",
		"R100\tsrc/a.py\tsrc/b.py",
		"M\tassets/logo.png",
	]
)

NUMSTAT = "\n".join(
	[
		"3\t1\tsrc/app.py",
		"10\t0\tsrc/new.py",
		"0\t8\tsrc/old.py",
		"0\t0\tsrc/{a.py => b.py}",
		"-\t-\tassets/logo.png",
	]
)

UNIFIED_DIFF = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,5 @@
 line
+added one
+added two
-removed one
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 000..333
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,10 @@
+brand new
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 444..000
--- a/src/old.py
+++ /dev/null
@@ -1,8 +0,0 @@
-gone
diff --git a/src/a.py b/src/b.py
similarity index 100%
rename from src/a.py
rename to src/b.py
diff --git a/assets/logo.png b/assets/logo.png
index 555..666 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""


def _find(delta_obj: StagedDelta, path: str) -> StagedFile:
	return next(f for f in delta_obj.files if f.path == path)


def test_parses_all_files_and_statuses():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	paths = {f.path: f.status for f in d.files}
	assert paths == {
		"src/app.py": "M",
		"src/new.py": "A",
		"src/old.py": "D",
		"src/b.py": "R",
		"assets/logo.png": "M",
	}


def test_line_counts_and_hunks():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	app = _find(d, "src/app.py")
	assert (app.added, app.removed) == (3, 1)
	assert len(app.hunks) == 1
	assert app.hunks[0].startswith("@@ -1,3 +1,5 @@")


def test_rename_uses_post_image_path():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	assert any(f.path == "src/b.py" for f in d.files)
	assert not any(f.path == "src/a.py" for f in d.files)


def test_deletion_has_no_hunks():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	old = _find(d, "src/old.py")
	assert old.status == "D"
	assert old.hunks == []
	assert old.removed == 8


def test_binary_file_flagged_no_diff():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	logo = _find(d, "assets/logo.png")
	assert logo.binary is True
	assert logo.hunks == []
	assert (logo.added, logo.removed) == (0, 0)


def test_totals():
	d = parse_staged_delta(NAME_STATUS, NUMSTAT, UNIFIED_DIFF)
	assert d.total_added == 13
	assert d.total_removed == 9


def test_raw_diff_truncated_at_cap():
	big = "diff --git a/x b/x\n" + ("+padding line\n" * 10_000)
	d = parse_staged_delta("M\tx", "5000\t0\tx", big)
	assert len(d.raw_diff) == delta.MAX_RAW_DIFF + len(delta.TRUNCATION_MARKER)
	assert d.raw_diff.endswith(delta.TRUNCATION_MARKER)


def test_empty_inputs():
	d = parse_staged_delta("", "", "")
	assert d.files == []
	assert d.raw_diff == ""


def test_triviality_no_changes():
	d = parse_staged_delta("", "", "")
	is_trivial, reason = d.triviality()
	assert is_trivial is True
	assert reason == "no staged changes"


def test_triviality_docs_only():
	d = parse_staged_delta(
		"M\tREADME.md\nA\tdocs/guide.rst",
		"40\t2\tREADME.md\n30\t0\tdocs/guide.rst",
		"",
	)
	is_trivial, reason = d.triviality()
	assert is_trivial is True
	assert reason == "docs-only change"


def test_triviality_small_change():
	d = parse_staged_delta("M\tsrc/app.py", "2\t1\tsrc/app.py", "")
	is_trivial, reason = d.triviality()
	assert is_trivial is True
	assert "small change" in reason


def test_triviality_only_lockfiles():
	d = parse_staged_delta(
		"M\tpoetry.lock\nM\tpackage-lock.json",
		"200\t50\tpoetry.lock\n300\t20\tpackage-lock.json",
		"",
	)
	is_trivial, reason = d.triviality()
	assert is_trivial is True
	assert reason == "only lockfiles/generated files"


def test_triviality_non_trivial():
	d = parse_staged_delta(
		"M\tsrc/app.py",
		"50\t20\tsrc/app.py",
		"",
	)
	is_trivial, reason = d.triviality()
	assert is_trivial is False
	assert reason == "non-trivial change"


def test_numstat_tab_separated_counts_attach_to_file():
	# Regression: real numstat fields are TAB-separated (added, removed, path).
	# Counts must attach to the matching StagedFile, not degrade to +0/-0.
	d = parse_staged_delta("M\tREADME.md\n", "1\t0\tREADME.md\n", "")
	assert len(d.files) == 1
	readme = d.files[0]
	assert readme.path == "README.md"
	assert readme.added == 1
	assert readme.removed == 0


def test_triviality_custom_excludes():
	d = parse_staged_delta(
		"M\tgen/schema.sql",
		"80\t10\tgen/schema.sql",
		"",
	)
	is_trivial, reason = d.triviality(excludes=["gen/*"])
	assert is_trivial is True
	assert reason == "only lockfiles/generated files"
