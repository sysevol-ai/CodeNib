#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Simple test script for Vertex AI LLM generation using LiteLLMChat.
"""

import os

import pytest

from codenib.llm.litellm_chat import LiteLLMChat, human_message

pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def require_vertex_credentials():
    """Skip external provider tests unless Vertex credentials are explicit."""
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        pytest.skip("GOOGLE_APPLICATION_CREDENTIALS is not configured")


def test_vertex_ai_gemini():
    """Test that Vertex AI Gemini can successfully generate text."""
    print("Testing Vertex AI Gemini text generation...")

    llm = LiteLLMChat(
        model="vertex_ai/gemini-2.5-flash",
        temperature=0.7,
        max_tokens=1024,
    )

    response = llm.invoke(
        [human_message("What is machine learning? Please provide a brief explanation.")]
    )

    print(f"Response: {response}")
    assert response, "Response content is empty"
    assert response.strip(), "Response content is just whitespace"


def test_vertex_ai_gemini_flash():
    """Test that Vertex AI Gemini Flash can successfully generate text."""
    print("Testing Vertex AI Gemini Flash text generation...")

    llm = LiteLLMChat(
        model="vertex_ai/gemini-2.0-flash",
        temperature=0.7,
        max_tokens=150,
    )

    response = llm.invoke(
        [human_message("How many R's are in the word 'strawberry'? Count carefully.")]
    )

    print(f"Response: {response}")
    assert response, "Response content is empty"
    assert response.strip(), "Response content is just whitespace"


def test_anthropic_vertex():
    """Test Vertex AI with Anthropic Claude model."""
    print("Testing Vertex AI with Anthropic Claude...")

    llm = LiteLLMChat(
        model="vertex_ai/claude-sonnet-4@20250514",
        temperature=0.7,
        max_tokens=1024,
    )

    response = llm.invoke(
        [human_message("What are the benefits of using cloud computing?")]
    )

    print(f"Response: {response}")
    assert response, "Response content is empty"
    assert response.strip(), "Response content is just whitespace"


def print_config_info():
    """Print information about the current LLM configuration."""
    print("=== LLM Configuration Information ===")

    # Check environment variables
    env_vars = ["GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_REGION", "ANTHROPIC_API_KEY"]

    for var in env_vars:
        value = os.environ.get(var)
        if value:
            # Mask sensitive information
            if "key" in var.lower() or "credential" in var.lower():
                masked_value = value[:8] + "..." if len(value) > 8 else "***"
                print(f"  {var}: {masked_value}")
            else:
                print(f"  {var}: {value}")
        else:
            print(f"  {var}: Not set")

    print("=" * 40)

    # Print troubleshooting info
    print("\nTroubleshooting Vertex AI 404 errors:")
    print(
        "1. Ensure Vertex AI API is enabled: gcloud services enable aiplatform.googleapis.com"
    )
    print(
        "2. Set VERTEX_REGION (defaults to us-central1): export VERTEX_REGION=us-central1"
    )
    print("3. Check your service account has 'Vertex AI User' role")
    print("4. Verify the model is available in your region")
    print("=" * 40)


if __name__ == "__main__":
    print("Running Vertex AI LLM tests with LiteLLMChat...\n")

    # Print configuration info
    print_config_info()
    print()

    tests = [
        ("Vertex AI Gemini Pro", test_vertex_ai_gemini),
        ("Vertex AI Gemini Flash", test_vertex_ai_gemini_flash),
        ("Vertex AI Anthropic Claude", test_anthropic_vertex),
    ]

    results = []

    for test_name, test_func in tests:
        print(f"Running {test_name} test...")
        try:
            test_func()
        except Exception as exc:  # pragma: no cover - standalone diagnostics
            print(f"{test_name} failed: {exc}")
            success = False
        else:
            success = True
        results.append((test_name, success))
        print("-" * 50)

    # Print summary
    print("\n=== Test Results Summary ===")
    passed = 0
    for test_name, success in results:
        status = "PASSED" if success else "FAILED"
        print(f"{test_name}: {status}")
        if success:
            passed += 1

    print(f"\nTotal: {passed}/{len(results)} tests passed")

    if passed == len(results):
        print("All tests completed successfully!")
    else:
        print("Some tests failed - check your credentials and configuration.")
        print("\nTroubleshooting:")
        print(
            "1. Ensure GOOGLE_APPLICATION_CREDENTIALS points to a valid service account file"
        )
        print("2. Verify your Google Cloud project has Vertex AI API enabled")
        print("3. Check that your service account has the necessary permissions")
        print("4. For Anthropic models, ensure you have the required access")
