from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request

from portable_runtime.api.http import create_app
from portable_runtime.core.runtime import Runtime
from portable_runtime.public_contracts.catalog import contract_catalog
from portable_runtime.public_contracts.experience import (
    commit_historical_experience_use_contract,
    evaluate_experience_use_contract,
    get_historical_experience_use_contract,
)
from portable_runtime.public_contracts.models import (
    ApiProblemV1,
    ExperienceUseAdmissionV1,
    ExperienceUseRequirementV1,
    HistoricalExperienceUseCommitV1,
    HistoricalExperienceUseV1,
)
from portable_runtime.public_contracts.responsibility import (
    DomainResponsibilityProposalReceiptV1,
    DomainResponsibilityProposalV1,
    ResponsibilityWorkAdmissionReceiptV1,
    ResponsibilityWorkAdmissionV1,
    admit_responsibility_work,
    record_domain_responsibility_proposal,
)
from portable_runtime.responsibility.admission import (
    BoundedLocalResponsibilityAdmissionPolicy,
    ResponsibilityAdmissionPolicy,
)


def _problem(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return ApiProblemV1(
        schema="api-problem-v1",
        code=code,
        message=message,
        details=details or {},
    ).model_dump(mode="json")


def _require_local_mutation(request: Request) -> None:
    client = request.client
    host = client.host if client is not None else None
    if host not in {None, "127.0.0.1", "::1", "localhost", "testclient", "testserver"}:
        raise HTTPException(
            status_code=403,
            detail=_problem("LocalControlRequired", "mutating contract API is local-only"),
        )


def contract_router(
    runtime: Runtime,
    *,
    responsibility_admission_policy: ResponsibilityAdmissionPolicy | None = None,
) -> APIRouter:
    router = APIRouter()
    admission_policy = (
        responsibility_admission_policy or BoundedLocalResponsibilityAdmissionPolicy()
    )

    @router.get("/v1/contracts")
    def get_contracts() -> dict[str, Any]:
        return contract_catalog()

    @router.post("/v1/experience/use/evaluate", response_model=ExperienceUseAdmissionV1)
    def evaluate_experience(value: ExperienceUseRequirementV1) -> ExperienceUseAdmissionV1:
        try:
            return evaluate_experience_use_contract(runtime, value)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=_problem("InvalidContractInput", str(exc)),
            ) from exc

    @router.post("/v1/experience/historical-use/commit", response_model=HistoricalExperienceUseV1)
    def commit_historical_experience(
        value: HistoricalExperienceUseCommitV1,
        request: Request,
    ) -> HistoricalExperienceUseV1:
        _require_local_mutation(request)
        try:
            return commit_historical_experience_use_contract(runtime, value)
        except ValueError as exc:
            message = str(exc)
            code = "HistoricalUseCommitRejected"
            if "rebound" in message:
                code = "HistoricalUseIdentityRebound"
            elif "backfill" in message or "qualifies selected experience" in message:
                code = "HistoricalUseSelfQualificationForbidden"
            elif "changed" in message or "digest" in message:
                code = "HistoricalUseDigestMismatch"
            raise HTTPException(status_code=409, detail=_problem(code, message)) from exc

    @router.get("/v1/experience/historical-use/{judgment_id}", response_model=HistoricalExperienceUseV1)
    def historical_experience(judgment_id: str) -> HistoricalExperienceUseV1:
        value = get_historical_experience_use_contract(runtime, judgment_id)
        if value is None:
            raise HTTPException(
                status_code=404,
                detail=_problem("HistoricalUseNotFound", "historical experience use not found"),
            )
        return value

    @router.post(
        "/v1/responsibilities/domain-proposals",
        response_model=DomainResponsibilityProposalReceiptV1,
    )
    def domain_responsibility_proposal(
        value: DomainResponsibilityProposalV1,
        request: Request,
    ) -> DomainResponsibilityProposalReceiptV1:
        _require_local_mutation(request)
        try:
            return record_domain_responsibility_proposal(runtime, value)
        except ValueError as exc:
            message = str(exc)
            code = "DomainResponsibilityProposalRejected"
            if "rebound" in message:
                code = "DomainResponsibilityIdentityRebound"
            elif "stale" in message or "fresh" in message:
                code = "DomainResponsibilityProposalStale"
            raise HTTPException(status_code=409, detail=_problem(code, message)) from exc

    @router.post(
        "/v1/responsibilities/work-admissions",
        response_model=ResponsibilityWorkAdmissionReceiptV1,
    )
    def responsibility_work_admission(
        value: ResponsibilityWorkAdmissionV1,
        request: Request,
    ) -> ResponsibilityWorkAdmissionReceiptV1:
        _require_local_mutation(request)
        try:
            return admit_responsibility_work(
                runtime,
                value,
                policy=admission_policy,
            )
        except ValueError as exc:
            message = str(exc)
            if "unknown WorkProposal" in message:
                raise HTTPException(
                    status_code=404,
                    detail=_problem("ResponsibilityProposalNotFound", message),
                ) from exc
            code = "ResponsibilityWorkAdmissionRejected"
            if "policy mismatch" in message or "different priority policy" in message:
                code = "ResponsibilityAdmissionPolicyMismatch"
            elif "stale" in message or "fresh" in message:
                code = "ResponsibilityProposalStale"
            elif "rebound" in message:
                code = "ResponsibilityAdmissionIdentityRebound"
            raise HTTPException(status_code=409, detail=_problem(code, message)) from exc

    return router


def create_public_app(
    runtime: Runtime | None = None,
    *,
    responsibility_admission_policy: ResponsibilityAdmissionPolicy | None = None,
) -> FastAPI:
    """Return the existing control-plane app with canonical contract routes attached."""

    runtime = runtime or Runtime()
    app = create_app(runtime)
    app.include_router(
        contract_router(
            runtime,
            responsibility_admission_policy=responsibility_admission_policy,
        )
    )
    return app
