from datetime import UTC, datetime

from fastapi.testclient import TestClient

from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.domain_effect_evidence import (
    DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA,
    project_domain_effect_verification_evidence,
)
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.records.models import EvidenceArtifact

NOW = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)
EVIDENCE_REF = "evidence_domain_effect_verification_test"


def _proof(*, observed: dict[str, object] | None = None) -> EvidenceArtifact:
    expected = {"active": True, "employee_ref": "employee:42"}
    return EvidenceArtifact(
        id=EVIDENCE_REF,
        created_at=NOW,
        kind="task-objective-proof",
        source_refs=["action:test"],
        lifecycle_status="current",
        metadata={
            "schema": "domain-effect-objective-verification-evidence-v1",
            "proof_class": "objective-verification",
            "verification_result": {"result": "pass", "message": "independent read-back"},
            "action_ref": "action:test",
            "work_id": "work:test",
            "run_id": "run:test",
            "verification_scope": {"expected_postcondition": expected},
            "observed_postcondition": dict(observed or expected),
            "verification_request_ref": "request:verify:test",
            "verification_attempt_ref": "attempt:verify:test",
            "verifier_provenance": {
                "provider_id": "provider:verify:test",
                "provider_execution_binding_ref": "binding:verify:test",
            },
        },
    )


def test_domain_effect_evidence_view_exposes_actual_observation_without_authority() -> None:
    runtime = Runtime()
    observed = {"active": True, "employee_ref": "employee:42", "source": "actual-read"}
    runtime.store.save_record(_proof(observed=observed))

    view = project_domain_effect_verification_evidence(runtime, EVIDENCE_REF)

    assert view is not None
    assert view.schema_ == DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA
    assert view.evidence_ref == EVIDENCE_REF
    assert view.observed_postcondition == observed
    assert view.expected_postcondition == {"active": True, "employee_ref": "employee:42"}
    assert view.objective_result == "pass"
    assert view.authority_bearing is False


def test_domain_effect_evidence_http_view_is_read_only_and_exact_identity() -> None:
    runtime = Runtime()
    runtime.store.save_record(_proof())
    before = runtime.store.export_state()
    client = TestClient(create_public_app(runtime))

    response = client.get(f"/v1/domain-effects/evidence/{EVIDENCE_REF}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == DOMAIN_EFFECT_VERIFICATION_EVIDENCE_VIEW_SCHEMA
    assert payload["evidence_ref"] == EVIDENCE_REF
    assert payload["authority_bearing"] is False
    assert payload["observed_postcondition"] == payload["expected_postcondition"]
    assert runtime.store.export_state() == before


def test_domain_effect_evidence_view_rejects_non_verification_evidence() -> None:
    runtime = Runtime()
    runtime.store.save_record(
        EvidenceArtifact(
            id="evidence_other",
            kind="artifact",
            created_at=NOW,
            metadata={"schema": "some-other-proof-v1"},
        )
    )
    client = TestClient(create_public_app(runtime))

    response = client.get("/v1/domain-effects/evidence/evidence_other")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "DomainEffectEvidenceRejected"


def test_domain_effect_evidence_view_returns_404_for_unknown_identity() -> None:
    client = TestClient(create_public_app(Runtime()))

    response = client.get("/v1/domain-effects/evidence/evidence_missing")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DomainEffectEvidenceNotFound"
