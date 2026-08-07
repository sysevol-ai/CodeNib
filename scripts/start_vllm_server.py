#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Script to start vLLM server with OpenAI-compatible API.

Usage:
    python scripts/start_vllm_server.py --model Qwen/Qwen2.5-Coder-7B
    python scripts/start_vllm_server.py --model microsoft/DialoGPT-medium --port 8001
"""

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _build_vllm_command(
    model: str,
    *,
    host: str = "localhost",
    port: int = 9000,
    revision: str | None = None,
    code_revision: str | None = None,
    tokenizer_revision: str | None = None,
    trust_remote_code: bool = False,
    max_model_len: int = 16384,
    gpu_memory_utilization: float = 0.5,
) -> list[str]:
    """Build a vLLM command without executing it."""
    is_local_model = Path(model).expanduser().is_dir()
    if trust_remote_code and not is_local_model:
        for option, value in (
            ("--revision", revision),
            ("--code-revision", code_revision),
        ):
            if not isinstance(value, str) or not _FULL_COMMIT_SHA.fullmatch(value):
                raise ValueError(
                    f"{option} must be a full 40-character lowercase commit SHA "
                    "when trusting remote model code"
                )

    cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        model,
        "--host",
        host,
        "--port",
        str(port),
        "--max-model-len",
        str(max_model_len),
        "--gpu-memory-utilization",
        str(gpu_memory_utilization),
    ]

    for option, value in (
        ("--revision", revision),
        ("--code-revision", code_revision),
        ("--tokenizer-revision", tokenizer_revision),
    ):
        if value:
            cmd.extend((option, value))
    if trust_remote_code:
        cmd.append("--trust-remote-code")
    return cmd


def start_vllm_server(
    model: str,
    host: str = "localhost",
    port: int = 9000,
    revision: str | None = None,
    code_revision: str | None = None,
    tokenizer_revision: str | None = None,
    trust_remote_code: bool = False,
    max_model_len: int = 16384,
    gpu_memory_utilization: float = 0.5,
):
    """Start vLLM server with OpenAI-compatible API."""

    cmd = _build_vllm_command(
        model,
        host=host,
        port=port,
        revision=revision,
        code_revision=code_revision,
        tokenizer_revision=tokenizer_revision,
        trust_remote_code=trust_remote_code,
        max_model_len=max_model_len,
        gpu_memory_utilization=gpu_memory_utilization,
    )

    print("Starting vLLM server with command:")
    print(shlex.join(cmd))
    print(f"\nServer will be available at: http://{host}:{port}/v1")
    print(
        "API documentation will be available at: http://{}:{}/docs".format(host, port)
    )
    print("\nPress Ctrl+C to stop the server.")

    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nShutting down vLLM server...")
    except subprocess.CalledProcessError as e:
        print(f"Error starting vLLM server: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Start vLLM server with OpenAI-compatible API"
    )
    parser.add_argument(
        "--model", required=True, help="Model name (e.g., Qwen/Qwen2.5-Coder-7B)"
    )
    parser.add_argument(
        "--host", default="localhost", help="Host to bind to (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=9000, help="Port to bind to (default: 9000)"
    )
    parser.add_argument(
        "--max-model-len",
        type=int,
        default=16384,
        help="Maximum model length (default: 16384)",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=0.5,
        help="GPU memory utilization (default: 0.5)",
    )
    parser.add_argument(
        "--revision",
        help="Model revision (use a full commit SHA for reproducible serving)",
    )
    parser.add_argument(
        "--code-revision",
        help="Custom model-code revision on the Hugging Face Hub",
    )
    parser.add_argument(
        "--tokenizer-revision",
        help="Tokenizer revision on the Hugging Face Hub",
    )
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        help=(
            "Execute Hugging Face model repository code. Remote models require "
            "full commit SHAs for --revision and --code-revision."
        ),
    )

    args = parser.parse_args()

    start_vllm_server(
        model=args.model,
        host=args.host,
        port=args.port,
        revision=args.revision,
        code_revision=args.code_revision,
        tokenizer_revision=args.tokenizer_revision,
        trust_remote_code=args.trust_remote_code,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
    )


if __name__ == "__main__":
    main()
