from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    InvocationContext,
    ProviderDescriptor,
)
from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Event, Run, Work, utcnow
from portable_runtime.core.provider_semantics import (
    ProviderReplayBinding,
    ProviderSemanticContract,
    ProviderSemanticProjection,
    build_provider_replay_binding,
    project_provider_semantics,
)
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.governance.provider_execution_binding import ProviderExecutionBinding
from portable_runtime.records.authorization import is_grant_valid
from portable_runtime.records.models import BaseRecord
from portable_runtime.responsibility.domain_effect_procedure_readiness import (
    DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT,
    DomainEffectProcedureReadinessAssessment,
    DomainEffectProcedureReadinessResult,
)

DOMAIN_EFFECT_PROVIDER_BINDING_EVENT = "domain-effect-provider-binding-recorded"
DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA = "domain-effect-provider-binding-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _semantic_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class DomainEffectProviderBindingInput(BaseModel):
    """Bind one ready domain effect to an exact configured provider target.

    ``provider_id`` is an explicit control-plane selection request. The actual
    provider identity and binding authority must still come from the live
    ``ProviderRegistry``. ``semantic_contract`` is explicit because the runtime
    currently has no authoritative semantic-contract registry and must never
    infer provider-visible semantics from a descriptor alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    semantic_contract: ProviderSemanticContract


class DomainEffectProviderBindingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["bound"] = "bound"
    run_ref: str
    work_ref: str
    qualification_event_ref: str
    readiness_event_ref: str
    binding_event_ref: str
    provider_id: str
    provider_descriptor: ProviderDescriptor
    provider_execution_binding: ProviderExecutionBinding
    provider_replay_binding: ProviderReplayBinding
    semantic_contract: ProviderSemanticContract
    semantic_projection: ProviderSemanticProjection
    invocation_context: InvocationContext
    lease_owner: str
    lease_generation: int = Field(ge=1)
    execution_authority_bearing: bool = False


class DomainEffectProviderBindingAssessment:
    """Freeze provider identity and provider-visible meaning without execution.

    This stage is deliberately earlier than DurableInvocationSpecification and
    the action boundary. It performs no provider method call, creates no
    AuthorizationUse, InvocationPermit, Attempt, dispatch, Action or Outcome,
    and does not claim provider success. The durable event records the exact
    configured ProviderExecutionBinding together with the declared semantic
    contract/projection that a later specification stage must consume.
    """

    def __init__(
        self,
        store: Any,
        provider_registry: ProviderRegistry,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
    ) -> None:
        self.store = store
        self.provider_registry = provider_registry
        self.readiness = DomainEffectProcedureReadinessAssessment(
            store,
            contract_registry=contract_registry,
        )

    def assess(
        self,
        value: DomainEffectProviderBindingInput,
        *,
        assessed_at: datetime | None = None,
    ) -> DomainEffectProviderBindingResult:
        run = self.readiness._require_active_run(value.run_ref)
        work = self.readiness._require_work(run)
        metadata = self.readiness._run_metadata(run)
        qualification_event, request = self.readiness._require_qualification(run)
        readiness_event = self._require_readiness_event(run)
        readiness_result = self.readiness._validate_event(
            readiness_event,
            run,
            work,
            qualification_event,
            request,
        )

        event_id = _stable_id(
            "event_domain_effect_provider_binding",
            run.id,
            run.lease_generation,
            readiness_event.id,
        )
        existing_events = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_PROVIDER_BINDING_EVENT
        ]
        if len(existing_events) > 1:
            raise ValueError("domain effect provider binding is not unique for the Run")
        if existing_events and existing_events[0].id != event_id:
            raise ValueError("domain effect provider binding event identity rebound")

        at = assessed_at or utcnow()
        if existing_events:
            return self._validate_event(
                existing_events[0],
                value,
                run,
                work,
                qualification_event,
                request,
                readiness_result,
            )

        self._assert_grant_valid(metadata, now=at)
        descriptor, provider, execution_binding = self._capture_provider(
            value.provider_id,
            request,
        )
        self._assert_semantic_contract(value.semantic_contract, descriptor)
        self._assert_recovery_compatibility(readiness_result, descriptor)
        invocation_context = self._invocation_context(value.runtime_id, run, request)
        semantic_projection = project_provider_semantics(
            request,
            invocation_context,
            value.semantic_contract,
        )
        replay_binding = build_provider_replay_binding(
            descriptor,
            value.semantic_contract,
            provider_binding_id=execution_binding.id,
        )

        event = Event(
            id=event_id,
            created_at=at,
            type=DOMAIN_EFFECT_PROVIDER_BINDING_EVENT,
            subject_ref=run.id,
            payload={
                "schema": DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA,
                "execution_authority_bearing": False,
                "binding_authority": "registry-configured-provider-target",
                "work_ref": work.id,
                "qualification_event_ref": qualification_event.id,
                "readiness_event_ref": readiness_event.id,
                "readiness_digest": readiness_result.readiness_digest,
                "request_ref": request.id,
                "provider_id": descriptor.id,
                "provider_descriptor": descriptor.model_dump(mode="json"),
                "provider_execution_binding": execution_binding.model_dump(mode="json"),
                "provider_replay_binding": replay_binding.model_dump(mode="json"),
                "semantic_contract": value.semantic_contract.model_dump(mode="json"),
                "semantic_contract_digest": value.semantic_contract.digest,
                "semantic_projection": semantic_projection.model_dump(mode="json"),
                "invocation_context": invocation_context.model_dump(mode="json"),
                "lease_owner": run.lease_owner,
                "lease_generation": run.lease_generation,
            },
        )

        with self.store.transaction():
            self.readiness._recheck_fencing(run)
            self._assert_grant_valid(metadata, now=at)
            current_binding = self.provider_registry.execution_binding(
                descriptor.id,
                expected_provider=provider,
            )
            if current_binding != execution_binding:
                raise ValueError("configured provider changed during provider binding")
            self.readiness._assert_authorization_unconsumed(
                self.readiness._required_ref(metadata, "domain_effect_authorization_ref")
            )
            if self.store.get_event(event.id) is not None:
                raise ValueError("domain effect provider binding appeared concurrently")
            self.store.save_event(event)

        persisted = self.store.get_event(event.id)
        current_run = self.store.get_run(run.id)
        if not isinstance(persisted, Event) or not isinstance(current_run, Run):
            raise ValueError("domain effect provider binding commit is incomplete")
        return self._validate_event(
            persisted,
            value,
            current_run,
            work,
            qualification_event,
            request,
            readiness_result,
        )

    def _require_readiness_event(self, run: Run) -> Event:
        events = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_PROCEDURE_READINESS_EVENT
        ]
        if len(events) != 1:
            raise ValueError(
                "domain effect provider binding requires exactly one procedure readiness event"
            )
        return events[0]

    def _assert_grant_valid(self, metadata: dict[str, Any], *, now: datetime) -> None:
        authorization_ref = self.readiness._required_ref(
            metadata,
            "domain_effect_authorization_ref",
        )
        context = self.readiness.authorization._resolve_context(authorization_ref)
        if not is_grant_valid(context.grant, now=now):
            raise ValueError("domain effect runtime grant is not current for provider binding")
        self.readiness._assert_authorization_unconsumed(authorization_ref)

    def _capture_provider(
        self,
        provider_id: str,
        request: CapabilityRequest,
    ) -> tuple[ProviderDescriptor, Any, ProviderExecutionBinding]:
        if provider_id in request.excluded_provider_ids:
            raise ValueError("selected provider is explicitly excluded by the canonical request")
        if request.preferred_provider_ids and provider_id not in request.preferred_provider_ids:
            raise ValueError("selected provider is outside the canonical request preference set")
        candidates = {
            descriptor.id: descriptor
            for descriptor in self.provider_registry.providers_for(request.capability)
        }
        descriptor = candidates.get(provider_id)
        if descriptor is None:
            raise ValueError(
                "selected provider is not an enabled configured provider for the capability"
            )
        provider, execution_binding = self.provider_registry.capture_execution_target(provider_id)
        if provider.descriptor.id != descriptor.id:
            raise ValueError("provider registry descriptor/object identity mismatch")
        if request.capability not in descriptor.capabilities:
            raise ValueError("selected provider does not advertise the canonical capability")
        return descriptor, provider, execution_binding

    @staticmethod
    def _assert_semantic_contract(
        contract: ProviderSemanticContract,
        descriptor: ProviderDescriptor,
    ) -> None:
        if contract.provider_id != descriptor.id:
            raise ValueError("provider semantic contract does not match selected provider")
        # This also validates that the selected descriptor can be represented by
        # the durable replay-binding model without weakening provider identity.
        if not contract.id.strip() or not contract.version.strip():
            raise ValueError("provider semantic contract requires stable id and version")

    def _assert_recovery_compatibility(
        self,
        readiness: DomainEffectProcedureReadinessResult,
        descriptor: ProviderDescriptor,
    ) -> None:
        recovery_refs = [ref for ref in readiness.readiness_refs if ref.kind == "recovery"]
        if len(recovery_refs) != 1:
            raise ValueError("domain effect readiness lacks one exact recovery proof")
        recovery = self.store.get_record(recovery_refs[0].ref_id)
        if not isinstance(recovery, BaseRecord) or recovery.record_type != "Policy":
            raise ValueError("domain effect recovery proof is not a typed Policy record")
        recovery_metadata = recovery.metadata if isinstance(recovery.metadata, dict) else {}
        if recovery_metadata.get("qualification_kind") != "recovery":
            raise ValueError("domain effect recovery proof role rebound")
        required_effect_semantics = recovery_metadata.get("required_effect_semantics")
        required_reversibility = recovery_metadata.get("required_reversibility")
        if not isinstance(required_effect_semantics, str) or not required_effect_semantics:
            raise ValueError("domain effect recovery proof lacks required effect semantics")
        if not isinstance(required_reversibility, str) or not required_reversibility:
            raise ValueError("domain effect recovery proof lacks required reversibility")
        if descriptor.effect_semantics != required_effect_semantics:
            raise ValueError(
                "selected provider does not satisfy recovery-required effect semantics"
            )
        if descriptor.side_effect_class != descriptor.effect_semantics:
            raise ValueError("selected provider effect semantics/side-effect class disagree")
        if descriptor.reversibility != required_reversibility:
            raise ValueError(
                "selected provider does not satisfy recovery-required reversibility"
            )

    @staticmethod
    def _invocation_context(
        runtime_id: str,
        run: Run,
        request: CapabilityRequest,
    ) -> InvocationContext:
        return InvocationContext(
            runtime_id=runtime_id,
            work_id=run.work_id,
            run_id=run.id,
            metadata={},
            lease_generation=run.lease_generation,
            idempotency_key=request.idempotency_key,
        )

    def _validate_event(
        self,
        event: Event,
        value: DomainEffectProviderBindingInput,
        run: Run,
        work: Work,
        qualification_event: Event,
        request: CapabilityRequest,
        readiness_result: DomainEffectProcedureReadinessResult,
    ) -> DomainEffectProviderBindingResult:
        if event.type != DOMAIN_EFFECT_PROVIDER_BINDING_EVENT or event.subject_ref != run.id:
            raise ValueError("domain effect provider binding event identity rebound")
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA:
            raise ValueError("domain effect provider binding schema rebound")
        if payload.get("execution_authority_bearing") is not False:
            raise ValueError("domain effect provider binding became execution authority")
        if payload.get("binding_authority") != "registry-configured-provider-target":
            raise ValueError("domain effect provider binding authority classification rebound")
        expected_refs = {
            "work_ref": work.id,
            "qualification_event_ref": qualification_event.id,
            "readiness_event_ref": readiness_result.readiness_event_ref,
            "readiness_digest": readiness_result.readiness_digest,
            "request_ref": request.id,
            "provider_id": value.provider_id,
            "lease_owner": run.lease_owner,
            "lease_generation": run.lease_generation,
        }
        for key, expected in expected_refs.items():
            if payload.get(key) != expected:
                raise ValueError(f"domain effect provider binding rebound at {key}")

        raw_descriptor = payload.get("provider_descriptor")
        raw_execution_binding = payload.get("provider_execution_binding")
        raw_replay_binding = payload.get("provider_replay_binding")
        raw_contract = payload.get("semantic_contract")
        raw_projection = payload.get("semantic_projection")
        raw_context = payload.get("invocation_context")
        if not all(
            isinstance(raw, dict)
            for raw in (
                raw_descriptor,
                raw_execution_binding,
                raw_replay_binding,
                raw_contract,
                raw_projection,
                raw_context,
            )
        ):
            raise ValueError("domain effect provider binding payload is incomplete")

        descriptor = ProviderDescriptor.model_validate(raw_descriptor)
        execution_binding = ProviderExecutionBinding.model_validate(raw_execution_binding)
        replay_binding = ProviderReplayBinding.model_validate(raw_replay_binding)
        contract = ProviderSemanticContract.model_validate(raw_contract)
        projection = ProviderSemanticProjection.model_validate(raw_projection)
        invocation_context = InvocationContext.model_validate(raw_context)
        if _semantic_dump(contract) != _semantic_dump(value.semantic_contract):
            raise ValueError("domain effect provider semantic contract rebound")
        if payload.get("semantic_contract_digest") != contract.digest:
            raise ValueError("domain effect provider semantic contract digest rebound")
        if invocation_context.runtime_id != value.runtime_id:
            raise ValueError("domain effect provider binding runtime identity rebound")
        expected_context = self._invocation_context(value.runtime_id, run, request)
        if _semantic_dump(invocation_context) != _semantic_dump(expected_context):
            raise ValueError("domain effect provider invocation context rebound")

        current_descriptor, provider, current_execution_binding = self._capture_provider(
            value.provider_id,
            request,
        )
        if _semantic_dump(descriptor) != _semantic_dump(current_descriptor):
            raise ValueError("domain effect provider descriptor snapshot is stale")
        if execution_binding != current_execution_binding:
            raise ValueError("domain effect provider execution binding snapshot is stale")
        exact_binding = self.provider_registry.execution_binding(
            value.provider_id,
            expected_provider=provider,
        )
        if exact_binding != execution_binding:
            raise ValueError("domain effect configured provider binding changed")

        self._assert_semantic_contract(contract, descriptor)
        self._assert_recovery_compatibility(readiness_result, descriptor)
        current_projection = project_provider_semantics(request, invocation_context, contract)
        if projection != current_projection:
            raise ValueError("domain effect provider semantic projection is stale")
        current_replay_binding = build_provider_replay_binding(
            descriptor,
            contract,
            provider_binding_id=execution_binding.id,
        )
        if replay_binding != current_replay_binding:
            raise ValueError("domain effect provider replay binding is stale")

        self._assert_grant_valid(
            self.readiness._run_metadata(run),
            now=event.created_at,
        )
        return DomainEffectProviderBindingResult(
            run_ref=run.id,
            work_ref=work.id,
            qualification_event_ref=qualification_event.id,
            readiness_event_ref=readiness_result.readiness_event_ref,
            binding_event_ref=event.id,
            provider_id=descriptor.id,
            provider_descriptor=descriptor,
            provider_execution_binding=execution_binding,
            provider_replay_binding=replay_binding,
            semantic_contract=contract,
            semantic_projection=projection,
            invocation_context=invocation_context,
            lease_owner=run.lease_owner or "",
            lease_generation=run.lease_generation,
        )


__all__ = [
    "DOMAIN_EFFECT_PROVIDER_BINDING_EVENT",
    "DOMAIN_EFFECT_PROVIDER_BINDING_SCHEMA",
    "DomainEffectProviderBindingAssessment",
    "DomainEffectProviderBindingInput",
    "DomainEffectProviderBindingResult",
]
