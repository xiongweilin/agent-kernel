from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.responsibility.admission import (
    BoundedLocalResponsibilityAdmissionPolicy,
    admit_responsibility_proposal,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
    DomainEffectAuthorizationAdmission,
    DomainEffectAuthorizationContext,
    DomainEffectAuthorizationJudgment,
    DomainEffectIntentEvidenceInput,
    REFERENCE_AUTHORIZATION_POLICY_REF,
    RUNTIME_ADMINISTRATIVE_ACTOR,
)
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel
from portable_runtime.stores.memory import InMemoryStateStore

NOW = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)
CASE_REF = "case-1"
RESPONSIBILITY_REF = "resp-admin-1"
ADMISSION_REF = "resp-admission-admin-1"
ASSESSMENT_REF = "assessment-admin-1"
PROPOSAL_REF = "proposal-admin-1"
DOMAIN_INTENT_REF = "intent-1"
DOMAIN_GRANT_REF = "grant-1"
GOVERNANCE_BASIS_REF = "governance-1"
APPROVAL_SATISFACTION_REF = "approval-1"
SUBJECT_REF = "employee:new"
AUTHORITY_EPOCH = 2


def _admitted_work(store: InMemoryStateStore):
    kernel = ResponsibilityKernel(store)
    responsibility = StandingResponsibility(
        id=RESPONSIBILITY_REF,
        created_at=NOW,
        responsibility_kind="administrative-obligation",
        statement="Discharge governed onboarding employee creation",
        scope={
            "administrative_case_id": CASE_REF,
            "authority_epoch": str(AUTHORITY_EPOCH),
            "obligation_id": "obligation-1",
            "governance_basis_id": GOVERNANCE_BASIS_REF,
            "execution_grant_id": DOMAIN_GRANT_REF,
            "target_system": "hris",
            "operation": "employee.create",
            "policy_ref": "employee-onboarding:v1",
        },
    )
    admission = ResponsibilityAdmission(
        id=ADMISSION_REF,
        created_at=NOW,
        responsibility_ref=RESPONSIBILITY_REF,
        responsibility_version=1,
        principal_ref="service:administrative-orchestrator",
        basis_refs=[
            f"administrative-grant:{DOMAIN_GRANT_REF}",
            f"governance-basis:{GOVERNANCE_BASIS_REF}",
            f"approval-satisfaction:{APPROVAL_SATISFACTION_REF}",
        ],
        admitted_at=NOW,
    )
    kernel.register(responsibility, admission)

    assessment = ResponsibilityAssessment(
        id=ASSESSMENT_REF,
        created_at=NOW,
        responsibility_ref=RESPONSIBILITY_REF,
        responsibility_version=1,
        subject_ref=SUBJECT_REF,
        assessment_kind="administrative-obligation-ready",
        basis_refs=[
            f"administrative-intent:{DOMAIN_INTENT_REF}",
            f"administrative-grant:{DOMAIN_GRANT_REF}",
            f"governance-basis:{GOVERNANCE_BASIS_REF}",
        ],
        assessed_at=NOW,
        fresh_until=NOW + timedelta(minutes=30),
        rationale="domain policy and approvals are closed",
    )
    record_domain_assessment(kernel, assessment, now=NOW)

    proposal = WorkProposal(
        id=PROPOSAL_REF,
        created_at=NOW,
        responsibility_ref=RESPONSIBILITY_REF,
        responsibility_version=1,
        assessment_ref=ASSESSMENT_REF,
        subject_ref=SUBJECT_REF,
        work_kind="administrative-effect",
        title="hris: employee.create employee:new",
        description="Execute one governed administrative effect intent",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        requested_capabilities=[ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE],
        expected_result="employee exists with governed onboarding fields",
        effect_class=EffectClass.EXTERNAL_EFFECT,
        fresh_until=NOW + timedelta(minutes=30),
    )
    kernel.propose(proposal, now=NOW)
    admitted = admit_responsibility_proposal(
        kernel,
        proposal.id,
        policy=BoundedLocalResponsibilityAdmissionPolicy(),
        now=NOW,
    )
    assert admitted.status == "work-materialized"
    assert admitted.work_ref is not None
    work = store.get_work(admitted.work_ref)
    assert work is not None
    return work


def _intent(work_ref: str, **updates: object) -> DomainEffectIntentEvidenceInput:
    payload: dict[str, object] = {
        "work_ref": work_ref,
        "domain_intent_ref": DOMAIN_INTENT_REF,
        "domain_grant_ref": DOMAIN_GRANT_REF,
        "governance_basis_ref": GOVERNANCE_BASIS_REF,
        "approval_satisfaction_ref": APPROVAL_SATISFACTION_REF,
        "capability": ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        "subject_ref": SUBJECT_REF,
        "authority_epoch": AUTHORITY_EPOCH,
        "parameters": {
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
            "manager_principal_id": "person:manager",
            "start_date": "2026-09-15",
            "employment_type": "full-time",
        },
        "expected_postcondition": {
            "target_system": "hris",
            "operation": "employee.create",
            "subject_ref": SUBJECT_REF,
            "active": True,
        },
        "observed_at": NOW,
    }
    payload.update(updates)
    return DomainEffectIntentEvidenceInput.model_validate(payload)


def test_first_administrative_capability_has_strict_kernel_effect_contract() -> None:
    contract = CapabilityContractRegistry().resolve(ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE)

    assert contract.minimum_impact_class == "write-remote"
    assert contract.effect_semantics == "reconcilable"
    assert contract.reversibility == "compensatable"
    assert contract.authorization_requirement == "required"
    assert contract.minimum_procedure_profile == "standard"
    assert contract.resource_required is True
    assert contract.subject_version_required is True
    assert contract.blast_radius == 1
    assert contract.exposure == 1


def test_kernel_admits_domain_evidence_then_mints_narrow_runtime_grant() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    admission = DomainEffectAuthorizationAdmission(store)

    result = admission.admit(_intent(work.id), now=NOW + timedelta(minutes=1))

    assert result.status == "authorized"
    assert result.policy_ref == REFERENCE_AUTHORIZATION_POLICY_REF
    assert result.actor_ref == RUNTIME_ADMINISTRATIVE_ACTOR
    assert result.resource_ref == "administrative:hris:employee:new"
    assert result.subject_version_ref == "administrative-authority-epoch:case-1:2"
    assert result.authority_bearing is False
    assert result.authorization_ref is not None

    evidence = store.get_evidence(result.evidence_ref)
    assert evidence is not None
    assert evidence.kind == "domain-effect-intent"
    assert evidence.status == "supported"
    assert evidence.metadata["authority_bearing"] is False
    assert evidence.metadata["parameters"]["department_ref"] == "department:engineering"

    decision = store.get_decision(result.decision_ref)
    assert decision is not None
    assert decision.selected_option == "authorized"
    assert decision.decision_type == "runtime-authorization-admission"
    assert decision.metadata["domain_effect_intent_evidence_ref"] == evidence.id

    grant = store.get_authorization(result.authorization_ref)
    assert grant is not None
    assert grant.source_decision_ref == decision.id
    assert grant.principal_ref == f"policy:{REFERENCE_AUTHORIZATION_POLICY_REF}"
    assert grant.grantee_ref == RUNTIME_ADMINISTRATIVE_ACTOR
    assert grant.allowed_capabilities == [ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE]
    assert grant.resource_scope == [result.resource_ref]
    assert grant.subject_version_refs == [result.subject_version_ref]
    assert grant.effect_ceiling == "write-remote"
    assert grant.typed_conditions[0].authority_ref == evidence.id

    assert store.list_runs() == []
    assert store.list_authorization_uses() == []


def test_minted_grant_covers_only_the_kernel_derived_strict_request() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    result = DomainEffectAuthorizationAdmission(store).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )
    assert result.authorization_ref is not None
    boundary = RealityBoundary(store=store, contract_registry=CapabilityContractRegistry())

    request = CapabilityRequest(
        id="request-admin-1",
        capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        work_id=work.id,
        actor_ref=result.actor_ref,
        resource_ref=result.resource_ref,
        subject_version_refs=[result.subject_version_ref],
        effect_class="write-remote",
        parameters=dict(_intent(work.id).parameters),
    )
    allowed, reason = boundary.check_authorization(request)
    assert allowed is True
    assert reason == "authorized"

    for update in (
        {"actor_ref": "service:administrative-orchestrator"},
        {"resource_ref": "administrative:hris:employee:other"},
        {"subject_version_refs": ["administrative-authority-epoch:case-1:3"]},
        {"effect_class": "admin"},
    ):
        changed = request.model_copy(update=update)
        denied, _ = boundary.check_authorization(changed)
        assert denied is False


class RejectAllPolicy:
    policy_ref = "kernel-administrative-runtime-authorization-deny-test-v1"

    def evaluate(
        self,
        context: DomainEffectAuthorizationContext,
    ) -> DomainEffectAuthorizationJudgment:
        assert context.intent.capability == ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
        return DomainEffectAuthorizationJudgment(False, "test policy rejected effect")


def test_kernel_policy_rejection_records_decision_but_mints_no_authority() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    result = DomainEffectAuthorizationAdmission(store, policy=RejectAllPolicy()).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )

    assert result.status == "rejected"
    assert result.authorization_ref is None
    assert store.get_evidence(result.evidence_ref) is not None
    decision = store.get_decision(result.decision_ref)
    assert decision is not None
    assert decision.selected_option == "rejected"
    assert store.list_authorizations() == []
    assert store.list_authorization_uses() == []
    assert store.list_runs() == []


def test_replay_converges_on_same_evidence_decision_and_grant() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    admission = DomainEffectAuthorizationAdmission(store)

    first = admission.admit(_intent(work.id), now=NOW + timedelta(minutes=1))
    second = admission.admit(_intent(work.id), now=NOW + timedelta(minutes=10))

    assert first == second
    assert len(store.list_evidence(work.id)) == 1
    assert len(store.list_authorizations()) == 1
    assert store.list_authorization_uses() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("authority_epoch", 3, "stale administrative authority epoch"),
        ("domain_grant_ref", "grant-other", "execution grant"),
        ("governance_basis_ref", "governance-other", "governance basis"),
        ("approval_satisfaction_ref", "approval-other", "authority provenance"),
    ],
)
def test_lineage_mismatch_fails_before_evidence_or_runtime_authority(
    field: str,
    value: object,
    message: str,
) -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    admission = DomainEffectAuthorizationAdmission(store)

    with pytest.raises(ValueError, match=message):
        admission.admit(_intent(work.id, **{field: value}), now=NOW + timedelta(minutes=1))

    assert store.list_evidence(work.id) == []
    assert store.list_authorizations() == []
    assert store.list_authorization_uses() == []
    assert store.list_runs() == []
