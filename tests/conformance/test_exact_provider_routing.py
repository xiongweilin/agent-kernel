from __future__ import annotations

import pytest

from portable_runtime.core.capabilities import CapabilityRequest, ProviderDescriptor
from portable_runtime.core.router import DeterministicPriorityRouting


@pytest.mark.asyncio
async def test_exact_provider_constraint_beats_preference_and_priority() -> None:
    routing = DeterministicPriorityRouting()
    request = CapabilityRequest(
        id="request:exact-provider",
        capability="administrative.hris.employee.create.v1",
        constraints={"exact_provider_id": "provider:b"},
        preferred_provider_ids=["provider:a"],
    )
    candidates = [
        ProviderDescriptor(
            id="provider:a",
            name="A",
            version="1",
            capabilities=[request.capability],
            priority=100,
        ),
        ProviderDescriptor(
            id="provider:b",
            name="B",
            version="1",
            capabilities=[request.capability],
            priority=0,
        ),
    ]

    selected = await routing.select(request, candidates)

    assert selected is not None
    assert selected.id == "provider:b"


@pytest.mark.asyncio
async def test_exact_provider_constraint_fails_closed_when_target_is_absent() -> None:
    routing = DeterministicPriorityRouting()
    request = CapabilityRequest(
        id="request:missing-exact-provider",
        capability="administrative.hris.employee.create.v1",
        constraints={"exact_provider_id": "provider:missing"},
    )
    candidates = [
        ProviderDescriptor(
            id="provider:a",
            name="A",
            version="1",
            capabilities=[request.capability],
        )
    ]

    assert await routing.select(request, candidates) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   ", 7, False])
async def test_exact_provider_constraint_rejects_malformed_identity(value: object) -> None:
    routing = DeterministicPriorityRouting()
    request = CapabilityRequest(
        id="request:malformed-exact-provider",
        capability="administrative.hris.employee.create.v1",
        constraints={"exact_provider_id": value},
    )
    candidates = [
        ProviderDescriptor(
            id="provider:a",
            name="A",
            version="1",
            capabilities=[request.capability],
        )
    ]

    assert await routing.select(request, candidates) is None
