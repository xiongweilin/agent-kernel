from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import CapabilityRequest, CapabilityResult
from portable_runtime.core.router import ExactProviderRouting, RoutingPolicy
from portable_runtime.governance.dispatch import (
    DispatchActionAuthorityBinding,
    bind_dispatch_action_authority,
)
from portable_runtime.responsibility.domain_effect_action_authority import (
    DomainEffectActionAuthorityResolver,
)


@dataclass(frozen=True, slots=True)
class _BoundaryExecutionScope:
    """Task-local CapabilityService-shaped inputs consumed by RealityBoundary."""

    registry: Any
    store: Any
    routing: RoutingPolicy


class DomainEffectRealityExecution:
    """Cut one bounded domain effect over the existing sole reality exit.

    This adapter owns no provider invocation. It resolves Kernel-owned action
    authority, narrows routing to the exact provider frozen by the durable
    InvocationSpecification, binds that authority to the current async task,
    and delegates the entire execution to ``RealityBoundary.execute``.
    """

    def __init__(
        self,
        boundary: RealityBoundary,
        action_authority: DomainEffectActionAuthorityResolver,
    ) -> None:
        if boundary.store is not None and boundary.store is not action_authority.store:
            raise ValueError("domain effect execution store must match RealityBoundary store")
        if (
            boundary.registry is not None
            and boundary.registry is not action_authority.provider_registry
        ):
            raise ValueError(
                "domain effect execution registry must match RealityBoundary registry"
            )
        self.boundary = boundary
        self.action_authority = action_authority

    async def execute(self, request: CapabilityRequest) -> CapabilityResult:
        binding = self.action_authority.resolve(request)
        routing = ExactProviderRouting(binding.provider_id, self.boundary.routing)
        scope = _BoundaryExecutionScope(
            registry=self.action_authority.provider_registry,
            store=self.action_authority.store,
            routing=routing,
        )
        dispatch_binding = DispatchActionAuthorityBinding(
            request_id=request.id,
            provider_id=binding.provider_id,
            provider_execution_binding_ref=binding.provider_execution_binding_ref,
            invocation_specification_ref=binding.invocation_specification_ref,
            authorization_use_factory=binding.authorization_use_factory,
        )
        with bind_dispatch_action_authority(dispatch_binding):
            return await self.boundary.execute(
                request,
                capability_service=scope,
            )


__all__ = ["DomainEffectRealityExecution"]
