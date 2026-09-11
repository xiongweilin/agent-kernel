from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from portable_runtime.responsibility.admission import (
    BoundedLocalResponsibilityAdmissionPolicy,
    admit_responsibility_proposal,
)
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.models import (
    EffectClass,
    ResourceVector,
    ResponsibilityAdmission,
    ResponsibilityAssessment,
    StandingResponsibility,
    WorkProposal,
)
from portable_runtime.responsibility.service import ResponsibilityKernel
from portable_runtime.stores.memory import InMemoryStateStore

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _kernel_with_external_proposal() -> tuple[ResponsibilityKernel, WorkProposal]:
    store = InMemoryStateStore()
    kernel = ResponsibilityKernel(store)
    responsibility = StandingResponsibility(
        id="resp_admin_hris",
        created_at=NOW,
        responsibility_kind="administrative-obligation",
        statement="Discharge one governed HRIS onboarding obligation",
        scope={"case_id": "case-1", "obligation_id": "obligation-1"},
    )
    admission = ResponsibilityAdmission(
        id="admission_admin_hris",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        principal_ref="service:administrative-orchestrator",
        basis_refs=["administrative-grant:grant-1"],
        admitted_at=NOW,
    )
    kernel.register(responsibility, admission)
    assessment = ResponsibilityAssessment(
        id="assessment_admin_hris",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        subject_ref="employee:new",
        assessment_kind="administrative-obligation-ready",
        basis_refs=["administrative-grant:grant-1"],
        assessed_at=NOW,
        fresh_until=NOW + timedelta(minutes=10),
        rationale="domain governance is closed",
    )
    record_domain_assessment(kernel, assessment, now=NOW)
    proposal = WorkProposal(
        id="proposal_admin_hris",
        created_at=NOW,
        responsibility_ref=responsibility.id,
        responsibility_version=1,
        assessment_ref=assessment.id,
        subject_ref="employee:new",
        work_kind="administrative-effect",
        title="Create employee in HRIS",
        requested_resources=ResourceVector(
            api_calls=1,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        requested_capabilities=["administrative.hris.employee.create.v1"],
        expected_result="employee exists with governed onboarding fields",
        effect_class=EffectClass.EXTERNAL_EFFECT,
        fresh_until=NOW + timedelta(minutes=10),
    )
    kernel.propose(proposal, now=NOW)
    return kernel, proposal


def _policy() -> BoundedLocalResponsibilityAdmissionPolicy:
    return BoundedLocalResponsibilityAdmissionPolicy(
        profile_id="administrative-shadow",
        version="1",
        max_request=ResourceVector(
            api_calls=2,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        capacity=ResourceVector(
            api_calls=8,
            concurrency_slots=4,
            domain_quota={"administrative:hris": 4},
        ),
    )


def test_kernel_admission_materializes_every_intermediate_before_work() -> None:
    kernel, proposal = _kernel_with_external_proposal()
    result = admit_responsibility_proposal(
        kernel,
        proposal.id,
        policy=_policy(),
        now=NOW,
    )

    assert result.status == "work-materialized"
    assert result.priority_judgment_ref is not None
    assert result.resource_pool_ref is not None
    assert result.portfolio_admission_ref is not None
    assert result.reservation_ref is not None
    assert result.commitment_ref is not None
    assert result.work_ref is not None

    assert kernel.journal.get(result.priority_judgment_ref) is not None
    assert kernel.journal.get(result.resource_pool_ref) is not None
    assert kernel.journal.get(result.portfolio_admission_ref) is not None
    assert kernel.journal.get(result.reservation_ref) is not None
    assert kernel.journal.get(result.commitment_ref) is not None

    work = kernel.store.get_work(result.work_ref)
    assert work is not None
    assert work.metadata["responsibility_proposal_ref"] == proposal.id
    assert work.metadata["responsibility_commitment_ref"] == result.commitment_ref
    assert work.metadata["external_effect_authority"] == "required-separately"
    assert kernel.store.list_authorizations() == []


def test_kernel_admission_replay_returns_same_work_without_duplicate_chain() -> None:
    kernel, proposal = _kernel_with_external_proposal()
    first = admit_responsibility_proposal(kernel, proposal.id, policy=_policy(), now=NOW)
    second = admit_responsibility_proposal(
        kernel,
        proposal.id,
        policy=_policy(),
        now=NOW + timedelta(seconds=30),
    )

    assert first == second
    assert len(kernel.store.list_work()) == 1
    priorities = [
        value
        for value in kernel.journal.list("PriorityJudgment")
        if getattr(value, "proposal_ref", None) == proposal.id
    ]
    portfolios = [
        value
        for value in kernel.journal.list("PortfolioAdmissionDecision")
        if getattr(value, "proposal_ref", None) == proposal.id
    ]
    reservations = [
        value
        for value in kernel.journal.list("ResourceReservation")
        if getattr(value, "proposal_ref", None) == proposal.id
    ]
    commitments = [
        value
        for value in kernel.journal.list("Commitment")
        if getattr(value, "proposal_ref", None) == proposal.id
    ]
    assert len(priorities) == 1
    assert len(portfolios) == 1
    assert len(reservations) == 1
    assert len(commitments) == 1
    assert kernel.store.list_authorizations() == []


def test_concurrent_work_admission_serializes_capacity_and_returns_rejection() -> None:
    kernel, template = _kernel_with_external_proposal()
    proposals = [template]
    proposals.extend(
        template.model_copy(update={"id": f"proposal_admin_hris_{index}"})
        for index in range(1, 5)
    )
    for proposal in proposals[1:]:
        kernel.propose(proposal, now=NOW)

    def admit(proposal: WorkProposal):
        return admit_responsibility_proposal(
            kernel,
            proposal.id,
            policy=_policy(),
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=len(proposals)) as executor:
        results = list(executor.map(admit, proposals))

    assert [result.status for result in results].count("work-materialized") == 4
    assert [result.status for result in results].count("portfolio-rejected") == 1
    assert len(kernel.store.list_work()) == 4


def test_kernel_admission_rejection_stops_before_resource_and_work_objects() -> None:
    kernel, proposal = _kernel_with_external_proposal()
    policy = BoundedLocalResponsibilityAdmissionPolicy(
        profile_id="read-only-only",
        version="1",
        max_request=ResourceVector(
            api_calls=2,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        capacity=ResourceVector(
            api_calls=8,
            concurrency_slots=4,
            domain_quota={"administrative:hris": 4},
        ),
        allowed_effect_classes=(EffectClass.READ_ONLY,),
    )

    result = admit_responsibility_proposal(kernel, proposal.id, policy=policy, now=NOW)

    assert result.status == "priority-rejected"
    assert kernel.journal.get(result.priority_judgment_ref) is not None
    assert result.resource_pool_ref is None
    assert result.portfolio_admission_ref is None
    assert result.reservation_ref is None
    assert result.commitment_ref is None
    assert result.work_ref is None
    assert kernel.store.list_work() == []
    assert kernel.store.list_authorizations() == []


def test_kernel_admission_fails_closed_when_proposal_is_stale() -> None:
    kernel, proposal = _kernel_with_external_proposal()

    try:
        admit_responsibility_proposal(
            kernel,
            proposal.id,
            policy=_policy(),
            now=NOW + timedelta(minutes=11),
        )
    except ValueError as exc:
        assert "stale" in str(exc) or "fresh" in str(exc)
    else:
        raise AssertionError("stale proposal must not enter Work admission")

    assert kernel.store.list_work() == []
    assert kernel.store.list_authorizations() == []
