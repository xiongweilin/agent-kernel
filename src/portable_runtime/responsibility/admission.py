from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from portable_runtime.core.models import Work
from portable_runtime.responsibility.models import (
    Commitment,
    EffectClass,
    PortfolioAdmissionDecision,
    PriorityDimensions,
    PriorityJudgment,
    ResourcePool,
    ResourceReservation,
    ResourceVector,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class PriorityPolicyDecision:
    dimensions: PriorityDimensions
    admitted: bool
    rationale: str


class ResponsibilityAdmissionPolicy(Protocol):
    @property
    def policy_ref(self) -> str: ...

    @property
    def pool_key(self) -> str: ...

    @property
    def pool_capacity(self) -> ResourceVector: ...

    @property
    def reservation_ttl_seconds(self) -> int: ...

    def judge_priority(self, proposal: WorkProposal) -> PriorityPolicyDecision: ...


@dataclass(frozen=True, slots=True)
class BoundedLocalResponsibilityAdmissionPolicy:
    """Versioned reference admission policy, not a framework-wide value function.

    The profile admits proposals only when their declared resource request fits
    the configured per-proposal ceiling. Priority dimensions are inspectable
    policy output; they are not combined into a universal weighted score.
    """

    profile_id: str = "bounded-local"
    version: str = "1"
    max_request: ResourceVector = field(
        default_factory=lambda: ResourceVector(
            compute_units=4,
            api_calls=8,
            money_minor=0,
            human_attention_units=1,
            concurrency_slots=1,
        )
    )
    capacity: ResourceVector = field(
        default_factory=lambda: ResourceVector(
            compute_units=16,
            api_calls=32,
            money_minor=0,
            human_attention_units=4,
            concurrency_slots=4,
        )
    )
    allowed_effect_classes: tuple[EffectClass, ...] = (
        EffectClass.READ_ONLY,
        EffectClass.INTERNAL_REVERSIBLE,
        EffectClass.EXTERNAL_EFFECT,
    )
    reservation_ttl_seconds: int = 300

    @property
    def policy_ref(self) -> str:
        return f"responsibility-admission:{self.profile_id}@{self.version}"

    @property
    def pool_key(self) -> str:
        return f"responsibility:{self.profile_id}"

    @property
    def pool_capacity(self) -> ResourceVector:
        return self.capacity

    def judge_priority(self, proposal: WorkProposal) -> PriorityPolicyDecision:
        resource_fit = proposal.requested_resources.fits_within(self.max_request)
        effect_allowed = proposal.effect_class in self.allowed_effect_classes
        admitted = resource_fit and effect_allowed
        risk = {
            EffectClass.READ_ONLY: 1,
            EffectClass.INTERNAL_REVERSIBLE: 2,
            EffectClass.EXTERNAL_EFFECT: 3,
        }[proposal.effect_class]
        reversibility = {
            EffectClass.READ_ONLY: 5,
            EffectClass.INTERNAL_REVERSIBLE: 4,
            EffectClass.EXTERNAL_EFFECT: 2,
        }[proposal.effect_class]
        dimensions = PriorityDimensions(
            urgency=3,
            impact=3,
            risk=risk,
            reversibility=reversibility,
            confidence=4,
            resource_cost=1 if resource_fit else 5,
            human_attention_cost=min(5, proposal.requested_resources.human_attention_units),
        )
        reasons: list[str] = []
        if not resource_fit:
            reasons.append("declared resource request exceeds profile ceiling")
        if not effect_allowed:
            reasons.append("effect class is not admitted by profile")
        return PriorityPolicyDecision(
            dimensions=dimensions,
            admitted=admitted,
            rationale="bounded proposal admitted" if admitted else "; ".join(reasons),
        )


ResponsibilityWorkAdmissionStatus = Literal[
    "work-materialized",
    "priority-rejected",
    "portfolio-rejected",
]


@dataclass(frozen=True, slots=True)
class ResponsibilityWorkAdmissionResult:
    status: ResponsibilityWorkAdmissionStatus
    proposal_ref: str
    policy_ref: str
    priority_judgment_ref: str
    resource_pool_ref: str | None = None
    portfolio_admission_ref: str | None = None
    reservation_ref: str | None = None
    commitment_ref: str | None = None
    work_ref: str | None = None


def _existing_work_for_proposal(kernel: ResponsibilityKernel, proposal_ref: str) -> Work | None:
    matches = [
        work
        for work in kernel.store.list_work()
        if isinstance(work.metadata, dict)
        and work.metadata.get("responsibility_proposal_ref") == proposal_ref
    ]
    if len(matches) > 1:
        raise ValueError("one responsibility proposal materialized multiple Work identities")
    return matches[0] if matches else None


def admit_responsibility_proposal(
    kernel: ResponsibilityKernel,
    proposal_ref: str,
    *,
    policy: ResponsibilityAdmissionPolicy,
    now: datetime,
) -> ResponsibilityWorkAdmissionResult:
    """Atomically admit one proposal and its complete bounded Work chain."""

    with kernel.store.transaction():
        return _admit_responsibility_proposal(
            kernel,
            proposal_ref,
            policy=policy,
            now=now,
        )


def _admit_responsibility_proposal(
    kernel: ResponsibilityKernel,
    proposal_ref: str,
    *,
    policy: ResponsibilityAdmissionPolicy,
    now: datetime,
) -> ResponsibilityWorkAdmissionResult:
    proposal_value = kernel.journal.get(proposal_ref)
    if not isinstance(proposal_value, WorkProposal):
        raise ValueError(f"unknown WorkProposal: {proposal_ref}")
    proposal = proposal_value

    existing_work = _existing_work_for_proposal(kernel, proposal.id)
    if existing_work is not None:
        commitment_ref = existing_work.metadata.get("responsibility_commitment_ref")
        if not isinstance(commitment_ref, str):
            raise ValueError("materialized responsibility Work lacks commitment provenance")
        commitment = kernel.journal.get(commitment_ref)
        if not isinstance(commitment, Commitment):
            raise ValueError("materialized responsibility Work references unknown commitment")
        priority = kernel.journal.get(commitment.priority_judgment_ref)
        if not isinstance(priority, PriorityJudgment) or priority.policy_ref != policy.policy_ref:
            raise ValueError("materialized Work was admitted under a different priority policy")
        portfolio = kernel.journal.get(commitment.portfolio_admission_ref)
        if not isinstance(portfolio, PortfolioAdmissionDecision):
            raise ValueError("materialized Work references unknown portfolio admission")
        return ResponsibilityWorkAdmissionResult(
            status="work-materialized",
            proposal_ref=proposal.id,
            policy_ref=policy.policy_ref,
            priority_judgment_ref=priority.id,
            resource_pool_ref=portfolio.resource_pool_ref,
            portfolio_admission_ref=portfolio.id,
            reservation_ref=commitment.reservation_ref,
            commitment_ref=commitment.id,
            work_ref=existing_work.id,
        )

    kernel.propose(proposal, now=now)

    priority_id = _stable_id("priority", proposal.id, policy.policy_ref)
    priority_value = kernel.journal.get(priority_id)
    if priority_value is None:
        decision = policy.judge_priority(proposal)
        priority = PriorityJudgment(
            id=priority_id,
            created_at=proposal.created_at,
            proposal_ref=proposal.id,
            dimensions=decision.dimensions,
            policy_ref=policy.policy_ref,
            admitted=decision.admitted,
            rationale=decision.rationale,
        )
        kernel.record_priority_judgment(priority)
    elif isinstance(priority_value, PriorityJudgment):
        priority = priority_value
        if priority.proposal_ref != proposal.id or priority.policy_ref != policy.policy_ref:
            raise ValueError("priority judgment identity rebound")
    else:
        raise ValueError("priority judgment identity rebound")

    if not priority.admitted:
        return ResponsibilityWorkAdmissionResult(
            status="priority-rejected",
            proposal_ref=proposal.id,
            policy_ref=policy.policy_ref,
            priority_judgment_ref=priority.id,
        )

    pool_id = _stable_id("resource_pool", policy.policy_ref, policy.pool_key)
    pool_value = kernel.journal.get(pool_id)
    if pool_value is None:
        pool = ResourcePool(
            id=pool_id,
            created_at=proposal.created_at,
            pool_key=policy.pool_key,
            capacity=policy.pool_capacity,
            policy_ref=policy.policy_ref,
        )
        kernel.create_resource_pool(pool)
    elif isinstance(pool_value, ResourcePool):
        pool = pool_value
        if (
            pool.pool_key != policy.pool_key
            or pool.policy_ref != policy.policy_ref
            or pool.capacity != policy.pool_capacity
        ):
            raise ValueError("resource pool identity rebound")
    else:
        raise ValueError("resource pool identity rebound")

    portfolio_id = _stable_id("portfolio", proposal.id, pool.id, policy.policy_ref)
    portfolio_value = kernel.journal.get(portfolio_id)
    if portfolio_value is None:
        available = kernel.available_resources(pool.id, now=now)
        portfolio = PortfolioAdmissionDecision(
            id=portfolio_id,
            created_at=proposal.created_at,
            proposal_ref=proposal.id,
            resource_pool_ref=pool.id,
            policy_ref=policy.policy_ref,
            admitted=proposal.requested_resources.fits_within(available),
            rationale=(
                "current pool capacity reserved for proposal"
                if proposal.requested_resources.fits_within(available)
                else "current pool capacity cannot satisfy proposal"
            ),
        )
        kernel.record_portfolio_admission(portfolio)
    elif isinstance(portfolio_value, PortfolioAdmissionDecision):
        portfolio = portfolio_value
        if (
            portfolio.proposal_ref != proposal.id
            or portfolio.resource_pool_ref != pool.id
            or portfolio.policy_ref != policy.policy_ref
        ):
            raise ValueError("portfolio admission identity rebound")
    else:
        raise ValueError("portfolio admission identity rebound")

    if not portfolio.admitted:
        return ResponsibilityWorkAdmissionResult(
            status="portfolio-rejected",
            proposal_ref=proposal.id,
            policy_ref=policy.policy_ref,
            priority_judgment_ref=priority.id,
            resource_pool_ref=pool.id,
            portfolio_admission_ref=portfolio.id,
        )

    reservation_id = _stable_id("reservation", proposal.id, pool.id, policy.policy_ref)
    reservation_value = kernel.journal.get(reservation_id)
    if reservation_value is None:
        reservation = ResourceReservation(
            id=reservation_id,
            created_at=now,
            responsibility_ref=proposal.responsibility_ref,
            proposal_ref=proposal.id,
            resource_pool_ref=pool.id,
            resources=proposal.requested_resources,
            reserved_at=now,
            expires_at=now + timedelta(seconds=policy.reservation_ttl_seconds),
        )
        kernel.reserve(reservation, now=now)
    elif isinstance(reservation_value, ResourceReservation):
        reservation = reservation_value
        if (
            reservation.responsibility_ref != proposal.responsibility_ref
            or reservation.proposal_ref != proposal.id
            or reservation.resource_pool_ref != pool.id
            or reservation.resources != proposal.requested_resources
        ):
            raise ValueError("resource reservation identity rebound")
    else:
        raise ValueError("resource reservation identity rebound")

    commitment_id = _stable_id("commitment", proposal.id, reservation.id, policy.policy_ref)
    commitment_value = kernel.journal.get(commitment_id)
    if commitment_value is None:
        commitment = Commitment(
            id=commitment_id,
            created_at=reservation.reserved_at,
            responsibility_ref=proposal.responsibility_ref,
            responsibility_version=proposal.responsibility_version,
            proposal_ref=proposal.id,
            priority_judgment_ref=priority.id,
            portfolio_admission_ref=portfolio.id,
            reservation_ref=reservation.id,
            resources=proposal.requested_resources,
            committed_at=reservation.reserved_at,
            stop_conditions=list(proposal.stop_conditions),
            escalation_conditions=list(proposal.escalation_conditions),
        )
        kernel.commit(commitment, now=now)
    elif isinstance(commitment_value, Commitment):
        commitment = commitment_value
        if (
            commitment.proposal_ref != proposal.id
            or commitment.priority_judgment_ref != priority.id
            or commitment.portfolio_admission_ref != portfolio.id
            or commitment.reservation_ref != reservation.id
        ):
            raise ValueError("commitment identity rebound")
    else:
        raise ValueError("commitment identity rebound")

    work = kernel.materialize_work(commitment.id)
    return ResponsibilityWorkAdmissionResult(
        status="work-materialized",
        proposal_ref=proposal.id,
        policy_ref=policy.policy_ref,
        priority_judgment_ref=priority.id,
        resource_pool_ref=pool.id,
        portfolio_admission_ref=portfolio.id,
        reservation_ref=reservation.id,
        commitment_ref=commitment.id,
        work_ref=work.id,
    )


__all__ = [
    "BoundedLocalResponsibilityAdmissionPolicy",
    "PriorityPolicyDecision",
    "ResponsibilityAdmissionPolicy",
    "ResponsibilityWorkAdmissionResult",
    "ResponsibilityWorkAdmissionStatus",
    "admit_responsibility_proposal",
]
