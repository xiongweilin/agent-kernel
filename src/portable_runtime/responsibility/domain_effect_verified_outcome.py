from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.core.invocation import InvocationFactory
from portable_runtime.core.models import Action, Event, Run, Step, StepAttempt, Work
from portable_runtime.core.router import ExactProviderRouting, RoutingPolicy
from portable_runtime.governance.dispatch import DISPATCH_COMMIT_EVENT
from portable_runtime.records.models import EvidenceArtifact, OutcomeRecord
from portable_runtime.records.verified_outcome import VerifiedOutcomeAuthority
from portable_runtime.responsibility.domain_effect_authorization import (
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE,
)
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
    DomainEffectAuthorizationUseContext,
)
from portable_runtime.responsibility.domain_effect_completion_contract import (
    DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA,
    require_domain_effect_completion_contract,
)

def domain_effect_verification_capability(effect_capability: str) -> str:
    value = effect_capability.strip()
    if not value:
        raise ValueError("domain effect verification requires a capability")
    return f"verify.{value}"


DOMAIN_EFFECT_VERIFICATION_CAPABILITY = domain_effect_verification_capability(
    ADMINISTRATIVE_HRIS_EMPLOYEE_CREATE
)
DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA = "domain-effect-objective-verification-evidence-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _BoundaryVerificationScope:
    registry: Any
    store: Any
    routing: RoutingPolicy


@dataclass(frozen=True, slots=True)
class _EffectExecutionGraph:
    work: Work
    run: Run
    step: Step
    attempt: StepAttempt
    action: Action
    dispatch: Event
    authorization: DomainEffectAuthorizationUseContext


class DomainEffectVerifiedOutcomeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["confirmed"] = "confirmed"
    objective_result: Literal["pass", "fail"]
    outcome_ref: str = Field(min_length=1)
    evidence_ref: str = Field(min_length=1)
    effect_action_ref: str = Field(min_length=1)
    effect_attempt_ref: str = Field(min_length=1)
    verification_request_ref: str = Field(min_length=1)
    verification_attempt_ref: str = Field(min_length=1)
    verifier_provider_id: str = Field(min_length=1)
    verifier_provider_execution_binding_ref: str = Field(min_length=1)
    authority_bearing: bool = False


class DomainEffectVerifiedOutcomeVerification:
    """Read reality independently, persist bound proof, then invoke Outcome authority.

    The effect provider's CapabilityResult is never an authority input here.
    A separate read-only verifier runs through the existing RealityBoundary.
    Its closed result remains non-authoritative until this adapter binds the
    verifier execution and observed postcondition to the exact effect
    Action/Attempt and persists a canonical EvidenceArtifact. Only the existing
    VerifiedOutcomeAuthority may then materialize a confirmed Outcome.

    The verifier is additionally bound to the Work completion contract frozen
    before the domain-effect Run existed. Verification cannot redefine scope,
    acceptance criteria, Work version, subject versions, or obligation coverage
    after observing reality.
    """

    def __init__(
        self,
        boundary: RealityBoundary,
        *,
        verifier_provider_id: str,
    ) -> None:
        if boundary.store is None:
            raise ValueError("domain effect verification requires an authoritative store")
        if boundary.registry is None:
            raise ValueError("domain effect verification requires a provider registry")
        if not verifier_provider_id.strip():
            raise ValueError("domain effect verifier provider id must be non-empty")
        self.boundary = boundary
        self.store = boundary.store
        self.registry = boundary.registry
        self.verifier_provider_id = verifier_provider_id

    async def verify_and_confirm(
        self,
        effect_request: CapabilityRequest,
    ) -> DomainEffectVerifiedOutcomeResult:
        graph = self._resolve_effect_execution(effect_request)
        completion_contract, completion_contract_digest = self._completion_contract(graph)
        verification_scope = dict(completion_contract["verification_scope"])
        verifier_binding = self.registry.execution_binding(self.verifier_provider_id)
        verification_request = self._verification_request(
            effect_request,
            graph,
            verification_scope,
            verifier_binding.id,
        )
        routing = ExactProviderRouting(self.verifier_provider_id, self.boundary.routing)
        scope = _BoundaryVerificationScope(
            registry=self.registry,
            store=self.store,
            routing=routing,
        )
        result = await self.boundary.execute(
            verification_request,
            capability_service=scope,
        )
        current_binding = self.registry.execution_binding(self.verifier_provider_id)
        if current_binding != verifier_binding:
            raise ValueError("domain effect verifier configured target changed during verification")
        self._validate_verifier_result(result, verification_scope)
        verification_attempt = self._require_single_attempt(
            graph.run.id,
            verification_request.id,
        )
        proof = self._verification_evidence(
            graph,
            effect_request,
            verification_request,
            verification_attempt,
            verification_scope,
            completion_contract,
            completion_contract_digest,
            verifier_binding.id,
            result,
        )
        self.store.save_record(proof)
        outcome = VerifiedOutcomeAuthority(self.store).confirm(
            action_ref=graph.action.id,
            evidence_refs=[proof.id],
            expected_work_id=graph.work.id,
            expected_run_id=graph.run.id,
            expected_request_id=effect_request.id,
            expected_attempt_ref=graph.attempt.id,
            verification_scope=verification_scope,
            subject_version_refs=list(completion_contract["subject_version_refs"]),
        )
        return self._result(
            graph,
            verification_request,
            verification_attempt,
            verifier_binding.id,
            proof,
            outcome,
        )

    def _resolve_effect_execution(
        self,
        request: CapabilityRequest,
    ) -> _EffectExecutionGraph:
        if not request.work_id or not request.run_id:
            raise ValueError("domain effect verification requires Work/Run-bound request")
        work = self.store.get_work(request.work_id)
        run = self.store.get_run(request.run_id)
        if not isinstance(work, Work) or not isinstance(run, Run):
            raise ValueError("domain effect verification requires durable Work and Run")
        if run.work_id != work.id:
            raise ValueError("domain effect verification Work/Run binding mismatch")

        attempts = self._attempts_for_request(run.id, request.id)
        if len(attempts) != 1:
            raise ValueError(
                "bounded domain effect verification requires exactly one effect Attempt"
            )
        attempt = attempts[0]
        step = self.store.get_step(attempt.step_id)
        if not isinstance(step, Step) or step.run_id != run.id:
            raise ValueError("domain effect verification Attempt/Step/Run binding mismatch")
        action_ref = attempt.metadata.get("action_ref") if isinstance(attempt.metadata, dict) else None
        if not isinstance(action_ref, str) or not action_ref:
            raise ValueError("domain effect effect Attempt lacks durable Action binding")
        action = self.store.get_action(action_ref)
        if not isinstance(action, Action):
            raise ValueError("domain effect effect Action is unavailable")
        if (
            action.work_id != work.id
            or action.run_id != run.id
            or action.request_ref != request.id
            or action.provider_id != attempt.provider_id
            or action.capability != request.capability
        ):
            raise ValueError("domain effect execution graph identity rebound")

        dispatches = [
            event
            for event in self.store.list_events(request.id)
            if event.type == DISPATCH_COMMIT_EVENT
        ]
        if len(dispatches) != 1:
            raise ValueError("domain effect verification requires exactly one dispatch commitment")
        dispatch = dispatches[0]
        payload = dispatch.payload if isinstance(dispatch.payload, dict) else {}
        expected_dispatch = {
            "request_id": request.id,
            "provider_id": action.provider_id,
            "attempt_ref": attempt.id,
        }
        for key, expected in expected_dispatch.items():
            if payload.get(key) != expected:
                raise ValueError(f"domain effect dispatch binding rebound at {key}")
        for key in (
            "authorization_use_ref",
            "invocation_specification_ref",
            "provider_execution_binding_ref",
        ):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"domain effect dispatch lacks {key}")

        run_metadata = run.metadata if isinstance(run.metadata, dict) else {}
        authorization_ref = run_metadata.get("domain_effect_authorization_ref")
        if not isinstance(authorization_ref, str) or not authorization_ref:
            raise ValueError("domain effect Run lacks runtime authorization ref")
        authorization = DomainEffectAuthorizationUseConsumption(self.store)._resolve_context(
            authorization_ref
        )
        authorization_use = self.store.get_authorization_use(payload["authorization_use_ref"])
        if authorization_use is None or getattr(authorization_use, "authorization_ref", None) != authorization.grant.id:
            raise ValueError("domain effect dispatch AuthorizationUse is unavailable or rebound")
        self._validate_effect_request(request, authorization)
        return _EffectExecutionGraph(
            work=work,
            run=run,
            step=step,
            attempt=attempt,
            action=action,
            dispatch=dispatch,
            authorization=authorization,
        )

    def _completion_contract(
        self,
        graph: _EffectExecutionGraph,
    ) -> tuple[dict[str, Any], str]:
        contract, digest = require_domain_effect_completion_contract(
            graph.work,
            graph.authorization,
        )
        run_metadata = graph.run.metadata if isinstance(graph.run.metadata, dict) else {}
        if run_metadata.get("domain_effect_completion_contract_digest") != digest:
            raise ValueError("domain effect Run completion contract binding drifted")
        return contract, digest

    def _attempts_for_request(self, run_id: str, request_id: str) -> list[StepAttempt]:
        attempts: list[StepAttempt] = []
        for step in self.store.list_steps(run_id):
            attempts.extend(
                attempt
                for attempt in self.store.list_attempts(step.id)
                if isinstance(attempt, StepAttempt) and attempt.request_ref == request_id
            )
        return attempts

    def _require_single_attempt(self, run_id: str, request_id: str) -> StepAttempt:
        attempts = self._attempts_for_request(run_id, request_id)
        if len(attempts) != 1:
            raise ValueError("domain effect verification execution requires exactly one Attempt")
        attempt = attempts[0]
        if attempt.status != "succeeded":
            raise ValueError("domain effect verification provider execution did not succeed")
        return attempt

    @staticmethod
    def _validate_effect_request(
        request: CapabilityRequest,
        authorization: DomainEffectAuthorizationUseContext,
    ) -> None:
        expected = authorization.request
        if expected.capability != request.capability:
            raise ValueError("domain effect verification capability rebound")
        if expected.actor_ref != (request.actor_ref or ""):
            raise ValueError("domain effect verification actor rebound")
        if expected.resource_ref != (request.resource_ref or ""):
            raise ValueError("domain effect verification resource rebound")
        if expected.effect_class != request.effect_class:
            raise ValueError("domain effect verification effect class rebound")
        if set(expected.subject_version_refs) != set(request.subject_version_refs):
            raise ValueError("domain effect verification subject versions rebound")
        if authorization.intent.parameters != request.parameters:
            raise ValueError("domain effect verification effect parameters rebound")

    def _verification_request(
        self,
        effect_request: CapabilityRequest,
        graph: _EffectExecutionGraph,
        verification_scope: dict[str, Any],
        verifier_binding_ref: str,
    ) -> CapabilityRequest:
        request_id = _stable_id(
            "request_domain_effect_verification",
            graph.action.id,
            verifier_binding_ref,
            _canonical_digest(verification_scope),
        )
        return InvocationFactory(
            store=self.store,
            contract_registry=self.boundary.contract_registry,
        ).build(
            domain_effect_verification_capability(effect_request.capability),
            work_id=graph.work.id,
            run_id=graph.run.id,
            parameters={"verification_scope": verification_scope},
            resource_ref=effect_request.resource_ref,
            subject_version_refs=list(effect_request.subject_version_refs),
            effect_class="read",
            idempotency_key=f"verify:{graph.action.id}:{verifier_binding_ref}",
            step_key=f"verify-domain-effect:{graph.action.id}:{verifier_binding_ref}",
            request_id=request_id,
        )

    def _validate_verifier_result(
        self,
        result: CapabilityResult,
        verification_scope: dict[str, Any],
    ) -> None:
        if result.provider_id != self.verifier_provider_id:
            raise ValueError("domain effect verification returned from unexpected provider")
        if result.status != "succeeded":
            raise ValueError("domain effect verifier execution did not succeed")
        closed = result.verification_result
        if closed is None or closed.result not in {"pass", "fail"}:
            raise ValueError("domain effect verifier returned no closed verification result")
        observed = result.metadata.get("observed_postcondition")
        if not isinstance(observed, dict):
            raise ValueError("domain effect verifier omitted observed postcondition")
        expected = verification_scope["expected_postcondition"]
        matches = observed == expected
        if closed.result == "pass" and not matches:
            raise ValueError("domain effect verifier pass contradicts its observed postcondition")
        if closed.result == "fail" and matches:
            raise ValueError("domain effect verifier fail contradicts its observed postcondition")

    def _verification_evidence(
        self,
        graph: _EffectExecutionGraph,
        effect_request: CapabilityRequest,
        verification_request: CapabilityRequest,
        verification_attempt: StepAttempt,
        verification_scope: dict[str, Any],
        completion_contract: dict[str, Any],
        completion_contract_digest: str,
        verifier_binding_ref: str,
        result: CapabilityResult,
    ) -> EvidenceArtifact:
        closed = result.verification_result
        if closed is None:
            raise ValueError("closed verification disappeared before evidence capture")
        evidence_id = _stable_id(
            "evidence_domain_effect_verification",
            graph.action.id,
            verification_attempt.id,
            verifier_binding_ref,
            closed.result,
        )
        return EvidenceArtifact(
            id=evidence_id,
            kind="task-objective-proof",
            source_refs=[graph.action.id],
            scope=dict(verification_scope),
            lifecycle_status="current",
            metadata={
                "schema": DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA,
                "verification_result": closed.model_dump(mode="json"),
                "proof_class": "objective-verification",
                "action_ref": graph.action.id,
                "request_id": effect_request.id,
                "attempt_ref": graph.attempt.id,
                "work_id": graph.work.id,
                "run_id": graph.run.id,
                "verification_scope": dict(verification_scope),
                "work_version": completion_contract["work_version"],
                "acceptance_criteria": list(completion_contract["acceptance_criteria"]),
                "subject_version_refs": list(completion_contract["subject_version_refs"]),
                "obligation_refs": list(completion_contract["required_obligations"]),
                "domain_effect_completion_contract_digest": completion_contract_digest,
                "artifact_refs": list(closed.artifact_refs),
                "observed_postcondition": dict(result.metadata["observed_postcondition"]),
                "effect_dispatch_ref": graph.dispatch.id,
                "effect_authorization_use_ref": graph.dispatch.payload[
                    "authorization_use_ref"
                ],
                "effect_invocation_specification_ref": graph.dispatch.payload[
                    "invocation_specification_ref"
                ],
                "effect_provider_execution_binding_ref": graph.dispatch.payload[
                    "provider_execution_binding_ref"
                ],
                "verification_request_ref": verification_request.id,
                "verification_attempt_ref": verification_attempt.id,
                "verifier_provenance": {
                    "provider_id": result.provider_id,
                    "verifier_id": result.provider_id,
                    "method": DOMAIN_EFFECT_VERIFICATION_CAPABILITY,
                    "provider_execution_binding_ref": verifier_binding_ref,
                    "verification_request_ref": verification_request.id,
                    "verification_attempt_ref": verification_attempt.id,
                },
            },
        )

    def _result(
        self,
        graph: _EffectExecutionGraph,
        verification_request: CapabilityRequest,
        verification_attempt: StepAttempt,
        verifier_binding_ref: str,
        proof: EvidenceArtifact,
        outcome: OutcomeRecord,
    ) -> DomainEffectVerifiedOutcomeResult:
        objective_result = outcome.metadata.get("objective_result")
        if objective_result not in {"pass", "fail"}:
            raise ValueError("confirmed domain effect Outcome lacks objective result")
        return DomainEffectVerifiedOutcomeResult(
            objective_result=objective_result,
            outcome_ref=outcome.id,
            evidence_ref=proof.id,
            effect_action_ref=graph.action.id,
            effect_attempt_ref=graph.attempt.id,
            verification_request_ref=verification_request.id,
            verification_attempt_ref=verification_attempt.id,
            verifier_provider_id=self.verifier_provider_id,
            verifier_provider_execution_binding_ref=verifier_binding_ref,
        )


__all__ = [
    "DOMAIN_EFFECT_VERIFICATION_CAPABILITY",
    "DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA",
    "domain_effect_verification_capability",
    "DOMAIN_EFFECT_VERIFICATION_SCOPE_SCHEMA",
    "DomainEffectVerifiedOutcomeResult",
    "DomainEffectVerifiedOutcomeVerification",
]
