from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext
from portable_runtime.core.models import Event, Run, Work, utcnow
from portable_runtime.core.provider_semantics import ProviderSemanticContract
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT,
)
from portable_runtime.responsibility.domain_effect_provider_binding import (
    DOMAIN_EFFECT_PROVIDER_BINDING_EVENT,
    DomainEffectProviderBindingAssessment,
    DomainEffectProviderBindingInput,
    DomainEffectProviderBindingResult,
)
from portable_runtime.workflows.invocation_specification import (
    DurableInvocationSpecification,
    InvocationSpecificationCommitRequest,
)

DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT = (
    "domain-effect-invocation-specification-recorded"
)
DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA = "domain-effect-invocation-specification-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectInvocationSpecificationInput(BaseModel):
    """Identify the bound Run whose durable provider operation meaning is captured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)


class DomainEffectInvocationSpecificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["recorded"] = "recorded"
    run_ref: str
    work_ref: str
    qualification_event_ref: str
    readiness_event_ref: str
    provider_binding_event_ref: str
    link_event_ref: str
    specification_ref: str
    specification: DurableInvocationSpecification
    provider_execution_binding_ref: str
    semantic_identity: str
    semantic_contract_digest: str
    lease_owner: str
    lease_generation: int = Field(ge=1)
    authority_bearing: bool = False
    execution_authority_bearing: bool = False


class DomainEffectInvocationSpecificationCapture:
    """Commit durable provider meaning without crossing the action boundary.

    The authoritative InvocationSpecification event is created exclusively by
    the store's ``commit_invocation_specification`` primitive. This domain
    stage adds a deterministic non-authoritative Run link that records which
    prior provider-binding fact justified that capture. It creates no
    AuthorizationUse, InvocationPermit, Attempt, dispatch, Action or Outcome
    and performs no provider method call.
    """

    def __init__(
        self,
        store: Any,
        provider_binding: DomainEffectProviderBindingAssessment,
    ) -> None:
        self.store = store
        self.provider_binding = provider_binding

    def capture(
        self,
        value: DomainEffectInvocationSpecificationInput,
        *,
        captured_at: datetime | None = None,
    ) -> DomainEffectInvocationSpecificationResult:
        run = self.provider_binding.readiness._require_active_run(value.run_ref)
        work = self.provider_binding.readiness._require_work(run)
        metadata = self.provider_binding.readiness._run_metadata(run)
        qualification_event, request = self.provider_binding.readiness._require_qualification(run)
        readiness_event = self._require_single_event(
            run,
            DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT,
            "procedure readiness",
        )
        provider_event = self._require_single_event(
            run,
            DOMAIN_EFFECT_PROVIDER_BINDING_EVENT,
            "provider binding",
        )
        binding = self._validate_provider_binding(
            provider_event,
            run,
            work,
            qualification_event,
            request,
            readiness_event,
        )
        self.provider_binding.readiness._assert_authorization_unconsumed(
            self.provider_binding.readiness._required_ref(
                metadata,
                "domain_effect_authorization_ref",
            )
        )

        commit = getattr(self.store, "commit_invocation_specification", None)
        if not callable(commit):
            raise ValueError(
                "domain effect invocation specification requires an authority-capable specification store"
            )
        commit_request = self._commit_request(request, binding)
        expected = self._expected_specification(commit_request)
        link_id = _stable_id(
            "event_domain_effect_invocation_specification",
            run.id,
            run.lease_generation,
            provider_event.id,
            expected.id,
        )
        existing = self.store.get_event(link_id)
        if existing is not None:
            return self._validate_link(
                existing,
                run,
                work,
                qualification_event,
                readiness_event,
                provider_event,
                binding,
                expected,
            )

        at = captured_at or utcnow()
        with self.store.transaction():
            self.provider_binding.readiness._recheck_fencing(run)
            current_provider_event = self.store.get_event(provider_event.id)
            if current_provider_event != provider_event:
                raise ValueError("domain effect provider binding changed during specification capture")
            self.provider_binding.readiness._assert_authorization_unconsumed(
                self.provider_binding.readiness._required_ref(
                    metadata,
                    "domain_effect_authorization_ref",
                )
            )
            specification = commit(commit_request)
            self._assert_specification_matches_binding(specification, request, binding)
            if specification != expected:
                raise ValueError("domain effect invocation specification candidate rebound")
            if self.store.get_event(link_id) is not None:
                raise ValueError(
                    "domain effect invocation specification link appeared concurrently"
                )
            link = Event(
                id=link_id,
                created_at=at,
                type=DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT,
                subject_ref=run.id,
                payload={
                    "schema": DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA,
                    "authority_bearing": False,
                    "execution_authority_bearing": False,
                    "work_ref": work.id,
                    "qualification_event_ref": qualification_event.id,
                    "readiness_event_ref": readiness_event.id,
                    "provider_binding_event_ref": provider_event.id,
                    "specification_ref": specification.id,
                    "source_request_ref": request.id,
                    "provider_execution_binding_ref": (
                        binding.provider_execution_binding.id
                    ),
                    "semantic_identity": specification.semantic_identity,
                    "semantic_contract_digest": specification.semantic_contract_digest,
                    "lease_owner": run.lease_owner,
                    "lease_generation": run.lease_generation,
                },
            )
            self.store.save_event(link)

        persisted = self.store.get_event(link_id)
        specification = self._get_specification(expected.id)
        current_run = self.store.get_run(run.id)
        if (
            not isinstance(persisted, Event)
            or not isinstance(specification, DurableInvocationSpecification)
            or not isinstance(current_run, Run)
        ):
            raise ValueError("domain effect invocation specification commit is incomplete")
        return self._validate_link(
            persisted,
            current_run,
            work,
            qualification_event,
            readiness_event,
            provider_event,
            binding,
            specification,
        )

    def _require_single_event(self, run: Run, event_type: str, label: str) -> Event:
        events = [event for event in self.store.list_events(run.id) if event.type == event_type]
        if len(events) != 1:
            raise ValueError(
                f"domain effect invocation specification requires exactly one {label} event"
            )
        return events[0]

    def _validate_provider_binding(
        self,
        event: Event,
        run: Run,
        work: Work,
        qualification_event: Event,
        request: CapabilityRequest,
        readiness_event: Event,
    ) -> DomainEffectProviderBindingResult:
        payload = event.payload if isinstance(event.payload, dict) else {}
        raw_contract = payload.get("semantic_contract")
        raw_context = payload.get("invocation_context")
        provider_id = payload.get("provider_id")
        if (
            not isinstance(raw_contract, dict)
            or not isinstance(raw_context, dict)
            or not isinstance(provider_id, str)
            or not provider_id
        ):
            raise ValueError("domain effect provider binding event is incomplete")
        contract = ProviderSemanticContract.model_validate(raw_contract)
        context = InvocationContext.model_validate(raw_context)
        command = DomainEffectProviderBindingInput(
            run_ref=run.id,
            provider_id=provider_id,
            runtime_id=context.runtime_id,
            semantic_contract=contract,
        )
        readiness = self.provider_binding.readiness._validate_event(
            readiness_event,
            run,
            work,
            qualification_event,
            request,
        )
        return self.provider_binding._validate_event(
            event,
            command,
            run,
            work,
            qualification_event,
            request,
            readiness,
        )

    @staticmethod
    def _commit_request(
        request: CapabilityRequest,
        binding: DomainEffectProviderBindingResult,
    ) -> InvocationSpecificationCommitRequest:
        return InvocationSpecificationCommitRequest(
            request=request,
            context=binding.invocation_context,
            provider_descriptor=binding.provider_descriptor,
            semantic_contract=binding.semantic_contract,
            provider_binding_id=binding.provider_execution_binding.id,
        )

    @staticmethod
    def _expected_specification(
        commit_request: InvocationSpecificationCommitRequest,
    ) -> DurableInvocationSpecification:
        from portable_runtime.workflows.invocation_specification import (
            build_invocation_specification,
        )

        return build_invocation_specification(
            commit_request.request,
            commit_request.context,
            commit_request.provider_descriptor,
            commit_request.semantic_contract,
            provider_binding_id=commit_request.provider_binding_id,
        )

    @staticmethod
    def _assert_specification_matches_binding(
        specification: DurableInvocationSpecification,
        request: CapabilityRequest,
        binding: DomainEffectProviderBindingResult,
    ) -> None:
        if specification.source_request_ref != request.id:
            raise ValueError("domain effect InvocationSpecification request rebound")
        if specification.source_work_ref != request.work_id:
            raise ValueError("domain effect InvocationSpecification Work rebound")
        if specification.source_run_ref != request.run_id:
            raise ValueError("domain effect InvocationSpecification Run rebound")
        if specification.provider_binding != binding.provider_replay_binding:
            raise ValueError("domain effect InvocationSpecification provider binding rebound")
        if specification.semantic_identity != binding.semantic_projection.identity:
            raise ValueError("domain effect InvocationSpecification semantic identity rebound")
        if (
            specification.canonical_semantic_payload
            != binding.semantic_projection.canonical_payload
        ):
            raise ValueError("domain effect InvocationSpecification semantic payload rebound")
        if specification.semantic_contract_digest != binding.semantic_contract.digest:
            raise ValueError("domain effect InvocationSpecification semantic contract rebound")
        if specification.effect_semantics != binding.provider_descriptor.effect_semantics:
            raise ValueError("domain effect InvocationSpecification effect semantics rebound")

    def _get_specification(
        self,
        specification_id: str,
    ) -> DurableInvocationSpecification | None:
        getter = getattr(self.store, "get_invocation_specification", None)
        if not callable(getter):
            return None
        value = getter(specification_id)
        return value if isinstance(value, DurableInvocationSpecification) else None

    def _validate_link(
        self,
        event: Event,
        run: Run,
        work: Work,
        qualification_event: Event,
        readiness_event: Event,
        provider_event: Event,
        binding: DomainEffectProviderBindingResult,
        specification: DurableInvocationSpecification,
    ) -> DomainEffectInvocationSpecificationResult:
        if (
            event.type != DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
            or event.subject_ref != run.id
        ):
            raise ValueError("domain effect InvocationSpecification link identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA:
            raise ValueError("domain effect InvocationSpecification link schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect InvocationSpecification link became authority")
        if payload.get("execution_authority_bearing") is not False:
            raise ValueError("domain effect InvocationSpecification link became execution authority")
        expected = {
            "work_ref": work.id,
            "qualification_event_ref": qualification_event.id,
            "readiness_event_ref": readiness_event.id,
            "provider_binding_event_ref": provider_event.id,
            "specification_ref": specification.id,
            "source_request_ref": specification.source_request_ref,
            "provider_execution_binding_ref": binding.provider_execution_binding.id,
            "semantic_identity": specification.semantic_identity,
            "semantic_contract_digest": specification.semantic_contract_digest,
            "lease_owner": run.lease_owner,
            "lease_generation": run.lease_generation,
        }
        for key, expected_value in expected.items():
            if payload.get(key) != expected_value:
                raise ValueError(f"domain effect InvocationSpecification link rebound at {key}")
        persisted = self._get_specification(specification.id)
        if persisted != specification:
            raise ValueError("domain effect InvocationSpecification authority is unavailable")
        self._assert_specification_matches_binding(persisted, binding_request(binding), binding)
        self.provider_binding.readiness._assert_authorization_unconsumed(
            self.provider_binding.readiness._required_ref(
                self.provider_binding.readiness._run_metadata(run),
                "domain_effect_authorization_ref",
            )
        )
        return DomainEffectInvocationSpecificationResult(
            run_ref=run.id,
            work_ref=work.id,
            qualification_event_ref=qualification_event.id,
            readiness_event_ref=readiness_event.id,
            provider_binding_event_ref=provider_event.id,
            link_event_ref=event.id,
            specification_ref=persisted.id,
            specification=persisted,
            provider_execution_binding_ref=binding.provider_execution_binding.id,
            semantic_identity=persisted.semantic_identity,
            semantic_contract_digest=persisted.semantic_contract_digest,
            lease_owner=run.lease_owner or "",
            lease_generation=run.lease_generation,
        )


def binding_request(binding: DomainEffectProviderBindingResult) -> CapabilityRequest:
    payload = binding.semantic_projection.payload().get("request")
    if not isinstance(payload, dict):
        raise ValueError("domain effect provider semantic projection lacks request payload")
    # The semantic projection intentionally omits runtime authority fields, so
    # it cannot reconstruct the canonical CapabilityRequest. This helper is
    # therefore never a source of request authority; callers must replace it
    # with the persisted qualification request before use.
    raise ValueError("canonical request must come from qualification, not semantic projection")


__all__ = [
    "DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT",
    "DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA",
    "DomainEffectInvocationSpecificationCapture",
    "DomainEffectInvocationSpecificationInput",
    "DomainEffectInvocationSpecificationResult",
]
