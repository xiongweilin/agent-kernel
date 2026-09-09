from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capability_contract import (
    CapabilityContract,
    CapabilityContractRegistry,
)
from portable_runtime.core.models import Decision, Evidence, utcnow
from portable_runtime.records.authorization import (
    AuthorizationGrant,
    AuthorizationUse,
    CanonicalAuthorizationRequest,
    authorization_use_covers_request,
    create_authorization_use,
)
from portable_runtime.responsibility.domain_effect_authorization import (
    DOMAIN_EFFECT_INTENT_EVIDENCE_SCHEMA,
    REFERENCE_AUTHORIZATION_POLICY_REF,
    DomainEffectAuthorizationAdmission,
    DomainEffectAuthorizationResult,
    DomainEffectIntentEvidenceInput,
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectAuthorizationUseInput(BaseModel):
    """Identify one Kernel-owned runtime grant to consume at action time.

    The caller cannot provide capability, actor, resource, effect class, subject
    version, timestamp lineage, or any provider-facing field. Those dimensions
    are re-derived from the canonical Evidence -> Decision -> Grant chain.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_ref: str = Field(min_length=1)


class DomainEffectAuthorizationUseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["consumed"] = "consumed"
    authorization_ref: str
    authorization_use_ref: str
    evidence_ref: str
    decision_ref: str
    work_ref: str
    capability: str
    actor_ref: str
    resource_ref: str
    subject_version_ref: str
    authorized_at: datetime
    authority_bearing: bool = False


@dataclass(frozen=True, slots=True)
class DomainEffectAuthorizationUseContext:
    grant: AuthorizationGrant
    evidence: Evidence
    decision: Decision
    intent: DomainEffectIntentEvidenceInput
    admission: DomainEffectAuthorizationResult
    contract: CapabilityContract
    request: CanonicalAuthorizationRequest


class DomainEffectAuthorizationUseConsumption:
    """Consume one bounded domain-effect grant into immutable at-time evidence.

    This remains pre-cutover. It creates no Run, InvocationPermit, provider
    request, dispatch event, or Outcome. The resulting AuthorizationUse is the
    historical proof a later action-boundary commit must consume atomically.
    """

    def __init__(
        self,
        store: Any,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
    ) -> None:
        self.store = store
        self._contract_registry_supplied = contract_registry is not None
        self.contract_registry = contract_registry or CapabilityContractRegistry()

    def consume(
        self,
        value: DomainEffectAuthorizationUseInput,
        *,
        authorized_at: datetime | None = None,
    ) -> DomainEffectAuthorizationUseResult:
        with self.store.transaction():
            context = self._resolve_context(value.authorization_ref)
            use_id = _stable_id(
                "authuse_domain_effect",
                context.grant.id,
                context.evidence.id,
                context.admission.subject_version_ref,
            )
            existing = self.store.get_authorization_use(use_id)
            foreign = [
                use
                for use in self.store.list_authorization_uses()
                if isinstance(use, AuthorizationUse)
                and use.authorization_ref == context.grant.id
                and use.id != use_id
            ]
            if foreign:
                raise ValueError(
                    "domain effect runtime grant was consumed outside the canonical use identity"
                )
            if existing is not None:
                return self._replay_result(context, existing, use_id)

            created = create_authorization_use(
                context.grant,
                context.request,
                authorized_at=authorized_at or utcnow(),
            )
            use = created.model_copy(
                update={
                    "id": use_id,
                    "created_at": created.authorized_at,
                }
            )
            self.store.save_authorization_use(use)
            return self._result(context, use)

    def _resolve_context(
        self,
        authorization_ref: str,
    ) -> DomainEffectAuthorizationUseContext:
        grant = self.store.get_authorization(authorization_ref)
        if not isinstance(grant, AuthorizationGrant):
            raise ValueError(
                "domain effect authorization use requires an existing AuthorizationGrant"
            )

        policy_ref = grant.metadata.get("policy_ref")
        if policy_ref != REFERENCE_AUTHORIZATION_POLICY_REF:
            raise ValueError(
                "domain effect authorization use requires the bounded reference policy"
            )
        if grant.principal_ref != f"policy:{REFERENCE_AUTHORIZATION_POLICY_REF}":
            raise ValueError(
                "runtime grant principal does not match the bounded reference policy"
            )

        decision_ref = grant.source_decision_ref
        if not isinstance(decision_ref, str) or not decision_ref:
            raise ValueError("domain effect runtime grant lacks its source Decision")
        decision = self.store.get_decision(decision_ref)
        if not isinstance(decision, Decision):
            raise ValueError("domain effect runtime grant references an unknown Decision")
        if decision.decision_type != "runtime-authorization-admission":
            raise ValueError("source Decision is not a runtime authorization admission")
        if decision.selected_option != "authorized":
            raise ValueError("source Decision did not authorize the domain effect")
        if decision.metadata.get("policy_ref") != REFERENCE_AUTHORIZATION_POLICY_REF:
            raise ValueError(
                "source Decision policy does not match the bounded reference policy"
            )

        evidence_ref = grant.metadata.get("domain_effect_intent_evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise ValueError(
                "domain effect runtime grant lacks intent Evidence provenance"
            )
        if decision.metadata.get("domain_effect_intent_evidence_ref") != evidence_ref:
            raise ValueError(
                "runtime Decision and AuthorizationGrant disagree on intent Evidence"
            )
        evidence = self.store.get_evidence(evidence_ref)
        if not isinstance(evidence, Evidence):
            raise ValueError(
                "domain effect runtime grant references unknown intent Evidence"
            )

        intent = self._intent_from_evidence(evidence)
        if decision.work_id != intent.work_ref:
            raise ValueError("runtime Decision is bound to a different Work")
        if grant.metadata.get("work_ref") != intent.work_ref:
            raise ValueError("runtime AuthorizationGrant is bound to a different Work")

        replay_registry, contract = self._contract_for_replay(grant, intent)
        admission = DomainEffectAuthorizationAdmission(
            self.store,
            contract_registry=replay_registry,
        ).admit(intent)
        if admission.status != "authorized":
            raise ValueError(
                "current domain effect lineage no longer resolves to authorized"
            )
        if admission.authorization_ref != grant.id:
            raise ValueError(
                "current domain effect lineage resolves to a different runtime grant"
            )
        if (
            admission.evidence_ref != evidence.id
            or admission.decision_ref != decision.id
        ):
            raise ValueError(
                "current domain effect lineage does not match stored authorization provenance"
            )

        self._validate_exact_grant(
            grant,
            evidence,
            decision,
            intent,
            admission,
            contract,
        )
        request = CanonicalAuthorizationRequest(
            capability=intent.capability,
            actor_ref=admission.actor_ref,
            resource_ref=admission.resource_ref,
            subject_version_refs=[admission.subject_version_ref],
            effect_class=contract.minimum_impact_class,
        )
        return DomainEffectAuthorizationUseContext(
            grant=grant,
            evidence=evidence,
            decision=decision,
            intent=intent,
            admission=admission,
            contract=contract,
            request=request,
        )

    def _contract_for_replay(
        self,
        grant: AuthorizationGrant,
        intent: DomainEffectIntentEvidenceInput,
    ) -> tuple[CapabilityContractRegistry, CapabilityContract]:
        raw = grant.metadata.get("capability_contract")
        if not isinstance(raw, dict):
            # Compatibility path for grants minted before contract snapshots
            # were added. These remain resolvable only when the consumer's
            # registry already knows the exact capability.
            contract = self.contract_registry.resolve(intent.capability)
            return self.contract_registry, contract

        snapshot = CapabilityContract.model_validate(raw)
        if snapshot.capability != intent.capability:
            raise ValueError("runtime grant capability contract snapshot rebound")
        if self._contract_registry_supplied:
            current = self.contract_registry.resolve(intent.capability)
            if current.model_dump(mode="json") != snapshot.model_dump(mode="json"):
                raise ValueError("current capability contract drifted from authorization snapshot")
            return self.contract_registry, current

        replay_registry = CapabilityContractRegistry(contracts=[snapshot])
        return replay_registry, replay_registry.resolve(intent.capability)

    @staticmethod
    def _intent_from_evidence(evidence: Evidence) -> DomainEffectIntentEvidenceInput:
        if evidence.kind != "domain-effect-intent":
            raise ValueError("runtime authorization Evidence has the wrong kind")
        if evidence.source != "domain:administrative-orchestrator":
            raise ValueError("runtime authorization Evidence has the wrong source")
        metadata = evidence.metadata
        if metadata.get("schema") != DOMAIN_EFFECT_INTENT_EVIDENCE_SCHEMA:
            raise ValueError("runtime authorization Evidence has the wrong schema")
        if metadata.get("authority_bearing") is not False:
            raise ValueError(
                "domain effect intent Evidence must remain non-authoritative"
            )
        parameters = metadata.get("parameters")
        postcondition = metadata.get("expected_postcondition")
        if not isinstance(parameters, dict) or not isinstance(postcondition, dict):
            raise ValueError(
                "domain effect intent Evidence lacks frozen parameters/postcondition"
            )
        intent = DomainEffectIntentEvidenceInput.model_validate(
            {
                "work_ref": metadata.get("work_ref"),
                "domain_intent_ref": metadata.get("domain_intent_ref"),
                "domain_grant_ref": metadata.get("domain_grant_ref"),
                "governance_basis_ref": metadata.get("governance_basis_ref"),
                "approval_satisfaction_ref": metadata.get("approval_satisfaction_ref"),
                "capability": metadata.get("capability"),
                "subject_ref": metadata.get("subject_ref"),
                "authority_epoch": metadata.get("authority_epoch"),
                "parameters": parameters,
                "expected_postcondition": postcondition,
                "observed_at": evidence.observed_at,
            }
        )
        if evidence.subject_refs != [intent.work_ref]:
            raise ValueError(
                "domain effect intent Evidence has invalid core subject refs"
            )
        return intent

    @staticmethod
    def _validate_exact_grant(
        grant: AuthorizationGrant,
        evidence: Evidence,
        decision: Decision,
        intent: DomainEffectIntentEvidenceInput,
        admission: DomainEffectAuthorizationResult,
        contract: CapabilityContract,
    ) -> None:
        if (
            contract.minimum_impact_class != "write-remote"
            or contract.effect_semantics != "reconcilable"
            or contract.reversibility != "compensatable"
            or contract.authorization_requirement != "required"
            or not contract.resource_required
            or not contract.subject_version_required
        ):
            raise ValueError("current capability contract no longer permits bounded use")
        if grant.source_decision_ref != decision.id:
            raise ValueError("runtime grant source Decision rebound")
        if grant.grantee_ref != admission.actor_ref:
            raise ValueError("runtime grant actor binding drifted")
        if grant.allowed_capabilities != [intent.capability]:
            raise ValueError("runtime grant capability binding drifted")
        if grant.resource_scope != [admission.resource_ref]:
            raise ValueError("runtime grant resource binding drifted")
        if grant.subject_version_refs != [admission.subject_version_ref]:
            raise ValueError("runtime grant subject-version binding drifted")
        if grant.effect_ceiling != contract.minimum_impact_class:
            raise ValueError("runtime grant effect ceiling drifted")
        if grant.conditions:
            raise ValueError(
                "runtime grant contains unsupported free-form conditions"
            )
        bindings = [
            condition
            for condition in grant.typed_conditions
            if condition.kind == "domain-business-authority-evidence"
            and condition.satisfied
            and condition.authority_ref == evidence.id
            and condition.params.get("evidence_ref") == evidence.id
        ]
        if len(grant.typed_conditions) != 1 or len(bindings) != 1:
            raise ValueError(
                "runtime grant does not exactly bind domain business authority Evidence"
            )
        if grant.metadata.get("domain_effect_intent_evidence_ref") != evidence.id:
            raise ValueError("runtime grant intent Evidence metadata drifted")
        if grant.metadata.get("work_ref") != decision.work_id:
            raise ValueError("runtime grant Work metadata drifted")
        for metadata_key in ("responsibility_ref", "proposal_ref"):
            if grant.metadata.get(metadata_key) != decision.metadata.get(metadata_key):
                raise ValueError(
                    f"runtime grant {metadata_key} metadata drifted from Decision"
                )
            if grant.metadata.get(metadata_key) != evidence.metadata.get(metadata_key):
                raise ValueError(
                    f"runtime grant {metadata_key} metadata drifted from Evidence"
                )

    def _replay_result(
        self,
        context: DomainEffectAuthorizationUseContext,
        existing: object,
        expected_use_id: str,
    ) -> DomainEffectAuthorizationUseResult:
        if not isinstance(existing, AuthorizationUse) or existing.id != expected_use_id:
            raise ValueError("domain effect AuthorizationUse identity rebound")
        if not authorization_use_covers_request(
            existing,
            context.grant,
            context.request,
        ):
            raise ValueError(
                "existing domain effect AuthorizationUse does not cover the canonical request"
            )
        return self._result(context, existing)

    @staticmethod
    def _result(
        context: DomainEffectAuthorizationUseContext,
        use: AuthorizationUse,
    ) -> DomainEffectAuthorizationUseResult:
        return DomainEffectAuthorizationUseResult(
            authorization_ref=context.grant.id,
            authorization_use_ref=use.id,
            evidence_ref=context.evidence.id,
            decision_ref=context.decision.id,
            work_ref=context.intent.work_ref,
            capability=context.request.capability,
            actor_ref=context.request.actor_ref,
            resource_ref=context.request.resource_ref or "",
            subject_version_ref=context.admission.subject_version_ref,
            authorized_at=use.authorized_at,
            authority_bearing=False,
        )


__all__ = [
    "DomainEffectAuthorizationUseConsumption",
    "DomainEffectAuthorizationUseContext",
    "DomainEffectAuthorizationUseInput",
    "DomainEffectAuthorizationUseResult",
]