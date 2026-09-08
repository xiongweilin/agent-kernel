from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from portable_runtime.records.authorization import (
    AuthorizationGrant,
    CanonicalAuthorizationRequest,
    create_authorization_use,
)
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

NOW = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
CASE_REF = "case-use-1"
RESP_REF = "resp-admin-use-1"
ASSESSMENT_REF = "assessment-admin-use-1"
PROPOSAL_REF = "proposal-admin-use-1"
INTENT_REF = "intent-use-1"
DOMAIN_GRANT_REF = "grant-use-1"
GOVERNANCE_REF = "governance-use-1"
APPROVAL_REF = "approval-use-1"
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
                "obligation_id": "obligation-use-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": DOMAIN_GRANT_REF,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-use-1",
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


def test_use_input_exposes_only_runtime_authorization_identity() -> None:
    with pytest.raises(ValidationError):
        DomainEffectAuthorizationUseInput.model_validate(
            {
                "authorization_ref": "authz-1",
                "actor_ref": "service:administrative-orchestrator",
            }
        )


def test_runtime_grant_consumption_materializes_one_kernel_derived_use() -> None:
    store = InMemoryStateStore()
    work, authorization = _authorized(store)
    consumption = DomainEffectAuthorizationUseConsumption(store)

    result = consumption.consume(
        DomainEffectAuthorizationUseInput(
            authorization_ref=authorization.authorization_ref,
        ),
        authorized_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "consumed"
    assert result.authorization_ref == authorization.authorization_ref
    assert result.evidence_ref == authorization.evidence_ref
    assert result.decision_ref == authorization.decision_ref
    assert result.work_ref == work.id
    assert result.capability == ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
    assert result.actor_ref == authorization.actor_ref
    assert result.resource_ref == authorization.resource_ref
    assert result.subject_version_ref == authorization.subject_version_ref
    assert result.authorized_at == NOW + timedelta(minutes=2)
    assert result.authority_bearing is False

    uses = store.list_authorization_uses()
    assert len(uses) == 1
    use = uses[0]
    assert use.id == result.authorization_use_ref
    assert use.authorization_ref == authorization.authorization_ref
    assert use.capability == ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
    assert use.actor_ref == authorization.actor_ref
    assert use.resource_ref == authorization.resource_ref
    assert use.effect_class == "write-remote"
    assert use.subject_version_refs == [authorization.subject_version_ref]
    assert store.list_runs() == []


def test_use_replay_preserves_historical_time_after_grant_expiry() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    consumption = DomainEffectAuthorizationUseConsumption(store)
    command = DomainEffectAuthorizationUseInput(
        authorization_ref=authorization.authorization_ref,
    )

    first = consumption.consume(
        command,
        authorized_at=NOW + timedelta(minutes=2),
    )
    replay = consumption.consume(
        command,
        authorized_at=NOW + timedelta(minutes=20),
    )

    assert replay == first
    assert replay.authorized_at == NOW + timedelta(minutes=2)
    assert len(store.list_authorization_uses()) == 1
    assert store.list_runs() == []


def test_first_consumption_rejects_expired_grant_without_use() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)

    with pytest.raises(ValueError, match="not authorized at action time"):
        DomainEffectAuthorizationUseConsumption(store).consume(
            DomainEffectAuthorizationUseInput(
                authorization_ref=authorization.authorization_ref,
            ),
            authorized_at=NOW + timedelta(minutes=20),
        )

    assert store.list_authorization_uses() == []
    assert store.list_runs() == []


def test_current_responsibility_revision_invalidates_unconsumed_grant() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    kernel = ResponsibilityKernel(store)
    _version, statement, scope = kernel.current_definition(RESP_REF)
    kernel.revise(
        ResponsibilityRevision(
            id="revision-admin-use-2",
            created_at=NOW + timedelta(minutes=2),
            responsibility_ref=RESP_REF,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "authority_epoch": "3"},
            basis_refs=["administrative-authority-epoch:3"],
            reason="administrative authority changed before action",
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectAuthorizationUseConsumption(store).consume(
            DomainEffectAuthorizationUseInput(
                authorization_ref=authorization.authorization_ref,
            ),
            authorized_at=NOW + timedelta(minutes=3),
        )

    assert store.list_authorization_uses() == []
    assert store.list_runs() == []


def test_foreign_authorization_use_identity_blocks_canonical_consumption() -> None:
    store = InMemoryStateStore()
    _work, authorization = _authorized(store)
    grant = store.get_authorization(authorization.authorization_ref)
    assert isinstance(grant, AuthorizationGrant)
    foreign = create_authorization_use(
        grant,
        CanonicalAuthorizationRequest(
            capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
            actor_ref=authorization.actor_ref,
            resource_ref=authorization.resource_ref,
            subject_version_refs=[authorization.subject_version_ref],
            effect_class="write-remote",
        ),
        authorized_at=NOW + timedelta(minutes=2),
    )
    store.save_authorization_use(foreign)

    with pytest.raises(ValueError, match="outside the canonical use identity"):
        DomainEffectAuthorizationUseConsumption(store).consume(
            DomainEffectAuthorizationUseInput(
                authorization_ref=authorization.authorization_ref,
            ),
            authorized_at=NOW + timedelta(minutes=3),
        )

    assert store.list_authorization_uses() == [foreign]
    assert store.list_runs() == []
