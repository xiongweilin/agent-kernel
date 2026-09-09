from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)
from portable_runtime.core.provider_semantics import ProviderSemanticContract
from portable_runtime.core.reconciliation_repeatability import (
    ReconciliationRepeatabilityConfiguration,
)
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.domain_effect import (
    BoundedDomainEffectExecutionProfile,
    BoundedDomainEffectExecutionService,
    BoundedDomainEffectExecutionV1,
)
from portable_runtime.public_contracts.domain_effect_recovery import (
    BoundedDomainEffectRecoveryService,
    BoundedDomainEffectRecoveryV1,
)
from portable_runtime.public_contracts.http import create_public_app
from portable_runtime.stores.bounded_domain_effect_recovery import (
    BoundedDomainEffectRecoveryInMemoryStateStore,
)
from tests.conformance.public_contracts.test_second_administrative_capability import (
    APPROVAL_REF,
    EPOCH,
    GOVERNANCE_REF,
    GRANT_REF,
    IAM_CAPABILITY,
    INTENT_REF,
    NOW,
    SUBJECT_REF,
    _IamReadbackVerifier,
    _admitted_work,
    _contract_registry,
    _register_verifier,
)


class _FailResultCommitStore(BoundedDomainEffectRecoveryInMemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_result_commit_once = False

    def save_attempt(self, value: Any) -> None:
        if self.fail_result_commit_once and getattr(value, "status", None) == "succeeded":
            self.fail_result_commit_once = False
            raise RuntimeError("injected post-provider result projection failure")
        super().save_attempt(value)


class _RecoverableIamProvider:
    def __init__(self, provider_id: str, reality: dict[str, dict[str, Any]]) -> None:
        self.reality = reality
        self.invocations = 0
        self.reconciliations = 0
        self.request_subjects: dict[str, str] = {}
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name="Recoverable IAM provider",
            version="1.0.0",
            capabilities=[IAM_CAPABILITY],
            effect_semantics="reconcilable",
            side_effect_class="reconcilable",
            reversibility="compensatable",
            provider_family="iam",
            operator="test-provider",
            execution_domain="iam-write",
            credential_domain="iam-write-credentials",
            data_source_domain="iam-api",
            evaluation_domain="provider-execution",
            trust_boundary="test-reality",
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
        subject_ref = str(request.parameters["employee_ref"])
        self.request_subjects[request.id] = subject_ref
        self.reality[subject_ref] = {
            "target_system": "iam",
            "operation": "identity.create",
            "subject_ref": subject_ref,
            "active": True,
            "payload": dict(request.parameters),
        }
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            external_operation_ref=f"iam:{subject_ref}",
        )

    async def cancel(self, request_id: str) -> None:
        del request_id

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        self.reconciliations += 1
        subject_ref = self.request_subjects.get(request_id)
        if subject_ref is None or subject_ref not in self.reality:
            return None
        return CapabilityResult(
            request_id=request_id,
            provider_id=self.descriptor.id,
            status="succeeded",
            reconciled=True,
            external_operation_ref=f"iam:{subject_ref}",
        )


def _repeat_safe() -> ReconciliationRepeatabilityConfiguration:
    return ReconciliationRepeatabilityConfiguration(
        reconciliation_protocol_identity="test-request-readback",
        reconciliation_protocol_version="1",
        repeatability_mode="repeat-safe",
        contract_version="1",
    )


def _command(work_ref: str, expected_postcondition: dict[str, Any]) -> BoundedDomainEffectExecutionV1:
    return BoundedDomainEffectExecutionV1(
        schema="bounded-domain-effect-execution-v1",
        work_ref=work_ref,
        domain_intent_ref=INTENT_REF,
        domain_grant_ref=GRANT_REF,
        governance_basis_ref=GOVERNANCE_REF,
        approval_satisfaction_ref=APPROVAL_REF,
        capability=IAM_CAPABILITY,
        subject_ref=SUBJECT_REF,
        authority_epoch=EPOCH,
        parameters={
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
        },
        expected_postcondition=expected_postcondition,
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_provider_success_before_result_projection_recovers_without_redispatch() -> None:
    store = _FailResultCommitStore()
    work = _admitted_work(store)
    registry = ProviderRegistry()
    runtime = Runtime(
        store=store,
        registry=registry,
        contract_registry=_contract_registry(),
        runtime_id="runtime:ambiguous-provider-recovery",
    )

    reality: dict[str, dict[str, Any]] = {}
    effect_provider = _RecoverableIamProvider("provider:iam:recoverable", reality)
    registry.register(
        effect_provider,
        configured_execution_identity="configured:iam:recoverable:v1",
        authoritative_configuration_ref="config:iam:recoverable:v1",
        reconciliation_repeatability=_repeat_safe(),
    )
    expected = {
        "target_system": "iam",
        "operation": "identity.create",
        "subject_ref": SUBJECT_REF,
        "active": True,
        "payload": {
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
        },
    }
    verifier = _IamReadbackVerifier("provider:iam:recovery-readback", reality)
    _register_verifier(registry, verifier)

    execution = BoundedDomainEffectExecutionService(
        runtime,
        [
            BoundedDomainEffectExecutionProfile(
                capability=IAM_CAPABILITY,
                provider_id=effect_provider.descriptor.id,
                verifier_provider_id=verifier.descriptor.id,
                semantic_contract=ProviderSemanticContract(
                    id="semantic:iam:recoverable",
                    version="1",
                    provider_id=effect_provider.descriptor.id,
                ),
                lease_owner="kernel:ambiguous-provider-recovery",
            )
        ],
    )
    command = _command(work.id, expected)

    store.fail_result_commit_once = True
    unknown = await execution.execute(command)

    assert unknown.status == "execution-unknown"
    assert effect_provider.invocations == 1
    assert SUBJECT_REF in reality
    assert runtime.get_work(work.id).status != "completed"
    assert unknown.run_ref is not None
    assert unknown.request_ref is not None

    attempts = [
        attempt
        for step in store.list_steps(unknown.run_ref)
        for attempt in store.list_attempts(step.id)
        if attempt.request_ref == unknown.request_ref
    ]
    assert len(attempts) == 1
    assert attempts[0].status != "succeeded"

    replay = await execution.execute(command)
    assert replay == unknown
    assert effect_provider.invocations == 1

    recovery = BoundedDomainEffectRecoveryService(execution)
    resolved = await recovery.recover(
        BoundedDomainEffectRecoveryV1(
            schema="bounded-domain-effect-recovery-v1",
            execution_ref=unknown.execution_ref,
        )
    )

    assert resolved.original_status == "execution-unknown"
    assert resolved.current_status == "recovered-completed"
    assert resolved.recovery_application_ref
    assert resolved.recovery_observation_ref
    assert resolved.outcome_ref
    assert resolved.evidence_ref
    assert effect_provider.invocations == 1
    assert effect_provider.reconciliations == 1
    assert verifier.invocations == 1
    assert runtime.get_work(work.id).status == "completed"

    # Historical execution projection stays immutable; current truth is the
    # separate resolution view.
    assert execution.inspect(unknown.execution_ref) == unknown
    current = recovery.inspect(unknown.execution_ref)
    assert current is not None
    assert current.current_status == "recovered-completed"

    recovered_replay = await recovery.recover(
        BoundedDomainEffectRecoveryV1(
            schema="bounded-domain-effect-recovery-v1",
            execution_ref=unknown.execution_ref,
        )
    )
    assert recovered_replay == current
    assert effect_provider.invocations == 1
    assert effect_provider.reconciliations == 1

    # The public API preserves immutable history and exposes recovery only via
    # the explicit current-resolution projection.
    client = TestClient(
        create_public_app(runtime, bounded_domain_effect_execution=execution)
    )
    historical = client.get(f"/v1/domain-effects/executions/{unknown.execution_ref}")
    assert historical.status_code == 200
    assert historical.json()["status"] == "execution-unknown"

    resolution = client.get(
        f"/v1/domain-effects/executions/{unknown.execution_ref}/resolution"
    )
    assert resolution.status_code == 200
    assert resolution.json()["current_status"] == "recovered-completed"

    replay_via_http = client.post(
        "/v1/domain-effects/recoveries",
        json={
            "schema": "bounded-domain-effect-recovery-v1",
            "execution_ref": unknown.execution_ref,
        },
    )
    assert replay_via_http.status_code == 200
    assert replay_via_http.json()["current_status"] == "recovered-completed"
    assert effect_provider.invocations == 1
    assert effect_provider.reconciliations == 1
