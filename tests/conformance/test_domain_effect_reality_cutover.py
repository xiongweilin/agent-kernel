from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.governance.dispatch import DISPATCH_COMMIT_EVENT
from portable_runtime.responsibility.admission import admit_responsibility_proposal
from portable_runtime.responsibility.admission_profiles import (
    administrative_public_responsibility_admission_policy,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)
from portable_runtime.responsibility.domain_effect_activation import (
    DomainEffectRunActivation,
    DomainEffectRunActivationInput,
)
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
    DomainEffectAuthorizationAdmission,
    DomainEffectIntentEvidenceInput,
)
from portable_runtime.responsibility.domain_effect_invocation_specification import (
    DomainEffectInvocationSpecificationCapture,
    DomainEffectInvocationSpecificationInput,
)
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessInput,
)
from portable_runtime.responsibility.domain_effect_provider_binding import (
    DomainEffectProviderBindingAssessment,
    DomainEffectProviderBindingInput,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DomainEffectQualificationAssessment,
    DomainEffectQualificationInput,
)
from portable_runtime.responsibility.domain_effect_reality_execution import (
    DomainEffectRealityExecution,
)
from portable_runtime.responsibility.domain_effect_request import (
    DomainEffectExecutionRequestPreparation,
    DomainEffectExecutionRequestPreparationInput,
)
from portable_runtime.responsibility.domain_effect_run import (
    DomainEffectRunPreparation,
    DomainEffectRunPreparationInput,
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
from tests.conformance.test_domain_effect_provider_binding import (
    _HrisProvider,
    _contract,
    _register,
)


def _live_cutover_fixture():
    store = InvocationSpecificationInMemoryStateStore()
    base = datetime.now(UTC) - timedelta(seconds=20)
    responsibility_ref = "resp-admin-live-cutover-1"
    assessment_ref = "assessment-admin-live-cutover-1"
    proposal_ref = "proposal-admin-live-cutover-1"
    intent_ref = "intent-live-cutover-1"
    domain_grant_ref = "grant-live-cutover-1"
    governance_ref = "governance-live-cutover-1"
    approval_ref = "approval-live-cutover-1"
    subject_ref = "employee:live-cutover"
    case_ref = "case-live-cutover-1"
    epoch = 1
    owner = "runtime-worker:live-cutover-1"

    kernel = ResponsibilityKernel(store)
    kernel.register(
        StandingResponsibility(
            id=responsibility_ref,
            created_at=base,
            responsibility_kind="administrative-obligation",
            statement="Discharge live governed onboarding employee creation",
            scope={
                "administrative_case_id": case_ref,
                "authority_epoch": str(epoch),
                "obligation_id": "obligation-live-cutover-1",
                "governance_basis_id": governance_ref,
                "execution_grant_id": domain_grant_ref,
                "target_system": "hris",
                "operation": "employee.create",
                "policy_ref": "employee-onboarding:v1",
            },
        ),
        ResponsibilityAdmission(
            id="resp-admission-admin-live-cutover-1",
            created_at=base,
            responsibility_ref=responsibility_ref,
            responsibility_version=1,
            principal_ref="service:administrative-orchestrator",
            basis_refs=[
                f"administrative-grant:{domain_grant_ref}",
                f"governance-basis:{governance_ref}",
                f"approval-satisfaction:{approval_ref}",
            ],
            admitted_at=base,
        ),
    )
    record_domain_assessment(
        kernel,
        ResponsibilityAssessment(
            id=assessment_ref,
            created_at=base,
            responsibility_ref=responsibility_ref,
            responsibility_version=1,
            subject_ref=subject_ref,
            assessment_kind="administrative-obligation-ready",
            basis_refs=[
                f"administrative-intent:{intent_ref}",
                f"administrative-grant:{domain_grant_ref}",
                f"governance-basis:{governance_ref}",
            ],
            assessed_at=base,
            fresh_until=base + timedelta(minutes=30),
            rationale="domain policy and approvals are closed",
        ),
        now=base,
    )
    proposal = WorkProposal(
        id=proposal_ref,
        created_at=base,
        responsibility_ref=responsibility_ref,
        responsibility_version=1,
        assessment_ref=assessment_ref,
        subject_ref=subject_ref,
        work_kind="administrative-effect",
        title="hris: employee.create employee:live-cutover",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        requested_capabilities=[ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE],
        expected_result="employee exists with governed onboarding fields",
        effect_class=EffectClass.EXTERNAL_EFFECT,
        fresh_until=base + timedelta(minutes=30),
    )
    kernel.propose(proposal, now=base)
    admitted = admit_responsibility_proposal(
        kernel,
        proposal.id,
        policy=administrative_public_responsibility_admission_policy(),
        now=base,
    )
    assert admitted.status == "work-materialized"
    assert admitted.work_ref is not None
    work = store.get_work(admitted.work_ref)
    assert work is not None

    intent = DomainEffectIntentEvidenceInput(
        work_ref=work.id,
        domain_intent_ref=intent_ref,
        domain_grant_ref=domain_grant_ref,
        governance_basis_ref=governance_ref,
        approval_satisfaction_ref=approval_ref,
        capability=ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
        subject_ref=subject_ref,
        authority_epoch=epoch,
        parameters={
            "employee_ref": subject_ref,
            "department_ref": "department:engineering",
            "manager_principal_id": "person:manager",
        },
        expected_postcondition={
            "target_system": "hris",
            "operation": "employee.create",
            "subject_ref": subject_ref,
            "active": True,
        },
        observed_at=base,
    )
    authorization = DomainEffectAuthorizationAdmission(store).admit(
        intent,
        now=base + timedelta(seconds=1),
    )
    assert authorization.status == "authorized"
    assert authorization.authorization_ref is not None
    run_result = DomainEffectRunPreparation(store).prepare(
        DomainEffectRunPreparationInput(
            authorization_ref=authorization.authorization_ref,
        ),
        prepared_at=base + timedelta(seconds=2),
    )
    DomainEffectExecutionRequestPreparation(store).prepare(
        DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
        prepared_at=base + timedelta(seconds=3),
    )
    DomainEffectRunActivation(store).activate(
        DomainEffectRunActivationInput(run_ref=run_result.run_ref),
        owner=owner,
        ttl_seconds=900,
        activated_at=base + timedelta(seconds=4),
    )
    qualification = DomainEffectQualificationAssessment(store).assess(
        DomainEffectQualificationInput(run_ref=run_result.run_ref),
        assessed_at=base + timedelta(seconds=5),
    )
    DomainEffectProcedureReadinessAssessment(store).assess(
        DomainEffectProcedureReadinessInput(run_ref=run_result.run_ref),
        assessed_at=base + timedelta(seconds=6),
    )

    registry = ProviderRegistry()
    provider = _HrisProvider(
        "provider:hris:live-cutover",
        qualification.request.capability,
    )
    configured_binding = _register(
        registry,
        provider,
        execution_identity="configured:hris:live-cutover",
        configuration_ref="config:hris:live-cutover:v1",
    )
    provider_assessment = DomainEffectProviderBindingAssessment(store, registry)
    provider_binding = provider_assessment.assess(
        DomainEffectProviderBindingInput(
            run_ref=run_result.run_ref,
            provider_id=provider.descriptor.id,
            runtime_id="runtime:domain-effect-live-cutover",
            semantic_contract=_contract(provider.descriptor.id),
        ),
        assessed_at=base + timedelta(seconds=7),
    )
    specification = DomainEffectInvocationSpecificationCapture(
        store,
        provider_assessment,
    ).capture(
        DomainEffectInvocationSpecificationInput(run_ref=run_result.run_ref),
        captured_at=base + timedelta(seconds=8),
    )
    return (
        store,
        qualification,
        registry,
        provider,
        configured_binding,
        provider_binding,
        specification,
    )


def _dispatch_events(store: InvocationSpecificationInMemoryStateStore):
    return [
        event
        for event in store.export_state()["event"]
        if event.get("type") == DISPATCH_COMMIT_EVENT
    ]


@pytest.mark.asyncio
async def test_domain_effect_crosses_reality_boundary_with_exact_atomic_action_authority() -> None:
    (
        store,
        qualification,
        registry,
        provider,
        configured_binding,
        _provider_binding,
        specification,
    ) = _live_cutover_fixture()
    fallback = _HrisProvider(
        "provider:hris:fallback",
        qualification.request.capability,
    )
    fallback._descriptor = fallback._descriptor.model_copy(update={"priority": 10_000})
    _register(
        registry,
        fallback,
        execution_identity="configured:hris:fallback",
        configuration_ref="config:hris:fallback:v1",
    )

    boundary = RealityBoundary(store=store, registry=registry)
    executor = DomainEffectRealityExecution(
        boundary,
        DomainEffectActionAuthorityResolver(store, registry),
    )

    result = await executor.execute(qualification.request)

    assert result.status == "succeeded"
    assert result.provider_id == provider.descriptor.id
    assert provider.invocations == 1
    assert fallback.invocations == 0
    uses = store.list_authorization_uses()
    assert len(uses) == 1
    dispatches = _dispatch_events(store)
    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch["payload"]["provider_id"] == provider.descriptor.id
    assert dispatch["payload"]["provider_execution_binding_ref"] == configured_binding.id
    assert dispatch["payload"]["invocation_specification_ref"] == specification.specification_ref
    assert dispatch["payload"]["authorization_use_ref"] == uses[0].id


@pytest.mark.asyncio
async def test_domain_effect_retarget_before_cutover_fails_before_any_reality_exit() -> None:
    (
        store,
        qualification,
        registry,
        provider,
        first_binding,
        _provider_binding,
        _specification,
    ) = _live_cutover_fixture()
    registry.unregister(provider.descriptor.id)
    replacement = _HrisProvider(
        provider.descriptor.id,
        qualification.request.capability,
    )
    second_binding = _register(
        registry,
        replacement,
        execution_identity="configured:hris:retargeted",
        configuration_ref="config:hris:retargeted:v1",
    )
    assert second_binding.id != first_binding.id

    boundary = RealityBoundary(store=store, registry=registry)
    executor = DomainEffectRealityExecution(
        boundary,
        DomainEffectActionAuthorityResolver(store, registry),
    )

    with pytest.raises(ValueError, match="configured provider target changed"):
        await executor.execute(qualification.request)

    assert provider.invocations == 0
    assert replacement.invocations == 0
    assert store.list_authorization_uses() == []
    assert _dispatch_events(store) == []
