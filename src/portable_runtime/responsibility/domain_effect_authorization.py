from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capability_contract import (
    CapabilityContract,
    CapabilityContractRegistry,
)
from portable_runtime.core.models import Decision, Evidence, Work, utcnow
from portable_runtime.records.authorization import AuthorizationGrant, TypedCondition
from portable_runtime.responsibility.models import (
    EffectClass,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    ResponsibilityStatus,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel

ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE = "administrative.hris.employee.create.v1"
DOMAIN_EFFECT_INTENT_EVIDENCE_SCHEMA = "domain-effect-intent-evidence-v1"
REFERENCE_AUTHORIZATION_POLICY_REF = "kernel-administrative-runtime-authorization-v1"
RUNTIME_ADMINISTRATIVE_ACTOR = "runtime:administrative-effect-executor"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _capability_for(target_system: str, operation: str) -> str:
    target = target_system.strip().lower().replace("_", "-")
    normalized_operation = operation.strip().lower().replace("_", "-")
    if not target or not normalized_operation:
        raise ValueError(
            "administrative responsibility scope has an empty target or operation"
        )
    return f"administrative.{target}.{normalized_operation}.v1"


class DomainEffectIntentEvidenceInput(BaseModel):
    """Non-authoritative domain evidence for one admitted Kernel Work item."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    work_ref: str = Field(min_length=1)
    domain_intent_ref: str = Field(min_length=1)
    domain_grant_ref: str = Field(min_length=1)
    governance_basis_ref: str = Field(min_length=1)
    approval_satisfaction_ref: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    authority_epoch: int = Field(ge=0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class DomainEffectAuthorizationContext:
    work: Work
    responsibility: StandingResponsibility
    admission: ResponsibilityAdmission
    assessment: ResponsibilityAssessment
    proposal: WorkProposal
    intent: DomainEffectIntentEvidenceInput
    contract: CapabilityContract
    actor_ref: str
    resource_ref: str
    subject_version_ref: str


@dataclass(frozen=True, slots=True)
class DomainEffectAuthorizationJudgment:
    admitted: bool
    reason: str
    ttl_seconds: int = 900


class DomainEffectAuthorizationPolicy(Protocol):
    policy_ref: str

    def evaluate(
        self,
        context: DomainEffectAuthorizationContext,
    ) -> DomainEffectAuthorizationJudgment: ...


class ReferenceAdministrativeEffectAuthorizationPolicy:
    """Kernel-owned structural policy for bounded administrative effects."""

    policy_ref = REFERENCE_AUTHORIZATION_POLICY_REF

    def evaluate(
        self,
        context: DomainEffectAuthorizationContext,
    ) -> DomainEffectAuthorizationJudgment:
        contract = context.contract
        if context.work.kind != "administrative-effect":
            return DomainEffectAuthorizationJudgment(
                False,
                "Work is not an administrative effect",
            )
        if context.responsibility.responsibility_kind != "administrative-obligation":
            return DomainEffectAuthorizationJudgment(
                False,
                "responsibility is not an administrative obligation",
            )
        if contract.minimum_impact_class != "write-remote":
            return DomainEffectAuthorizationJudgment(
                False,
                "capability impact is not the approved write-remote class",
            )
        if (
            contract.effect_semantics != "reconcilable"
            or contract.reversibility != "compensatable"
        ):
            return DomainEffectAuthorizationJudgment(
                False,
                "capability lacks required reconciliation/compensation semantics",
            )
        if contract.authorization_requirement != "required":
            return DomainEffectAuthorizationJudgment(
                False,
                "capability must require runtime authorization",
            )
        if not contract.resource_required or not contract.subject_version_required:
            return DomainEffectAuthorizationJudgment(
                False,
                "capability must require resource and subject-version binding",
            )
        if context.work.metadata.get("external_effect_authority") != "required-separately":
            return DomainEffectAuthorizationJudgment(
                False,
                "Work does not preserve separate external-effect authority",
            )
        return DomainEffectAuthorizationJudgment(
            True,
            "bounded administrative effect is eligible for runtime authorization",
        )


class DomainEffectAuthorizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["authorized", "rejected"]
    evidence_ref: str
    decision_ref: str
    authorization_ref: str | None = None
    policy_ref: str
    actor_ref: str
    resource_ref: str
    subject_version_ref: str
    reason: str
    authority_bearing: bool = False


class DomainEffectAuthorizationAdmission:
    """Admit domain evidence into Kernel-owned runtime authorization semantics."""

    def __init__(
        self,
        store: Any,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
        policy: DomainEffectAuthorizationPolicy | None = None,
    ) -> None:
        self.store = store
        self.contract_registry = contract_registry or CapabilityContractRegistry()
        self.policy = policy or ReferenceAdministrativeEffectAuthorizationPolicy()
        self.kernel = ResponsibilityKernel(store)

    def admit(
        self,
        intent: DomainEffectIntentEvidenceInput,
        *,
        now: datetime | None = None,
    ) -> DomainEffectAuthorizationResult:
        context = self._resolve_context(intent)
        evidence = self._evidence(context)
        decision_id = _stable_id(
            "decision_domain_effect_authorization",
            evidence.id,
            self.policy.policy_ref,
        )

        existing_decision = self.store.get_decision(decision_id)
        if existing_decision is not None:
            return self._replay_result(context, evidence, existing_decision)

        judgment = self.policy.evaluate(context)
        decided_at = now or utcnow()
        decision = self._decision(
            context,
            evidence,
            judgment,
            decision_id=decision_id,
            decided_at=decided_at,
        )
        grant = self._grant(
            context,
            evidence,
            decision,
            judgment,
            decided_at=decided_at,
        )

        with self.store.transaction():
            self.store.save_evidence(evidence)
            self.store.save_decision(decision)
            if grant is not None:
                self.store.save_authorization(grant)

        return DomainEffectAuthorizationResult(
            status="authorized" if grant is not None else "rejected",
            evidence_ref=evidence.id,
            decision_ref=decision.id,
            authorization_ref=grant.id if grant is not None else None,
            policy_ref=self.policy.policy_ref,
            actor_ref=context.actor_ref,
            resource_ref=context.resource_ref,
            subject_version_ref=context.subject_version_ref,
            reason=judgment.reason,
            authority_bearing=False,
        )

    def _resolve_context(
        self,
        intent: DomainEffectIntentEvidenceInput,
    ) -> DomainEffectAuthorizationContext:
        work = self.store.get_work(intent.work_ref)
        if work is None:
            raise ValueError("domain effect intent requires an existing admitted Work")
        if not isinstance(work.metadata, dict):
            raise ValueError("admitted Work lacks responsibility metadata")

        responsibility_ref = work.metadata.get("standing_responsibility_ref")
        proposal_ref = work.metadata.get("responsibility_proposal_ref")
        responsibility_version = work.metadata.get("standing_responsibility_version")
        if not isinstance(responsibility_ref, str) or not isinstance(proposal_ref, str):
            raise ValueError(
                "admitted Work lacks canonical responsibility/proposal refs"
            )

        responsibility = self.kernel.journal.get(responsibility_ref)
        proposal = self.kernel.journal.get(proposal_ref)
        if not isinstance(responsibility, StandingResponsibility):
            raise ValueError(
                "Work responsibility ref does not resolve to StandingResponsibility"
            )
        if not isinstance(proposal, WorkProposal):
            raise ValueError("Work proposal ref does not resolve to WorkProposal")
        assessment = self.kernel.journal.get(proposal.assessment_ref)
        if not isinstance(assessment, ResponsibilityAssessment):
            raise ValueError("Work proposal assessment ref is invalid")

        admission = self._current_initial_admission(
            responsibility,
            proposal.responsibility_version,
        )
        current_version, _statement, scope = self.kernel.current_definition(
            responsibility.id
        )
        self._validate_current_chain(
            context_work=work,
            responsibility=responsibility,
            proposal=proposal,
            intent=intent,
            current_version=current_version,
            work_responsibility_version=responsibility_version,
        )
        self._validate_scope_and_provenance(
            scope=scope,
            admission=admission,
            assessment=assessment,
            intent=intent,
        )

        contract = self.contract_registry.resolve(intent.capability)
        actor_ref = RUNTIME_ADMINISTRATIVE_ACTOR
        resource_ref = f"administrative:{scope['target_system']}:{intent.subject_ref}"
        subject_version_ref = (
            "administrative-authority-epoch:"
            f"{scope['administrative_case_id']}:{intent.authority_epoch}"
        )
        return DomainEffectAuthorizationContext(
            work=work,
            responsibility=responsibility,
            admission=admission,
            assessment=assessment,
            proposal=proposal,
            intent=intent,
            contract=contract,
            actor_ref=actor_ref,
            resource_ref=resource_ref,
            subject_version_ref=subject_version_ref,
        )

    def _current_initial_admission(
        self,
        responsibility: StandingResponsibility,
        responsibility_version: int,
    ) -> ResponsibilityAdmission:
        admissions = [
            value
            for value in self.kernel.journal.list(
                "ResponsibilityAdmission",
                responsibility.id,
            )
            if isinstance(value, ResponsibilityAdmission)
            and value.responsibility_version == responsibility_version
        ]
        if len(admissions) != 1:
            raise ValueError(
                "responsibility must have exactly one current initial admission"
            )
        return admissions[0]

    def _validate_current_chain(
        self,
        *,
        context_work: Work,
        responsibility: StandingResponsibility,
        proposal: WorkProposal,
        intent: DomainEffectIntentEvidenceInput,
        current_version: int,
        work_responsibility_version: object,
    ) -> None:
        if self.kernel.current_status(responsibility.id) is not ResponsibilityStatus.ACTIVE:
            raise ValueError("domain effect authorization requires active responsibility")
        if (
            current_version != proposal.responsibility_version
            or work_responsibility_version != current_version
        ):
            raise ValueError("admitted Work is bound to a stale responsibility version")
        if proposal.effect_class is not EffectClass.EXTERNAL_EFFECT:
            raise ValueError("domain effect authorization requires external-effect Work")
        if (
            proposal.requested_capabilities != [intent.capability]
            or context_work.requested_capabilities != [intent.capability]
        ):
            raise ValueError(
                "domain effect capability must exactly match admitted proposal and Work"
            )
        if proposal.subject_ref != intent.subject_ref:
            raise ValueError("domain effect subject does not match admitted proposal")

    def _validate_scope_and_provenance(
        self,
        *,
        scope: dict[str, str],
        admission: ResponsibilityAdmission,
        assessment: ResponsibilityAssessment,
        intent: DomainEffectIntentEvidenceInput,
    ) -> None:
        required_scope = {
            "administrative_case_id",
            "authority_epoch",
            "execution_grant_id",
            "governance_basis_id",
            "target_system",
            "operation",
        }
        if not required_scope.issubset(scope):
            raise ValueError("administrative responsibility scope is incomplete")
        if scope["authority_epoch"] != str(intent.authority_epoch):
            raise ValueError(
                "domain effect intent is bound to a stale administrative authority epoch"
            )
        if scope["execution_grant_id"] != intent.domain_grant_ref:
            raise ValueError(
                "domain effect intent does not bind the responsibility execution grant"
            )
        if scope["governance_basis_id"] != intent.governance_basis_ref:
            raise ValueError(
                "domain effect intent does not bind the responsibility governance basis"
            )

        expected_capability = _capability_for(
            scope["target_system"],
            scope["operation"],
        )
        if intent.capability != expected_capability:
            raise ValueError(
                "domain effect capability does not match responsibility target/operation"
            )

        self._require_basis_refs(
            set(assessment.basis_refs),
            (
                f"administrative-intent:{intent.domain_intent_ref}",
                f"administrative-grant:{intent.domain_grant_ref}",
                f"governance-basis:{intent.governance_basis_ref}",
            ),
            "domain effect intent provenance does not match responsibility assessment",
        )
        self._require_basis_refs(
            set(admission.basis_refs),
            (
                f"administrative-grant:{intent.domain_grant_ref}",
                f"governance-basis:{intent.governance_basis_ref}",
                f"approval-satisfaction:{intent.approval_satisfaction_ref}",
            ),
            "domain effect authority provenance does not match responsibility admission",
        )

    @staticmethod
    def _require_basis_refs(
        actual: set[str],
        required: tuple[str, ...],
        message: str,
    ) -> None:
        if any(value not in actual for value in required):
            raise ValueError(message)

    def _evidence(self, context: DomainEffectAuthorizationContext) -> Evidence:
        intent = context.intent
        evidence_id = _stable_id(
            "evidence_domain_effect_intent",
            context.work.id,
            intent.domain_intent_ref,
            intent.authority_epoch,
        )
        return Evidence(
            id=evidence_id,
            created_at=intent.observed_at,
            kind="domain-effect-intent",
            # Responsibility and business subject identities live in separate
            # semantic planes. Keep them as provenance metadata rather than
            # creating invalid core state-graph subject edges.
            subject_refs=[context.work.id],
            source="domain:administrative-orchestrator",
            observed_at=intent.observed_at,
            status="supported",
            metadata={
                "schema": DOMAIN_EFFECT_INTENT_EVIDENCE_SCHEMA,
                "authority_bearing": False,
                "domain_intent_ref": intent.domain_intent_ref,
                "domain_grant_ref": intent.domain_grant_ref,
                "governance_basis_ref": intent.governance_basis_ref,
                "approval_satisfaction_ref": intent.approval_satisfaction_ref,
                "responsibility_ref": context.responsibility.id,
                "proposal_ref": context.proposal.id,
                "work_ref": context.work.id,
                "capability": intent.capability,
                "subject_ref": intent.subject_ref,
                "authority_epoch": intent.authority_epoch,
                "parameters": dict(intent.parameters),
                "expected_postcondition": dict(intent.expected_postcondition),
                "derived_actor_ref": context.actor_ref,
                "derived_resource_ref": context.resource_ref,
                "derived_subject_version_ref": context.subject_version_ref,
            },
        )

    def _decision(
        self,
        context: DomainEffectAuthorizationContext,
        evidence: Evidence,
        judgment: DomainEffectAuthorizationJudgment,
        *,
        decision_id: str,
        decided_at: datetime,
    ) -> Decision:
        return Decision(
            id=decision_id,
            created_at=decided_at,
            work_id=context.work.id,
            decision_type="runtime-authorization-admission",
            selected_option="authorized" if judgment.admitted else "rejected",
            authorized_by=[f"policy:{self.policy.policy_ref}"],
            metadata={
                "policy_ref": self.policy.policy_ref,
                "domain_effect_intent_evidence_ref": evidence.id,
                "responsibility_ref": context.responsibility.id,
                "proposal_ref": context.proposal.id,
                "capability": context.intent.capability,
                "actor_ref": context.actor_ref,
                "resource_ref": context.resource_ref,
                "subject_version_ref": context.subject_version_ref,
                "reason": judgment.reason,
                "authority_bearing": False,
            },
        )

    def _grant(
        self,
        context: DomainEffectAuthorizationContext,
        evidence: Evidence,
        decision: Decision,
        judgment: DomainEffectAuthorizationJudgment,
        *,
        decided_at: datetime,
    ) -> AuthorizationGrant | None:
        if not judgment.admitted:
            return None
        return AuthorizationGrant(
            id=_stable_id("authz_domain_effect", decision.id),
            created_at=decided_at,
            principal_ref=f"policy:{self.policy.policy_ref}",
            grantee_ref=context.actor_ref,
            allowed_capabilities=[context.intent.capability],
            resource_scope=[context.resource_ref],
            effect_ceiling=context.contract.minimum_impact_class,
            valid_from=decided_at,
            expires_at=decided_at + timedelta(seconds=max(1, judgment.ttl_seconds)),
            typed_conditions=[
                TypedCondition(
                    kind="domain-business-authority-evidence",
                    params={"evidence_ref": evidence.id},
                    satisfied=True,
                    authority_ref=evidence.id,
                )
            ],
            revocable=True,
            source_decision_ref=decision.id,
            subject_version_refs=[context.subject_version_ref],
            metadata={
                "policy_ref": self.policy.policy_ref,
                "domain_effect_intent_evidence_ref": evidence.id,
                "responsibility_ref": context.responsibility.id,
                "proposal_ref": context.proposal.id,
                "work_ref": context.work.id,
                "authority_bearing": True,
            },
        )

    def _replay_result(
        self,
        context: DomainEffectAuthorizationContext,
        evidence: Evidence,
        decision: Decision,
    ) -> DomainEffectAuthorizationResult:
        existing_evidence = self.store.get_evidence(evidence.id)
        if existing_evidence != evidence:
            raise ValueError("domain effect intent evidence identity rebound")
        if (
            decision.work_id != context.work.id
            or decision.metadata.get("policy_ref") != self.policy.policy_ref
        ):
            raise ValueError("runtime authorization decision identity rebound")
        if decision.metadata.get("domain_effect_intent_evidence_ref") != evidence.id:
            raise ValueError(
                "runtime authorization decision does not bind current evidence"
            )

        authorization_ref = _stable_id("authz_domain_effect", decision.id)
        grant = self.store.get_authorization(authorization_ref)
        if decision.selected_option == "authorized":
            if grant is None:
                raise ValueError(
                    "authorized runtime decision is missing its AuthorizationGrant"
                )
            status: Literal["authorized", "rejected"] = "authorized"
        elif decision.selected_option == "rejected":
            if grant is not None:
                raise ValueError(
                    "rejected runtime decision unexpectedly has an AuthorizationGrant"
                )
            status = "rejected"
        else:
            raise ValueError(
                "runtime authorization decision has an invalid selected option"
            )

        return DomainEffectAuthorizationResult(
            status=status,
            evidence_ref=evidence.id,
            decision_ref=decision.id,
            authorization_ref=grant.id if grant is not None else None,
            policy_ref=self.policy.policy_ref,
            actor_ref=context.actor_ref,
            resource_ref=context.resource_ref,
            subject_version_ref=context.subject_version_ref,
            reason=str(decision.metadata.get("reason") or ""),
            authority_bearing=False,
        )


__all__ = [
    "ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE",
    "DOMAIN_EFFECT_INTENT_EVIDENCE_SCHEMA",
    "DomainEffectAuthorizationAdmission",
    "DomainEffectAuthorizationContext",
    "DomainEffectAuthorizationJudgment",
    "DomainEffectAuthorizationPolicy",
    "DomainEffectAuthorizationResult",
    "DomainEffectIntentEvidenceInput",
    "REFERENCE_AUTHORIZATION_POLICY_REF",
    "RUNTIME_ADMINISTRATIVE_ACTOR",
    "ReferenceAdministrativeEffectAuthorizationPolicy",
]