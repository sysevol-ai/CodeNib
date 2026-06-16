#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Start the CodeMiner FastAPI backend pointed at a local LLM server.
# Run this in Terminal 2 (on your main machine).
# See docs/running-locally.md for the full setup guide.

set -e

CONDA_ENV="${CONDA_ENV:-codeminer}"
BACKEND_PORT="${CODEMINER_DEMO_PORT:-8000}"
LLM_PORT="${LLM_PORT:-8080}"
DEFAULT_GPU_HOST="vscode-dsmlp-l40s"

# ── Prerequisites ────────────────────────────────────────────────────────────

echo "=== CodeMiner Backend ==="
echo ""

# Must run from repo root
if [ ! -f "qa_config.yaml" ]; then
    echo "ERROR: qa_config.yaml not found."
    echo "Run this script from the CodeMiner repo root:"
    echo "  cd ~/projects/CodeMiner/CodeMiner"
    echo "  bash scripts/start_web.sh"
    exit 1
fi

# Check codeminer is installed
if ! conda run -n "$CONDA_ENV" python -c "import codeminer" &>/dev/null; then
    echo "ERROR: codeminer not installed in conda env '$CONDA_ENV'."
    echo "Run:  make dev   (or pip install -e '.[dev]')"
    exit 1
fi

# Check qa_registry.json
REGISTRY=".codeminer_qa/qa_registry.json"
if [ ! -f "$REGISTRY" ]; then
    echo "ERROR: No repo registry found at $REGISTRY"
    echo ""
    echo "Index a repo first:"
    echo "  conda activate codeminer"
    echo "  python scripts/index_repo.py repos/<your-repo>"
    echo ""
    echo "Or clone + index in one go:"
    echo "  git clone https://github.com/owner/repo repos/repo"
    echo "  python scripts/index_repo.py repos/repo"
    exit 1
fi

REPO_COUNT=$(python3 -c "import json; print(len(json.load(open('$REGISTRY'))))" 2>/dev/null || echo 0)
echo "Found $REPO_COUNT repo(s) in registry."
echo ""

# ── GPU node hostname ─────────────────────────────────────────────────────────

GPU_HOST="${LLM_HOST:-}"

if [ -z "$GPU_HOST" ]; then
    echo "Enter the hostname of the node running scripts/start_llm.sh."
    read -rp "GPU node hostname [${DEFAULT_GPU_HOST}]: " GPU_HOST
    GPU_HOST="${GPU_HOST:-$DEFAULT_GPU_HOST}"
fi

LLM_BASE="http://${GPU_HOST}:${LLM_PORT}/v1"

# Check LLM server is reachable
echo ""
echo "Checking LLM server at $LLM_BASE ..."
if curl -sf --max-time 5 "${LLM_BASE}/models" > /dev/null 2>&1; then
    echo "  LLM server: OK"
else
    echo "  WARNING: Cannot reach LLM server at $LLM_BASE"
    echo ""
    echo "  Make sure scripts/start_llm.sh is running on $GPU_HOST."
    echo "  If the hostname is wrong, re-run and enter the correct hostname."
    echo ""
    read -rp "  Continue anyway? [y/N]: " CONT
    [[ "$CONT" =~ ^[Yy]$ ]] || exit 1
fi

# ── Check qa_config.yaml model ───────────────────────────────────────────────

CURRENT_MODEL=$(python3 -c "import yaml; c=yaml.safe_load(open('qa_config.yaml')); print(c.get('model',''))" 2>/dev/null || echo "")
if [[ "$CURRENT_MODEL" != openai/* ]]; then
    echo ""
    echo "WARNING: qa_config.yaml has model: $CURRENT_MODEL"
    echo "         For local LLM, it should be: openai/qwen2.5-coder"
    echo ""
    read -rp "Update qa_config.yaml automatically? [Y/n]: " FIX
    if [[ ! "$FIX" =~ ^[Nn]$ ]]; then
        python3 -c "
import re, sys
with open('qa_config.yaml') as f: content = f.read()
content = re.sub(r'^model:.*$', 'model: openai/qwen2.5-coder', content, flags=re.MULTILINE)
with open('qa_config.yaml', 'w') as f: f.write(content)
print('  Updated qa_config.yaml: model: openai/qwen2.5-coder')
"
    fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────

echo ""
echo "Starting CodeMiner backend..."
echo "  Backend:  http://localhost:$BACKEND_PORT"
echo "  LLM:      $LLM_BASE"
echo ""
echo "─────────────────────────────────────────────"
echo "Once backend is up, start the frontend in"
echo "a SEPARATE terminal:"
echo ""
echo "  cd $(pwd)/web && npm run dev"
echo ""
echo "Then open: http://localhost:3000"
echo "─────────────────────────────────────────────"
echo ""

export OPENAI_API_KEY="fake"
export OPENAI_API_BASE="$LLM_BASE"
export CODEMINER_DEMO_PORT="$BACKEND_PORT"

conda run -n "$CONDA_ENV" --no-capture-output \
    env OPENAI_API_KEY=fake OPENAI_API_BASE="$LLM_BASE" \
    python -m codeminer.web.app
