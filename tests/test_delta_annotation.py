"""Tests for absolute line-number annotation of unified diffs."""

from quack.delta import annotate_with_line_numbers, parse_staged_delta


def test_numbers_context_and_added_lines_from_hunk_header() -> None:
	diff = "@@ -1,3 +10,4 @@\n ctx\n+added\n ctx2"
	out = annotate_with_line_numbers(diff).split("\n")
	assert out[0] == "@@ -1,3 +10,4 @@"
	assert out[1].startswith("    10 | ")
	assert out[2].startswith("    11 | ")
	assert out[3].startswith("    12 | ")


def test_removed_lines_carry_no_number_and_do_not_advance_counter() -> None:
	# A removed line does not exist in the post-image, so it gets no number
	# and must not consume one -- otherwise every later line is off by one.
	diff = "@@ -1,3 +10,3 @@\n ctx\n-gone\n ctx2"
	out = annotate_with_line_numbers(diff).split("\n")
	assert out[2].startswith("       | -gone")
	assert out[3].startswith("    11 | ")


def test_file_headers_are_not_numbered() -> None:
	# "+++"/"---" start with + and - but are headers, not content.
	diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n ctx"
	out = annotate_with_line_numbers(diff).split("\n")
	assert out[1] == "--- a/f.py"
	assert out[2] == "+++ b/f.py"


def test_counter_resets_per_hunk() -> None:
	diff = "@@ -1,1 +5,1 @@\n ctx\n@@ -20,1 +100,1 @@\n ctx"
	out = annotate_with_line_numbers(diff).split("\n")
	assert out[1].startswith("     5 | ")
	assert out[3].startswith("   100 | ")


def test_parse_staged_delta_annotates_raw_diff_but_not_hunks() -> None:
	# raw_diff goes to the model and must be annotated; hunks are parsed by
	# quack itself and must stay raw.
	name_status = "M\tsrc/f.py"
	numstat = "1\t0\tsrc/f.py"
	diff = (
		"diff --git a/src/f.py b/src/f.py\n"
		"--- a/src/f.py\n"
		"+++ b/src/f.py\n"
		"@@ -1,1 +1,2 @@\n"
		" ctx\n"
		"+added"
	)
	delta = parse_staged_delta(name_status, numstat, diff)
	assert " | " in delta.raw_diff
	assert delta.files[0].hunks
	assert " | " not in delta.files[0].hunks[0]
