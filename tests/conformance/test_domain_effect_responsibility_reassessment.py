from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from portable_runtime.core.models import Work
from portable_runtime.responsibility.domain_effect_responsibility_reassessment import (
    DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED,
    DOMAIN_EFFECT_RESPONSIBILITY_CLEAR,
    DomainEffectResponsibilityReassessment,
    DomainEffectResponsibilityReassessmentInput,
)
from portable_runtime.responsibility.domain_effect_terminal_completion import (
    DomainEffectTerminalCompletion,
    DomainEffectTerminalCompletionInput,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DomainEffectVerifiedOutcomeVerification,
)
from portable_runtime.responsibility.models import (
    ResponsibilityExpectation,
    ResponsibilityRevision,
    ResponsibilityStatus,
)
from portable_runtime.responsibility.service import ResponsibilityKernel
from tests.conformance.test_domain_effect_verified_outcome import (
    _ReadbackVerifier,
    _authorization_context,
    _executed_effect,
    _register_verifier,
)


def _now() -> datetime:
    return datetime(2026, 9, 9, 0, 20, tzinfo=UTC)


async def _completed_effect():
    store, qualification, registry, _effect_provider, boundary = await _executed_effect()
    context = _authorization_context(store, qualification.request)
    verifier = _ReadbackVerifier(
        "provider:hris:responsibility-reassessment",
        {context.intent.subject_ref: dict(context.intent.expected_postcondition)},
    )
    _register_verifier(registry, verifier)
    verified = await DomainEffectVerifiedOutcomeVerification(
        boundary,
        verifier_provider_id=verifier.descriptor.id,
    ).verify_and_confirm(qualification.request)
    completed = DomainEffectTerminalCompletion(store).complete(
        DomainEffectTerminalCompletionInput(outcome_ref=verified.outcome_ref)
    )
    return store, qualification, verified, completed


@pytest.mark.asyncio
async def test_verified_terminal_effect_records_clear_reassessment_without_discharge() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    kernel = ResponsibilityKernel(store)
    assessed_at = _now()

    result = DomainEffectResponsibilityReassessment(store).reassess(
        DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref),
        assessed_at=assessed_at,
    )

    assessment = kernel.journal.get(result.assessment_ref)
    assert assessment is not None
    current_version, _statement, _scope = kernel.current_definition(completed.responsibility_ref)
    assert assessment.object_type == "ResponsibilityAssessment"
    assert assessment.assessment_kind == DOMAIN_EFFECT_RESPONSIBILITY_CLEAR
    assert assessment.responsibility_ref == completed.responsibility_ref
    assert assessment.responsibility_version == current_version
    assert assessment.fresh_until == assessed_at + timedelta(seconds=300)
    assert result.blockers_clear is True
    assert result.blocker_refs == []
    assert kernel.current_status(completed.responsibility_ref) is ResponsibilityStatus.ACTIVE
    assert kernel.journal.list("ResponsibilityDischargeDecision", completed.responsibility_ref) == []
    assert kernel.journal.list("ResponsibilityLifecycleTransition", completed.responsibility_ref) == []


@pytest.mark.asyncio
async def test_open_current_work_is_reported_as_current_blocker() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    completed_work = store.get_work(completed.work_ref)
    assert completed_work is not None
    blocker = Work(
        id="work_same_responsibility_still_open",
        title="Another current obligation",
        status="open",
        metadata={
            "standing_responsibility_ref": completed.responsibility_ref,
            "standing_responsibility_version": completed_work.metadata[
                "standing_responsibility_version"
            ],
        },
    )
    store.save_work(blocker)

    result = DomainEffectResponsibilityReassessment(store).reassess(
        DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref),
        assessed_at=_now(),
    )

    assessment = ResponsibilityKernel(store).journal.get(result.assessment_ref)
    assert assessment is not None
    assert assessment.assessment_kind == DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED
    assert result.blockers_clear is False
    assert result.blocker_refs == [blocker.id]
    assert ResponsibilityKernel(store).current_status(completed.responsibility_ref) is ResponsibilityStatus.ACTIVE


@pytest.mark.asyncio
async def test_failed_or_cancelled_work_is_reported_without_domain_judgment() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    completed_work = store.get_work(completed.work_ref)
    assert completed_work is not None
    blockers = []
    for status in ("failed", "cancelled"):
        candidate = Work(
            id=f"work_same_responsibility_{status}",
            title=f"Another {status} obligation",
            status=status,
            metadata={
                "standing_responsibility_ref": completed.responsibility_ref,
                "standing_responsibility_version": completed_work.metadata[
                    "standing_responsibility_version"
                ],
            },
        )
        store.save_work(candidate)
        blockers.append(candidate.id)

    result = DomainEffectResponsibilityReassessment(store).reassess(
        DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref),
        assessed_at=_now(),
    )

    assert result.blockers_clear is False
    assert result.blocker_refs == sorted(blockers)


@pytest.mark.asyncio
async def test_open_current_expectation_is_reported_as_current_blocker() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    kernel = ResponsibilityKernel(store)
    version, _statement, _scope = kernel.current_definition(completed.responsibility_ref)
    expectation = ResponsibilityExpectation(
        id="expectation_same_responsibility_open",
        responsibility_ref=completed.responsibility_ref,
        responsibility_version=version,
        subject_ref="employee:post-onboarding",
        expected_signal_kind="manager-acknowledgement",
        due_at=_now() + timedelta(hours=1),
    )
    kernel.create_expectation(expectation)

    result = DomainEffectResponsibilityReassessment(store).reassess(
        DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref),
        assessed_at=_now(),
    )

    assert result.blockers_clear is False
    assert result.blocker_refs == [expectation.id]
    assessment = kernel.journal.get(result.assessment_ref)
    assert assessment is not None
    assert assessment.assessment_kind == DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED


@pytest.mark.asyncio
async def test_responsibility_revision_makes_completed_work_stale_for_reassessment() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    kernel = ResponsibilityKernel(store)
    version, statement, scope = kernel.current_definition(completed.responsibility_ref)
    assert version == 1
    kernel.revise(
        ResponsibilityRevision(
            id="revision_after_domain_effect_completion",
            responsibility_ref=completed.responsibility_ref,
            from_version=1,
            to_version=2,
            statement=statement,
            scope={**scope, "revision": "2"},
            basis_refs=["governance:scope-changed"],
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        DomainEffectResponsibilityReassessment(store).reassess(
            DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref),
            assessed_at=_now(),
        )

    assessments = kernel.journal.list("ResponsibilityAssessment", completed.responsibility_ref)
    assert all(item.assessment_kind != DOMAIN_EFFECT_RESPONSIBILITY_CLEAR for item in assessments)


@pytest.mark.asyncio
async def test_same_reassessment_replay_is_idempotent_at_same_assessment_time() -> None:
    store, _qualification, verified, completed = await _completed_effect()
    kernel = ResponsibilityKernel(store)
    assessed_at = _now()
    adapter = DomainEffectResponsibilityReassessment(store)
    value = DomainEffectResponsibilityReassessmentInput(outcome_ref=verified.outcome_ref)

    first = adapter.reassess(value, assessed_at=assessed_at)
    second = adapter.reassess(value, assessed_at=assessed_at)

    assert first == second
    matches = [
        item
        for item in kernel.journal.list("ResponsibilityAssessment", completed.responsibility_ref)
        if item.id == first.assessment_ref
    ]
    assert len(matches) == 1
