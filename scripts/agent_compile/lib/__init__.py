# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared, importable building blocks for the agent-compile cost study.

Everything a sweep runner or an offline ablation needs lives here so that no
*script* imports another *script*. The dependency direction is strictly
``scripts/* -> scripts/agent_compile/lib/*`` (never script -> sibling script).

Modules:

* :mod:`config` — the ``SweepConfig`` experiment schema (model / dataset /
  harness knobs), loaded from a base + per-arm YAML overlay.
* :mod:`harness` — per-instance dataset loading, prebuilt-index staging into
  the agent ``contexts`` dict, scenario classification, and the single
  ``run_cell`` agent invocation.
* :mod:`prebuilt` — stage offline-built per-instance indexes into the
  ``cache_dir/<type>`` layout ``build_skill_contexts`` expects.
"""
