from __future__ import annotations

import pytest

from portable_runtime.core.capabilities import CapabilityRequest, ProviderDescriptor
from portable_runtime.core.router import DeterministicPriorityRouting, ExactProviderRouting


def _request() -> CapabilityRequest:
    return CapabilityRequest(
        id="request:exact-provider",
        capability="administrative.hris.employee.create.v1",
        preferred_provider_ids=["provider:a"],
    )


def _candidates(request: CapabilityRequest) -> list[ProviderDescriptor]:
    return [
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


@pytest.mark.asyncio
async def test_exact_provider_routing_beats_request_preference_and_priority() -> None:
    request = _request()
    routing = ExactProviderRouting("provider:b", DeterministicPriorityRouting())

    selected = await routing.select(request, _candidates(request))

    assert selected is not None
    assert selected.id == "provider:b"
    assert request.preferred_provider_ids == ["provider:a"]
    assert request.constraints == {}


@pytest.mark.asyncio
async def test_exact_provider_routing_fails_closed_when_target_is_absent() -> None:
    request = _request()
    routing = ExactProviderRouting("provider:missing", DeterministicPriorityRouting())

    assert await routing.select(request, _candidates(request)) is None


@pytest.mark.parametrize("value", ["", "   "])
def test_exact_provider_routing_rejects_malformed_identity(value: str) -> None:
    with pytest.raises(ValueError, match="non-empty provider id"):
        ExactProviderRouting(value, DeterministicPriorityRouting())
