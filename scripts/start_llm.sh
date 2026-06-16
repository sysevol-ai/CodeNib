#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Start the llama-cpp-python OpenAI-compatible server on the GPU node.
# Run this in Terminal 1 (on the GPU node).
# See docs/running-locally.md for the full setup guide.

set -e

CONDA_ENV="${CONDA_ENV:-codeminer}"
PORT="${LLM_PORT:-8080}"
N_CTX="${N_CTX:-8192}"
DEFAULT_MODEL="$HOME/.ollama/models/blobs/sha256-60e05f2100071479f596b964f89f510f057ce397ea22f2833a0cfe029bfc2463"

# ── Prerequisites ────────────────────────────────────────────────────────────

echo "=== CodeMiner LLM Server ==="
echo ""

# Check conda env
if ! conda run -n "$CONDA_ENV" python -c "import llama_cpp" &>/dev/null; then
    echo "ERROR: llama-cpp-python not found in conda env '$CONDA_ENV'."
    echo ""
    echo "Install it with:"
    echo "  conda activate $CONDA_ENV"
    echo "  pip install 'llama-cpp-python[server]==0.3.29' \\"
    echo "    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124"
    exit 1
fi

# Check GPU support
GPU_OK=$(conda run -n "$CONDA_ENV" python -c "import llama_cpp; print(llama_cpp.llama_supports_gpu_offload())" 2>/dev/null)
if [ "$GPU_OK" != "True" ]; then
    echo "WARNING: llama-cpp-python reports GPU offload not supported."
    echo "         This likely means a CPU-only wheel is installed."
    echo ""
    echo "Reinstall with CUDA support:"
    echo "  pip install 'llama-cpp-python[server]==0.3.29' \\"
    echo "    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124"
    echo ""
    read -rp "Continue with CPU-only inference? (very slow) [y/N]: " CONT
    [[ "$CONT" =~ ^[Yy]$ ]] || exit 1
fi

# ── Model path ───────────────────────────────────────────────────────────────

MODEL_PATH="${LLAMA_MODEL:-}"

if [ -z "$MODEL_PATH" ]; then
    if [ -f "$DEFAULT_MODEL" ]; then
        echo "Found default model: $DEFAULT_MODEL"
        read -rp "Use this model? [Y/n]: " USE_DEFAULT
        if [[ ! "$USE_DEFAULT" =~ ^[Nn]$ ]]; then
            MODEL_PATH="$DEFAULT_MODEL"
        fi
    fi
fi

if [ -z "$MODEL_PATH" ]; then
    read -rp "Enter path to your GGUF model file: " MODEL_PATH
fi

if [ ! -f "$MODEL_PATH" ]; then
    echo "ERROR: Model file not found: $MODEL_PATH"
    echo ""
    echo "If using Ollama, models are in: ~/.ollama/models/blobs/"
    echo "Or download a GGUF from HuggingFace (e.g. Qwen/Qwen2.5-Coder-7B-Instruct-GGUF)"
    exit 1
fi

# ── Launch ───────────────────────────────────────────────────────────────────

echo ""
echo "Starting LLM server..."
echo "  Model:    $MODEL_PATH"
echo "  Context:  $N_CTX tokens"
echo "  Port:     $PORT"
echo "  GPU layers: all (-1)"
echo ""
echo "Server will be available at http://$(hostname):$PORT/v1"
echo "Pass this to start_web.sh as the GPU node hostname: $(hostname)"
echo ""

conda run -n "$CONDA_ENV" python -m llama_cpp.server \
    --model "$MODEL_PATH" \
    --n_gpu_layers -1 \
    --n_ctx "$N_CTX" \
    --port "$PORT" \
    --host 0.0.0.0
