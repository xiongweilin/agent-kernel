from __future__ import annotations

import json
from datetime import timedelta

import pytest

from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult, InvocationContext
from portable_runtime.core.provider_semantics import ProviderSemanticContract
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.providers.fake import EchoProvider
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessInput,
)
from portable_runtime.responsibility.domain_effect_provider_binding import (
    DOMAIN_EFFECT_PROVIDER_BINDING_EVENT,
    DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA,
    DomainEffectProviderBindingAssessment,
    DomainEffectProviderBindingInput,
)
from portable_runtime.stores.memory import InMemoryStateStore
from tests.conformance.test_domain_effect_procedure_readiness import NOW, _qualified


class _HrisProvider(EchoProvider):
    def __init__(
        self,
        provider_id: str,
        capability: str,
        *,
        effect_semantics: str = "reconcilable",
        reversibility: str = "compensatable",
    ) -> None:
        super().__init__(provider_id)
        self.invocations = 0
        self._descriptor = self._descriptor.model_copy(
            update={
                "name": "Controlled HRIS Provider",
                "version": "2026.09",
                "capabilities": [capability],
                "effect_semantics": effect_semantics,
                "side_effect_class": effect_semantics,
                "reversibility": reversibility,
                "provider_family": "test-hris",
                "operator": "test-control-plane",
                "execution_domain": "fixture:hris",
                "credential_domain": "fixture:credential",
                "data_source_domain": "fixture:hris-state",
                "network_domain": "fixture:network",
                "trust_boundary": "fixture",
            }
        )

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        self.invocations += 1
        return await super().invoke(request, context)


def _contract(provider_id: str) -> ProviderSemanticContract:
    return ProviderSemanticContract(
        id=f"semantic:{provider_id}:employee-create",
        version="1",
        provider_id=provider_id,
    )


def _ready(store: InMemoryStateStore):
    work, authorization, run_result, qualification = _qualified(store)
    readiness = DomainEffectProcedureReadinessAssessment(store).assess(
        DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref),
        assessed_at=NOW + timedelta(minutes=6),
    )
    return work, authorization, run_result, qualification, readiness


def _register(
    registry: ProviderRegistry,
    provider: _HrisProvider,
    *,
    execution_identity: str = "configured:hris:tenant-a",
    configuration_ref: str = "config:hris:tenant-a:v1",
):
    registry.register(
        provider,
        configured_execution_identity=execution_identity,
        authoritative_configuration_ref=configuration_ref,
    )
    return registry.execution_binding(provider.descriptor.id)


def test_provider_binding_freezes_registry_target_and_provider_visible_semantics() -> None:
    store = InMemoryStateStore()
    work, _authorization, run_result, qualification, readiness = _ready(store)
    registry = ProviderRegistry()
    provider = _HrisProvider("provider:hris:test", qualification.request.capability)
    configured_binding = _register(registry, provider)
    contract = _contract(provider.descriptor.id)
    command = DomainEffectProviderBindingInput(
        run_ref=run_result.run_ref,
        provider_id=provider.descriptor.id,
        runtime_id="runtime:test-domain-effect",
        semantic_contract=contract,
    )
    assessment = DomainEffectProviderBindingAssessment(store, registry)

    result = assessment.assess(command, assessed_at=NOW + timedelta(minutes=7))
    replay = assessment.assess(command, assessed_at=NOW + timedelta(minutes=8))

    assert replay == result
    assert result.status == "bound"
    assert result.execution_authority_bearing is False
    assert result.work_ref == work.id
    assert result.qualification_event_ref == qualification.qualification_event_ref
    assert result.readiness_event_ref == readiness.readiness_event_ref
    assert result.provider_id == provider.descriptor.id
    assert result.provider_execution_binding == configured_binding
    assert result.provider_replay_binding.provider_binding_id == configured_binding.id
    assert result.semantic_contract.digest == contract.digest
    assert result.semantic_projection.contract_digest == contract.digest
    assert result.invocation_context.runtime_id == "runtime:test-domain-effect"
    assert result.invocation_context.lease_generation == result.lease_generation

    semantic_payload = json.loads(result.semantic_projection.canonical_payload)
    assert semantic_payload["provider_id"] == provider.descriptor.id
    assert semantic_payload["request"]["capability"] == qualification.request.capability
    assert semantic_payload["request"]["parameters"] == qualification.request.parameters
    assert semantic_payload["request"]["resource_ref"] == qualification.request.resource_ref
    assert semantic_payload["request"]["subject_version_refs"] == qualification.request.subject_version_refs

    events = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_PROVIDER_BINDING_EVENT
    ]
    assert len(events) == 1
    event = events[0]
    assert event.id == result.binding_event_ref
    assert event.payload["schema"] == DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA
    assert event.payload["execution_authority_bearing"] is False
    assert event.payload["binding_authority"] == "registry-configured-provider-target"
    assert event.payload["provider_execution_binding"]["id"] == configured_binding.id
    assert event.payload["semantic_contract_digest"] == contract.digest

    run = store.get_run(run_result.run_ref)
    assert run is not None
    assert run.provider_invocation_refs == []
    assert run.metadata["provider_selection"] == "not-performed"
    assert run.metadata["invocation_permit"] == "not-issued"
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0


def test_provider_binding_rejects_provider_that_breaks_recovery_contract() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, qualification, _readiness = _ready(store)
    registry = ProviderRegistry()
    provider = _HrisProvider(
        "provider:hris:not-reconcilable",
        qualification.request.capability,
        effect_semantics="idempotent",
    )
    _register(registry, provider)

    with pytest.raises(ValueError, match="recovery-required effect semantics"):
        DomainEffectProviderBindingAssessment(store, registry).assess(
            DomainEffectProviderBindingInput(
                run_ref=run_result.run_ref,
                provider_id=provider.descriptor.id,
                runtime_id="runtime:test-domain-effect",
                semantic_contract=_contract(provider.descriptor.id),
            ),
            assessed_at=NOW + timedelta(minutes=7),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_PROVIDER_BINDING_EVENT
    ] == []
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0


def test_provider_binding_replay_rejects_same_provider_id_retargeted_in_registry() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, qualification, _readiness = _ready(store)
    registry = ProviderRegistry()
    provider = _HrisProvider("provider:hris:stable-id", qualification.request.capability)
    first_binding = _register(registry, provider)
    command = DomainEffectProviderBindingInput(
        run_ref=run_result.run_ref,
        provider_id=provider.descriptor.id,
        runtime_id="runtime:test-domain-effect",
        semantic_contract=_contract(provider.descriptor.id),
    )
    assessment = DomainEffectProviderBindingAssessment(store, registry)
    first = assessment.assess(command, assessed_at=NOW + timedelta(minutes=7))
    assert first.provider_execution_binding == first_binding

    registry.unregister(provider.descriptor.id)
    replacement = _HrisProvider(provider.descriptor.id, qualification.request.capability)
    second_binding = _register(
        registry,
        replacement,
        execution_identity="configured:hris:tenant-b",
        configuration_ref="config:hris:tenant-b:v1",
    )
    assert second_binding.id != first_binding.id

    with pytest.raises(ValueError, match="execution binding snapshot is stale"):
        assessment.assess(command, assessed_at=NOW + timedelta(minutes=8))

    assert store.list_authorization_uses() == []
    assert provider.invocations == 0
    assert replacement.invocations == 0


def test_provider_binding_requires_prior_readiness_and_does_not_create_it() -> None:
    store = InMemoryStateStore()
    _work, _authorization, run_result, qualification = _qualified(store)
    registry = ProviderRegistry()
    provider = _HrisProvider("provider:hris:no-readiness", qualification.request.capability)
    _register(registry, provider)

    with pytest.raises(ValueError, match="exactly one procedure readiness event"):
        DomainEffectProviderBindingAssessment(store, registry).assess(
            DomainEffectProviderBindingInput(
                run_ref=run_result.run_ref,
                provider_id=provider.descriptor.id,
                runtime_id="runtime:test-domain-effect",
                semantic_contract=_contract(provider.descriptor.id),
            ),
            assessed_at=NOW + timedelta(minutes=6),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type in {
            DOMAIN_EFFECT_PROVIDER_BINDING_EVENT,
            "domain-effect-procedure-readiness-assessed",
        }
    ] == []
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0
