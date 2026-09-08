from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from portable_runtime.responsibility.admission import admit_responsibility_proposal
from portable_runtime.responsibility.admission_profiles import (
    administrative_public_responsibility_admission_policy,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_activation import (
    DOMAIN_EFFECT_ACTIVATION_EVENT,
    DOMAIN_EFFECT_ACTIVATION_SCHEMA,
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
from portable_runtime.responsibility.domain_effect_request import (
    DOMAIN_EFFECT_REQUEST_EVENT,
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

NOW = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)
CASE_REF = "case-activation-1"
RESP_REF = "resp-admin-activation-1"
ASSESSMENT_REF = "assessment-admin-activation-1"
PROPOSAL_REF = "proposal-admin-activation-1"
INTENT_REF = "intent-activation-1"
DOMAIN_GRANT_REF = "grant-activation-1"
GOVERNANCE_REF = "governance-activation-1"
APPROVAL_REF = "approval-activation-1"
SUBJECT_REF = "employee:new"
EPOCH = 6
OWNER = "runtime-worker:admin-1"


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
                "obligation_id": "obligation-activation-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": DOMAIN_GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-activation-1",
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


def _prepared(store: InMemoryStateStore):
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
    return work, authorization, run, request


def test_activation_input_does_not_allow_caller_fencing_fields() -> None:
    with pytest.raises(ValidationError):
        DomainEffectRunActivationInput.model_validate(
            {
                "run_ref": "run-1",
                "lease_owner": "domain:caller",
                "lease_generation": 99,
            }
        )


def test_activation_requires_prepared_request() -> None:
    store = InMemoryStateStore()
    work = _admitted_work(store)
    authorization = DomainEffectAuthorizationAdmission(store).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )
    assert authorization.authorization_ref is not None
    run = DomainEffectRunPreparation(store).prepare(
        DomainEffectRunPreparationInput(authorization_ref=authorization.authorization_ref),
        prepared_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(ValueError, match="exactly one prepared request"):
        DomainEffectRunActivation(store).activate(
            DomainEffectRunActivationInput(run_ref=run.run_ref),
            owner=OWNER,
            activated_at=NOW + timedelta(minutes=3),
        )

    current = store.get_run(run.run_ref)
    assert current is not None
    assert current.status == "queued"
    assert current.lease_generation == 0


def test_activation_acquires_fencing_and_starts_only_the_run() -> None:
    store = InMemoryStateStore()
    work, _authorization, run_result, request = _prepared(store)

    result = DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner=OWNER,
        ttl_seconds=30,
        activated_at=NOW + timedelta(minutes=4),
    )

    run = store.get_run(run_result.run_ref)
    current_work = store.get_work(work.id)
    event = store.get_event(result.activation_event_ref)
    assert run is not None and current_work is not None and event is not None
    assert result.status == "running"
    assert result.authority_bearing is False
    assert result.request_event_ref == request.event_ref
    assert result.lease_owner == OWNER
    assert result.lease_generation == 1
    assert run.status == "running"
    assert run.started_at == NOW + timedelta(minutes=4)
    assert run.lease_owner == OWNER
    assert run.lease_generation == result.lease_generation
    assert run.lease_expires_at is not None
    assert run.provider_invocation_refs == []
    assert current_work.status == "open"
    assert event.type == DOMAIN_EFFECT_ACTIVATION_EVENT
    assert event.payload["schema"] == DOMAIN_EFFECT_ACTIVATION_SCHEMA
    assert event.payload["authority_bearing"] is False
    assert event.payload["request_event_ref"] == request.event_ref
    assert store.list_authorization_uses() == []


def test_same_owner_activation_replay_does_not_increment_generation() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request = _prepared(store)
    activation = DomainEffectRunActivation(store)
    command = DomainEffectRunActivationInput(run_ref=run_result.run_ref)

    first = activation.activate(
        command,
        owner=OWNER,
        activated_at=NOW + timedelta(minutes=4),
    )
    replay = activation.activate(
        command,
        owner=OWNER,
        activated_at=NOW + timedelta(minutes=5),
    )

    assert replay == first
    current = store.get_run(run_result.run_ref)
    assert current is not None
    assert current.lease_generation == first.lease_generation == 1
    events = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_ACTIVATION_EVENT
    ]
    assert len(events) == 1


def test_competing_owner_is_fenced_out() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request = _prepared(store)
    activation = DomainEffectRunActivation(store)
    activation.activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner=OWNER,
        activated_at=NOW + timedelta(minutes=4),
    )

    with pytest.raises(ValueError, match="held by another owner"):
        activation.activate(
            DomainEffectRunActivationInput(run_ref=run_result.run_ref),
            owner="runtime-worker:admin-2",
            activated_at=NOW + timedelta(minutes=5),
        )

    current = store.get_run(run_result.run_ref)
    assert current is not None
    assert current.lease_owner == OWNER
    assert current.lease_generation == 1


def test_preacquired_same_owner_lease_is_reused_after_crash_window() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request = _prepared(store)
    assert store.acquire_lease(run_result.run_ref, OWNER, ttl_seconds=30) is True
    leased = store.get_run(run_result.run_ref)
    assert leased is not None
    generation = leased.lease_generation
    assert generation == 1
    assert leased.status == "queued"

    result = DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner=OWNER,
        activated_at=NOW + timedelta(minutes=4),
    )

    assert result.lease_generation == generation
    current = store.get_run(run_result.run_ref)
    assert current is not None
    assert current.status == "running"
    assert current.lease_generation == generation


def test_authorization_use_before_activation_is_rejected() -> None:
    store = InMemoryStateStore()
    _work, authorization, run_result, _request = _prepared(store)
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=4),
    )

    with pytest.raises(ValueError, match="consumed before Run activation"):
        DomainEffectRunActivation(store).activate(
            DomainEffectRunActivationInput(run_ref=run_result.run_ref),
            owner=OWNER,
            activated_at=NOW + timedelta(minutes=5),
        )

    current = store.get_run(run_result.run_ref)
    assert current is not None
    assert current.status == "queued"
    assert current.lease_generation == 0


def test_responsibility_revision_blocks_first_activation() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request = _prepared(store)
    kernel = ResponsibilityKernel(store)
    _version, statement, scope = kernel.current_definition(RESP_REF)
    kernel.revise(
        ResponsibilityRevision(
            id="revision-admin-activation-2",
            created_at=NOW + timedelta(minutes=4),
            responsibility_ref=RESP_REF,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "authority_epoch": "7"},
            basis_refs=["administrative-authority-epoch:7"],
            reason="administrative authority changed before Run activation",
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectRunActivation(store).activate(
            DomainEffectRunActivationInput(run_ref=run_result.run_ref),
            owner=OWNER,
            activated_at=NOW + timedelta(minutes=5),
        )

    current = store.get_run(run_result.run_ref)
    assert current is not None
    assert current.status == "queued"
    assert current.lease_generation == 0
    assert store.list_authorization_uses() == []
    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_ACTIVATION_EVENT
    ] == []


def test_request_event_remains_single_after_activation() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request = _prepared(store)
    DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner=OWNER,
        activated_at=NOW + timedelta(minutes=4),
    )

    requests = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_REQUEST_EVENT
    ]
    assert len(requests) == 1
