# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codeminer.scip_interface.scip_decode_go import SCIPGoGraphDecoder
from codeminer.types import NODE_TYPE_FUNCTION


def test_go_init_identity_includes_defining_file(tmp_path):
    decoder = SCIPGoGraphDecoder(tmp_path / "index.decoded", project_root=tmp_path)
    decoder.internal_module = "example.com/project"
    symbol = "scip-go gomod example.com/project v1 `example.com/project/pkg`/init()."

    first = decoder._make_symbol_key(symbol, "pkg/first.go")
    second = decoder._make_symbol_key(symbol, "pkg/second.go")

    assert first == "pkg/first.go:init"
    assert second == "pkg/second.go:init"
    assert first != second
    assert (
        decoder._get_unified_name(first, "pkg/first.go", NODE_TYPE_FUNCTION)
        == "pkg/first.go:init()"
    )
