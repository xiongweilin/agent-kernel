from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.models import Event, Run, utcnow
from portable_runtime.records.authorization import is_grant_valid
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
    DomainEffectAuthorizationUseContext,
)
from portable_runtime.responsibility.domain_effect_run import (
    DOMAIN_EFFECT_RUN_SCHEMA,
    DOMAIN_EFFECT_WORKFLOW_ID,
)

DOMAIN_EFFECT_REQUEST_SCHEMA = "domain-effect-capability-request-v1"
DOMAIN_EFFECT_REQUEST_EVENT = "domain-effect-capability-request-prepared"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectExecutionRequestPreparationInput(BaseModel):
    """Identify only the canonical Run whose execution request must be prepared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)


class DomainEffectExecutionRequestPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["prepared"] = "prepared"
    request: CapabilityRequest
    event_ref: str
    run_ref: str
    work_ref: str
    authorization_ref: str
    evidence_ref: str
    decision_ref: str
    logical_effect_ref: str
    authority_bearing: bool = False


class DomainEffectExecutionRequestPreparation:
    """Prepare one provider-agnostic canonical CapabilityRequest.

    The Run is the only caller-controlled identity. Runtime actor, resource,
    subject version, effect class, capability, parameters, and logical-effect
    identity are re-derived from the persisted Kernel lineage. Preparation is
    still pre-action: it does not consume AuthorizationUse, select a provider,
    issue InvocationPermit, create an Attempt/Action, or dispatch a side effect.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.authorization = DomainEffectAuthorizationUseConsumption(store)

    def prepare(
        self,
        value: DomainEffectExecutionRequestPreparationInput,
        *,
        prepared_at: datetime | None = None,
    ) -> DomainEffectExecutionRequestPreparationResult:
        with self.store.transaction():
            run = self._require_run(value.run_ref)
            metadata = self._run_metadata(run)
            logical_effect_ref = self._required_ref(metadata, "logical_effect_ref")
            event_id = _stable_id(
                "event_domain_effect_request",
                run.id,
                logical_effect_ref,
            )
            existing = self.store.get_event(event_id)
            if existing is not None:
                return self._replay(existing, run, metadata)

            authorization_ref = self._required_ref(
                metadata,
                "domain_effect_authorization_ref",
            )
            context = self.authorization._resolve_context(authorization_ref)
            self._validate_run_against_context(run, metadata, context)

            at = prepared_at or utcnow()
            if not is_grant_valid(context.grant, now=at):
                raise ValueError(
                    "domain effect runtime grant is not current for request preparation"
                )
            if any(
                getattr(use, "authorization_ref", None) == context.grant.id
                for use in self.store.list_authorization_uses()
            ):
                raise ValueError(
                    "domain effect authorization was consumed before canonical request preparation"
                )

            request = self._request(run, metadata, context)
            event = Event(
                id=event_id,
                created_at=at,
                type=DOMAIN_EFFECT_REQUEST_EVENT,
                subject_ref=run.id,
                payload={
                    "schema": DOMAIN_EFFECT_REQUEST_SCHEMA,
                    "authority_bearing": False,
                    "request": request.model_dump(mode="json"),
                    "authorization_ref": context.grant.id,
                    "evidence_ref": context.evidence.id,
                    "decision_ref": context.decision.id,
                    "logical_effect_ref": logical_effect_ref,
                },
            )
            self.store.save_event(event)
            return self._result(event, request)

    def _require_run(self, run_ref: str) -> Run:
        run = self.store.get_run(run_ref)
        if not isinstance(run, Run):
            raise ValueError("domain effect request preparation requires an existing Run")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("domain effect request requires the canonical workflow")
        if run.status != "queued":
            raise ValueError("domain effect request requires a queued canonical Run")
        if run.started_at is not None:
            raise ValueError("domain effect request cannot be prepared after Run start")
        if run.provider_invocation_refs:
            raise ValueError("domain effect request cannot follow provider invocation")
        return run

    @staticmethod
    def _run_metadata(run: Run) -> dict[str, Any]:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        if metadata.get("schema") != DOMAIN_EFFECT_RUN_SCHEMA:
            raise ValueError("domain effect Run has an incompatible schema")
        if metadata.get("authority_bearing") is not False:
            raise ValueError("domain effect Run must remain non-authoritative")
        if metadata.get("authorization_use_requirement") != "consume-at-action-boundary":
            raise ValueError("domain effect Run changed its authorization-use ordering")
        if metadata.get("provider_selection") != "not-performed":
            raise ValueError("domain effect Run already claims provider selection")
        if metadata.get("invocation_permit") != "not-issued":
            raise ValueError("domain effect Run already claims an InvocationPermit")
        return metadata

    @staticmethod
    def _required_ref(metadata: dict[str, Any], key: str) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"domain effect Run lacks {key}")
        return value

    def _validate_run_against_context(
        self,
        run: Run,
        metadata: dict[str, Any],
        context: DomainEffectAuthorizationUseContext,
    ) -> None:
        expected = {
            "domain_effect_authorization_ref": context.grant.id,
            "domain_effect_authorization_decision_ref": context.decision.id,
            "domain_effect_intent_evidence_ref": context.evidence.id,
            "capability": context.intent.capability,
            "actor_ref": context.admission.actor_ref,
            "resource_ref": context.admission.resource_ref,
            "subject_version_ref": context.admission.subject_version_ref,
        }
        if run.work_id != context.intent.work_ref:
            raise ValueError("domain effect Run is bound to a different Work")
        for key, expected_value in expected.items():
            if metadata.get(key) != expected_value:
                raise ValueError(f"domain effect Run provenance rebound at {key}")

    @staticmethod
    def _request(
        run: Run,
        metadata: dict[str, Any],
        context: DomainEffectAuthorizationUseContext,
    ) -> CapabilityRequest:
        logical_effect_ref = DomainEffectExecutionRequestPreparation._required_ref(
            metadata,
            "logical_effect_ref",
        )
        request_id = _stable_id(
            "request_domain_effect",
            run.id,
            context.grant.id,
            context.evidence.id,
            context.admission.subject_version_ref,
        )
        return CapabilityRequest(
            id=request_id,
            capability=context.request.capability,
            work_id=run.work_id,
            run_id=run.id,
            parameters=dict(context.intent.parameters),
            actor_ref=context.request.actor_ref,
            resource_ref=context.request.resource_ref,
            subject_version_refs=list(context.request.subject_version_refs),
            effect_class=context.request.effect_class,
            idempotency_key=logical_effect_ref,
            preferred_provider_ids=[],
            excluded_provider_ids=[],
            constraints={},
            metadata={},
            lease_generation=0,
            lease_owner=None,
        )

    def _replay(
        self,
        event: Event,
        run: Run,
        metadata: dict[str, Any],
    ) -> DomainEffectExecutionRequestPreparationResult:
        if event.type != DOMAIN_EFFECT_REQUEST_EVENT or event.subject_ref != run.id:
            raise ValueError("canonical domain effect request event identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_REQUEST_SCHEMA:
            raise ValueError("canonical domain effect request event schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("canonical domain effect request event became authoritative")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("canonical domain effect request event lacks request payload")
        request = CapabilityRequest.model_validate(raw_request)
        expected_refs = {
            "authorization_ref": self._required_ref(
                metadata,
                "domain_effect_authorization_ref",
            ),
            "evidence_ref": self._required_ref(
                metadata,
                "domain_effect_intent_evidence_ref",
            ),
            "decision_ref": self._required_ref(
                metadata,
                "domain_effect_authorization_decision_ref",
            ),
            "logical_effect_ref": self._required_ref(metadata, "logical_effect_ref"),
        }
        for key, expected in expected_refs.items():
            if payload.get(key) != expected:
                raise ValueError(f"canonical domain effect request event rebound at {key}")
        if request.work_id != run.work_id or request.run_id != run.id:
            raise ValueError("canonical domain effect request rebound to another Run or Work")
        if request.capability != metadata.get("capability"):
            raise ValueError("canonical domain effect request capability rebound")
        if request.actor_ref != metadata.get("actor_ref"):
            raise ValueError("canonical domain effect request actor rebound")
        if request.resource_ref != metadata.get("resource_ref"):
            raise ValueError("canonical domain effect request resource rebound")
        if request.subject_version_refs != [metadata.get("subject_version_ref")]:
            raise ValueError("canonical domain effect request subject version rebound")
        if request.idempotency_key != expected_refs["logical_effect_ref"]:
            raise ValueError("canonical domain effect request logical effect rebound")
        if request.preferred_provider_ids or request.excluded_provider_ids:
            raise ValueError("canonical domain effect request cannot preselect a provider")
        if request.lease_generation != 0 or request.lease_owner is not None:
            raise ValueError("canonical domain effect request cannot synthesize a Run lease")
        return self._result(event, request)

    @staticmethod
    def _result(
        event: Event,
        request: CapabilityRequest,
    ) -> DomainEffectExecutionRequestPreparationResult:
        payload = event.payload
        return DomainEffectExecutionRequestPreparationResult(
            request=request,
            event_ref=event.id,
            run_ref=request.run_id or "",
            work_ref=request.work_id or "",
            authorization_ref=str(payload["authorization_ref"]),
            evidence_ref=str(payload["evidence_ref"]),
            decision_ref=str(payload["decision_ref"]),
            logical_effect_ref=str(payload["logical_effect_ref"]),
        )


__all__ = [
    "DOMAIN_EFFECT_REQUEST_EVENT",
    "DOMAIN_EFFECT_REQUEST_SCHEMA",
    "DomainEffectExecutionRequestPreparation",
    "DomainEffectExecutionRequestPreparationInput",
    "DomainEffectExecutionRequestPreparationResult",
]
