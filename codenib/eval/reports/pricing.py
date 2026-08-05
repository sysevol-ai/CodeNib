# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Immutable pricing snapshots for reproducible offline cost projection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

PRICING_SNAPSHOT_SCHEMA = "codenib.pricing-snapshot.v1"
DEFAULT_PRICING_SNAPSHOT = "anthropic-direct-2026-08-04.json"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATE_KEYS = (
    "uncached_input",
    "output",
    "cache_read_input",
    "cache_write_input",
)
_TOKEN_SEMANTICS = {"prompt_excludes_cache", "prompt_includes_cache"}


class PricingSnapshotError(ValueError):
    """Raised when a pricing snapshot is malformed or ambiguous."""


@dataclass(frozen=True)
class ModelPrice:
    """One provider-channel-specific token price contract."""

    model_id: str
    aliases: tuple[str, ...]
    channel: str
    token_semantics: str
    cache_write_ttl: str
    rates_per_million: Mapping[str, float]
    source_url: str


@dataclass(frozen=True)
class PricingSnapshot:
    """Validated immutable catalog plus its exact content hash."""

    snapshot_id: str
    sha256: str
    currency: str
    effective_date: str
    retrieved_date: str
    models: tuple[ModelPrice, ...]
    source_file: Optional[str] = None

    def resolve_model(self, model: str) -> Optional[ModelPrice]:
        """Resolve an exact id or alias; snapshots reject ambiguous aliases."""

        selected = str(model or "").strip()
        for entry in self.models:
            if selected == entry.model_id or selected in entry.aliases:
                return entry
        return None

    def public_identity(self) -> dict[str, Any]:
        """Return provenance fields suitable for a machine-readable report."""

        return {
            "schema": PRICING_SNAPSHOT_SCHEMA,
            "snapshot_id": self.snapshot_id,
            "sha256": self.sha256,
            "currency": self.currency,
            "effective_date": self.effective_date,
            "retrieved_date": self.retrieved_date,
            "source_file": self.source_file,
        }


def bundled_pricing_snapshot_path() -> Path:
    """Return the bundled direct-provider example catalog."""

    return Path(__file__).resolve().parents[1] / "pricing" / DEFAULT_PRICING_SNAPSHOT


def load_pricing_snapshot(path: Path) -> PricingSnapshot:
    """Load and validate one exact JSON pricing snapshot."""

    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_bytes()
        value = json.loads(raw)
    except OSError as exc:
        raise PricingSnapshotError(
            f"cannot read pricing snapshot {source}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PricingSnapshotError(f"invalid pricing JSON {source}: {exc}") from exc
    return parse_pricing_snapshot(
        value,
        sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        source_path=str(source),
    )


def parse_pricing_snapshot(
    value: Mapping[str, Any],
    *,
    sha256: Optional[str] = None,
    source_path: Optional[str] = None,
) -> PricingSnapshot:
    """Validate a decoded snapshot and build its alias index."""

    if not isinstance(value, Mapping):
        raise PricingSnapshotError("pricing snapshot must be a JSON object")
    if value.get("schema") != PRICING_SNAPSHOT_SCHEMA:
        raise PricingSnapshotError(
            f"pricing snapshot schema must be {PRICING_SNAPSHOT_SCHEMA!r}"
        )

    snapshot_id = _required_string(value, "snapshot_id")
    currency = _required_string(value, "currency")
    if currency != "USD":
        raise PricingSnapshotError("only USD pricing snapshots are currently supported")
    effective_date = _date(value, "effective_date")
    retrieved_date = _date(value, "retrieved_date")
    raw_models = value.get("models")
    if not isinstance(raw_models, Sequence) or isinstance(raw_models, (str, bytes)):
        raise PricingSnapshotError("pricing snapshot models must be an array")
    if not raw_models:
        raise PricingSnapshotError("pricing snapshot must contain at least one model")

    models = []
    names: dict[str, str] = {}
    for index, raw_model in enumerate(raw_models):
        if not isinstance(raw_model, Mapping):
            raise PricingSnapshotError(f"models[{index}] must be an object")
        model_id = _required_string(raw_model, "model_id", prefix=f"models[{index}].")
        aliases_value = raw_model.get("aliases") or []
        if not isinstance(aliases_value, Sequence) or isinstance(
            aliases_value, (str, bytes)
        ):
            raise PricingSnapshotError(f"models[{index}].aliases must be an array")
        aliases = tuple(
            _nonempty_string(alias, f"models[{index}].aliases")
            for alias in aliases_value
        )
        channel = _required_string(raw_model, "channel", prefix=f"models[{index}].")
        token_semantics = _required_string(
            raw_model, "token_semantics", prefix=f"models[{index}]."
        )
        if token_semantics not in _TOKEN_SEMANTICS:
            raise PricingSnapshotError(
                f"models[{index}].token_semantics must be one of "
                f"{sorted(_TOKEN_SEMANTICS)}"
            )
        cache_write_ttl = _required_string(
            raw_model, "cache_write_ttl", prefix=f"models[{index}]."
        )
        rates = _rates(raw_model.get("rates_per_million"), index=index)
        source_url = _required_string(
            raw_model, "source_url", prefix=f"models[{index}]."
        )
        if not source_url.startswith("https://"):
            raise PricingSnapshotError(f"models[{index}].source_url must use https")

        for name in (model_id, *aliases):
            previous = names.get(name)
            if previous is not None:
                raise PricingSnapshotError(
                    f"pricing alias {name!r} is ambiguous between "
                    f"{previous!r} and {model_id!r}"
                )
            names[name] = model_id
        models.append(
            ModelPrice(
                model_id=model_id,
                aliases=aliases,
                channel=channel,
                token_semantics=token_semantics,
                cache_write_ttl=cache_write_ttl,
                rates_per_million=rates,
                source_url=source_url,
            )
        )

    if sha256 is None:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        sha256 = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha256):
        raise PricingSnapshotError("pricing snapshot hash must be sha256:<64 hex>")

    return PricingSnapshot(
        snapshot_id=snapshot_id,
        sha256=sha256,
        currency=currency,
        effective_date=effective_date,
        retrieved_date=retrieved_date,
        models=tuple(models),
        source_file=Path(source_path).name if source_path else None,
    )


def project_cell_cost(
    cell: Mapping[str, Any], snapshot: PricingSnapshot
) -> dict[str, Any]:
    """Project one cell from complete raw token classes under ``snapshot``."""

    model = str(cell.get("model") or "")
    entry = snapshot.resolve_model(model)
    if entry is None:
        return _unavailable("model_not_in_snapshot", model=model)

    fields = {
        "prompt_tokens": cell.get("prompt_tokens"),
        "completion_tokens": cell.get("completion_tokens"),
        "cache_read_input_tokens": cell.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": cell.get("cache_creation_input_tokens"),
    }
    missing = sorted(name for name, raw in fields.items() if raw is None)
    if missing:
        return _unavailable(
            "missing_token_classes",
            model=model,
            missing_fields=missing,
        )
    try:
        tokens = {name: _nonnegative_number(raw, name) for name, raw in fields.items()}
    except PricingSnapshotError as exc:
        return _unavailable("invalid_token_classes", model=model, detail=str(exc))

    prompt = tokens["prompt_tokens"]
    cache_read = tokens["cache_read_input_tokens"]
    cache_write = tokens["cache_creation_input_tokens"]
    if entry.token_semantics == "prompt_includes_cache":
        prompt -= cache_read + cache_write
        if prompt < 0:
            return _unavailable(
                "inconsistent_token_classes",
                model=model,
                detail="cache tokens exceed prompt_tokens",
            )

    billable = {
        "uncached_input": prompt,
        "output": tokens["completion_tokens"],
        "cache_read_input": cache_read,
        "cache_write_input": cache_write,
    }
    total = sum(
        billable[name] * entry.rates_per_million[name] / 1_000_000
        for name in _RATE_KEYS
    )
    return {
        "available": True,
        "kind": "offline_projection",
        "currency": snapshot.currency,
        "total": total,
        "model": model,
        "pricing_model_id": entry.model_id,
        "provider_channel": entry.channel,
        "token_semantics": entry.token_semantics,
        "cache_write_ttl": entry.cache_write_ttl,
        "billable_tokens": billable,
        "rates_per_million": dict(entry.rates_per_million),
        "source_url": entry.source_url,
        "snapshot": snapshot.public_identity(),
    }


def _unavailable(reason: str, **details: Any) -> dict[str, Any]:
    return {
        "available": False,
        "kind": "offline_projection",
        "reason": reason,
        **details,
    }


def _required_string(value: Mapping[str, Any], key: str, *, prefix: str = "") -> str:
    return _nonempty_string(value.get(key), f"{prefix}{key}")


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingSnapshotError(f"{field} must be a non-empty string")
    return value.strip()


def _date(value: Mapping[str, Any], key: str) -> str:
    selected = _required_string(value, key)
    if not _DATE_RE.fullmatch(selected):
        raise PricingSnapshotError(f"{key} must use YYYY-MM-DD")
    try:
        date.fromisoformat(selected)
    except ValueError as exc:
        raise PricingSnapshotError(f"{key} must be a valid calendar date") from exc
    return selected


def _rates(value: Any, *, index: int) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise PricingSnapshotError(
            f"models[{index}].rates_per_million must be an object"
        )
    missing = sorted(set(_RATE_KEYS) - set(value))
    extra = sorted(set(value) - set(_RATE_KEYS))
    if missing or extra:
        raise PricingSnapshotError(
            f"models[{index}].rates_per_million keys mismatch: "
            f"missing={missing}, extra={extra}"
        )
    return {
        key: _nonnegative_number(value[key], f"models[{index}].rates_per_million.{key}")
        for key in _RATE_KEYS
    }


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingSnapshotError(f"{field} must be a non-negative number")
    selected = float(value)
    if (
        selected < 0
        or selected != selected
        or selected in {float("inf"), float("-inf")}
    ):
        raise PricingSnapshotError(f"{field} must be a finite non-negative number")
    return selected


__all__ = [
    "DEFAULT_PRICING_SNAPSHOT",
    "PRICING_SNAPSHOT_SCHEMA",
    "ModelPrice",
    "PricingSnapshot",
    "PricingSnapshotError",
    "bundled_pricing_snapshot_path",
    "load_pricing_snapshot",
    "parse_pricing_snapshot",
    "project_cell_cost",
]
