# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codeminer.graph.incremental.lsp_client import LSPClient


def test_sync_document_sends_versioned_full_content_change(tmp_path):
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    client = LSPClient(["unused"], str(tmp_path), "go")
    notifications = []
    client._notify = lambda method, params: notifications.append((method, params))

    client.sync_document("main.go")
    source.write_text("package main\n\nfunc main() {}\n")
    client.sync_document("main.go")

    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didChange",
    ]
    assert notifications[0][1]["textDocument"]["version"] == 1
    change = notifications[1][1]
    assert change["textDocument"]["version"] == 2
    assert change["contentChanges"] == [{"text": source.read_text()}]


def test_close_document_resets_version(tmp_path):
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    client = LSPClient(["unused"], str(tmp_path), "go")
    notifications = []
    client._notify = lambda method, params: notifications.append((method, params))

    client.sync_document("main.go")
    client.close_document("main.go")
    client.sync_document("main.go")

    assert [method for method, _ in notifications] == [
        "textDocument/didOpen",
        "textDocument/didClose",
        "textDocument/didOpen",
    ]
    assert notifications[-1][1]["textDocument"]["version"] == 1
