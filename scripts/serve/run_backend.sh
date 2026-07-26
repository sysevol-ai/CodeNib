#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Run the CodeNib demo backend (FastAPI) on the serving box. Server-local
# settings (paths, project ids, config file) come from ~/.codenib-demo.env,
# which is never committed. Normally started inside tmux by deploy_demo.sh.
set -euo pipefail

source "$HOME/.codenib-demo.env"
source "$CODENIB_VENV/bin/activate"
cd "$CODENIB_REPO"
python -m codenib.web.app 2>&1 | tee "$HOME/nib-backend.log"
