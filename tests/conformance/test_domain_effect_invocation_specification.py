from __future__ import annotations

from datetime import timedelta

import pytest

from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.responsibility.domain_effect_invocation_specification import (
    DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT,
    DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA,
    DomainEffectInvocationSpecificationCapture,
    DomainEffectInvocationSpecificationInput,
)
from portable_runtime.responsibility.domain_effect_provider_binding import (
    DomainEffectProviderBindingAssessment,
    DomainEffectProviderBindingInput,
)
from portable_runtime.stores.invocation_specification import (
    InvocationSpecificationInMemoryStateStore,
)
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.workflows.invocation_specification import (
    INVOCATION_SPECIFICATION_EVENT,
    INVOCATION_SPECIFICATION_SCHEMA,
)
from tests.conformance.test_domain_effect_procedure_readiness import NOW
from tests.conformance.test_domain_effect_provider_binding import (
    _HrisProvider,
    _contract,
    _ready,
    _register,
)


def _bound(store):
    work, authorization, run_result, qualification, readiness = _ready(store)
    registry = ProviderRegistry()
    provider = _HrisProvider(
        "provider:hris:specification",
        qualification.request.capability,
    )
    configured_binding = _register(registry, provider)
    provider_assessment = DomainEffectProviderBindingAssessment(store, registry)
    provider_binding = provider_assessment.assess(
        DomainEffectProviderBindingInput(
            run_ref=run_result.run_ref,
            provider_id=provider.descriptor.id,
            runtime_id="runtime:test-domain-effect-specification",
            semantic_contract=_contract(provider.descriptor.id),
        ),
        assessed_at=NOW + timedelta(minutes=7),
    )
    return (
        work,
        authorization,
        run_result,
        qualification,
        readiness,
        registry,
        provider,
        configured_binding,
        provider_assessment,
        provider_binding,
    )


def test_invocation_specification_commits_exact_bound_semantics_without_execution() -> None:
    store = InvocationSpecificationInMemoryStateStore()
    (
        work,
        _authorization,
        run_result,
        qualification,
        readiness,
        _registry,
        provider,
        configured_binding,
        provider_assessment,
        provider_binding,
    ) = _bound(store)
    capture = DomainEffectInvocationSpecificationCapture(store, provider_assessment)
    command = DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref)

    result = capture.capture(command, captured_at=NOW + timedelta(minutes=8))
    replay = capture.capture(command, captured_at=NOW + timedelta(minutes=8, seconds=30))

    assert replay == result
    assert result.status == "recorded"
    assert result.authority_bearing is False
    assert result.execution_authority_bearing is False
    assert result.work_ref == work.id
    assert result.qualification_event_ref == qualification.qualification_event_ref
    assert result.readiness_event_ref == readiness.readiness_event_ref
    assert result.provider_binding_event_ref == provider_binding.binding_event_ref
    assert result.provider_execution_binding_ref == configured_binding.id

    specification = result.specification
    assert specification.id == result.specification_ref
    assert specification.source_request_ref == qualification.request.id
    assert specification.source_work_ref == qualification.request.work_id
    assert specification.source_run_ref == qualification.request.run_id
    assert specification.provider_binding == provider_binding.provider_replay_binding
    assert specification.semantic_identity == provider_binding.semantic_projection.identity
    assert (
        specification.canonical_semantic_payload
        == provider_binding.semantic_projection.canonical_payload
    )
    assert specification.semantic_contract_digest == provider_binding.semantic_contract.digest
    assert specification.effect_semantics == provider_binding.provider_descriptor.effect_semantics
    assert result.semantic_identity == specification.semantic_identity
    assert result.semantic_contract_digest == specification.semantic_contract_digest

    authority_event = store.get_event(specification.id)
    assert authority_event is not None
    assert authority_event.type == INVOCATION_SPECIFICATION_EVENT
    assert authority_event.subject_ref == qualification.request.id
    assert authority_event.payload["schema"] == INVOCATION_SPECIFICATION_SCHEMA
    assert store.get_invocation_specification(specification.id) == specification

    links = [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
    ]
    assert len(links) == 1
    link = links[0]
    assert link.id == result.link_event_ref
    assert link.created_at == NOW + timedelta(minutes=8)
    assert link.payload["schema"] == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA
    assert link.payload["authority_bearing"] is False
    assert link.payload["execution_authority_bearing"] is False
    assert link.payload["specification_ref"] == specification.id
    assert link.payload["provider_execution_binding_ref"] == configured_binding.id

    run = store.get_run(run_result.run_ref)
    assert run is not None
    assert run.provider_invocation_refs == []
    assert run.metadata["provider_selection"] == "not-performed"
    assert run.metadata["invocation_permit"] == "not-issued"
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0


def test_invocation_specification_requires_authority_capable_store() -> None:
    store = InMemoryStateStore()
    (
        _work,
        _authorization,
        run_result,
        _qualification,
        _readiness,
        _registry,
        provider,
        _configured_binding,
        provider_assessment,
        _provider_binding,
    ) = _bound(store)

    with pytest.raises(ValueError, match="authority-capable specification store"):
        DomainEffectInvocationSpecificationCapture(store, provider_assessment).capture(
            DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref),
            captured_at=NOW + timedelta(minutes=8),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
    ] == []
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0


def test_invocation_specification_rejects_provider_retarget_before_capture() -> None:
    store = InvocationSpecificationInMemoryStateStore()
    (
        _work,
        _authorization,
        run_result,
        qualification,
        _readiness,
        registry,
        provider,
        first_binding,
        provider_assessment,
        _provider_binding,
    ) = _bound(store)

    registry.unregister(provider.descriptor.id)
    replacement = _HrisProvider(provider.descriptor.id, qualification.request.capability)
    second_binding = _register(
        registry,
        replacement,
        execution_identity="configured:hris:specification-retargeted",
        configuration_ref="config:hris:specification-retargeted:v1",
    )
    assert second_binding.id != first_binding.id

    with pytest.raises(ValueError, match="execution binding snapshot is stale"):
        DomainEffectInvocationSpecificationCapture(store, provider_assessment).capture(
            DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref),
            captured_at=NOW + timedelta(minutes=8),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
    ] == []
    assert [
        event
        for event in store.export_state()["event"]
        if event.get("type") == INVOCATION_SPECIFICATION_EVENT
    ] == []
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0
    assert replacement.invocations == 0


def test_invocation_specification_requires_prior_provider_binding() -> None:
    store = InvocationSpecificationInMemoryStateStore()
    _work, _authorization, run_result, _qualification, _readiness = _ready(store)
    registry = ProviderRegistry()
    provider_assessment = DomainEffectProviderBindingAssessment(store, registry)

    with pytest.raises(ValueError, match="exactly one provider binding event"):
        DomainEffectInvocationSpecificationCapture(store, provider_assessment).capture(
            DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref),
            captured_at=NOW + timedelta(minutes=7),
        )

    assert [
        event
        for event in store.list_events(run_result.run_ref)
        if event.type == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
    ] == []
    assert store.list_authorization_uses() == []
