# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build the code walkthroughs a Wiki serves.

A walkthrough follows one question through the code, one function per step:
from the call a user writes to the function that answers the question. The
path comes from the index; a model writes each step's text, which is checked
against the source. ``build_walkthroughs.py`` is the entry point.
"""
