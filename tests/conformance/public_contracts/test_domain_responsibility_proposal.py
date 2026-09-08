from datetime import UTC, datetime

from fastapi.testclient import TestClient

from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.http import create_public_app
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


def _payload() -> dict[str, object]:
    responsibility = StandingResponsibility(
        id="resp_admin_1",
        created_at=NOW,
        responsibility_kind="administrative-obligation",
        statement="Discharge governed onboarding obligation",
        scope={"administrative_case_id": "case-1", "obligation_id": "obligation-1"},
    )
    admission = ResponsibilityAdmission(
        id="resp_admission_admin_1",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        principal_ref="service:administrative-orchestrator",
        basis_refs=["administrative-grant:grant-1"],
        admitted_at=NOW,
    )
    assessment = ResponsibilityAssessment(
        id="assessment_admin_1",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        subject_ref="employee:new",
        assessment_kind="administrative-obligation-ready",
        basis_refs=["administrative-grant:grant-1"],
        assessed_at=NOW,
        rationale="domain policy and governance are closed",
    )
    proposal = WorkProposal(
        id="proposal_admin_1",
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
    )
    return {
        "schema": "domain-responsibility-proposal-v1",
        "responsibility": responsibility.model_dump(mode="json"),
        "admission": admission.model_dump(mode="json"),
        "assessment": assessment.model_dump(mode="json"),
        "proposal": proposal.model_dump(mode="json"),
    }


def test_domain_proposal_records_canonical_chain_but_does_not_admit_work() -> None:
    runtime = Runtime()
    client = TestClient(create_public_app(runtime))

    first = client.post("/v1/responsibilities/domain-proposals", json=_payload())
    second = client.post("/v1/responsibilities/domain-proposals", json=_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    receipt = first.json()
    assert receipt["status"] == "proposal-recorded"
    assert receipt["responsibility_ref"] == "resp_admin_1"
    assert receipt["proposal_ref"] == "proposal_admin_1"
    assert receipt["authority_bearing"] is False

    kernel = ResponsibilityKernel(runtime.store)
    assert kernel.journal.get("resp_admin_1") is not None
    assert kernel.journal.get("resp_admission_admin_1") is not None
    assert kernel.journal.get("assessment_admin_1") is not None
    assert kernel.journal.get("proposal_admin_1") is not None
    assert runtime.list_work() == []
    assert runtime.store.list_authorization_grants() == []


def test_domain_proposal_rejects_cross_responsibility_admission() -> None:
    runtime = Runtime()
    client = TestClient(create_public_app(runtime))
    payload = _payload()
    admission = dict(payload["admission"])
    admission["responsibility_ref"] = "resp_other"
    payload["admission"] = admission

    response = client.post("/v1/responsibilities/domain-proposals", json=payload)

    assert response.status_code == 409
    assert "bind the standing responsibility" in str(response.json())
    assert runtime.list_work() == []
