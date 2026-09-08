"""Execution-phase views over the canonical procedure profile.

The canonical ``check_procedure`` API remains a full-lifecycle assessment:
callers still receive every gate in the selected profile. Reality execution,
however, must not require a post-effect result before the effect is allowed to
occur. This module defines the phase projection and the list-compatible
assessment contract consumed by the RealityBoundary.

``pre-action`` excludes only gates whose truth depends on an execution result:
``result-confirmation`` and ``verification``. All other profile obligations
remain in force, including authorization, evidence, rollback/failure-stop, and
review.

``post-action`` contains exactly the result-dependent obligations present in
the selected profile. It is intended for the provider-result/read-back path;
it does not itself promote provider success into a verified outcome.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from portable_runtime.workflows.procedure import ObligationStatus, ProcedureProfile


class ProcedurePhase(str, Enum):  # noqa: UP042 - stable wire values
    full = "full"
    pre_action = "pre-action"
    post_action = "post-action"


POST_ACTION_OBLIGATIONS = frozenset({"result-confirmation", "verification"})
_BLOCKING_STATUSES = frozenset({"open", "required", "blocked", "expired", "invalidated"})


def _phase(value: ProcedurePhase | str) -> ProcedurePhase:
    if isinstance(value, ProcedurePhase):
        return value
    try:
        return ProcedurePhase(value)
    except ValueError as exc:
        raise ValueError(
            f"unknown ProcedurePhase {value!r} -> configuration error / blocked (fail-closed)"
        ) from exc


def _obligation_name(status: Any) -> str:
    value = status.obligation
    kind = getattr(value, "kind", None)
    return str(kind if kind is not None else value)


class ProcedureAssessment(list[Any]):
    """Full procedure statuses plus explicit pre-action executability.

    It intentionally subclasses ``list`` so historical callers retain the
    exact full-lifecycle iteration/reporting surface. ``executable`` answers a
    narrower question: whether the provider may be entered *before* any result
    exists. Result confirmation and verification remain visible in the list
    and must be closed later by the read-back/verification path.
    """

    @property
    def pre_action_statuses(self) -> list[Any]:
        return [
            status
            for status in self
            if _obligation_name(status) not in POST_ACTION_OBLIGATIONS
        ]

    @property
    def post_action_statuses(self) -> list[Any]:
        return [
            status
            for status in self
            if _obligation_name(status) in POST_ACTION_OBLIGATIONS
        ]

    @property
    def unresolved_pre_action_obligations(self) -> tuple[str, ...]:
        return tuple(
            _obligation_name(status)
            for status in self.pre_action_statuses
            if getattr(status, "status", None) in _BLOCKING_STATUSES
        )

    @property
    def executable(self) -> bool:
        return not self.unresolved_pre_action_obligations


def check_procedure_phase(
    work: Any,
    run: Any,
    profile: ProcedureProfile | str,
    *,
    phase: ProcedurePhase | str,
    **kwargs: Any,
) -> list[ObligationStatus]:
    """Project the canonical full procedure assessment into an execution phase."""

    # Resolve dynamically so tests/controllers that replace the canonical
    # checker continue to observe the same source of gate truth.
    from portable_runtime.workflows import procedure

    selected_phase = _phase(phase)
    statuses = procedure.check_procedure(work, run, profile, **kwargs)
    if selected_phase is ProcedurePhase.full:
        return list(statuses)
    if isinstance(statuses, ProcedureAssessment):
        if selected_phase is ProcedurePhase.pre_action:
            return list(statuses.pre_action_statuses)
        return list(statuses.post_action_statuses)
    if selected_phase is ProcedurePhase.pre_action:
        return [
            status
            for status in statuses
            if _obligation_name(status) not in POST_ACTION_OBLIGATIONS
        ]
    return [
        status
        for status in statuses
        if _obligation_name(status) in POST_ACTION_OBLIGATIONS
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
    "POST_ACTION_OBLIGATIONS",
    "ProcedureAssessment",
    "ProcedurePhase",
    "check_post_action_verification",
    "check_pre_action_readiness",
    "check_procedure_phase",
]
