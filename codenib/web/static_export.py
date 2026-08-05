# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Export a manifest-backed repository Wiki for static hosting."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import quoteattr

from .._version import package_version
from ..compiler.manifest import RepoManifest
from .launcher import find_frontend_dir
from .local import prepare_local_wiki

STATIC_EXPORT_SCHEMA_VERSION = "1.0"
STATIC_EXPORT_MANIFEST = "codenib-static.json"

_SENSITIVE_ENV_NAMES = {
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "CODENIB_DEMO_API_KEY",
    "GITHUB_TOKEN",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}
_SENSITIVE_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")
_ABSOLUTE_REFERENCE_RE = re.compile(r"(?:src|href)=(['\"])/(?!/)")
_DOCUMENT_BASE_RE = re.compile(r"<base\s+[^>]*href=(['\"])[^'\"]*\1[^>]*>", re.I)
_SAFE_BASE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&()*+,;=:@%/-]*$")
_SAFE_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class StaticExportResult:
    """Summary returned after publishing one static Wiki directory."""

    output_dir: Path
    manifest_path: Path
    repo_id: str
    page_count: int
    file_count: int


def normalize_base_path(value: str) -> str:
    """Return a canonical URL path used as the static site's mount point."""

    raw = (value or "/").strip()
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("base path must be a URL path without a host or query")
    if not raw.startswith("/") or "\\" in raw:
        raise ValueError("base path must start with '/' and use forward slashes")

    decoded = unquote(parsed.path)
    parts = PurePosixPath(decoded).parts
    if any(part in {".", ".."} for part in parts):
        raise ValueError("base path must not contain '.' or '..' segments")
    if not _SAFE_BASE_PATH_RE.fullmatch(parsed.path):
        raise ValueError("base path contains unsupported URL characters")

    normalized = "/" + "/".join(part for part in parts if part != "/")
    return "/" if normalized == "/" else normalized.rstrip("/")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes(root: Path, relative: str, content: bytes) -> Path:
    target = root.joinpath(*PurePosixPath(relative).parts).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"export path escapes the output directory: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _write_json(root: Path, relative: str, value: Any) -> Path:
    return _write_bytes(root, relative, _json_bytes(value))


def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return dict(value.dict())
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported repository metadata type: {type(value).__name__}")


def _page_ids(nodes: Iterable[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    for node in nodes:
        page_id = str(node.get("id") or "").strip()
        if not page_id:
            raise ValueError("Wiki page tree contains an empty page id")
        if not _SAFE_PAGE_ID_RE.fullmatch(page_id):
            raise ValueError(f"Wiki page id is not URL-safe: {page_id!r}")
        result.append(page_id)
        children = node.get("children") or ()
        if not isinstance(children, (list, tuple)):
            raise ValueError(f"Wiki page {page_id!r} has invalid children")
        result.extend(_page_ids(children))
    if len(result) != len(set(result)):
        raise ValueError("Wiki page tree contains duplicate page ids")
    return result


def _source_path(value: str) -> str:
    path = value.replace("\\", "/")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        raise ValueError(f"Wiki source path must be repository-relative: {path}")
    if any(part == ".." for part in parts):
        raise ValueError(f"Wiki source path contains traversal: {path}")
    return PurePosixPath(path).as_posix() if path else ""


def _normalize_source_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                _source_path(str(item))
                if key == "file" and item is not None
                else _normalize_source_fields(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_source_fields(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_source_fields(item) for item in value]
    return value


def _normalize_page(builder: Any, page: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(page)
    citations = []
    for value in payload.get("citations") or ():
        citation = dict(value)
        file = _source_path(str(citation.get("file") or ""))
        citation["file"] = file or None
        if file and not citation.get("content"):
            source = builder.source(
                file,
                citation.get("start_line"),
                citation.get("end_line"),
            )
            if source is not None:
                citation["content"] = source.get("content")
        citations.append(citation)
    payload["citations"] = citations

    if "generation" not in payload:
        payload["generation"] = {
            "mode": "offline",
            "model": None,
            "repaired": False,
        }
    if "grounding" not in payload:
        count = len(citations)
        payload["grounding"] = {
            "valid": True,
            "citation_coverage": 1.0,
            "cited_evidence": count,
            "evidence_count": count,
            "relation_count": 0,
        }
    return _normalize_source_fields(payload)


def _unavailable_page_graph(note: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "nodes": [],
        "edges": [],
        "mermaid": "",
    }
    if note:
        payload["note"] = note
    return payload


def _page_graph(bundle: Any, page: Mapping[str, Any]) -> dict[str, Any]:
    try:
        graph = bundle.code_graph()
        if graph is None:
            return _unavailable_page_graph()
        from .codemap import build_page_subgraph

        return _normalize_source_fields(
            build_page_subgraph(
                graph,
                page.get("citations") or (),
                repo_dir=bundle.entry.repo_dir,
                hierarchy_graph=bundle.hierarchical_graph(),
            )
        )
    except Exception:  # noqa: BLE001 - an optional graph must not block the Wiki
        return _unavailable_page_graph("The indexed dependency view was unavailable.")


def _prebuilt_frontend(explicit: str | os.PathLike[str] | None) -> Path:
    frontend = find_frontend_dir(explicit)
    if frontend is None:
        raise ValueError(
            "CodeNib Wiki frontend was not found; pass --frontend-dir or install "
            "a release wheel containing the prebuilt frontend"
        )
    if (frontend / "package.json").is_file():
        frontend = frontend / "dist"
    if not (frontend / "index.html").is_file():
        raise ValueError(
            f"prebuilt frontend not found under {frontend}; run "
            "`cd web && npm ci && npm run build` first"
        )
    return frontend


def _copy_frontend(source: Path, target: Path, *, base_path: str) -> None:
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"frontend contains a symbolic link: {candidate}")
    shutil.copytree(source, target, dirs_exist_ok=True)

    index = target / "index.html"
    index_text = index.read_text(encoding="utf-8")
    references = _DOCUMENT_BASE_RE.sub("", index_text)
    if base_path != "/" and _ABSOLUTE_REFERENCE_RE.search(references):
        raise ValueError(
            "prebuilt frontend contains root-relative assets and cannot be "
            "mounted below '/'; rebuild it with the current CodeNib frontend"
        )
    document_base = f"{base_path.rstrip('/')}/" if base_path != "/" else "/"
    base_element = f"<base href={quoteattr(document_base)}>"
    if _DOCUMENT_BASE_RE.search(index_text):
        index_text = _DOCUMENT_BASE_RE.sub(base_element, index_text, count=1)
    elif "<head>" in index_text:
        index_text = index_text.replace("<head>", f"<head>\n    {base_element}", 1)
    else:
        raise ValueError("prebuilt frontend index.html has no <head> element")
    index.write_text(index_text, encoding="utf-8")


def _runtime_config(base_path: str) -> bytes:
    data_base = f"{base_path.rstrip('/')}/data" if base_path != "/" else "/data"
    config = {
        "mode": "static",
        "basePath": base_path,
        "dataBase": data_base,
    }
    encoded = json.dumps(
        config, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return (
        f"window.__CODENIB_RUNTIME__ = Object.freeze({encoded});\n"
        'window.__CODENIB_API_BASE__ = "";\n'
    ).encode("utf-8")


def _not_found_page(base_path: str) -> bytes:
    root = f"{base_path.rstrip('/')}/" if base_path != "/" else "/"
    encoded_root = json.dumps(root)
    return (
        '<!doctype html><meta charset="utf-8"><title>CodeNib Wiki</title>'
        "<script>"
        f"const root={encoded_root};"
        "const route=location.pathname+location.search+location.hash;"
        "location.replace(root+'?__codenib_route='+encodeURIComponent(route));"
        "</script>"
    ).encode("utf-8")


def _view_provenance(manifest: RepoManifest) -> dict[str, Any]:
    result = {}
    for name, entry in sorted(manifest.indexes.items()):
        result[name] = {
            "status": entry.status,
            "current": manifest.index_is_current(name),
            "commit": entry.commit,
            "source_fingerprint": entry.source_fingerprint,
        }
    return result


def _github_repository_url(repo_path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    origin = result.stdout.strip()
    if origin.startswith("git@github.com:"):
        path = origin.split(":", 1)[1]
    else:
        parsed = urlsplit(origin)
        if (parsed.hostname or "").lower() != "github.com":
            return None
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        return None
    return f"https://github.com/{path}"


def _load_static_bundle(local: Any, manifest_path: Path) -> Any:
    """Load only the persisted views required to render static Wiki pages."""

    from ..index.sparse_idx.bm25_index import BM25CodeIndexer
    from .config import REGISTRY_FILENAME, load_registry
    from .repo_registry import RepoBundle

    manifest = RepoManifest.load(manifest_path)
    bm25_entry = manifest.indexes.get("bm25")
    if bm25_entry is None or not manifest.index_is_current("bm25"):
        raise ValueError("static Wiki export requires a current bm25 view")

    entries = load_registry(str(local.data_dir / REGISTRY_FILENAME))
    entry = next((item for item in entries if item.instance_id == local.repo_id), None)
    if entry is None:
        raise ValueError(f"prepared repository {local.repo_id!r} could not be loaded")

    def load_views(bundle: Any) -> None:
        index = BM25CodeIndexer()
        index.load_index(bm25_entry.path)
        bundle.bm25 = index
        bundle.vector_store = None

    return RepoBundle(
        entry=entry,
        manifest=manifest,
        chat_available=False,
        view_loader=load_views,
        runtime_loader=None,
    )


def _generation_summary(pages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    page_list = list(pages)
    modes = sorted(
        {
            str((page.get("generation") or {}).get("mode") or "offline")
            for page in page_list
        }
    )
    models = sorted(
        {
            str((page.get("generation") or {}).get("model"))
            for page in page_list
            if (page.get("generation") or {}).get("model")
        }
    )
    policies = sorted(
        {
            str((page.get("generation") or {}).get("renderer") or "index-derived")
            for page in page_list
        }
    )
    prompt_versions = sorted(
        {
            str((page.get("generation") or {}).get("prompt_version"))
            for page in page_list
            if (page.get("generation") or {}).get("prompt_version")
        }
    )
    grounded = sum(
        bool((page.get("grounding") or {}).get("valid")) for page in page_list
    )
    return {
        "modes": modes,
        "models": models,
        "policies": policies,
        "prompt_versions": prompt_versions,
        "page_count": len(page_list),
        "grounding_valid_pages": grounded,
    }


def _file_inventory(root: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == STATIC_EXPORT_MANIFEST:
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return files


def _secret_values(environ: Mapping[str, str]) -> list[bytes]:
    values = []
    for name, value in environ.items():
        upper = name.upper()
        sensitive = upper in _SENSITIVE_ENV_NAMES or upper.endswith(
            _SENSITIVE_ENV_SUFFIXES
        )
        if sensitive and len(value) >= 8:
            values.append(value.encode("utf-8"))
    return values


def _assert_publishable(
    root: Path,
    *,
    forbidden_paths: Iterable[Path],
    environ: Mapping[str, str],
) -> None:
    forbidden = [str(path.resolve()).encode("utf-8") for path in forbidden_paths]
    secrets = _secret_values(environ)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"static export contains a symbolic link: {path}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(value and value in data for value in forbidden):
            raise ValueError(
                "static export contains an absolute build-machine path in "
                f"{path.relative_to(root)}"
            )
        if any(secret in data for secret in secrets):
            raise ValueError(
                "static export contains a configured credential in "
                f"{path.relative_to(root)}"
            )


def _validated_output(repo_path: Path, output_dir: Path) -> Path:
    repo_path = repo_path.resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir == repo_path or repo_path in output_dir.parents:
        raise ValueError("static export output must be outside the target repository")
    if output_dir in repo_path.parents:
        raise ValueError("static export output must not contain the target repository")
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"static export output is not a directory: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()):
        if not (output_dir / STATIC_EXPORT_MANIFEST).is_file():
            raise ValueError(
                "refusing to replace a non-empty directory that is not a CodeNib "
                f"static export: {output_dir}"
            )
    return output_dir


def export_static_wiki(
    repo_path: Path,
    manifest_path: Path,
    output_dir: Path,
    *,
    frontend_dir: str | os.PathLike[str] | None = None,
    base_path: str = "/",
    environ: Mapping[str, str] | None = None,
) -> StaticExportResult:
    """Build a deterministic static Wiki from an existing repository manifest."""

    repo_path = repo_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    output_dir = _validated_output(repo_path, output_dir)
    base_path = normalize_base_path(base_path)
    frontend = _prebuilt_frontend(frontend_dir)
    environment = os.environ if environ is None else environ

    local = prepare_local_wiki(
        repo_path,
        manifest_path,
        frontend_port=0,
        agent_wiki=False,
    )

    from ..wiki import WikiBuilder

    bundle = _load_static_bundle(local, manifest_path)
    builder = WikiBuilder(bundle)

    tree = builder.page_tree()
    page_ids = _page_ids(tree)
    pages = []
    graphs: dict[str, dict[str, Any]] = {}
    for page_id in page_ids:
        page = builder.page(page_id)
        if page is None:
            raise ValueError(f"Wiki page tree references a missing page: {page_id}")
        normalized = _normalize_page(builder, page)
        pages.append(normalized)
        graphs[page_id] = _page_graph(bundle, normalized)

    repo_info = _model_dict(bundle.info())
    capabilities = {name: False for name in dict(repo_info.get("capabilities") or {})}
    capabilities.update(
        {
            "chat": False,
            "codemap": False,
            "edge_labels": False,
            "source_citations": True,
            "static_wiki": True,
            "wiki_graph": any(graph.get("available") for graph in graphs.values()),
        }
    )
    repo_info["capabilities"] = capabilities
    repo_info["problem_statement"] = ""
    repo_info["incremental"] = None
    repo_info["source_url"] = _github_repository_url(repo_path)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.tmp-",
            dir=str(output_dir.parent),
        )
    ).resolve()
    try:
        _copy_frontend(frontend, stage, base_path=base_path)
        _write_bytes(stage, "runtime-config.js", _runtime_config(base_path))
        _write_bytes(stage, "404.html", _not_found_page(base_path))
        _write_json(stage, "data/repos.json", [repo_info])

        repo_component = quote(local.repo_id, safe="")
        repo_root = f"data/repos/{repo_component}"
        _write_json(
            stage,
            f"{repo_root}/wiki.json",
            {"repo": bundle.entry.repo, "pages": tree},
        )
        _write_json(
            stage,
            f"{repo_root}/commits.json",
            {"available": False, "commits": [], "selected": None},
        )
        for page, page_id in zip(pages, page_ids, strict=True):
            component = quote(page_id, safe="")
            _write_json(stage, f"{repo_root}/pages/{component}.json", page)
            _write_json(
                stage,
                f"{repo_root}/page-graphs/{component}.json",
                graphs[page_id],
            )

        _assert_publishable(
            stage,
            forbidden_paths=(repo_path, manifest_path.parent),
            environ=environment,
        )
        source_manifest = bundle.manifest
        export_manifest = {
            "schema_version": STATIC_EXPORT_SCHEMA_VERSION,
            "repository": {
                "id": local.repo_id,
                "slug": bundle.entry.repo,
                "url": repo_info["source_url"],
                "commit": source_manifest.commit,
                "source_fingerprint": source_manifest.source_fingerprint,
                "languages": list(source_manifest.languages),
            },
            "builder": {
                "codenib_version": package_version(),
                "manifest_version": source_manifest.version,
                "profile": sorted(
                    name
                    for name in source_manifest.indexes
                    if source_manifest.index_is_current(name)
                ),
            },
            "source_locations": {
                "path": "repository-relative-posix",
                "line_base": 1,
                "end_line": "inclusive",
                "commit": source_manifest.commit,
            },
            "views": _view_provenance(source_manifest),
            "capabilities": capabilities,
            "generation": _generation_summary(pages),
            "base_path": base_path,
            "files": _file_inventory(stage),
        }
        manifest_file = _write_json(stage, STATIC_EXPORT_MANIFEST, export_manifest)
        _assert_publishable(
            stage,
            forbidden_paths=(repo_path, manifest_path.parent),
            environ=environment,
        )

        if output_dir.exists():
            shutil.rmtree(output_dir)
        stage.rename(output_dir)
        manifest_file = output_dir / manifest_file.relative_to(stage)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return StaticExportResult(
        output_dir=output_dir,
        manifest_path=manifest_file,
        repo_id=local.repo_id,
        page_count=len(pages),
        file_count=len(export_manifest["files"]),
    )


__all__ = [
    "STATIC_EXPORT_MANIFEST",
    "STATIC_EXPORT_SCHEMA_VERSION",
    "StaticExportResult",
    "export_static_wiki",
    "normalize_base_path",
]
