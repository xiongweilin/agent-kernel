from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.models import utcnow
from portable_runtime.core.runtime import Runtime
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.models import (
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel


class DomainResponsibilityProposalV1(BaseModel):
    """Wire command for domain-owned responsibility evidence and proposal.

    The command stops at WorkProposal. It contains no PriorityJudgment,
    portfolio admission, Commitment, Work, runtime authorization, InvocationPermit,
    provider execution, or Outcome.
    """

    model_config = ConfigDict(extra="forbid")

    schema: Literal["domain-responsibility-proposal-v1"] = "domain-responsibility-proposal-v1"
    responsibility: StandingResponsibility
    admission: ResponsibilityAdmission
    assessment: ResponsibilityAssessment
    proposal: WorkProposal


class DomainResponsibilityProposalReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: Literal["domain-responsibility-proposal-receipt-v1"] = (
        "domain-responsibility-proposal-receipt-v1"
    )
    status: Literal["proposal-recorded"] = "proposal-recorded"
    responsibility_ref: str
    admission_ref: str
    assessment_ref: str
    proposal_ref: str
    recorded_at: datetime = Field(default_factory=utcnow)
    authority_bearing: Literal[False] = False


def record_domain_responsibility_proposal(
    runtime: Runtime,
    command: DomainResponsibilityProposalV1,
    *,
    now: datetime | None = None,
) -> DomainResponsibilityProposalReceiptV1:
    """Record an idempotent domain proposal without admitting Work.

    Domain interpretation is accepted only as canonical responsibility objects.
    Kernel enforces identity/version/activity/freshness constraints. The caller
    cannot use this command to mint any later admission or execution authority.
    """

    now = now or utcnow()
    kernel = ResponsibilityKernel(runtime.store)
    identity = command.responsibility
    admission = command.admission

    existing_identity = kernel.journal.get(identity.id)
    if existing_identity is None:
        kernel.register(identity, admission)
    else:
        if existing_identity != identity:
            raise ValueError(f"responsibility identity rebound: {identity.id}")
        existing_admission = kernel.journal.get(admission.id)
        if existing_admission is None:
            raise ValueError("responsibility admission missing for existing identity")
        if existing_admission != admission:
            raise ValueError(f"responsibility admission identity rebound: {admission.id}")

    assessment = record_domain_assessment(kernel, command.assessment, now=now)
    proposal = kernel.propose(command.proposal, now=now)
    return DomainResponsibilityProposalReceiptV1(
        responsibility_ref=identity.id,
        admission_ref=admission.id,
        assessment_ref=assessment.id,
        proposal_ref=proposal.id,
        recorded_at=now,
    )


__all__ = [
    "DomainResponsibilityProposalReceiptV1",
    "DomainResponsibilityProposalV1",
    "record_domain_responsibility_proposal",
]
