"""Native identity recovery without changing benchmark/source receipt identity."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.contextbench_multiswe import solver_cwd
from scripts.contextbench_native_repos import native_repository
from scripts.contextbench_polybench import parser_for_repo


@pytest.mark.parametrize(
    "benchmark,native,iid,cwd",
    [
        ("grpc/grpc", "grpc/grpc-go", "grpc__grpc-go-12", "/home/grpc-go"),
        ("zeromicro/go", "zeromicro/go-zero", "zeromicro__go-zero-17", "/home/go-zero"),
        (
            "fasterxml/jackson",
            "fasterxml/jackson-core",
            "fasterxml__jackson-core-2",
            "/home/jackson-core",
        ),
        (
            "fasterxml/jackson",
            "fasterxml/jackson-databind",
            "fasterxml__jackson-databind-8",
            "/home/jackson-databind",
        ),
        (
            "fasterxml/jackson",
            "fasterxml/jackson-dataformat-xml",
            "fasterxml__jackson-dataformat-xml-3",
            "/home/jackson-dataformat-xml",
        ),
        ("mui/material", "mui/material-ui", "mui__material-ui-18", "/home/material-ui"),
    ],
)
def test_native_alias_uses_full_instance_repository(benchmark, native, iid, cwd):
    assert native_repository("Multi", benchmark, iid) == native
    assert native_repository("Multi", native, iid) == native
    assert solver_cwd("Multi", native) == cwd
    with pytest.raises(ValueError, match="benchmark metadata"):
        native_repository("Multi", "unrelated/repository", iid)


@pytest.mark.parametrize(
    "repo,iid",
    [
        (None, "grpc__grpc-go-1"),
        ("", "grpc__grpc-go-1"),
        ("fmtlib/fmt", "fmtlib__fmt-0"),
        ("fmtlib/fmt", "fmtlib__fmt-../1"),
        ("fmtlib/fmt", "google__gson-1"),
        ("unknown/repo", "unknown__repo-12"),
    ],
)
def test_native_alias_never_guesses_foreign_or_malformed_identity(repo, iid):
    with pytest.raises(ValueError, match="native Multi|Native Multi"):
        native_repository("Multi", repo, iid)


def test_other_corpora_keep_their_repository_identity():
    for source, repo, cwd in [
        ("Poly", "prettier/prettier", "/testbed"),
        ("Poly", "huggingface/transformers", "/testbed"),
        ("Pro", "NodeBB/NodeBB", "/app"),
    ]:
        assert native_repository(source, repo, "not-a-Multi-id") == repo
        assert solver_cwd(source, repo) == cwd


def test_poly_repository_parser_does_not_fall_back_to_javascript():
    js, python, jest = object(), object(), object()
    native = SimpleNamespace(
        parser=js,
        parser_classes={
            "huggingface/transformers": python,
            "prettier/prettier": jest,
        },
    )
    assert parser_for_repo(native, "huggingface/transformers") is python
    assert parser_for_repo(native, "prettier/prettier") is jest
    with pytest.raises(KeyError):
        parser_for_repo(native, "unknown/repo")
    legacy = SimpleNamespace(parser=js)
    assert parser_for_repo(legacy, "sveltejs/svelte") is js
    with pytest.raises(ValueError, match="unavailable"):
        parser_for_repo(legacy, "huggingface/transformers")


@pytest.mark.parametrize(
    "filename",
    ["contextbench_multiswe.py", "contextbench_polybench.py", "contextbench_pro.py"],
)
def test_native_corpus_cli_help_requires_only_standard_library(filename):
    path = Path(__file__).resolve().parents[2] / "scripts" / filename
    result = subprocess.run(
        [sys.executable, "-S", str(path), "--help"],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--solver-run" in result.stdout
