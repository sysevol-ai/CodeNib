# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Capture the homepage example from indexed calls and authenticated source.

Only repository metadata, the existing cached-chapter graph and pinned source
are read. This does not request a Wiki page or invoke a model. Re-run explicitly
when changing the featured source commit; the public preview is self-contained.
"""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "f361ead047be5cb873174218582f7d8b9fcd9f49"
FILE = "src/requests/sessions.py"
STEPS = [
    ("Session.send()", "Send the request", 800, 808),
    ("SessionRedirectMixin.resolve_redirects()", "Follow the redirect", 270, 275),
    ("SessionRedirectMixin.rebuild_auth()", "Rebuild credentials", 320, 332),
    ("SessionRedirectMixin.should_strip_auth()", "Check the destination", 154, 160),
]


def capture(api_base):
    def read(path):
        with urlopen(f"{api_base.rstrip('/')}/api/{path}", timeout=30) as response:
            return json.load(response)

    repo = next(r for r in read("repos") if r["id"] == "psf__requests")
    if repo["base_commit"] != COMMIT:
        raise ValueError("Featured source commit changed; review the example first")
    graph = read("repos/psf__requests/wiki/redirects/graph")
    nodes = []
    for symbol, title, start, end in STEPS:
        node = next(n for n in graph["nodes"] if n["name"] == f"{FILE}:{symbol}")
        query = urlencode(dict(file=FILE, start=start, end=end, commit=COMMIT))
        source = read(f"repos/psf__requests/source?{query}")
        if (
            source["file"] != FILE
            or source["start_line"] != start
            or source["end_line"] != end
            or len(source["content"].splitlines()) != end - start + 1
        ):
            raise ValueError("Source range does not match the reviewed excerpt")
        nodes.append(
            dict(
                id=node["id"],
                symbol=symbol,
                title=title,
                file=FILE,
                definition_line=node["line"],
                start_line=start,
                content=source["content"],
                sha256=hashlib.sha256(source["content"].encode()).hexdigest(),
            )
        )
    edges = []
    for source, target in zip(nodes, nodes[1:], strict=False):
        edge = next(
            e
            for e in graph["edges"]
            if e["source"] == source["id"] and e["target"] == target["id"]
        )
        anchor = next(
            a
            for a in edge["anchors"]
            if a["file"] == FILE
            and source["start_line"]
            <= a["line"]
            < source["start_line"] + len(source["content"].splitlines())
        )
        if (
            target["symbol"].split(".")[-1].removesuffix("()")
            not in source["content"].splitlines()[anchor["line"] - source["start_line"]]
        ):
            raise ValueError("Indexed occurrence does not match the source excerpt")
        edges.append(dict(source=source["id"], target=target["id"], anchor=anchor))
    return dict(
        repository=repo["repo"],
        commit=COMMIT,
        page="redirects",
        graph_path="/api/repos/psf__requests/wiki/redirects/graph",
        license="Apache-2.0",
        license_url=f"https://github.com/psf/requests/blob/{COMMIT}/LICENSE",
        nodes=nodes,
        edges=edges,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "landing/assets/explore/requests.js"
    data = capture(args.api_base)
    payload = (
        "// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "// Requests source excerpts: Requests contributors, Apache-2.0.\n"
        "// Captured by scripts/build_first_visit_example.py; do not hand-edit.\n"
        f"export default {json.dumps(data, indent=2)};\n"
    )
    if args.check:
        if target.read_text() != payload:
            raise SystemExit("Homepage evidence differs from the indexed source")
        print("Homepage example matches all three indexed calls and pinned excerpts")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload)
        print(f"Captured {len(data['nodes'])} symbols and 3 calls: {target}")


if __name__ == "__main__":
    main()
