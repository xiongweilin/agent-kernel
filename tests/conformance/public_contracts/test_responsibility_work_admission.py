from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.catalog import contract_catalog
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.responsibility.admission import BoundedLocalResponsibilityAdmissionPolicy
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel

NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def _domain_payload() -> dict[str, object]:
    responsibility = StandingResponsibility(
        id="resp_public_admission_1",
        created_at=NOW,
        responsibility_kind="administrative-obligation",
        statement="Discharge governed onboarding obligation",
        scope={"administrative_case_id": "case-1", "obligation_id": "obligation-1"},
    )
    admission = ResponsibilityAdmission(
        id="resp_admission_public_1",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        principal_ref="service:administrative-orchestrator",
        basis_refs=["administrative-grant:grant-1"],
        admitted_at=NOW,
    )
    assessment = ResponsibilityAssessment(
        id="assessment_public_1",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        subject_ref="employee:new",
        assessment_kind="administrative-obligation-ready",
        basis_refs=["administrative-grant:grant-1"],
        assessed_at=NOW,
        fresh_until=NOW + timedelta(days=1),
        rationale="domain policy and governance are closed",
    )
    proposal = WorkProposal(
        id="proposal_public_1",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        assessment_ref=assessment.id,
        subject_ref="employee:new",
        work_kind="administrative-effect",
        title="hris: employee.create employee:new",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        requested_capabilities=["administrative.hris.employee.create.v1"],
        expected_result="employee exists with governed onboarding fields",
        effect_class=EffectClass.EXTERNAL_EFFECT,
        fresh_until=NOW + timedelta(days=1),
    )
    return {
        "schema": "domain-responsibility-proposal-v1",
        "responsibility": responsibility.model_dump(mode="json"),
        "admission": admission.model_dump(mode="json"),
        "assessment": assessment.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"),
    }


def _policy() -> BoundedLocalResponsibilityAdmissionPolicy:
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


def _record_proposal(client: TestClient) -> None:
    response = client.post("/v1/responsibilities/domain-proposals", json=_domain_payload())
    assert response.status_code == 200


def test_work_admission_is_registered_as_non_authoritative_contract() -> None:
    contract = contract_catalog()["contracts"]["responsibility_work_admission"]

    assert contract["current"] == "responsibility-work-admission-v1"
    assert contract["receipt"] == "responsibility-work-admission-receipt-v1"
    assert contract["authority_bearing"] is False


def test_public_work_admission_materializes_kernel_owned_chain_without_authorization() -> None:
    runtime = Runtime()
    policy = _policy()
    client = TestClient(
        create_public_app(runtime, responsibility_admission_policy=policy)
    )
    _record_proposal(client)

    command = {
        "schema": "responsibility-work-admission-v1",
        "proposal_ref": "proposal_public_1",
        "expected_policy_ref": policy.policy_ref,
    }
    first = client.post("/v1/responsibilities/work-admissions", json=command)
    second = client.post("/v1/responsibilities/work-admissions", json=command)

    assert first.status_code == 200
    assert second.status_code == 200
    receipt = first.json()
    replay = second.json()
    assert receipt["status"] == "work-materialized"
    assert receipt["authority_bearing"] is False
    assert receipt["policy_ref"] == policy.policy_ref
    assert receipt["priority_judgment_ref"]
    assert receipt["resource_pool_ref"]
    assert receipt["portfolio_admission_ref"]
    assert receipt["reservation_ref"]
    assert receipt["commitment_ref"]
    assert receipt["work_ref"]
    for key in (
        "priority_judgment_ref",
        "resource_pool_ref",
        "portfolio_admission_ref",
        "reservation_ref",
        "commitment_ref",
        "work_ref",
    ):
        assert replay[key] == receipt[key]

    kernel = ResponsibilityKernel(runtime.store)
    for key in (
        "priority_judgment_ref",
        "resource_pool_ref",
        "portfolio_admission_ref",
        "reservation_ref",
        "commitment_ref",
    ):
        assert kernel.journal.get(receipt[key]) is not None
    assert len(runtime.list_work()) == 1
    assert runtime.store.list_authorizations() == []


def test_public_work_admission_policy_mismatch_fails_before_chain_materialization() -> None:
    runtime = Runtime()
    policy = _policy()
    client = TestClient(
        create_public_app(runtime, responsibility_admission_policy=policy)
    )
    _record_proposal(client)

    response = client.post(
        "/v1/responsibilities/work-admissions",
        json={
            "schema": "responsibility-work-admission-v1",
            "proposal_ref": "proposal_public_1",
            "expected_policy_ref": "responsibility-admission:other@9",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ResponsibilityAdmissionPolicyMismatch"
    assert runtime.list_work() == []
    assert runtime.store.list_authorizations() == []


def test_public_work_admission_does_not_accept_client_policy_configuration() -> None:
    runtime = Runtime()
    policy = _policy()
    client = TestClient(
        create_public_app(runtime, responsibility_admission_policy=policy)
    )
    _record_proposal(client)

    response = client.post(
        "/v1/responsibilities/work-admissions",
        json={
            "schema": "responsibility-work-admission-v1",
            "proposal_ref": "proposal_public_1",
            "expected_policy_ref": policy.policy_ref,
            "capacity": {"api_calls": 999999},
        },
    )

    assert response.status_code == 422
    assert runtime.list_work() == []
    assert runtime.store.list_authorizations() == []


def test_public_work_admission_can_return_priority_rejection_without_work() -> None:
    runtime = Runtime()
    policy = BoundedLocalResponsibilityAdmissionPolicy(
        profile_id="read-only-public",
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
        allowed_effect_classes=(EffectClass.READ_ONLY,),
    )
    client = TestClient(
        create_public_app(runtime, responsibility_admission_policy=policy)
    )
    _record_proposal(client)

    response = client.post(
        "/v1/responsibilities/work-admissions",
        json={
            "schema": "responsibility-work-admission-v1",
            "proposal_ref": "proposal_public_1",
            "expected_policy_ref": policy.policy_ref,
        },
    )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "priority-rejected"
    assert receipt["priority_judgment_ref"]
    assert receipt["resource_pool_ref"] is None
    assert receipt["work_ref"] is None
    assert receipt["authority_bearing"] is False
    assert runtime.list_work() == []
    assert runtime.store.list_authorizations() == []


def test_public_work_admission_unknown_proposal_is_not_found() -> None:
    runtime = Runtime()
    policy = _policy()
    client = TestClient(
        create_public_app(runtime, responsibility_admission_policy=policy)
    )

    response = client.post(
        "/v1/responsibilities/work-admissions",
        json={
            "schema": "responsibility-work-admission-v1",
            "proposal_ref": "proposal_missing",
            "expected_policy_ref": policy.policy_ref,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ResponsibilityProposalNotFound"
    assert runtime.list_work() == []
    assert runtime.store.list_authorizations() == []
