from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from portable_runtime.responsibility.admission import admit_responsibility_proposal
from portable_runtime.responsibility.admission_profiles import (
    administrative_public_responsibility_admission_policy,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
    DomainEffectAuthorizationAdmission,
    DomainEffectIntentEvidenceInput,
)
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
    DomainEffectAuthorizationUseInput,
)
from portable_runtime.responsibility.domain_effect_request import (
    DOMAIN_EFFECT_REQUEST_EVENT,
    DOMAIN_EFFECT_REQUEST_SCHEMA,
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

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
CASE_REF = "case-request-1"
RESP_REF = "resp-admin-request-1"
ASSESSMENT_REF = "assessment-admin-request-1"
PROPOSAL_REF = "proposal-admin-request-1"
INTENT_REF = "intent-request-1"
DOMAIN_GRANT_REF = "grant-request-1"
GOVERNANCE_REF = "governance-request-1"
APPROVAL_REF = "approval-request-1"
SUBJECT_REF = "employee:new"
EPOCH = 4


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
                "obligation_id": "obligation-request-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": DOMAIN_GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-request-1",
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


def _authorized_run(store: InMemoryStateStore):
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
    return work, authorization, run


def test_request_preparation_input_exposes_only_run_identity() -> None:
    with pytest.raises(ValidationError):
        DomainEffectExecutionRequestPreparationInput.model_validate(
            {
                "run_ref": "run-1",
                "actor_ref": "person:caller-selected",
            }
        )


def test_request_preparation_persists_provider_agnostic_canonical_request() -> None:
    store = InMemoryStateStore()
    work, authorization, run_result = _authorized_run(store)

    result = DomainEffectExecutionRequestPreparation(store).prepare(
        DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
        prepared_at=NOW + timedelta(minutes=3),
    )

    run = store.get_run(run_result.run_ref)
    event = store.get_event(result.event_ref)
    assert run is not None and event is not None
    assert result.status == "prepared"
    assert result.authority_bearing is False
    assert result.work_ref == work.id
    assert result.authorization_ref == authorization.authorization_ref
    assert result.request.work_id == work.id
    assert result.request.run_id == run.id
    assert result.request.capability == ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
    assert result.request.actor_ref == "runtime:administrative-effect-executor"
    assert result.request.resource_ref
    assert len(result.request.subject_version_refs) == 1
    assert result.request.effect_class == "write-remote"
    assert result.request.parameters["department_ref"] == "department:engineering"
    assert result.request.preferred_provider_ids == []
    assert result.request.excluded_provider_ids == []
    assert result.request.metadata == {}
    assert result.request.lease_generation == 0
    assert result.request.lease_owner is None
    assert result.request.idempotency_key == result.logical_effect_ref
    assert event.type == DOMAIN_EFFECT_REQUEST_EVENT
    assert event.payload["schema"] == DOMAIN_EFFECT_REQUEST_SCHEMA
    assert event.payload["authority_bearing"] is False
    assert event.subject_ref == run.id
    assert run.status == "queued"
    assert run.started_at is None
    assert run.provider_invocation_refs == []
    assert store.list_authorization_uses() == []


def test_request_preparation_replays_same_history_after_later_authorization_use() -> None:
    store = InMemoryStateStore()
    _work, authorization, run_result = _authorized_run(store)
    preparation = DomainEffectExecutionRequestPreparation(store)
    command = DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref)

    first = preparation.prepare(
        command,
        prepared_at=NOW + timedelta(minutes=3),
    )
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=4),
    )
    replay = preparation.prepare(
        command,
        prepared_at=NOW + timedelta(minutes=25),
    )

    assert replay == first
    events = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_REQUEST_EVENT
    ]
    assert len(events) == 1
    assert events[0].created_at == NOW + timedelta(minutes=3)


def test_expired_grant_cannot_prepare_first_execution_request() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result = _authorized_run(store)

    with pytest.raises(ValueError, match="not current for request preparation"):
        DomainEffectExecutionRequestPreparation(store).prepare(
            DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
            prepared_at=NOW + timedelta(minutes=20),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_REQUEST_EVENT
    ] == []
    assert store.list_authorization_uses() == []


def test_authorization_use_before_first_request_preparation_is_rejected() -> None:
    store = InMemoryStateStore()
    _work, authorization, run_result = _authorized_run(store)
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=3),
    )

    with pytest.raises(ValueError, match="consumed before canonical request preparation"):
        DomainEffectExecutionRequestPreparation(store).prepare(
            DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
            prepared_at=NOW + timedelta(minutes=4),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_REQUEST_EVENT
    ] == []


def test_responsibility_revision_blocks_first_request_preparation() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result = _authorized_run(store)
    kernel = ResponsibilityKernel(store)
    _version, statement, scope = kernel.current_definition(RESP_REF)
    kernel.revise(
        ResponsibilityRevision(
            id="revision-admin-request-2",
            created_at=NOW + timedelta(minutes=3),
            responsibility_ref=RESP_REF,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "authority_epoch": "5"},
            basis_refs=["administrative-authority-epoch:5"],
            reason="administrative authority changed before request preparation",
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectExecutionRequestPreparation(store).prepare(
            DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
            prepared_at=NOW + timedelta(minutes=4),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_REQUEST_EVENT
    ] == []
    assert store.list_authorization_uses() == []
