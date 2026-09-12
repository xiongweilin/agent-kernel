from __future__ import annotations

from datetime import timedelta

import pytest

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.models import utcnow
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)
from portable_runtime.responsibility.domain_effect_activation import (
    DomainEffectRunActivation,
    DomainEffectRunActivationInput,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DOMAIN_EFFECT_QUALIFICATION_EVENT,
    DomainEffectQualificationAssessment,
    DomainEffectQualificationInput,
)
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessInput,
)
from portable_runtime.responsibility.domain_effect_reality_execution import (
    DomainEffectRealityExecution,
)
from portable_runtime.stores.memory import InMemoryStateStore
from tests.conformance.test_domain_effect_qualification import NOW, _activated
from tests.conformance.test_domain_effect_reality_cutover import _live_cutover_fixture


@pytest.mark.asyncio
async def test_committed_action_refencing_replays_historical_qualification_closure() -> None:
    (
        store,
        qualification,
        registry,
        provider,
        _configured_binding,
        _provider_binding,
        _specification,
    ) = _live_cutover_fixture()
    boundary = RealityBoundary(store=store, registry=registry)
    executor = DomainEffectRealityExecution(
        boundary,
        DomainEffectActionAuthorityResolver(store, registry),
    )

    result = await executor.execute(qualification.request)

    assert result.status == "succeeded"
    assert provider.invocations == 1
    assert len(store.list_authorization_uses()) == 1

    run_ref = qualification.run_ref
    historical_generation = qualification.lease_generation
    assert store.release_lease(run_ref, qualification.lease_owner)

    activation = DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_ref),
        owner="runtime-worker:qualification-recovery-fresh",
        ttl_seconds=300,
        activated_at=utcnow(),
    )
    assert activation.lease_generation > historical_generation

    replay = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_ref),
        assessed_at=utcnow(),
    )

    assert replay == qualification
    assert provider.invocations == 1
    events = [
        event
        for event in store.list_events(run_ref)
        if event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
    ]
    assert len(events) == 1
    assert events[0].id == qualification.qualification_event_ref


def test_requalification_ignores_downstream_procedure_proof_refs() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, _request, _activation = _activated(store)
    qualification = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=5),
    )
    DomainEffectProcedureReadinessAssessment(store).assess(
        DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=6),
    )

    assert store.release_lease(run_result.run_ref, qualification.lease_owner)
    activation = DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner="runtime-worker:qualification-retry",
        ttl_seconds=300,
        activated_at=NOW + timedelta(minutes=7),
    )

    replay = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=8),
    )

    assert replay.status == "qualified"
    assert replay.lease_generation == activation.lease_generation
