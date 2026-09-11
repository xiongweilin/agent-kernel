from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.public_contracts.catalog import contract_catalog
from portable_runtime.responsibility import (
    ResponsibilityAdmission,
    ResponsibilityKernel,
    StandingResponsibility,
)

NOW = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)


def _runtime() -> tuple[Runtime, TestClient]:
    runtime = Runtime()
    kernel = ResponsibilityKernel(runtime.store)
    responsibility = StandingResponsibility(
        id="responsibility_public_discharge",
        responsibility_kind="generic-bounded-obligation",
        statement="Carry a bounded obligation until verified completion",
        scope={"obligation_ref": "obligation:1"},
        created_at=NOW,
    )
    kernel.register(
        responsibility,
        ResponsibilityAdmission(
            id="admission_public_discharge",
            responsibility_ref=responsibility.id,
            responsibility_version=1,
            principal_ref="service:domain-owner",
            basis_refs=["domain-grant:1"],
            admitted_at=NOW,
            created_at=NOW,
        ),
    )
    return runtime, TestClient(create_public_app(runtime))


def _assessment() -> dict[str, object]:
    assessed_at = datetime.now(UTC)
    return {
        "id": "assessment_public_discharge",
        "object_type": "ResponsibilityAssessment",
        "responsibility_ref": "responsibility_public_discharge",
        "responsibility_version": 1,
        "subject_ref": "obligation:1",
        "assessment_kind": "responsibility-discharge-reassessment",
        "basis_refs": ["completion:1", "outcome:1"],
        "assessed_at": assessed_at.isoformat(),
        "fresh_until": (assessed_at + timedelta(minutes=10)).isoformat(),
        "rationale": "the generic obligation is currently satisfied",
        "created_at": NOW.isoformat(),
    }


def _decision(*, assessment_ref: str = "assessment_public_discharge") -> dict[str, object]:
    decided_at = datetime.now(UTC)
    return {
        "id": "decision_public_discharge",
        "object_type": "ResponsibilityDischargeDecision",
        "responsibility_ref": "responsibility_public_discharge",
        "responsibility_version": 1,
        "status_at_decision": "active",
        "assessment_ref": assessment_ref,
        "disposition": "discharge",
        "basis_refs": [assessment_ref, "completion:1"],
        "policy_ref": "domain:responsibility-discharge@1",
        "decided_at": decided_at.isoformat(),
        "rationale": "fresh assessment authorizes discharge judgment",
        "created_at": decided_at.isoformat(),
    }


def test_public_discharge_keeps_decision_and_transition_separate() -> None:
    _runtime_value, client = _runtime()
    initial = client.get(
        "/v1/responsibilities/responsibility_public_discharge/status"
    )
    assert initial.status_code == 200
    assert initial.json()["current_status"] == "active"

    assessment = client.post(
        "/v1/responsibilities/assessments",
        json={
            "schema": "responsibility-assessment-record-v1",
            "assessment": _assessment(),
        },
    )
    assert assessment.status_code == 200
    assert assessment.json()["assessment"]["id"] == "assessment_public_discharge"

    decision = client.post(
        "/v1/responsibilities/discharge-decisions",
        json={
            "schema": "responsibility-discharge-decision-record-v1",
            "decision": _decision(),
        },
    )
    assert decision.status_code == 200
    assert decision.json()["status_after_decision"] == "active"
    still_active = client.get(
        "/v1/responsibilities/responsibility_public_discharge/status"
    )
    assert still_active.json()["current_status"] == "active"

    transition = client.post(
        "/v1/responsibilities/lifecycle-transitions",
        json={
            "schema": "responsibility-lifecycle-transition-apply-v1",
            "transition": {
                "id": "transition_public_discharge",
                "object_type": "ResponsibilityLifecycleTransition",
                "responsibility_ref": "responsibility_public_discharge",
                "responsibility_version": 1,
                "from_status": "active",
                "to_status": "discharged",
                "decision_ref": "decision_public_discharge",
                "basis_refs": ["decision_public_discharge"],
                "reason": "explicit generic lifecycle application",
                "applied_at": (NOW + timedelta(seconds=2)).isoformat(),
                "created_at": (NOW + timedelta(seconds=2)).isoformat(),
            },
        },
    )
    assert transition.status_code == 200
    assert transition.json()["current_status"] == "discharged"
    final = client.get(
        "/v1/responsibilities/responsibility_public_discharge/status"
    )
    assert final.json()["current_status"] == "discharged"


def test_public_discharge_rejects_missing_assessment_and_stale_version() -> None:
    _runtime_value, client = _runtime()
    missing = client.post(
        "/v1/responsibilities/discharge-decisions",
        json={
            "schema": "responsibility-discharge-decision-record-v1",
            "decision": _decision(assessment_ref="assessment:missing"),
        },
    )
    assert missing.status_code == 409

    stale = _assessment()
    stale["responsibility_version"] = 2
    rejected = client.post(
        "/v1/responsibilities/assessments",
        json={
            "schema": "responsibility-assessment-record-v1",
            "assessment": stale,
        },
    )
    assert rejected.status_code == 409


def test_public_status_returns_not_found_for_unknown_responsibility() -> None:
    _runtime_value, client = _runtime()
    response = client.get("/v1/responsibilities/responsibility:missing/status")
    assert response.status_code == 404


def test_discharge_public_contracts_are_catalogued() -> None:
    contracts = contract_catalog()["contracts"]
    assert contracts["responsibility_assessment_record"]["current"] == (
        "responsibility-assessment-record-v1"
    )
    assert contracts["responsibility_discharge_decision_record"]["current"] == (
        "responsibility-discharge-decision-record-v1"
    )
    assert contracts["responsibility_lifecycle_transition_apply"]["current"] == (
        "responsibility-lifecycle-transition-apply-v1"
    )
    assert contracts["responsibility_status"]["current"] == (
        "responsibility-status-view-v1"
    )
