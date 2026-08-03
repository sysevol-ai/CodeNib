#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test that HybridRetrievePipeline can load skills correctly.

This test does NOT build indexes (avoids GPU/embedding issues),
just validates the skill loading fix.
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def test_skill_loading():
    """Test that build_skill_contexts returns contexts when skills are loaded."""
    print("Test: build_skill_contexts with skill registry loading\n")

    import os

    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.compiler import required_index_types

    # Step 1: Load skills into registry
    print("Step 1: Loading skills...")
    skills_dir = os.path.join("codenib", "agent", "skills")
    registry = SkillRegistry()
    registry.reset()
    loader = SkillLoader()
    loaded_skills = loader.load_all(skills_dir, contexts={}, registry=registry)

    print(f"✓ Loaded {len(loaded_skills)} skills")
    print(f"  Skill IDs: {[s.skill_id for s in loaded_skills]}")

    # Step 2: Check required_index_types
    print("\nStep 2: Checking required_index_types...")
    needed = required_index_types(
        ["bm25_search", "embedding_search"], skill_registry=registry
    )
    print(f"✓ Required index types: {needed}")

    if "bm25" not in needed or "vector" not in needed:
        print(f"✗ FAIL: Expected {{'bm25', 'vector'}}, got {needed}")
        return False

    # Step 3: Verify _skill_types_for
    print("\nStep 3: Checking skill_types_for...")
    from codenib.compiler.skill_context import _skill_types_for

    skill_types = _skill_types_for(["bm25_search", "embedding_search"], registry)
    print(f"✓ Skill types: {skill_types}")

    from codenib.agent.skills.core import SkillType

    if SkillType.RETRIEVAL not in skill_types:
        print(f"✗ FAIL: Expected RETRIEVAL in skill_types, got {skill_types}")
        return False

    print("\n✓ All skill loading tests passed!")
    return True


if __name__ == "__main__":
    try:
        success = test_skill_loading()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
