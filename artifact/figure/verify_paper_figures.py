#!/usr/bin/env python3
"""Verify deterministic paper outputs and PDF font embedding."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageStat
from scipy.fft import dctn

STRUCTURED_OUTPUTS = (
    "graphrag_vs_rerank.json",
    "agent_runtime.json",
    "paper_claims.json",
)

RASTER_OUTPUTS = (
    "agent_runtime_schematic.png",
    "eval_recall_at_k.png",
    "pareto_rerank.png",
    "graphrag_vs_rerank.png",
    "retrieval_ablation_suite.png",
    "profile_l0_vs_l2.png",
    "profile_query_time.png",
    "faiss_index_ablation.png",
    "lsp_replay_100.png",
    "lsp_replay_latency.png",
    "materialized_trace.png",
    "maintenance_lifecycle.png",
    "serving_lifecycle_suite.png",
    "agent_runtime.png",
    "agent_runtime_breakdown.png",
)

# Text rasterization varies across font packages while the plotted geometry and
# claim ledger remain stable. Exact hashes pass immediately; this normalized
# mean absolute error admits only small rendering differences.
MAX_DIMENSION_DRIFT = 0.02
MAX_PHASH_DISTANCE = 0.12
MAX_THUMBNAIL_MAE = 0.03

PDF_OUTPUTS = (
    "agent_runtime_schematic.pdf",
    "eval_recall_at_k.pdf",
    "pareto_rerank.pdf",
    "graphrag_vs_rerank.pdf",
    "retrieval_ablation_suite.pdf",
    "profile_l0_vs_l2.pdf",
    "profile_query_time.pdf",
    "faiss_index_ablation.pdf",
    "lsp_replay_100.pdf",
    "lsp_replay_latency.pdf",
    "materialized_trace.pdf",
    "maintenance_lifecycle.pdf",
    "serving_lifecycle_suite.pdf",
    "agent_runtime.pdf",
    "agent_runtime_breakdown.pdf",
)

FONT_ROW = re.compile(r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--actual-dir", type=Path, required=True)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def perceptual_hash(image: Image.Image) -> np.ndarray:
    pixels = np.asarray(
        image.convert("L").resize((32, 32), Image.Resampling.LANCZOS),
        dtype=float,
    )
    coefficients = dctn(pixels, type=2, norm="ortho")[:8, :8].ravel()[1:]
    return coefficients > np.median(coefficients)


def raster_metrics(expected: Path, actual: Path) -> tuple[float, float, float]:
    with Image.open(expected) as expected_image, Image.open(actual) as actual_image:
        expected_rgba = expected_image.convert("RGBA")
        actual_rgba = actual_image.convert("RGBA")
        dimension_drift = max(
            abs(actual_rgba.width - expected_rgba.width) / expected_rgba.width,
            abs(actual_rgba.height - expected_rgba.height) / expected_rgba.height,
        )
        hash_distance = float(
            np.mean(perceptual_hash(expected_rgba) != perceptual_hash(actual_rgba))
        )
        expected_thumb = expected_rgba.convert("L").resize(
            (32, 32), Image.Resampling.LANCZOS
        )
        actual_thumb = actual_rgba.convert("L").resize(
            (32, 32), Image.Resampling.LANCZOS
        )
        difference = ImageChops.difference(expected_thumb, actual_thumb)
        thumbnail_mae = ImageStat.Stat(difference).mean[0] / 255
        return dimension_drift, hash_distance, thumbnail_mae


def verify_json(expected: Path, actual: Path) -> None:
    expected_payload = json.loads(expected.read_text(encoding="utf-8"))
    actual_payload = json.loads(actual.read_text(encoding="utf-8"))
    if expected_payload != actual_payload:
        raise ValueError(f"structured output mismatch for {actual.name}")


def verify_deterministic(expected_dir: Path, actual_dir: Path) -> None:
    for name in STRUCTURED_OUTPUTS:
        expected = expected_dir / name
        actual = actual_dir / name
        if not expected.is_file():
            raise FileNotFoundError(f"missing expected output: {expected}")
        if not actual.is_file():
            raise FileNotFoundError(f"missing reproduced output: {actual}")
        verify_json(expected, actual)
        print(f"PASS structured {name}")

    for name in RASTER_OUTPUTS:
        expected = expected_dir / name
        actual = actual_dir / name
        if not expected.is_file():
            raise FileNotFoundError(f"missing expected output: {expected}")
        if not actual.is_file():
            raise FileNotFoundError(f"missing reproduced output: {actual}")
        expected_sha = sha256(expected)
        actual_sha = sha256(actual)
        if expected_sha == actual_sha:
            print(f"PASS deterministic {name} {actual_sha}")
            continue
        dimension_drift, hash_distance, thumbnail_mae = raster_metrics(expected, actual)
        if (
            dimension_drift > MAX_DIMENSION_DRIFT
            or hash_distance > MAX_PHASH_DISTANCE
            or thumbnail_mae > MAX_THUMBNAIL_MAE
        ):
            raise ValueError(
                f"raster output drift for {name}: dimension={dimension_drift:.6f}, "
                f"phash={hash_distance:.6f}, thumbnail_mae={thumbnail_mae:.6f}"
            )
        print(
            f"PASS visual {name} dimension={dimension_drift:.6f} "
            f"phash={hash_distance:.6f} thumbnail_mae={thumbnail_mae:.6f}"
        )


def verify_pdf_fonts(actual_dir: Path) -> None:
    pdffonts = shutil.which("pdffonts")
    if pdffonts is None:
        print("SKIP PDF font audit: pdffonts is not installed")
        return

    for name in PDF_OUTPUTS:
        path = actual_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"missing reproduced PDF: {path}")
        completed = subprocess.run(
            [pdffonts, str(path)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        rows = [
            match
            for line in completed.stdout.splitlines()
            if (match := FONT_ROW.search(line))
        ]
        if not rows:
            raise ValueError(f"pdffonts returned no font rows for {name}")
        if any(match.group(1) != "yes" for match in rows):
            raise ValueError(f"PDF contains an unembedded font: {name}")
        print(f"PASS embedded fonts {name} fonts={len(rows)}")


def verify_claim_ledger(actual_dir: Path) -> None:
    path = actual_dir / "paper_claims.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    claims = payload.get("claims")
    if payload.get("schema_version") != 1:
        raise ValueError("paper claim ledger has an unsupported schema")
    if payload.get("all_passed") is not True:
        raise ValueError("paper claim ledger did not pass")
    if not isinstance(claims, list) or not claims:
        raise ValueError("paper claim ledger contains no claims")
    if payload.get("claim_count") != len(claims):
        raise ValueError("paper claim ledger count does not match its records")
    claim_ids = [claim.get("id") for claim in claims]
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("paper claim ledger contains duplicate claim ids")
    failed = [
        claim_id
        for claim_id, claim in zip(claim_ids, claims, strict=True)
        if claim.get("passed") is not True
    ]
    if failed:
        raise ValueError(f"paper claim ledger contains failed claims: {failed}")
    print(f"PASS semantic paper claims claims={len(claims)}")


def main() -> None:
    args = parse_args()
    expected_dir = args.expected_dir.resolve()
    actual_dir = args.actual_dir.resolve()
    verify_deterministic(expected_dir, actual_dir)
    verify_claim_ledger(actual_dir)
    verify_pdf_fonts(actual_dir)


if __name__ == "__main__":
    main()
