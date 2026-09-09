from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.models import Event, StepAttempt, utcnow
from portable_runtime.core.provider_semantics import ProviderSemanticContract
from portable_runtime.core.runtime import Runtime
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)
from portable_runtime.responsibility.domain_effect_activation import (
    DomainEffectRunActivation,
    DomainEffectRunActivationInput,
)
from portable_runtime.responsibility.domain_effect_authorization import (
    DomainEffectAuthorizationAdmission,
    DomainEffectIntentEvidenceInput,
)
from portable_runtime.responsibility.domain_effect_invocation_specification import (
    DomainEffectInvocationSpecificationCapture,
    DomainEffectInvocationSpecificationInput,
)
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessInput,
)
from portable_runtime.responsibility.domain_effect_provider_binding import (
    DomainEffectProviderBindingAssessment,
    DomainEffectProviderBindingInput,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DomainEffectQualificationAssessment,
    DomainEffectQualificationInput,
)
from portable_runtime.responsibility.domain_effect_reality_execution import (
    DomainEffectRealityExecution,
)
from portable_runtime.responsibility.domain_effect_request import (
    DOMAIN_EFFECT_REQUEST_EVENT,
    DomainEffectExecutionRequestPreparation,
    DomainEffectExecutionRequestPreparationInput,
)
from portable_runtime.responsibility.domain_effect_run import (
    DomainEffectRunPreparation,
    DomainEffectRunPreparationInput,
)
from portable_runtime.responsibility.domain_effect_terminal_completion import (
    DomainEffectTerminalCompletion,
    DomainEffectTerminalCompletionInput,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DomainEffectVerifiedOutcomeVerification,
)

BOUNDED_DOMAIN_EFFECT_EXECUTION_EVENT = "bounded-domain-effect-execution-receipt-recorded"
BOUNDED_DOMAIN_EFFECT_EXECUTION_SCHEMA = "bounded-domain-effect-execution-v1"
BOUNDED_DOMAIN_EFFECT_EXECUTION_RECEIPT_SCHEMA = "bounded-domain-effect-execution-receipt-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


class BoundedDomainEffectExecutionV1(BaseModel):
    """Ask Kernel to execute one already-admitted bounded domain effect.

    The command carries domain evidence, not runtime action authority. Provider
    selection, verifier selection, semantic provider binding, lease ownership,
    AuthorizationUse, InvocationPermit, Action and the RealityBoundary remain
    server-owned.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["bounded-domain-effect-execution-v1"] = Field(
        "bounded-domain-effect-execution-v1",
        alias="schema",
    )
    work_ref: str = Field(min_length=1)
    domain_intent_ref: str = Field(min_length=1)
    domain_grant_ref: str = Field(min_length=1)
    governance_basis_ref: str = Field(min_length=1)
    approval_satisfaction_ref: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    authority_epoch: int = Field(ge=0)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


class BoundedDomainEffectExecutionReceiptV1(BaseModel):
    """Non-authoritative durable execution projection."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    schema_: Literal["bounded-domain-effect-execution-receipt-v1"] = Field(
        "bounded-domain-effect-execution-receipt-v1",
        alias="schema",
    )
    execution_ref: str
    status: Literal[
        "authorization-rejected",
        "execution-failed",
        "execution-unknown",
        "verified-fail",
        "completed",
    ]
    work_ref: str
    run_ref: str | None = None
    request_ref: str | None = None
    authorization_ref: str | None = None
    provider_id: str | None = None
    action_ref: str | None = None
    outcome_ref: str | None = None
    evidence_ref: str | None = None
    responsibility_ref: str | None = None
    processed_at: datetime = Field(default_factory=utcnow)
    authority_bearing: Literal[False] = False


@dataclass(frozen=True, slots=True)
class BoundedDomainEffectExecutionProfile:
    """Server-owned physical execution profile; never supplied by the domain."""

    capability: str
    provider_id: str
    verifier_provider_id: str
    semantic_contract: ProviderSemanticContract
    lease_owner: str = "kernel:bounded-domain-effect-executor"
    lease_ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.capability.strip():
            raise ValueError("bounded domain-effect profile requires capability")
        if not self.provider_id.strip() or not self.verifier_provider_id.strip():
            raise ValueError("bounded domain-effect profile requires provider and verifier ids")
        if self.semantic_contract.provider_id != self.provider_id:
            raise ValueError("bounded domain-effect semantic contract/provider mismatch")
        if self.lease_ttl_seconds <= 0:
            raise ValueError("bounded domain-effect lease TTL must be positive")


class BoundedDomainEffectExecutionService:
    """High-level public command over the existing Kernel-owned effect pipeline.

    This service deliberately composes the canonical internal stages instead of
    exporting them one-by-one. A downstream domain can request one bounded
    execution, but cannot choose provider routing, mint runtime authority or
    cross reality except through the existing ``RealityBoundary``.
    """

    def __init__(
        self,
        runtime: Runtime,
        profiles: list[BoundedDomainEffectExecutionProfile],
    ) -> None:
        self.runtime = runtime
        self.profiles = {profile.capability: profile for profile in profiles}
        if len(self.profiles) != len(profiles):
            raise ValueError("bounded domain-effect profiles must be unique by capability")

    async def execute(
        self,
        command: BoundedDomainEffectExecutionV1,
        *,
        processed_at: datetime | None = None,
    ) -> BoundedDomainEffectExecutionReceiptV1:
        execution_ref = self.execution_ref(command)
        replay = self.inspect(execution_ref)
        if replay is not None:
            return replay

        profile = self.profiles.get(command.capability)
        if profile is None:
            raise ValueError("bounded domain-effect capability is not configured server-side")
        at = processed_at or utcnow()
        intent = DomainEffectIntentEvidenceInput(
            work_ref=command.work_ref,
            domain_intent_ref=command.domain_intent_ref,
            domain_grant_ref=command.domain_grant_ref,
            governance_basis_ref=command.governance_basis_ref,
            approval_satisfaction_ref=command.approval_satisfaction_ref,
            capability=command.capability,
            subject_ref=command.subject_ref,
            authority_epoch=command.authority_epoch,
            parameters=dict(command.parameters),
            expected_postcondition=dict(command.expected_postcondition),
            observed_at=command.observed_at,
        )
        authorization = DomainEffectAuthorizationAdmission(
            self.runtime.store,
            contract_registry=self.runtime.contract_registry,
        ).admit(intent, now=at)
        if authorization.status != "authorized" or authorization.authorization_ref is None:
            return self._record(
                BoundedDomainEffectExecutionReceiptV1(
                    schema="bounded-domain-effect-execution-receipt-v1",
                    execution_ref=execution_ref,
                    status="authorization-rejected",
                    work_ref=command.work_ref,
                    authorization_ref=authorization.authorization_ref,
                    processed_at=at,
                )
            )

        run_result = DomainEffectRunPreparation(
            self.runtime.store,
            contract_registry=self.runtime.contract_registry,
        ).prepare(
            DomainEffectRunPreparationInput(
                authorization_ref=authorization.authorization_ref,
            ),
            prepared_at=at,
        )
        request = self._prepared_request(run_result.run_ref)
        if request is None:
            request = DomainEffectExecutionRequestPreparation(self.runtime.store).prepare(
                DomainEffectExecutionRequestPreparationInput(run_ref=run_result.run_ref),
                prepared_at=at,
            ).request

        existing_attempt = self._attempt_for_request(run_result.run_ref, request.id)
        if existing_attempt is not None:
            return await self._resume_from_attempt(
                command,
                execution_ref,
                run_result.run_ref,
                request,
                authorization.authorization_ref,
                profile,
                existing_attempt,
                at,
            )

        activation = DomainEffectRunActivation(self.runtime.store).activate(
            DomainEffectRunActivationInput(run_ref=run_result.run_ref),
            owner=profile.lease_owner,
            ttl_seconds=profile.lease_ttl_seconds,
            activated_at=at,
        )
        DomainEffectQualificationAssessment(self.runtime.store).assess(
            DomainEffectQualificationInput(run_ref=activation.run_ref),
            assessed_at=at,
        )
        DomainEffectProcedureReadinessAssessment(
            self.runtime.store,
            contract_registry=self.runtime.contract_registry,
        ).assess(
            DomainEffectProcedureReadinessInput(run_ref=activation.run_ref),
            assessed_at=at,
        )
        provider_binding = DomainEffectProviderBindingAssessment(
            self.runtime.store,
            self.runtime.registry,
            contract_registry=self.runtime.contract_registry,
        )
        provider_binding.assess(
            DomainEffectProviderBindingInput(
                run_ref=activation.run_ref,
                provider_id=profile.provider_id,
                runtime_id=self.runtime.runtime_id,
                semantic_contract=profile.semantic_contract,
            ),
            assessed_at=at,
        )
        DomainEffectInvocationSpecificationCapture(
            self.runtime.store,
            provider_binding,
        ).capture(
            DomainEffectInvocationSpecificationInput(run_ref=activation.run_ref),
            captured_at=at,
        )

        result = await DomainEffectRealityExecution(
            self.runtime.boundary,
            DomainEffectActionAuthorityResolver(
                self.runtime.store,
                self.runtime.registry,
            ),
        ).execute(request)
        attempt = self._attempt_for_request(run_result.run_ref, request.id)
        if attempt is None:
            raise ValueError("bounded domain-effect execution returned without durable Attempt")
        if result.status != "succeeded":
            result_status: Literal["execution-failed", "execution-unknown"] = (
                "execution-failed" if result.status == "failed" else "execution-unknown"
            )
            return self._record(
                self._runtime_receipt(
                    command,
                    execution_ref,
                    run_result.run_ref,
                    request,
                    authorization.authorization_ref,
                    profile,
                    attempt,
                    status=result_status,
                    processed_at=at,
                )
            )
        return await self._verify_and_complete(
            command,
            execution_ref,
            run_result.run_ref,
            request,
            authorization.authorization_ref,
            profile,
            attempt,
            at,
        )

    def inspect(self, execution_ref: str) -> BoundedDomainEffectExecutionReceiptV1 | None:
        event = self.runtime.store.get_event(execution_ref)
        if not isinstance(event, Event):
            return None
        if event.type != BOUNDED_DOMAIN_EFFECT_EXECUTION_EVENT:
            raise ValueError("bounded domain-effect execution ref resolves to another event type")
        if not isinstance(event.payload, dict):
            raise ValueError("bounded domain-effect execution receipt payload is invalid")
        return BoundedDomainEffectExecutionReceiptV1.model_validate(event.payload)

    @staticmethod
    def execution_ref(command: BoundedDomainEffectExecutionV1) -> str:
        return _stable_id(
            "execution_bounded_domain_effect",
            command.work_ref,
            command.domain_intent_ref,
            command.domain_grant_ref,
            command.capability,
            command.subject_ref,
            command.authority_epoch,
        )

    def _prepared_request(self, run_ref: str) -> CapabilityRequest | None:
        events = [
            event
            for event in self.runtime.store.list_events(run_ref)
            if event.type == DOMAIN_EFFECT_REQUEST_EVENT
        ]
        if not events:
            return None
        if len(events) != 1:
            raise ValueError("bounded domain-effect Run has multiple prepared requests")
        payload = events[0].payload if isinstance(events[0].payload, dict) else {}
        raw = payload.get("request")
        if not isinstance(raw, dict):
            raise ValueError("bounded domain-effect prepared request payload is invalid")
        return CapabilityRequest.model_validate(raw)

    def _attempt_for_request(self, run_ref: str, request_ref: str) -> StepAttempt | None:
        attempts: list[StepAttempt] = []
        for step in self.runtime.store.list_steps(run_ref):
            attempts.extend(
                attempt
                for attempt in self.runtime.store.list_attempts(step.id)
                if isinstance(attempt, StepAttempt) and attempt.request_ref == request_ref
            )
        if len(attempts) > 1:
            raise ValueError("bounded domain-effect execution has multiple Attempts for one request")
        return attempts[0] if attempts else None

    async def _resume_from_attempt(
        self,
        command: BoundedDomainEffectExecutionV1,
        execution_ref: str,
        run_ref: str,
        request: CapabilityRequest,
        authorization_ref: str,
        profile: BoundedDomainEffectExecutionProfile,
        attempt: StepAttempt,
        at: datetime,
    ) -> BoundedDomainEffectExecutionReceiptV1:
        if attempt.status == "succeeded":
            return await self._verify_and_complete(
                command,
                execution_ref,
                run_ref,
                request,
                authorization_ref,
                profile,
                attempt,
                at,
            )
        attempt_status: Literal["execution-failed", "execution-unknown"] = (
            "execution-failed" if attempt.status == "failed" else "execution-unknown"
        )
        return self._record(
            self._runtime_receipt(
                command,
                execution_ref,
                run_ref,
                request,
                authorization_ref,
                profile,
                attempt,
                status=attempt_status,
                processed_at=at,
            )
        )

    async def _verify_and_complete(
        self,
        command: BoundedDomainEffectExecutionV1,
        execution_ref: str,
        run_ref: str,
        request: CapabilityRequest,
        authorization_ref: str,
        profile: BoundedDomainEffectExecutionProfile,
        attempt: StepAttempt,
        at: datetime,
    ) -> BoundedDomainEffectExecutionReceiptV1:
        verified = await DomainEffectVerifiedOutcomeVerification(
            self.runtime.boundary,
            verifier_provider_id=profile.verifier_provider_id,
        ).verify_and_confirm(request)
        action_ref = verified.effect_action_ref
        if verified.objective_result != "pass":
            return self._record(
                BoundedDomainEffectExecutionReceiptV1(
                    schema="bounded-domain-effect-execution-receipt-v1",
                    execution_ref=execution_ref,
                    status="verified-fail",
                    work_ref=command.work_ref,
                    run_ref=run_ref,
                    request_ref=request.id,
                    authorization_ref=authorization_ref,
                    provider_id=profile.provider_id,
                    action_ref=action_ref,
                    outcome_ref=verified.outcome_ref,
                    evidence_ref=verified.evidence_ref,
                    processed_at=at,
                )
            )
        completed = DomainEffectTerminalCompletion(self.runtime.store).complete(
            DomainEffectTerminalCompletionInput(outcome_ref=verified.outcome_ref)
        )
        return self._record(
            BoundedDomainEffectExecutionReceiptV1(
                schema="bounded-domain-effect-execution-receipt-v1",
                execution_ref=execution_ref,
                status="completed",
                work_ref=completed.work_ref,
                run_ref=completed.run_ref,
                request_ref=request.id,
                authorization_ref=authorization_ref,
                provider_id=profile.provider_id,
                action_ref=completed.action_ref,
                outcome_ref=completed.outcome_ref,
                evidence_ref=verified.evidence_ref,
                responsibility_ref=completed.responsibility_ref,
                processed_at=at,
            )
        )

    @staticmethod
    def _action_ref(attempt: StepAttempt) -> str | None:
        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        value = metadata.get("action_ref")
        return value if isinstance(value, str) and value else None

    def _runtime_receipt(
        self,
        command: BoundedDomainEffectExecutionV1,
        execution_ref: str,
        run_ref: str,
        request: CapabilityRequest,
        authorization_ref: str,
        profile: BoundedDomainEffectExecutionProfile,
        attempt: StepAttempt,
        *,
        status: Literal["execution-failed", "execution-unknown"],
        processed_at: datetime,
    ) -> BoundedDomainEffectExecutionReceiptV1:
        return BoundedDomainEffectExecutionReceiptV1(
            schema="bounded-domain-effect-execution-receipt-v1",
            execution_ref=execution_ref,
            status=status,
            work_ref=command.work_ref,
            run_ref=run_ref,
            request_ref=request.id,
            authorization_ref=authorization_ref,
            provider_id=profile.provider_id,
            action_ref=self._action_ref(attempt),
            processed_at=processed_at,
        )

    def _record(
        self,
        receipt: BoundedDomainEffectExecutionReceiptV1,
    ) -> BoundedDomainEffectExecutionReceiptV1:
        existing = self.inspect(receipt.execution_ref)
        if existing is not None:
            if existing != receipt:
                raise ValueError("bounded domain-effect execution receipt identity rebound")
            return existing
        event = Event(
            id=receipt.execution_ref,
            created_at=receipt.processed_at,
            type=BOUNDED_DOMAIN_EFFECT_EXECUTION_EVENT,
            subject_ref=receipt.work_ref,
            payload=receipt.model_dump(mode="json", by_alias=True),
        )
        self.runtime.store.append_event(event)
        persisted = self.inspect(receipt.execution_ref)
        if persisted != receipt:
            raise ValueError("bounded domain-effect execution receipt persistence mismatch")
        return receipt


__all__ = [
    "BOUNDED_DOMAIN_EFFECT_EXECUTION_RECEIPT_SCHEMA",
    "BOUNDED_DOMAIN_EFFECT_EXECUTION_SCHEMA",
    "BoundedDomainEffectExecutionProfile",
    "BoundedDomainEffectExecutionReceiptV1",
    "BoundedDomainEffectExecutionService",
    "BoundedDomainEffectExecutionV1",
]
