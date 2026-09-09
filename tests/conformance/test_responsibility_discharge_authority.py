from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from portable_runtime.responsibility import (
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    ResponsibilityDischargeDecision,
    ResponsibilityDischargeDecisionAuthority,
    ResponsibilityDischargeDisposition,
    ResponsibilityKernel,
    ResponsibilityLifecycleTransition,
    ResponsibilityRevision,
    ResponsibilityStatus,
    StandingResponsibility,
)
from portable_runtime.stores.memory import InMemoryStateStore


def _now() -> datetime:
    return datetime(2026, 9, 9, 0, 0, tzinfo=UTC)


def _kernel() -> tuple[InMemoryStateStore, ResponsibilityKernel, ResponsibilityAssessment]:
    store = InMemoryStateStore()
    kernel = ResponsibilityKernel(store)
    identity = StandingResponsibility(
        id="responsibility_discharge_conformance",
        responsibility_kind="administrative-obligation",
        statement="Complete one bounded administrative obligation",
        scope={"obligation_id": "obligation:1"},
    )
    kernel.register(
        identity,
        ResponsibilityAdmission(
            id="admission_discharge_conformance",
            responsibility_ref=identity.id,
            responsibility_version=1,
            principal_ref="service:administrative-orchestrator",
            basis_refs=["mission:1"],
            admitted_at=_now(),
        ),
    )
    assessment = ResponsibilityAssessment(
        id="assessment_discharge_conformance",
        responsibility_ref=identity.id,
        responsibility_version=1,
        subject_ref="obligation:1",
        assessment_kind="responsibility-discharge-reassessment",
        basis_refs=["outcome:confirmed-pass", "work:completed"],
        assessed_at=_now(),
        fresh_until=_now() + timedelta(minutes=10),
        rationale="bounded obligation is currently satisfied",
    )
    kernel.journal.save(assessment)
    return store, kernel, assessment


def _decision(
    assessment: ResponsibilityAssessment,
    *,
    decision_id: str = "decision_discharge_conformance",
    version: int = 1,
    status: ResponsibilityStatus = ResponsibilityStatus.ACTIVE,
    disposition: ResponsibilityDischargeDisposition = ResponsibilityDischargeDisposition.DISCHARGE,
    decided_at: datetime | None = None,
) -> ResponsibilityDischargeDecision:
    return ResponsibilityDischargeDecision(
        id=decision_id,
        responsibility_ref=assessment.responsibility_ref,
        responsibility_version=version,
        status_at_decision=status,
        assessment_ref=assessment.id,
        disposition=disposition,
        basis_refs=[assessment.id, "outcome:confirmed-pass"],
        policy_ref="responsibility-discharge-policy:v1",
        decided_at=decided_at or (_now() + timedelta(seconds=1)),
        rationale="explicit current reassessment supports this judgment",
    )


def test_discharge_decision_does_not_mutate_lifecycle() -> None:
    store, kernel, assessment = _kernel()

    decision = ResponsibilityDischargeDecisionAuthority(store).record(_decision(assessment))

    assert decision.disposition is ResponsibilityDischargeDisposition.DISCHARGE
    assert kernel.current_status(assessment.responsibility_ref) is ResponsibilityStatus.ACTIVE
    assert kernel.journal.list("ResponsibilityLifecycleTransition", assessment.responsibility_ref) == []


def test_stale_responsibility_version_cannot_record_discharge_decision() -> None:
    store, kernel, assessment = _kernel()
    kernel.revise(
        ResponsibilityRevision(
            id="revision_discharge_conformance_v2",
            responsibility_ref=assessment.responsibility_ref,
            from_version=1,
            to_version=2,
            statement="Complete one bounded administrative obligation",
            scope={"obligation_id": "obligation:1", "revision": "2"},
            basis_refs=["policy-change:1"],
        )
    )

    with pytest.raises(ValueError, match="stale responsibility version"):
        ResponsibilityDischargeDecisionAuthority(store).record(_decision(assessment))


def test_stale_lifecycle_status_cannot_record_discharge_decision() -> None:
    store, kernel, assessment = _kernel()
    kernel.transition(
        ResponsibilityLifecycleTransition(
            id="suspend_before_discharge_decision",
            responsibility_ref=assessment.responsibility_ref,
            responsibility_version=1,
            from_status=ResponsibilityStatus.ACTIVE,
            to_status=ResponsibilityStatus.SUSPENDED,
            basis_refs=["incident:hold"],
        )
    )

    with pytest.raises(ValueError, match="stale lifecycle status"):
        ResponsibilityDischargeDecisionAuthority(store).record(_decision(assessment))


def test_expired_reassessment_cannot_record_discharge_decision() -> None:
    store, _kernel_value, assessment = _kernel()

    with pytest.raises(ValueError, match="no longer fresh"):
        ResponsibilityDischargeDecisionAuthority(store).record(
            _decision(
                assessment,
                decided_at=_now() + timedelta(minutes=11),
            )
        )


def test_retain_decision_cannot_authorize_discharge_transition() -> None:
    store, kernel, assessment = _kernel()
    decision = ResponsibilityDischargeDecisionAuthority(store).record(
        _decision(
            assessment,
            decision_id="decision_retain_conformance",
            disposition=ResponsibilityDischargeDisposition.RETAIN,
        )
    )

    with pytest.raises(ValueError, match="does not authorize discharge"):
        kernel.transition(
            ResponsibilityLifecycleTransition(
                id="transition_retain_conformance",
                responsibility_ref=assessment.responsibility_ref,
                responsibility_version=1,
                from_status=ResponsibilityStatus.ACTIVE,
                to_status=ResponsibilityStatus.DISCHARGED,
                decision_ref=decision.id,
                basis_refs=[decision.id],
            )
        )
    assert kernel.current_status(assessment.responsibility_ref) is ResponsibilityStatus.ACTIVE


def test_discharge_decision_remains_authoritative_after_state_round_trip() -> None:
    source, source_kernel, assessment = _kernel()
    decision = ResponsibilityDischargeDecisionAuthority(source).record(_decision(assessment))
    assert source_kernel.current_status(assessment.responsibility_ref) is ResponsibilityStatus.ACTIVE

    target = InMemoryStateStore()
    target.import_state(source.export_state())
    target_kernel = ResponsibilityKernel(target)
    restored = target_kernel.journal.get(decision.id)
    assert isinstance(restored, ResponsibilityDischargeDecision)
    assert restored.disposition is ResponsibilityDischargeDisposition.DISCHARGE
    assert target_kernel.current_status(assessment.responsibility_ref) is ResponsibilityStatus.ACTIVE

    target_kernel.transition(
        ResponsibilityLifecycleTransition(
            id="transition_discharge_after_roundtrip",
            responsibility_ref=assessment.responsibility_ref,
            responsibility_version=1,
            from_status=ResponsibilityStatus.ACTIVE,
            to_status=ResponsibilityStatus.DISCHARGED,
            decision_ref=decision.id,
            basis_refs=[decision.id],
        )
    )
    assert target_kernel.current_status(assessment.responsibility_ref) is ResponsibilityStatus.DISCHARGED
