from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Event, Run, utcnow
from portable_runtime.records.authorization import is_grant_valid
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_request import (
    DOMAIN_EFFECT_REQUEST_EVENT,
    DOMAIN_EFFECT_REQUEST_SCHEMA,
)
from portable_runtime.responsibility.domain_effect_run import (
    DOMAIN_EFFECT_RUN_SCHEMA,
    DOMAIN_EFFECT_WORKFLOW_ID,
)

DOMAIN_EFFECT_ACTIVATION_EVENT = "domain-effect-run-activated"
DOMAIN_EFFECT_ACTIVATION_SCHEMA = "domain-effect-run-activation-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectRunActivationInput(BaseModel):
    """Identify only the canonical Run that should receive execution ownership."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)


class DomainEffectRunActivationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["running"] = "running"
    run_ref: str
    work_ref: str
    request_event_ref: str
    activation_event_ref: str
    lease_owner: str
    lease_generation: int = Field(ge=1)
    lease_expires_at: datetime
    started_at: datetime
    authority_bearing: bool = False


class DomainEffectRunActivation:
    """Activate one prepared domain-effect Run under a concrete fencing lease.

    The execution owner is supplied by the Kernel scheduler/service boundary,
    not by the domain request. Activation does not consume AuthorizationUse,
    select a provider, issue InvocationPermit, or create an Action/Attempt.
    """

    def __init__(
        self,
        store: Any,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
    ) -> None:
        self.store = store
        self.contract_registry = contract_registry or CapabilityContractRegistry()
        self.authorization = DomainEffectAuthorizationUseConsumption(
            store,
            contract_registry=self.contract_registry,
        )

    def activate(
        self,
        value: DomainEffectRunActivationInput,
        *,
        owner: str,
        ttl_seconds: float = 30,
        activated_at: datetime | None = None,
    ) -> DomainEffectRunActivationResult:
        owner = owner.strip()
        if not owner:
            raise ValueError("domain effect Run activation requires a non-empty lease owner")
        if ttl_seconds <= 0:
            raise ValueError("domain effect Run activation requires a positive lease TTL")

        run = self._require_run(value.run_ref)
        metadata = self._run_metadata(run)
        request_event = self._require_request_event(run, metadata)

        replay = self._replay_current_generation(run, request_event, owner)
        if replay is not None:
            return replay

        at = activated_at or utcnow()
        authorization_ref = self._required_ref(
            metadata,
            "domain_effect_authorization_ref",
        )
        context = self.authorization._resolve_context(authorization_ref)
        self._validate_run_context(run, metadata, context)
        if not is_grant_valid(context.grant, now=at):
            raise ValueError(
                "domain effect runtime grant is not current for Run activation"
            )
        if any(
            getattr(use, "authorization_ref", None) == context.grant.id
            for use in self.store.list_authorization_uses()
        ):
            raise ValueError(
                "domain effect authorization was consumed before Run activation"
            )

        leased = self._ensure_lease(run, owner, ttl_seconds)
        generation = leased.lease_generation
        if generation < 1 or leased.lease_expires_at is None:
            raise ValueError("domain effect Run activation lacks a durable fencing lease")

        event_id = _stable_id(
            "event_domain_effect_activation",
            leased.id,
            generation,
        )
        existing = self.store.get_event(event_id)
        if existing is not None:
            return self._validate_activation_event(existing, leased, request_event, owner)

        with self.store.transaction():
            current = self.store.get_run(leased.id)
            if not isinstance(current, Run):
                raise ValueError("domain effect Run disappeared during activation")
            if current.lease_owner != owner or current.lease_generation != generation:
                raise ValueError("domain effect Run fencing changed during activation")
            lease_expires_at = current.lease_expires_at
            if lease_expires_at is None:
                raise ValueError("domain effect Run lost lease expiry during activation")
            if current.status not in {"queued", "running"}:
                raise ValueError(
                    "domain effect Run cannot activate from its current status"
                )
            started_at = current.started_at or at
            updated = current.model_copy(
                update={
                    "status": "running",
                    "started_at": started_at,
                }
            )
            self.store.save_run(updated)
            event = Event(
                id=event_id,
                created_at=at,
                type=DOMAIN_EFFECT_ACTIVATION_EVENT,
                subject_ref=updated.id,
                payload={
                    "schema": DOMAIN_EFFECT_ACTIVATION_SCHEMA,
                    "authority_bearing": False,
                    "work_ref": updated.work_id,
                    "request_event_ref": request_event.id,
                    "lease_owner": owner,
                    "lease_generation": generation,
                    "lease_expires_at": lease_expires_at.isoformat(),
                    "started_at": started_at.isoformat(),
                },
            )
            self.store.save_event(event)

        current = self.store.get_run(leased.id)
        event = self.store.get_event(event_id)
        if not isinstance(current, Run) or not isinstance(event, Event):
            raise ValueError("domain effect Run activation commit is incomplete")
        return self._validate_activation_event(event, current, request_event, owner)

    def _require_run(self, run_ref: str) -> Run:
        run = self.store.get_run(run_ref)
        if not isinstance(run, Run):
            raise ValueError("domain effect activation requires an existing Run")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("domain effect activation requires the canonical workflow")
        if run.status not in {"queued", "running"}:
            raise ValueError("domain effect activation requires queued or running Run")
        if run.provider_invocation_refs:
            raise ValueError("domain effect activation cannot follow provider invocation")
        return run

    @staticmethod
    def _run_metadata(run: Run) -> dict[str, Any]:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        if metadata.get("schema") != DOMAIN_EFFECT_RUN_SCHEMA:
            raise ValueError("domain effect Run has an incompatible schema")
        if metadata.get("authority_bearing") is not False:
            raise ValueError("domain effect Run must remain non-authoritative")
        if metadata.get("authorization_use_requirement") != "consume-at-action-boundary":
            raise ValueError("domain effect Run changed authorization-use ordering")
        if metadata.get("provider_selection") != "not-performed":
            raise ValueError("domain effect Run already claims provider selection")
        if metadata.get("invocation_permit") != "not-issued":
            raise ValueError("domain effect Run already claims InvocationPermit")
        return metadata

    @staticmethod
    def _required_ref(metadata: dict[str, Any], key: str) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"domain effect Run lacks {key}")
        return value

    def _require_request_event(
        self,
        run: Run,
        metadata: dict[str, Any],
    ) -> Event:
        events = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_REQUEST_EVENT
        ]
        if len(events) != 1:
            raise ValueError(
                "domain effect Run activation requires exactly one prepared request"
            )
        event = events[0]
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_REQUEST_SCHEMA:
            raise ValueError("domain effect prepared request schema is incompatible")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect prepared request became authoritative")
        expected = {
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
        for key, expected_value in expected.items():
            if payload.get(key) != expected_value:
                raise ValueError(f"domain effect prepared request rebound at {key}")
        return event

    def _validate_run_context(
        self,
        run: Run,
        metadata: dict[str, Any],
        context: Any,
    ) -> None:
        if run.work_id != context.intent.work_ref:
            raise ValueError("domain effect Run is bound to a different Work")
        expected = {
            "domain_effect_authorization_ref": context.grant.id,
            "domain_effect_authorization_decision_ref": context.decision.id,
            "domain_effect_intent_evidence_ref": context.evidence.id,
            "capability": context.intent.capability,
            "actor_ref": context.admission.actor_ref,
            "resource_ref": context.admission.resource_ref,
            "subject_version_ref": context.admission.subject_version_ref,
        }
        for key, expected_value in expected.items():
            if metadata.get(key) != expected_value:
                raise ValueError(f"domain effect Run provenance rebound at {key}")

    @staticmethod
    def _lease_is_current(run: Run, owner: str) -> bool:
        return (
            run.lease_owner == owner
            and run.lease_generation > 0
            and run.lease_expires_at is not None
            and run.lease_expires_at > utcnow()
        )

    def _ensure_lease(
        self,
        run: Run,
        owner: str,
        ttl_seconds: float,
    ) -> Run:
        if self._lease_is_current(run, owner):
            return run
        if not self.store.acquire_lease(run.id, owner, ttl_seconds):
            raise ValueError("domain effect Run fencing lease is held by another owner")
        leased = self.store.get_run(run.id)
        if not isinstance(leased, Run):
            raise ValueError("domain effect Run disappeared after lease acquisition")
        if leased.lease_owner != owner:
            raise ValueError("domain effect Run lease owner rebound after acquisition")
        return leased

    def _replay_current_generation(
        self,
        run: Run,
        request_event: Event,
        owner: str,
    ) -> DomainEffectRunActivationResult | None:
        if not self._lease_is_current(run, owner):
            return None
        event_id = _stable_id(
            "event_domain_effect_activation",
            run.id,
            run.lease_generation,
        )
        event = self.store.get_event(event_id)
        if event is None:
            return None
        return self._validate_activation_event(event, run, request_event, owner)

    @staticmethod
    def _validate_activation_event(
        event: Event,
        run: Run,
        request_event: Event,
        owner: str,
    ) -> DomainEffectRunActivationResult:
        if event.type != DOMAIN_EFFECT_ACTIVATION_EVENT or event.subject_ref != run.id:
            raise ValueError("domain effect activation event identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_ACTIVATION_SCHEMA:
            raise ValueError("domain effect activation event schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect activation event became authoritative")
        if run.status != "running" or run.started_at is None:
            raise ValueError("domain effect activation event requires running Run")
        if run.lease_owner != owner or run.lease_generation < 1:
            raise ValueError("domain effect activation event lease owner/generation rebound")
        if run.lease_expires_at is None:
            raise ValueError("domain effect activation event lacks current lease expiry")
        expected = {
            "work_ref": run.work_id,
            "request_event_ref": request_event.id,
            "lease_owner": owner,
            "lease_generation": run.lease_generation,
            "lease_expires_at": run.lease_expires_at.isoformat(),
            "started_at": run.started_at.isoformat(),
        }
        for key, expected_value in expected.items():
            if payload.get(key) != expected_value:
                raise ValueError(f"domain effect activation event rebound at {key}")
        return DomainEffectRunActivationResult(
            run_ref=run.id,
            work_ref=run.work_id,
            request_event_ref=request_event.id,
            activation_event_ref=event.id,
            lease_owner=owner,
            lease_generation=run.lease_generation,
            lease_expires_at=run.lease_expires_at,
            started_at=run.started_at,
        )


__all__ = [
    "DOMAIN_EFFECT_ACTIVATION_EVENT",
    "DOMAIN_EFFECT_ACTIVATION_SCHEMA",
    "DomainEffectRunActivation",
    "DomainEffectRunActivationInput",
    "DomainEffectRunActivationResult",
]
