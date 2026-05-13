#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Script to start vLLM server with OpenAI-compatible API.

Usage:
    python scripts/start_vllm_server.py --model Qwen/Qwen2.5-Coder-7B
    python scripts/start_vllm_server.py --model microsoft/DialoGPT-medium --port 8001
"""

import argparse
import subprocess
import sys


def start_vllm_server(
    model: str,
    host: str = "localhost",
    port: int = 9000,
    trust_remote_code: bool = True,
    max_model_len: int = 4096,
    gpu_memory_utilization: float = 0.8,
):
    """Start vLLM server with OpenAI-compatible API."""

    cmd = [
        "python",
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

    if trust_remote_code:
        cmd.append("--trust-remote-code")

    print(f"Starting vLLM server with command:")
    print(" ".join(cmd))
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
        help="GPU memory utilization (default: 0.8)",
    )
    parser.add_argument(
        "--no-trust-remote-code", action="store_true", help="Disable trust remote code"
    )

    args = parser.parse_args()

    start_vllm_server(
        model=args.model,
        host=args.host,
        port=args.port,
        trust_remote_code=not args.no_trust_remote_code,
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_memory_utilization,
    )


if __name__ == "__main__":
    main()
