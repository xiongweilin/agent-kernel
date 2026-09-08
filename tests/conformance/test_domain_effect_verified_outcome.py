from __future__ import annotations

from typing import Any, Literal

import pytest

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)
from portable_runtime.records.models import EvidenceArtifact, OutcomeRecord
from portable_runtime.records.open_validation import ClosedVerificationResult
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_reality_execution import (
    DomainEffectRealityExecution,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DOMAIN_EFFECT_VERIFICATION_CAPABILITY,
    DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA,
    DomainEffectVerifiedOutcomeVerification,
)
from tests.conformance.test_domain_effect_reality_cutover import _live_cutover_fixture


class _ReadbackVerifier:
    def __init__(
        self,
        provider_id: str,
        reality: dict[str, dict[str, Any]],
        *,
        forced_result: Literal["pass", "fail"] | None = None,
    ) -> None:
        self.reality = reality
        self.forced_result = forced_result
        self.invocations = 0
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name="HRIS independent readback verifier",
            version="1.0.0",
            capabilities=[DOMAIN_EFFECT_VERIFICATION_CAPABILITY],
            effect_semantics="pure",
            side_effect_class="pure",
            reversibility="unknown",
            provider_family="hris-readback",
            operator="test-verifier",
            execution_domain="verification",
            credential_domain="verification-credentials",
            data_source_domain="hris-read-api",
            evaluation_domain="objective-postcondition",
            trust_boundary="test-verification",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True)

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        del context
        self.invocations += 1
        scope = request.parameters.get("verification_scope")
        if not isinstance(scope, dict):
            raise ValueError("verification_scope required")
        subject_ref = scope.get("subject_ref")
        expected = scope.get("expected_postcondition")
        if not isinstance(subject_ref, str) or not isinstance(expected, dict):
            raise ValueError("verification scope is incomplete")
        observed = dict(self.reality.get(subject_ref, {}))
        result = self.forced_result or ("pass" if observed == expected else "fail")
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            metadata={"observed_postcondition": observed},
            verification_result=ClosedVerificationResult(
                result=result,
                message="independent HRIS readback",
            ),
        )

    async def cancel(self, request_id: str) -> None:
        del request_id

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        del request_id
        return None


def _register_verifier(registry, verifier: _ReadbackVerifier):
    registry.register(
        verifier,
        configured_execution_identity=f"configured:{verifier.descriptor.id}",
        authoritative_configuration_ref=f"config:{verifier.descriptor.id}:v1",
    )
    return registry.execution_binding(verifier.descriptor.id)


async def _executed_effect():
    (
        store,
        qualification,
        registry,
        effect_provider,
        _effect_binding,
        _provider_binding,
        _specification,
    ) = _live_cutover_fixture()
    boundary = RealityBoundary(store=store, registry=registry)
    executor = DomainEffectRealityExecution(
        boundary,
        DomainEffectActionAuthorityResolver(store, registry),
    )
    effect_result = await executor.execute(qualification.request)
    assert effect_result.status == "succeeded"
    assert effect_provider.invocations == 1
    return store, qualification, registry, effect_provider, boundary


def _authorization_context(store, request: CapabilityRequest):
    run = store.get_run(request.run_id)
    assert run is not None
    authorization_ref = run.metadata["domain_effect_authorization_ref"]
    return DomainEffectAuthorizationUseConsumption(store)._resolve_context(
        authorization_ref
    )


def _domain_verification_evidence(store) -> list[EvidenceArtifact]:
    return [
        record
        for record in store.list_records("EvidenceArtifact")
        if isinstance(record, EvidenceArtifact)
        and record.metadata.get("schema") == DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA
    ]


def _confirmed_outcomes(store) -> list[OutcomeRecord]:
    return [
        record
        for record in store.list_records("Outcome")
        if isinstance(record, OutcomeRecord) and record.lifecycle_status == "confirmed"
    ]


@pytest.mark.asyncio
async def test_independent_readback_pass_confirms_outcome_without_terminalizing_run() -> None:
    store, qualification, registry, effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    reality = {
        context.intent.subject_ref: dict(context.intent.expected_postcondition),
    }
    verifier = _ReadbackVerifier("provider:hris:readback-pass", reality)
    verifier_binding = _register_verifier(registry, verifier)

    result = await DomainEffectVerifiedOutcomeVerification(
        boundary,
        verifier_provider_id=verifier.descriptor.id,
    ).verify_and_confirm(qualification.request)

    assert result.objective_result == "pass"
    assert result.verifier_provider_execution_binding_ref == verifier_binding.id
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1
    evidence = _domain_verification_evidence(store)
    assert [item.id for item in evidence] == [result.evidence_ref]
    assert evidence[0].metadata["verification_result"]["result"] == "pass"
    assert evidence[0].metadata["observed_postcondition"] == context.intent.expected_postcondition
    outcomes = _confirmed_outcomes(store)
    assert [item.id for item in outcomes] == [result.outcome_ref]
    assert outcomes[0].metadata["objective_result"] == "pass"
    run = store.get_run(qualification.request.run_id)
    assert run is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_independent_readback_fail_confirms_explicit_not_satisfied_outcome() -> None:
    store, qualification, registry, effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    reality = {
        context.intent.subject_ref: {
            **context.intent.expected_postcondition,
            "active": False,
        }
    }
    verifier = _ReadbackVerifier("provider:hris:readback-fail", reality)
    _register_verifier(registry, verifier)

    result = await DomainEffectVerifiedOutcomeVerification(
        boundary,
        verifier_provider_id=verifier.descriptor.id,
    ).verify_and_confirm(qualification.request)

    assert result.objective_result == "fail"
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1
    evidence = _domain_verification_evidence(store)
    assert len(evidence) == 1
    assert evidence[0].metadata["verification_result"]["result"] == "fail"
    outcomes = _confirmed_outcomes(store)
    assert len(outcomes) == 1
    assert outcomes[0].metadata["objective_result"] == "fail"
    run = store.get_run(qualification.request.run_id)
    assert run is not None
    assert run.status == "running"


@pytest.mark.asyncio
async def test_verifier_pass_that_contradicts_observed_reality_cannot_create_authority() -> None:
    store, qualification, registry, _effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    reality = {
        context.intent.subject_ref: {
            **context.intent.expected_postcondition,
            "active": False,
        }
    }
    verifier = _ReadbackVerifier(
        "provider:hris:dishonest-readback",
        reality,
        forced_result="pass",
    )
    _register_verifier(registry, verifier)

    with pytest.raises(ValueError, match="pass contradicts"):
        await DomainEffectVerifiedOutcomeVerification(
            boundary,
            verifier_provider_id=verifier.descriptor.id,
        ).verify_and_confirm(qualification.request)

    assert verifier.invocations == 1
    assert _domain_verification_evidence(store) == []
    assert _confirmed_outcomes(store) == []


@pytest.mark.asyncio
async def test_effect_success_alone_never_materializes_confirmed_outcome() -> None:
    store, _qualification, _registry, effect_provider, _boundary = await _executed_effect()

    assert effect_provider.invocations == 1
    assert _domain_verification_evidence(store) == []
    assert _confirmed_outcomes(store) == []
