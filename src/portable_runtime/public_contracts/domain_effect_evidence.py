from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.runtime import Runtime
from portable_runtime.records.models import EvidenceArtifact
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA,
)

DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA = (
    "domain-effect-verification-evidence-view-v1"
)


class DomainEffectVerificationEvidenceViewV1(BaseModel):
    """Non-authoritative read projection of Kernel's independent reality proof."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["domain-effect-verification-evidence-view-v1"] = Field(
        "domain-effect-verification-evidence-view-v1",
        alias="schema",
    )
    evidence_ref: str = Field(min_length=1)
    action_ref: str = Field(min_length=1)
    work_ref: str = Field(min_length=1)
    run_ref: str = Field(min_length=1)
    objective_result: Literal["pass", "fail"]
    observed_postcondition: dict[str, Any]
    expected_postcondition: dict[str, Any]
    verification_request_ref: str = Field(min_length=1)
    verification_attempt_ref: str = Field(min_length=1)
    verifier_provider_id: str = Field(min_length=1)
    verifier_provider_execution_binding_ref: str = Field(min_length=1)
    captured_at: datetime
    authority_bearing: Literal[False] = False


def _required_string(mapping: dict[str, Any], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"domain effect evidence lacks {name}")
    return value


def project_domain_effect_verification_evidence(
    runtime: Runtime,
    evidence_ref: str,
) -> DomainEffectVerificationEvidenceViewV1 | None:
    """Project one canonical EvidenceArtifact without minting new truth or authority."""

    record = runtime.store.get_record(evidence_ref)
    if record is None:
        return None
    if not isinstance(record, EvidenceArtifact):
        raise ValueError("domain effect evidence ref does not identify an EvidenceArtifact")
    metadata = dict(record.metadata or {})
    if metadata.get("schema") != DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA:
        raise ValueError("evidence is not canonical domain-effect objective verification proof")
    if record.kind != "task-objective-proof":
        raise ValueError("domain effect evidence has unexpected proof kind")
    if metadata.get("proof_class") != "objective-verification":
        raise ValueError("domain effect evidence has unexpected proof class")

    verification = metadata.get("verification_result")
    if not isinstance(verification, dict):
        raise ValueError("domain effect evidence lacks verification_result")
    objective_result = verification.get("result")
    if objective_result not in {"pass", "fail"}:
        raise ValueError("domain effect evidence lacks closed objective result")

    observed = metadata.get("observed_postcondition")
    if not isinstance(observed, dict):
        raise ValueError("domain effect evidence lacks observed_postcondition")
    verification_scope = metadata.get("verification_scope")
    if not isinstance(verification_scope, dict):
        raise ValueError("domain effect evidence lacks verification_scope")
    expected = verification_scope.get("expected_postcondition")
    if not isinstance(expected, dict):
        raise ValueError("domain effect evidence lacks frozen expected_postcondition")

    verifier = metadata.get("verifier_provenance")
    if not isinstance(verifier, dict):
        raise ValueError("domain effect evidence lacks verifier provenance")

    return DomainEffectVerificationEvidenceViewV1(
        schema=DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA,
        evidence_ref=record.id,
        action_ref=_required_string(metadata, "action_ref"),
        work_ref=_required_string(metadata, "work_id"),
        run_ref=_required_string(metadata, "run_id"),
        objective_result=objective_result,
        observed_postcondition=dict(observed),
        expected_postcondition=dict(expected),
        verification_request_ref=_required_string(metadata, "verification_request_ref"),
        verification_attempt_ref=_required_string(metadata, "verification_attempt_ref"),
        verifier_provider_id=_required_string(verifier, "provider_id"),
        verifier_provider_execution_binding_ref=_required_string(
            verifier,
            "provider_execution_binding_ref",
        ),
        captured_at=record.created_at,
    )


__all__ = [
    "DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA",
    "DomainEffectVerificationEvidenceViewV1",
    "project_domain_effect_verification_evidence",
]
