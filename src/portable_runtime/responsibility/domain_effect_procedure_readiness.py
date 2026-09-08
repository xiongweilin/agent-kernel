from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.capability_contract import (
    CapabilityContractRegistry,
    compute_effective_procedure_profile,
)
from portable_runtime.core.models import Event, Run, Work, utcnow
from portable_runtime.core.qualification import AssessmentContext, QualificationRef
from portable_runtime.records.models import BaseRecord
from portable_runtime.records.relations import RecordRelation
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_qualification import (
    DOMAIN_EFFECT_QUALIFICATION_EVENT,
    DOMAIN_EFFECT_QUALIFICATION_SCHEMA,
)
from portable_runtime.responsibility.domain_effect_run import (
    DOMAIN_EFFECT_RUN_SCHEMA,
    DOMAIN_EFFECT_WORKFLOW_ID,
)
from portable_runtime.workflows.procedure_phase import check_pre_action_readiness

DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT = "domain-effect-procedure-readiness-assessed"
DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA = "domain-effect-procedure-readiness-v1"
_BLOCKING_PROCEDURE_STATUSES = frozenset(
    {"open", "required", "blocked", "expired", "invalidated"}
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _semantic_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class DomainEffectProcedureReadinessInput(BaseModel):
    """Identify only the activated canonical Run whose pre-action procedure closes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)


class DomainEffectProcedureReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"] = "ready"
    run_ref: str
    work_ref: str
    qualification_event_ref: str
    readiness_event_ref: str
    readiness_digest: str
    readiness_refs: tuple[QualificationRef, ...]
    procedure_profile: str
    pre_action_obligations: tuple[str, ...]
    lease_owner: str
    lease_generation: int = Field(ge=1)
    authority_bearing: bool = False


class DomainEffectProcedureReadinessAssessment:
    """Close the standard pre-action procedure without inventing post-action truth.

    The stage derives deterministic typed procedure substrate from the already
    admitted administrative lineage. It creates no VerificationResult,
    result-confirmation fact, AuthorizationUse, provider selection,
    InvocationPermit, Attempt, dispatch, Action or Outcome.
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
        value: DomainEffectProcedureReadinessInput,
        *,
        assessed_at: datetime | None = None,
    ) -> DomainEffectProcedureReadinessResult:
        run = self._require_active_run(value.run_ref)
        work = self._require_work(run)
        metadata = self._run_metadata(run)
        qualification_event, request = self._require_qualification(run)
        authorization_ref = self._required_ref(metadata, "domain_effect_authorization_ref")
        context = self.authorization._resolve_context(authorization_ref)
        self._validate_run_context(run, metadata, context)
        self._assert_authorization_unconsumed(authorization_ref)

        event_id = _stable_id(
            "event_domain_effect_procedure_readiness",
            run.id,
            run.lease_generation,
            qualification_event.id,
        )
        existing = self.store.get_event(event_id)
        if existing is not None:
            return self._validate_event(
                existing,
                run,
                work,
                qualification_event,
                request,
            )

        at = assessed_at or utcnow()
        failure_stop = BaseRecord(
            id=_stable_id(
                "record_domain_effect_failure_stop",
                run.id,
                run.lease_generation,
                qualification_event.id,
            ),
            created_at=at,
            record_type="Policy",
            lifecycle_status="candidate",
            metadata={
                "qualification_kind": "failure-stop",
                "condition": "provider failure, timeout, or ambiguous completion",
                "policy": "stop automatic forward progress and enter reconciliation",
                "target_refs": [work.id],
                "source_qualification_ref": qualification_event.id,
                "logical_effect_ref": self._required_ref(metadata, "logical_effect_ref"),
            },
        )
        evidence = BaseRecord(
            id=_stable_id(
                "record_domain_effect_execution_evidence",
                run.id,
                run.lease_generation,
                qualification_event.id,
            ),
            created_at=at,
            record_type="EvidenceArtifact",
            lifecycle_status="current",
            metadata={
                "qualification_kind": "evidence",
                "target_refs": [work.id],
                "uri": f"evidence:{self._required_ref(metadata, 'domain_effect_intent_evidence_ref')}",
                "source_evidence_ref": self._required_ref(
                    metadata,
                    "domain_effect_intent_evidence_ref",
                ),
                "source_qualification_ref": qualification_event.id,
            },
        )
        evidence_relation = RecordRelation(
            id=_stable_id("relation_domain_effect_execution_evidence", work.id, evidence.id),
            created_at=at,
            relation_type="records",
            subject_ref=work.id,
            object_ref=evidence.id,
        )
        recovery = BaseRecord(
            id=_stable_id(
                "record_domain_effect_recovery",
                run.id,
                run.lease_generation,
                qualification_event.id,
            ),
            created_at=at,
            record_type="Policy",
            lifecycle_status="candidate",
            metadata={
                "qualification_kind": "recovery",
                "procedure": (
                    "reconcile the exact logical effect with the selected provider before "
                    "retry, compensation, or terminal disposition"
                ),
                "target_refs": [work.id],
                "source_qualification_ref": qualification_event.id,
                "logical_effect_ref": self._required_ref(metadata, "logical_effect_ref"),
                "required_effect_semantics": "reconcilable",
                "required_reversibility": "compensatable",
            },
        )
        readiness_refs = (
            QualificationRef(id=failure_stop.id, kind="failure-stop"),
            QualificationRef(id=evidence.id, kind="evidence"),
            QualificationRef(id=evidence_relation.id, kind="relation"),
            QualificationRef(id=recovery.id, kind="recovery"),
        )
        updated_run = self._run_with_readiness_metadata(
            run,
            work,
            readiness_refs,
            evidence_relation,
            context.intent.expected_postcondition,
        )

        with self.store.transaction():
            self._recheck_fencing(run)
            self._save_record_exact(failure_stop)
            self._save_record_exact(evidence)
            self._save_relation_exact(evidence_relation)
            self._save_record_exact(recovery)
            self.store.save_run(updated_run)

            assessment = AssessmentContext.resolve(
                self.store,
                request,
                work=work,
                run=updated_run,
                now=at,
            )
            profile, obligations = self._assert_pre_action_ready(
                assessment,
                work,
                updated_run,
                request,
                now=at,
            )
            self._assert_authorization_unconsumed(authorization_ref)
            event = Event(
                id=event_id,
                created_at=at,
                type=DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT,
                subject_ref=run.id,
                payload={
                    "schema": DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA,
                    "authority_bearing": False,
                    "work_ref": work.id,
                    "qualification_event_ref": qualification_event.id,
                    "readiness_digest": assessment.digest,
                    "readiness_refs": [
                        ref.model_dump(mode="json", by_alias=True)
                        for ref in readiness_refs
                    ],
                    "procedure_profile": profile,
                    "pre_action_obligations": list(obligations),
                    "lease_owner": run.lease_owner,
                    "lease_generation": run.lease_generation,
                },
            )
            self.store.save_event(event)

        current_run = self.store.get_run(run.id)
        persisted = self.store.get_event(event_id)
        if not isinstance(current_run, Run) or not isinstance(persisted, Event):
            raise ValueError("domain effect procedure readiness commit is incomplete")
        return self._validate_event(
            persisted,
            current_run,
            work,
            qualification_event,
            request,
        )

    def _require_active_run(self, run_ref: str) -> Run:
        run = self.store.get_run(run_ref)
        if not isinstance(run, Run):
            raise ValueError("domain effect procedure readiness requires an existing Run")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("domain effect procedure readiness requires the canonical workflow")
        if run.status != "running" or run.started_at is None:
            raise ValueError("domain effect procedure readiness requires an activated running Run")
        if not run.lease_owner or run.lease_generation < 1 or run.lease_expires_at is None:
            raise ValueError("domain effect procedure readiness requires a fencing lease")
        if run.lease_expires_at <= utcnow():
            raise ValueError("domain effect procedure readiness requires a current fencing lease")
        if run.provider_invocation_refs:
            raise ValueError("domain effect procedure readiness cannot follow provider invocation")
        return run

    def _require_work(self, run: Run) -> Work:
        work = self.store.get_work(run.work_id)
        if not isinstance(work, Work):
            raise ValueError("domain effect procedure readiness requires the canonical Work")
        return work

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

    def _require_qualification(self, run: Run) -> tuple[Event, CapabilityRequest]:
        events = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_QUALIFICATION_EVENT
        ]
        if len(events) != 1:
            raise ValueError(
                "domain effect procedure readiness requires exactly one qualification event"
            )
        event = events[0]
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_QUALIFICATION_SCHEMA:
            raise ValueError("domain effect qualification schema is incompatible")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect qualification event became authoritative")
        if payload.get("work_ref") != run.work_id:
            raise ValueError("domain effect qualification event Work rebound")
        if payload.get("lease_owner") != run.lease_owner:
            raise ValueError("domain effect qualification event lease owner rebound")
        if payload.get("lease_generation") != run.lease_generation:
            raise ValueError("domain effect qualification event fencing generation rebound")
        raw_request = payload.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("domain effect qualification event lacks request snapshot")
        request = CapabilityRequest.model_validate(raw_request)
        if request.work_id != run.work_id or request.run_id != run.id:
            raise ValueError("domain effect qualification request rebound")
        if request.lease_owner != run.lease_owner:
            raise ValueError("domain effect qualification request lease owner rebound")
        if request.lease_generation != run.lease_generation:
            raise ValueError("domain effect qualification request fencing generation rebound")
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

    def _assert_authorization_unconsumed(self, authorization_ref: str) -> None:
        if any(
            getattr(use, "authorization_ref", None) == authorization_ref
            for use in self.store.list_authorization_uses()
        ):
            raise ValueError(
                "domain effect authorization was consumed before procedure readiness closure"
            )

    @staticmethod
    def _run_with_readiness_metadata(
        run: Run,
        work: Work,
        refs: tuple[QualificationRef, ...],
        relation: RecordRelation,
        expected_postcondition: dict[str, Any],
    ) -> Run:
        metadata = dict(run.metadata or {})
        proof_refs = [
            ref.model_dump(mode="json", by_alias=True)
            for ref in refs
            if ref.kind in {"failure-stop", "recovery"}
        ]
        evidence_refs = [
            ref.model_dump(mode="json", by_alias=True)
            for ref in refs
            if ref.kind == "evidence"
        ]
        metadata.update(
            {
                "purpose": work.description or work.title,
                "execution_boundary": "provider",
                "candidate": [metadata.get("logical_effect_ref")],
                "procedure_profile": "standard",
                "procedure_proof_refs": proof_refs,
                "evidence_artifact_refs": evidence_refs,
                "relation_refs": [
                    QualificationRef(id=relation.id, kind="relation").model_dump(
                        mode="json",
                        by_alias=True,
                    )
                ],
                "expected_postcondition": expected_postcondition,
            }
        )
        return run.model_copy(update={"metadata": metadata})

    def _effective_profile(self, work: Work, run: Run, request: CapabilityRequest) -> str:
        contract = self.contract_registry.resolve(request.capability)
        work_metadata = work.metadata if isinstance(work.metadata, dict) else {}
        run_metadata = run.metadata if isinstance(run.metadata, dict) else {}
        request_metadata = request.metadata if isinstance(request.metadata, dict) else {}
        return compute_effective_procedure_profile(
            contract.minimum_procedure_profile,
            work_metadata.get("procedure_profile"),
            run_metadata.get("procedure_profile"),
            request_metadata.get("procedure_profile"),
        )

    def _assert_pre_action_ready(
        self,
        assessment: AssessmentContext,
        work: Work,
        run: Run,
        request: CapabilityRequest,
        *,
        now: datetime,
    ) -> tuple[str, tuple[str, ...]]:
        profile = self._effective_profile(work, run, request)
        statuses = check_pre_action_readiness(
            assessment.work,
            assessment.run,
            profile,
            now=now,
            proofs=assessment.procedure_proofs(),
            grants=(
                assessment.proofs.get("grants")
                if assessment.has_authorization_refs
                else None
            ),
        )
        obligations = tuple(
            str(getattr(status.obligation, "kind", status.obligation))
            for status in statuses
        )
        blocking = [
            obligation
            for obligation, status in zip(obligations, statuses, strict=True)
            if status.status in _BLOCKING_PROCEDURE_STATUSES
        ]
        if blocking:
            raise ValueError(
                "domain effect pre-action procedure remains incomplete: "
                + ", ".join(blocking)
            )
        return profile, obligations

    def _save_record_exact(self, record: BaseRecord) -> None:
        existing = self.store.get_record(record.id)
        if existing is not None:
            if _semantic_dump(existing) != _semantic_dump(record):
                raise ValueError("deterministic domain effect procedure record identity rebound")
            return
        self.store.save_record(record)

    def _save_relation_exact(self, relation: RecordRelation) -> None:
        existing = self.store.get_relation(relation.id)
        if existing is not None:
            if _semantic_dump(existing) != _semantic_dump(relation):
                raise ValueError("deterministic domain effect procedure relation identity rebound")
            return
        self.store.save_relation(relation)

    def _recheck_fencing(self, expected: Run) -> None:
        current = self.store.get_run(expected.id)
        if not isinstance(current, Run):
            raise ValueError("domain effect Run disappeared during procedure readiness")
        if (
            current.status != "running"
            or current.lease_owner != expected.lease_owner
            or current.lease_generation != expected.lease_generation
            or current.lease_expires_at is None
            or current.lease_expires_at <= utcnow()
        ):
            raise ValueError("domain effect fencing changed during procedure readiness")
        if current.provider_invocation_refs:
            raise ValueError("provider invocation began before procedure readiness closure")

    def _validate_event(
        self,
        event: Event,
        run: Run,
        work: Work,
        qualification_event: Event,
        request: CapabilityRequest,
    ) -> DomainEffectProcedureReadinessResult:
        if (
            event.type != DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT
            or event.subject_ref != run.id
        ):
            raise ValueError("domain effect procedure readiness event identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA:
            raise ValueError("domain effect procedure readiness schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect procedure readiness became authoritative")
        if payload.get("work_ref") != work.id:
            raise ValueError("domain effect procedure readiness Work rebound")
        if payload.get("qualification_event_ref") != qualification_event.id:
            raise ValueError("domain effect procedure readiness qualification rebound")
        if payload.get("lease_owner") != run.lease_owner:
            raise ValueError("domain effect procedure readiness lease owner rebound")
        if payload.get("lease_generation") != run.lease_generation:
            raise ValueError("domain effect procedure readiness fencing generation rebound")
        digest = payload.get("readiness_digest")
        profile = payload.get("procedure_profile")
        raw_refs = payload.get("readiness_refs")
        obligations = payload.get("pre_action_obligations")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("domain effect procedure readiness lacks a valid digest")
        if not isinstance(profile, str) or not profile:
            raise ValueError("domain effect procedure readiness lacks profile")
        if not isinstance(raw_refs, list) or not isinstance(obligations, list):
            raise ValueError("domain effect procedure readiness lacks refs/obligations")
        refs = tuple(QualificationRef.model_validate(ref) for ref in raw_refs)
        if len(refs) != 4:
            raise ValueError("domain effect procedure readiness ref graph is incomplete")

        fresh = AssessmentContext.resolve(
            self.store,
            request,
            work=work,
            run=run,
            now=event.created_at,
        )
        if fresh.digest != digest:
            raise ValueError("domain effect procedure readiness snapshot is stale")
        current_profile, current_obligations = self._assert_pre_action_ready(
            fresh,
            work,
            run,
            request,
            now=event.created_at,
        )
        if current_profile != profile:
            raise ValueError("domain effect procedure readiness profile changed")
        if tuple(str(value) for value in obligations) != current_obligations:
            raise ValueError("domain effect procedure readiness obligations changed")
        self._assert_authorization_unconsumed(
            self._required_ref(self._run_metadata(run), "domain_effect_authorization_ref")
        )
        return DomainEffectProcedureReadinessResult(
            run_ref=run.id,
            work_ref=work.id,
            qualification_event_ref=qualification_event.id,
            readiness_event_ref=event.id,
            readiness_digest=digest,
            readiness_refs=refs,
            procedure_profile=profile,
            pre_action_obligations=current_obligations,
            lease_owner=run.lease_owner or "",
            lease_generation=run.lease_generation,
        )


__all__ = [
    "DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT",
    "DOMAIN_EFFECT_PROCEDURE_READINESS_SCHEMA",
    "DomainEffectProcedureReadinessAssessment",
    "DomainEffectProcedureReadinessInput",
    "DomainEffectProcedureReadinessResult",
]
