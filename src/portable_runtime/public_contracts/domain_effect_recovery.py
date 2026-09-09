"""Public recovery/resolution surface for ambiguous bounded domain effects.

The original bounded execution receipt is immutable historical projection. Recovery
never rewrites it. An ``execution-unknown`` receipt can later acquire a separate,
monotonic resolution view after the existing B4 authority chain has reconciled the
exact historical dispatch and independent objective verification has completed.

This module does not create retry authority and never calls ``provider.invoke``.
The only external recovery call is owned by ``RecoveryReconciliationConsumer`` via
its exact A/B/C reconciliation boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.boundary_stages import ExecutionRecordIds, commit_execution_projection
from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.core.models import Event, StepAttempt, utcnow
from portable_runtime.governance.dispatch import DISPATCH_COMMIT_EVENT
from portable_runtime.public_contracts.domain_effect import (
    BoundedDomainEffectExecutionReceiptV1,
    BoundedDomainEffectExecutionService,
)
from portable_runtime.responsibility.domain_effect_request import DOMAIN_EFFECT_REQUEST_EVENT
from portable_runtime.responsibility.domain_effect_terminal_completion import (
    DomainEffectTerminalCompletion,
    DomainEffectTerminalCompletionInput,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DomainEffectVerifiedOutcomeVerification,
)
from portable_runtime.workflows.reconciliation_consumer import (
    RecoveryReconciliationConsumer,
    RecoveryReconciliationRequest,
)
from portable_runtime.workflows.recovery_application import RecoveryApplicationCommitRequest
from portable_runtime.workflows.recovery_disposition import (
    RecoveryDispositionBasis,
    RecoveryDispositionCommitRequest,
)
from portable_runtime.workflows.recovery_observation import RecoveryObservationCommitRequest

BOUNDED_DOMAIN_EFFECT_RECOVERY_SCHEMA = "bounded-domain-effect-recovery-v1"
BOUNDED_DOMAIN_EFFECT_RESOLUTION_SCHEMA = "bounded-domain-effect-resolution-v1"
BOUNDED_DOMAIN_EFFECT_RESOLUTION_EVENT = "bounded-domain-effect-resolution-recorded"
BOUNDED_DOMAIN_EFFECT_RECOVERY_POLICY_REF = "bounded-domain-effect-recovery:safe-reconcile@1"

ResolutionStatus = Literal[
    "authorization-rejected",
    "execution-failed",
    "execution-unknown",
    "verified-fail",
    "completed",
    "recovery-pending",
    "recovery-unavailable",
    "manual-resolution-required",
    "recovered-failed",
    "recovered-verified-fail",
    "recovered-completed",
]
_TERMINAL_RECOVERY_STATUSES = frozenset(
    {
        "manual-resolution-required",
        "recovered-failed",
        "recovered-verified-fail",
        "recovered-completed",
    }
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def _resolution_semantics(value: BoundedDomainEffectResolutionV1) -> dict[str, object]:
    payload = value.model_dump(mode="json", by_alias=True)
    payload.pop("processed_at", None)
    return payload


def _resolution_semantic_key(value: BoundedDomainEffectResolutionV1) -> str:
    return json.dumps(
        _resolution_semantics(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class BoundedDomainEffectRecoveryV1(BaseModel):
    """Request recovery of one exact historical bounded execution identity."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_: Literal["bounded-domain-effect-recovery-v1"] = Field(
        "bounded-domain-effect-recovery-v1",
        alias="schema",
    )
    execution_ref: str = Field(min_length=1)


class BoundedDomainEffectResolutionV1(BaseModel):
    """Non-authoritative current projection over immutable execution/recovery facts."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    schema_: Literal["bounded-domain-effect-resolution-v1"] = Field(
        "bounded-domain-effect-resolution-v1",
        alias="schema",
    )
    execution_ref: str = Field(min_length=1)
    original_status: str = Field(min_length=1)
    current_status: ResolutionStatus
    work_ref: str = Field(min_length=1)
    run_ref: str | None = None
    request_ref: str | None = None
    recovery_observation_ref: str | None = None
    recovery_disposition_ref: str | None = None
    recovery_application_ref: str | None = None
    outcome_ref: str | None = None
    evidence_ref: str | None = None
    responsibility_ref: str | None = None
    reason: str = ""
    processed_at: datetime = Field(default_factory=utcnow)
    authority_bearing: Literal[False] = False


class _SafeBoundedRecoveryPolicy:
    """Open reconciliation only for a dispatch already classified reconcilable."""

    def decide(self, basis: RecoveryDispositionBasis) -> str:
        if basis.recovery_mode == "reconcile":
            return "reconcile-again"
        return "require-manual-resolution"


class BoundedDomainEffectRecoveryService:
    """Progress one ``execution-unknown`` receipt through canonical B4 recovery.

    The service composes existing authorities only:

    durable dispatch -> generic unknown RecoveryObservation -> RecoveryDisposition
    -> RecoveryApplication -> exact A/B/C reconciliation -> execution projection
    -> independent objective verification -> original Work completion.

    The historical bounded execution receipt is never rewritten. A separate
    resolution event records current downstream projection after recovery.
    """

    def __init__(self, execution: BoundedDomainEffectExecutionService) -> None:
        self.execution = execution
        self.runtime = execution.runtime

    def inspect(self, execution_ref: str) -> BoundedDomainEffectResolutionV1 | None:
        receipt = self.execution.inspect(execution_ref)
        if receipt is None:
            return None
        events = [
            event
            for event in self.runtime.store.list_events(execution_ref)
            if isinstance(event, Event) and event.type == BOUNDED_DOMAIN_EFFECT_RESOLUTION_EVENT
        ]
        if not events:
            return self._from_receipt(receipt)
        views = [self._view_from_event(event) for event in events]
        terminal = [view for view in views if view.current_status in _TERMINAL_RECOVERY_STATUSES]
        terminal_semantics = {_resolution_semantic_key(view) for view in terminal}
        if len(terminal_semantics) > 1:
            raise ValueError("bounded domain-effect recovery terminal resolution conflicted")
        candidates = terminal or views
        candidates.sort(key=lambda value: (value.processed_at, value.current_status))
        return candidates[-1]

    async def recover(
        self,
        command: BoundedDomainEffectRecoveryV1,
    ) -> BoundedDomainEffectResolutionV1:
        execution_ref = command.execution_ref.strip()
        receipt = self.execution.inspect(execution_ref)
        if receipt is None:
            raise LookupError("bounded domain-effect execution receipt is unavailable")
        current = self.inspect(execution_ref)
        if current is None:
            raise LookupError("bounded domain-effect execution resolution is unavailable")
        if current.current_status in _TERMINAL_RECOVERY_STATUSES:
            return current
        if receipt.status != "execution-unknown":
            return current
        if not receipt.run_ref or not receipt.request_ref:
            raise ValueError("execution-unknown receipt lacks durable Run/request lineage")

        request = self._prepared_request(receipt.run_ref, receipt.request_ref)
        profile = self.execution.profiles.get(request.capability)
        if profile is None:
            raise ValueError("bounded domain-effect recovery capability is not configured")
        dispatch, attempt = self._dispatch_attempt(receipt.run_ref, request.id)

        observation = self.runtime.store.commit_recovery_observation(
            RecoveryObservationCommitRequest(
                observation_instance_ref=f"bounded-domain-effect-ambiguity:{execution_ref}",
                dispatch_commit_ref=dispatch.id,
                observation_source="bounded-domain-effect-execution-unknown",
                reported_status="reported-unknown",
                provenance_refs=(execution_ref,),
            )
        )
        disposition = self.runtime.store.commit_recovery_disposition(
            RecoveryDispositionCommitRequest(
                dispatch_commit_ref=dispatch.id,
                observation_refs=(observation.id,),
                outcome_refs=(),
                policy_ref=BOUNDED_DOMAIN_EFFECT_RECOVERY_POLICY_REF,
            ),
            _SafeBoundedRecoveryPolicy(),
        )
        application = self.runtime.store.commit_recovery_application(
            RecoveryApplicationCommitRequest(disposition_ref=disposition.id)
        )

        pending = self._record(
            BoundedDomainEffectResolutionV1(
                schema="bounded-domain-effect-resolution-v1",
                execution_ref=execution_ref,
                original_status=receipt.status,
                current_status=(
                    "recovery-pending"
                    if application.application_kind == "reconciliation-request"
                    else "manual-resolution-required"
                ),
                work_ref=receipt.work_ref,
                run_ref=receipt.run_ref,
                request_ref=receipt.request_ref,
                recovery_observation_ref=observation.id,
                recovery_disposition_ref=disposition.id,
                recovery_application_ref=application.id,
                reason=(
                    "exact B4 reconciliation responsibility prepared"
                    if application.application_kind == "reconciliation-request"
                    else "dispatch semantics do not authorize automatic reconciliation"
                ),
            )
        )
        if application.application_kind != "reconciliation-request":
            return pending

        consumer = RecoveryReconciliationConsumer(
            store=self.runtime.store,
            registry=self.runtime.registry,
        )
        result = await consumer.consume(
            RecoveryReconciliationRequest(recovery_application_ref=application.id)
        )
        if not result.durable_completion or result.recovery_observation_ref is None:
            return self._record(
                pending.model_copy(
                    update={
                        "current_status": "recovery-unavailable",
                        "reason": result.reason or "reconciliation did not durably complete",
                        "processed_at": utcnow(),
                    }
                )
            )

        reconciled = self.runtime.store.get_recovery_application_observation(application.id)
        if reconciled is None or reconciled.id != result.recovery_observation_ref:
            raise ValueError("reconciliation completion observation is unavailable or rebound")
        if reconciled.reported_status == "reported-unknown":
            return self._record(
                pending.model_copy(
                    update={
                        "current_status": "recovery-unavailable",
                        "recovery_observation_ref": reconciled.id,
                        "reason": "authoritative reconciliation remained unknown",
                        "processed_at": utcnow(),
                    }
                )
            )

        projected_status: Literal["succeeded", "failed"] = (
            "succeeded" if reconciled.reported_status == "reported-succeeded" else "failed"
        )
        projection = commit_execution_projection(
            self.runtime.store,
            request,
            CapabilityResult(
                request_id=request.id,
                provider_id=receipt.provider_id or attempt.provider_id,
                status=projected_status,
                reconciled=True,
            ),
            provider_id=receipt.provider_id or attempt.provider_id,
            records=self._records(attempt),
        )
        if projection.error is not None:
            return self._record(
                pending.model_copy(
                    update={
                        "current_status": "recovery-unavailable",
                        "recovery_observation_ref": reconciled.id,
                        "reason": f"reconciled execution projection failed: {projection.error}",
                        "processed_at": utcnow(),
                    }
                )
            )
        if projected_status != "succeeded":
            return self._record(
                pending.model_copy(
                    update={
                        "current_status": "recovered-failed",
                        "recovery_observation_ref": reconciled.id,
                        "reason": "reconciliation confirmed provider execution failure",
                        "processed_at": utcnow(),
                    }
                )
            )

        verified = await DomainEffectVerifiedOutcomeVerification(
            self.runtime.boundary,
            verifier_provider_id=profile.verifier_provider_id,
        ).verify_and_confirm(request)
        if verified.objective_result != "pass":
            return self._record(
                pending.model_copy(
                    update={
                        "current_status": "recovered-verified-fail",
                        "recovery_observation_ref": reconciled.id,
                        "outcome_ref": verified.outcome_ref,
                        "evidence_ref": verified.evidence_ref,
                        "reason": "reconciled execution exists but objective verification failed",
                        "processed_at": utcnow(),
                    }
                )
            )

        completed = DomainEffectTerminalCompletion(
            self.runtime.store,
            contract_registry=self.runtime.contract_registry,
        ).complete(DomainEffectTerminalCompletionInput(outcome_ref=verified.outcome_ref))
        return self._record(
            pending.model_copy(
                update={
                    "current_status": "recovered-completed",
                    "recovery_observation_ref": reconciled.id,
                    "outcome_ref": completed.outcome_ref,
                    "evidence_ref": verified.evidence_ref,
                    "responsibility_ref": completed.responsibility_ref,
                    "reason": "authority-bound reconciliation and independent verification completed",
                    "processed_at": utcnow(),
                }
            )
        )

    def _from_receipt(
        self,
        receipt: BoundedDomainEffectExecutionReceiptV1,
    ) -> BoundedDomainEffectResolutionV1:
        return BoundedDomainEffectResolutionV1(
            schema="bounded-domain-effect-resolution-v1",
            execution_ref=receipt.execution_ref,
            original_status=receipt.status,
            current_status=receipt.status,
            work_ref=receipt.work_ref,
            run_ref=receipt.run_ref,
            request_ref=receipt.request_ref,
            outcome_ref=receipt.outcome_ref,
            evidence_ref=receipt.evidence_ref,
            responsibility_ref=receipt.responsibility_ref,
            processed_at=receipt.processed_at,
        )

    def _prepared_request(self, run_ref: str, request_ref: str) -> CapabilityRequest:
        events = [
            event
            for event in self.runtime.store.list_events(run_ref)
            if isinstance(event, Event) and event.type == DOMAIN_EFFECT_REQUEST_EVENT
        ]
        matches: list[CapabilityRequest] = []
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw = payload.get("request")
            if not isinstance(raw, dict):
                continue
            request = CapabilityRequest.model_validate(raw)
            if request.id == request_ref:
                matches.append(request)
        if len(matches) != 1:
            raise ValueError("bounded recovery requires exactly one durable prepared request")
        return matches[0]

    def _dispatch_attempt(self, run_ref: str, request_ref: str) -> tuple[Event, StepAttempt]:
        dispatches = [
            event
            for event in self.runtime.store.list_events(request_ref)
            if isinstance(event, Event) and event.type == DISPATCH_COMMIT_EVENT
        ]
        if len(dispatches) != 1:
            raise ValueError("bounded recovery requires exactly one dispatch commitment")
        dispatch = dispatches[0]
        payload = dispatch.payload if isinstance(dispatch.payload, dict) else {}
        attempt_ref = payload.get("attempt_ref")
        if not isinstance(attempt_ref, str) or not attempt_ref:
            raise ValueError("bounded recovery dispatch lacks Attempt identity")
        attempt = self.runtime.store.get_attempt(attempt_ref)
        if not isinstance(attempt, StepAttempt):
            raise ValueError("bounded recovery Attempt is unavailable")
        if attempt.request_ref != request_ref:
            raise ValueError("bounded recovery Attempt request identity rebound")
        step = self.runtime.store.get_step(attempt.step_id)
        if step is None or step.run_id != run_ref:
            raise ValueError("bounded recovery Attempt Run identity rebound")
        return dispatch, attempt

    def _records(self, attempt: StepAttempt) -> ExecutionRecordIds:
        metadata = attempt.metadata if isinstance(attempt.metadata, dict) else {}
        action_ref = metadata.get("action_ref")
        if not isinstance(action_ref, str) or not action_ref:
            raise ValueError("bounded recovery Attempt lacks Action identity")
        return ExecutionRecordIds(
            step_id=attempt.step_id,
            attempt_id=attempt.id,
            action_id=action_ref,
        )

    def _record(self, view: BoundedDomainEffectResolutionV1) -> BoundedDomainEffectResolutionV1:
        event_id = _stable_id(
            "bounded_domain_effect_resolution",
            view.execution_ref,
            view.current_status,
            view.recovery_application_ref or "",
            view.recovery_observation_ref or "",
            view.outcome_ref or "",
            view.evidence_ref or "",
            view.reason,
        )
        event = Event(
            id=event_id,
            type=BOUNDED_DOMAIN_EFFECT_RESOLUTION_EVENT,
            subject_ref=view.execution_ref,
            payload=view.model_dump(mode="json", by_alias=True),
        )
        existing = self.runtime.store.get_event(event_id)
        if existing is not None:
            durable = self._view_from_event(existing)
            if _resolution_semantics(durable) != _resolution_semantics(view):
                raise ValueError("bounded domain-effect resolution identity rebound")
            return durable
        self.runtime.store.append_event(event)
        return self._view_from_event(event)

    @staticmethod
    def _view_from_event(event: Event) -> BoundedDomainEffectResolutionV1:
        if event.type != BOUNDED_DOMAIN_EFFECT_RESOLUTION_EVENT:
            raise ValueError("event is not a bounded domain-effect resolution")
        if not isinstance(event.payload, dict):
            raise ValueError("bounded domain-effect resolution payload is invalid")
        return BoundedDomainEffectResolutionV1.model_validate(event.payload)


__all__ = [
    "BOUNDED_DOMAIN_EFFECT_RECOVERY_POLICY_REF",
    "BOUNDED_DOMAIN_EFFECT_RECOVERY_SCHEMA",
    "BOUNDED_DOMAIN_EFFECT_RESOLUTION_SCHEMA",
    "BoundedDomainEffectRecoveryService",
    "BoundedDomainEffectRecoveryV1",
    "BoundedDomainEffectResolutionV1",
]
