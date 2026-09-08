from __future__ import annotations

import pytest

from portable_runtime.records.models import EvidenceArtifact, OutcomeRecord
from portable_runtime.responsibility.domain_effect_completion_contract import (
    DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA,
    DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA,
)
from portable_runtime.responsibility.domain_effect_terminal_completion import (
    DomainEffectTerminalCompletion,
    DomainEffectTerminalCompletionInput,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DomainEffectVerifiedOutcomeVerification,
)
from portable_runtime.responsibility.models import ResponsibilityStatus
from portable_runtime.responsibility.service import ResponsibilityKernel
from tests.conformance.test_domain_effect_verified_outcome import (
    _ReadbackVerifier,
    _authorization_context,
    _executed_effect,
    _register_verifier,
)


def _completion_contract(store, request):
    work = store.get_work(request.work_id)
    run = store.get_run(request.run_id)
    assert work is not None and run is not None
    contract = work.metadata.get("domain_effect_completion_contract")
    assert isinstance(contract, dict)
    return work, run, contract


@pytest.mark.asyncio
async def test_pass_outcome_terminalizes_work_and_run_but_not_responsibility() -> None:
    store, qualification, registry, _effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    work, run, contract = _completion_contract(store, qualification.request)

    assert contract["schema"] == DOMAIN_EFFECT_COMPLETION_CONTRACT_SCHEMA
    assert contract["verification_scope"]["schema"] == DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA
    assert contract["verification_scope"]["expected_postcondition"] == context.intent.expected_postcondition
    assert contract["acceptance_criteria"] == list(work.acceptance_criteria)
    assert contract["required_obligations"] == list(work.acceptance_criteria)
    assert run.metadata["domain_effect_completion_contract_digest"] == work.metadata[
        "domain_effect_completion_contract_digest"
    ]

    verifier = _ReadbackVerifier(
        "provider:hris:terminal-readback-pass",
        {context.intent.subject_ref: dict(context.intent.expected_postcondition)},
    )
    _register_verifier(registry, verifier)
    verified = await DomainEffectVerifiedOutcomeVerification(
        boundary,
        verifier_provider_id=verifier.descriptor.id,
    ).verify_and_confirm(qualification.request)

    proof = store.get_record(verified.evidence_ref)
    outcome = store.get_record(verified.outcome_ref)
    assert isinstance(proof, EvidenceArtifact)
    assert isinstance(outcome, OutcomeRecord)
    assert proof.metadata["work_version"] == contract["work_version"]
    assert proof.metadata["acceptance_criteria"] == contract["acceptance_criteria"]
    assert proof.metadata["obligation_refs"] == contract["required_obligations"]
    assert proof.metadata["domain_effect_completion_contract_digest"] == work.metadata[
        "domain_effect_completion_contract_digest"
    ]

    completed = DomainEffectTerminalCompletion(store).complete(
        DomainEffectTerminalCompletionInput(outcome_ref=verified.outcome_ref)
    )

    current_work = store.get_work(work.id)
    current_run = store.get_run(run.id)
    assert current_work is not None and current_run is not None
    assert completed.status == "completed"
    assert completed.work_ref == work.id
    assert completed.run_ref == run.id
    assert completed.evidence_refs == [verified.evidence_ref]
    assert current_work.status == "completed"
    assert current_run.status == "succeeded"
    assert current_work.metadata["completion_missing_obligations"] == []
    assert current_run.metadata["completion_missing_obligations"] == []

    kernel = ResponsibilityKernel(store)
    responsibility_ref = current_work.metadata["standing_responsibility_ref"]
    assert completed.responsibility_ref == responsibility_ref
    assert completed.responsibility_status == ResponsibilityStatus.ACTIVE.value
    assert kernel.current_status(responsibility_ref) is ResponsibilityStatus.ACTIVE
    assert kernel.journal.list("ResponsibilityLifecycleTransition", responsibility_ref) == []


@pytest.mark.asyncio
async def test_confirmed_fail_outcome_cannot_terminalize_work_or_run() -> None:
    store, qualification, registry, _effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    verifier = _ReadbackVerifier(
        "provider:hris:terminal-readback-fail",
        {
            context.intent.subject_ref: {
                **context.intent.expected_postcondition,
                "active": False,
            }
        },
    )
    _register_verifier(registry, verifier)
    verified = await DomainEffectVerifiedOutcomeVerification(
        boundary,
        verifier_provider_id=verifier.descriptor.id,
    ).verify_and_confirm(qualification.request)
    assert verified.objective_result == "fail"

    with pytest.raises(ValueError, match="objective pass"):
        DomainEffectTerminalCompletion(store).complete(
            DomainEffectTerminalCompletionInput(outcome_ref=verified.outcome_ref)
        )

    work = store.get_work(qualification.request.work_id)
    run = store.get_run(qualification.request.run_id)
    assert work is not None and run is not None
    assert work.status != "completed"
    assert run.status == "running"
    responsibility_ref = work.metadata["standing_responsibility_ref"]
    assert ResponsibilityKernel(store).current_status(responsibility_ref) is ResponsibilityStatus.ACTIVE


@pytest.mark.asyncio
async def test_recorded_or_unconfirmed_outcome_cannot_substitute_for_verified_completion() -> None:
    store, qualification, _registry, _effect_provider, _boundary = await _executed_effect()
    action = next(
        action
        for action in (store.get_action(ref.metadata["action_ref"]) for ref in store.list_attempts())
        if action is not None and action.request_ref == qualification.request.id
    )
    recorded = OutcomeRecord(
        id="outcome_domain_effect_recorded_only",
        action_ref=action.id,
        lifecycle_status="recorded",
        metadata={"objective_result": "pass"},
    )
    store.save_record(recorded)

    with pytest.raises(ValueError, match="confirmed Outcome"):
        DomainEffectTerminalCompletion(store).complete(
            DomainEffectTerminalCompletionInput(outcome_ref=recorded.id)
        )

    work = store.get_work(qualification.request.work_id)
    run = store.get_run(qualification.request.run_id)
    assert work is not None and run is not None
    assert work.status != "completed"
    assert run.status == "running"
