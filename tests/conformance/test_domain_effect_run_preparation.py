from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from portable_runtime.core.models import Run
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
from portable_runtime.responsibility.domain_effect_run import (
    DOMAIN_EFFECT_RUN_SCHEMA,
    DOMAIN_EFFECT_WORKFLOW_ID,
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

NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
CASE_REF = "case-run-1"
RESP_REF = "resp-admin-run-1"
ASSESSMENT_REF = "assessment-admin-run-1"
PROPOSAL_REF = "proposal-admin-run-1"
INTENT_REF = "intent-run-1"
DOMAIN_GRANT_REF = "grant-run-1"
GOVERNANCE_REF = "governance-run-1"
APPROVAL_REF = "approval-run-1"
SUBJECT_REF = "employee:new"
EPOCH = 2


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
                "obligation_id": "obligation-run-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": DOMAIN_GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-run-1",
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


def _authorized(store: InMemoryStateStore):
    work = _admitted_work(store)
    result = DomainEffectAuthorizationAdmission(store).admit(
        _intent(work.id),
        now=NOW + timedelta(minutes=1),
    )
    assert result.status == "authorized"
    assert result.authorization_ref is not None
    return work, result


def test_run_preparation_input_exposes_only_runtime_authorization_identity() -> None:
    with pytest.raises(ValidationError):
        DomainEffectRunPreparationInput.model_validate(
            {
                "authorization_ref": "authz-1",
                "provider_id": "hris-provider",
            }
        )


def test_run_preparation_creates_one_queued_non_executing_run() -> None:
    store = InMemoryStateStore()
    work, authorization = _authorized(store)

    result = DomainEffectRunPreparation(store).prepare(
        DomainEffectRunPreparationInput(
            authorization_ref=authorization.authorization_ref,
        ),
        prepared_at=NOW + timedelta(minutes=2),
    )

    run = store.get_run(result.run_ref)
    current_work = store.get_work(work.id)
    assert run is not None and current_work is not None
    assert result.status == "prepared"
    assert result.work_ref == work.id
    assert result.authorization_ref == authorization.authorization_ref
    assert result.authority_bearing is False
    assert run.status == "queued"
    assert run.started_at is None
    assert run.workflow_id == DOMAIN_EFFECT_WORKFLOW_ID
    assert run.provider_invocation_refs == []
    assert run.metadata["schema"] == DOMAIN_EFFECT_RUN_SCHEMA
    assert run.metadata["authorization_use_requirement"] == "consume-at-action-boundary"
    assert run.metadata["provider_selection"] == "not-performed"
    assert run.metadata["invocation_permit"] == "not-issued"
    assert current_work.status == "open"
    assert store.list_authorization_uses() == []


def test_run_preparation_replays_same_identity_without_refreshing_run() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    preparation = DomainEffectRunPreparation(store)
    command = DomainEffectRunPreparationInput(
        authorization_ref=authorization.authorization_ref,
    )

    first = preparation.prepare(
        command,
        prepared_at=NOW + timedelta(minutes=2),
    )
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=3),
    )
    replay = preparation.prepare(
        command,
        prepared_at=NOW + timedelta(minutes=20),
    )

    assert replay == first
    assert len(store.list_runs(first.work_ref)) == 1
    run = store.get_run(first.run_ref)
    assert run is not None
    assert run.created_at == NOW + timedelta(minutes=2)


def test_expired_grant_cannot_create_first_canonical_run() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)

    with pytest.raises(ValueError, match="not current for Run preparation"):
        DomainEffectRunPreparation(store).prepare(
            DomainEffectRunPreparationInput(
                authorization_ref=authorization.authorization_ref,
            ),
            prepared_at=NOW + timedelta(minutes=20),
        )

    assert store.list_runs() == []
    assert store.list_authorization_uses() == []


def test_foreign_run_blocks_canonical_domain_effect_run() -> None:
    store = InMemoryStateStore()
    work, authorization = _authorized(store)
    foreign = Run(
        id="run-foreign-admin",
        created_at=NOW + timedelta(minutes=2),
        work_id=work.id,
        status="queued",
        workflow_id="generic-task",
    )
    store.save_run(foreign)

    with pytest.raises(ValueError, match="non-canonical Run"):
        DomainEffectRunPreparation(store).prepare(
            DomainEffectRunPreparationInput(
                authorization_ref=authorization.authorization_ref,
            ),
            prepared_at=NOW + timedelta(minutes=3),
        )

    assert store.list_runs(work.id) == [foreign]
    assert store.list_authorization_uses() == []


def test_authorization_consumed_before_run_preparation_is_rejected() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    DomainEffectAuthorizationUseConsumption(store).consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(ValueError, match="consumed before canonical Run preparation"):
        DomainEffectRunPreparation(store).prepare(
            DomainEffectRunPreparationInput(
                authorization_ref=authorization.authorization_ref,
            ),
            prepared_at=NOW + timedelta(minutes=3),
        )

    assert store.list_runs() == []
    assert len(store.list_authorization_uses()) == 1


def test_responsibility_revision_blocks_first_run_preparation() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    kernel = ResponsibilityKernel(store)
    _version, statement, scope = kernel.current_definition(RESP_REF)
    kernel.revise(
        ResponsibilityRevision(
            id="revision-admin-run-2",
            created_at=NOW + timedelta(minutes=2),
            responsibility_ref=RESP_REF,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "authority_epoch": "3"},
            basis_refs=["administrative-authority-epoch:3"],
            reason="administrative authority changed before Run preparation",
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectRunPreparation(store).prepare(
            DomainEffectRunPreparationInput(
                authorization_ref=authorization.authorization_ref,
            ),
            prepared_at=NOW + timedelta(minutes=3),
        )

    assert store.list_runs() == []
    assert store.list_authorization_uses() == []
