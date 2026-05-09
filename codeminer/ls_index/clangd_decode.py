#!/usr/bin/env python3
"""
Clangd .idx decoder: parse binary RIFF .idx files and build CodeGraph.

.idx format (clangd BackgroundIndex):
  RIFF container (form type "CdIx") with chunks:
    meta - version (uint32)
    stri - zlib-compressed string table (null-separated)
    symb - symbols
    refs - references
    rela - relations
    srcs - include graph / sources
    cmdl - compile command

Two-pass approach for graph building:
  Pass 1: Parse all .idx files, collect symbols/refs/relations
  Pass 2: Resolve definitions, build graph nodes and edges
"""

import logging
import struct
import zlib
from pathlib import Path
from typing import Dict, Optional, Tuple

from ..code_chunking import CppCodeChunker
from ..graph.code_graph import CodeGraph
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    ROOT_NODE,
)

logger = logging.getLogger(__name__)


# ===================================================================
# Constants
# ===================================================================

# RefKind bitmask (from clangd/index/Ref.h)
REF_KIND_DECLARATION = 0x1
REF_KIND_DEFINITION = 0x2
REF_KIND_REFERENCE = 0x4
REF_KIND_SPELLED = 0x8

# SymbolKind integer constants for programmatic use
KIND_MACRO = 4
KIND_STRUCT = 6
KIND_CLASS = 7
KIND_FUNCTION = 12
KIND_FIELD = 14
KIND_INSTANCE_METHOD = 16
KIND_CLASS_METHOD = 17
KIND_STATIC_METHOD = 18
KIND_CONSTRUCTOR = 22
KIND_DESTRUCTOR = 23

# Relation predicates
RELATION_BASE_OF = 0
RELATION_OVERRIDDEN_BY = 1

# Zero SymbolID (8 bytes = 16 hex chars)
ZERO_SYMBOL_ID = "0" * 16


# ===================================================================
# Low-level RIFF / .idx parsing
# ===================================================================


def read_riff(data: bytes) -> dict:
    """Parse RIFF container into chunks."""
    assert data[:4] == b"RIFF", f"Not a RIFF file, got {data[:4]!r}"
    total_len = struct.unpack_from("<I", data, 4)[0]
    form_type = data[8:12]
    assert form_type == b"CdIx", f"Not a clangd index, form type: {form_type!r}"

    offset = 12
    chunks = {}
    while offset < 8 + total_len:
        chunk_id = data[offset : offset + 4].decode("ascii")
        chunk_len = struct.unpack_from("<I", data, offset + 4)[0]
        chunk_data = data[offset + 8 : offset + 8 + chunk_len]
        chunks[chunk_id] = chunk_data
        offset += 8 + chunk_len
        if chunk_len % 2 == 1:
            offset += 1  # RIFF padding
    return chunks


def decode_string_table(stri_data: bytes) -> list:
    """Decode zlib-compressed string table."""
    uncompressed_size = struct.unpack_from("<I", stri_data, 0)[0]
    compressed = stri_data[4:]
    if uncompressed_size == 0:
        raw = compressed
    else:
        raw = zlib.decompress(compressed)
    strings = raw.split(b"\x00")
    if strings and strings[-1] == b"":
        strings = strings[:-1]
    return [s.decode("utf-8", errors="replace") for s in strings]


def read_varint(data: bytes, offset: int) -> tuple:
    """Read clangd-style varint (7 bits per byte, MSB = continue)."""
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


def decode_location(data: bytes, offset: int, strings: list) -> tuple:
    """Decode a SymbolLocation: FileURI (varint idx), Start, End."""
    file_idx, offset = read_varint(data, offset)
    start_line, offset = read_varint(data, offset)
    start_col, offset = read_varint(data, offset)
    end_line, offset = read_varint(data, offset)
    end_col, offset = read_varint(data, offset)

    file_uri = strings[file_idx] if file_idx < len(strings) else f"<idx:{file_idx}>"
    return {
        "file": file_uri,
        "start": (start_line, start_col),
        "end": (end_line, end_col),
    }, offset


# ===================================================================
# Chunk decoders
# ===================================================================


def decode_symbols(symb_data: bytes, strings: list) -> list:
    """Decode the symb chunk into a list of symbol records."""
    symbols = []
    offset = 0

    while offset < len(symb_data):
        sym = {}

        # SymbolID: 8 bytes
        if offset + 8 > len(symb_data):
            break
        sym["id"] = symb_data[offset : offset + 8].hex()
        offset += 8

        # SymbolKind: uint8
        sym["kind"] = symb_data[offset]
        offset += 1

        # SymbolLanguage: uint8
        sym["language"] = symb_data[offset]
        offset += 1

        # Name: varint string index
        name_idx, offset = read_varint(symb_data, offset)
        sym["name"] = strings[name_idx] if name_idx < len(strings) else ""

        # Scope: varint string index
        scope_idx, offset = read_varint(symb_data, offset)
        sym["scope"] = strings[scope_idx] if scope_idx < len(strings) else ""

        # TemplateSpecializationArgs: varint string index
        tpl_idx, offset = read_varint(symb_data, offset)
        sym["template_args"] = strings[tpl_idx] if tpl_idx < len(strings) else ""

        # Definition location
        try:
            sym["definition"], offset = decode_location(symb_data, offset, strings)
        except (IndexError, struct.error):
            sym["definition"] = None
            break

        # CanonicalDeclaration location
        try:
            sym["declaration"], offset = decode_location(symb_data, offset, strings)
        except (IndexError, struct.error):
            sym["declaration"] = None
            break

        # References count: varint
        refs_count, offset = read_varint(symb_data, offset)
        sym["references"] = refs_count

        # Flags: uint8
        if offset < len(symb_data):
            sym["flags"] = symb_data[offset]
            offset += 1

        # Signature: varint string index
        sig_idx, offset = read_varint(symb_data, offset)
        sym["signature"] = strings[sig_idx] if sig_idx < len(strings) else ""

        # CompletionSnippetSuffix: varint string index
        snip_idx, offset = read_varint(symb_data, offset)

        # Documentation: varint string index
        doc_idx, offset = read_varint(symb_data, offset)

        # ReturnType: varint string index
        ret_idx, offset = read_varint(symb_data, offset)
        sym["return_type"] = strings[ret_idx] if ret_idx < len(strings) else ""

        # Type: varint string index
        type_idx, offset = read_varint(symb_data, offset)

        # IncludeHeaders
        n_headers, offset = read_varint(symb_data, offset)
        for _ in range(n_headers):
            _h_idx, offset = read_varint(symb_data, offset)
            _refs_and_dir, offset = read_varint(symb_data, offset)

        symbols.append(sym)

    return symbols


def decode_refs(refs_data: bytes, strings: list) -> list:
    """Decode the refs chunk into a list of per-symbol reference records."""
    refs = []
    offset = 0

    while offset < len(refs_data):
        if offset + 8 > len(refs_data):
            break
        sym_id = refs_data[offset : offset + 8].hex()
        offset += 8

        num_refs, offset = read_varint(refs_data, offset)

        sym_refs = []
        for _ in range(num_refs):
            if offset >= len(refs_data):
                break
            kind = refs_data[offset]
            offset += 1

            try:
                loc, offset = decode_location(refs_data, offset, strings)
            except (IndexError, struct.error):
                break

            if offset + 8 > len(refs_data):
                container_id = ""
            else:
                container_id = refs_data[offset : offset + 8].hex()
                offset += 8

            sym_refs.append(
                {
                    "kind": kind,
                    "location": loc,
                    "container": container_id,
                }
            )

        refs.append({"symbol_id": sym_id, "refs": sym_refs})

    return refs


def decode_relations(rela_data: bytes) -> list:
    """Decode the rela chunk into a list of relation records."""
    relations = []
    offset = 0

    while offset < len(rela_data):
        if offset + 17 > len(rela_data):  # 8 + 1 + 8
            break
        subject = rela_data[offset : offset + 8].hex()
        offset += 8
        predicate = rela_data[offset]
        offset += 1
        obj = rela_data[offset : offset + 8].hex()
        offset += 8

        relations.append(
            {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
            }
        )

    return relations


def parse_idx_file(filepath: str) -> dict:
    """
    Parse a single .idx file and return structured data.

    Returns:
        dict with keys: 'symbols', 'refs', 'relations', 'version'
    """
    data = Path(filepath).read_bytes()
    chunks = read_riff(data)

    strings = []
    if "stri" in chunks:
        strings = decode_string_table(chunks["stri"])

    version = None
    if "meta" in chunks:
        version = struct.unpack("<I", chunks["meta"])[0]

    symbols = []
    if "symb" in chunks:
        symbols = decode_symbols(chunks["symb"], strings)

    refs = []
    if "refs" in chunks:
        refs = decode_refs(chunks["refs"], strings)

    relations = []
    if "rela" in chunks:
        relations = decode_relations(chunks["rela"])

    return {
        "symbols": symbols,
        "refs": refs,
        "relations": relations,
        "version": version,
    }


# ===================================================================
# ClangdGraphDecoder — .idx directory → CodeGraph
# ===================================================================


class ClangdGraphDecoder:
    """
    Decoder that builds a CodeGraph from clangd .idx files.

    Usage:
        decoder = ClangdGraphDecoder(
            idx_directory=".cache/clangd/index",
            project_root="/path/to/project"
        )
        graph = decoder.decode()
    """

    def __init__(self, idx_directory: str, project_root: str):
        self.idx_directory = Path(idx_directory)
        self.project_root = Path(project_root).resolve()
        self.code_graph = CodeGraph(str(self.project_root))

        # Accumulated data from all .idx files (Pass 1)
        self._symbols = {}  # sym_id -> symbol record dict
        self._refs = {}  # sym_id -> [ref record, ...]
        self._relations = []  # [relation record, ...]

        # Mappings built during Pass 2
        self._id_to_display = (
            {}
        )  # sym_id -> display name (e.g. "Namespace::Class::method")
        self._indexed_files = set()
        self._indexed_directories = set()

        # Buffered edges for batch insert — filled by _queue_edge during the
        # three _add_*_edges passes, flushed once at the end of _build_graph.
        # Preserves CodeGraph._add_edge's first-wins dedup semantics.
        self._pending_edges: Dict[Tuple[int, int], str] = {}

        # Code chunker for range detection
        self._chunker = CppCodeChunker()
        self._chunks_cache = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decode(self) -> CodeGraph:
        """Main entry point. Parse all .idx files and build CodeGraph."""
        logger.info(f"Starting clangd idx decode from {self.idx_directory}")

        # Pass 1: collect all data
        self._collect_all_idx()

        # Build name mapping
        self._build_id_to_display()

        # Pass 2: build the graph
        self.code_graph.add_root_node(ROOT_NODE)
        self._build_graph()

        return self.code_graph

    def save_graph(self, output_path: str):
        """Save the code graph to a file."""
        self.code_graph.save_graph(output_path)

    # ------------------------------------------------------------------
    # Pass 1: Collect
    # ------------------------------------------------------------------

    def _collect_all_idx(self):
        """Parse all .idx files and merge symbols/refs/relations."""
        idx_files = sorted(self.idx_directory.glob("*.idx"))
        if not idx_files:
            logger.warning(f"No .idx files found in {self.idx_directory}")
            return

        for idx_file in idx_files:
            try:
                parsed = parse_idx_file(str(idx_file))
            except Exception as e:
                logger.warning(f"Failed to parse {idx_file.name}: {e}")
                continue

            # Merge symbols (first-seen wins for same ID)
            for sym in parsed["symbols"]:
                sym_id = sym["id"]
                if sym_id not in self._symbols:
                    self._symbols[sym_id] = sym
                else:
                    # If existing has no valid definition but this one does, update
                    existing_def = self._symbols[sym_id].get("definition")
                    new_def = sym.get("definition")
                    if (
                        not existing_def or existing_def.get("file", "") == ""
                    ) and new_def:
                        self._symbols[sym_id] = sym

            # Merge refs (union all)
            for ref_group in parsed["refs"]:
                sym_id = ref_group["symbol_id"]
                if sym_id not in self._refs:
                    self._refs[sym_id] = []
                self._refs[sym_id].extend(ref_group["refs"])

            # Merge relations
            self._relations.extend(parsed["relations"])

        logger.info(
            f"Collected {len(self._symbols)} symbols, "
            f"{sum(len(v) for v in self._refs.values())} refs, "
            f"{len(self._relations)} relations"
        )

    # ------------------------------------------------------------------
    # Name mapping
    # ------------------------------------------------------------------

    def _build_id_to_display(self):
        """Build sym_id -> display name mapping from symbols."""
        for sym_id, sym in self._symbols.items():
            name = sym.get("name", "")
            scope = sym.get("scope", "")
            qualified = f"{scope}{name}" if scope else name
            self._id_to_display[sym_id] = qualified

    def _resolve_sym_id(self, sym_id: str) -> Optional[str]:
        """Resolve a symbol ID to its vertex key, or None if unknown.

        Returns sym_id itself as the vertex key (each overload gets its
        own node). Returns None if the sym_id was not seen during parsing.
        """
        if sym_id in self._id_to_display:
            return sym_id
        return None

    def _sym_id_to_display_name(self, sym_id: str) -> str:
        """Get the display name for a symbol ID (e.g. 'Ns::Class::method')."""
        return self._id_to_display.get(sym_id, "")

    # ------------------------------------------------------------------
    # File URI handling
    # ------------------------------------------------------------------

    def _file_uri_to_relative(self, uri: str) -> str:
        """Convert file URI to project-relative path."""
        if uri.startswith("file://"):
            abs_path = uri[7:]
        else:
            abs_path = uri

        try:
            rel = Path(abs_path).relative_to(self.project_root)
            return str(rel)
        except ValueError:
            return abs_path

    # ------------------------------------------------------------------
    # Kind mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _kind_to_node_type(kind: int) -> str:
        """Map clangd SymbolKind to CodeGraph NODE_TYPE."""
        if kind in (KIND_STRUCT, KIND_CLASS):
            return NODE_TYPE_CLASS
        elif kind == KIND_FUNCTION:
            return NODE_TYPE_FUNCTION
        elif kind in (
            KIND_INSTANCE_METHOD,
            KIND_CLASS_METHOD,
            KIND_STATIC_METHOD,
            KIND_CONSTRUCTOR,
            KIND_DESTRUCTOR,
        ):
            return NODE_TYPE_METHOD
        elif kind == KIND_FIELD:
            return NODE_TYPE_FIELD
        else:
            return NODE_TYPE_SYMBOL

    # ------------------------------------------------------------------
    # Definition resolution
    # ------------------------------------------------------------------

    def _resolve_definition(self, sym_id: str) -> dict:
        """
        Resolve the primary definition location for a symbol.

        Priority:
          1. [Definition|Spelled] ref -> hand-written, not macro-expanded
          2. [Definition] ref (no Spelled) -> macro expansion site
          3. symb definition -> fallback
        """
        result = {
            "file": None,
            "line": 0,
            "col": 0,
            "macro_expanded": False,
            "spelling_file": None,
            "spelling_line": None,
        }

        ref_list = self._refs.get(sym_id, [])
        sym = self._symbols.get(sym_id)

        # Collect definition refs
        spelled_defs = []
        expansion_defs = []
        for ref in ref_list:
            kind = ref["kind"]
            is_def = kind & REF_KIND_DEFINITION
            is_spelled = kind & REF_KIND_SPELLED
            if is_def:
                if is_spelled:
                    spelled_defs.append(ref)
                else:
                    expansion_defs.append(ref)

        # Priority 1: Spelled Definition
        if spelled_defs:
            loc = spelled_defs[0]["location"]
            result["file"] = loc["file"]
            result["line"] = loc["start"][0]
            result["col"] = loc["start"][1]
            result["macro_expanded"] = False
            return result

        # Priority 2: non-Spelled Definition (possible macro expansion)
        if expansion_defs:
            loc = expansion_defs[0]["location"]
            result["file"] = loc["file"]
            result["line"] = loc["start"][0]
            result["col"] = loc["start"][1]
            if sym and sym.get("definition"):
                sym_def = sym["definition"]
                if sym_def.get("file"):
                    spelling_file = sym_def["file"]
                    spelling_line = sym_def["start"][0]
                    expansion_file = loc["file"]
                    expansion_line = loc["start"][0]
                    if (
                        spelling_file != expansion_file
                        or spelling_line != expansion_line
                    ):
                        result["macro_expanded"] = True
                        result["spelling_file"] = spelling_file
                        result["spelling_line"] = spelling_line
            return result

        # Priority 3: symb definition (fallback)
        if sym and sym.get("definition") and sym["definition"].get("file"):
            sym_def = sym["definition"]
            result["file"] = sym_def["file"]
            result["line"] = sym_def["start"][0]
            result["col"] = sym_def["start"][1]
            return result

        return result

    # ------------------------------------------------------------------
    # Range detection via CppCodeChunker
    # ------------------------------------------------------------------

    def _find_range(self, file_path: str, line: int) -> Tuple[int, int]:
        """Find the scope range for a symbol at the given line."""
        chunks = self._get_file_chunks(file_path)
        if chunks:
            for chunk in chunks:
                if chunk.start_line <= line <= chunk.end_line:
                    return (chunk.start_line, chunk.end_line)
        return (line, line)

    def _get_file_chunks(self, file_path: str):
        """Get code chunks for a file, with caching."""
        if file_path in self._chunks_cache:
            return self._chunks_cache[file_path]

        full_path = self.project_root / file_path
        if not full_path.exists():
            self._chunks_cache[file_path] = None
            return None

        try:
            chunks = self._chunker.chunk_file(str(full_path))
            self._chunks_cache[file_path] = chunks
            return chunks
        except Exception as e:
            logger.debug(f"Error chunking {file_path}: {e}")
            self._chunks_cache[file_path] = None
            return None

    # ------------------------------------------------------------------
    # Pass 2: Build graph
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Build the complete CodeGraph from collected data."""
        self._add_symbol_nodes()
        self._add_contain_edges()
        self._add_reference_edges()
        self._add_relation_edges()
        self._flush_edges()

    # ------------------------------------------------------------------
    # Batch-edge helpers — accumulate (src_id, tgt_id) -> type during the
    # three edge-adding passes and flush once at the end. igraph's
    # per-edge add_edge() was 72% of decode wall time on fmt (483 .idx →
    # 27k edges); the single batched add_edges() call collapses that to
    # under 1% of wall time.
    # ------------------------------------------------------------------

    def _queue_edge(self, src_name: str, tgt_name: str, edge_type: str) -> None:
        """Buffer an edge for the batch flush.

        Mirrors CodeGraph._add_edge's first-wins dedup on (src, tgt): if a
        pair was already queued with any type, subsequent calls are no-ops
        so the earlier edge's type survives.
        """
        n2v = self.code_graph.name_to_vertex
        src_id = n2v.get(src_name)
        tgt_id = n2v.get(tgt_name)
        if src_id is None or tgt_id is None:
            return
        key = (src_id, tgt_id)
        if key not in self._pending_edges:
            self._pending_edges[key] = edge_type

    def _flush_edges(self) -> None:
        """Insert all queued edges in one batched igraph call."""
        if not self._pending_edges:
            return
        g = self.code_graph.graph
        # Skip pairs that already exist on the graph (edges added during
        # _add_symbol_nodes via _ensure_file_hierarchy go through the serial
        # _add_edge path).
        existing = set(map(tuple, g.get_edgelist()))
        pairs, types = [], []
        for pair, etype in self._pending_edges.items():
            if pair in existing:
                continue
            pairs.append(pair)
            types.append(etype)
        if pairs:
            g.add_edges(pairs, attributes={"type": types})
        self._pending_edges.clear()

    def _ensure_file_hierarchy(self, relative_path: str):
        """Add file and directory nodes to the graph if not already present."""
        if relative_path in self._indexed_files:
            return
        self._indexed_files.add(relative_path)

        # Build directory hierarchy
        dir_path = Path(relative_path).parent
        while dir_path != dir_path.parent and str(dir_path) != ".":
            dir_path_str = str(dir_path)
            if dir_path_str not in self._indexed_directories:
                self.code_graph.add_directory_node(dir_path_str)
                self._indexed_directories.add(dir_path_str)
                parent = str(dir_path.parent)
                if parent == ".":
                    parent = ROOT_NODE
                self.code_graph._add_edge(parent, dir_path_str, EDGE_TYPE_CONTAIN)
            dir_path = dir_path.parent

        # Add file node
        self.code_graph.add_file_node(relative_path)
        parent_dir = str(Path(relative_path).parent)
        if parent_dir == ".":
            parent_dir = ROOT_NODE
        self.code_graph._add_edge(parent_dir, relative_path, EDGE_TYPE_CONTAIN)

    def _add_symbol_nodes(self):
        """Add all symbol nodes to the graph.

        Uses sym_id as the unique vertex key so that overloaded functions
        each get their own node.  The human-readable qualified name is
        stored in the ``display_name`` attribute.
        """
        for sym_id, sym in self._symbols.items():
            kind = sym.get("kind", 0)

            # Skip macros
            if kind == KIND_MACRO:
                continue

            node_type = self._kind_to_node_type(kind)
            qualified_name = self._sym_id_to_display_name(sym_id)
            if not qualified_name:
                continue

            # Resolve definition location
            def_info = self._resolve_definition(sym_id)
            if not def_info["file"]:
                continue

            # Convert file URI to relative path
            relative_file = self._file_uri_to_relative(def_info["file"])

            # Skip symbols defined outside the project root
            if relative_file.startswith("/"):
                continue
            # Skip vendored/third-party code
            if "third_party/" in relative_file:
                continue

            # Ensure file hierarchy exists
            self._ensure_file_hierarchy(relative_file)

            # Find range
            line = def_info["line"]
            scope_start, scope_end = self._find_range(relative_file, line)

            # Add node — sym_id is the vertex key (unique per overload)
            node_key = sym_id
            self.code_graph.add_symbol_node(
                node_key,
                line,
                scope_start_line=scope_start,
                scope_end_line=scope_end,
                symbol_type=node_type,
            )

            # Set extra clangd-specific attributes
            if node_key in self.code_graph.name_to_vertex:
                vid = self.code_graph.name_to_vertex[node_key]
                # unified_name uses "." as member separator (cross-language convention)
                unified_display = qualified_name.replace("::", ".")
                if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
                    unified_display = f"{unified_display}()"
                self.code_graph.graph.vs[vid][
                    "unified_name"
                ] = f"{relative_file}:{unified_display}"
                self.code_graph.graph.vs[vid]["file"] = relative_file
                self.code_graph.graph.vs[vid]["macro_expanded"] = def_info[
                    "macro_expanded"
                ]
                if def_info["spelling_file"]:
                    self.code_graph.graph.vs[vid]["spelling_file"] = (
                        self._file_uri_to_relative(def_info["spelling_file"])
                    )
                if def_info["spelling_line"]:
                    self.code_graph.graph.vs[vid]["spelling_line"] = def_info[
                        "spelling_line"
                    ]

    def _add_contain_edges(self):
        """Add containment edges from refs container field."""
        contained_syms = set()

        for sym_id, ref_list in self._refs.items():
            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name:
                continue
            if sym_name not in self.code_graph.name_to_vertex:
                continue

            for ref in ref_list:
                kind = ref["kind"]
                is_def = kind & REF_KIND_DEFINITION
                container_id = ref.get("container", "")

                if is_def and container_id and container_id != ZERO_SYMBOL_ID:
                    container_name = self._resolve_sym_id(container_id)
                    if (
                        container_name
                        and container_name in self.code_graph.name_to_vertex
                    ):
                        self._queue_edge(container_name, sym_name, EDGE_TYPE_CONTAIN)
                        contained_syms.add(sym_id)
                        break

        # Top-level symbols: file --contain--> symbol
        for sym_id, sym in self._symbols.items():
            if sym_id in contained_syms:
                continue
            if sym.get("kind", 0) == KIND_MACRO:
                continue

            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name or sym_name not in self.code_graph.name_to_vertex:
                continue

            vid = self.code_graph.name_to_vertex[sym_name]
            attrs = self.code_graph.graph.vs[vid].attributes()
            file_path = attrs.get("file")
            if file_path and file_path in self.code_graph.name_to_vertex:
                self._queue_edge(file_path, sym_name, EDGE_TYPE_CONTAIN)

    def _add_reference_edges(self):
        """Add reference edges from refs with Reference kind."""
        for sym_id, ref_list in self._refs.items():
            sym_name = self._resolve_sym_id(sym_id)
            if not sym_name:
                continue
            if sym_name not in self.code_graph.name_to_vertex:
                continue

            for ref in ref_list:
                kind = ref["kind"]
                is_reference = kind & REF_KIND_REFERENCE
                container_id = ref.get("container", "")

                if is_reference and container_id and container_id != ZERO_SYMBOL_ID:
                    container_name = self._resolve_sym_id(container_id)
                    if (
                        container_name
                        and container_name in self.code_graph.name_to_vertex
                    ):
                        self._queue_edge(container_name, sym_name, EDGE_TYPE_REFERENCE)

    def _add_relation_edges(self):
        """Add edges from rela chunk (inheritance, override)."""
        for rel in self._relations:
            subject_id = rel["subject"]
            object_id = rel["object"]
            predicate = rel["predicate"]

            subject_name = self._resolve_sym_id(subject_id)
            object_name = self._resolve_sym_id(object_id)

            if not subject_name or not object_name:
                continue
            if subject_name not in self.code_graph.name_to_vertex:
                continue
            if object_name not in self.code_graph.name_to_vertex:
                continue

            if predicate == RELATION_BASE_OF:
                # "subject is base of object" → derived(object) references base(subject)
                self._queue_edge(object_name, subject_name, EDGE_TYPE_REFERENCE)
            elif predicate == RELATION_OVERRIDDEN_BY:
                self._queue_edge(object_name, subject_name, EDGE_TYPE_REFERENCE)
