# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Static diagnostics for LiteLLM-backed product configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .options import validate_model_options

_OPTION_REQUIREMENTS = {
    "AZURE_API_VERSION": ("api_version",),
    "VERTEXAI_LOCATION": ("vertex_location",),
    "VERTEXAI_PROJECT": ("vertex_project", "vertex_project_id"),
}
_CREDENTIAL_CHAIN_PROVIDERS = {
    "bedrock": "AWS SDK credentials are verified only by the live probe",
    "sagemaker": "AWS SDK credentials are verified only by the live probe",
    "vertex_ai": "Google Application Default Credentials are verified only by the live probe",
}


@dataclass(frozen=True)
class ModelBackendDiagnostic:
    """Redacted, actionable readiness report for one model route."""

    model: str
    provider: str
    configured: bool
    api_base: str | None
    auth_source: str | None
    option_names: tuple[str, ...]
    missing_keys: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def detail(self) -> str:
        parts = [self.model, f"provider={self.provider or 'unresolved'}"]
        if self.api_base:
            parts.append(f"endpoint={self.api_base}")
        if self.auth_source:
            parts.append(f"auth={self.auth_source}")
        if self.option_names:
            parts.append("options=" + ",".join(self.option_names))
        if self.missing_keys:
            parts.append("missing " + ", ".join(self.missing_keys))
        parts.extend(f"issue={issue}" for issue in self.issues)
        parts.extend(f"note={note}" for note in self.notes)
        return "; ".join(parts)


def _endpoint_issue(api_base: str | None) -> str | None:
    if not api_base:
        return None
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "API base must be an absolute http(s) URL"
    if parsed.path.rstrip("/").endswith(("/chat/completions", "/responses")):
        return "API base must not include an operation path"
    return None


def _option_supplies_requirement(
    requirement: str,
    options: Mapping[str, Any],
) -> bool:
    return any(
        options.get(name) not in (None, "")
        for name in _OPTION_REQUIREMENTS.get(requirement, ())
    )


def diagnose_model_backend(
    *,
    model: str,
    api_base: str | None = None,
    api_key: str | None = None,
    auth_source: str | None = None,
    options: Mapping[str, Any] | None = None,
) -> ModelBackendDiagnostic:
    """Resolve a LiteLLM route and reconcile explicit options with env checks."""
    normalized_options = validate_model_options(
        options,
        source="model diagnostics options",
    )
    endpoint_issue = _endpoint_issue(api_base)
    if endpoint_issue:
        return ModelBackendDiagnostic(
            model=model,
            provider="",
            configured=False,
            api_base=api_base,
            auth_source=auth_source,
            option_names=tuple(sorted(normalized_options)),
            issues=(endpoint_issue,),
        )

    from . import litellm_chat

    try:
        _, provider, _, _ = litellm_chat.litellm.get_llm_provider(
            model=model,
            api_base=api_base,
            api_key=api_key,
        )
    except Exception:
        return ModelBackendDiagnostic(
            model=model,
            provider="",
            configured=False,
            api_base=api_base,
            auth_source=auth_source,
            option_names=tuple(sorted(normalized_options)),
            issues=(
                "model must include a LiteLLM provider prefix "
                "(for example openai/<served-model>)",
            ),
        )

    try:
        environment = litellm_chat.litellm.validate_environment(
            model=model,
            api_base=api_base,
            api_key=api_key,
        )
    except Exception as exc:
        return ModelBackendDiagnostic(
            model=model,
            provider=provider,
            configured=False,
            api_base=api_base,
            auth_source=auth_source,
            option_names=tuple(sorted(normalized_options)),
            issues=(str(exc).splitlines()[0][:240],),
        )

    missing = {
        str(key)
        for key in environment.get("missing_keys") or ()
        if not _option_supplies_requirement(str(key), normalized_options)
    }
    notes_list = []
    if provider == "openai" and api_base and "OPENAI_API_KEY" in missing:
        # OpenAI-compatible gateways may intentionally run without
        # authentication. Only a live request can distinguish that from a
        # gateway whose key is missing.
        missing.remove("OPENAI_API_KEY")
        notes_list.append(
            "custom endpoint authentication is verified only by the live probe"
        )
    chain_note = _CREDENTIAL_CHAIN_PROVIDERS.get(provider)
    if chain_note:
        notes_list.append(chain_note)
    return ModelBackendDiagnostic(
        model=model,
        provider=provider,
        configured=not missing,
        api_base=api_base,
        auth_source=auth_source or ("explicit" if api_key else None),
        option_names=tuple(sorted(normalized_options)),
        missing_keys=tuple(sorted(missing)),
        notes=tuple(notes_list),
    )


__all__ = ["ModelBackendDiagnostic", "diagnose_model_backend"]
