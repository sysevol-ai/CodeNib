#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
#
# Run the CodeNib demo frontend (Next.js production server) on the serving
# box. Assumes deploy_demo.sh already ran `npm run build`. Server-local
# settings come from ~/.codenib-demo.env. Normally started inside tmux.
set -euo pipefail

source "$HOME/.codenib-demo.env"
export NVM_DIR="$HOME/.nvm"
. "$NVM_DIR/nvm.sh"
nvm use --lts >/dev/null
cd "$CODENIB_REPO/web"
npm run start -- -H 127.0.0.1 -p 3000 2>&1 | tee "$HOME/nib-frontend.log"
