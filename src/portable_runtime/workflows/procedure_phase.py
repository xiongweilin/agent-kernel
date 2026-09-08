"""Execution-phase views over the canonical procedure profile.

The canonical ``check_procedure`` API intentionally remains a full-lifecycle
assessment for compatibility.  Reality execution, however, must not require a
post-effect result before the effect is allowed to occur.  This module provides
explicit phase projections without weakening or redefining the underlying
profile gates.

``pre-action`` excludes only gates whose truth depends on an execution result:
``result-confirmation`` and ``verification``.  All other profile obligations
remain in force, including authorization, evidence, rollback/failure-stop, and
review.

``post-action`` contains exactly the result-dependent obligations present in
the selected profile.  It is intended for the provider-result/read-back path;
it does not itself promote provider success into a verified outcome.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from portable_runtime.workflows.procedure import ObligationStatus, ProcedureProfile


class ProcedurePhase(str, Enum):  # noqa: UP042 - stable wire values
    full = "full"
    pre_action = "pre-action"
    post_action = "post-action"


_POST_ACTION_OBLIGATIONS = frozenset({"result-confirmation", "verification"})


def _phase(value: ProcedurePhase | str) -> ProcedurePhase:
    if isinstance(value, ProcedurePhase):
        return value
    try:
        return ProcedurePhase(value)
    except ValueError as exc:
        raise ValueError(f"unknown ProcedurePhase {value!r} -> configuration error / blocked (fail-closed)") from exc


def _obligation_name(status: ObligationStatus) -> str:
    value = status.obligation
    kind = getattr(value, "kind", None)
    return str(kind if kind is not None else value)


def check_procedure_phase(
    work: Any,
    run: Any,
    profile: ProcedureProfile | str,
    *,
    phase: ProcedurePhase | str,
    **kwargs: Any,
) -> list[ObligationStatus]:
    """Project the canonical full procedure assessment into an execution phase.

    The underlying checker still evaluates the complete profile and therefore
    remains the single definition of gate semantics.  This function only
    determines which gates are relevant at the named execution phase.
    """

    # Resolve dynamically so tests/controllers that replace the canonical
    # checker continue to observe the same boundary call path.
    from portable_runtime.workflows import procedure

    selected_phase = _phase(phase)
    statuses = procedure.check_procedure(work, run, profile, **kwargs)
    if selected_phase is ProcedurePhase.full:
        return statuses
    if selected_phase is ProcedurePhase.pre_action:
        return [
            status
            for status in statuses
            if _obligation_name(status) not in _POST_ACTION_OBLIGATIONS
        ]
    return [
        status
        for status in statuses
        if _obligation_name(status) in _POST_ACTION_OBLIGATIONS
    ]


def check_pre_action_readiness(
    work: Any,
    run: Any,
    profile: ProcedureProfile | str,
    **kwargs: Any,
) -> list[ObligationStatus]:
    """Return only obligations that must be true before provider execution."""

    return check_procedure_phase(
        work,
        run,
        profile,
        phase=ProcedurePhase.pre_action,
        **kwargs,
    )


def check_post_action_verification(
    work: Any,
    run: Any,
    profile: ProcedureProfile | str,
    **kwargs: Any,
) -> list[ObligationStatus]:
    """Return result-dependent obligations for the post-provider verification path."""

    return check_procedure_phase(
        work,
        run,
        profile,
        phase=ProcedurePhase.post_action,
        **kwargs,
    )


__all__ = [
    "ProcedurePhase",
    "check_post_action_verification",
    "check_pre_action_readiness",
    "check_procedure_phase",
]
