# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compatibility namespace for the agent-compile cost study.

Reusable sweep configuration and harness helpers now live in
``codeminer.eval.agent_runner``. This package remains so older experiment
commands and notebooks can import the previous paths while scripts migrate to
the package APIs directly.

Modules:

* :mod:`config` — the ``SweepConfig`` experiment schema (model / dataset /
  harness knobs), loaded from a base + per-arm YAML overlay.
* :mod:`harness` — per-instance dataset loading, prebuilt-index staging into
  the agent ``contexts`` dict, scenario classification, and the single
  ``run_cell`` agent invocation.
* :mod:`prebuilt` — stage offline-built per-instance indexes into the
  ``cache_dir/<type>`` layout ``build_skill_contexts`` expects.
"""
