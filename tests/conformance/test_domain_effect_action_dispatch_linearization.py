from __future__ import annotations

import hashlib
from datetime import timedelta

from portable_runtime.core.boundary_stages import precommit_execution_records
from portable_runtime.core.qualification import InvocationPermit
from portable_runtime.governance.dispatch import (
    DISPATCH_COMMIT_EVENT,
    GovernanceDispatchCommitter,
)
from portable_runtime.records.authorization import create_authorization_use
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
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


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class _FailDispatchStore(InvocationSpecificationInMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_dispatch = False

    def append_event(self, value):
        if self.fail_dispatch and value.type == DISPATCH_COMMIT_EVENT:
            raise RuntimeError("injected dispatch journal failure")
        return super().append_event(value)


def _prepared(store: InvocationSpecificationInMemoryStateStore):
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
    request = qualification.request
    permit = InvocationPermit.issue(
        request,
        provider_id=provider.descriptor.id,
        qualification_digest=readiness.readiness_digest,
        lease_generation=run_result.lease_generation,
        governance_applicable=False,
    )
    precommit = precommit_execution_records(
        store,
        permit.materialize_request(),
        provider_id=provider.descriptor.id,
        permit_digest=permit.request_digest,
        lease_generation=run_result.lease_generation,
        side_effect=True,
        side_effect_class=provider.descriptor.side_effect_class,
        effect_semantics=provider.descriptor.effect_semantics,
        reversibility=provider.descriptor.reversibility,
    )
    assert precommit.error is None
    assert precommit.records.attempt_id is not None
    return (
        authorization,
        request,
        permit,
        specification,
        registry,
        provider,
        configured_binding,
        precommit.records,
    )


def _factory(store, authorization_ref: str, *, authorized_at):
    consumer = DomainEffectAuthorizationUseConsumption(store)

    def build(request, _permit, execution_binding, specification):
        assert execution_binding is not None
        assert specification is not None
        context = consumer._resolve_context(authorization_ref)
        created = create_authorization_use(
            context.grant,
            context.request,
            authorized_at=authorized_at,
        )
        use_id = _stable_id(
            "authuse_domain_effect",
            context.grant.id,
            context.evidence.id,
            context.admission.subject_version_ref,
        )
        use = created.model_copy(update={"id": use_id, "created_at": created.authorized_at})
        assert use.capability == request.capability
        return use

    return build


def test_action_authority_and_dispatch_commit_linearize_together() -> None:
    store = InvocationSpecificationInMemoryStateStore()
    (
        authorization,
        request,
        permit,
        specification,
        registry,
        provider,
        configured_binding,
        records,
    ) = _prepared(store)

    provider_object = registry.get(provider.descriptor.id)
    decision = GovernanceDispatchCommitter(store).commit(
        permit.materialize_request(),
        permit,
        None,
        attempt_id=records.attempt_id,
        provider_registry=registry,
        expected_provider=provider_object,
        authorization_use_factory=_factory(
            store,
            authorization.authorization_ref,
            authorized_at=NOW + timedelta(minutes=9),
        ),
        invocation_specification_ref=specification.specification_ref,
    )

    assert decision.status == "committed"
    assert decision.commit_ref is not None
    assert decision.authorization_use_ref is not None
    assert decision.invocation_specification_ref == specification.specification_ref
    assert decision.provider_execution_binding_ref == configured_binding.id

    use = store.get_authorization_use(decision.authorization_use_ref)
    event = store.get_event(decision.commit_ref)
    attempt = store.get_attempt(records.attempt_id)
    assert use is not None
    assert event is not None
    assert attempt is not None
    assert event.type == DISPATCH_COMMIT_EVENT
    assert event.payload["authorization_use_ref"] == use.id
    assert event.payload["invocation_specification_ref"] == specification.specification_ref
    assert event.payload["provider_execution_binding_ref"] == configured_binding.id
    assert attempt.metadata["dispatch_commit_ref"] == event.id
    assert attempt.metadata["authorization_use_ref"] == use.id
    assert attempt.metadata["invocation_specification_ref"] == specification.specification_ref
    assert provider.invocations == 0


def test_dispatch_journal_failure_rolls_back_authorization_use_and_attempt_binding() -> None:
    store = _FailDispatchStore()
    (
        authorization,
        _request,
        permit,
        specification,
        registry,
        provider,
        _configured_binding,
        records,
    ) = _prepared(store)
    before_attempt = store.get_attempt(records.attempt_id)
    assert before_attempt is not None
    assert "dispatch_commit_ref" not in before_attempt.metadata
    store.fail_dispatch = True

    provider_object = registry.get(provider.descriptor.id)
    decision = GovernanceDispatchCommitter(store).commit(
        permit.materialize_request(),
        permit,
        None,
        attempt_id=records.attempt_id,
        provider_registry=registry,
        expected_provider=provider_object,
        authorization_use_factory=_factory(
            store,
            authorization.authorization_ref,
            authorized_at=NOW + timedelta(minutes=9),
        ),
        invocation_specification_ref=specification.specification_ref,
    )

    assert decision.status == "unavailable"
    assert "injected dispatch journal failure" in decision.reason
    assert store.list_authorization_uses() == []
    after_attempt = store.get_attempt(records.attempt_id)
    assert after_attempt is not None
    assert "dispatch_commit_ref" not in after_attempt.metadata
    assert "authorization_use_ref" not in after_attempt.metadata
    assert [
        event
        for event in store.export_state()["event"]
        if event.get("type") == DISPATCH_COMMIT_EVENT
    ] == []
    assert provider.invocations == 0
