from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Event, Run, utcnow
from portable_runtime.core.qualification import AssessmentContext, QualificationRef
from portable_runtime.governance.dispatch import DISPATCH_COMMIT_EVENT
from portable_runtime.responsibility.domain_effect_activation import (
    DOMAIN_EFFECT_ACTIVATION_EVENT,
    DOMAIN_EFFECT_ACTIVATION_SCHEMA,
)
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

DOMAIN_EFFECT_QUALIFICATION_EVENT = "domain-effect-qualification-assessed"
DOMAIN_EFFECT_QUALIFICATION_SCHEMA = "domain-effect-qualification-assessment-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectQualificationInput(BaseModel):
    """Identify only the activated canonical Run to qualify."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)


class DomainEffectQualificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["qualified"] = "qualified"
    run_ref: str
    work_ref: str
    request_event_ref: str
    qualification_event_ref: str
    qualification_digest: str
    qualification_refs: tuple[QualificationRef, ...]
    request: CapabilityRequest
    lease_owner: str
    lease_generation: int = Field(ge=1)
    authority_bearing: bool = False


class DomainEffectQualificationAssessment:
    """Resolve current execution qualification from authoritative Kernel state.

    The persisted request event remains provider-agnostic and contains no inline
    proof material. This stage adds only authoritative reference transport to a
    fenced request projection, then delegates proof resolution and snapshot
    digesting to the existing AssessmentContext mechanism.

    Qualification is a pre-action closure. After a committed action boundary,
    a fresh process may acquire a new Run fencing generation, but it must replay
    the exact historical qualification bound into that dispatch instead of
    minting a second qualification from already-consumed authority.
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

    def assess(
        self,
        value: DomainEffectQualificationInput,
        *,
        assessed_at: datetime | None = None,
    ) -> DomainEffectQualificationResult:
        run = self._require_active_run(value.run_ref)
        metadata = self._run_metadata(run)
        request_event, prepared_request = self._require_prepared_request(run, metadata)
        event_id = _stable_id(
            "event_domain_effect_qualification",
            run.id,
            run.lease_generation,
            request_event.id,
        )
        existing = self.store.get_event(event_id)
        if existing is not None:
            return self._replay(existing, run, request_event)

        authorization_ref = self._required_ref(metadata, "domain_effect_authorization_ref")
        evidence_ref = self._required_ref(metadata, "domain_effect_intent_evidence_ref")
        decision_ref = self._required_ref(metadata, "domain_effect_authorization_decision_ref")
        context = self.authorization._resolve_context(authorization_ref)
        self._validate_run_context(run, metadata, context)
        authorization_uses = [
            use
            for use in self.store.list_authorization_uses()
            if getattr(use, "authorization_ref", None) == context.grant.id
        ]
        if authorization_uses:
            replay = self._replay_after_committed_action_boundary(
                run,
                request_event,
                prepared_request,
                authorization_ref=context.grant.id,
                authorization_uses=authorization_uses,
            )
            if replay is not None:
                return replay
            raise ValueError(
                "domain effect authorization was consumed before qualification closure"
            )

        current_request = prepared_request.model_copy(
            update={
                "lease_owner": run.lease_owner,
                "lease_generation": run.lease_generation,
                "metadata": {
                    "authorization_refs": [
                        {"id": authorization_ref, "kind": "authorization"}
                    ],
                    "evidence_refs": [
                        {"id": evidence_ref, "kind": "evidence"}
                    ],
                    "decision_refs": [
                        {"id": decision_ref, "kind": "decision"}
                    ],
                },
            }
        )
        at = assessed_at or utcnow()

        # Qualification owns only the canonical authority closure above.  The
        # Run is subsequently enriched by procedure-readiness with failure
        # stop, recovery, evidence, and relation refs.  Reusing that enriched
        # metadata as an input here would make a later reactivation treat
        # downstream procedure proofs as part of the qualification authority
        # graph, so a valid recovery can fail with a graph-mismatch error.
        # Keep the stage boundary explicit: resolve only the refs carried by
        # ``current_request`` and leave downstream proof transport to the
        # procedure-readiness stage.
        authoritative_work = self.store.get_work(run.work_id)
        if authoritative_work is None:
            raise ValueError("domain effect qualification requires the canonical Work")
        qualification_work = authoritative_work.model_copy(update={"metadata": {}})
        qualification_run = run.model_copy(update={"metadata": {}})
        assessment = AssessmentContext.resolve(
            self.store,
            current_request,
            work=qualification_work,
            run=qualification_run,
            now=at,
        )
        expected_refs = {
            (authorization_ref, "authorization"),
            (evidence_ref, "evidence"),
            (decision_ref, "decision"),
        }
        actual_refs = {(ref.ref_id, ref.kind) for ref in assessment.refs}
        if actual_refs != expected_refs:
            raise ValueError("domain effect qualification resolved an unexpected authority graph")
        if not assessment.has_authorization_refs:
            raise ValueError("domain effect qualification lacks runtime authorization proof")

        event = Event(
            id=event_id,
            created_at=at,
            type=DOMAIN_EFFECT_QUALIFICATION_EVENT,
            subject_ref=run.id,
            payload={
                "schema": DOMAIN_EFFECT_QUALIFICATION_SCHEMA,
                "authority_bearing": False,
                "work_ref": run.work_id,
                "request_event_ref": request_event.id,
                "qualification_digest": assessment.digest,
                "qualification_refs": [
                    ref.model_dump(mode="json", by_alias=True) for ref in assessment.refs
                ],
                "request": current_request.model_dump(mode="json"),
                "lease_owner": run.lease_owner,
                "lease_generation": run.lease_generation,
            },
        )
        with self.store.transaction():
            self._recheck_fencing(run)
            if self.store.get_event(event_id) is None:
                self.store.save_event(event)

        persisted = self.store.get_event(event_id)
        if not isinstance(persisted, Event):
            raise ValueError("domain effect qualification commit is incomplete")
        current_run = self.store.get_run(run.id)
        if not isinstance(current_run, Run):
            raise ValueError("domain effect Run disappeared after qualification")
        return self._validate_event(persisted, current_run, request_event)

    def _replay_after_committed_action_boundary(
        self,
        run: Run,
        request_event: Event,
        prepared_request: CapabilityRequest,
        *,
        authorization_ref: str,
        authorization_uses: list[Any],
    ) -> DomainEffectQualificationResult | None:
        """Replay the qualification already committed into the physical dispatch.

        The current activation must itself prove it is a post-action refencing
        generation. DomainEffectRunActivation only sets that marker after it has
        validated the exact historical activation, dispatch, AuthorizationUse,
        Attempt, and Run lineage. Qualification therefore reuses the historical
        closure bound to that dispatch; it does not re-resolve consumed authority.
        """

        activation_event_id = _stable_id(
            "event_domain_effect_activation",
            run.id,
            run.lease_generation,
        )
        activation = self.store.get_event(activation_event_id)
        if not isinstance(activation, Event):
            return None
        activation_payload = activation.payload if isinstance(activation.payload, dict) else {}
        if (
            activation.type != DOMAIN_EFFECT_ACTIVATION_EVENT
            or activation.subject_ref != run.id
            or activation_payload.get("schema") != DOMAIN_EFFECT_ACTIVATION_SCHEMA
            or activation_payload.get("authority_bearing") is not False
            or activation_payload.get("work_ref") != run.work_id
            or activation_payload.get("request_event_ref") != request_event.id
            or activation_payload.get("lease_owner") != run.lease_owner
            or activation_payload.get("lease_generation") != run.lease_generation
        ):
            raise ValueError("domain effect recovery activation lineage rebound")
        if activation_payload.get("resume_after_committed_action_boundary") is not True:
            return None

        if len(authorization_uses) != 1:
            raise ValueError("domain effect authorization has multiple canonical uses")
        use = authorization_uses[0]
        if getattr(use, "authorization_ref", None) != authorization_ref:
            raise ValueError("domain effect recovery AuthorizationUse rebound")

        dispatches = [
            event
            for event in self.store.list_events(prepared_request.id)
            if event.type == DISPATCH_COMMIT_EVENT
        ]
        if len(dispatches) != 1:
            raise ValueError(
                "domain effect committed recovery requires exactly one dispatch commitment"
            )
        dispatch = dispatches[0]
        dispatch_payload = dispatch.payload if isinstance(dispatch.payload, dict) else {}
        if dispatch_payload.get("request_id") != prepared_request.id:
            raise ValueError("domain effect recovery dispatch request identity rebound")
        if dispatch_payload.get("authorization_use_ref") != getattr(use, "id", None):
            raise ValueError("domain effect recovery dispatch AuthorizationUse rebound")
        historical_generation = dispatch_payload.get("lease_generation")
        if not isinstance(historical_generation, int) or historical_generation < 1:
            raise ValueError("domain effect recovery dispatch lacks fencing generation")

        historical_event_id = _stable_id(
            "event_domain_effect_qualification",
            run.id,
            historical_generation,
            request_event.id,
        )
        historical = self.store.get_event(historical_event_id)
        if not isinstance(historical, Event):
            raise ValueError(
                "domain effect committed action boundary lacks historical qualification closure"
            )
        historical_payload = historical.payload if isinstance(historical.payload, dict) else {}
        historical_owner = historical_payload.get("lease_owner")
        if not isinstance(historical_owner, str) or not historical_owner:
            raise ValueError("domain effect historical qualification lacks lease owner")
        historical_run = run.model_copy(
            update={
                "lease_owner": historical_owner,
                "lease_generation": historical_generation,
            }
        )
        result = self._validate_event(historical, historical_run, request_event)
        if result.request.id != prepared_request.id:
            raise ValueError("domain effect historical qualification request identity rebound")
        return result

    def _require_active_run(self, run_ref: str) -> Run:
        run = self.store.get_run(run_ref)
        if not isinstance(run, Run):
            raise ValueError("domain effect qualification requires an existing Run")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("domain effect qualification requires the canonical workflow")
        if run.status != "running" or run.started_at is None:
            raise ValueError("domain effect qualification requires an activated running Run")
        if not run.lease_owner or run.lease_generation < 1 or run.lease_expires_at is None:
            raise ValueError("domain effect qualification requires a fencing lease")
        if run.lease_expires_at <= utcnow():
            raise ValueError("domain effect qualification requires a current fencing lease")
        if run.provider_invocation_refs:
            raise ValueError("domain effect qualification cannot follow provider invocation")
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

    def _require_prepared_request(
        self,
        run: Run,
        metadata: dict[str, Any],
    ) -> tuple[Event, CapabilityRequest]:
        events = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_REQUEST_EVENT
        ]
        if len(events) != 1:
            raise ValueError("domain effect qualification requires exactly one prepared request")
        event = events[0]
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_REQUEST_SCHEMA:
            raise ValueError("domain effect prepared request schema is incompatible")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect prepared request became authoritative")
        raw = payload.get("request")
        if not isinstance(raw, dict):
            raise ValueError("domain effect prepared request lacks request payload")
        request = CapabilityRequest.model_validate(raw)
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
        if request.work_id != run.work_id or request.run_id != run.id:
            raise ValueError("domain effect prepared request rebound to another Run or Work")
        if request.preferred_provider_ids or request.excluded_provider_ids:
            raise ValueError("domain effect prepared request must remain provider-agnostic")
        if request.metadata:
            raise ValueError("domain effect prepared request must not carry inline qualification")
        if request.lease_generation != 0 or request.lease_owner is not None:
            raise ValueError("domain effect prepared request must precede fencing")
        return event, request

    def _validate_run_context(self, run: Run, metadata: dict[str, Any], context: Any) -> None:
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

    def _recheck_fencing(self, expected: Run) -> None:
        current = self.store.get_run(expected.id)
        if not isinstance(current, Run):
            raise ValueError("domain effect Run disappeared during qualification")
        if (
            current.status != "running"
            or current.lease_owner != expected.lease_owner
            or current.lease_generation != expected.lease_generation
            or current.lease_expires_at is None
            or current.lease_expires_at <= utcnow()
        ):
            raise ValueError("domain effect fencing changed during qualification")

    def _replay(
        self,
        event: Event,
        run: Run,
        request_event: Event,
    ) -> DomainEffectQualificationResult:
        return self._validate_event(event, run, request_event)

    @staticmethod
    def _validate_event(
        event: Event,
        run: Run,
        request_event: Event,
    ) -> DomainEffectQualificationResult:
        if event.type != DOMAIN_EFFECT_QUALIFICATION_EVENT or event.subject_ref != run.id:
            raise ValueError("domain effect qualification event identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_QUALIFICATION_SCHEMA:
            raise ValueError("domain effect qualification event schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect qualification event became authoritative")
        if payload.get("work_ref") != run.work_id:
            raise ValueError("domain effect qualification event Work rebound")
        if payload.get("request_event_ref") != request_event.id:
            raise ValueError("domain effect qualification event request rebound")
        if payload.get("lease_owner") != run.lease_owner:
            raise ValueError("domain effect qualification event lease owner rebound")
        if payload.get("lease_generation") != run.lease_generation:
            raise ValueError("domain effect qualification event fencing generation rebound")
        digest = payload.get("qualification_digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError("domain effect qualification event lacks digest")
        raw_request = payload.get("request")
        raw_refs = payload.get("qualification_refs")
        if not isinstance(raw_request, dict) or not isinstance(raw_refs, list):
            raise ValueError("domain effect qualification event lacks request/ref snapshot")
        request = CapabilityRequest.model_validate(raw_request)
        refs = tuple(QualificationRef.model_validate(ref) for ref in raw_refs)
        if request.run_id != run.id or request.work_id != run.work_id:
            raise ValueError("domain effect qualified request rebound")
        if request.lease_owner != run.lease_owner:
            raise ValueError("domain effect qualified request lease owner rebound")
        if request.lease_generation != run.lease_generation:
            raise ValueError("domain effect qualified request fencing generation rebound")
        return DomainEffectQualificationResult(
            run_ref=run.id,
            work_ref=run.work_id,
            request_event_ref=request_event.id,
            qualification_event_ref=event.id,
            qualification_digest=digest,
            qualification_refs=refs,
            request=request,
            lease_owner=run.lease_owner or "",
            lease_generation=run.lease_generation,
        )


__all__ = [
    "DOMAIN_EFFECT_QUALIFICATION_EVENT",
    "DOMAIN_EFFECT_QUALIFICATION_SCHEMA",
    "DomainEffectQualificationAssessment",
    "DomainEffectQualificationInput",
    "DomainEffectQualificationResult",
]
