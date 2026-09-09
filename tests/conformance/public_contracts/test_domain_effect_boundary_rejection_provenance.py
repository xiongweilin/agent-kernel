from __future__ import annotations

import pytest

from portable_runtime.core.capabilities import CapabilityResult
from portable_runtime.public_contracts.domain_effect import BoundedDomainEffectExecutionService
from portable_runtime.responsibility.domain_effect_reality_execution import DomainEffectRealityExecution
from tests.conformance.public_contracts.test_bounded_domain_effect_execution import _fixture


@pytest.mark.asyncio
async def test_high_level_execution_preserves_boundary_rejection_before_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, service, command, effect_provider, verifier = _fixture()

    async def blocked_before_precommit(
        self: DomainEffectRealityExecution,
        request,
    ) -> CapabilityResult:
        del self
        return CapabilityResult(
            request_id=request.id,
            provider_id="provider:hris:public-domain-effect",
            status="unavailable",
            error={
                "code": "ReliabilityBlocked",
                "reason": "cooldown active",
            },
        )

    monkeypatch.setattr(DomainEffectRealityExecution, "execute", blocked_before_precommit)

    with pytest.raises(
        ValueError,
        match=(
            "bounded domain-effect execution blocked before durable Attempt: "
            "ReliabilityBlocked: cooldown active"
        ),
    ):
        await service.execute(command)

    assert effect_provider.invocations == 0
    assert verifier.invocations == 0


def test_service_type_is_imported_for_public_contract_stability() -> None:
    assert BoundedDomainEffectExecutionService.__name__ == "BoundedDomainEffectExecutionService"
