# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Native repository identities in the frozen canonical500 metadata inventory.

These sets bind benchmark I/O; membership does not validate an environment.
Source-image checks and native reference controls remain per-dispatch gates.
"""

import re

MULTI_REPOS = frozenset(
    {
        "BurntSushi/ripgrep",
        "alibaba/fastjson2",
        "axios/axios",
        "catchorg/Catch2",
        "clap-rs/clap",
        "cli/cli",
        "darkreader/darkreader",
        "elastic/logstash",
        "expressjs/express",
        "facebook/zstd",
        "fasterxml/jackson-core",
        "fasterxml/jackson-databind",
        "fasterxml/jackson-dataformat-xml",
        "fmtlib/fmt",
        "google/gson",
        "grpc/grpc-go",
        "iamkun/dayjs",
        "jqlang/jq",
        "mockito/mockito",
        "mui/material-ui",
        "nlohmann/json",
        "nushell/nushell",
        "ponylang/ponyc",
        "rayon-rs/rayon",
        "simdjson/simdjson",
        "sveltejs/svelte",
        "tokio-rs/bytes",
        "tokio-rs/tokio",
        "tokio-rs/tracing",
        "vuejs/core",
        "zeromicro/go-zero",
    }
)

POLY_REPOS = frozenset(
    {
        "Significant-Gravitas/AutoGPT",
        "coder/code-server",
        "huggingface/transformers",
        "keras-team/keras",
        "langchain-ai/langchain",
        "microsoft/vscode",
        "mui/material-ui",
        "prettier/prettier",
        "serverless/serverless",
        "sveltejs/svelte",
        "yt-dlp/yt-dlp",
    }
)

PRO_REPOS = frozenset(
    {
        "NodeBB/NodeBB",
        "ansible/ansible",
        "element-hq/element-web",
        "flipt-io/flipt",
        "future-architect/vuls",
        "internetarchive/openlibrary",
        "navidrome/navidrome",
        "qutebrowser/qutebrowser",
    }
)

MULTI_BENCHMARK_ALIASES = {
    "grpc/grpc-go": "grpc/grpc",
    "zeromicro/go-zero": "zeromicro/go",
    "fasterxml/jackson-core": "fasterxml/jackson",
    "fasterxml/jackson-databind": "fasterxml/jackson",
    "fasterxml/jackson-dataformat-xml": "fasterxml/jackson",
    "mui/material-ui": "mui/material",
}


def native_repository(source, repo, original_instance_id):
    """Resolve only inventoried Multi aliases, retaining native instance identity."""
    if source != "Multi":
        return repo
    if (
        not isinstance(repo, str)
        or not repo
        or not isinstance(original_instance_id, str)
    ):
        raise ValueError("Malformed native Multi repository metadata")
    match = re.fullmatch(
        r"([A-Za-z0-9_.-]+)__([A-Za-z0-9_.-]+)-([1-9][0-9]*)",
        original_instance_id,
    )
    if not match:
        raise ValueError("Malformed native Multi instance identity")
    native = match.group(1) + "/" + match.group(2)
    if native not in MULTI_REPOS or repo not in {
        native,
        MULTI_BENCHMARK_ALIASES.get(native),
    }:
        raise ValueError("Native Multi repository does not match benchmark metadata")
    return native
