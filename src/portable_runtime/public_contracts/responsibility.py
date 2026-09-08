from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.models import utcnow
from portable_runtime.core.runtime import Runtime
from portable_runtime.responsibility.admission import (
    ResponsibilityAdmissionPolicy,
    admit_responsibility_proposal,
)
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

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["domain-responsibility-proposal-v1"] = Field(
        "domain-responsibility-proposal-v1",
        alias="schema",
    )
    responsibility: StandingResponsibility
    admission: ResponsibilityAdmission
    assessment: ResponsibilityAssessment
    proposal: WorkProposal


class DomainResponsibilityProposalReceiptV1(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["domain-responsibility-proposal-receipt-v1"] = Field(
        "domain-responsibility-proposal-receipt-v1",
        alias="schema",
    )
    status: Literal["proposal-recorded"] = "proposal-recorded"
    responsibility_ref: str
    admission_ref: str
    assessment_ref: str
    proposal_ref: str
    recorded_at: datetime = Field(default_factory=utcnow)
    authority_bearing: Literal[False] = False


class ResponsibilityWorkAdmissionV1(BaseModel):
    """Ask Kernel to admit one already-recorded WorkProposal.

    The caller supplies no priority decision, capacity threshold, reservation,
    commitment, Work object, or execution authority. Those remain Kernel-owned.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["responsibility-work-admission-v1"] = Field(
        "responsibility-work-admission-v1",
        alias="schema",
    )
    proposal_ref: str
    expected_policy_ref: str


class ResponsibilityWorkAdmissionReceiptV1(BaseModel):
    """Non-authoritative receipt for Kernel-owned Work admission."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["responsibility-work-admission-receipt-v1"] = Field(
        "responsibility-work-admission-receipt-v1",
        alias="schema",
    )
    status: Literal["work-materialized", "priority-rejected", "portfolio-rejected"]
    proposal_ref: str
    policy_ref: str
    priority_judgment_ref: str
    resource_pool_ref: str | None = None
    portfolio_admission_ref: str | None = None
    reservation_ref: str | None = None
    commitment_ref: str | None = None
    work_ref: str | None = None
    processed_at: datetime = Field(default_factory=utcnow)
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
        schema="domain-responsibility-proposal-receipt-v1",
        responsibility_ref=identity.id,
        admission_ref=admission.id,
        assessment_ref=assessment.id,
        proposal_ref=proposal.id,
        recorded_at=now,
    )


def admit_responsibility_work(
    runtime: Runtime,
    command: ResponsibilityWorkAdmissionV1,
    *,
    policy: ResponsibilityAdmissionPolicy,
    now: datetime | None = None,
) -> ResponsibilityWorkAdmissionReceiptV1:
    """Run Kernel-owned proposal admission without minting execution authority."""

    if command.expected_policy_ref != policy.policy_ref:
        raise ValueError(
            "responsibility admission policy mismatch: "
            f"expected {command.expected_policy_ref!r}, active {policy.policy_ref!r}"
        )
    now = now or utcnow()
    result = admit_responsibility_proposal(
        ResponsibilityKernel(runtime.store),
        command.proposal_ref,
        policy=policy,
        now=now,
    )
    return ResponsibilityWorkAdmissionReceiptV1(
        schema="responsibility-work-admission-receipt-v1",
        status=result.status,
        proposal_ref=result.proposal_ref,
        policy_ref=result.policy_ref,
        priority_judgment_ref=result.priority_judgment_ref,
        resource_pool_ref=result.resource_pool_ref,
        portfolio_admission_ref=result.portfolio_admission_ref,
        reservation_ref=result.reservation_ref,
        commitment_ref=result.commitment_ref,
        work_ref=result.work_ref,
        processed_at=now,
    )


__all__ = [
    "DomainResponsibilityProposalReceiptV1",
    "DomainResponsibilityProposalV1",
    "ResponsibilityWorkAdmissionReceiptV1",
    "ResponsibilityWorkAdmissionV1",
    "admit_responsibility_work",
    "record_domain_responsibility_proposal",
]
