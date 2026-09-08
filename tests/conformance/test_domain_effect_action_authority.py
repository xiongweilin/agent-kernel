from __future__ import annotations

from datetime import timedelta

import pytest

from portable_runtime.core.qualification import InvocationPermit
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)
from portable_runtime.responsibility.domain_effect_invocation_specification import (
    DomainEffectInvocationSpecificationCapture,
    DomainEffectInvocationSpecificationInput,
)
from portable_runtime.stores.invocation_specification import (
    InvocationSpecificationInMemoryStateStore,
)
from tests.conformance.test_domain_effect_invocation_specification import _bound
from tests.conformance.test_domain_effect_procedure_readiness import NOW
from tests.conformance.test_domain_effect_provider_binding import _HrisProvider, _register


def _action_ready():
    store = InvocationSpecificationInMemoryStateStore()
    (
        _work,
        authorization,
        run_result,
        qualification,
        readiness,
        registry,
        provider,
        configured_binding,
        provider_assessment,
        _provider_binding,
    ) = _bound(store)
    specification = DomainEffectInvocationSpecificationCapture(
        store,
        provider_assessment,
    ).capture(
        DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref),
        captured_at=NOW + timedelta(minutes=8),
    )
    return (
        store,
        authorization,
        run_result,
        qualification,
        readiness,
        registry,
        provider,
        configured_binding,
        specification,
    )


def test_action_authority_is_derived_from_kernel_facts_without_consuming_use() -> None:
    (
        store,
        authorization,
        _run_result,
        qualification,
        readiness,
        registry,
        provider,
        configured_binding,
        specification_result,
    ) = _action_ready()
    resolver = DomainEffectActionAuthorityResolver(
        store,
        registry,
        now=lambda: NOW + timedelta(minutes=9),
    )

    binding = resolver.resolve(qualification.request)

    assert binding.provider_id == provider.descriptor.id
    assert binding.provider_execution_binding_ref == configured_binding.id
    assert binding.invocation_specification_ref == specification_result.specification_ref
    assert binding.authorization_ref == authorization.authorization_ref
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0

    permit = InvocationPermit.issue(
        qualification.request,
        provider_id=binding.provider_id,
        qualification_digest=readiness.readiness_digest,
        lease_generation=qualification.request.lease_generation,
        governance_applicable=False,
    )
    candidate = binding.authorization_use_factory(
        qualification.request,
        permit,
        configured_binding,
        specification_result.specification,
    )
    assert candidate.authorization_ref == authorization.authorization_ref
    assert candidate.capability == qualification.request.capability
    assert candidate.resource_ref == qualification.request.resource_ref
    assert candidate.subject_version_refs == qualification.request.subject_version_refs
    assert store.list_authorization_uses() == []
    assert provider.invocations == 0


def test_action_authority_rejects_exact_provider_retargeting() -> None:
    (
        store,
        _authorization,
        _run_result,
        qualification,
        _readiness,
        registry,
        provider,
        first_binding,
        _specification_result,
    ) = _action_ready()
    registry.unregister(provider.descriptor.id)
    replacement = _HrisProvider(provider.descriptor.id, qualification.request.capability)
    second_binding = _register(
        registry,
        replacement,
        execution_identity="configured:hris:action-retargeted",
        configuration_ref="config:hris:action-retargeted:v1",
    )
    assert second_binding.id != first_binding.id

    with pytest.raises(ValueError, match="exact configured provider target changed"):
        DomainEffectActionAuthorityResolver(
            store,
            registry,
            now=lambda: NOW + timedelta(minutes=9),
        ).resolve(qualification.request)

    assert store.list_authorization_uses() == []
    assert provider.invocations == 0
    assert replacement.invocations == 0


def test_action_authority_rejects_request_semantic_rebinding() -> None:
    (
        store,
        _authorization,
        _run_result,
        qualification,
        _readiness,
        registry,
        provider,
        _configured_binding,
        _specification_result,
    ) = _action_ready()
    changed_parameters = dict(qualification.request.parameters)
    changed_parameters["department_ref"] = "department:finance"
    rebound = qualification.request.model_copy(update={"parameters": changed_parameters})

    with pytest.raises(ValueError, match="parameters rebound"):
        DomainEffectActionAuthorityResolver(
            store,
            registry,
            now=lambda: NOW + timedelta(minutes=9),
        ).resolve(rebound)

    assert store.list_authorization_uses() == []
    assert provider.invocations == 0
