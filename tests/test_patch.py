"""Patch engine tests: exact-once apply, safety checks, parallel regions, retry. Offline."""
import json

import pytest

from sre_agent.agents.coding import patch

SRC = "function load(user) {\n  const doc = find(user);\n  const { id } = doc;\n  return id;\n}\n"


def _edit(search, replace, file="a.js", reason="r"):
    return {"file": file, "search": search, "replace": replace, "reason": reason}


def test_apply_exact_once():
    out, errs = patch.apply_edits(SRC, [_edit("const { id } = doc;", "if (!doc) return null;\n  const { id } = doc;")])
    assert errs == [None] and "if (!doc) return null;" in out and out.count("return id;") == 1


def test_apply_rejects_missing_and_ambiguous():
    text = "x = 1\ny = 1\n"
    _, errs = patch.apply_edits(text, [_edit("= 1", "= 2"), _edit("nope", "z")])
    assert "more than one" in errs[0] and "not found" in errs[1]


def test_apply_tolerates_trailing_space_and_keeps_crlf():
    text = "a = 1;  \r\nb = 2;\r\n"
    out, errs = patch.apply_edits(text, [_edit("a = 1;\nb = 2;", "a = 3;\nb = 4;")])
    assert errs == [None] and out == "a = 3;\r\nb = 4;\r\n"


def test_check_diff_rejects_mass_deletion():
    big = "\n".join(f"line {i}" for i in range(100)) + "\n"
    ok, removed, _ = patch.check_diff(big, "line 0\n")
    assert not ok and removed > 90
    assert patch.check_diff(big, big.replace("line 5\n", "line five\n"))[0]


def test_parse_edits_requires_json_and_fields():
    assert patch.parse_edits('note {"edits": [{"file": "a", "search": "s", "replace": "r"}]}')[0]["file"] == "a"
    assert patch.parse_edits('{"edits": [{"file": "a"}]}') == []
    with pytest.raises(ValueError):
        patch.parse_edits("sorry, cannot do that")


def _big_file():
    lines = [f"// filler line number {i} " + "x" * 40 for i in range(2000)]
    lines[100] = "const a = doc.x;"
    lines[1500] = "const b = doc.y;"
    return "\n".join(lines) + "\n"


def test_big_file_gets_one_task_per_region():
    notes = []
    tasks = patch.build_tasks({"big.js": _big_file()},
                              {"code_refs": [{"path": "big.js", "line": 101}, {"path": "big.js", "line": 1501}]}, notes)
    assert len(tasks) == 2 and tasks[0]["end"] < tasks[1]["start"] and not notes
    assert patch.build_tasks({"big.js": _big_file()}, {}, notes) == [] and "skipped" in notes[0]


def test_fix_files_patches_regions_in_parallel_and_leaves_rest_untouched(tmp_path):
    (tmp_path / "big.js").write_bytes(_big_file().encode())
    inv = {"code_refs": [{"path": "big.js", "line": 101}, {"path": "big.js", "line": 1501}]}

    def ask(prompt):
        if "doc.x" in prompt:
            return json.dumps({"edits": [_edit("const a = doc.x;", "const a = doc?.x;", "big.js")]})
        if "doc.y" in prompt:
            return json.dumps({"edits": [_edit("const b = doc.y;", "const b = doc?.y;", "big.js")]})
        return json.dumps({"edits": []})

    out = patch.fix_files(tmp_path, {"big.js": _big_file()}, inv, ask, rounds=1)
    new = (tmp_path / "big.js").read_text()
    assert out["changed"] == ["big.js"] and out["edits_applied"] == 2 and not out["failures"]
    assert "doc?.x" in new and "doc?.y" in new and new.count("// filler") == 1998


def test_failed_edit_is_retried_with_its_error(tmp_path):
    (tmp_path / "a.js").write_text(SRC)
    seen = []

    def ask(prompt):
        seen.append(prompt)
        search = "const { id } = doc;" if "PREVIOUS EDITS FAILED" in prompt else "const { idd } = doc;"
        return json.dumps({"edits": [_edit(search, "const { id } = doc || {};", "a.js")]})

    out = patch.fix_files(tmp_path, {"a.js": SRC}, {}, ask, rounds=2)
    assert out["edits_applied"] == 1 and out["changed"] == ["a.js"]
    assert any("not found" in p for p in seen[1:])


def test_syntax_failure_reverts_the_file(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    ask = lambda p: json.dumps({"edits": [_edit("x = 1", "x = (", "m.py")]})  # noqa: E731
    out = patch.fix_files(tmp_path, {"m.py": "x = 1\n"}, {}, ask, rounds=1)
    assert out["changed"] == [] and "reverted" in out["failures"][0]
    assert (tmp_path / "m.py").read_text() == "x = 1\n"


def test_midline_search_gets_the_file_indent_for_later_lines():
    text = "function f() {\n    const x = doc;\n}\n"
    out, errs = patch.apply_edits(text, [_edit("const x = doc;", "if (!doc) {\n  return null;\n}\nconst x = doc;")])
    assert errs == [None]
    assert out == "function f() {\n    if (!doc) {\n      return null;\n    }\n    const x = doc;\n}\n"


def test_sweep_round_fixes_a_second_site_the_first_round_missed(tmp_path):
    text = "a = doc.x;\nb = other;\nc = doc.y;\n"
    (tmp_path / "s.js").write_text(text)

    def ask(prompt):
        if "EDITS ALREADY APPLIED" in prompt:
            return json.dumps({"edits": [_edit("c = doc.y;", "c = doc?.y;", "s.js", "second site")]})
        return json.dumps({"edits": [_edit("a = doc.x;", "a = doc?.x;", "s.js", "first site")]})

    out = patch.fix_files(tmp_path, {"s.js": text}, {}, ask, rounds=1)
    assert out["edits_applied"] == 2
    assert (tmp_path / "s.js").read_text() == "a = doc?.x;\nb = other;\nc = doc?.y;\n"


def test_failed_search_hint_names_the_first_differing_line():
    text = "a();\nb();\nc();\n"
    _, errs = patch.apply_edits(text, [_edit("a();\nb();\nx();", "y();")])
    assert "differs" in errs[0] and "x();" in errs[0] and "c();" in errs[0]


def test_search_with_wrong_indentation_still_applies_in_the_files_indentation():
    text = "function f() {\n    const a = find();\n    const { x } = a;\n    return x;\n}\n"
    edit = _edit("      const a = find();\n      const { x } = a;",
                 "      const a = find();\n      if (!a) {\n        return null;\n      }\n      const { x } = a;")
    out, errs = patch.apply_edits(text, [edit])
    assert errs == [None]
    assert out == ("function f() {\n    const a = find();\n    if (!a) {\n      return null;\n    }\n"
                   "    const { x } = a;\n    return x;\n}\n")


def test_indent_free_match_refuses_when_two_places_match():
    text = "  a();\n  b();\nx;\n    a();\n    b();\n"
    _, errs = patch.apply_edits(text, [_edit("a();\nb();", "c();")])
    assert "more than one" in errs[0]
