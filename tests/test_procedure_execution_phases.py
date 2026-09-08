from __future__ import annotations

import pytest

from portable_runtime.core.models import Run, Work
from portable_runtime.workflows.procedure import ProcedureProfile, check_procedure
from portable_runtime.workflows.procedure_phase import (
    ProcedurePhase,
    check_post_action_verification,
    check_pre_action_readiness,
    check_procedure_phase,
)


def _names(statuses):
    return [str(getattr(status.obligation, "kind", status.obligation)) for status in statuses]


def _stable_semantics(statuses):
    return [
        (
            str(getattr(status.obligation, "kind", status.obligation)),
            status.status,
            status.reason,
            status.waiver_authority_ref,
        )
        for status in statuses
    ]


def _work_run() -> tuple[Work, Run]:
    work = Work(
        id="work-procedure-phase",
        title="phase split",
        metadata={
            "purpose": "execute one governed effect",
            "execution_boundary": "provider",
            "candidate": ["execute"],
        },
    )
    run = Run(id="run-procedure-phase", work_id=work.id, status="running")
    return work, run


def test_standard_pre_action_projection_excludes_only_result_dependent_gates() -> None:
    work, run = _work_run()

    full = check_procedure(work, run, ProcedureProfile.standard)
    pre = check_pre_action_readiness(work, run, ProcedureProfile.standard)

    assert _names(full) == [
        "purpose-identified",
        "execution-boundary",
        "result-confirmation",
        "failure-stop",
        "candidate-considered",
        "evidence",
        "authorization",
        "verification",
        "rollback",
        "review",
    ]
    assert _names(pre) == [
        "purpose-identified",
        "execution-boundary",
        "failure-stop",
        "candidate-considered",
        "evidence",
        "authorization",
        "rollback",
        "review",
    ]


def test_standard_post_action_projection_contains_only_result_confirmation_and_verification() -> None:
    work, run = _work_run()

    post = check_post_action_verification(work, run, ProcedureProfile.standard)

    assert _names(post) == ["result-confirmation", "verification"]
    assert all(status.status == "open" for status in post)


def test_minimal_post_action_projection_requires_result_confirmation_only() -> None:
    work, run = _work_run()

    post = check_post_action_verification(work, run, ProcedureProfile.minimal)

    assert _names(post) == ["result-confirmation"]


def test_full_phase_preserves_legacy_full_lifecycle_assessment() -> None:
    work, run = _work_run()

    legacy = check_procedure(work, run, ProcedureProfile.standard)
    projected = check_procedure_phase(
        work,
        run,
        ProcedureProfile.standard,
        phase=ProcedurePhase.full,
    )

    assert _stable_semantics(projected) == _stable_semantics(legacy)


def test_unknown_procedure_phase_fails_closed() -> None:
    work, run = _work_run()

    with pytest.raises(ValueError, match="unknown ProcedurePhase"):
        check_procedure_phase(
            work,
            run,
            ProcedureProfile.standard,
            phase="before-ish",
        )
