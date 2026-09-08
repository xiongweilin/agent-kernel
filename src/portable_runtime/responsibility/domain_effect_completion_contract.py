from __future__ import annotations

import hashlib
import json
from typing import Any

from portable_runtime.core.models import Work
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseContext,
)

DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA = "domain-effect-objective-verification-scope-v1"
DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA = "domain-effect-completion-contract-v1"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_domain_effect_verification_scope(
    context: DomainEffectAuthorizationUseContext,
) -> dict[str, Any]:
    """Build the exact objective-verification scope from durable authorization facts."""

    return {
        "schema": DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA,
        "effect_capability": context.intent.capability,
        "resource_ref": context.admission.resource_ref,
        "subject_ref": context.intent.subject_ref,
        "expected_postcondition": dict(context.intent.expected_postcondition),
    }


def build_domain_effect_completion_contract(
    work: Work,
    context: DomainEffectAuthorizationUseContext,
) -> dict[str, Any]:
    """Build the bounded completion contract before any external effect occurs."""

    if work.id != context.intent.work_ref:
        raise ValueError("domain effect completion contract Work binding mismatch")
    acceptance_criteria = [
        criterion.strip()
        for criterion in work.acceptance_criteria
        if isinstance(criterion, str) and criterion.strip()
    ]
    if not acceptance_criteria:
        raise ValueError("domain effect completion requires explicit Work acceptance criteria")
    metadata = work.metadata if isinstance(work.metadata, dict) else {}
    work_version = metadata.get("work_version", 1)
    return {
        "schema": DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA,
        "verification_scope": build_domain_effect_verification_scope(context),
        "work_version": work_version,
        "acceptance_criteria": acceptance_criteria,
        "required_obligations": acceptance_criteria,
        "subject_version_refs": [context.admission.subject_version_ref],
    }


def domain_effect_completion_contract_digest(contract: dict[str, Any]) -> str:
    if contract.get("schema") != DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA:
        raise ValueError("domain effect completion contract has wrong schema")
    return _canonical_digest(contract)


def freeze_domain_effect_completion_contract(
    work: Work,
    context: DomainEffectAuthorizationUseContext,
) -> Work:
    """Persistable Work snapshot with a pre-effect completion contract frozen in metadata."""

    contract = build_domain_effect_completion_contract(work, context)
    metadata = dict(work.metadata) if isinstance(work.metadata, dict) else {}

    existing_scope = metadata.get("verification_scope")
    if existing_scope is not None and existing_scope != contract["verification_scope"]:
        raise ValueError("Work verification scope conflicts with domain effect completion contract")
    existing_obligations = metadata.get("verification_obligations")
    if existing_obligations is not None and existing_obligations != contract["required_obligations"]:
        raise ValueError("Work verification obligations conflict with domain effect completion contract")
    existing_contract = metadata.get("domain_effect_completion_contract")
    if existing_contract is not None and existing_contract != contract:
        raise ValueError("Work domain effect completion contract is already bound differently")

    metadata["verification_scope"] = dict(contract["verification_scope"])
    metadata["work_version"] = contract["work_version"]
    metadata["verification_obligations"] = list(contract["required_obligations"])
    metadata["domain_effect_completion_contract"] = dict(contract)
    metadata["domain_effect_completion_contract_digest"] = domain_effect_completion_contract_digest(
        contract
    )
    return work.model_copy(update={"metadata": metadata})


def require_domain_effect_completion_contract(
    work: Work,
    context: DomainEffectAuthorizationUseContext,
) -> tuple[dict[str, Any], str]:
    """Re-read and validate the exact pre-effect contract; never backfill it."""

    metadata = work.metadata if isinstance(work.metadata, dict) else {}
    stored = metadata.get("domain_effect_completion_contract")
    if not isinstance(stored, dict):
        raise ValueError("domain effect Work lacks pre-effect completion contract")
    expected = build_domain_effect_completion_contract(work, context)
    if stored != expected:
        raise ValueError("domain effect Work completion contract drifted")
    digest = domain_effect_completion_contract_digest(expected)
    if metadata.get("domain_effect_completion_contract_digest") != digest:
        raise ValueError("domain effect Work completion contract digest drifted")
    if metadata.get("verification_scope") != expected["verification_scope"]:
        raise ValueError("domain effect Work verification scope drifted")
    if metadata.get("verification_obligations") != expected["required_obligations"]:
        raise ValueError("domain effect Work verification obligations drifted")
    if metadata.get("work_version") != expected["work_version"]:
        raise ValueError("domain effect Work version drifted from completion contract")
    return expected, digest


__all__ = [
    "DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA",
    "DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA",
    "build_domain_effect_completion_contract",
    "build_domain_effect_verification_scope",
    "domain_effect_completion_contract_digest",
    "freeze_domain_effect_completion_contract",
    "require_domain_effect_completion_contract",
]
