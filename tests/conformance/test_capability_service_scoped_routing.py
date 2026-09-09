from portable_runtime.core.router import CapabilityService, ExactProviderRouting
from portable_runtime.core.runtime import Runtime


def test_shared_boundary_preserves_explicit_scoped_routing() -> None:
    runtime = Runtime()
    exact = ExactProviderRouting("provider:verifier", runtime.routing)

    scoped = CapabilityService(
        boundary=runtime.boundary,
        routing=exact,
    )

    assert scoped.boundary is runtime.boundary
    assert scoped.registry is runtime.registry
    assert scoped.store is runtime.store
    assert scoped.routing is exact
