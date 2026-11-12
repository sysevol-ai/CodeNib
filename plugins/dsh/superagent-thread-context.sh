#!/bin/sh
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Thread context for a dsh-superagent Ticket.
#
# Call this at the end of the dsh-superagent prepare command, after the Ticket
# directory holds a checkout. It writes THREAD_CONTEXT.md there: the issue or
# pull request that started the Ticket, the threads linked to it, and the code
# they point at, inside a token budget.
#
# A failed prepare command fails the Ticket, so this script never fails: when
# context cannot be built it says why on stderr and exits 0.
#
# Reads the variables dsh-superagent gives the prepare command:
#   DSH_SUPERAGENT_DIR                 the Ticket directory (required)
#   DSH_SUPERAGENT_GITHUB_NUMBER       the issue or pull request (skip if empty)
#   DSH_SUPERAGENT_GITHUB_REPOSITORY   owner/name
# Optional:
#   CODENIB                       command that runs CodeNib (default: codenib)
#   CODENIB_THREADS_BUDGET        token budget (default: 3000)
#   CODENIB_THREADS_OPEN_PULLS    also index this many open pull requests, so
#                                 the context names those changing the same code
#   CODENIB_THREADS_FLAGS         extra flags for `codenib threads context`
#                                 (default: --sync)

dir=${DSH_SUPERAGENT_DIR:-}
number=${DSH_SUPERAGENT_GITHUB_NUMBER:-}
repo=${DSH_SUPERAGENT_GITHUB_REPOSITORY:-}
codenib=${CODENIB:-codenib}
output=THREAD_CONTEXT.md

skip() {
    echo "codenib thread context skipped: $1" >&2
    exit 0
}

[ -n "$dir" ] && [ -d "$dir" ] || skip "DSH_SUPERAGENT_DIR is not a directory"
[ -n "$number" ] || skip "the Ticket did not start from a GitHub issue or pull request"
[ -n "$repo" ] || skip "DSH_SUPERAGENT_GITHUB_REPOSITORY is empty"

if [ -n "${CODENIB_THREADS_OPEN_PULLS:-}" ]; then
    $codenib threads sync --open-pulls "$CODENIB_THREADS_OPEN_PULLS" \
        --repo "$repo" --quiet ||
        echo "codenib: open pull requests were not indexed" >&2
fi

# shellcheck disable=SC2086  # the flags are meant to split into words
$codenib threads context "$number" --repo "$repo" --checkout "$dir" \
    --budget "${CODENIB_THREADS_BUDGET:-3000}" --output "$dir/$output" \
    ${CODENIB_THREADS_FLAGS:---sync} ||
    skip "codenib threads context failed"

# Keep the file out of the worker's commits without touching .gitignore.
exclude=$(git -C "$dir" rev-parse --git-path info/exclude 2>/dev/null) || exit 0
case $exclude in /*) ;; *) exclude=$dir/$exclude ;; esac
mkdir -p "$(dirname "$exclude")"
grep -qxF "$output" "$exclude" 2>/dev/null || echo "$output" >>"$exclude"
exit 0
