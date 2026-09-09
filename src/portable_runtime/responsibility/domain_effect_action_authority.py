from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Event, Run, utcnow
from portable_runtime.core.qualification import InvocationPermit
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.governance.dispatch import DispatchAuthorizationUseFactory
from portable_runtime.governance.provider_execution_binding import ProviderExecutionBinding
from portable_runtime.records.authorization import AuthorizationUse, create_authorization_use
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_invocation_specification import (
    DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT,
    DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA,
)
from portable_runtime.responsibility.domain_effect_run import (
    DOMAIN_EFFECT_RUN_SCHEMA,
    DOMAIN_EFFECT_WORKFLOW_ID,
)
from portable_runtime.workflows.invocation_specification import DurableInvocationSpecification


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class DomainEffectActionAuthorityBinding:
    """Runtime-owned action binding derived from durable Kernel facts.

    It is not itself authority. The callable returns a fresh, unpersisted
    `AuthorizationUse` candidate only when invoked from dispatch linearization.
    The committer remains responsible for persisting that use atomically with
    the dispatch commitment.
    """

    run_ref: str
    provider_id: str
    provider_execution_binding_ref: str
    invocation_specification_ref: str
    authorization_ref: str
    authorization_use_factory: DispatchAuthorizationUseFactory


class DomainEffectActionAuthorityResolver:
    """Resolve one bounded domain-effect action without trusting caller metadata."""

    def __init__(
        self,
        store: Any,
        provider_registry: ProviderRegistry,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.provider_registry = provider_registry
        self.contract_registry = contract_registry or CapabilityContractRegistry()
        self.authorization = DomainEffectAuthorizationUseConsumption(
            store,
            contract_registry=self.contract_registry,
        )
        self.now = now

    def resolve(self, request: CapabilityRequest) -> DomainEffectActionAuthorityBinding:
        run = self._require_run(request)
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        if metadata.get("schema") != DOMAIN_EFFECT_RUN_SCHEMA:
            raise ValueError("domain effect action authority requires the canonical Run schema")
        if metadata.get("authorization_use_requirement") != "consume-at-action-boundary":
            raise ValueError("domain effect action authority ordering changed")

        link = self._require_specification_link(run)
        payload = link.payload if isinstance(link.payload, dict) else {}
        if payload.get("schema") != DOMAIN_EFFECT_INVOCATION_SPECIFICATION_SCHEMA:
            raise ValueError("domain effect InvocationSpecification link schema rebound")
        if payload.get("authority_bearing") is not False:
            raise ValueError("domain effect InvocationSpecification link became authority")
        if payload.get("execution_authority_bearing") is not False:
            raise ValueError("domain effect InvocationSpecification link became execution authority")
        if payload.get("source_request_ref") != request.id:
            raise ValueError("domain effect action request does not match captured specification request")
        if payload.get("work_ref") != request.work_id or request.work_id != run.work_id:
            raise ValueError("domain effect action request Work rebound")
        if request.run_id != run.id:
            raise ValueError("domain effect action request Run rebound")
        if payload.get("lease_owner") != run.lease_owner:
            raise ValueError("domain effect action specification lease owner rebound")
        if payload.get("lease_generation") != run.lease_generation:
            raise ValueError("domain effect action specification fencing generation rebound")

        specification_ref = self._required_ref(payload, "specification_ref")
        specification = self._get_specification(specification_ref)
        if specification.source_request_ref != request.id:
            raise ValueError("InvocationSpecification source request rebound at action time")
        if specification.source_work_ref != request.work_id:
            raise ValueError("InvocationSpecification source Work rebound at action time")
        if specification.source_run_ref != request.run_id:
            raise ValueError("InvocationSpecification source Run rebound at action time")

        provider_id = specification.provider_binding.provider_id
        expected_binding_ref = specification.provider_binding.provider_binding_id
        if payload.get("provider_execution_binding_ref") != expected_binding_ref:
            raise ValueError("domain effect specification link provider binding rebound")
        current_binding = self.provider_registry.execution_binding(provider_id)
        if current_binding.id != expected_binding_ref:
            raise ValueError("exact configured provider target changed before action boundary")

        authorization_ref = self._required_ref(metadata, "domain_effect_authorization_ref")
        self._validate_live_authorization_request(authorization_ref, request)
        factory = self._authorization_factory(
            authorization_ref,
            expected_binding_ref,
            specification,
        )
        return DomainEffectActionAuthorityBinding(
            run_ref=run.id,
            provider_id=provider_id,
            provider_execution_binding_ref=expected_binding_ref,
            invocation_specification_ref=specification.id,
            authorization_ref=authorization_ref,
            authorization_use_factory=factory,
        )

    def _require_run(self, request: CapabilityRequest) -> Run:
        if not request.run_id:
            raise ValueError("domain effect action authority requires a Run-bound request")
        run = self.store.get_run(request.run_id)
        if not isinstance(run, Run):
            raise ValueError("domain effect action authority requires an existing Run")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("domain effect action authority requires the canonical workflow")
        if run.status != "running" or run.started_at is None:
            raise ValueError("domain effect action authority requires an active Run")
        if not run.lease_owner or run.lease_generation < 1 or run.lease_expires_at is None:
            raise ValueError("domain effect action authority requires current fencing")
        if run.lease_expires_at <= self.now():
            raise ValueError("domain effect action authority fencing lease expired")
        if request.lease_owner != run.lease_owner:
            raise ValueError("domain effect action request lease owner rebound")
        if request.lease_generation != run.lease_generation:
            raise ValueError("domain effect action request fencing generation rebound")
        return run

    def _require_specification_link(self, run: Run) -> Event:
        links = [
            event
            for event in self.store.list_events(run.id)
            if event.type == DOMAIN_EFFECT_INVOCATION_SPECIFICATION_EVENT
        ]
        if len(links) != 1:
            raise ValueError(
                "domain effect action authority requires exactly one InvocationSpecification link"
            )
        return links[0]

    @staticmethod
    def _required_ref(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"domain effect action authority lacks {key}")
        return value

    def _get_specification(self, specification_ref: str) -> DurableInvocationSpecification:
        getter = getattr(self.store, "get_invocation_specification", None)
        if not callable(getter):
            raise ValueError("action boundary store cannot resolve InvocationSpecification")
        specification = getter(specification_ref)
        if not isinstance(specification, DurableInvocationSpecification):
            raise ValueError("domain effect action InvocationSpecification is unavailable")
        return specification

    def _validate_live_authorization_request(
        self,
        authorization_ref: str,
        request: CapabilityRequest,
    ) -> None:
        context = self.authorization._resolve_context(authorization_ref)
        expected = context.request
        if expected.capability != request.capability:
            raise ValueError("domain effect action capability rebound")
        if expected.actor_ref != (request.actor_ref or ""):
            raise ValueError("domain effect action actor rebound")
        if expected.resource_ref != (request.resource_ref or ""):
            raise ValueError("domain effect action resource rebound")
        if expected.effect_class != request.effect_class:
            raise ValueError("domain effect action effect class rebound")
        if set(expected.subject_version_refs) != set(request.subject_version_refs):
            raise ValueError("domain effect action subject version rebound")
        if context.intent.parameters != request.parameters:
            raise ValueError("domain effect action parameters rebound")

    def _authorization_factory(
        self,
        authorization_ref: str,
        expected_binding_ref: str,
        expected_specification: DurableInvocationSpecification,
    ) -> DispatchAuthorizationUseFactory:
        def build(
            request: CapabilityRequest,
            _permit: InvocationPermit,
            execution_binding: ProviderExecutionBinding | None,
            specification: Any | None,
        ) -> AuthorizationUse:
            if execution_binding is None or execution_binding.id != expected_binding_ref:
                raise ValueError("domain effect action provider binding changed at dispatch")
            if specification != expected_specification:
                raise ValueError("domain effect action InvocationSpecification changed at dispatch")
            context = self.authorization._resolve_context(authorization_ref)
            self._validate_live_authorization_request(authorization_ref, request)
            foreign = [
                use
                for use in self.store.list_authorization_uses()
                if isinstance(use, AuthorizationUse)
                and use.authorization_ref == context.grant.id
            ]
            if foreign:
                raise ValueError("domain effect runtime grant was already consumed")
            created = create_authorization_use(
                context.grant,
                context.request,
                authorized_at=self.now(),
            )
            use_id = _stable_id(
                "authuse_domain_effect",
                context.grant.id,
                context.evidence.id,
                context.admission.subject_version_ref,
            )
            return created.model_copy(
                update={
                    "id": use_id,
                    "created_at": created.authorized_at,
                }
            )

        return build


__all__ = [
    "DomainEffectActionAuthorityBinding",
    "DomainEffectActionAuthorityResolver",
]
