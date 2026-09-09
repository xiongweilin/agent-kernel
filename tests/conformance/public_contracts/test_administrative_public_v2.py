from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.responsibility.admission_profiles import (
    ADMINISTRATIVE_PUBLIC_POLICY_REF,
    ADMINISTRATIVE_PUBLIC_V2_POLICY_REF,
    ADMINISTRATIVE_PUBLIC_V2_PROFILE,
    administrative_public_responsibility_admission_policy,
    administrative_public_v2_responsibility_admission_policy,
    responsibility_admission_policy_for_profile,
)
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)

NOW = datetime(2026, 9, 9, 3, 30, tzinfo=UTC)


def _iam_domain_payload() -> dict[str, object]:
    responsibility = StandingResponsibility(
        id="resp_public_iam_v2",
        created_at=NOW,
        responsibility_kind="administrative-obligation",
        statement="Discharge governed onboarding IAM identity creation",
        scope={
            "administrative_case_id": "case-iam-v2",
            "authority_epoch": "1",
            "obligation_id": "obligation-iam-v2",
            "governance_basis_id": "governance-iam-v2",
            "execution_grant_id": "grant-iam-v2",
            "target_system": "iam",
            "operation": "identity.create",
            "policy_ref": "employee-onboarding:v1",
        },
    )
    admission = ResponsibilityAdmission(
        id="resp_admission_public_iam_v2",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        principal_ref="service:administrative-orchestrator",
        basis_refs=["administrative-grant:grant-iam-v2"],
        admitted_at=NOW,
    )
    assessment = ResponsibilityAssessment(
        id="assessment_public_iam_v2",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        subject_ref="employee:iam-v2",
        assessment_kind="administrative-obligation-ready",
        basis_refs=["administrative-grant:grant-iam-v2"],
        assessed_at=NOW,
        fresh_until=NOW + timedelta(days=1),
        rationale="domain policy and governance are closed",
    )
    proposal = WorkProposal(
        id="proposal_public_iam_v2",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        assessment_ref=assessment.id,
        subject_ref="employee:iam-v2",
        work_kind="administrative-effect",
        title="iam: identity.create employee:iam-v2",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:iam": 1},
        ),
        requested_capabilities=["administrative.iam.identity.create.v1"],
        expected_result="identity exists with governed onboarding fields",
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


def test_administrative_public_v1_remains_hris_only_and_v2_is_explicitly_dual_domain() -> None:
    v1 = administrative_public_responsibility_admission_policy()
    v2 = administrative_public_v2_responsibility_admission_policy()

    assert v1.policy_ref == ADMINISTRATIVE_PUBLIC_POLICY_REF
    assert v1.max_request.domain_quota == {"administrative:hris": 1}
    assert v1.pool_capacity.domain_quota == {"administrative:hris": 4}

    assert v2.policy_ref == ADMINISTRATIVE_PUBLIC_V2_POLICY_REF
    assert v2.max_request.domain_quota == {
        "administrative:hris": 1,
        "administrative:iam": 1,
    }
    assert v2.pool_capacity.domain_quota == {
        "administrative:hris": 4,
        "administrative:iam": 4,
    }
    assert responsibility_admission_policy_for_profile(ADMINISTRATIVE_PUBLIC_V2_PROFILE) == v2
    assert responsibility_admission_policy_for_profile(ADMINISTRATIVE_PUBLIC_V2_POLICY_REF) == v2


def test_administrative_public_v2_materializes_iam_work_through_public_http() -> None:
    runtime = Runtime()
    client = TestClient(
        create_public_app(
            runtime,
            responsibility_admission_profile=ADMINISTRATIVE_PUBLIC_V2_PROFILE,
        )
    )

    proposal = client.post(
        "/v1/responsibilities/domain-proposals",
        json=_iam_domain_payload(),
    )
    assert proposal.status_code == 200

    response = client.post(
        "/v1/responsibilities/work-admissions",
        json={
            "schema": "responsibility-work-admission-v1",
            "proposal_ref": "proposal_public_iam_v2",
            "expected_policy_ref": ADMINISTRATIVE_PUBLIC_V2_POLICY_REF,
        },
    )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "work-materialized"
    assert receipt["policy_ref"] == ADMINISTRATIVE_PUBLIC_V2_POLICY_REF
    assert receipt["work_ref"]
    assert len(runtime.list_work()) == 1
    assert runtime.store.list_authorizations() == []
