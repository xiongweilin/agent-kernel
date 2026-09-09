from __future__ import annotations

from typing import Any

from portable_runtime.responsibility.models import (
    ResponsibilityAssessment,
    ResponsibilityDischargeDecision,
    ResponsibilityStatus,
)
from portable_runtime.responsibility.service import ResponsibilityKernel


class ResponsibilityDischargeDecisionAuthority:
    """Validate and persist discharge judgments without applying lifecycle state.

    This authority records exactly one durable decision fact. It cannot create a
    ``ResponsibilityLifecycleTransition``. A later transition remains a separate
    operation and the journal independently re-reads the decision before
    accepting a transition to ``discharged``.
    """

    def __init__(self, store: Any) -> None:
        self.kernel = ResponsibilityKernel(store)
        self.journal = self.kernel.journal

    def record(
        self,
        decision: ResponsibilityDischargeDecision,
    ) -> ResponsibilityDischargeDecision:
        current_version, _statement, _scope = self.kernel.current_definition(
            decision.responsibility_ref
        )
        if decision.responsibility_version != current_version:
            raise ValueError("responsibility discharge decision is bound to stale responsibility version")
        current_status = self.kernel.current_status(decision.responsibility_ref)
        if current_status is ResponsibilityStatus.DISCHARGED:
            raise ValueError("responsibility is already discharged")
        if decision.status_at_decision is not current_status:
            raise ValueError("responsibility discharge decision is bound to stale lifecycle status")

        assessment = self.journal.get(decision.assessment_ref)
        if not isinstance(assessment, ResponsibilityAssessment):
            raise ValueError("responsibility discharge decision requires durable ResponsibilityAssessment")
        if assessment.responsibility_ref != decision.responsibility_ref:
            raise ValueError("responsibility discharge assessment belongs to another responsibility")
        if assessment.responsibility_version != current_version:
            raise ValueError("responsibility discharge assessment is stale")
        if assessment.assessed_at > decision.decided_at:
            raise ValueError("responsibility discharge decision predates its assessment")
        if assessment.fresh_until is not None and decision.decided_at > assessment.fresh_until:
            raise ValueError("responsibility discharge assessment is no longer fresh")

        recorded = self.journal.save(decision)
        if not isinstance(recorded, ResponsibilityDischargeDecision):
            raise ValueError("responsibility discharge decision identity rebound")
        return recorded


__all__ = ["ResponsibilityDischargeDecisionAuthority"]
