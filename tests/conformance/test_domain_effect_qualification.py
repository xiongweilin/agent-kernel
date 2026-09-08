from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from portable_runtime.core.qualification import AssessmentContext
from portable_runtime.responsibility.admission import admit_responsibility_proposal
from portable_runtime.responsibility.admission_profiles import (
    administrative_public_responsibility_admission_policy,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_activation import (
    DomainEffectRunActivation,
    DomainEffectRunActivationInput,
)
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
    DomainEffectAuthorizationAdmission,
    DomainEffectIntentEvidenceInput,
)
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
    DomainEffectAuthorizationUseInput,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DOMAIN_EFFECT_QUALIFICATION_EVENT,
    DOMAIN_EFFECT_QUALIFICATION_SCHEMA,
    DomainEffectQualificationAssessment,
    DomainEffectQualificationInput,
)
from portable_runtime.responsibility.domain_effect_request import (
    DomainEffectExecutionRequestPreparation,
    DomainEffectExecutionRequestPreparationInput,
)
from portable_runtime.responsibility.domain_effect_run import (
    DomainEffectRunPreparation,
    DomainEffectRunPreparationInput,
)
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    ResponsibilityRevision,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel
from portable_runtime.stores.memory import InMemoryStateStore

# Keep one stable clock per test process while avoiding a wall-clock-expiring fixture.
NOW = datetime.now(UTC).replace(microsecond=0)
RESP_REF = "resp-admin-qualification-1"
ASSESSMENT_REF = "assessment-admin-qualification-1"
PROPOSAL_REF = "proposal-admin-qualification-1"
INTENT_REF = "intent-qualification-1"
DOMAIN_GRANT_REF = "grant-qualification-1"
GOVERNANCE_REF = "governance-qualification-1"
APPROVAL_REF = "approval-qualification-1"
SUBJECT_REF = "employee:new"
CASE_REF = "case-qualification-1"
EPOCH = 8
OWNER = "runtime-worker:qualification-1"


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
                "obligation_id": "obligation-qualification-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": DOMAIN_GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-qualification-1",
            created_at=NOW,
            responsibility_ref=RESP_REF,
            responsibility_version=1,
            principal_ref="service:administrative-orchestrator",
            basis_refs=[
                f"administrative-grant:{DOMAIN_GRANT_REF}",
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
                f"administrative-grant:{DOMAIN_GRANT_REF}",
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
        policy=administrative_public_responsibility_admission_policy(),
        now=NOW,
    )
    assert admitted.status == "work-materialized"
    assert admitted.work_ref is not None
    work = store.get_work(admitted.work_ref)
    assert work is not None
    return work


def _intent(work_ref: str) -> DomainEffectIntentEvidenceInput:
    return DomainEffectIntentEvidenceInput(
        work_ref=work_ref,
        domain_intent_ref=INTENT_REF,
        domain_grant_ref=DOMAIN_GRANT_REF,
        governance_basis_ref=GOVERNANCE_REF,
        approval_satisfaction_ref=APPROVAL_REF,
        capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        subject_ref=SUBJECT_REF,
        authority_epoch=EPOCH,
        parameters={
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
            "manager_principal_id": "person:manager",
        },
        expected_postcondition={
            "target_system": "hris",
            "operation": "employee.create",
            "subject_ref": SUBJECT_REF,
            "active": True,
        },
        observed_at=NOW,
    )


def _activated(store: InMemoryStateStore):
    work = _admitted_work(store)
    authorization = DomainEffectAuthorizationAdmission(store).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )
    assert authorization.status == "authorized"
    assert authorization.authorization_ref is not None
    run = DomainEffectRunPreparation(store).prepare(
        DomainEffectRunPreparationInput(
            authorization_ref=authorization.authorization_ref,
        ),
        prepared_at=NOW + timedelta(minutes=2),
    )
    request = DomainEffectExecutionRequestPreparation(store).prepare(
        DomainEffectExecutionRequestPreparationInput(run_ref=run.run_ref),
        prepared_at=NOW + timedelta(minutes=3),
    )
    activation = DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run.run_ref),
        owner=OWNER,
        ttl_seconds=300,
        activated_at=NOW + timedelta(minutes=4),
    )
    return work, authorization, run, request, activation


def test_qualification_input_exposes_only_run_identity() -> None:
    with pytest.raises(ValidationError):
        DomainEffectQualificationInput.model_validate(
            {
                "run_ref": "run-1",
                "authorization_refs": ["caller:grant"],
            }
        )


def test_qualification_resolves_authoritative_refs_into_immutable_digest() -> None:
    store = InMemoryStateStore()
    work, authorization, run_result, request_result, activation = _activated(store)

    result = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=5),
    )

    event = store.get_event(result.qualification_event_ref)
    run = store.get_run(run_result.run_ref)
    assert event is not None and run is not None
    assert result.status == "qualified"
    assert result.authority_bearing is False
    assert result.work_ref == work.id
    assert result.request_event_ref == request_result.event_ref
    assert result.lease_owner == activation.lease_owner
    assert result.lease_generation == activation.lease_generation
    assert result.request.lease_owner == activation.lease_owner
    assert result.request.lease_generation == activation.lease_generation
    assert result.request.preferred_provider_ids == []
    assert result.request.excluded_provider_ids == []
    assert set(result.request.metadata) == {
        "authorization_refs",
        "evidence_refs",
        "decision_refs",
    }
    resolved = {(ref.ref_id, ref.kind) for ref in result.qualification_refs}
    assert (authorization.authorization_ref, "authorization") in resolved
    assert (authorization.evidence_ref, "evidence") in resolved
    assert (authorization.decision_ref, "decision") in resolved
    assert len(resolved) == 3
    assert len(result.qualification_digest) == 64
    assert event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
    assert event.payload["schema"] == DOMAIN_EFFECT_QUALIFICATION_SCHEMA
    assert event.payload["authority_bearing"] is False
    assert event.payload["qualification_digest"] == result.qualification_digest
    assert store.list_authorization_uses() == []

    fresh = AssessmentContext.resolve(store, result.request, work=work, run=run)
    assert fresh.digest == result.qualification_digest
    assert fresh.has_authorization_refs


def test_qualification_replay_preserves_same_snapshot_for_same_fencing_generation() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request, _activation = _activated(store)
    assessment = DomainEffectQualificationAssessment(store)
    command = DomainEffectQualificationInput(run_ref=run_result.run_ref)

    first = assessment.assess(command, assessed_at=NOW + timedelta(minutes=5))
    replay = assessment.assess(command, assessed_at=NOW + timedelta(minutes=6))

    assert replay == first
    events = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
    ]
    assert len(events) == 1
    assert events[0].created_at == NOW + timedelta(minutes=5)


def test_authorization_use_before_first_qualification_is_rejected() -> None:
    store = InMemoryStateStore()
    _work, authorization, run_result, _request, _activation = _activated(store)
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="consumed before qualification closure"):
        DomainEffectQualificationAssessment(store).assess(
            DomainEffectQualificationInput(run_ref=run_result.run_ref),
            assessed_at=NOW + timedelta(minutes=6),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
    ] == []


def test_responsibility_revision_blocks_first_qualification() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request, _activation = _activated(store)
    kernel = ResponsibilityKernel(store)
    _version, statement, scope = kernel.current_definition(RESP_REF)
    kernel.revise(
        ResponsibilityRevision(
            id="revision-admin-qualification-2",
            created_at=NOW + timedelta(minutes=5),
            responsibility_ref=RESP_REF,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "authority_epoch": "9"},
            basis_refs=["administrative-authority-epoch:9"],
            reason="administrative authority changed before qualification",
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectQualificationAssessment(store).assess(
            DomainEffectQualificationInput(run_ref=run_result.run_ref),
            assessed_at=NOW + timedelta(minutes=6),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
    ] == []
    assert store.list_authorization_uses() == []


def test_qualification_request_contains_refs_not_inline_proof_objects() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request, _activation = _activated(store)
    result = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=5),
    )

    assert "grants" not in result.request.metadata
    assert "evidences" not in result.request.metadata
    assert "decisions" not in result.request.metadata
    assert "procedure_proofs" not in result.request.metadata
