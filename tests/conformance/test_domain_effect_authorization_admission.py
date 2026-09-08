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
RESP_REF = "resp-admin-1"
ASSESSMENT_REF = "assessment-admin-1"
PROPOSAL_REF = "proposal-admin-1"
INTENT_REF = "intent-1"
GRANT_REF = "grant-1"
GOVERNANCE_REF = "governance-1"
APPROVAL_REF = "approval-1"
SUBJECT_REF = "employee:new"
EPOCH = 2


def _work_policy() -> BoundedLocalResponsibilityAdmissionPolicy:
    return BoundedLocalResponsibilityAdmissionPolicy(
        profile_id="administrative-public",
        version="1",
        max_request=ResourceVector(
            api_calls=2,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        capacity=ResourceVector(
            api_calls=8,
            concurrency_slots=4,
            domain_quota={"administrative:hris": 4},
        ),
    )


def _admitted_work(store: InMemoryStateStore):
    kernel = ResponsibilityKernel(store)
    kernel.register(
        StandingResponsibility(
            id=RESP_REF,
            created_at=NOW,
            responsibility_kind="administrative-obligation",
            statement="Discharge governed onboarding employee creation",
            scope={
                "administrative_case_id": CASE_REF,
                "authority_epoch": str(EPOCH),
                "obligation_id": "obligation-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-1",
            created_at=NOW,
            responsibility_ref=RESP_REF,
            responsibility_version=1,
            principal_ref="service:administrative-orchestrator",
            basis_refs=[
                f"administrative-grant:{GRANT_REF}",
                f"governance-basis:{GOVERNANCE_REF}",
                f"approval-satisfaction:{APPROVAL_REF}",
            ],
            admitted_at=NOW,
        ),
    )
    record_domain_assessment(
        kernel,
        ResponsibilityAssessment(
            id=ASSESSMENT_REF,
            created_at=NOW,
            responsibility_ref=RESP_REF,
            responsibility_version=1,
            subject_ref=SUBJECT_REF,
            assessment_kind="administrative-obligation-ready",
            basis_refs=[
                f"administrative-intent:{INTENT_REF}",
                f"administrative-grant:{GRANT_REF}",
                f"governance-basis:{GOVERNANCE_REF}",
            ],
            assessed_at=NOW,
            fresh_until=NOW + timedelta(minutes=30),
            rationale="domain policy and approvals are closed",
        ),
        now=NOW,
    )
    proposal = WorkProposal(
        id=PROPOSAL_REF,
        created_at=NOW,
        responsibility_ref=RESP_REF,
        responsibility_version=1,
        assessment_ref=ASSESSMENT_REF,
        subject_ref=SUBJECT_REF,
        work_kind="administrative-effect",
        title="hris: employee.create employee:new",
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
        policy=_work_policy(),
        now=NOW,
    )
    assert admitted.status == "work-materialized"
    assert admitted.work_ref is not None
    work = store.get_work(admitted.work_ref)
    assert work is not None
    return work


def _intent(work_ref: str, **updates: object) -> DomainEffectIntentEvidenceInput:
    raw: dict[str, object] = {
        "work_ref": work_ref,
        "domain_intent_ref": INTENT_REF,
        "domain_grant_ref": GRANT_REF,
        "governance_basis_ref": GOVERNANCE_REF,
        "approval_satisfaction_ref": APPROVAL_REF,
        "capability": ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        "subject_ref": SUBJECT_REF,
        "authority_epoch": EPOCH,
        "parameters": {
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
            "manager_principal_id": "person:manager",
        },
        "expected_postcondition": {
            "target_system": "hris",
            "operation": "employee.create",
            "subject_ref": SUBJECT_REF,
            "active": True,
        },
        "observed_at": NOW,
    }
    raw.update(updates)
    return DomainEffectIntentEvidenceInput.model_validate(raw)


def test_administrative_employee_create_has_strict_effect_contract() -> None:
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


def test_domain_evidence_mints_only_kernel_derived_runtime_grant() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    result = DomainEffectAuthorizationAdmission(store).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )

    assert result.status == "authorized"
    assert result.policy_ref == REFERENCE_AUTHORIZATION_POLICY_REF
    assert result.actor_ref == RUNTIME_ADMINISTRATIVE_ACTOR
    assert result.resource_ref == "administrative:hris:employee:new"
    assert result.subject_version_ref == "administrative-authority-epoch:case-1:2"
    assert result.authority_bearing is False
    assert result.authorization_ref is not None

    evidence = store.get_evidence(result.evidence_ref)
    decision = store.get_decision(result.decision_ref)
    grant = store.get_authorization(result.authorization_ref)
    assert evidence is not None and decision is not None and grant is not None
    assert evidence.subject_refs == [work.id]
    assert evidence.metadata["authority_bearing"] is False
    assert evidence.metadata["responsibility_ref"] == RESP_REF
    assert evidence.metadata["subject_ref"] == SUBJECT_REF
    assert evidence.metadata["parameters"]["department_ref"] == "department:engineering"
    assert decision.selected_option == "authorized"
    assert grant.source_decision_ref == decision.id
    assert grant.principal_ref == f"policy:{REFERENCE_AUTHORIZATION_POLICY_REF}"
    assert grant.grantee_ref == RUNTIME_ADMINISTRATIVE_ACTOR
    assert grant.allowed_capabilities == [ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE]
    assert grant.resource_scope == [result.resource_ref]
    assert grant.subject_version_refs == [result.subject_version_ref]
    assert grant.effect_ceiling == "write-remote"
    assert grant.typed_conditions[0].authority_ref == evidence.id
    assert store.list_authorization_uses() == []
    assert store.list_runs() == []


def test_runtime_grant_covers_only_exact_kernel_binding() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    result = DomainEffectAuthorizationAdmission(store).admit(_intent(work.id))
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
    assert boundary.check_authorization(request) == (True, "authorized")

    for update in (
        {"actor_ref": "service:administrative-orchestrator"},
        {"resource_ref": "administrative:hris:employee:other"},
        {"subject_version_refs": ["administrative-authority-epoch:case-1:3"]},
        {"effect_class": "admin"},
    ):
        allowed, _reason = boundary.check_authorization(request.model_copy(update=update))
        assert allowed is False


class RejectAllPolicy:
    policy_ref = "kernel-administrative-runtime-authorization-deny-test-v1"

    def evaluate(
        self,
        context: DomainEffectAuthorizationContext,
    ) -> DomainEffectAuthorizationJudgment:
        assert context.intent.capability == ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
        return DomainEffectAuthorizationJudgment(False, "test policy rejected effect")


def test_policy_rejection_records_decision_without_runtime_authority() -> None:
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
    assert decision is not None and decision.selected_option == "rejected"
    assert store.list_authorizations() == []
    assert store.list_authorization_uses() == []
    assert store.list_runs() == []


def test_replay_converges_without_refreshing_authority() -> None:
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
def test_lineage_mismatch_fails_before_runtime_mutation(
    field: str,
    value: object,
    message: str,
) -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    with pytest.raises(ValueError, match=message):
        DomainEffectAuthorizationAdmission(store).admit(
            _intent(work.id, **{field: value}),
            now=NOW + timedelta(minutes=1),
        )
    assert store.list_evidence(work.id) == []
    assert store.list_authorizations() == []
    assert store.list_authorization_uses() == []
    assert store.list_runs() == []
