# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Change-time guard: the tests a change reaches, and notes about its code.

``codenib guard check`` compares the working tree with a commit, maps the
changed lines to definitions in that commit's symbol graph, and lists the
tests that reach them, each with its call path. Notes anchored to the changed
code, and notes whose code changed, are reported with it. ``codenib guard
install`` runs the same check from Claude Code or Codex before the agent
finishes.
"""
