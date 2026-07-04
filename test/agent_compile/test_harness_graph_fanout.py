# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph fan-out routing helpers in the synthesis harness."""

from __future__ import annotations

from codeminer.agent.agent_types import (
    AgentResult,
    AgentRunTrace,
    AgentTraceEvent,
    ContextLedgerEntry,
    ToolCallRecord,
)
from scripts.agent_compile.lib.config import SweepConfig
from scripts.agent_compile.lib.harness import (
    _is_traceback_route_query,
    _query_terms,
    _scheduled_context_state_with_trace_fallback,
    _scheduled_route_budget_locations,
    _scheduled_route_dynamic_budget_locations,
    _scheduled_route_prompt_locations,
    _tool_call_turns_by_id,
    apply_scheduled_named_anchor_location_patch,
    apply_scheduled_top_anchor_answer_patch,
    filter_answer_locations_to_trace_evidence,
    make_graph_context_scheduler,
    normalize_answer_location_order,
    normalize_answer_path_aliases,
    preload_symbol_hints,
    read_outputs_have_scoped_symbols,
    read_symbol_hints,
    render_scheduled_anchor_correction,
    render_scheduled_ordering_correction,
    render_scheduled_top_anchor_correction,
    run_cell,
    salvage_answer_schema_from_trace,
    scheduled_anchor_audit,
    scheduled_answer_ordering_audit,
    scheduled_definition_candidates_for_symbols,
    scheduled_named_anchor_location_audit,
    scheduled_top_anchor_audit,
    scheduled_top_anchor_required_anchor_audit,
    search_fanout_pressure,
    tool_call_pressure,
)
from scripts.agent_compile.lib.verify_expand import GraphNav


def _result(turns: int, tools: list[str]) -> AgentResult:
    return AgentResult(
        answer="",
        total_turns=turns,
        tool_calls=[
            ToolCallRecord(tool_call_id=f"call_{i}", skill_id=tool, arguments={})
            for i, tool in enumerate(tools)
        ],
    )


def test_search_fanout_pressure_triggers_on_search_calls():
    pressure = search_fanout_pressure(
        _result(2, ["grep", "glob", "bash", "read"]),
        max_turns=10,
        min_search_calls=3,
    )

    assert pressure["triggered"] is True
    assert pressure["reason"] == "search_calls"
    assert pressure["search_calls"] == 3
    assert pressure["read_calls"] == 1


def test_search_fanout_pressure_triggers_near_turn_budget():
    pressure = search_fanout_pressure(
        _result(8, ["read"]),
        max_turns=10,
        min_search_calls=3,
        turn_ratio=0.75,
    )

    assert pressure["triggered"] is True
    assert pressure["reason"] == "turn_budget"


def test_run_cell_passes_compact_tail_chars_to_runner(tmp_path, monkeypatch):
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, query, **kwargs):
            return AgentResult(
                answer="Files: src/a.py\nLocations: src/a.py:1-1",
                total_turns=1,
                trace=AgentRunTrace(),
            )

    monkeypatch.setattr("codeminer.agent.runner.AgentRunner", FakeRunner)
    cfg = SweepConfig(
        sweep_id="unit",
        subsets={"preinj_eager_compact": ["read"]},
        max_turns=2,
    )

    out = run_cell(
        cfg,
        contexts={},
        llm=object(),
        repo_path=str(tmp_path),
        language_key="python",
        query="Locate the value",
        subset_id="preinj_eager_compact",
        skills=["read"],
        preload_spec={
            "mode": "eager_compact",
            "compact_keep_reads": 1,
            "compact_tail_chars": 123,
        },
    )

    assert out["answer"].startswith("Files:")
    assert captured["compact_after_read"] is True
    assert captured["compact_keep_reads"] == 1
    assert captured["compact_tail_chars"] == 123


def test_search_fanout_pressure_triggers_on_read_fanout():
    pressure = search_fanout_pressure(
        _result(4, ["read", "read", "read", "read"]),
        max_turns=10,
        min_search_calls=3,
        min_read_calls=4,
        turn_ratio=0.75,
    )

    assert pressure["triggered"] is True
    assert pressure["reason"] == "read_calls"
    assert pressure["read_calls"] == 4


def test_search_fanout_pressure_stays_off_for_short_confirmed_reads():
    pressure = search_fanout_pressure(
        _result(2, ["read", "read"]),
        max_turns=10,
        min_search_calls=3,
        turn_ratio=0.75,
    )

    assert pressure["triggered"] is False
    assert pressure["reason"] == "none"


def test_tool_call_pressure_accepts_raw_tool_records():
    pressure = tool_call_pressure(
        [
            ToolCallRecord(
                tool_call_id="call_1", skill_id="lsp_definition", arguments={}
            ),
            ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
        ],
        turns=1,
        max_turns=8,
        min_search_calls=1,
    )

    assert pressure["triggered"] is True
    assert pressure["reason"] == "search_calls"
    assert pressure["search_calls"] == 1
    assert pressure["read_calls"] == 1


def test_tool_call_turns_by_id_reads_trace_events():
    result = AgentResult(
        answer="",
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_2",
                skill_id="read",
                arguments={"file_path": "src/a.py"},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="tool_call",
                    turn=3,
                    data={"tool_call_id": "call_2", "tool": "read"},
                ),
                AgentTraceEvent(
                    kind="tool_call",
                    turn=4,
                    data={"tool_call_id": "call_3", "tool": "grep"},
                ),
            ]
        ),
    )

    assert _tool_call_turns_by_id(result) == {"call_2": 3, "call_3": 4}


def test_read_symbol_hints_extracts_scoped_definition_targets():
    hints = read_symbol_hints(
        [
            {
                "path": "src/rule.rs",
                "content": "    76 | Expr::Tuple(tuple) | Expr::List(tuple) => true",
            }
        ],
        max_symbols=4,
    )

    assert hints == ["ExprTuple", "ExprList", "Expr"]


def test_read_symbol_hints_extracts_python_definition_targets():
    hints = read_symbol_hints(
        [
            {
                "path": "astropy/utils/data.py",
                "content": "\n".join(
                    [
                        " 1043 | if not urlparse(f).netloc or is_url_in_cache(f)]",
                        " 1450 | dldir = _get_download_cache_loc(pkgname)",
                        " 1453 | filename = os.path.join("
                        "dldir, _url_to_dirname(url_key), 'contents')",
                        " 1730 | def _url_to_dirname(url):",
                    ]
                ),
            }
        ],
        max_symbols=5,
    )

    assert hints[:3] == [
        "_get_download_cache_loc",
        "_url_to_dirname",
        "is_url_in_cache",
    ]


def test_read_symbol_hints_demotes_python_same_file_attributes():
    hints = read_symbol_hints(
        [
            {
                "path": "astropy/utils/iers/iers.py",
                "content": "\n".join(
                    [
                        "  936 | cls._expires = expires",
                        "  937 | cls._auto_open_files = files",
                        "  990 | today = cls._today()",
                        " 1043 | if not urlparse(f).netloc or is_url_in_cache(f)]",
                        " 1450 | dldir = _get_download_cache_loc(pkgname)",
                        " 1453 | filename = _url_to_dirname(url_key)",
                    ]
                ),
            }
        ],
        max_symbols=5,
    )

    assert hints[:3] == [
        "_get_download_cache_loc",
        "_url_to_dirname",
        "is_url_in_cache",
    ]
    assert "_auto_open_files" not in hints


def test_read_symbol_hints_keeps_cache_check_across_later_cache_reads():
    hints = read_symbol_hints(
        [
            {
                "path": "astropy/utils/iers/iers.py",
                "content": "\n".join(
                    [
                        " 1030 | for f in all_urls:",
                        " 1043 | if not urlparse(f).netloc or is_url_in_cache(f):",
                        " 1058 | return cls.open(f, cache=True)",
                    ]
                ),
            },
            {
                "path": "astropy/utils/iers/iers.py",
                "content": "\n".join(
                    [
                        " 982 | file = download_file(file, cache=cache)",
                        " 985 | return get_readable_fileobj(file)",
                    ]
                ),
            },
            {
                "path": "astropy/utils/data.py",
                "content": "\n".join(
                    [
                        " 1344 | dldir = _get_download_cache_loc(pkgname)",
                        " 1359 | filename = os.path.join(dldir, dirname)",
                        " 1363 | set_temp_cache(tmp_cache)",
                    ]
                ),
            },
        ],
        max_symbols=8,
        query=(
            "remote leap-second fetching checks cached versions before "
            "resolving the download cache location"
        ),
    )

    assert "is_url_in_cache" in hints
    assert hints.index("is_url_in_cache") < len(hints)


def test_read_symbol_hints_extracts_go_definition_targets():
    hints = read_symbol_hints(
        [
            {
                "path": "replacer.go",
                "content": "\n".join(
                    [
                        " 254 | networkAddr, err := caddy.NewReplacer()."
                        "ReplaceOrErr(upstream.Dial, true, true)",
                        "  29 | func NewReplacer() *Replacer {",
                        " 297 | func globalDefaultReplacements(key string) (any, bool) {",
                    ]
                ),
            }
        ],
        max_symbols=5,
    )

    assert hints[:2] == [
        "NewReplacer",
        "ReplaceOrErr",
    ]


def test_read_symbol_hints_uses_query_terms_for_go_bridge_targets():
    hints = read_symbol_hints(
        [
            {
                "path": "modules/caddyhttp/reverseproxy/hosts.go",
                "content": "\n".join(
                    [
                        "  88 | host, port, err := net.SplitHostPort(address)",
                        "  89 | joined := net.JoinHostPort(host, port)",
                        " 102 | ctx = context.WithValue(ctx, key, value)",
                        " 112 | ctx = context.WithValue(ctx, caddy.ReplacerCtxKey, repl)",
                        " 180 | h.doActiveHealthCheck(upstream)",
                        " 181 | var checks HealthChecks",
                        " 182 | handler := func(r *http.Request) {}",
                        " 254 | dial, err := caddy.NewReplacer()."
                        "ReplaceOrErr(upstream.Dial, true, true)",
                    ]
                ),
            }
        ],
        max_symbols=5,
        query=(
            "The reverse proxy health checking loop resolves placeholder tokens "
            "in the dial address. What component bridges template variable "
            "substitution to provider values?"
        ),
    )

    assert hints[:2] == ["NewReplacer", "ReplaceOrErr"]
    assert "doActiveHealthCheck" in hints[:5]
    assert "func" not in hints
    if "ReplacerCtxKey" in hints:
        assert hints.index("ReplaceOrErr") < hints.index("ReplacerCtxKey")


def test_read_symbol_hints_preserves_base_type_for_many_variants():
    hints = read_symbol_hints(
        [
            {
                "path": "src/rule.rs",
                "content": (
                    "    76 | Expr::Tuple(_) | Expr::List(_) | Expr::Set(_) | "
                    "Expr::Call(_) | Expr::Name(_) | Expr::Generator(_) | "
                    "Expr::ListComp(_) | Expr::Starred(_) => true"
                ),
            }
        ],
        max_symbols=5,
    )

    assert hints == ["ExprTuple", "ExprList", "ExprSet", "Expr", "ExprCall"]


def test_read_symbol_hints_preserves_base_type_across_read_window():
    hints = read_symbol_hints(
        [
            {
                "path": "src/rule.rs",
                "content": "\n".join(
                    [
                        "    76 | Expr::Tuple(_) => true,",
                        "    77 | Expr::List(_) => true,",
                        "    78 | Expr::Set(_) => true,",
                        "    79 | Expr::Call(_) => true,",
                        "    80 | Expr::Name(_) => true,",
                    ]
                ),
            }
        ],
        max_symbols=4,
    )

    assert hints == ["Expr", "ExprTuple", "ExprList", "ExprSet"]


def test_read_symbol_hints_adds_query_expr_variants():
    hints = read_symbol_hints(
        [
            {
                "path": "src/rule.rs",
                "content": "\n".join(
                    [
                        " 165 | match value {",
                        " 166 | Expr::Call(call) => Some(call),",
                        " 240 | Expr::List(list) => Some(list),",
                    ]
                ),
            }
        ],
        max_symbols=6,
        query=(
            "ordered sequence literals passed as arguments and tuple list set "
            "collection values"
        ),
    )

    assert "ExprTuple" in hints
    assert "ExprList" in hints
    assert "ExprSet" in hints
    assert hints.index("ExprTuple") < hints.index("ExprCall")


def test_read_outputs_have_scoped_symbols_detects_rust_variants():
    assert (
        read_outputs_have_scoped_symbols(
            [{"content": "    76 | Expr::Tuple(tuple) | Expr::List(tuple) => true"}]
        )
        is True
    )
    assert (
        read_outputs_have_scoped_symbols(
            [{"content": " 254 | caddy.NewReplacer().ReplaceOrErr(upstream.Dial)"}]
        )
        is False
    )


def test_preload_symbol_hints_recovers_nearby_query_endpoint():
    class _Nav:
        file_to_idx = {
            "crates/ruff_linter/src/rules/perflint/rules/unnecessary_list_cast.rs": [
                0,
                1,
            ],
            "crates/ruff_linter/src/rules/pylint/rules/len_test.rs": [2],
        }
        idx_to_span = {
            0: (
                "crates/ruff_linter/src/rules/perflint/rules/unnecessary_list_cast.rs",
                43,
                45,
                "ruff_linter/rules/perflint/rules/unnecessary_list_cast/"
                "impl#[UnnecessaryListCast][AlwaysFixableViolation]message",
            ),
            1: (
                "crates/ruff_linter/src/rules/perflint/rules/unnecessary_list_cast.rs",
                53,
                124,
                "ruff_linter/rules/perflint/rules/unnecessary_list_cast/"
                "unnecessary_list_cast",
            ),
            2: (
                "crates/ruff_linter/src/rules/pylint/rules/len_test.rs",
                107,
                141,
                "ruff_linter/rules/pylint/rules/len_test/len_test",
            ),
        }

    hints = preload_symbol_hints(
        _Nav(),
        [
            {
                "file": "crates/ruff_linter/src/rules/pylint/rules/len_test.rs",
                "start": 107,
                "end": 141,
            },
            {
                "file": (
                    "crates/ruff_linter/src/rules/perflint/rules/"
                    "unnecessary_list_cast.rs"
                ),
                "start": 43,
                "end": 45,
            },
        ],
        max_symbols=1,
        query=(
            "inefficient wrapping of iterable collection values requires no "
            "list conversion"
        ),
    )

    assert hints[0] == "unnecessary_list_cast"


def test_preload_symbol_hints_extracts_rust_impl_method_leafs():
    class _Nav:
        file_to_idx = {
            "crates/ruff_python_formatter/src/string/docstring.rs": [0, 1, 2]
        }
        idx_to_span = {
            0: (
                "crates/ruff_python_formatter/src/string/docstring.rs",
                271,
                292,
                "ruff_python_formatter/string/docstring/"
                "impl#[`DocstringLinePrinter<'_, '_, '_, 'src>`]add_iter",
            ),
            1: (
                "crates/ruff_python_formatter/src/string/docstring.rs",
                315,
                382,
                "ruff_python_formatter/string/docstring/"
                "impl#[`DocstringLinePrinter<'_, '_, '_, 'src>`]run_action_queue",
            ),
            2: (
                "crates/ruff_python_formatter/src/string/docstring.rs",
                1379,
                1415,
                "ruff_python_formatter/string/docstring/"
                "impl#[CodeExampleMarkdown]add_code_line",
            ),
        }

    hints = preload_symbol_hints(
        _Nav(),
        [
            {
                "file": "crates/ruff_python_formatter/src/string/docstring.rs",
                "start": 300,
                "end": 313,
            },
            {
                "file": "crates/ruff_python_formatter/src/string/docstring.rs",
                "start": 316,
                "end": 382,
            },
        ],
        max_symbols=4,
        query=(
            "formatting embedded code snippets inside documentation blocks has "
            "duplicated raw fallback lines collected before fallback recovery "
            "path triggers"
        ),
        line_slack=20,
    )

    assert "add_iter" in hints
    assert "run_action_queue" in hints
    assert all("[" not in hint and "]" not in hint for hint in hints)


def test_scheduled_definition_role_quota_promotes_cache_route_neighbor():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=5):
            assert symbols[:3] == ["get_cache_dir", "download_file", "is_url_in_cache"]
            return [
                {
                    "file": "astropy/config/paths.py",
                    "start_line": 124,
                    "end_line": 170,
                    "name": "astropy/config/paths.py:get_cache_dir()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 103,
                    "end_line": 114,
                    "name": "astropy/utils/iers/iers.py:download_file()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1424,
                    "end_line": 1456,
                    "name": "astropy/utils/data.py:is_url_in_cache()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 910,
                    "end_line": 1223,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1627,
                    "end_line": 1692,
                    "name": "astropy/utils/data.py:clear_download_cache()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 997,
                    "end_line": 1076,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.auto_open()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1225,
                    "end_line": 1421,
                    "name": "astropy/utils/data.py:download_file()",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            if seeds[0][0] != "astropy/config/paths.py":
                return []
            return [
                {
                    "file": "astropy/utils/tests/test_data.py",
                    "start_line": 1502,
                    "end_line": 1591,
                    "name": "astropy/utils/tests/test_data.py:test_cache_dir()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1695,
                    "end_line": 1727,
                    "name": "astropy/utils/data.py:_get_download_cache_loc()",
                },
            ]

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "get_cache_dir",
            "download_file",
            "is_url_in_cache",
            "LeapSeconds",
            "clear_download_cache",
            "auto_open",
        ],
        max_nodes=5,
        pool_nodes=7,
        query=(
            "remote leap-second fetching checks cached versions and resolves "
            "the download cache directory location under the user config path"
        ),
    )

    names = [c["name"] for c in candidates]
    assert "astropy/utils/data.py:_get_download_cache_loc()" in names
    assert "astropy/utils/tests/test_data.py:test_cache_dir()" not in names
    assert names.index("astropy/utils/data.py:_get_download_cache_loc()") < names.index(
        "astropy/config/paths.py:get_cache_dir()"
    )
    roles = {c["name"]: c.get("role") for c in candidates}
    assert roles["astropy/utils/data.py:_get_download_cache_loc()"] == "provider"
    assert roles["astropy/utils/iers/iers.py:LeapSeconds.auto_open()"] == "endpoint"


def test_scheduled_definition_role_quota_keeps_provider_over_support():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=5):
            assert "get_cache_dir" not in symbols
            return [
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 103,
                    "end_line": 114,
                    "name": "astropy/utils/iers/iers.py:download_file()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1424,
                    "end_line": 1456,
                    "name": "astropy/utils/data.py:is_url_in_cache()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 1105,
                    "end_line": 1122,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.from_iers_leap_seconds()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 1124,
                    "end_line": 1154,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.from_leap_seconds_list()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 997,
                    "end_line": 1076,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.auto_open()",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            if seeds[0][0] != "astropy/utils/data.py":
                return []
            return [
                {
                    "file": "astropy/utils/tests/test_data.py",
                    "start_line": 1201,
                    "end_line": 1207,
                    "name": "astropy/utils/tests/test_data.py:test_is_url_in_cache_remote()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1695,
                    "end_line": 1727,
                    "name": "astropy/utils/data.py:_get_download_cache_loc()",
                },
            ]

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "download_file",
            "is_url_in_cache",
            "LeapSeconds",
            "from_iers_leap_seconds",
            "from_leap_seconds_list",
            "auto_open",
        ],
        max_nodes=5,
        pool_nodes=5,
        neighbor_nodes=40,
        query=(
            "remote leap-second fetching checks cached versions and resolves "
            "the download cache location"
        ),
    )

    names = [c["name"] for c in candidates]
    assert "astropy/utils/data.py:_get_download_cache_loc()" in names
    assert "astropy/utils/data.py:is_url_in_cache()" in names
    assert names.index("astropy/utils/data.py:is_url_in_cache()") < names.index(
        "astropy/utils/iers/iers.py:LeapSeconds.from_iers_leap_seconds()"
    )


def test_scheduled_definition_role_quota_prioritizes_type_base_and_variants():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=5):
            return [
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ruff_python_ast/nodes/ExprTuple",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 565,
                    "end_line": 634,
                    "name": "ruff_python_ast/nodes/Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2824,
                    "end_line": 2830,
                    "name": "ruff_python_ast/nodes/ExprList",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        ["ExprTuple", "Expr", "ExprList"],
        max_nodes=3,
        query="match iteration targets for tuple and list AST expressions",
    )

    assert [c["name"] for c in candidates] == [
        "ruff_python_ast/nodes/Expr",
        "ruff_python_ast/nodes/ExprTuple",
        "ruff_python_ast/nodes/ExprList",
    ]


def test_scheduled_definition_role_quota_reserves_type_heavy_query_endpoint():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=8):
            return [
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 1121,
                    "end_line": 1127,
                    "name": "ruff_python_ast/nodes/ExprCall",
                },
                {
                    "file": (
                        "crates/ruff_linter/src/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element.rs"
                    ),
                    "start_line": 156,
                    "end_line": 264,
                    "name": (
                        "ruff_linter/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element/"
                        "match_iteration_target"
                    ),
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ruff_python_ast/nodes/ExprTuple",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 565,
                    "end_line": 634,
                    "name": "ruff_python_ast/nodes/Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2824,
                    "end_line": 2830,
                    "name": "ruff_python_ast/nodes/ExprList",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2880,
                    "end_line": 2886,
                    "name": "ruff_python_ast/nodes/ExprSet",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            if seeds[0][1] != "ExprList":
                return []
            return [
                {
                    "file": (
                        "crates/ruff_linter/src/rules/flake8_simplify/rules/"
                        "split_static_string.rs"
                    ),
                    "start_line": 118,
                    "end_line": 136,
                    "name": (
                        "ruff_linter/rules/flake8_simplify/rules/"
                        "split_static_string/construct_replacement"
                    ),
                }
            ]

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "Expr",
            "ExprCall",
            "match_iteration_target",
            "ExprTuple",
            "ExprList",
            "ExprSet",
        ],
        max_nodes=5,
        query="match iteration target tuple list set AST expression variants",
    )

    names = [c["name"] for c in candidates]
    assert (
        "ruff_linter/rules/ruff/rules/"
        "unnecessary_iterable_allocation_for_first_element/match_iteration_target"
    ) in names
    assert names[:4] == [
        "ruff_python_ast/nodes/Expr",
        "ruff_python_ast/nodes/ExprTuple",
        "ruff_python_ast/nodes/ExprList",
        "ruff_python_ast/nodes/ExprSet",
    ]
    assert sum("ruff_python_ast/nodes/Expr" in name for name in names) == 4
    assert not any("construct_replacement" in name for name in names)


def test_scheduled_definition_role_quota_avoids_weak_semantic_types_first():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=12):
            return [
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 1121,
                    "end_line": 1127,
                    "name": "ruff_python_ast/nodes/ExprCall",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2804,
                    "end_line": 2810,
                    "name": "ruff_python_ast/nodes/ExprName",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 565,
                    "end_line": 634,
                    "name": "ruff_python_ast/nodes/Expr",
                },
                {
                    "file": "crates/ruff_python_semantic/src/nodes.rs",
                    "start_line": 86,
                    "end_line": 86,
                    "name": "ruff_python_semantic/nodes/NodeRef#Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 100,
                    "end_line": 101,
                    "name": "ruff_python_ast/nodes/Stmt#Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/node.rs",
                    "start_line": 8106,
                    "end_line": 8106,
                    "name": "ruff_python_ast/node/StatementRef#Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ruff_python_ast/nodes/ExprTuple",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2824,
                    "end_line": 2830,
                    "name": "ruff_python_ast/nodes/ExprList",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2880,
                    "end_line": 2886,
                    "name": "ruff_python_ast/nodes/ExprSet",
                },
                {
                    "file": (
                        "crates/ruff_linter/src/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element.rs"
                    ),
                    "start_line": 156,
                    "end_line": 264,
                    "name": (
                        "ruff_linter/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element/"
                        "match_iteration_target"
                    ),
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "Expr",
            "ExprCall",
            "ExprName",
            "match_iteration_target",
            "ExprTuple",
            "unnecessary_list_cast",
            "ExprList",
            "ExprSet",
        ],
        max_nodes=8,
        pool_nodes=10,
        query=(
            "ordered sequence literals passed as arguments and inner argument "
            "collection values"
        ),
    )

    names = [c["name"] for c in candidates]
    assert names[:4] == [
        "ruff_python_ast/nodes/Expr",
        "ruff_python_ast/nodes/ExprTuple",
        "ruff_python_ast/nodes/ExprList",
        (
            "ruff_linter/rules/ruff/rules/"
            "unnecessary_iterable_allocation_for_first_element/match_iteration_target"
        ),
    ]
    assert "ruff_python_ast/nodes/ExprSet" not in names
    assert (
        "ruff_linter/rules/ruff/rules/"
        "unnecessary_iterable_allocation_for_first_element/match_iteration_target"
    ) in names
    assert not any("NodeRef#Expr" in name for name in names)
    assert not any("StatementRef#Expr" in name for name in names)


def test_scheduled_definition_role_quota_prefers_query_leaf_endpoint_with_types():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=12):
            return [
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 565,
                    "end_line": 634,
                    "name": "ruff_python_ast/nodes/Expr",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ruff_python_ast/nodes/ExprTuple",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2824,
                    "end_line": 2830,
                    "name": "ruff_python_ast/nodes/ExprList",
                },
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 2880,
                    "end_line": 2886,
                    "name": "ruff_python_ast/nodes/ExprSet",
                },
                {
                    "file": (
                        "crates/ruff_linter/src/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element.rs"
                    ),
                    "start_line": 156,
                    "end_line": 264,
                    "name": (
                        "ruff_linter/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element/"
                        "match_iteration_target"
                    ),
                },
                {
                    "file": (
                        "crates/ruff_linter/src/rules/perflint/rules/"
                        "unnecessary_list_cast.rs"
                    ),
                    "start_line": 53,
                    "end_line": 124,
                    "name": (
                        "ruff_linter/rules/perflint/rules/unnecessary_list_cast/"
                        "unnecessary_list_cast"
                    ),
                },
                {
                    "file": (
                        "crates/ruff_linter/src/rules/ruff/rules/"
                        "unnecessary_literal_within_deque_call.rs"
                    ),
                    "start_line": 54,
                    "end_line": 104,
                    "name": (
                        "ruff_linter/rules/ruff/rules/"
                        "unnecessary_literal_within_deque_call/"
                        "unnecessary_literal_within_deque_call"
                    ),
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "Expr",
            "ExprTuple",
            "ExprList",
            "ExprSet",
            "match_iteration_target",
            "unnecessary_list_cast",
            "unnecessary_literal_within_deque_call",
        ],
        max_nodes=5,
        query=(
            "inefficient wrapping of an iterable collection inside a loop "
            "recognizes ordered sequence literals passed as arguments and "
            "avoids list conversion"
        ),
    )

    names = [c["name"] for c in candidates]
    assert names == [
        "ruff_python_ast/nodes/Expr",
        "ruff_python_ast/nodes/ExprTuple",
        "ruff_python_ast/nodes/ExprList",
        (
            "ruff_linter/rules/perflint/rules/unnecessary_list_cast/"
            "unnecessary_list_cast"
        ),
    ]
    assert "ruff_python_ast/nodes/ExprSet" not in names
    assert not any("unnecessary_literal_within_deque_call" in name for name in names)


def test_scheduled_definition_role_quota_keeps_late_query_endpoint_with_types():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=10):
            return [
                {
                    "file": "crates/ruff_python_ast/src/nodes.rs",
                    "start_line": 1121 + i,
                    "end_line": 1127 + i,
                    "name": f"ruff_python_ast/nodes/ExprType{i}",
                }
                for i in range(8)
            ] + [
                {
                    "file": (
                        "crates/ruff_linter/src/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element.rs"
                    ),
                    "start_line": 156,
                    "end_line": 264,
                    "name": (
                        "ruff_linter/rules/ruff/rules/"
                        "unnecessary_iterable_allocation_for_first_element/"
                        "match_iteration_target"
                    ),
                }
            ]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "Expr",
            "ExprCall",
            "ExprName",
            "match_iteration_target",
            "ExprTuple",
            "unnecessary_list_cast",
            "ExprList",
            "ExprSet",
        ],
        max_nodes=8,
        pool_nodes=10,
        query="match iteration target tuple list set AST expression variants",
    )

    names = [c["name"] for c in candidates]
    assert (
        "ruff_linter/rules/ruff/rules/"
        "unnecessary_iterable_allocation_for_first_element/match_iteration_target"
    ) in names
    assert len(names) < 8
    assert sum("ruff_python_ast/nodes/ExprType" in name for name in names) <= 3


def test_scheduled_definition_role_quota_keeps_go_direct_bridges():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=8):
            return [
                {
                    "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                    "start_line": 42,
                    "end_line": 91,
                    "name": "modules/caddyhttp/reverseproxy/HealthChecks",
                },
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                },
                {
                    "file": "replacer.go",
                    "start_line": 73,
                    "end_line": 80,
                    "name": "ReplaceAll",
                },
                {
                    "file": "replacer.go",
                    "start_line": 112,
                    "end_line": 126,
                    "name": "ReplaceKnown",
                },
                {
                    "file": "modules/caddyhttp/reverseproxy/hosts.go",
                    "start_line": 102,
                    "end_line": 125,
                    "name": "modules/caddyhttp/reverseproxy/Upstream#fillDialInfo",
                },
                {
                    "file": "caddyconfig/httpcaddyfile/addresses.go",
                    "start_line": 301,
                    "end_line": 320,
                    "name": "ParseNetworkAddress",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            if seeds[0][1] == "ReplaceAll":
                return [
                    {
                        "file": "modules/caddyhttp/fileserver/staticfiles.go",
                        "start_line": 553,
                        "end_line": 565,
                        "name": "modules/caddyhttp/fileserver/FileServer#transformHidePaths",
                    },
                    {
                        "file": "replacer.go",
                        "start_line": 297,
                        "end_line": 337,
                        "name": "globalDefaultReplacements",
                    },
                ]
            if seeds[0][1] == "NewReplacer":
                return [
                    {
                        "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                        "start_line": 308,
                        "end_line": 448,
                        "name": "modules/caddyhttp/reverseproxy/Handler#doActiveHealthCheck",
                    }
                ]
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "NewReplacer",
            "HealthChecks",
            "ReplaceAll",
            "ReplaceKnown",
            "ParseNetworkAddress",
        ],
        max_nodes=7,
        query=(
            "reverse proxy health checking loop resolves placeholder tokens "
            "and bridges template variable substitution to provider values"
        ),
    )

    names = [c["name"] for c in candidates]
    assert "ReplaceAll" in names
    assert "ReplaceKnown" in names
    assert "globalDefaultReplacements" in names
    assert "modules/caddyhttp/reverseproxy/Handler#doActiveHealthCheck" in names
    assert not any("transformHidePaths" in name for name in names)


def test_scheduled_definition_role_quota_keeps_cache_check_over_property_noise():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=8):
            return [
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 937,
                    "end_line": 937,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds._auto_open_files",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 997,
                    "end_line": 1076,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.auto_open()",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 103,
                    "end_line": 114,
                    "name": "astropy/utils/iers/iers.py:download_file()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1695,
                    "end_line": 1727,
                    "name": "astropy/utils/data.py:_get_download_cache_loc()",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1424,
                    "end_line": 1456,
                    "name": "astropy/utils/data.py:is_url_in_cache()",
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "_auto_open_files",
            "auto_open",
            "download_file",
            "_get_download_cache_loc",
            "is_url_in_cache",
        ],
        max_nodes=4,
        query=(
            "remote file opening checks cached URLs before resolving the "
            "download cache directory"
        ),
    )

    names = [c["name"] for c in candidates]
    assert "astropy/utils/data.py:is_url_in_cache()" in names
    assert "astropy/utils/iers/iers.py:LeapSeconds._auto_open_files" not in names


def test_scheduled_definition_role_quota_keeps_health_loop_over_type_noise():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=8):
            return [
                {
                    "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                    "start_line": 36,
                    "end_line": 36,
                    "name": "modules/caddyhttp/reverseproxy/HealthChecks",
                },
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                },
                {
                    "file": "replacer.go",
                    "start_line": 297,
                    "end_line": 337,
                    "name": "globalDefaultReplacements",
                },
                {
                    "file": "listeners.go",
                    "start_line": 216,
                    "end_line": 221,
                    "name": "NetworkAddress#JoinHostPort",
                },
                {
                    "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                    "start_line": 243,
                    "end_line": 299,
                    "name": (
                        "modules/caddyhttp/reverseproxy/Handler#"
                        "doActiveHealthCheckForAllHosts"
                    ),
                },
            ][:max_nodes]

        def neighbors(self, seeds, max_nodes=10):
            return []

    candidates = scheduled_definition_candidates_for_symbols(
        _Nav(),
        [
            "HealthChecks",
            "NewReplacer",
            "globalDefaultReplacements",
            "JoinHostPort",
            "doActiveHealthCheckForAllHosts",
        ],
        max_nodes=4,
        query=(
            "reverse proxy health checking loop initiates address substitution "
            "and bridges placeholder tokens to provider values"
        ),
    )

    names = [c["name"] for c in candidates]
    assert (
        "modules/caddyhttp/reverseproxy/Handler#doActiveHealthCheckForAllHosts" in names
    )
    assert "modules/caddyhttp/reverseproxy/HealthChecks" not in names


def test_graph_nav_definition_symbols_prioritize_exact_nodes_definitions():
    class _Vertex:
        def __init__(self, index, **attrs):
            self.index = index
            self._attrs = attrs

        def attributes(self):
            return self._attrs

    class _Graph:
        def __init__(self):
            self.vs = [
                _Vertex(
                    0,
                    file="src/node.rs",
                    start_line=10,
                    end_line=10,
                    name="ruff_python_ast/node/NodeKind#ExprTuple",
                ),
                _Vertex(
                    1,
                    file="src/nodes.rs",
                    start_line=100,
                    end_line=110,
                    name="ruff_python_ast/nodes/ExprTuple",
                ),
                _Vertex(
                    2,
                    file="src/nodes.rs",
                    start_line=120,
                    end_line=128,
                    name="ruff_python_ast/nodes/ExprList",
                ),
            ]

    nodes = GraphNav(_Graph()).definitions_for_symbols(
        ["ExprTuple", "ExprList"], max_nodes=2
    )

    assert nodes == [
        {
            "file": "src/nodes.rs",
            "start_line": 101,
            "end_line": 111,
            "name": "ruff_python_ast/nodes/ExprTuple",
        },
        {
            "file": "src/nodes.rs",
            "start_line": 121,
            "end_line": 129,
            "name": "ruff_python_ast/nodes/ExprList",
        },
    ]


def test_graph_context_scheduler_injects_read_symbol_definitions():
    class _Nav:
        def __init__(self):
            self.definition_calls = []

        def definitions_for_symbols(self, symbols, max_nodes=5):
            self.definition_calls.append((symbols, max_nodes))
            return [
                {
                    "file": "src/nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ExprTuple",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [{"file": "src/rule.rs", "start": 50, "end": 120}],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_symbols": 4,
            "graph_read_symbol_max_nodes": 3,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 1,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "src/rule.rs",
                    "content": "    76 | matches!(expr, Expr::Tuple(_))",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["source"] == "scheduled_graph_lsp"
    assert "Related definitions" in injected["content"]
    assert "first five comma-separated entries" in injected["content"]
    assert "src/nodes.rs:2861-2870 ExprTuple" in injected["content"]
    assert injected["metadata"]["reason"] == "read_symbols"
    assert injected["metadata"]["symbol_count"] == 2
    assert injected["metadata"]["node_count"] == 1
    assert nav.definition_calls == [(["ExprTuple", "Expr"], 3)]
    assert state["triggered"] is True
    assert state["reason"] == "read_symbols"
    assert state["symbol_count"] == 2


def test_graph_context_scheduler_injects_generic_read_symbol_definitions():
    class _Nav:
        def __init__(self):
            self.definition_calls = []

        def definitions_for_symbols(self, symbols, max_nodes=5):
            self.definition_calls.append((symbols, max_nodes))
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                },
                {
                    "file": "replacer.go",
                    "start_line": 104,
                    "end_line": 106,
                    "name": "Replacer#ReplaceOrErr",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [{"file": "modules/caddyhttp/reverseproxy/healthchecks.go"}],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_symbols": 4,
            "graph_read_symbol_max_nodes": 3,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "healthchecks.go",
                    "content": (
                        " 254 | networkAddr, err := "
                        "caddy.NewReplacer().ReplaceOrErr(upstream.Dial, true, true)"
                    ),
                }
            ],
        }
    )

    assert injected is not None
    assert "replacer.go:29-38 NewReplacer" in injected["content"]
    assert "small helper methods" in injected["content"]
    assert injected["metadata"]["reason"] == "read_symbols"
    assert injected["metadata"]["symbols"][:2] == ["NewReplacer", "ReplaceOrErr"]
    assert nav.definition_calls == [(["NewReplacer", "ReplaceOrErr"], 3)]
    assert state["triggered"] is True
    assert state["symbol_count"] == 2


def test_graph_context_scheduler_prefers_lsp_route_operation():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(
                (symbols, max_nodes, query, pool_nodes, neighbor_nodes)
            )
            return [
                {
                    "file": "health.go",
                    "start_line": 308,
                    "end_line": 448,
                    "name": "HealthChecks",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "defaults.go",
                    "start_line": 12,
                    "end_line": 24,
                    "name": "globalDefaultReplacements",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "NewReplacer",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_symbols": 4,
            "graph_read_symbol_max_nodes": 3,
            "graph_read_symbol_pool_nodes": 10,
            "graph_read_symbol_neighbor_nodes": 20,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "health check placeholder replacement defaults",
            "tool_calls": [
                ToolCallRecord(
                    tool_call_id="call_1",
                    skill_id="read",
                    arguments={
                        "file_path": "healthchecks.go",
                        "offset": 308,
                        "limit": 120,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_2",
                    skill_id="read",
                    arguments={
                        "file_path": "replacer.go",
                        "offset": 29,
                        "limit": 80,
                    },
                ),
            ],
            "read_outputs": [
                {
                    "path": "healthchecks.go",
                    "content": (
                        " 254 | networkAddr, err := "
                        "caddy.NewReplacer().ReplaceOrErr(upstream.Dial, true, true)"
                    ),
                }
            ],
        }
    )

    assert injected is not None
    assert "Related route anchors" in injected["content"]
    assert "Read-window plan" not in injected["content"]
    assert "health.go:308-448 HealthChecks [endpoint; direct_definition]" in (
        injected["content"]
    )
    assert (
        "defaults.go:12-24 globalDefaultReplacements "
        "[provider; route_neighbor; via NewReplacer]"
    ) in injected["content"]
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["route_roles"] == ["endpoint", "provider"]
    assert nav.route_calls == [
        (
            ["NewReplacer", "ReplaceOrErr"],
            3,
            "health check placeholder replacement defaults",
            10,
            20,
        )
    ]
    assert state["operation"] == "lsp_route"


def test_graph_context_scheduler_budgets_wide_traceback_lsp_route():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append((max_nodes, pool_nodes, neighbor_nodes))
            return [
                {
                    "file": "docstring.rs",
                    "start_line": 315,
                    "end_line": 382,
                    "name": "run_action_queue",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 271,
                    "end_line": 292,
                    "name": "collect_fallback_lines",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1379,
                    "end_line": 1415,
                    "name": "add_code_line",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1536,
                    "end_line": 1572,
                    "name": "resolve_reset_state",
                    "role": "provider",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 90,
                    "end_line": 95,
                    "name": "format_helper",
                    "role": "support",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, _state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_read_symbol_max_nodes": 3,
            "graph_read_symbol_traceback_max_nodes": 8,
            "graph_read_symbol_traceback_route_budget_nodes": 3,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                "embedded code snippets duplicated raw fallback lines collected "
                "before fallback recovery path triggers"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"path": "docstring.rs", "content": " 330 | printer.add_iter(lines)"}
            ],
        }
    )

    assert injected is not None
    assert nav.route_calls == [(8, 30, 80)]
    assert injected["metadata"]["raw_node_count"] == 5
    assert injected["metadata"]["node_count"] == 3
    assert injected["metadata"]["route_budget_applied"] is True
    assert injected["metadata"]["route_budget_reason"] == "explicit"
    assert injected["metadata"]["route_budget_target"] == 3
    assert injected["metadata"]["route_budget_dropped"] == 2
    assert len(injected["metadata"]["locations"]) == 3
    assert "docstring.rs:315-382 run_action_queue" in injected["content"]
    assert "docstring.rs:90-95 format_helper" not in injected["content"]


def test_traceback_route_budget_preserves_via_provider_anchor():
    locations = [
        {
            "file": "iers.py",
            "start_line": 997,
            "end_line": 1076,
            "name": "LeapSeconds.auto_open()",
            "role": "endpoint",
            "source": "route_neighbor",
            "via": "is_url_in_cache",
        },
        {
            "file": "iers.py",
            "start_line": 679,
            "end_line": 739,
            "name": "IERS_Auto.open()",
            "role": "endpoint",
            "source": "route_neighbor",
            "via": "download_file",
        },
        {
            "file": "data.py",
            "start_line": 1225,
            "end_line": 1421,
            "name": "download_file()",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "data.py",
            "start_line": 1762,
            "end_line": 1845,
            "name": "check_download_cache()",
            "role": "provider",
            "source": "direct_definition",
        },
        {
            "file": "data.py",
            "start_line": 1424,
            "end_line": 1456,
            "name": "is_url_in_cache()",
            "role": "provider",
            "source": "direct_definition",
        },
    ]

    budgeted = _scheduled_route_budget_locations(
        locations,
        max_locations=4,
        query_terms={"cache", "download", "file"},
        route_traceback=True,
    )

    names = [loc["name"] for loc in budgeted]
    assert "is_url_in_cache()" in names
    assert "check_download_cache()" not in names


def test_traceback_route_budget_preserves_via_bridge_anchor():
    locations = [
        {
            "file": "healthchecks.go",
            "start_line": 243,
            "end_line": 299,
            "name": "Handler#doActiveHealthCheckForAllHosts",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "replacer.go",
            "start_line": 29,
            "end_line": 38,
            "name": "NewReplacer",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "replacer.go",
            "start_line": 297,
            "end_line": 337,
            "name": "globalDefaultReplacements",
            "role": "provider",
            "source": "route_neighbor",
            "via": "NewReplacer",
        },
        {
            "file": "listeners.go",
            "start_line": 308,
            "end_line": 310,
            "name": "ParseNetworkAddress",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "reverseproxy.go",
            "start_line": 461,
            "end_line": 560,
            "name": "Handler#proxyLoopIteration",
            "role": "endpoint",
            "source": "direct_definition",
        },
    ]

    budgeted = _scheduled_route_budget_locations(
        locations,
        max_locations=3,
        query_terms={"health", "replacement", "default"},
        route_traceback=True,
    )

    names = [loc["name"] for loc in budgeted]
    assert "globalDefaultReplacements" in names
    assert "NewReplacer" in names


def test_dynamic_traceback_route_budget_refuses_missing_bridge_type_core():
    locations = [
        {
            "file": "handler.go",
            "start_line": 10,
            "end_line": 40,
            "name": "Handler#doActiveHealthCheckForAllHosts",
            "role": "endpoint",
            "source": "route_neighbor",
        },
        {
            "file": "healthchecks.go",
            "start_line": 50,
            "end_line": 90,
            "name": "Handler#checkHostHealth",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "replacer.go",
            "start_line": 100,
            "end_line": 130,
            "name": "Replacer#ReplaceKnown",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "library.go",
            "start_line": 140,
            "end_line": 190,
            "name": "ServerType#compileEncodedMatcherSets",
            "role": "type",
            "source": "direct_definition",
        },
    ]

    budgeted, metadata = _scheduled_route_dynamic_budget_locations(
        locations,
        target_locations=3,
        query_terms={"health", "check"},
        route_traceback=True,
    )

    assert budgeted == locations
    assert metadata["applied"] is False
    assert metadata["reason"] == "unsafe_missing_core"
    assert metadata["missing_required_count"] == 1
    assert metadata["missing_required"][0]["name"] == (
        "ServerType#compileEncodedMatcherSets"
    )


def test_dynamic_traceback_route_budget_trims_support_noise():
    locations = [
        {
            "file": "iers.py",
            "start_line": 997,
            "end_line": 1076,
            "name": "LeapSeconds.auto_open()",
            "role": "endpoint",
            "source": "route_neighbor",
            "via": "is_url_in_cache",
        },
        {
            "file": "data.py",
            "start_line": 1225,
            "end_line": 1421,
            "name": "download_file()",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "data.py",
            "start_line": 1424,
            "end_line": 1456,
            "name": "is_url_in_cache()",
            "role": "provider",
            "source": "direct_definition",
        },
        {
            "file": "iers.py",
            "start_line": 937,
            "end_line": 937,
            "name": "_auto_open_files",
            "role": "support",
            "source": "direct_definition",
        },
        {
            "file": "iers/tests/test_leap_second.py",
            "start_line": 228,
            "end_line": 228,
            "name": "TestDefaultAutoOpen._auto_open_files",
            "role": "support",
            "source": "direct_definition",
        },
    ]

    budgeted, metadata = _scheduled_route_dynamic_budget_locations(
        locations,
        target_locations=3,
        query_terms={"cache", "download", "file"},
        route_traceback=True,
    )

    assert metadata["applied"] is True
    assert metadata["reason"] == "target_budget"
    assert metadata["dropped_count"] == 2
    assert [loc["name"] for loc in budgeted] == [
        "LeapSeconds.auto_open()",
        "download_file()",
        "is_url_in_cache()",
    ]


def test_traceback_route_prompt_order_ranks_causal_line_anchors_without_trimming():
    locations = [
        {
            "file": "docstring.rs",
            "start_line": 315,
            "end_line": 382,
            "name": "DocstringLinePrinter#run_action_queue",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "docstring.rs",
            "start_line": 1379,
            "end_line": 1415,
            "name": "CodeExampleMarkdown#add_code_line",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "docstring.rs",
            "start_line": 1536,
            "end_line": 1572,
            "name": "CodeExampleAddAction",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "docstring.rs",
            "start_line": 271,
            "end_line": 292,
            "name": "DocstringLinePrinter#add_iter",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "docstring.rs",
            "start_line": 905,
            "end_line": 949,
            "name": "CodeExampleDoctest#add_code_line",
            "role": "endpoint",
            "source": "direct_definition",
        },
    ]

    ordered = _scheduled_route_prompt_locations(
        locations,
        query_terms={"fallback", "recovery", "line", "collected"},
        route_traceback=True,
    )

    assert len(ordered) == len(locations)
    assert [loc["name"] for loc in ordered[:2]] == [
        "CodeExampleMarkdown#add_code_line",
        "CodeExampleDoctest#add_code_line",
    ]


def test_dynamic_traceback_route_budget_falls_back_to_support_only_trim():
    locations = [
        {
            "file": "iers.py",
            "start_line": 997,
            "end_line": 1076,
            "name": "LeapSeconds.auto_open()",
            "role": "endpoint",
            "source": "route_neighbor",
            "via": "is_url_in_cache",
        },
        {
            "file": "data.py",
            "start_line": 1225,
            "end_line": 1421,
            "name": "download_file()",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "data.py",
            "start_line": 1424,
            "end_line": 1456,
            "name": "is_url_in_cache()",
            "role": "provider",
            "source": "direct_definition",
        },
        {
            "file": "iers.py",
            "start_line": 937,
            "end_line": 937,
            "name": "_auto_open_files",
            "role": "support",
            "source": "direct_definition",
        },
    ]

    budgeted, metadata = _scheduled_route_dynamic_budget_locations(
        locations,
        target_locations=2,
        query_terms={"cache", "download", "file"},
        route_traceback=True,
    )

    assert metadata["applied"] is True
    assert metadata["reason"] == "support_noise_only"
    assert metadata["target"] == 3
    assert [loc["name"] for loc in budgeted] == [
        "LeapSeconds.auto_open()",
        "download_file()",
        "is_url_in_cache()",
    ]


def test_dynamic_route_budget_reports_missing_core_anchors():
    locations = [
        {
            "file": "handler.go",
            "start_line": 10,
            "end_line": 40,
            "name": "Handler#doActiveHealthCheckForAllHosts",
            "role": "endpoint",
            "source": "route_neighbor",
        },
        {
            "file": "healthchecks.go",
            "start_line": 50,
            "end_line": 90,
            "name": "Handler#checkHostHealth",
            "role": "endpoint",
            "source": "direct_definition",
        },
        {
            "file": "replacer.go",
            "start_line": 100,
            "end_line": 130,
            "name": "NewReplacer",
            "role": "bridge",
            "source": "direct_definition",
        },
        {
            "file": "library.go",
            "start_line": 140,
            "end_line": 190,
            "name": "NewMatcherCELLibrary",
            "role": "type",
            "source": "direct_definition",
        },
    ]

    budgeted, metadata = _scheduled_route_dynamic_budget_locations(
        locations,
        target_locations=1,
        query_terms={"health", "check"},
        route_traceback=True,
    )

    assert budgeted == locations
    assert metadata["applied"] is False
    assert metadata["reason"] == "unsafe_missing_core"
    assert metadata["missing_required_count"] == 2
    assert [loc["name"] for loc in metadata["missing_required"]] == [
        "NewReplacer",
        "NewMatcherCELLibrary",
    ]


def test_graph_context_scheduler_auto_opens_priority_lsp_route_anchor(tmp_path):
    health = tmp_path / "health.go"
    health.write_text(
        "\n".join(f"line {idx}" for idx in range(1, 40)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "health.go",
                    "start_line": 5,
                    "end_line": 5,
                    "name": "Handler#activeHealthCheckPort",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "health.go",
                    "start_line": 20,
                    "end_line": 25,
                    "name": "Handler#doActiveHealthCheckForAllHosts",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "active health check replacement",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "health.go",
                    "content": " 20 | NewReplacer().ReplaceOrErr(upstream.Dial)",
                }
            ],
        }
    )

    assert injected is not None
    assert "Auto-opened priority route source" in injected["content"]
    assert "health.go:20-25 Handler#doActiveHealthCheckForAllHosts" in (
        injected["content"]
    )
    assert "    20 | line 20" in injected["content"]
    assert "... (3 more lines in anchor)" in injected["content"]
    assert injected["metadata"]["auto_open_count"] == 1
    assert injected["metadata"]["auto_open_locations"][0]["start_line"] == 20


def test_graph_context_scheduler_auto_open_prefers_route_neighbor_core(tmp_path):
    health = tmp_path / "health.go"
    health.write_text(
        "\n".join(f"health {idx}" for idx in range(1, 80)),
        encoding="utf-8",
    )
    replacer = tmp_path / "replacer.go"
    replacer.write_text(
        "\n".join(f"replacer {idx}" for idx in range(1, 160)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "health.go",
                    "start_line": 20,
                    "end_line": 25,
                    "name": "Handler#doActiveHealthCheckForAllHosts",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 47,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 110,
                    "end_line": 125,
                    "name": "globalDefaultReplacements",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "NewReplacer",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "active health check replacement",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "health.go",
                    "content": " 20 | NewReplacer().ReplaceOrErr(upstream.Dial)",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 1
    assert injected["metadata"]["auto_open_locations"][0]["name"] == (
        "globalDefaultReplacements"
    )
    assert injected["metadata"]["auto_open_locations"][0]["source"] == (
        "route_neighbor"
    )
    assert "replacer.go:110-125 globalDefaultReplacements" in injected["content"]
    assert "health.go:20-25 Handler#doActiveHealthCheckForAllHosts" not in (
        injected["content"].split("Auto-opened priority route source", 1)[1]
    )


def test_graph_context_scheduler_auto_open_prefers_route_bridge_via(tmp_path):
    health = tmp_path / "health.go"
    health.write_text(
        "\n".join(f"health {idx}" for idx in range(1, 80)),
        encoding="utf-8",
    )
    replacer = tmp_path / "replacer.go"
    replacer.write_text(
        "\n".join(f"replacer {idx}" for idx in range(1, 160)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "health.go",
                    "start_line": 20,
                    "end_line": 25,
                    "name": "Handler#doActiveHealthCheckForAllHosts",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 47,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 110,
                    "end_line": 110,
                    "name": "globalDefaultReplacements",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "NewReplacer",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "active health check replacement",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "health.go",
                    "content": " 20 | NewReplacer().ReplaceOrErr(upstream.Dial)",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 1
    assert injected["metadata"]["auto_open_locations"][0]["name"] == "NewReplacer"
    assert injected["metadata"]["auto_open_locations"][0]["role"] == "bridge"
    assert "replacer.go:29-47 NewReplacer" in injected["content"]


def test_graph_context_scheduler_auto_open_prefers_protected_bridge_over_endpoint(
    tmp_path,
):
    listeners = tmp_path / "listeners.go"
    listeners.write_text(
        "\n".join(f"listener {idx}" for idx in range(1, 260)),
        encoding="utf-8",
    )
    replacer = tmp_path / "replacer.go"
    replacer.write_text(
        "\n".join(f"replacer {idx}" for idx in range(1, 180)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "listeners.go",
                    "start_line": 216,
                    "end_line": 221,
                    "name": "NetworkAddress#JoinHostPort",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 120,
                    "end_line": 123,
                    "name": "Replacer#ReplaceAll",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 111,
                    "end_line": 114,
                    "name": "Replacer#ReplaceKnown",
                    "role": "bridge",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "placeholder replacement variable expansion path",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "replacer.go",
                    "content": " 20 | NewReplacer().ReplaceAll(input)",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 1
    assert injected["metadata"]["auto_open_locations"][0]["name"] == (
        "Replacer#ReplaceAll"
    )
    assert injected["metadata"]["auto_open_locations"][0]["role"] == "bridge"
    assert "replacer.go:120-123 Replacer#ReplaceAll" in injected["content"]
    assert "listeners.go:216-221 NetworkAddress#JoinHostPort" not in (
        injected["content"].split("Auto-opened priority route source", 1)[1]
    )


def test_graph_context_scheduler_auto_open_skips_already_read_route_anchor(tmp_path):
    health = tmp_path / "health.go"
    health.write_text(
        "\n".join(f"line {idx}" for idx in range(1, 50)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "health.go",
                    "start_line": 20,
                    "end_line": 25,
                    "name": "Handler#doActiveHealthCheckForAllHosts",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "health.go",
                    "start_line": 30,
                    "end_line": 35,
                    "name": "Handler#replacementDefaults",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    run_state = {
        "turn": 2,
        "query": "active health check replacement",
        "tool_calls": [
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "health.go", "offset": 20, "limit": 6},
            ),
            ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
        ],
        "read_outputs": [
            {
                "path": "health.go",
                "content": " 20 | NewReplacer().ReplaceOrErr(upstream.Dial)",
            }
        ],
    }

    injected = scheduler(run_state)

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 1
    assert injected["metadata"]["auto_open_locations"][0]["start_line"] == 30
    assert injected["metadata"]["route_read_confirmed_count"] == 2
    assert injected["metadata"]["route_read_confirmed_core_count"] == 2
    assert injected["metadata"]["route_unread_core_count"] == 0
    assert [
        loc["start_line"]
        for loc in injected["metadata"]["route_read_confirmed_locations"]
    ] == [20, 30]
    assert injected["metadata"]["route_unread_core_locations"] == []
    assert "    20 | line 20" not in injected["content"]
    assert "    30 | line 30" in injected["content"]


def test_graph_context_scheduler_auto_opens_traceback_guard_order(tmp_path):
    docstring = tmp_path / "docstring.rs"
    docstring.write_text(
        "\n".join(f"line {idx}" for idx in range(1, 1600)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "docstring.rs",
                    "start_line": 315,
                    "end_line": 382,
                    "name": "run_action_queue",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1379,
                    "end_line": 1415,
                    "name": "add_code_line",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1536,
                    "end_line": 1572,
                    "name": "CodeExampleAddAction",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 271,
                    "end_line": 292,
                    "name": "add_iter",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 2,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                "embedded code snippets duplicated raw fallback lines collected "
                "before fallback recovery path triggers"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"path": "docstring.rs", "content": " 330 | printer.add_iter(lines)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 2
    assert [loc["name"] for loc in injected["metadata"]["auto_open_locations"]] == [
        "run_action_queue",
        "add_iter",
    ]
    assert "docstring.rs:271-292 add_iter" in injected["content"]


def test_graph_context_scheduler_auto_opens_base_and_variant_for_type_route(
    tmp_path,
):
    nodes = tmp_path / "nodes.rs"
    nodes.write_text(
        "\n".join(f"line {idx}" for idx in range(1, 3000)),
        encoding="utf-8",
    )
    rule = tmp_path / "rule.rs"
    rule.write_text(
        "\n".join(f"rule {idx}" for idx in range(1, 200)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "nodes.rs",
                    "start_line": 565,
                    "end_line": 634,
                    "name": "Expr",
                    "role": "type",
                    "source": "direct_definition",
                },
                {
                    "file": "nodes.rs",
                    "start_line": 2861,
                    "end_line": 2870,
                    "name": "ExprTuple",
                    "role": "type",
                    "source": "direct_definition",
                },
                {
                    "file": "rule.rs",
                    "start_line": 52,
                    "end_line": 124,
                    "name": "unnecessary_list_cast",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, _state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 2,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "ordered sequence literals",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"path": "rule.rs", "content": " 76 | Expr::Tuple(tuple)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["auto_open_count"] == 2
    opened = injected["metadata"]["auto_open_locations"]
    assert [loc["name"] for loc in opened] == ["Expr", "ExprTuple"]
    assert "nodes.rs:565-634 Expr" in injected["content"]
    assert "nodes.rs:2861-2870 ExprTuple" in injected["content"]

    guard = scheduler(
        {
            "turn": 3,
            "query": "ordered sequence literals",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert "route finalization guard" in guard["content"]
    assert "rule.rs:52-124 unnecessary_list_cast [endpoint]" in guard["content"]
    assert "nodes.rs:565-634 Expr [type]" in guard["content"]
    assert "nodes.rs:2861-2870 ExprTuple [type]" in guard["content"]
    assert guard["metadata"]["operation"] == "lsp_route_finalization_guard"


def test_graph_context_scheduler_routes_with_clean_query():
    class _Nav:
        def __init__(self):
            self.route_queries = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_queries.append(query)
            return [
                {
                    "file": "nodes.rs",
                    "start_line": 10,
                    "end_line": 20,
                    "name": "ExprTuple",
                    "role": "type",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    clean_query = "ordered sequence literals need tuple and list evidence"
    scheduler, _state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_symbols": 4,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
        query=clean_query,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                f"{clean_query}\n\n"
                "Preloaded snippets: ExprCall ExprName deque call name helper"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "nodes.rs",
                    "content": " 10 | Expr::Tuple(ExprTuple { elts })",
                }
            ],
        }
    )

    assert injected is not None
    assert nav.route_queries == [clean_query]
    assert injected["metadata"]["query_chars"] == len(clean_query)


def test_graph_context_scheduler_merges_preload_symbol_fallback():
    class _Nav:
        file_to_idx = {
            "crates/ruff_linter/src/rules/perflint/rules/unnecessary_list_cast.rs": [10]
        }
        idx_to_span = {
            10: (
                "crates/ruff_linter/src/rules/perflint/rules/unnecessary_list_cast.rs",
                53,
                124,
                "ruff_linter/rules/perflint/rules/unnecessary_list_cast/"
                "unnecessary_list_cast",
            )
        }

        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(symbols)
            return [
                {
                    "file": (
                        "crates/ruff_linter/src/rules/perflint/rules/"
                        "unnecessary_list_cast.rs"
                    ),
                    "start_line": 53,
                    "end_line": 124,
                    "name": "unnecessary_list_cast",
                    "role": "endpoint",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [
            {
                "file": (
                    "crates/ruff_linter/src/rules/perflint/rules/"
                    "unnecessary_list_cast.rs"
                ),
                "start": 43,
                "end": 45,
            }
        ],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_symbols": 4,
            "graph_read_symbol_preload_max_symbols": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                "inefficient wrapping of iterable collection values avoids "
                "list conversion"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "wrong_rule.rs",
                    "content": (
                        " 166 | Expr::Call(call) => Some(call),\n"
                        " 240 | Expr::List(list) => Some(list),"
                    ),
                }
            ],
        }
    )

    assert injected is not None
    assert "unnecessary_list_cast" in nav.route_calls[0]
    assert injected["metadata"]["fallback_symbols"] == ["unnecessary_list_cast"]
    assert state["symbol_count"] == len(nav.route_calls[0])


def test_graph_context_scheduler_waits_on_generic_symbols_until_more_reads():
    class _Nav:
        def definitions_for_symbols(self, symbols, max_nodes=5):
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 4,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceOrErr(upstream.Dial)"}
            ],
        }
    )

    assert injected is None
    assert state["triggered"] is False
    assert state["attempted"] is False


def test_graph_context_scheduler_waits_on_default_generic_symbols_until_four_reads():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(symbols)
            return [
                {
                    "file": "rules.rs",
                    "start_line": 10,
                    "end_line": 20,
                    "name": "analyze_collection",
                    "role": "endpoint",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 3,
            "query": "ordered collection analysis for iterable allocation",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
            ],
            "read_outputs": [{"content": " 10 | analyze_collection(expr, values)"}],
        }
    )

    assert injected is None
    assert nav.route_calls == []
    assert state["triggered"] is False
    assert state["attempted"] is False


def test_graph_context_scheduler_bridge_query_waits_by_default_until_four_reads():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(symbols)
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    query = (
        "placeholder tokens in the dial address get resolved through template "
        "variable substitution; what component bridges that replacement path"
    )
    injected = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceAll(upstream.Dial)"}
            ],
        }
    )

    assert injected is None
    assert nav.route_calls == []
    assert state["triggered"] is False
    assert state["attempted"] is False


def test_graph_context_scheduler_bridge_query_opt_in_triggers_after_three_reads():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(
                (symbols, max_nodes, query, pool_nodes, neighbor_nodes)
            )
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_bridge_read_symbol_min_reads": 3,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    query = (
        "placeholder tokens in the dial address get resolved through template "
        "variable substitution; what component bridges that replacement path"
    )
    injected = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceAll(upstream.Dial)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["generic_min_read_calls"] == 3
    assert injected["metadata"]["bridge_route_query"] is True
    assert "NewReplacer" in nav.route_calls[0][0]
    assert state["triggered"] is True


def test_graph_context_scheduler_bridge_route_gets_post_read_final_guard():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                    "start_line": 299,
                    "end_line": 330,
                    "name": "Handler#doActiveHealthCheckForAllHosts",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "modules/caddyhttp/reverseproxy/hosts.go",
                    "start_line": 102,
                    "end_line": 130,
                    "name": "Host#ReplacePlaceholders",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "NewReplacer",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )
    query = (
        "placeholder tokens in the dial address get resolved through template "
        "variable replacement; which bridge path connects the health check loop"
    )
    injected = scheduler(
        {
            "turn": 2,
            "query": query,
            "tool_calls": [
                ToolCallRecord(
                    tool_call_id="call_1",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/reverseproxy/healthchecks.go",
                        "offset": 299,
                        "limit": 35,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_2",
                    skill_id="read",
                    arguments={
                        "file_path": "replacer.go",
                        "offset": 29,
                        "limit": 10,
                    },
                ),
            ],
            "read_outputs": [
                {"content": " 304 | caddy.NewReplacer().ReplaceAll(upstream.Dial)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["bridge_route_query"] is True
    assert state["route_bridge_query"] is True
    assert state["route_traceback"] is False
    assert state["route_type_heavy"] is False

    guard = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(
                    tool_call_id="call_1",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/reverseproxy/healthchecks.go",
                        "offset": 299,
                        "limit": 35,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_2",
                    skill_id="read",
                    arguments={
                        "file_path": "replacer.go",
                        "offset": 29,
                        "limit": 10,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_3",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/reverseproxy/hosts.go",
                        "offset": 102,
                        "limit": 30,
                    },
                ),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert guard["metadata"]["operation"] == "lsp_route_finalization_guard"
    assert guard["metadata"]["bridge_route_query"] is True
    assert (
        "modules/caddyhttp/reverseproxy/hosts.go:102-130 "
        "Host#ReplacePlaceholders [provider]"
    ) in guard["content"]
    assert "replacer.go:29-38 NewReplacer [bridge]" in guard["content"]


def test_graph_context_scheduler_bridge_final_guard_can_be_disabled():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_read_symbol_bridge_final_guard": False,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )
    query = "placeholder template variable replacement bridge path"

    injected = scheduler(
        {
            "turn": 2,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"content": " 304 | caddy.NewReplacer().ReplaceAll(upstream.Dial)"}
            ],
        }
    )
    assert injected is not None
    assert state["route_bridge_query"] is True

    guard = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
            ],
            "read_outputs": [],
        }
    )

    assert guard is None
    assert not state.get("final_guard_attempted")


def test_graph_context_scheduler_triggers_traceback_route_after_three_generic_reads():
    class _Nav:
        def __init__(self):
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(
                (symbols, max_nodes, query, pool_nodes, neighbor_nodes)
            )
            return [
                {
                    "file": "modules/caddyhttp/matchers.go",
                    "start_line": 1090,
                    "end_line": 1134,
                    "name": "MatchHeaderRE#CELLibrary",
                    "role": "endpoint",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_read_symbol_max_nodes": 8,
            "graph_read_symbol_pool_nodes": 20,
            "graph_read_symbol_neighbor_nodes": 20,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 3,
            "query": (
                "custom expression request matcher unsupported data type "
                "upstream configuration assembled before validation"
            ),
            "tool_calls": [
                ToolCallRecord(
                    tool_call_id="call_1",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/celmatcher.go",
                        "offset": 337,
                        "limit": 50,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_2",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/celmatcher.go",
                        "offset": 93,
                        "limit": 70,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_3",
                    skill_id="read",
                    arguments={
                        "file_path": "modules/caddyhttp/celmatcher.go",
                        "offset": 337,
                        "limit": 100,
                    },
                ),
            ],
            "read_outputs": [
                {
                    "path": "modules/caddyhttp/celmatcher.go",
                    "content": " 360 | NewMatcherCELLibrary(adapter).Compile(expr)",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["traceback_route"] is True
    assert injected["metadata"]["symbols"] == ["NewMatcherCELLibrary"]
    assert nav.route_calls == [
        (
            ["NewMatcherCELLibrary"],
            12,
            (
                "custom expression request matcher unsupported data type "
                "upstream configuration assembled before validation"
            ),
            30,
            80,
        )
    ]
    assert state["route_traceback"] is True


def test_traceback_route_query_ignores_architectural_bridge_path():
    bridge_query = (
        "When my reverse proxy performs periodic liveness checks on upstream "
        "backend servers, it allows placeholder tokens in the dial address "
        "configuration. I need to understand the architectural path between "
        "the health checking loop that initiates address substitution and the "
        "low-level provider function that returns system hostname values."
    )
    traceback_query = (
        "custom expression request matcher unsupported data type upstream "
        "configuration assembled before validation"
    )

    assert _is_traceback_route_query(_query_terms(bridge_query)) is False
    assert _is_traceback_route_query(_query_terms(traceback_query)) is True


def test_traceback_route_query_ignores_cache_bridge_path():
    cache_bridge_query = (
        "When my library tries to automatically fetch an up-to-date table of "
        "leap seconds from remote sources, it checks cached versions first "
        "before re-downloading. I can see that the auto-open logic iterates "
        "through candidate files and attempts to use cached remote resources, "
        "and I also observe that ultimately a cache directory path gets "
        "resolved and created under my home directory's configuration folder. "
        "However, I'm confused about how the request for a remote leap-second "
        "file translates into querying that specific download cache location. "
        "What intermediate retrieval or caching mechanism connects the "
        "high-level file-opening routine to the low-level cache directory "
        "resolution?"
    )
    traceback_query = (
        "custom expression request matcher unsupported data type upstream "
        "configuration assembled before validation"
    )

    assert _is_traceback_route_query(_query_terms(cache_bridge_query)) is False
    assert _is_traceback_route_query(_query_terms(traceback_query)) is True


def test_graph_context_scheduler_final_guard_for_traceback_route():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "docstring.rs",
                    "start_line": 315,
                    "end_line": 382,
                    "name": "run_action_queue",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1379,
                    "end_line": 1415,
                    "name": "add_code_line",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1536,
                    "end_line": 1572,
                    "name": "CodeExampleAddAction",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1251,
                    "end_line": 1280,
                    "name": "push_format_action",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 1186,
                    "end_line": 1239,
                    "name": "add_first_line",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "docstring.rs",
                    "start_line": 271,
                    "end_line": 292,
                    "name": "add_iter",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                "embedded code snippets duplicated raw fallback lines collected "
                "before fallback recovery path triggers"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "docstring.rs",
                    "content": " 330 | printer.add_iter(lines)",
                }
            ],
        }
    )

    assert injected is not None
    assert state["route_type_heavy"] is False
    assert state["route_traceback"] is True
    assert [loc["name"] for loc in state["guard_locations"][:2]] == [
        "run_action_queue",
        "add_iter",
    ]

    guard = scheduler(
        {
            "turn": 3,
            "query": (
                "embedded code snippets duplicated raw fallback lines collected "
                "before fallback recovery path triggers"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(
                    tool_call_id="call_3",
                    skill_id="read",
                    arguments={
                        "file_path": "docstring.rs",
                        "offset": 271,
                        "limit": 25,
                    },
                ),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert guard["metadata"]["operation"] == "lsp_route_finalization_guard"
    assert guard["metadata"]["priority_count"] == 2
    assert "docstring.rs:315-382 run_action_queue [endpoint]" in guard["content"]
    assert "docstring.rs:271-292 add_iter [endpoint]" in guard["content"]
    assert [loc["name"] for loc in guard["metadata"]["locations"][:2]] == [
        "run_action_queue",
        "add_iter",
    ]


def test_graph_context_scheduler_traceback_guard_limits_weak_priority_prefix():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "iers.py",
                    "start_line": 997,
                    "end_line": 1076,
                    "name": "LeapSeconds.auto_open",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "is_url_in_cache",
                },
                {
                    "file": "iers.py",
                    "start_line": 679,
                    "end_line": 739,
                    "name": "IERS_Auto.open",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "download_file",
                },
                {
                    "file": "data.py",
                    "start_line": 1695,
                    "end_line": 1727,
                    "name": "_get_download_cache_loc",
                    "role": "provider",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )
    query = "where cached file path flows before fallback recovery"

    injected = scheduler(
        {
            "turn": 2,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"path": "iers.py", "content": " 1043 | is_url_in_cache(f)"}
            ],
        }
    )

    assert injected is not None
    assert state["route_traceback"] is True

    guard = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert guard["metadata"]["priority_count"] == 1


def test_graph_context_scheduler_skips_weak_provider_only_route():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "sky_coordinate_parsers.py",
                    "start_line": 62,
                    "end_line": 208,
                    "name": "_get_frame_without_data",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "get_frame_attr_names",
                },
                {
                    "file": "baseframe.py",
                    "start_line": 1327,
                    "end_line": 1360,
                    "name": "is_equivalent_frame",
                    "role": "support",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_read_symbol_weak_route_nudge": False,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "sequential frame conversion should preserve equivalent frames",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "transformations.py",
                    "content": " 1508 | if from_frame.is_equivalent_frame(to_frame):",
                }
            ],
        }
    )

    assert injected is None
    assert state["attempted"] is True
    assert state["triggered"] is False
    assert state["skipped"] is True
    assert state["skip_reason"] == "weak_route_roles"
    assert state["operation"] == "lsp_route"
    assert state["nodes"] == 2


def test_graph_context_scheduler_nudges_after_weak_provider_only_route():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "sky_coordinate_parsers.py",
                    "start_line": 62,
                    "end_line": 208,
                    "name": "_get_frame_without_data",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "get_frame_attr_names",
                },
                {
                    "file": "baseframe.py",
                    "start_line": 1327,
                    "end_line": 1360,
                    "name": "is_equivalent_frame",
                    "role": "support",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "sequential frame conversion should preserve equivalent frames",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "transformations.py",
                    "content": " 1508 | if from_frame.is_equivalent_frame(to_frame):",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["source"] == "scheduled_graph_lsp"
    assert injected["metadata"]["operation"] == "lsp_route_weak_skip"
    assert injected["metadata"]["node_count"] == 2
    assert "only resolved weak provider/support anchors" in injected["content"]
    assert (
        "finalize now from those exact read-confirmed Locations" in injected["content"]
    )
    assert state["attempted"] is True
    assert state["triggered"] is True
    assert state["skipped"] is True
    assert state["skip_reason"] == "weak_route_roles"
    assert state["operation"] == "lsp_route"


def test_graph_context_scheduler_cache_bridge_keeps_query_providers(tmp_path):
    iers_dir = tmp_path / "astropy" / "utils" / "iers"
    data_dir = tmp_path / "astropy" / "utils"
    config_dir = tmp_path / "astropy" / "config"
    iers_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True)
    (iers_dir / "iers.py").write_text(
        "\n".join(f"iers line {idx}" for idx in range(1, 1100)),
        encoding="utf-8",
    )
    (data_dir / "data.py").write_text(
        "\n".join(f"data line {idx}" for idx in range(1, 1900)),
        encoding="utf-8",
    )
    (config_dir / "paths.py").write_text(
        "\n".join(f"path line {idx}" for idx in range(1, 220)),
        encoding="utf-8",
    )

    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 997,
                    "end_line": 1076,
                    "name": "astropy/utils/iers/iers.py:LeapSeconds.auto_open()",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "is_url_in_cache",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1468,
                    "end_line": 1471,
                    "name": "astropy/utils/data.py:_do_download_files_in_parallel()",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1695,
                    "end_line": 1727,
                    "name": "astropy/utils/data.py:_get_download_cache_loc()",
                    "role": "provider",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/config/paths.py",
                    "start_line": 124,
                    "end_line": 170,
                    "name": "astropy/config/paths.py:get_cache_dir()",
                    "role": "provider",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1424,
                    "end_line": 1456,
                    "name": "astropy/utils/data.py:is_url_in_cache()",
                    "role": "provider",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 103,
                    "end_line": 114,
                    "name": "astropy/utils/iers/iers.py:download_file()",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/utils/data.py",
                    "start_line": 1225,
                    "end_line": 1421,
                    "name": "astropy/utils/data.py:download_file()",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "astropy/utils/iers/iers.py",
                    "start_line": 679,
                    "end_line": 739,
                    "name": "astropy/utils/iers/iers.py:IERS_Auto.open()",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "download_file",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    query = (
        "When my library tries to automatically fetch an up-to-date table of "
        "leap seconds from remote sources, it checks cached versions first "
        "before re-downloading. I can see that the auto-open logic iterates "
        "through candidate files and attempts to use cached remote resources, "
        "and I also observe that ultimately a cache directory path gets "
        "resolved and created under my home directory's configuration folder. "
        "What intermediate retrieval or caching mechanism connects the "
        "high-level file-opening routine to the low-level cache directory "
        "resolution?"
    )
    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
            "graph_read_symbol_auto_open_max_lines": 3,
        },
        max_turns=12,
        repo_path=str(tmp_path),
    )

    injected = scheduler(
        {
            "turn": 4,
            "query": query,
            "tool_calls": [
                ToolCallRecord(
                    tool_call_id="call_1",
                    skill_id="read",
                    arguments={
                        "file_path": "astropy/utils/iers/iers.py",
                        "offset": 997,
                        "limit": 80,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_2",
                    skill_id="read",
                    arguments={
                        "file_path": "astropy/utils/iers/iers.py",
                        "offset": 944,
                        "limit": 45,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_3",
                    skill_id="read",
                    arguments={
                        "file_path": "astropy/utils/iers/iers.py",
                        "offset": 989,
                        "limit": 100,
                    },
                ),
                ToolCallRecord(
                    tool_call_id="call_4",
                    skill_id="read",
                    arguments={
                        "file_path": "astropy/utils/iers/iers.py",
                        "offset": 103,
                        "limit": 20,
                    },
                ),
            ],
            "read_outputs": [
                {
                    "path": "astropy/utils/iers/iers.py",
                    "content": "download_file() is_url_in_cache _get_download_cache_loc",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["traceback_route"] is False
    assert state["route_traceback"] is False
    auto_names = {loc["name"] for loc in injected["metadata"]["auto_open_locations"]}
    assert "astropy/utils/data.py:is_url_in_cache()" in auto_names
    assert "astropy/utils/data.py:_get_download_cache_loc()" in auto_names

    guard = scheduler(
        {
            "turn": 5,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_4", skill_id="read", arguments={}),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert "astropy/utils/data.py:1424-1456" in guard["content"]
    assert "astropy/utils/data.py:1695-1727" in guard["content"]


def test_graph_context_scheduler_can_allow_weak_lsp_route_context():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "sky_coordinate_parsers.py",
                    "start_line": 62,
                    "end_line": 208,
                    "name": "_get_frame_without_data",
                    "role": "provider",
                    "source": "route_neighbor",
                    "via": "get_frame_attr_names",
                },
                {
                    "file": "baseframe.py",
                    "start_line": 1327,
                    "end_line": 1360,
                    "name": "is_equivalent_frame",
                    "role": "support",
                    "source": "direct_definition",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_read_symbol_allow_weak_lsp_route": True,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "sequential frame conversion should preserve equivalent frames",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "transformations.py",
                    "content": " 1508 | if from_frame.is_equivalent_frame(to_frame):",
                }
            ],
        }
    )

    assert injected is not None
    assert injected["source"] == "scheduled_graph_lsp"
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["route_roles"] == ["provider", "support"]
    assert "only resolved weak provider/support anchors" not in injected["content"]
    assert state["attempted"] is True
    assert state["triggered"] is True
    assert state["skipped"] is False
    assert state["operation"] == "lsp_route"


def test_graph_context_scheduler_final_guard_protects_route_neighbor_anchors():
    class _Nav:
        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return [
                {
                    "file": "celmatcher.go",
                    "start_line": 475,
                    "end_line": 495,
                    "name": "celMatcherStringListMacroExpander",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "celmatcher.go",
                    "start_line": 390,
                    "end_line": 395,
                    "name": "NewMatcherCELLibrary",
                    "role": "bridge",
                    "source": "direct_definition",
                },
                {
                    "file": "httptype.go",
                    "start_line": 1301,
                    "end_line": 1384,
                    "name": "ServerType#compileEncodedMatcherSets",
                    "role": "type",
                    "source": "direct_definition",
                },
                {
                    "file": "httptype.go",
                    "start_line": 1459,
                    "end_line": 1469,
                    "name": "encodeMatcherSet",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "celmatcher.go",
                    "start_line": 337,
                    "end_line": 378,
                    "name": "CELMatcherImpl",
                    "role": "endpoint",
                    "source": "direct_definition",
                },
                {
                    "file": "fileserver/matcher.go",
                    "start_line": 171,
                    "end_line": 220,
                    "name": "MatchFile#CELLibrary",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "NewMatcherCELLibrary",
                },
                {
                    "file": "matchers.go",
                    "start_line": 1090,
                    "end_line": 1134,
                    "name": "MatchHeaderRE#CELLibrary",
                    "role": "endpoint",
                    "source": "route_neighbor",
                    "via": "NewMatcherCELLibrary",
                },
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            return []

        def neighbors(self, seeds, max_nodes=10):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 2,
            "graph_fanout_search_calls": 10,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": (
                "custom expression matcher unsupported data type upstream "
                "configuration assembled before validation"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {
                    "path": "celmatcher.go",
                    "content": " 390 | NewMatcherCELLibrary(envOptions)",
                }
            ],
        }
    )

    assert injected is not None
    assert state["route_traceback"] is True

    guard = scheduler(
        {
            "turn": 3,
            "query": (
                "custom expression matcher unsupported data type upstream "
                "configuration assembled before validation"
            ),
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="read", arguments={}),
                ToolCallRecord(
                    tool_call_id="call_3",
                    skill_id="read",
                    arguments={
                        "file_path": "httptype.go",
                        "offset": 1301,
                        "limit": 170,
                    },
                ),
            ],
            "read_outputs": [],
        }
    )

    assert guard is not None
    assert "fileserver/matcher.go:171-220 MatchFile#CELLibrary [endpoint]" in (
        guard["content"]
    )
    assert "matchers.go:1090-1134 MatchHeaderRE#CELLibrary [endpoint]" in (
        guard["content"]
    )


def _scheduled_result(answer: str, *, read_offset: int = 100) -> AgentResult:
    return AgentResult(
        answer=answer,
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={
                    "file_path": "src/nodes.rs",
                    "offset": read_offset,
                    "limit": 20,
                },
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "src/nodes.rs",
                                "start_line": 101,
                                "end_line": 111,
                                "name": "pkg/nodes/Expr",
                            }
                        ],
                    },
                )
            ]
        ),
    )


def test_scheduled_anchor_audit_triggers_for_read_uncited_anchor():
    audit = scheduled_anchor_audit(
        _scheduled_result(
            "Files: src/rule.rs\nSymbols: src/rule.rs:check\nLocations: src/rule.rs:1-5"
        )
    )

    assert audit["triggered"] is True
    assert audit["offered_count"] == 1
    assert audit["read_count"] == 1
    assert audit["cited_count"] == 0
    correction = render_scheduled_anchor_correction(audit["read_uncited"])
    assert "src/nodes.rs:101-111 pkg/nodes/Expr" in correction


def test_scheduled_anchor_audit_stays_off_when_answer_cites_anchor():
    audit = scheduled_anchor_audit(
        _scheduled_result(
            "Files: src/nodes.rs\n"
            "Symbols: src/nodes.rs:Expr\n"
            "Locations: src/nodes.rs:101-111"
        )
    )

    assert audit["triggered"] is False
    assert audit["read_count"] == 1
    assert audit["cited_count"] == 1


def test_scheduled_anchor_audit_stays_off_when_anchor_was_not_read():
    audit = scheduled_anchor_audit(
        _scheduled_result(
            "Files: src/rule.rs\nSymbols: src/rule.rs:check\nLocations: src/rule.rs:1-5",
            read_offset=200,
        )
    )

    assert audit["triggered"] is False
    assert audit["read_count"] == 0


def _scheduled_priority_result(answer: str) -> AgentResult:
    return AgentResult(
        answer=answer,
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "src/root.rs", "offset": 1, "limit": 100},
            ),
            ToolCallRecord(
                tool_call_id="call_2",
                skill_id="read",
                arguments={"file_path": "src/helper.rs", "offset": 1, "limit": 100},
            ),
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "src/root.rs",
                                "start_line": 10,
                                "end_line": 20,
                                "name": "pkg/root/Important",
                            },
                            {
                                "file": "src/helper.rs",
                                "start_line": 30,
                                "end_line": 40,
                                "name": "pkg/helper/Helper",
                            },
                        ],
                    },
                )
            ]
        ),
    )


def test_scheduled_top_anchor_audit_triggers_when_priority_anchor_is_late():
    result = _scheduled_priority_result(
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4, "
        "src/helper.rs:5-6, src/helper.rs:7-8, src/helper.rs:30-40, "
        "src/root.rs:10-20"
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is True
    assert audit["reason"] == "priority_anchor_outside_top_k"
    assert audit["missing_count"] == 1
    correction = render_scheduled_top_anchor_correction(audit)
    assert "src/root.rs:10-20 pkg/root/Important" in correction
    assert "first 5 parsed answer ranges" in correction
    assert "Required top-route anchors:" in correction
    assert "Required anchors below must appear before optional anchors" in correction
    assert "smaller interior ranges" in correction


def test_scheduled_top_anchor_answer_patch_promotes_read_confirmed_anchor():
    result = _scheduled_priority_result(
        "Files: src/helper.rs\n"
        "Symbols: pkg/helper/Helper\n"
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4, "
        "src/helper.rs:5-6, src/helper.rs:7-8, src/helper.rs:30-40, "
        "src/root.rs:10-20"
    )
    audit = scheduled_top_anchor_audit(result)

    patched = apply_scheduled_top_anchor_answer_patch(result.answer or "", audit)

    assert patched is not None
    assert patched.splitlines()[0] == "Files: src/root.rs, src/helper.rs"
    assert patched.splitlines()[1].startswith("Symbols: pkg/root/Important")
    assert patched.splitlines()[2] == (
        "Locations: src/root.rs:10-20, src/helper.rs:1-2, "
        "src/helper.rs:3-4, src/helper.rs:5-6, src/helper.rs:7-8"
    )


def test_scheduled_top_anchor_answer_patch_ignores_absent_anchor():
    result = _scheduled_priority_result(
        "Files: src/helper.rs\n"
        "Symbols: pkg/helper/Helper\n"
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4"
    )
    audit = scheduled_top_anchor_audit(result)

    assert apply_scheduled_top_anchor_answer_patch(result.answer or "", audit) is None


def test_scheduled_top_anchor_answer_patch_can_insert_read_confirmed_anchor():
    result = _scheduled_priority_result(
        "Files: src/helper.rs\n"
        "Symbols: pkg/helper/Helper\n"
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4, "
        "src/helper.rs:5-6, src/helper.rs:7-8, src/helper.rs:30-40"
    )
    audit = scheduled_top_anchor_audit(result)

    patched = apply_scheduled_top_anchor_answer_patch(
        result.answer or "", audit, allow_insert=True
    )

    assert patched is not None
    assert patched.splitlines()[0] == "Files: src/root.rs, src/helper.rs"
    assert patched.splitlines()[1].startswith("Symbols: pkg/root/Important")
    assert patched.splitlines()[2] == (
        "Locations: src/root.rs:10-20, src/helper.rs:1-2, "
        "src/helper.rs:3-4, src/helper.rs:5-6, src/helper.rs:7-8"
    )
    assert scheduled_top_anchor_required_anchor_audit(patched, audit)["ok"] is True


def test_scheduled_top_anchor_answer_patch_inserts_caddy_entry_anchor():
    result = AgentResult(
        answer=(
            "Files: replacer.go, modules/caddyhttp/reverseproxy/healthchecks.go\n"
            "Symbols: replacer.go:NewReplacer, replacer.go:globalDefaultReplacements\n"
            "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:308-448, "
            "replacer.go:29-38, replacer.go:297-337, replacer.go:120-123, "
            "replacer.go:55-58"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={
                    "file_path": "modules/caddyhttp/reverseproxy/healthchecks.go",
                    "offset": 243,
                    "limit": 80,
                },
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                                "start_line": 243,
                                "end_line": 299,
                                "name": (
                                    "modules/caddyhttp/reverseproxy/"
                                    "Handler#doActiveHealthCheckForAllHosts"
                                ),
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "replacer.go",
                                "start_line": 29,
                                "end_line": 38,
                                "name": "NewReplacer",
                                "role": "bridge",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )
    audit = scheduled_top_anchor_audit(result)

    patched = apply_scheduled_top_anchor_answer_patch(
        result.answer or "", audit, allow_insert=True
    )

    assert patched is not None
    assert patched.splitlines()[0].startswith(
        "Files: modules/caddyhttp/reverseproxy/healthchecks.go, replacer.go"
    )
    assert (
        patched.splitlines()[2]
        == "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:243-299, "
        "modules/caddyhttp/reverseproxy/healthchecks.go:308-448, "
        "replacer.go:29-38, replacer.go:297-337, replacer.go:120-123"
    )
    assert scheduled_top_anchor_required_anchor_audit(patched, audit)["ok"] is True


def test_filter_answer_locations_to_trace_evidence_removes_unconfirmed_location():
    trace_payload = {
        "events": [
            {
                "kind": "scheduled_context",
                "turn": 2,
                "data": {
                    "source": "scheduled_graph_lsp",
                    "operation": "lsp_route",
                    "locations": [
                        {
                            "file": "src/route.rs",
                            "start_line": 10,
                            "end_line": 20,
                            "name": "src/route.rs:Route",
                        }
                    ],
                },
            }
        ],
        "context": [
            {
                "source": "read",
                "state": "read",
                "path": "src/read.rs",
                "metadata": {"start_line": 30, "end_line": 40},
            }
        ],
    }
    answer = (
        "Files: src/route.rs, src/read.rs, src/extra.rs\n"
        "Symbols: src/route.rs:Route\n"
        "Locations: src/route.rs:10-20, src/extra.rs:1-5, src/read.rs:30-40"
    )

    filtered = filter_answer_locations_to_trace_evidence(
        answer,
        trace_payload=trace_payload,
    )

    assert filtered["applied"] is True
    assert filtered["removed_locations"] == ["src/extra.rs:1-5"]
    assert filtered["answer"].splitlines()[2] == (
        "Locations: src/route.rs:10-20, src/read.rs:30-40"
    )


def test_scheduled_top_anchor_required_anchor_audit_checks_top_k():
    result = _scheduled_priority_result(
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4, "
        "src/helper.rs:5-6, src/helper.rs:7-8, src/helper.rs:30-40, "
        "src/root.rs:10-20"
    )
    audit = scheduled_top_anchor_audit(result)

    failed = scheduled_top_anchor_required_anchor_audit(
        "Locations: src/helper.rs:1-2, src/helper.rs:3-4", audit
    )
    passed = scheduled_top_anchor_required_anchor_audit(
        "Locations: src/root.rs:10-20, src/helper.rs:1-2", audit
    )

    assert failed["ok"] is False
    assert failed["missing_count"] == 1
    assert passed["ok"] is True
    assert passed["missing_count"] == 0


def test_scheduled_top_anchor_audit_stays_off_when_priority_anchor_is_top_k():
    result = _scheduled_priority_result(
        "Locations: src/root.rs:10-20, src/helper.rs:1-2, "
        "src/helper.rs:3-4, src/helper.rs:5-6, src/helper.rs:30-40"
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["missing_count"] == 0


def test_scheduled_top_anchor_audit_triggers_when_priority_anchor_unread():
    result = AgentResult(
        answer=(
            "Locations: replacer.go:29-38, replacer.go:297-337, "
            "replacer.go:120-123, healthchecks.go:308-351"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "replacer.go", "offset": 1, "limit": 400},
            ),
            ToolCallRecord(
                tool_call_id="call_2",
                skill_id="read",
                arguments={
                    "file_path": "healthchecks.go",
                    "offset": 308,
                    "limit": 50,
                },
            ),
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "healthchecks.go",
                                "start_line": 243,
                                "end_line": 299,
                                "name": "Handler#doActiveHealthCheckForAllHosts",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "replacer.go",
                                "start_line": 29,
                                "end_line": 38,
                                "name": "NewReplacer",
                                "role": "bridge",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is True
    assert audit["reason"] == "priority_anchor_unread"
    assert audit["missing_unread_count"] == 1
    correction = render_scheduled_top_anchor_correction(audit)
    assert "was offered but was not read" in correction
    assert "Read the anchor below first" in correction
    assert "healthchecks.go:243-299 Handler#doActiveHealthCheckForAllHosts" in (
        correction
    )


def test_scheduled_top_anchor_audit_counts_auto_open_as_read():
    result = AgentResult(
        answer=(
            "Locations: replacer.go:29-38, replacer.go:297-337, "
            "replacer.go:120-123, healthchecks.go:308-351, listeners.go:216-221"
        ),
        tool_calls=[],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "healthchecks.go",
                                "start_line": 243,
                                "end_line": 299,
                                "name": "Handler#doActiveHealthCheckForAllHosts",
                                "role": "endpoint",
                                "source": "direct_definition",
                            }
                        ],
                        "auto_open_locations": [
                            {
                                "file": "healthchecks.go",
                                "start_line": 243,
                                "end_line": 299,
                                "name": "Handler#doActiveHealthCheckForAllHosts",
                                "role": "endpoint",
                                "source": "direct_definition",
                            }
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is True
    assert audit["reason"] == "priority_anchor_outside_top_k"
    assert audit["read_count"] == 1
    assert audit["missing_unread_count"] == 0


def test_scheduled_top_anchor_audit_ignores_provider_only_read_symbols():
    result = AgentResult(
        answer="Locations: astropy/coordinates/transformations.py:1484-1560",
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={
                    "file_path": "astropy/coordinates/sky_coordinate_parsers.py",
                    "offset": 62,
                    "limit": 100,
                },
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": (
                                    "astropy/coordinates/" "sky_coordinate_parsers.py"
                                ),
                                "start_line": 62,
                                "end_line": 208,
                                "name": "_get_frame_without_data",
                                "role": "provider",
                                "source": "route_neighbor",
                                "via": "get_frame_attr_names",
                            },
                            {
                                "file": "astropy/coordinates/baseframe.py",
                                "start_line": 1327,
                                "end_line": 1360,
                                "name": "is_equivalent_frame",
                                "role": "support",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["reason"] == "no_priority_endpoint_or_type"
    assert audit["priority_count"] == 0


def test_scheduled_named_anchor_patch_promotes_symbol_named_provider_location():
    result = AgentResult(
        answer=(
            "Files: modules/caddyhttp/reverseproxy/healthchecks.go, replacer.go\n"
            "Symbols: modules/caddyhttp/reverseproxy/"
            "Handler#doActiveHealthCheckForAllHosts, replacer.go:NewReplacer, "
            "replacer.go:globalDefaultReplacements\n"
            "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:243-299, "
            "replacer.go:29-38, replacer.go:55-58, replacer.go:73-80, "
            "replacer.go:120-123"
        ),
        tool_calls=[],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="tool_call",
                    turn=1,
                    data={
                        "tool": "read",
                        "arguments": {
                            "file_path": (
                                "modules/caddyhttp/reverseproxy/healthchecks.go"
                            ),
                            "offset": 243,
                            "limit": 80,
                        },
                        "error": None,
                    },
                ),
                AgentTraceEvent(
                    kind="tool_call",
                    turn=1,
                    data={
                        "tool": "read",
                        "arguments": {
                            "file_path": "replacer.go",
                            "offset": 1,
                            "limit": 360,
                        },
                        "error": None,
                    },
                ),
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": (
                                    "modules/caddyhttp/reverseproxy/" "healthchecks.go"
                                ),
                                "start_line": 243,
                                "end_line": 299,
                                "name": "Handler#doActiveHealthCheckForAllHosts",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "replacer.go",
                                "start_line": 29,
                                "end_line": 38,
                                "name": "NewReplacer",
                                "role": "bridge",
                                "source": "direct_definition",
                            },
                            {
                                "file": "replacer.go",
                                "start_line": 297,
                                "end_line": 337,
                                "name": "globalDefaultReplacements",
                                "role": "provider",
                                "source": "route_neighbor",
                            },
                        ],
                    },
                ),
            ]
        ),
    )

    audit = scheduled_named_anchor_location_audit(result)
    patched = apply_scheduled_named_anchor_location_patch(result.answer or "", audit)

    assert audit["triggered"] is True
    assert audit["reason"] == "named_anchor_missing_location"
    assert audit["missing_count"] == 1
    assert patched is not None
    assert patched.splitlines()[2] == (
        "Locations: replacer.go:297-337, "
        "modules/caddyhttp/reverseproxy/healthchecks.go:243-299, "
        "replacer.go:29-38, replacer.go:55-58, replacer.go:73-80"
    )


def test_scheduled_named_anchor_patch_ignores_unnamed_provider_location():
    result = AgentResult(
        answer=(
            "Files: replacer.go\n"
            "Symbols: replacer.go:NewReplacer\n"
            "Locations: replacer.go:29-38, replacer.go:55-58, "
            "replacer.go:73-80, replacer.go:120-123"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "replacer.go", "offset": 1, "limit": 360},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "replacer.go",
                                "start_line": 297,
                                "end_line": 337,
                                "name": "globalDefaultReplacements",
                                "role": "provider",
                                "source": "route_neighbor",
                            }
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_named_anchor_location_audit(result)

    assert audit["triggered"] is False
    assert audit["missing_count"] == 0
    assert (
        apply_scheduled_named_anchor_location_patch(result.answer or "", audit) is None
    )


def test_scheduled_top_anchor_audit_does_not_correct_bridge_only_route_anchor():
    result = AgentResult(
        answer=(
            "Locations: src/helper.go:1-2, src/helper.go:3-4, "
            "src/helper.go:5-6, src/helper.go:7-8, src/helper.go:9-10"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "src/replacer.go", "offset": 20, "limit": 80},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "src/replacer.go",
                                "start_line": 29,
                                "end_line": 67,
                                "name": "NewReplacer",
                                "role": "bridge",
                                "source": "direct_definition",
                            },
                            {
                                "file": "src/helper.go",
                                "start_line": 1,
                                "end_line": 10,
                                "name": "helper",
                                "role": "support",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["reason"] == "no_priority_endpoint_or_type"
    assert audit["priority_count"] == 0


def test_scheduled_top_anchor_audit_protects_type_heavy_variant_anchor():
    result = AgentResult(
        answer=("Locations: rule.rs:52-124, nodes.rs:565-634, " "typing.rs:1042-1046"),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "nodes.rs", "offset": 2861, "limit": 20},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "nodes.rs",
                                "start_line": 565,
                                "end_line": 634,
                                "name": "Expr",
                                "role": "type",
                                "source": "direct_definition",
                            },
                            {
                                "file": "nodes.rs",
                                "start_line": 2861,
                                "end_line": 2870,
                                "name": "ExprTuple",
                                "role": "type",
                                "source": "direct_definition",
                            },
                            {
                                "file": "rule.rs",
                                "start_line": 52,
                                "end_line": 124,
                                "name": "unnecessary_list_cast",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result, priority_count=2)

    assert audit["triggered"] is True
    assert audit["reason"] == "priority_anchor_outside_top_k"
    assert audit["missing_count"] == 1
    assert audit["missing_anchors"][0]["name"] == "ExprTuple"


def test_scheduled_top_anchor_audit_prefers_rich_route_anchor_over_one_line_decl():
    result = AgentResult(
        answer=(
            "Locations: src/route.go:30-80, src/helper.go:1-2, "
            "src/helper.go:3-4, src/helper.go:5-6, src/helper.go:7-8"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "src/decl.go", "offset": 1, "limit": 100},
            ),
            ToolCallRecord(
                tool_call_id="call_2",
                skill_id="read",
                arguments={"file_path": "src/route.go", "offset": 1, "limit": 100},
            ),
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "src/decl.go",
                                "start_line": 10,
                                "end_line": 10,
                                "name": "pkg/HealthChecks",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "src/route.go",
                                "start_line": 30,
                                "end_line": 80,
                                "name": "pkg/Handler#doActiveHealthCheck",
                                "role": "endpoint",
                                "source": "route_neighbor",
                                "via": "NewReplacer",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["missing_count"] == 0
    assert audit["read_confirmed"][1]["via"] == "NewReplacer"


def test_scheduled_top_anchor_audit_prefers_final_guard_locations():
    result = AgentResult(
        answer=(
            "Locations: modules/caddyhttp/celmatcher.go:337-378, "
            "modules/caddyhttp/celmatcher.go:390-395, "
            "modules/caddyhttp/fileserver/matcher.go:171-220, "
            "modules/caddyhttp/matchers.go:1090-1134, "
            "modules/caddyhttp/celmatcher.go:409-455"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={
                    "file_path": "modules/caddyhttp/celmatcher.go",
                    "offset": 337,
                    "limit": 120,
                },
            ),
            ToolCallRecord(
                tool_call_id="call_2",
                skill_id="read",
                arguments={
                    "file_path": "modules/caddyhttp/matchers.go",
                    "offset": 1090,
                    "limit": 45,
                },
            ),
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=2,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "modules/caddyhttp/celmatcher.go",
                                "start_line": 475,
                                "end_line": 495,
                                "name": "celMatcherStringListMacroExpander",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "modules/caddyhttp/celmatcher.go",
                                "start_line": 337,
                                "end_line": 378,
                                "name": "CELMatcherImpl",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "modules/caddyhttp/matchers.go",
                                "start_line": 1090,
                                "end_line": 1134,
                                "name": "MatchHeaderRE#CELLibrary",
                                "role": "endpoint",
                                "source": "route_neighbor",
                                "via": "NewMatcherCELLibrary",
                            },
                        ],
                    },
                ),
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=4,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "route_finalization_guard",
                        "locations": [
                            {
                                "file": "modules/caddyhttp/fileserver/matcher.go",
                                "start_line": 171,
                                "end_line": 220,
                                "name": "MatchFile#CELLibrary",
                                "role": "endpoint",
                                "source": "route_neighbor",
                                "via": "NewMatcherCELLibrary",
                            },
                            {
                                "file": "modules/caddyhttp/matchers.go",
                                "start_line": 1090,
                                "end_line": 1134,
                                "name": "MatchHeaderRE#CELLibrary",
                                "role": "endpoint",
                                "source": "route_neighbor",
                                "via": "NewMatcherCELLibrary",
                            },
                            {
                                "file": "modules/caddyhttp/celmatcher.go",
                                "start_line": 337,
                                "end_line": 378,
                                "name": "CELMatcherImpl",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                        ],
                    },
                ),
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["missing_count"] == 0


def test_scheduled_top_anchor_audit_protects_entire_final_guard():
    result = AgentResult(
        answer=(
            "Locations: docstring.rs:315-382, docstring.rs:1379-1415, "
            "docstring.rs:1536-1572, docstring.rs:328-337, "
            "docstring.rs:1405-1410"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "docstring.rs", "offset": 271, "limit": 1305},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=1,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "read_symbols",
                        "locations": [
                            {
                                "file": "docstring.rs",
                                "start_line": 315,
                                "end_line": 382,
                                "name": "run_action_queue",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 271,
                                "end_line": 292,
                                "name": "add_iter",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                        ],
                    },
                ),
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=3,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "route_finalization_guard",
                        "locations": [
                            {
                                "file": "docstring.rs",
                                "start_line": 315,
                                "end_line": 382,
                                "name": "run_action_queue",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 1379,
                                "end_line": 1415,
                                "name": "add_code_line",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 1536,
                                "end_line": 1572,
                                "name": "CodeExampleAddAction",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 1251,
                                "end_line": 1280,
                                "name": "push_format_action",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 271,
                                "end_line": 292,
                                "name": "add_iter",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                        ],
                    },
                ),
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is True
    assert audit["reason"] == "priority_anchor_outside_top_k"
    assert audit["priority_count"] == 5
    assert any(loc["name"] == "add_iter" for loc in audit["missing_anchors"])


def test_scheduled_top_anchor_audit_honors_final_guard_priority_count():
    result = AgentResult(
        answer=(
            "Locations: docstring.rs:315-382, docstring.rs:271-292, "
            "docstring.rs:1379-1415"
        ),
        tool_calls=[
            ToolCallRecord(
                tool_call_id="call_1",
                skill_id="read",
                arguments={"file_path": "docstring.rs", "offset": 271, "limit": 1305},
            )
        ],
        trace=AgentRunTrace(
            events=[
                AgentTraceEvent(
                    kind="scheduled_context",
                    turn=3,
                    data={
                        "source": "scheduled_graph_lsp",
                        "reason": "route_finalization_guard",
                        "priority_count": 2,
                        "locations": [
                            {
                                "file": "docstring.rs",
                                "start_line": 315,
                                "end_line": 382,
                                "name": "run_action_queue",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 271,
                                "end_line": 292,
                                "name": "add_iter",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 1379,
                                "end_line": 1415,
                                "name": "add_code_line",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 1536,
                                "end_line": 1572,
                                "name": "CodeExampleAddAction",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                            {
                                "file": "docstring.rs",
                                "start_line": 905,
                                "end_line": 949,
                                "name": "doctest_add_code_line",
                                "role": "endpoint",
                                "source": "direct_definition",
                            },
                        ],
                    },
                )
            ]
        ),
    )

    audit = scheduled_top_anchor_audit(result)

    assert audit["triggered"] is False
    assert audit["priority_count"] == 2
    assert audit["missing_count"] == 0


def test_scheduled_context_state_backfills_operation_from_trace():
    state = {
        "attempted": True,
        "triggered": True,
        "reason": "read_symbols",
        "operation": None,
        "nodes": 8,
        "seed_count": 0,
        "symbol_count": 0,
        "skipped": False,
        "skip_reason": None,
        "verified_preload": 0,
    }
    trace = AgentRunTrace(
        events=[
            AgentTraceEvent(
                kind="scheduled_context",
                turn=2,
                data={
                    "source": "scheduled_graph_lsp",
                    "reason": "read_symbols",
                    "operation": "lsp_route",
                    "node_count": 8,
                    "symbol_count": 4,
                },
            )
        ]
    )

    merged = _scheduled_context_state_with_trace_fallback(state, trace)

    assert merged["operation"] == "lsp_route"
    assert merged["nodes"] == 8
    assert merged["symbol_count"] == 4


def test_scheduled_answer_ordering_audit_triggers_for_helper_crowding():
    result = AgentResult(
        answer=(
            "Locations: replacer.go:29-38, replacer.go:52-58, "
            "replacer.go:73-80, replacer.go:104-106, replacer.go:120-123, "
            "replacer.go:297-337, "
            "modules/caddyhttp/reverseproxy/healthchecks.go:243-299"
        )
    )

    audit = scheduled_answer_ordering_audit(result)

    assert audit["triggered"] is True
    assert audit["reason"] == "dominant_file_top_spans"
    assert audit["dominant_file"] == "replacer.go"
    assert audit["dominant_file_count"] == 5
    correction = render_scheduled_ordering_correction(audit)
    assert "at most 5 comma-separated entries" in correction
    assert "no markdown bullets" in correction
    assert "healthchecks.go:243-299" in correction


def test_scheduled_answer_ordering_audit_stays_off_for_mixed_core_spans():
    result = AgentResult(
        answer=(
            "Locations: src/entry.py:10-80, src/cache.py:100-145, "
            "src/download.py:200-260, src/cache.py:300-340, "
            "src/format.py:400-430, src/cache.py:500-540"
        )
    )

    audit = scheduled_answer_ordering_audit(result)

    assert audit["triggered"] is False
    assert audit["reason"] == "none"


def test_normalize_answer_path_aliases_uses_unique_candidate_path():
    repair = normalize_answer_path_aliases(
        "Files: healthchecks.go\n"
        "Symbols: healthchecks.go:doActiveHealthCheckForAllHosts\n"
        "Locations: healthchecks.go:243-299, replacer.go:29-38",
        candidate_paths=[
            "modules/caddyhttp/reverseproxy/healthchecks.go",
            "replacer.go",
        ],
    )

    assert repair["applied"] is True
    assert repair["replacements"] == {
        "healthchecks.go": "modules/caddyhttp/reverseproxy/healthchecks.go"
    }
    assert "Files: modules/caddyhttp/reverseproxy/healthchecks.go" in repair["answer"]
    assert (
        "Symbols: modules/caddyhttp/reverseproxy/healthchecks.go:"
        "doActiveHealthCheckForAllHosts"
    ) in repair["answer"]
    assert (
        "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:243-299, "
        "replacer.go:29-38"
    ) in repair["answer"]


def test_normalize_answer_path_aliases_uses_unique_repo_suffix(tmp_path):
    target = tmp_path / "modules" / "caddyhttp" / "reverseproxy"
    target.mkdir(parents=True)
    (target / "healthchecks.go").write_text("package reverseproxy\n")

    repair = normalize_answer_path_aliases(
        "Locations: reverseproxy/healthchecks.go:243-299",
        repo_path=str(tmp_path),
    )

    assert repair["applied"] is True
    assert (
        repair["answer"]
        == "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:243-299"
    )


def test_normalize_answer_path_aliases_leaves_ambiguous_basename(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "target.py").write_text("")
    (tmp_path / "b" / "target.py").write_text("")

    repair = normalize_answer_path_aliases(
        "Locations: target.py:10-20",
        repo_path=str(tmp_path),
    )

    assert repair["applied"] is False
    assert repair["answer"] == "Locations: target.py:10-20"


def test_normalize_answer_path_aliases_does_not_override_exact_repo_path(tmp_path):
    (tmp_path / "modules" / "caddyhttp").mkdir(parents=True)
    (tmp_path / "replacer.go").write_text("package caddy\n")
    (tmp_path / "modules" / "caddyhttp" / "replacer.go").write_text(
        "package caddyhttp\n"
    )

    repair = normalize_answer_path_aliases(
        "Files: replacer.go\nLocations: replacer.go:29-38",
        repo_path=str(tmp_path),
        candidate_paths=["modules/caddyhttp/replacer.go"],
    )

    assert repair["applied"] is False
    assert repair["answer"] == "Files: replacer.go\nLocations: replacer.go:29-38"


def test_normalize_answer_path_aliases_strips_embedded_repo_root(tmp_path):
    (tmp_path / "modules" / "caddyhttp").mkdir(parents=True)
    (tmp_path / "modules" / "caddyhttp" / "matchers.go").write_text(
        "package caddyhttp\n"
    )

    repair = normalize_answer_path_aliases(
        f"Locations: {tmp_path}/modules/caddyhttp/matchers.go:10-20",
        repo_path=str(tmp_path),
    )

    assert repair["applied"] is True
    assert repair["answer"] == "Locations: modules/caddyhttp/matchers.go:10-20"


def test_normalize_answer_location_order_promotes_scheduled_tail_anchor():
    answer = (
        "Locations: replacer.go:29-38, replacer.go:52-58, "
        "replacer.go:73-80, replacer.go:104-106, replacer.go:120-123, "
        "modules/caddyhttp/reverseproxy/healthchecks.go:243-299"
    )
    trace = {
        "events": [
            {
                "kind": "scheduled_context",
                "turn": 3,
                "data": {
                    "source": "scheduled_graph_lsp",
                    "reason": "read_symbols",
                    "operation": "lsp_route",
                    "locations": [
                        {
                            "file": "modules/caddyhttp/reverseproxy/healthchecks.go",
                            "start_line": 243,
                            "end_line": 299,
                            "name": "doActiveHealthCheckForAllHosts",
                            "role": "endpoint",
                            "source": "direct_definition",
                        }
                    ],
                },
            }
        ]
    }

    repair = normalize_answer_location_order(answer, trace_payload=trace, top_k=5)

    assert repair["applied"] is True
    assert repair["promotion_count"] == 1
    assert repair["promoted_locations"] == [
        "modules/caddyhttp/reverseproxy/healthchecks.go:243-299"
    ]
    assert repair["answer"].startswith(
        "Locations: modules/caddyhttp/reverseproxy/healthchecks.go:243-299, "
    )


def test_normalize_answer_location_order_promotes_read_confirmed_tail_span():
    answer = (
        "Locations: helper.py:1-3, helper.py:4-6, helper.py:7-9, "
        "helper.py:10-12, helper.py:13-15, src/core.py:100-150"
    )
    trace = {
        "context": [
            {
                "source": "read",
                "state": "read",
                "path": "src/core.py",
                "metadata": {"start_line": 90, "end_line": 160},
            }
        ]
    }

    repair = normalize_answer_location_order(answer, trace_payload=trace, top_k=5)

    assert repair["applied"] is True
    assert repair["answer"].startswith("Locations: src/core.py:100-150, ")


def test_normalize_answer_location_order_keeps_unverified_order():
    answer = (
        "Locations: helper.py:1-3, helper.py:4-6, helper.py:7-9, "
        "helper.py:10-12, helper.py:13-15, src/core.py:100-150"
    )

    repair = normalize_answer_location_order(answer, trace_payload={}, top_k=5)

    assert repair["applied"] is False
    assert repair["answer"] == answer


def test_salvage_answer_schema_from_trace_uses_read_confirmed_explicit_file():
    answer = "Files: src/core.py\nSymbols: src/core.py:Core.fix"
    trace = {
        "context": [
            {
                "source": "read",
                "state": "read",
                "path": "src/core.py",
                "metadata": {
                    "start_line": 40,
                    "end_line": 52,
                    "read_turn": 2,
                },
            },
            {
                "source": "read",
                "state": "read",
                "path": "src/other.py",
                "metadata": {"start_line": 1, "end_line": 4, "read_turn": 1},
            },
        ]
    }

    repair = salvage_answer_schema_from_trace(answer, trace_payload=trace, top_k=5)

    assert repair["applied"] is True
    assert repair["location_count"] == 1
    assert repair["answer"] == (
        "Files: src/core.py\n"
        "Symbols: src/core.py:Core.fix\n"
        "Locations: src/core.py:40-52"
    )


def test_salvage_answer_schema_from_trace_uses_read_window_fallback():
    answer = "Files: src/core.py\nSymbols:"

    repair = salvage_answer_schema_from_trace(
        answer,
        read_windows=[
            {"file_path": "src/core.py", "offset": 10, "limit": 4, "turn": 3},
        ],
        top_k=5,
    )

    assert repair["applied"] is True
    assert repair["answer"].endswith("Locations: src/core.py:10-13")


def test_salvage_answer_schema_from_trace_requires_explicit_files():
    repair = salvage_answer_schema_from_trace(
        "I found it in the core runner.",
        read_windows=[
            {"file_path": "src/core.py", "offset": 10, "limit": 4, "turn": 3},
        ],
        top_k=5,
    )

    assert repair["applied"] is False
    assert repair["reason"] == "no_explicit_files"


def test_salvage_answer_schema_from_trace_requires_read_confirmed_evidence():
    answer = "Files: src/core.py\nSymbols: src/core.py:Core.fix"
    trace = {
        "events": [
            {
                "kind": "scheduled_context",
                "turn": 1,
                "data": {
                    "locations": [
                        {"file": "src/core.py", "start_line": 40, "end_line": 52}
                    ]
                },
            }
        ]
    }

    repair = salvage_answer_schema_from_trace(answer, trace_payload=trace, top_k=5)

    assert repair["applied"] is False
    assert repair["reason"] == "no_read_confirmed_evidence"


def test_graph_context_scheduler_injects_once_under_search_pressure():
    class _Nav:
        def __init__(self):
            self.seed_calls = []
            self.neighbour_calls = []

        def seeds_from_spans(self, candidates, max_seeds=5):
            self.seed_calls.append((candidates, max_seeds))
            return [("src/a.py", "target")]

        def neighbors(self, seeds, max_nodes=10):
            self.neighbour_calls.append((seeds, max_nodes))
            return [
                {
                    "file": "src/a.py",
                    "start_line": 10,
                    "end_line": 14,
                    "name": "target",
                }
            ]

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [{"file": "src/a.py", "start": 10, "end": 14}],
        {
            "graph_fanout_search_calls": 2,
            "graph_fanout_read_calls": 6,
            "graph_fanout_max_seeds": 3,
            "graph_fanout_max_nodes": 4,
        },
        max_turns=12,
    )

    quiet = scheduler(
        {
            "turn": 1,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="grep", arguments={})
            ],
        }
    )
    assert quiet is None
    assert state["attempted"] is False

    injected = scheduler(
        {
            "turn": 2,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="grep", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="glob", arguments={}),
            ],
        }
    )

    assert injected is not None
    assert injected["source"] == "scheduled_graph_lsp"
    assert "src/a.py:10-14 target" in injected["content"]
    assert injected["metadata"]["reason"] == "search_calls"
    assert injected["metadata"]["min_read_calls"] == 6
    assert injected["metadata"]["node_count"] == 1
    assert state["triggered"] is True
    assert state["nodes"] == 1
    assert nav.seed_calls[0][1] == 3
    assert nav.neighbour_calls[0][1] == 4
    assert scheduler({"turn": 3, "tool_calls": []}) is None


def test_graph_context_scheduler_holds_fanout_for_pending_bridge_route():
    class _Nav:
        def __init__(self):
            self.seed_calls = []
            self.route_calls = []

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            self.route_calls.append(symbols)
            return [
                {
                    "file": "replacer.go",
                    "start_line": 29,
                    "end_line": 38,
                    "name": "NewReplacer",
                    "role": "bridge",
                    "source": "direct_definition",
                }
            ]

        def seeds_from_spans(self, candidates, max_seeds=5):
            self.seed_calls.append((candidates, max_seeds))
            return [("src/a.py", "target")]

        def neighbors(self, seeds, max_nodes=10):
            return [
                {
                    "file": "src/a.py",
                    "start_line": 10,
                    "end_line": 14,
                    "name": "target",
                }
            ]

    nav = _Nav()
    scheduler, state = make_graph_context_scheduler(
        nav,
        [{"file": "src/a.py", "start": 10, "end": 14}],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 4,
            "graph_fanout_search_calls": 2,
            "graph_fanout_read_calls": 6,
        },
        max_turns=12,
    )
    query = (
        "placeholder tokens in the dial address get resolved through template "
        "variable replacement; which bridge path connects the health check loop"
    )

    held = scheduler(
        {
            "turn": 2,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="grep", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="glob", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceOrErr(upstream.Dial)"}
            ],
        }
    )

    assert held is None
    assert state["attempted"] is False
    assert state["route_fanout_hold_count"] == 1
    assert state["route_fanout_hold_first_turn"] == 2
    assert state["route_fanout_hold_read_calls"] == 1
    assert state["route_fanout_hold_search_calls"] == 2
    assert state["route_fanout_hold_generic_min_reads"] == 4
    assert state["route_fanout_hold_reason"] == "search_calls"
    assert nav.seed_calls == []
    assert nav.route_calls == []

    injected = scheduler(
        {
            "turn": 3,
            "query": query,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="grep", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="glob", arguments={}),
                ToolCallRecord(tool_call_id="call_4", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_5", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_6", skill_id="read", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceOrErr(upstream.Dial)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["operation"] == "lsp_route"
    assert injected["metadata"]["bridge_route_query"] is True
    assert nav.route_calls == [["NewReplacer", "ReplaceOrErr"]]
    assert state["triggered"] is True
    assert state["route_fanout_hold_count"] == 1


def test_graph_context_scheduler_can_disable_route_query_fanout_hold():
    class _Nav:
        def seeds_from_spans(self, candidates, max_seeds=5):
            return [("src/a.py", "target")]

        def neighbors(self, seeds, max_nodes=10):
            return [
                {
                    "file": "src/a.py",
                    "start_line": 10,
                    "end_line": 14,
                    "name": "target",
                }
            ]

        def route_for_symbols(
            self,
            symbols,
            *,
            max_nodes=5,
            query=None,
            pool_nodes=None,
            neighbor_nodes=12,
        ):
            return []

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [{"file": "src/a.py", "start": 10, "end": 14}],
        {
            "graph_schedule_on_read_symbols": True,
            "graph_read_symbol_min_reads": 2,
            "graph_generic_read_symbol_min_reads": 4,
            "graph_read_symbol_hold_fanout_for_route_queries": False,
            "graph_fanout_search_calls": 2,
            "graph_fanout_read_calls": 6,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 2,
            "query": "placeholder template variable replacement bridge path",
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="read", arguments={}),
                ToolCallRecord(tool_call_id="call_2", skill_id="grep", arguments={}),
                ToolCallRecord(tool_call_id="call_3", skill_id="glob", arguments={}),
            ],
            "read_outputs": [
                {"content": " 254 | caddy.NewReplacer().ReplaceOrErr(upstream.Dial)"}
            ],
        }
    )

    assert injected is not None
    assert injected["metadata"]["reason"] == "search_calls"
    assert state["triggered"] is True
    assert state["route_fanout_hold_count"] == 0


def test_graph_context_scheduler_skips_when_preload_is_verified():
    class _Nav:
        def seeds_from_spans(self, candidates, max_seeds=5):
            return [("src/a.py", "target")]

        def neighbors(self, seeds, max_nodes=10):
            return [
                {
                    "file": "src/a.py",
                    "start_line": 10,
                    "end_line": 14,
                    "name": "target",
                }
            ]

    scheduler, state = make_graph_context_scheduler(
        _Nav(),
        [{"file": "src/a.py", "start": 10, "end": 14}],
        {
            "graph_fanout_search_calls": 1,
            "graph_fanout_skip_verified_preload": 2,
        },
        max_turns=12,
    )

    injected = scheduler(
        {
            "turn": 3,
            "tool_calls": [
                ToolCallRecord(tool_call_id="call_1", skill_id="grep", arguments={})
            ],
            "context": [
                ContextLedgerEntry(source="preload", state="verified"),
                ContextLedgerEntry(source="preload", state="verified"),
            ],
        }
    )

    assert injected is None
    assert state["attempted"] is False
    assert state["triggered"] is False
    assert state["skipped"] is True
    assert state["skip_reason"] == "preload_verified"
    assert state["verified_preload"] == 2
