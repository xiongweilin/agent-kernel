from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from portable_runtime.core.models import utcnow
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.domain_effect import (
    BoundedDomainEffectExecutionProfile,
    BoundedDomainEffectExecutionService,
    BoundedDomainEffectExecutionV1,
)
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
)
from portable_runtime.stores.invocation_specification import (
    InvocationSpecificationInMemoryStateStore,
)
from tests.conformance.test_domain_effect_authorization_admission import (
    APPROVAL_REF,
    EPOCH,
    GOVERNANCE_REF,
    GRANT_REF,
    INTENT_REF,
    NOW,
    SUBJECT_REF,
    _admitted_work,
    _intent,
)
from tests.conformance.test_domain_effect_provider_binding import (
    _HrisProvider,
    _contract,
    _register,
)
from tests.conformance.test_domain_effect_verified_outcome import (
    _ReadbackVerifier,
    _register_verifier,
)


def _fixture():
    store = InvocationSpecificationInMemoryStateStore()
    work = _admitted_work(store)
    registry = ProviderRegistry()
    runtime = Runtime(
        store=store,
        registry=registry,
        runtime_id="runtime:public-domain-effect",
    )
    effect_provider = _HrisProvider(
        "provider:hris:public-domain-effect",
        ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
    )
    _register(registry, effect_provider)
    intent = _intent(work.id)
    verifier = _ReadbackVerifier(
        "provider:hris:public-domain-effect-readback",
        {SUBJECT_REF: dict(intent.expected_postcondition)},
    )
    _register_verifier(registry, verifier)
    service = BoundedDomainEffectExecutionService(
        runtime,
        [
            BoundedDomainEffectExecutionProfile(
                capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
                provider_id=effect_provider.descriptor.id,
                verifier_provider_id=verifier.descriptor.id,
                semantic_contract=_contract(effect_provider.descriptor.id),
                lease_owner="kernel:public-domain-effect-test",
            )
        ],
    )
    command = BoundedDomainEffectExecutionV1(
        schema="bounded-domain-effect-execution-v1",
        work_ref=work.id,
        domain_intent_ref=INTENT_REF,
        domain_grant_ref=GRANT_REF,
        governance_basis_ref=GOVERNANCE_REF,
        approval_satisfaction_ref=APPROVAL_REF,
        capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        subject_ref=SUBJECT_REF,
        authority_epoch=EPOCH,
        parameters=dict(intent.parameters),
        expected_postcondition=dict(intent.expected_postcondition),
        observed_at=NOW,
    )
    return runtime, service, command, effect_provider, verifier


@pytest.mark.asyncio
async def test_high_level_execution_uses_one_reality_exit_and_is_receipt_idempotent() -> None:
    runtime, service, command, effect_provider, verifier = _fixture()
    # Physical execution fencing is evaluated against live runtime time. The
    # command's domain evidence may be historical, but the execution itself is
    # not a time-travel API.
    at = utcnow()

    first = await service.execute(command, processed_at=at)
    second = await service.execute(command, processed_at=at + timedelta(minutes=1))

    assert first == second
    assert first.status == "completed"
    assert first.authority_bearing is False
    assert first.work_ref == command.work_ref
    assert first.run_ref
    assert first.request_ref
    assert first.authorization_ref
    assert first.action_ref
    assert first.outcome_ref
    assert first.evidence_ref
    assert first.responsibility_ref
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1
    assert runtime.get_work(command.work_ref).status == "completed"
    assert service.inspect(first.execution_ref) == first


@pytest.mark.asyncio
async def test_domain_cannot_select_an_unconfigured_physical_capability() -> None:
    _runtime, service, command, effect_provider, verifier = _fixture()
    unsupported = command.model_copy(update={"capability": "administrative.iam.identity.create.v1"})

    with pytest.raises(ValueError, match="not configured server-side"):
        await service.execute(unsupported, processed_at=NOW + timedelta(minutes=10))

    assert effect_provider.invocations == 0
    assert verifier.invocations == 0


def test_public_http_execution_requires_server_configuration() -> None:
    runtime, _service, command, _effect_provider, _verifier = _fixture()
    response = TestClient(create_public_app(runtime)).post(
        "/v1/domain-effects/executions",
        json=command.model_dump(mode="json", by_alias=True),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DomainEffectExecutionUnavailable"


def test_public_http_executes_and_inspects_durable_receipt() -> None:
    runtime, service, command, effect_provider, verifier = _fixture()
    client = TestClient(create_public_app(runtime, bounded_domain_effect_execution=service))

    response = client.post(
        "/v1/domain-effects/executions",
        json=command.model_dump(mode="json", by_alias=True),
    )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["schema"] == "bounded-domain-effect-execution-receipt-v1"
    assert receipt["status"] == "completed"
    assert receipt["authority_bearing"] is False
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1

    inspected = client.get(f"/v1/domain-effects/executions/{receipt['execution_ref']}")
    assert inspected.status_code == 200
    assert inspected.json() == receipt
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1
