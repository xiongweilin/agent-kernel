from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)
from portable_runtime.core.capability_contract import CapabilityContract, CapabilityContractRegistry
from portable_runtime.core.provider_semantics import ProviderSemanticContract
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.domain_effect import (
    BoundedDomainEffectExecutionProfile,
    BoundedDomainEffectExecutionService,
    BoundedDomainEffectExecutionV1,
)
from portable_runtime.records.open_validation import ClosedVerificationResult
from portable_runtime.responsibility.admission import (
    BoundedLocalResponsibilityAdmissionPolicy,
    admit_responsibility_proposal,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    domain_effect_verification_capability,
)
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel
from portable_runtime.stores.invocation_specification import (
    InvocationSpecificationInMemoryStateStore,
)
from tests.conformance.test_domain_effect_provider_binding import _HrisProvider, _register

NOW = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
IAM_CAPABILITY = "administrative.iam.identity.create.v1"
CASE_REF = "case-iam-1"
RESP_REF = "resp-admin-iam-1"
ASSESSMENT_REF = "assessment-admin-iam-1"
PROPOSAL_REF = "proposal-admin-iam-1"
INTENT_REF = "intent-iam-1"
GRANT_REF = "grant-iam-1"
GOVERNANCE_REF = "governance-iam-1"
APPROVAL_REF = "approval-iam-1"
SUBJECT_REF = "employee:iam-new"
EPOCH = 3


class _IamReadbackVerifier:
    def __init__(self, provider_id: str, reality: dict[str, dict[str, Any]]) -> None:
        self.reality = reality
        self.invocations = 0
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name="IAM independent readback verifier",
            version="1.0.0",
            capabilities=[domain_effect_verification_capability(IAM_CAPABILITY)],
            effect_semantics="pure",
            side_effect_class="pure",
            reversibility="unknown",
            provider_family="iam-readback",
            operator="test-verifier",
            execution_domain="verification",
            credential_domain="verification-credentials",
            data_source_domain="iam-read-api",
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
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            metadata={"observed_postcondition": observed},
            verification_result=ClosedVerificationResult(
                result="pass" if observed == expected else "fail",
                message="independent IAM readback",
            ),
        )

    async def cancel(self, request_id: str) -> None:
        del request_id

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        del request_id
        return None


def _contract_registry() -> CapabilityContractRegistry:
    return CapabilityContractRegistry(
        contracts=[
            CapabilityContract(
                capability=IAM_CAPABILITY,
                minimum_impact_class="write-remote",
                effect_semantics="reconcilable",
                reversibility="compensatable",
                authorization_requirement="required",
                minimum_procedure_profile="standard",
                resource_required=True,
                subject_version_required=True,
                default_independence_requirements=[],
                blast_radius=1,
                exposure=1,
            )
        ]
    )


def _work_policy() -> BoundedLocalResponsibilityAdmissionPolicy:
    return BoundedLocalResponsibilityAdmissionPolicy(
        profile_id="administrative-iam-test",
        version="1",
        max_request=ResourceVector(
            api_calls=2,
            concurrency_slots=1,
            domain_quota={"administrative:iam": 1},
        ),
        capacity=ResourceVector(
            api_calls=8,
            concurrency_slots=4,
            domain_quota={"administrative:iam": 4},
        ),
    )


def _admitted_work(store: InvocationSpecificationInMemoryStateStore):
    kernel = ResponsibilityKernel(store)
    kernel.register(
        StandingResponsibility(
            id=RESP_REF,
            created_at=NOW,
            responsibility_kind="administrative-obligation",
            statement="Discharge governed onboarding IAM identity creation",
            scope={
                "administrative_case_id": CASE_REF,
                "authority_epoch": str(EPOCH),
                "obligation_id": "obligation-iam-1",
                "governance_basis_id": GOVERNANCE_REF,
                "execution_grant_id": GRANT_REF,
                "target_system": "iam",
                "operation": "identity.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-iam-1",
            created_at=NOW,
            responsibility_ref=RESP_REF,
            responsibility_version=1,
            principal_ref="service:administrative-orchestrator",
            basis_refs=[
                f"administrative-grant:{GRANT_REF}",
                f"governance-basis:{GOVERNANCE_REF}",
                f"approval-satisfaction:{APPROVAL_REF}",
            ],
            admitted_at=NOW,
        ),
    )
    record_domain_assessment(
        kernel,
        ResponsibilityAssessment(
            id=ASSESSMENT_REF,
            created_at=NOW,
            responsibility_ref=RESP_REF,
            responsibility_version=1,
            subject_ref=SUBJECT_REF,
            assessment_kind="administrative-obligation-ready",
            basis_refs=[
                f"administrative-intent:{INTENT_REF}",
                f"administrative-grant:{GRANT_REF}",
                f"governance-basis:{GOVERNANCE_REF}",
            ],
            assessed_at=NOW,
            fresh_until=NOW + timedelta(hours=1),
            rationale="domain policy, approval and IAM obligation are closed",
        ),
        now=NOW,
    )
    proposal = WorkProposal(
        id=PROPOSAL_REF,
        created_at=NOW,
        responsibility_ref=RESP_REF,
        responsibility_version=1,
        assessment_ref=ASSESSMENT_REF,
        subject_ref=SUBJECT_REF,
        work_kind="administrative-effect",
        title="iam: identity.create employee:iam-new",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:iam": 1},
        ),
        requested_capabilities=[IAM_CAPABILITY],
        expected_result="IAM identity exists with governed onboarding fields",
        effect_class=EffectClass.EXTERNAL_EFFECT,
        fresh_until=NOW + timedelta(hours=1),
    )
    kernel.propose(proposal, now=NOW)
    admitted = admit_responsibility_proposal(
        kernel,
        proposal.id,
        policy=_work_policy(),
        now=NOW,
    )
    assert admitted.status == "work-materialized"
    assert admitted.work_ref is not None
    work = store.get_work(admitted.work_ref)
    assert work is not None
    return work


def _register_verifier(registry: ProviderRegistry, verifier: _IamReadbackVerifier) -> None:
    registry.register(
        verifier,
        configured_execution_identity=f"configured:{verifier.descriptor.id}",
        authoritative_configuration_ref=f"config:{verifier.descriptor.id}:v1",
    )


@pytest.mark.asyncio
async def test_second_administrative_capability_runs_same_bounded_kernel_pipeline() -> None:
    store = InvocationSpecificationInMemoryStateStore()
    work = _admitted_work(store)
    registry = ProviderRegistry()
    contracts = _contract_registry()
    runtime = Runtime(
        store=store,
        registry=registry,
        contract_registry=contracts,
        runtime_id="runtime:second-administrative-capability",
    )

    effect_provider = _HrisProvider("provider:iam:identity-create", IAM_CAPABILITY)
    _register(
        registry,
        effect_provider,
        execution_identity="configured:iam:tenant-a",
        configuration_ref="config:iam:tenant-a:v1",
    )
    expected_postcondition = {
        "target_system": "iam",
        "operation": "identity.create",
        "subject_ref": SUBJECT_REF,
        "active": True,
        "payload": {
            "employee_ref": SUBJECT_REF,
            "department_ref": "department:engineering",
        },
    }
    verifier = _IamReadbackVerifier(
        "provider:iam:identity-readback",
        {SUBJECT_REF: dict(expected_postcondition)},
    )
    _register_verifier(registry, verifier)

    service = BoundedDomainEffectExecutionService(
        runtime,
        [
            BoundedDomainEffectExecutionProfile(
                capability=IAM_CAPABILITY,
                provider_id=effect_provider.descriptor.id,
                verifier_provider_id=verifier.descriptor.id,
                semantic_contract=ProviderSemanticContract(
                    id="semantic:iam:identity-create",
                    version="1",
                    provider_id=effect_provider.descriptor.id,
                ),
                lease_owner="kernel:iam-second-capability-test",
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

    receipt = await service.execute(command)
    replay = await service.execute(command)

    assert receipt == replay
    assert receipt.status == "completed"
    assert receipt.work_ref == work.id
    assert receipt.run_ref
    assert receipt.action_ref
    assert receipt.outcome_ref
    assert receipt.evidence_ref
    assert receipt.responsibility_ref == RESP_REF
    assert effect_provider.invocations == 1
    assert verifier.invocations == 1
    assert runtime.get_work(work.id).status == "completed"
