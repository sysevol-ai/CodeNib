# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The walkthrough build's pure parts: excerpts, text checks, layout, merging."""

import json

from codenib.web.walkthroughs import load_walkthroughs
from scripts.walkthrough.graph_paths import is_test, path_names, words
from scripts.walkthrough.layout import (
    RepoMap,
    map_sentence,
    outline_areas,
    walk_layout,
    walk_source,
    wiki_pages,
)
from scripts.walkthrough.served import merge_parts, served_form, write_served
from scripts.walkthrough.steps import (
    check_overview,
    check_step,
    excerpt,
    html,
    page_steps,
    path_text,
    plain,
)
from scripts.walkthrough.verify import verify_walk


def _reader(files):
    """A line reader over {file: [lines]}, 1-based like the index's."""

    def line(rel, n):
        lines = files.get(rel, [])
        return lines[n - 1] if 0 < n <= len(lines) else ""

    return line


def _fn(name, file, a, b, vid=0):
    return {"vid": vid, "name": name, "file": file, "a": a, "b": b}


def test_html_escapes_and_plain_reverses_it():
    text = "a < b & `run()` > c"
    assert html(text) == "a &lt; b &amp; <code>run()</code> &gt; c"
    assert plain(html(text)) == text
    assert html(None) == ""


def test_excerpt_gives_the_leading_call_two_lines_of_context():
    lines = [f"    line {n}" for n in range(1, 31)]
    lines[8] = ""  # line 9 is blank and is not shown as context
    f = _fn("f()", "m.py", 1, 30)
    assert excerpt(_reader({"m.py": lines}), f, [10], next_line=10) == [
        [1, 1],
        [8, 8],
        [10, 12],
    ]


def test_excerpt_shrinks_context_then_drops_marks_but_keeps_the_call():
    lines = ["x" * 120 for _ in range(40)]  # three rows each
    f = _fn("f()", "m.py", 1, 40)
    line = _reader({"m.py": lines})
    assert excerpt(line, f, [5, 15, 25, 35]) == [[1, 1], [5, 5], [15, 15]]
    assert excerpt(line, f, [35, 5, 15, 25], next_line=35) == [
        [1, 1],
        [5, 5],
        [35, 35],
    ]


def test_check_step_filters_marks_and_reports_limits_and_citations():
    lines = [f"line {n}" for n in range(1, 21)]
    line = _reader({"pkg/m.py": lines})
    f = _fn("f()", "pkg/m.py", 2, 20)
    st = {
        "role": "hands it on",
        "title": "t" * 57,
        "lead": "Calls it at m.py:5 and at m.py:99.",
        "body": ["b" * 10],
        "idea": "",
        "what": "a value",
        "packet": "value",
        "mark": [1, "4", 4, "x", 30, 6],
    }
    errs = check_step(line, st, f, 6, {"m.py": "pkg/m.py"})
    assert st["mark"] == [4, 6]
    assert st["show"] == [[2, 8]]  # the def line and both marks, merged
    assert errs == [
        "title is 57 characters; the limit is 56",
        "m.py:99 is not a source line on this path",
    ]


def test_check_step_needs_a_marked_line_and_the_required_fields():
    line = _reader({"m.py": ["def f():", "    pass"]})
    st = {"title": "", "lead": "x", "mark": []}
    errs = check_step(line, st, _fn("f()", "m.py", 1, 2), None, {})
    assert "title is empty" in errs
    assert "what is empty" in errs
    assert "mark at least one line" in errs


def test_check_overview_allows_only_the_paths_own_counts():
    ok = {
        "title": "Three functions in two files",
        "dek": "Line m.py:7 matters.",
        "overview": {"title": "The path", "lead": "One call.", "idea": "Watch it."},
        "recap": {"title": "Back", "lead": "It returns."},
    }
    assert check_overview(ok, 3, 2) == []
    bad = dict(ok, title="Seven functions")
    assert check_overview(bad, 3, 2) == [
        "title says 'seven', but the path has 3 functions in 2 files"
    ]
    assert "recap.lead is empty" in check_overview(dict(ok, recap={"title": "x"}), 3, 2)


def _path():
    return [
        {"fn": _fn("Agent.run()", "src/a/agents.py", 10, 20, 1), "via": None},
        {
            "fn": _fn("Agent.step()", "src/a/agents.py", 30, 40, 2),
            "via": {"kind": "call", "line": 15},
        },
        {
            "fn": _fn("Code.step()", "src/a/agents.py", 50, 60, 3),
            "via": {"kind": "subclass", "line": None},
        },
        {
            "fn": _fn("Executor.__call__()", "src/a/exec.py", 5, 9, 4),
            "via": {"kind": "unrecorded", "line": 55, "how": "attr"},
        },
    ]


def test_path_text_says_how_each_stop_is_reached():
    text = path_text(_path()).split("\n")
    assert text[0] == "1. Agent.run() (src/a/agents.py:10-20) (start)"
    assert (
        text[1] == "2. Agent.step() (src/a/agents.py:30-40) reached from line 15 (call)"
    )
    # A subclass hop has no call line; the prompt has always said so this way.
    assert text[2].endswith("reached from line None (subclass)")
    assert "a call through an attribute" in text[3]
    assert path_names(_path())[2:] == [
        "Code.step() [subclass]",
        "Executor.__call__() [unrecorded]",
    ]


def _repo_map():
    outline = {
        "pages": [
            {"id": "overview", "title": "Overview", "files": ["README.md"]},
            {
                "id": "agents",
                "title": "Agents and Memory",
                "files": ["src/a/agents.py"],
                "children": [{"id": "steps", "files": ["src/a/steps.py"]}],
            },
            {"id": "exec", "title": "Execution, Sandboxes", "files": ["src/a/exec.py"]},
        ]
    }
    wiki_map = {
        "areas": [
            {"id": "agents", "symbols": 40, "files": 2},
            {"id": "exec", "symbols": 9, "files": 1},
        ],
        "links": [
            {"source": "agents", "target": "exec", "calls": 12},
            {"source": "exec", "target": "agents", "calls": 3},
        ],
    }
    pages = wiki_pages(
        [
            {"id": "overview", "title": "Overview"},
            {
                "id": "agents",
                "title": "Agents",
                "children": [{"id": "steps", "title": "Steps"}],
            },
        ]
    )
    return RepoMap.build(outline, wiki_map, pages, {"Agent.run()": {"steps", "agents"}})


def test_outline_areas_name_each_top_page_and_own_its_files():
    assert outline_areas({"pages": [{"id": "overview", "title": "Overview"}]}) == (
        [],
        {},
    )
    rmap = _repo_map()
    assert [(a["id"], a["name"], a["symbols"]) for a in rmap.areas] == [
        ("agents", "Agents", 40),
        ("exec", "Execution", 9),
    ]
    assert rmap.area_of_file["src/a/steps.py"] == "agents"
    assert rmap.links == [["agents", "exec", 12]]
    assert rmap.wiki()["cites"] == {"Agent.run()": ["agents", "steps"]}
    assert rmap.wiki()["pages"]["steps"] == {
        "title": "Steps",
        "top": "agents",
        "depth": 1,
    }


def test_walk_layout_marks_call_sites_crossings_and_zones():
    lay = walk_layout(_path(), "agent.run(task)", _repo_map())
    assert lay["stops"][0] == ["", "agent.run(task)", None, None, True]
    assert lay["stops"][1] == ["Agent", ".run()", "agents.py:10–20", ""]
    assert lay["segs"] == [
        ["", ""],
        [":15", "agents.py:15", "", ""],
        ["subclass :30", "agents.py:30", "dash", "subclass"],
        ["not in the index :55", "agents.py:55", "dash", "not in the index", "attr"],
    ]
    assert lay["zones"] == [
        ["agents.py", "Agents", [1, 2, 3]],
        ["exec.py", "Execution", [4]],
    ]
    assert lay["cross"] == [["agents", "exec", 12, 4, "unrecorded"]]
    assert lay["areas"] == ["agents", "exec"]
    assert lay["paths"] == {"agents.py": "src/a/agents.py", "exec.py": "src/a/exec.py"}
    assert lay["map"] == (
        "The path starts in Agents, and crosses into Execution at step 4 through a "
        "call the index does not record; the map records 12 other calls in that "
        "direction."
    )


def test_map_sentence_without_crossings():
    areas = [{"id": "x", "name": "Core"}]
    assert map_sentence([], ["x"], areas) == "Every step stays inside Core."
    assert map_sentence([], [], areas) == "The path runs outside the Wiki map."
    assert map_sentence(
        [["x", "y", 0, 2, "call"]], ["x"], areas + [{"id": "y", "name": "Y"}]
    ) == ("The path starts in Core, crosses into Y at step 2.")


def _walk(path):
    lines = {
        "src/a/agents.py": [f"agents {n}" for n in range(1, 61)],
        "src/a/exec.py": [f"exec {n}" for n in range(1, 10)],
    }
    lay = walk_layout(path, "agent.run(task)", _repo_map())
    steps_out = [
        {
            "title": f"Step {k}",
            "lead": f"Lead {k} with `x`.",
            "body": ["Body."],
            "idea": "",
            "what": "a task",
            "packet": "task",
            "show": [[p["fn"]["a"], p["fn"]["a"]]],
            "mark": [p["fn"]["a"]],
        }
        for k, p in enumerate(path, start=1)
    ]
    ov = {
        "overview": {"title": "Overview", "lead": "Lead.", "body": [], "idea": "Idea."},
        "recap": {"title": "Recap", "lead": "Back.", "body": []},
    }
    walk = {
        "id": "run",
        "stops": lay["stops"],
        "segs": lay["segs"],
        "steps": page_steps(path, steps_out, ov, _repo_map().area_of_file),
    }
    return walk, walk_source(_reader(lines), path)


class _FakeChecker:
    def __init__(self):
        self.prompts = []

    def check(self, system, user):
        self.prompts.append(user)
        if "step about Agent.step()" in user:
            return {
                "problems": [{"claim": "c", "verdict": "subject", "why": "w"}],
                "fixed": {"lead": "Fixed `lead`.", "body": "One body.", "idea": ""},
            }
        return {"problems": [], "fixed": None}


def test_verify_walk_shows_the_caller_and_applies_fixes():
    path = _path()
    walk, source = _walk(path)
    model = _FakeChecker()
    report = verify_walk(walk, source, model)
    assert [r["step"] for r in report] == [1, 2, 3, 4, 0, 5]
    step = walk["steps"][2]
    assert step["lead"] == "Fixed <code>lead</code>."
    assert step["body"] == ["One body."]
    # Agent.step() is called from Agent.run() at line 15.
    assert "Its caller around the call (src/a/agents.py:15):" in model.prompts[1]
    assert "   10  agents 10" in model.prompts[1]
    # Code.step() is reached through a subclass hop: its caller is Agent.run().
    assert "Its caller around the call (src/a/agents.py:15):" in model.prompts[2]
    assert model.prompts[4].startswith("Text of the walkthrough's overview:")


def test_merge_and_served_file_loads_for_the_backend(tmp_path):
    path = _path()
    walk, source = _walk(path)

    def part(wid, src):
        return {
            "repo": {"id": "o__r", "slug": "o/r", "commit": "abc", "model": "m"},
            "areas": [],
            "links": [],
            "paths": {"agents.py": "src/a/agents.py"},
            "wiki": {"pages": {}, "cites": {}, "size": {}, "calls": {}},
            "walks": [dict(walk, id=wid)],
            "source": src,
            "log": {"calls": [{"in": 1}], "checks": [], "verify": []},
        }

    first = part("run", {"src/a/agents.py": {"10": "a"}})
    second = part("tool", {"src/a/agents.py": {"11": "b"}, "src/a/exec.py": {"5": "c"}})
    merged = merge_parts([first, second])
    assert [w["id"] for w in merged["walks"]] == ["run", "tool"]
    assert merged["source"] == {
        "src/a/agents.py": {"10": "a", "11": "b"},
        "src/a/exec.py": {"5": "c"},
    }
    assert len(merged["log"]["calls"]) == 2
    assert first["source"] == {"src/a/agents.py": {"10": "a"}}  # parts are not changed

    served = served_form(merged)
    assert served["version"] == 1 and "log" not in served
    target = write_served(merged, tmp_path / "walkthroughs")
    assert target == tmp_path / "walkthroughs" / "o__r.json"
    assert json.loads(target.read_text()) == served
    loaded = load_walkthroughs(tmp_path, "o__r")
    assert loaded is not None
    assert loaded["repo"] == {
        "id": "o__r",
        "slug": "o/r",
        "commit": "abc",
        "model": "m",
    }


def test_words_and_test_files():
    # Stems of five letters; stop words and words under three letters dropped.
    assert words("How does LogisticRegressionCV pick its best C?") == [
        "logis",
        "regre",
        "pick",
        "best",
    ]
    assert is_test({"file": "pkg/tests/test_x.py"})
    assert is_test({"file": "gin_test.go"})
    assert not is_test({"file": "src/pkg/core.py"})


def test_shipped_questions_load():
    from scripts.walkthrough.build_walkthroughs import DEFAULT_QUESTIONS, load_questions

    questions = load_questions(DEFAULT_QUESTIONS, "scikit-learn__scikit-learn")
    assert [q["id"] for q in questions] == ["bestc", "pipe"]
    assert questions[0]["via"] == ["_log_reg_scoring_path()"]
