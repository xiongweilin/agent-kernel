"""Composite local stores for bounded domain-effect recovery.

Bounded domain-effect recovery needs two opt-in authority capabilities together:

- DurableInvocationSpecification authority used by the original physical dispatch;
- RecoveryApplication-bound RecoveryObservation authority used by B4 reconciliation.

The baseline StateStore remains unchanged. These composites deliberately combine
only those already-frozen authority surfaces for deployments that execute bounded
external effects and must later reconcile an ambiguous committed dispatch.
"""

from __future__ import annotations

from portable_runtime.stores.invocation_specification import (
    InvocationSpecificationInMemoryStateStore,
    InvocationSpecificationSQLiteStateStore,
)
from portable_runtime.stores.recovery_application_observation import (
    RecoveryApplicationObservationInMemoryStateStore,
    RecoveryApplicationObservationSQLiteStateStore,
)


class BoundedDomainEffectRecoveryInMemoryStateStore(
    InvocationSpecificationInMemoryStateStore,
    RecoveryApplicationObservationInMemoryStateStore,
):
    """In-memory bounded-effect store with explicit recovery completion authority."""


class BoundedDomainEffectRecoverySQLiteStateStore(
    InvocationSpecificationSQLiteStateStore,
    RecoveryApplicationObservationSQLiteStateStore,
):
    """SQLite bounded-effect store with explicit recovery completion authority."""


__all__ = [
    "BoundedDomainEffectRecoveryInMemoryStateStore",
    "BoundedDomainEffectRecoverySQLiteStateStore",
]
