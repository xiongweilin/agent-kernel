from __future__ import annotations

from datetime import timedelta

import pytest

from portable_runtime.core.qualification import AssessmentContext
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT,
    DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA,
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessInput,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DomainEffectQualificationAssessment,
    DomainEffectQualificationInput,
)
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.workflows.procedure import ProcedureProfile, check_procedure
from portable_runtime.workflows.procedure_phase import check_pre_action_readiness
from tests.conformance.test_domain_effect_qualification import NOW, _activated


def _name(status) -> str:
    return str(getattr(status.obligation, "kind", status.obligation))


def _qualified(store: InMemoryStateStore):
    work, authorization, run_result, _request, _activation = _activated(store)
    qualification = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=5),
    )
    return work, authorization, run_result, qualification


def test_readiness_closes_only_standard_pre_action_obligations() -> None:
    store = InMemoryStateStore()
    work, _authorization, run_result, qualification = _qualified(store)

    result = DomainEffectProcedureReadinessAssessment(store).assess(
        DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=6),
    )

    run = store.get_run(run_result.run_ref)
    event = store.get_event(result.readiness_event_ref)
    assert run is not None and event is not None
    assert result.status == "ready"
    assert result.authority_bearing is False
    assert result.procedure_profile == "standard"
    assert len(result.readiness_digest) == 64
    assert {ref.kind for ref in result.readiness_refs} == {
        "failure-stop",
        "evidence",
        "relation",
        "recovery",
    }
    assert event.type == DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT
    assert event.payload["schema"] == DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA
    assert event.payload["authority_bearing"] is False
    assert event.payload["readiness_digest"] == result.readiness_digest

    assert run.metadata["purpose"] == work.description
    assert run.metadata["execution_boundary"] == "provider"
    assert run.metadata["candidate"] == [run.metadata["logical_effect_ref"]]
    assert run.metadata["procedure_profile"] == "standard"
    assert "result_confirmed" not in run.metadata
    assert "verification_result_refs" not in run.metadata
    assert "verification_refs" not in run.metadata
    assert store.list_authorization_uses() == []

    fresh = AssessmentContext.resolve(
        store,
        qualification.request,
        work=work,
        run=run,
    )
    assert fresh.digest == result.readiness_digest
    assert fresh.has_authorization_refs
    pre = check_pre_action_readiness(
        fresh.work,
        fresh.run,
        ProcedureProfile.standard,
        proofs=fresh.procedure_proofs(),
        grants=fresh.proofs.get("grants"),
    )
    assert [_name(status) for status in pre] == list(result.pre_action_obligations)
    assert all(status.status == "satisfied" for status in pre)

    full = check_procedure(
        fresh.work,
        fresh.run,
        ProcedureProfile.standard,
        proofs=fresh.procedure_proofs(),
        grants=fresh.proofs.get("grants"),
    )
    by_name = {_name(status): status.status for status in full}
    assert by_name["result-confirmation"] == "open"
    assert by_name["verification"] == "open"
    assert full.executable is True


def test_readiness_replay_revalidates_the_authoritative_snapshot() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _qualification = _qualified(store)
    assessment = DomainEffectProcedureReadinessAssessment(store)
    command = DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref)

    first = assessment.assess(command, assessed_at=NOW + timedelta(minutes=6))
    replay = assessment.assess(command, assessed_at=NOW + timedelta(minutes=7))
    assert replay == first
    events = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT
    ]
    assert len(events) == 1
    assert events[0].created_at == NOW + timedelta(minutes=6)

    recovery_ref = next(
        ref.ref_id for ref in first.readiness_refs if ref.kind == "recovery"
    )
    recovery = store.get_record(recovery_ref)
    assert recovery is not None
    changed_metadata = dict(recovery.metadata)
    changed_metadata["required_effect_semantics"] = "pure"
    store.save_record(recovery.model_copy(update={"metadata": changed_metadata}))

    with pytest.raises(ValueError, match="snapshot is stale"):
        assessment.assess(command, assessed_at=NOW + timedelta(minutes=8))


def test_readiness_requires_prior_qualification_and_keeps_authorization_unused() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request, _activation = _activated(store)

    with pytest.raises(ValueError, match="exactly one qualification event"):
        DomainEffectProcedureReadinessAssessment(store).assess(
            DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref),
            assessed_at=NOW + timedelta(minutes=5),
        )

    assert store.list_authorization_uses() == []
    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT
    ] == []
