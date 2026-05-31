# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Type parsing, runtime arg coercion, and skill-dataflow type checking.

Two complaints motivated this module:

1. **"input type ``any`` everywhere"** — the ``type_hint`` strings on
   ``SkillInputSpec`` were only ever used to emit a JSON-Schema for the model;
   nothing validated the arguments the model actually sent. :func:`coerce_args`
   coerces and checks the LLM's JSON arguments against the declared input types
   at call time (the runner calls it before dispatch).

2. **"output type ``any`` disobeys the compiler's type check / inference"** —
   ``SkillOutputSpec.type_hint`` was dead: ``tool_schema`` never reads it (the
   OpenAI tool schema has no return slot), so it was metadata for a compiler
   that never type-checked it. :func:`check_pipeline_coherent` consumes it,
   giving the swept-subset a real dataflow type check: every *consumer* skill
   (one that takes ``List[QueriedNode]`` candidates) must have a *producer* in
   the subset whose output type matches.

The type grammar is intentionally tiny — it only needs to cover what skills
declare: scalars (``str``/``int``/``float``/``bool``), ``Any``, nominal types
(``QueriedNode``), and one level of ``List[...]`` / ``Dict[...]`` nesting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
# Type grammar
# --------------------------------------------------------------------------

_SCALARS = {"str": str, "int": int, "float": float, "bool": bool}


@dataclass(frozen=True)
class TypeExpr:
    """A parsed ``type_hint`` string.

    ``kind`` is one of ``scalar`` / ``list`` / ``dict`` / ``nominal`` / ``any``.
    ``name`` holds the scalar/nominal name; ``inner`` holds the element type for
    ``list`` (and the value type for ``dict``).
    """

    kind: str
    name: str = ""
    inner: Optional["TypeExpr"] = None

    def __str__(self) -> str:
        if self.kind == "list":
            return f"List[{self.inner}]"
        if self.kind == "dict":
            return f"Dict[str, {self.inner}]"
        if self.kind == "any":
            return "Any"
        return self.name


_ANY = TypeExpr("any", "Any")


def parse_type_hint(hint: Optional[str]) -> TypeExpr:
    """Parse a ``type_hint`` string into a :class:`TypeExpr` (best-effort).

    Unknown / unparseable hints become ``Any`` so callers never crash on an
    odd annotation — they just lose the constraint for that field.
    """
    s = (hint or "").strip()
    if not s or s == "Any":
        return _ANY
    low = s.lower()
    if low.startswith("list[") and s.endswith("]"):
        return TypeExpr("list", "List", parse_type_hint(s[5:-1]))
    if low.startswith("dict[") and s.endswith("]"):
        # Dict[K, V] -> record the value type only (keys are always str-ish).
        body = s[5:-1]
        parts = _split_top(body)
        val = parts[-1] if parts else "Any"
        return TypeExpr("dict", "Dict", parse_type_hint(val))
    if s in _SCALARS:
        return TypeExpr("scalar", s)
    return TypeExpr("nominal", s)


def _split_top(body: str) -> List[str]:
    """Split a comma list at top-level bracket depth (for ``Dict[K, V]``)."""
    out, depth, cur = [], 0, ""
    for ch in body:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def types_compatible(produced: TypeExpr, consumed: TypeExpr) -> bool:
    """Is a value of type ``produced`` acceptable where ``consumed`` is wanted?

    ``Any`` matches anything (either direction). Lists match element-wise; a
    ``List[List[X]]`` consumer (``hybrid_search``) also accepts a ``List[X]``
    producer, since several producer outputs are bundled into the outer list.
    """
    if produced.kind == "any" or consumed.kind == "any":
        return True
    if consumed.kind == "list" and produced.kind == "list":
        if types_compatible(produced, consumed.inner or _ANY):
            return True  # producer list fits as one element of a list-of-lists
        return types_compatible(produced.inner or _ANY, consumed.inner or _ANY)
    if produced.kind != consumed.kind:
        return False
    if produced.kind in ("scalar", "nominal"):
        return produced.name == consumed.name
    if produced.kind in ("list", "dict"):
        return types_compatible(produced.inner or _ANY, consumed.inner or _ANY)
    return False


# --------------------------------------------------------------------------
# Runtime argument coercion (input type enforcement)
# --------------------------------------------------------------------------


def _coerce_scalar(value: Any, name: str) -> Any:
    """Coerce a JSON value to the Python scalar ``name`` (raises ValueError)."""
    target = _SCALARS[name]
    if isinstance(value, target) and not (target is int and isinstance(value, bool)):
        return value
    if target is bool:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no", ""):
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValueError(f"expected bool, got {type(value).__name__}")
    if target is int:
        if isinstance(value, bool):
            raise ValueError("expected int, got bool")
        # Truncate any float (matches the prior executor-side int(...) behaviour,
        # so a stray top_k=10.5 from the model degrades gracefully to 10 rather
        # than hard-failing the call).
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            return int(float(value.strip()))
        if isinstance(value, int):
            return value
        raise ValueError(f"expected int, got {type(value).__name__}")
    if target is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        raise ValueError(f"expected float, got {type(value).__name__}")
    # str
    if isinstance(value, str):
        return value
    raise ValueError(f"expected str, got {type(value).__name__}")


def _coerce(value: Any, t: TypeExpr, field: str) -> Any:
    """Coerce ``value`` to type ``t``; raise ``ValueError`` with a clear msg."""
    if value is None or t.kind in ("any", "nominal"):
        # Nominal types (e.g. QueriedNode candidate lists threaded from another
        # tool's result) arrive as already-structured JSON; don't second-guess.
        return value
    if t.kind == "scalar":
        try:
            return _coerce_scalar(value, t.name)
        except ValueError as exc:
            raise ValueError(f"{field!r}: {exc}") from None
    if t.kind == "list":
        if not isinstance(value, list):
            raise ValueError(f"{field!r}: expected a list, got {type(value).__name__}")
        return [_coerce(v, t.inner or _ANY, f"{field}[]") for v in value]
    if t.kind == "dict":
        if not isinstance(value, dict):
            raise ValueError(
                f"{field!r}: expected an object, got {type(value).__name__}"
            )
        return value
    return value


def coerce_args(
    inputs: Iterable[Any], args: Dict[str, Any]
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Validate + coerce LLM ``args`` against declared input specs.

    ``inputs`` is any iterable of specs exposing ``name`` / ``type_hint`` /
    ``required`` / ``default`` (``SkillInputSpec`` or ``ToolInputSpec``).

    Returns ``(coerced_args, error)``. ``error`` is ``None`` on success, else a
    human-readable message (a missing required parameter, or a value that
    cannot be coerced to its declared type). Arguments not named in ``inputs``
    are passed through untouched (executors accept ``**kwargs``).
    """
    specs = list(inputs)
    by_name = {s.name: s for s in specs}
    out = dict(args)

    # Coerce every declared, present argument to its type.
    for name, spec in by_name.items():
        if name in out:
            try:
                out[name] = _coerce(out[name], parse_type_hint(spec.type_hint), name)
            except ValueError as exc:
                return out, str(exc)

    # A required parameter must be supplied by the model — and "supplied" means
    # a non-null value: an absent key OR an explicit JSON null both count as
    # missing (``required`` means must-be-given, regardless of any default).
    for spec in specs:
        if spec.required and out.get(spec.name) is None:
            return out, f"missing required parameter {spec.name!r}"

    return out, None


# --------------------------------------------------------------------------
# Skill-dataflow type checking (output type inference)
# --------------------------------------------------------------------------


def skill_output_type(meta: Any) -> TypeExpr:
    """The declared output type of a skill (``Any`` for tools / unset)."""
    outputs = getattr(meta, "outputs", None)
    if outputs is None:
        # Tools declare a flat ``output_type_hint``.
        return parse_type_hint(getattr(meta, "output_type_hint", "Any"))
    return parse_type_hint(getattr(outputs, "type_hint", "Any"))


def skill_input_types(meta: Any) -> Dict[str, TypeExpr]:
    """Map of input name -> parsed type for a skill/tool."""
    return {i.name: parse_type_hint(i.type_hint) for i in getattr(meta, "inputs", [])}


# Input names that carry "candidate node lists threaded from an upstream
# retriever" — these are the dataflow edges worth type-checking across a subset.
_CANDIDATE_INPUTS = ("candidates", "nodes")


def check_pipeline_coherent(skill_ids: Iterable[str], registry: Any) -> List[str]:
    """Dataflow type check over a swept subset (consumes ``output_type_hint``).

    For every skill in ``skill_ids`` that *consumes* upstream node lists (a
    rerank/aggregate/transform skill with a ``candidates``/``nodes`` input),
    verify at least one *other* skill in the subset *produces* a compatible
    output type. Returns a list of human-readable warnings (empty == coherent).

    This is the type *inference* the compiler should do on a skill set: it makes
    the otherwise-dead ``outputs.type_hint`` load-bearing, and catches a subset
    like ``[llm_rerank]`` (a reranker with nothing to rerank) at config time
    instead of at a confusing runtime tool error.
    """
    metas = {sid: registry.get(sid) for sid in skill_ids}
    present = {sid: m for sid, m in metas.items() if m is not None}
    warnings: List[str] = []

    for sid, meta in present.items():
        in_types = skill_input_types(meta)
        for cand_name in _CANDIDATE_INPUTS:
            if cand_name not in in_types:
                continue
            want = in_types[cand_name]
            producers = [
                other
                for other, om in present.items()
                if other != sid and types_compatible(skill_output_type(om), want)
            ]
            if not producers:
                warnings.append(
                    f"skill {sid!r} consumes {cand_name}: {want} but no other "
                    f"skill in the subset produces a compatible output — the "
                    f"LLM has nothing to feed it"
                )
    return warnings


__all__ = [
    "TypeExpr",
    "parse_type_hint",
    "types_compatible",
    "coerce_args",
    "skill_output_type",
    "skill_input_types",
    "check_pipeline_coherent",
]
