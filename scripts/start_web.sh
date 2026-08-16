#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Start the CodeNib FastAPI backend with either a local LLM or a hosted API
# profile. Run this in Terminal 2 (on your main machine).
# See docs/running-locally.md for the full setup guide.

set -e

CONDA_ENV="${CONDA_ENV:-codenib}"
BACKEND_PORT="${CODENIB_DEMO_PORT:-8000}"
LLM_PORT="${LLM_PORT:-8080}"
DEFAULT_GPU_HOST="vscode-dsmlp-l40s"
PROFILE="${CODENIB_DEMO_PROFILE:-local}"
MODEL_OVERRIDE="${CODENIB_DEMO_MODEL:-}"
CONFIG_PATH="${CODENIB_DEMO_CONFIG:-}"

case "$PROFILE" in
    local|api) ;;
    *)
        echo "ERROR: CODENIB_DEMO_PROFILE must be 'local' or 'api'."
        exit 1
        ;;
esac

if [ -z "$CONFIG_PATH" ]; then
    if [ "$PROFILE" = "api" ]; then
        CONFIG_PATH="qa_config.api.yaml"
    elif [ -f "qa_config.local.yaml" ]; then
        CONFIG_PATH="qa_config.local.yaml"
    else
        CONFIG_PATH="qa_config.yaml"
    fi
fi

# ── Prerequisites ────────────────────────────────────────────────────────────

echo "=== CodeNib Backend ==="
echo "Profile: $PROFILE"
echo ""

# Must run from repo root
if [ ! -f "$CONFIG_PATH" ]; then
    echo "ERROR: config not found: $CONFIG_PATH"
    echo "Run this script from the CodeNib repo root:"
    echo "  cd ~/projects/CodeNib/CodeNib"
    echo "  bash scripts/start_web.sh"
    echo ""
    if [ "$PROFILE" = "api" ]; then
        echo "Create the layered API profile:"
        echo "  cp qa_config.local.yaml.example qa_config.local.yaml"
        echo "  cp qa_config.api.yaml.example qa_config.api.yaml"
    else
        echo "For local overrides, copy qa_config.local.yaml.example to qa_config.local.yaml."
    fi
    exit 1
fi

# Check codenib is installed
if ! conda run -n "$CONDA_ENV" python -c "import codenib" &>/dev/null; then
    echo "ERROR: codenib not installed in conda env '$CONDA_ENV'."
    echo "Run:  make dev   (or pip install -e '.[dev]')"
    exit 1
fi

# Check qa_registry.json
CONFIG_DETAILS=$(CODENIB_DEMO_CONFIG="$CONFIG_PATH" \
    conda run -n "$CONDA_ENV" python -c "
from codenib.web.config import load_config
config = load_config()
print(config.registry_path)
print(config.model)
print(config.model_api_base or '')
print('1' if config.model_api_key else '0')
" 2>/dev/null) || {
    echo "ERROR: Could not load layered config: $CONFIG_PATH"
    exit 1
}
REGISTRY=$(printf '%s\n' "$CONFIG_DETAILS" | sed -n '1p')
CONFIG_MODEL=$(printf '%s\n' "$CONFIG_DETAILS" | sed -n '2p')
CONFIG_API_BASE=$(printf '%s\n' "$CONFIG_DETAILS" | sed -n '3p')
CONFIG_HAS_API_KEY=$(printf '%s\n' "$CONFIG_DETAILS" | sed -n '4p')
SELECTED_MODEL="${MODEL_OVERRIDE:-$CONFIG_MODEL}"
if [ ! -f "$REGISTRY" ]; then
    echo "ERROR: No repo registry found at $REGISTRY"
    echo ""
    echo "Index a repo first:"
    echo "  conda activate codenib"
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

# ── Model backend ─────────────────────────────────────────────────────────────

if [ "$PROFILE" = "local" ]; then
    GPU_HOST="${LLM_HOST:-}"

    if [ -z "$GPU_HOST" ]; then
        echo "Enter the hostname of the node running scripts/start_llm.sh."
        read -rp "GPU node hostname [${DEFAULT_GPU_HOST}]: " GPU_HOST
        GPU_HOST="${GPU_HOST:-$DEFAULT_GPU_HOST}"
    fi

    MODEL_ENDPOINT="http://${GPU_HOST}:${LLM_PORT}/v1"

    # Check LLM server is reachable.
    echo ""
    echo "Checking LLM server at $MODEL_ENDPOINT ..."
    if curl -sf --max-time 5 "${MODEL_ENDPOINT}/models" > /dev/null 2>&1; then
        echo "  LLM server: OK"
    else
        echo "  WARNING: Cannot reach LLM server at $MODEL_ENDPOINT"
        echo ""
        echo "  Make sure scripts/start_llm.sh is running on $GPU_HOST."
        echo "  If the hostname is wrong, re-run and enter the correct hostname."
        echo ""
        read -rp "  Continue anyway? [y/N]: " CONT
        [[ "$CONT" =~ ^[Yy]$ ]] || exit 1
    fi
else
    MODEL_ENDPOINT="${CONFIG_API_BASE:-provider default}"
    if [ "$CONFIG_HAS_API_KEY" != "1" ] \
        && [ -z "${CODENIB_DEMO_API_KEY:-}" ] \
        && [ -z "${DEEPSEEK_API_KEY:-}" ] \
        && [ -z "${OPENAI_API_KEY:-}" ]; then
        echo "WARNING: No hosted-model credential was found."
        echo "Set CODENIB_DEMO_API_KEY or the provider's standard key variable."
    fi
fi

# ── Selected profile ─────────────────────────────────────────────────────────

echo ""
echo "Using $PROFILE model profile: $SELECTED_MODEL"
echo "  Config: $CONFIG_PATH"
echo "  Endpoint: $MODEL_ENDPOINT"
echo "  Override with CODENIB_DEMO_MODEL, CODENIB_DEMO_CONFIG, or CODENIB_DEMO_PROFILE."

# ── Launch ────────────────────────────────────────────────────────────────────

echo ""
echo "Starting CodeNib backend..."
echo "  Backend:  http://localhost:$BACKEND_PORT"
echo "  Model:    $SELECTED_MODEL"
echo "  Endpoint: $MODEL_ENDPOINT"
echo "  Config:   $CONFIG_PATH"
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

export CODENIB_DEMO_CONFIG="$CONFIG_PATH"
export CODENIB_DEMO_PORT="$BACKEND_PORT"
if [ -n "$MODEL_OVERRIDE" ]; then
    export CODENIB_DEMO_MODEL="$MODEL_OVERRIDE"
fi

if [ "$PROFILE" = "local" ]; then
    export OPENAI_API_KEY="fake"
    export OPENAI_API_BASE="$MODEL_ENDPOINT"
    export CODENIB_DEMO_API_BASE="$MODEL_ENDPOINT"
    export CODENIB_DEMO_API_KEY="fake"
fi

conda run -n "$CONDA_ENV" --no-capture-output \
    python -m codenib.web.app
