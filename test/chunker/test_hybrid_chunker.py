#!/usr/bin/env python3
"""Tests for hybrid chunking logic using the httpie/cli repo fixture."""

from codeminer.code_chunker import CodeChunker, HybridChunkingConfig, RepoChunkingConfig


def test_hybrid_chunker_assigns_primary_secondary(httpie_cli_repo):
    chunker = CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(languages=["python"]),
        chunk_depth=2,
    )
    # Keep thresholds permissive so both tiers are populated on real repos
    config = HybridChunkingConfig(
        high_importance_ratio=0.2,
        min_high_importance_lines=1,
    )

    result = chunker.hybrid_chunk_repository(httpie_cli_repo, hybrid_config=config)

    assert result.primary_chunks, "No primary chunks returned"
    assert result.secondary_chunks, "No secondary chunks returned"
    assert len(result.all_chunks()) == len(result.primary_chunks) + len(
        result.secondary_chunks
    )

    # Primary chunks should have scores at least as high as the cutoff; secondary lower.
    primary_scores = [chunk["importance_score"] for chunk in result.primary_chunks]
    secondary_scores = [chunk["importance_score"] for chunk in result.secondary_chunks]

    assert all(score >= result.cutoff_score for score in primary_scores)
    assert any(score < result.cutoff_score for score in secondary_scores)

    for chunk in result.primary_chunks:
        assert chunk["importance_tier"] == "primary"

    for chunk in result.secondary_chunks:
        assert chunk["importance_tier"] == "secondary"

    print(
        f"Primary chunks: {len(result.primary_chunks)}, "
        f"Secondary chunks: {len(result.secondary_chunks)}"
    )
