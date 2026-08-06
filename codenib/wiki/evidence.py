# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Grounding primitives for generated repository Wiki pages."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, List, Sequence

FACT_CLAIM_ROLES = frozenset(
    {
        "purpose",
        "entry",
        "flow",
        "responsibility",
        "contract",
        "component",
    }
)
_CITATION_RE = re.compile(r"\[((?:E|R)\d+)\]")
_CITATION_TAIL_RE = re.compile(
    r"(?:\s*\[(?:E|R)\d+\])+\s*[.!?]?\s*$",
)
_CODE_RE = re.compile(r"`([^`\n]+)`")
# Publication floor: at least half a page's blocks must carry their source.
_MIN_CITATION_COVERAGE = 0.5
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_INTERNAL_WIKI_NAV_ITEM_RE = re.compile(
    r"^\s*[-*]\s+\[[^\]\n]+\]\(\?p=[^)\s]+\)\s*$",
)


def is_internal_wiki_navigation(block: str) -> bool:
    """Whether a block is only a list of internal Wiki page links."""

    lines = [line for line in block.splitlines() if line.strip()]
    return bool(lines) and all(
        _INTERNAL_WIKI_NAV_ITEM_RE.fullmatch(line) for line in lines
    )


def _corpus_contains_exact(corpus: str, value: str) -> bool:
    """Match one complete code-like value instead of an arbitrary substring."""

    value = (value or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            rf"(?<![\w.]){re.escape(value)}(?![\w.])",
            corpus,
            flags=re.IGNORECASE,
        )
    )


def _corpus_contains_leaf(corpus: str, value: str) -> bool:
    """Match a complete leaf name, including after attribute punctuation."""

    value = (value or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            rf"(?<!\w){re.escape(value)}(?!\w)",
            corpus,
            flags=re.IGNORECASE,
        )
    )


def _owner_and_leaf(value: str) -> tuple[str, str]:
    """Split the last attribute separator from a qualified identifier."""

    separators = list(re.finditer(r"\.|::", value or ""))
    if not separators:
        return "", (value or "").strip()
    separator = separators[-1]
    return value[: separator.start()].strip(), value[separator.end() :].strip()


def _qualified_identifier_supported(
    value: str,
    *,
    evidence_corpora: Sequence[str],
    relation_endpoints: Sequence[str],
) -> bool:
    """Require a qualified name's owner and leaf to share one source.

    Evidence often stores ``Owner.member`` as separate symbol and source-code
    fields, so an item may support the two halves without containing the joined
    display name.  Do not, however, combine an owner from one item with a leaf
    from another.  Relations are already canonical symbol identities and only
    support the qualified name when one complete endpoint names it.
    """

    owner, leaf = _owner_and_leaf(value)
    if not owner or not leaf:
        return False
    if any(_corpus_contains_exact(endpoint, value) for endpoint in relation_endpoints):
        return True
    return any(
        _corpus_contains_exact(corpus, value)
        or (
            _corpus_contains_exact(corpus, owner)
            and _corpus_contains_leaf(corpus, leaf)
        )
        for corpus in evidence_corpora
    )


def _block_is_cited(block: str) -> bool:
    """Whether a rendered block carries its own source attribution.

    Prose puts its citations in a trailing run. A table cites per row instead,
    and its last cell closes with a pipe, so the trailing-run test never matches
    -- which counted a fully attributed table as an uncited block. Require every
    data row to cite, which is stricter than the prose rule, not looser.
    """

    if _CITATION_TAIL_RE.search(block):
        return True
    rows = [line for line in block.splitlines() if line.strip().startswith("|")]
    if len(rows) >= 3 and _TABLE_DIVIDER_RE.match(rows[1]):
        data_rows = rows[2:]
        return bool(data_rows) and all(_CITATION_RE.search(row) for row in data_rows)
    return False


_PATH_RE = re.compile(
    r"(?<![\w/])([\w./-]+\.(?:py|pyi|go|rs|ts|tsx|js|jsx|c|h|cc|cpp|hpp|"
    r"java|rb|php|cs|kt|kts|swift|scala))(?![\w/])",
    re.IGNORECASE,
)
_COMMON_CODE_TERMS = frozenset(
    {
        "api",
        "async",
        "await",
        "bool",
        "class",
        "dict",
        "false",
        "float",
        "function",
        "int",
        "list",
        "main",
        "method",
        "none",
        "null",
        "object",
        "self",
        "str",
        "string",
        "true",
        "void",
    }
)
_PROMOTIONAL_RE = re.compile(
    r"\b(accurate(?:ly)?|adapt(?:s|ing)?|adaptable|advanced|aids?|better|"
    r"allows(?: for| the system| developers| users)|"
    r"allowing (?:for|developers|users)|allow(?:s|ed|ing)?|"
    r"comprehensive|crucial|dynamic(?:ally)?|"
    r"cater(?:s|ing)?|easy access|easy to use|easier|effectively|"
    r"efficient|efficiently|fast|"
    r"enabl(?:e|es|ing)(?: developers| users)?|"
    r"enhanc(?:e|es|ing)(?: productivity)?|"
    r"ensur(?:e|es|ing) (?:that )?(?:all relevant|everything|resources?)|"
    r"ensur(?:e|es|ing)|"
    r"essential|flexible|"
    r"facilitat(?:e|es|ing)|for clarity|gain insights?|"
    r"help(?:s|ed|ing)? (?:developers|users)|improv(?:e|es|ing)|"
    r"intuitive|invaluable|"
    r"important (?:for|to)|key functionalit(?:y|ies)|making it|"
    r"powerful|provid(?:e|es|ing) (?:easy|quick)|quickly|responsive|significantly|"
    r"sensible|supports? (?:interactions?|management)|vital|"
    r"optimiz(?:e|es|ing)|sophisticated|user-friendly|versatile)\b",
    re.IGNORECASE,
)
_FLOW_RE = re.compile(
    r"\b(call(?:s|ed|ing)?|delegat(?:e|es|ed|ing)|dispatch(?:es|ed|ing)?|"
    r"feed(?:s|ing)?|hand(?:s|ed)?\s+(?:off|to)|"
    r"interact(?:s|ed|ing)?\s+with|invok(?:e|es|ed|ing)|"
    r"load(?:s|ed|ing)?\s+.+\s+from|pass(?:es|ed|ing)?\s+.+\s+to|"
    r"publish(?:es|ed)?|read(?:s)?\s+.+\s+from|"
    r"quer(?:y|ies|ied|ying)\s+.+\s+(?:through|via)|"
    r"receiv(?:e|es|ed|ing)\s+.+\s+from|"
    r"retriev(?:e|es|ed|ing)\s+.+\s+from|rout(?:e|es|ed|ing)|"
    r"send(?:s|ing)?\s+.+\s+to|us(?:e|es|ed|ing)|"
    r"utiliz(?:e|es|ed|ing)|"
    r"writ(?:e|es|ten|ing)\s+.+\s+to)\b",
    re.IGNORECASE,
)
_RETURN_FLOW_RE = re.compile(
    r"\breturn(?:s|ed|ing)?\s+.+?\s+to\s+`[^`\n]+`",
    re.IGNORECASE,
)
_STRONG_HANDOFF_RE = re.compile(
    r"\b(?:"
    r"dispatch(?:es|ed|ing)?\s+.+\s+to|hand(?:s|ed)?\s+(?:off|to)|"
    r"interact(?:s|ed|ing)?\s+with|"
    r"load(?:s|ed|ing)?\s+.+\s+from|pass(?:es|ed|ing)?\s+.+\s+to|"
    r"provid(?:e|es|ed|ing)\s+.+\s+to|"
    r"quer(?:y|ies|ied|ying)\s+.+\s+(?:through|via)|"
    r"read(?:s)?\s+.+\s+from|receiv(?:e|es|ed|ing)\s+.+\s+from|"
    r"retriev(?:e|es|ed|ing)\s+.+\s+from|"
    r"rout(?:e|es|ed|ing)\s+.+\s+to|send(?:s|ing)?\s+.+\s+to|"
    r"us(?:e|es|ed|ing)\s+.+\s+from|"
    r"utiliz(?:e|es|ed|ing)\s+.+\s+from|"
    r"writ(?:e|es|ten|ing)\s+.+\s+to"
    r")\b",
    re.IGNORECASE,
)
_COMPONENT_TERM_RE = re.compile(
    r"\b(?:(?:[A-Za-z][\w.-]*\s+)?"
    r"(?:client|compiler|indexer|process|runtime|server|service|subsystem)|"
    r"(?<!local\s)wiki)\b",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(
    r"\b(command|entry\s*point|endpoint|public|request|users?)\b",
    re.IGNORECASE,
)
_EXPLICIT_ENTRY_RE = re.compile(
    r"\b(?:"
    r"entry\s*point|public\s+(?:api|callable|command|endpoint|function|method)|"
    r"users?\s+(?:call|execute|invoke|run)"
    r")\b",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER_RE = re.compile(
    r"`(?:[^`\n]*[:.])?_[A-Za-z]\w*(?:\([^`\n]*\))?`",
)
_RESPONSIBILITY_RE = re.compile(
    r"\b(build(?:s)?|compile(?:s)?|coordinate(?:s)?|execut(?:e|es)|"
    r"handle(?:s)?|manage(?:s)?|organize(?:s)?|own(?:s)?|persist(?:s)?|"
    r"process(?:es)?|"
    r"quer(?:y|ies)|register(?:s)?|responsib(?:le|ility)|serve(?:s)?|"
    r"rout(?:e|es|ing)|store(?:s)?|validat(?:e|es))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One source-backed symbol supplied to page planning and writing."""

    id: str
    file: str
    start_line: int | None
    end_line: int | None
    symbol: str
    kind: str
    content: str
    routes: tuple[str, ...] = ()

    def prompt_block(self) -> str:
        line = ""
        if self.start_line is not None:
            line = f":{self.start_line}"
            if self.end_line is not None and self.end_line != self.start_line:
                line += f"-{self.end_line}"
        routes = ", ".join(self.routes) if self.routes else "repository index"
        return (
            f"### [{self.id}] `{self.symbol or self.file}`\n"
            f"Source: `{self.file}{line}` | kind: {self.kind or 'code'} | "
            f"routes: {routes}\n"
            f"```\n{self.content}\n```"
        )


@dataclass(frozen=True, slots=True)
class RelationItem:
    """One static reference relation among page evidence symbols."""

    id: str
    source: str
    target: str
    anchors: tuple[str, ...] = ()

    def prompt_line(self) -> str:
        anchors = f" at {', '.join(self.anchors)}" if self.anchors else ""
        return f"- [{self.id}] `{self.source}` references `{self.target}`{anchors}"


def _endpoint_symbol(endpoint: str) -> str:
    symbol = (endpoint or "").rsplit(":", 1)[-1].strip()
    return re.sub(r"\([^)]*\)$", "", symbol).strip().lower()


def _claim_identifiers(statement: str) -> List[str]:
    return [
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in _CODE_RE.findall(statement or "")
    ]


def _identifier_names_endpoint(identifier: str, endpoint: str) -> bool:
    symbol = _endpoint_symbol(endpoint)
    return bool(symbol and (identifier == symbol or identifier.endswith(":" + symbol)))


def relation_endpoints_named(statement: str, relation: RelationItem) -> bool:
    """Whether a claim names both endpoints, independent of direction."""

    identifiers = _claim_identifiers(statement)
    return any(
        _identifier_names_endpoint(identifier, relation.source)
        for identifier in identifiers
    ) and any(
        _identifier_names_endpoint(identifier, relation.target)
        for identifier in identifiers
    )


def relation_matches_claim(statement: str, relation: RelationItem) -> bool:
    """Whether a direct flow claim states the cited source-to-target relation."""

    identifiers = _claim_identifiers(statement)
    source_positions = [
        index
        for index, identifier in enumerate(identifiers)
        if _identifier_names_endpoint(identifier, relation.source)
    ]
    target_positions = [
        index
        for index, identifier in enumerate(identifiers)
        if _identifier_names_endpoint(identifier, relation.target)
    ]
    return any(
        source_index < target_index
        for source_index in source_positions
        for target_index in target_positions
    )


def evidence_matches_claim(statement: str, evidence: EvidenceItem) -> bool:
    """Whether a cited callable body directly invokes the named target.

    Merely mentioning both names is not enough: a file that independently
    defines ``alpha`` and ``beta`` does not prove an ``alpha -> beta`` edge.
    Source-only flow support is therefore limited to a function/method whose
    identity matches the claim's source and whose body contains a call to the
    target. Other interactions require a static ``RelationItem``.
    """

    identifiers = [
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in _CODE_RE.findall(statement or "")
    ]
    if len(identifiers) < 2:
        return False
    evidence_symbol = _endpoint_symbol(evidence.symbol)
    evidence_leaf = re.split(r"::|\.", evidence_symbol)[-1]

    def names_evidence_symbol(identifier: str) -> bool:
        normalized = _endpoint_symbol(identifier)
        leaf = re.split(r"::|\.", normalized)[-1]
        return normalized == evidence_symbol or (bool(leaf) and leaf == evidence_leaf)

    def source_body(identifier: str) -> str:
        if names_evidence_symbol(identifier):
            return evidence.content or ""
        source = _endpoint_symbol(identifier)
        leaf = re.split(r"::|\.", source)[-1]
        definitions = (
            re.compile(
                rf"^(?P<indent>\s*)(?:async\s+)?def\s+{re.escape(leaf)}\s*\(",
                re.IGNORECASE,
            ),
            re.compile(
                rf"^(?P<indent>\s*)(?:(?:export|public|private|protected|static)"
                rf"\s+)*(?:async\s+)?function\s+{re.escape(leaf)}\s*\(",
                re.IGNORECASE,
            ),
        )
        lines = (evidence.content or "").splitlines()
        for index, line in enumerate(lines):
            match = next(
                (
                    pattern.search(line)
                    for pattern in definitions
                    if pattern.search(line)
                ),
                None,
            )
            if match is None:
                continue
            indent = len(match.group("indent").expandtabs(4))
            body = [line]
            for later in lines[index + 1 :]:
                stripped = later.strip()
                later_indent = len(later) - len(later.lstrip())
                if (
                    stripped
                    and later_indent <= indent
                    and re.match(
                        r"(?:async\s+)?def\s+|class\s+|"
                        r"(?:(?:export|public|private|protected|static)\s+)*"
                        r"(?:async\s+)?function\s+",
                        stripped,
                        re.IGNORECASE,
                    )
                ):
                    break
                body.append(later)
            return "\n".join(body)
        return ""

    def body_calls(identifier: str, content: str) -> bool:
        target = _endpoint_symbol(identifier)
        leaf = re.split(r"::|\.", target)[-1]
        if len(leaf) < 2:
            return False
        call = re.compile(rf"(?<!\w){re.escape(leaf)}\s*\(", re.IGNORECASE)
        declarations = (
            re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(leaf)}\s*\("),
            re.compile(
                rf"^\s*(?:(?:export|public|private|protected|static)\s+)*"
                rf"(?:async\s+)?function\s+{re.escape(leaf)}\s*\("
            ),
        )
        return any(
            call.search(line)
            and not any(pattern.search(line) for pattern in declarations)
            for line in content.splitlines()
        )

    return any(
        bool(body := source_body(source)) and body_calls(target, body)
        for source, target in zip(identifiers, identifiers[1:], strict=False)
    )


def is_interaction_claim(statement: str) -> bool:
    """Whether a claim explicitly describes a component-to-component handoff."""

    identifiers = re.findall(r"`([^`\n]+)`", statement or "")
    return bool(
        len(set(identifiers)) >= 2
        and (
            _FLOW_RE.search(statement or "") or _RETURN_FLOW_RE.search(statement or "")
        )
    )


def _describes_handoff(statement: str) -> bool:
    """Whether prose asserts a handoff that must name verifiable endpoints."""

    text = statement or ""
    if is_interaction_claim(text):
        return True
    if not _STRONG_HANDOFF_RE.search(text):
        return False
    identifiers = {
        re.sub(r"\([^)]*\)$", "", item.strip()).lower()
        for item in _CODE_RE.findall(text)
    }
    prose = _CODE_RE.sub(" ", text)
    components = {
        re.sub(r"\s+", " ", match.group(0)).strip().lower()
        for match in _COMPONENT_TERM_RE.finditer(prose)
    }
    return len(identifiers) + len(components) >= 2


def infer_claim_role(statement: str) -> str:
    """Infer a conservative role for plans from older prompts."""

    text = statement or ""
    if _describes_handoff(text):
        return "flow"
    if _ENTRY_RE.search(text):
        return "entry"
    if _RESPONSIBILITY_RE.search(text):
        return "responsibility"
    return "component"


def describes_private_entry(statement: str, *, role: str = "") -> bool:
    """Whether prose presents a private identifier as a public/user entry."""

    text = statement or ""
    return bool(
        _PRIVATE_IDENTIFIER_RE.search(text)
        and (role == "entry" or _EXPLICIT_ENTRY_RE.search(text))
    )


def promotional_phrases(text: str) -> List[str]:
    """Return deterministic marketing or unsupported benefit language."""

    return sorted(
        {match.group(0).lower() for match in _PROMOTIONAL_RE.finditer(text or "")}
    )


def _supported_evidence_ids(
    raw: Any,
    allowed: set[str],
    *,
    label: str,
    errors: List[str],
) -> list[str]:
    requested = [str(item) for item in raw or []]
    unknown = [item for item in requested if item not in allowed]
    if unknown:
        errors.append(f"{label} references unknown evidence: {', '.join(unknown)}")
    return [item for item in requested if item in allowed]


def candidate_key(node: Any, get: Callable[[Any, str, Any], Any]) -> tuple:
    """Stable source identity for a retrieval candidate."""

    return (
        get(node, "file", "") or "",
        get(node, "start_line", None),
        get(node, "end_line", None),
        get(node, "node_name", "") or get(node, "name", "") or "",
    )


def reciprocal_rank_fuse(
    routes: Sequence[tuple[str, Sequence[Any]]],
    *,
    key: Callable[[Any], tuple],
    rank_constant: int = 60,
) -> List[tuple[Any, tuple[str, ...], float]]:
    """Fuse independently ranked routes while preserving provenance."""

    by_key: dict[tuple, dict[str, Any]] = {}
    for route, nodes in routes:
        for rank, node in enumerate(nodes, start=1):
            item = by_key.setdefault(
                key(node),
                {"node": node, "routes": [], "score": 0.0, "first": rank},
            )
            if route not in item["routes"]:
                item["routes"].append(route)
                item["score"] += 1.0 / (rank_constant + rank)
            item["first"] = min(item["first"], rank)
    ordered = sorted(
        by_key.values(),
        key=lambda item: (-item["score"], item["first"]),
    )
    return [
        (item["node"], tuple(item["routes"]), float(item["score"])) for item in ordered
    ]


def diversify_by_file(
    ranked: Sequence[tuple[Any, tuple[str, ...], float]],
    *,
    file_of: Callable[[Any], str],
    limit: int,
    max_per_file: int = 2,
) -> List[tuple[Any, tuple[str, ...], float]]:
    """Bound one file's ability to crowd every other subsystem out."""

    selected: List[tuple[Any, tuple[str, ...], float]] = []
    deferred: List[tuple[Any, tuple[str, ...], float]] = []
    counts: dict[str, int] = {}
    for item in ranked:
        file = file_of(item[0])
        if counts.get(file, 0) >= max_per_file:
            deferred.append(item)
            continue
        selected.append(item)
        counts[file] = counts.get(file, 0) + 1
        if len(selected) >= limit:
            return selected
    selected.extend(deferred[: max(0, limit - len(selected))])
    return selected


def parse_fact_plan(
    text: str,
    allowed_ids: Iterable[str],
) -> tuple[dict[str, Any], List[str]]:
    """Parse and structurally validate a model-authored page plan."""

    allowed = set(allowed_ids)
    errors: List[str] = []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {
            "thesis": {"statement": "", "evidence": []},
            "sections": [],
        }, ["plan is not a JSON object"]
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {
            "thesis": {"statement": "", "evidence": []},
            "sections": [],
        }, [f"invalid plan JSON: {exc}"]

    raw_thesis = raw.get("thesis")
    if isinstance(raw_thesis, dict):
        thesis_statement = str(raw_thesis.get("statement") or "").strip()
        thesis_evidence = _supported_evidence_ids(
            raw_thesis.get("evidence"),
            allowed,
            label="thesis",
            errors=errors,
        )
    else:
        thesis_statement = str(raw_thesis or "").strip()
        thesis_evidence = []

    sections = []
    for section in raw.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = str(section.get("title") or "").strip()
        claims = []
        for claim in section.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            statement = str(claim.get("statement") or "").strip()
            evidence = _supported_evidence_ids(
                claim.get("evidence"),
                allowed,
                label="claim",
                errors=errors,
            )
            if statement and evidence:
                role = str(claim.get("role") or "").strip().lower()
                inferred_role = infer_claim_role(statement)
                if role not in FACT_CLAIM_ROLES:
                    role = inferred_role
                elif inferred_role == "flow":
                    role = "flow"
                elif role == "flow":
                    role = inferred_role
                elif (
                    role in {"entry", "component"} and inferred_role == "responsibility"
                ):
                    role = inferred_role
                claims.append(
                    {
                        "role": role,
                        "statement": statement,
                        "evidence": evidence,
                    }
                )
        # Mechanism-level framing for the section. It may reason across two
        # sentences and need not name two endpoints, but it still has to cite.
        lead: dict[str, Any] = {}
        raw_lead = section.get("lead")
        if isinstance(raw_lead, dict):
            statements = [
                str(item).strip()
                for item in raw_lead.get("statements") or []
                if str(item or "").strip()
            ]
            lead_evidence = _supported_evidence_ids(
                raw_lead.get("evidence"),
                allowed,
                label="section lead",
                errors=errors,
            )
            if statements and lead_evidence:
                lead = {"statements": statements[:2], "evidence": lead_evidence}

        # The excerpt names which evidence to show; the source is rendered from
        # that item rather than reproduced by the model, so it stays verbatim.
        excerpt: dict[str, Any] = {}
        raw_excerpt = section.get("excerpt")
        if isinstance(raw_excerpt, dict):
            ref = str(raw_excerpt.get("evidence") or "").strip()
            why = str(raw_excerpt.get("why") or "").strip()
            if ref in allowed:
                excerpt = {"evidence": ref, "why": why}

        if title and claims:
            entry: dict[str, Any] = {"title": title, "claims": claims}
            if lead:
                entry["lead"] = lead
            if excerpt:
                entry["excerpt"] = excerpt
            sections.append(entry)
    if not sections:
        errors.append("plan has no supported sections")

    # Reader-facing framing and the scan table are grounded like any claim: each
    # needs admissible evidence ids or it is dropped rather than passed through.
    purpose: dict[str, Any] = {}
    raw_purpose = raw.get("purpose")
    if isinstance(raw_purpose, dict):
        statements = [
            str(item).strip()
            for item in raw_purpose.get("statements") or []
            if str(item or "").strip()
        ]
        purpose_evidence = _supported_evidence_ids(
            raw_purpose.get("evidence"),
            allowed,
            label="purpose",
            errors=errors,
        )
        if statements and purpose_evidence:
            purpose = {"statements": statements, "evidence": purpose_evidence}

    scan_map: List[dict[str, Any]] = []
    for row in raw.get("map") or []:
        if not isinstance(row, dict):
            continue
        concern = str(row.get("concern") or "").strip()
        entity = str(row.get("entity") or "").strip()
        row_evidence = _supported_evidence_ids(
            row.get("evidence"),
            allowed,
            label="map row",
            errors=errors,
        )
        if concern and entity and row_evidence:
            scan_map.append(
                {"concern": concern, "entity": entity, "evidence": row_evidence}
            )

    # A diagram is validated here, on structured data, rather than by reading the
    # rendered fence back out: a mermaid block is stripped before the prose
    # checks run, so a step naming a symbol nobody supplied would sail through.
    flow: dict[str, Any] = {}
    raw_flow = raw.get("flow")
    if isinstance(raw_flow, dict):
        steps = []
        for step in raw_flow.get("steps") or []:
            if not isinstance(step, dict):
                continue
            source = str(step.get("from") or "").strip()
            target = str(step.get("to") or "").strip()
            label = str(step.get("label") or "").strip()
            step_evidence = _supported_evidence_ids(
                step.get("evidence"),
                allowed,
                label="flow step",
                errors=errors,
            )
            if source and target and source != target and step_evidence:
                steps.append(
                    {
                        "from": source,
                        "to": target,
                        "label": label,
                        "evidence": step_evidence,
                    }
                )
        # One arrow is not a flow.
        if len(steps) >= 2:
            flow = {
                "title": str(raw_flow.get("title") or "").strip(),
                "steps": steps[:6],
            }

    see_also: List[dict[str, Any]] = []
    for ref in raw.get("see_also") or []:
        if not isinstance(ref, dict):
            continue
        page = str(ref.get("page") or "").strip()
        why = str(ref.get("why") or "").strip()
        if page:
            see_also.append({"page": page, "why": why})

    plan: dict[str, Any] = {
        "thesis": {
            "statement": thesis_statement,
            "evidence": thesis_evidence,
        },
        "sections": sections,
    }
    if purpose:
        plan["purpose"] = purpose
    if scan_map:
        plan["map"] = scan_map
    if flow:
        plan["flow"] = flow
    if see_also:
        plan["see_also"] = see_also[:3]
    return plan, errors


def _quotes_cited_evidence(block: str, evidence: Sequence["EvidenceItem"]) -> bool:
    """Whether a block reproduces text that is already in its cited source.

    A project's own one-line description is the natural opening for an overview,
    and it is reported with a citation rather than asserted. Judging it as the
    page's own marketing flagged pages for saying what their README says --
    "A fast and flexible static site generator" -- which the page did not claim,
    it quoted.
    """

    text = _CITATION_TAIL_RE.sub("", block).strip()
    text = re.sub(r"\[(?:E|R)\d+\]", "", text).strip()
    if len(text) < 24:
        return False
    needle = " ".join(text.split()).casefold()
    for item in evidence:
        haystack = " ".join((item.content or "").split()).casefold()
        if needle and needle in haystack:
            return True
    return False


def grounding_report(
    markdown: str,
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    """Check citation coverage and source-backed identifiers before publish."""

    allowed_ids = {item.id for item in evidence} | {item.id for item in relations}
    cited = set(_CITATION_RE.findall(markdown))
    unknown_citations = sorted(cited - allowed_ids)

    without_fences = re.sub(r"```[\s\S]*?```", "", markdown)
    prose_blocks = []
    blocks = []
    for raw in re.split(r"\n\s*\n", without_fences):
        block = raw.strip()
        if not block or block.startswith("#"):
            continue
        # Related-page links are navigation metadata, not factual prose. Keep
        # this narrow: any description beside a link, or any ordinary list,
        # remains part of the grounding denominator.
        if is_internal_wiki_navigation(block):
            continue
        prose_blocks.append(block)
        plain = re.sub(r"^[-*]\s+", "", block, flags=re.MULTILINE)
        if len(re.sub(r"[`*_[\]()#>-]", "", plain).strip()) >= 40:
            blocks.append(block)
    cited_blocks = sum(1 for block in blocks if _block_is_cited(block))
    coverage = cited_blocks / len(blocks) if blocks else 0.0

    evidence_corpora = [
        "\n".join((item.file, item.symbol, item.content)) for item in evidence
    ]
    relation_endpoints = [
        endpoint for item in relations for endpoint in (item.source, item.target)
    ]
    support_corpora = [
        *evidence_corpora,
        *relation_endpoints,
        *(anchor for item in relations for anchor in item.anchors),
    ]

    def any_exact(value: str) -> bool:
        return any(_corpus_contains_exact(corpus, value) for corpus in support_corpora)

    def any_leaf(value: str) -> bool:
        return any(_corpus_contains_leaf(corpus, value) for corpus in support_corpora)

    known_files = {item.file.lower().lstrip("./") for item in evidence}
    unsupported_identifiers = []
    for identifier in _CODE_RE.findall(without_fences):
        normalized = identifier.strip()
        source_name = re.sub(r":\d+(?:-\d+)?$", "", normalized)
        call_name_match = re.fullmatch(
            r"([A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*)\s*\([^`\n]*\)",
            source_name,
        )
        call_name = call_name_match.group(1) if call_name_match else ""
        call_owner, call_leaf = _owner_and_leaf(call_name)
        call_supported = bool(
            call_name
            and (
                (not call_owner and any_leaf(call_leaf))
                or _qualified_identifier_supported(
                    call_name,
                    evidence_corpora=evidence_corpora,
                    relation_endpoints=relation_endpoints,
                )
            )
        )
        # `path/to/file.rs:Symbol` is a display form; the corpus holds the path
        # and the symbol separately, never joined. Accept it only when both
        # halves are known.
        qualified = ""
        if ":" in source_name and not source_name.endswith(":"):
            head, _, tail = source_name.rpartition(":")
            tail = re.sub(r"\([^`\n]*\)$", "", tail).strip()
            matching_evidence = [
                corpus
                for item, corpus in zip(evidence, evidence_corpora, strict=True)
                if item.file.lower().lstrip("./") == head.lower().lstrip("./")
            ]
            if tail and any(
                _corpus_contains_exact(corpus, tail)
                or (
                    not _owner_and_leaf(tail)[0] and _corpus_contains_leaf(corpus, tail)
                )
                or _qualified_identifier_supported(
                    tail,
                    evidence_corpora=[corpus],
                    relation_endpoints=(),
                )
                for corpus in matching_evidence
            ):
                qualified = source_name
        # Attribute access, e.g. `PreparedRequest.url`. Calls already resolve by
        # their complete owner; do the same for attributes.
        attribute = ""
        if not call_name and re.search(r"\.|::", source_name):
            if _qualified_identifier_supported(
                source_name,
                evidence_corpora=evidence_corpora,
                relation_endpoints=relation_endpoints,
            ):
                attribute = source_name
        bare_member = bool(
            re.fullmatch(r"[A-Za-z_]\w*", source_name) and any_leaf(source_name)
        )
        if (
            not normalized
            or normalized.lower() in _COMMON_CODE_TERMS
            # A URI scheme (`mailto:`, `https:`) is not a code identifier.
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9+.\-]*:", normalized)
            or any_exact(normalized)
            or any_exact(source_name)
            or bare_member
            or qualified
            or attribute
            or call_supported
        ):
            continue
        unsupported_identifiers.append(normalized)

    unknown_files = sorted(
        {
            path
            for path in _PATH_RE.findall(without_fences)
            if path.lower().lstrip("./") not in known_files
            and not any_exact(path)
            and not any(file.endswith("/" + path.lower()) for file in known_files)
        }
    )
    unsupported_identifiers = sorted(set(unsupported_identifiers))
    # Marketing words the page wrote itself are a defect; the same words inside
    # a block it is quoting from cited source are not.
    authored = "\n\n".join(
        block for block in prose_blocks if not _quotes_cited_evidence(block, evidence)
    )
    promotional = promotional_phrases(authored)
    valid = (
        bool(blocks)
        and coverage == 1.0
        and not unknown_citations
        and not unknown_files
        and not unsupported_identifiers
    )
    # ``valid`` is the strict, descriptive reading: every substantial block is
    # cited and every source reference resolves. ``grounded`` keeps a looser
    # coverage floor for readable connective prose, but never admits an unknown
    # source path, invented identifier, or promotional claim.
    grounded = (
        bool(blocks)
        and coverage >= _MIN_CITATION_COVERAGE
        # Citing an id that was never supplied is an integrity failure, not a
        # shortfall: the page points at evidence that does not exist.
        and not unknown_citations
        and not unknown_files
        and not unsupported_identifiers
        and not promotional
    )
    return {
        "valid": valid,
        "grounded": grounded,
        "citation_coverage": round(coverage, 3),
        "cited_evidence": len(cited & allowed_ids),
        "evidence_count": len(evidence),
        "relation_count": len(relations),
        "unknown_citations": unknown_citations,
        "unknown_files": unknown_files,
        "unsupported_identifiers": unsupported_identifiers,
        "promotional_phrases": promotional,
    }


def remove_promotional_sentences(markdown: str) -> str:
    """Remove benefit claims while retaining citations for factual sentences."""

    cleaned_blocks = []
    for block in re.split(r"\n\s*\n", markdown.strip()):
        stripped = block.strip()
        if not stripped or stripped.startswith("#"):
            cleaned_blocks.append(stripped)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        kept = [
            sentence for sentence in sentences if not _PROMOTIONAL_RE.search(sentence)
        ]
        if not kept:
            continue
        rebuilt = " ".join(kept).strip()
        citations = list(dict.fromkeys(_CITATION_RE.findall(stripped)))
        present = set(_CITATION_RE.findall(rebuilt))
        missing = [citation for citation in citations if citation not in present]
        if missing:
            rebuilt += " " + "".join(f"[{citation}]" for citation in missing)
        cleaned_blocks.append(rebuilt)
    return "\n\n".join(block for block in cleaned_blocks if block)


def evidence_metadata(
    evidence: Sequence[EvidenceItem],
    relations: Sequence[RelationItem],
) -> dict[str, Any]:
    return {
        "items": [
            {key: value for key, value in asdict(item).items() if key != "content"}
            for item in evidence
        ],
        "relations": [asdict(item) for item in relations],
    }


__all__ = [
    "EvidenceItem",
    "RelationItem",
    "candidate_key",
    "describes_private_entry",
    "diversify_by_file",
    "evidence_metadata",
    "evidence_matches_claim",
    "grounding_report",
    "parse_fact_plan",
    "promotional_phrases",
    "relation_endpoints_named",
    "relation_matches_claim",
    "reciprocal_rank_fuse",
    "remove_promotional_sentences",
]
