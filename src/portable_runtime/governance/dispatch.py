from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from portable_runtime.core.capabilities import CapabilityRequest
from portable_runtime.core.models import Event
from portable_runtime.core.qualification import InvocationPermit
from portable_runtime.core.reconciliation_repeatability import (
    ReconciliationRepeatabilityAuthority,
)
from portable_runtime.governance.provider_execution_binding import ProviderExecutionBinding
from portable_runtime.governance.use_admission import (
    GovernanceUseAdmission,
    GovernanceUseRequirementResolver,
)
from portable_runtime.records.authorization import AuthorizationUse
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.stores.sqlite import SQLiteStateStore

DISPATCH_COMMIT_EVENT = "InvocationDispatchCommitted"
DISPATCH_COMMIT_SCHEMA = "governance-dispatch-commit-v1"

DispatchCommitStatus = Literal[
    "not-applicable",
    "committed",
    "blocked",
    "changed",
    "unavailable",
    "stale",
]
DispatchRecoveryMode = Literal[
    "uncommitted",
    "idempotent-retry",
    "reconcile",
    "unknown",
]
DispatchAuthorizationUseFactory = Callable[
    [CapabilityRequest, InvocationPermit, ProviderExecutionBinding | None, Any | None],
    AuthorizationUse,
]


@dataclass(frozen=True)
class DispatchCommitDecision:
    status: DispatchCommitStatus
    commit_ref: str | None = None
    reason: str = ""
    current_snapshot_digest: str | None = None
    provider_execution_binding_ref: str | None = None
    reconciliation_repeatability_authority_ref: str | None = None
    authorization_use_ref: str | None = None
    invocation_specification_ref: str | None = None


class DispatchLinearizationError(RuntimeError):
    """The authoritative StateStore cannot provide a linearized write scope."""


@contextmanager
def _dispatch_linearized_write(store: Any) -> Iterator[None]:
    """Serialize governance truth and dispatch commitment in one store domain."""

    if isinstance(store, InMemoryStateStore):
        with store.transaction():
            yield
        return

    if isinstance(store, SQLiteStateStore):
        namespace = vars(store)
        connection = cast(sqlite3.Connection, namespace["_connection"])
        lock = namespace["_lock"]
        with lock:
            if connection.in_transaction:
                raise DispatchLinearizationError(
                    "dispatch commitment requires a top-level SQLite write transaction"
                )
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return

    raise DispatchLinearizationError(
        "StateStore does not support governance dispatch linearization"
    )


@contextmanager
def _provider_binding_dispatch_authority(store: Any, *, bound: bool) -> Iterator[None]:
    """Open the narrow store-owned append gate for a B-aware dispatch fact.

    Every valid C-aware dispatch is also B-aware because C is structurally bound
    to the exact ProviderExecutionBinding. The existing B gate therefore also
    fences direct append and P5 import of valid C authority-bearing dispatches.
    """

    if not bound:
        yield
        return
    attribute = "_provider_execution_binding_dispatch_commit_depth"
    if not hasattr(store, attribute):
        raise DispatchLinearizationError(
            "StateStore does not support provider execution-binding dispatch authority"
        )
    setattr(store, attribute, int(getattr(store, attribute)) + 1)
    try:
        yield
    finally:
        setattr(store, attribute, int(getattr(store, attribute)) - 1)


def _optional_identity_ref(
    payload: dict[str, Any],
    key: str,
    label: str,
    identity: dict[str, Any],
) -> None:
    if key not in payload:
        return
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"dispatch {label} ref is malformed")
    identity[key] = value


def dispatch_commit_identity_from_payload(payload: dict[str, Any]) -> str:
    """Reconstruct one dispatch identity with exact additive compatibility.

    Absence of optional B/C/action-authority refs preserves earlier deterministic
    identities byte-for-byte. Presence of any ref makes that authority identity
    part of the dispatch identity. A malformed present ref is never interpreted
    as an older case.
    """

    identity: dict[str, Any] = {
        "schema": payload.get("schema"),
        "request_id": payload.get("request_id"),
        "provider_id": payload.get("provider_id"),
        "attempt_id": payload.get("attempt_ref"),
        "invocation_permit_digest": payload.get("invocation_permit_digest"),
        "governance_requirement_digest": payload.get("governance_requirement_digest"),
        "governance_snapshot_digest": payload.get("governance_snapshot_digest"),
    }
    _optional_identity_ref(
        payload,
        "provider_execution_binding_ref",
        "provider execution binding",
        identity,
    )
    _optional_identity_ref(
        payload,
        "reconciliation_repeatability_authority_ref",
        "reconciliation repeatability authority",
        identity,
    )
    _optional_identity_ref(
        payload,
        "authorization_use_ref",
        "authorization use",
        identity,
    )
    _optional_identity_ref(
        payload,
        "invocation_specification_ref",
        "invocation specification",
        identity,
    )
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"dispatch_{hashlib.sha256(raw.encode()).hexdigest()}"


def _dispatch_commit_ref(
    request: CapabilityRequest,
    permit: InvocationPermit,
    attempt_id: str | None,
    provider_execution_binding_ref: str | None = None,
    reconciliation_repeatability_authority_ref: str | None = None,
    authorization_use_ref: str | None = None,
    invocation_specification_ref: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema": DISPATCH_COMMIT_SCHEMA,
        "request_id": request.id,
        "provider_id": permit.provider_id,
        "attempt_ref": attempt_id,
        "invocation_permit_digest": permit.request_digest,
        "governance_requirement_digest": permit.governance_requirement_digest,
        "governance_snapshot_digest": permit.governance_snapshot_digest,
    }
    if provider_execution_binding_ref is not None:
        payload["provider_execution_binding_ref"] = provider_execution_binding_ref
    if reconciliation_repeatability_authority_ref is not None:
        payload["reconciliation_repeatability_authority_ref"] = (
            reconciliation_repeatability_authority_ref
        )
    if authorization_use_ref is not None:
        payload["authorization_use_ref"] = authorization_use_ref
    if invocation_specification_ref is not None:
        payload["invocation_specification_ref"] = invocation_specification_ref
    return dispatch_commit_identity_from_payload(payload)


def dispatch_recovery_mode(step: Any, attempt: Any) -> DispatchRecoveryMode:
    """Classify recovery after a durable dispatch commitment."""

    metadata = getattr(attempt, "metadata", {})
    if not isinstance(metadata, dict) or not metadata.get("dispatch_commit_ref"):
        return "uncommitted"
    semantics = str(getattr(step, "effect_semantics", ""))
    if semantics in {"pure", "idempotent", "deduplicatable"}:
        return "idempotent-retry"
    if semantics == "reconcilable":
        return "reconcile"
    return "unknown"


class GovernanceDispatchCommitter:
    """Linearize one dispatch claim against canonical governance/action authority.

    The normal RealityBoundary path first resolves the live provider object
    from ProviderRegistry. Inside this dispatch linearization the registry is
    re-entered with ``expected_provider`` so same-id replacement between lookup
    and dispatch fails closed. B is durable provider-target provenance. When an
    explicit repeat-safe reconciliation contract exists, C is instantiated for
    this request-id in the same commitment.

    Optional action-authority inputs are additive. When supplied, a fresh
    AuthorizationUse and/or DurableInvocationSpecification reference is bound
    to the exact same dispatch commitment. Legacy callers that supply neither
    retain the frozen Phase-E governed/non-governed behavior and identity.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    @staticmethod
    def _validate_specification(
        store: Any,
        specification_ref: str | None,
        request: CapabilityRequest,
        permit: InvocationPermit,
        execution_binding: ProviderExecutionBinding | None,
    ) -> Any | None:
        if specification_ref is None:
            return None
        if not specification_ref.strip():
            raise DispatchLinearizationError(
                "dispatch InvocationSpecification ref must be non-empty"
            )
        if execution_binding is None:
            raise DispatchLinearizationError(
                "InvocationSpecification-bound dispatch requires exact provider execution binding"
            )
        getter = getattr(store, "get_invocation_specification", None)
        if not callable(getter):
            raise DispatchLinearizationError(
                "StateStore cannot resolve InvocationSpecification authority"
            )
        specification = getter(specification_ref)
        if specification is None:
            raise DispatchLinearizationError(
                "dispatch references unavailable InvocationSpecification authority"
            )
        if getattr(specification, "id", None) != specification_ref:
            raise DispatchLinearizationError(
                "InvocationSpecification identity rebound before dispatch"
            )
        if getattr(specification, "source_request_ref", None) != request.id:
            raise DispatchLinearizationError(
                "InvocationSpecification is bound to a different request"
            )
        provider_binding = getattr(specification, "provider_binding", None)
        if getattr(provider_binding, "provider_id", None) != permit.provider_id:
            raise DispatchLinearizationError(
                "InvocationSpecification provider does not match InvocationPermit"
            )
        if (
            getattr(provider_binding, "provider_binding_id", None)
            != execution_binding.id
        ):
            raise DispatchLinearizationError(
                "InvocationSpecification provider binding does not match configured target"
            )
        return specification

    @staticmethod
    def _validate_authorization_use(
        use: AuthorizationUse,
        request: CapabilityRequest,
    ) -> None:
        if use.capability != request.capability:
            raise DispatchLinearizationError(
                "AuthorizationUse capability does not match dispatch request"
            )
        if use.actor_ref != (request.actor_ref or ""):
            raise DispatchLinearizationError(
                "AuthorizationUse actor does not match dispatch request"
            )
        if use.resource_ref != (request.resource_ref or ""):
            raise DispatchLinearizationError(
                "AuthorizationUse resource does not match dispatch request"
            )
        if use.effect_class != request.effect_class:
            raise DispatchLinearizationError(
                "AuthorizationUse effect class does not match dispatch request"
            )
        if set(use.subject_version_refs) != set(request.subject_version_refs):
            raise DispatchLinearizationError(
                "AuthorizationUse subject versions do not match dispatch request"
            )

    def commit(
        self,
        request: CapabilityRequest,
        permit: InvocationPermit,
        resolver: GovernanceUseRequirementResolver | None,
        *,
        attempt_id: str | None,
        provider_registry: Any | None = None,
        expected_provider: Any | None = None,
        authorization_use_factory: DispatchAuthorizationUseFactory | None = None,
        invocation_specification_ref: str | None = None,
    ) -> DispatchCommitDecision:
        action_authority_bound = (
            authorization_use_factory is not None
            or invocation_specification_ref is not None
        )
        if not permit.governance_applicable and not action_authority_bound:
            return DispatchCommitDecision(
                status="not-applicable",
                reason="invocation permit is explicitly not governance-bound",
            )
        if self.store is None:
            return DispatchCommitDecision(
                status="unavailable",
                reason="dispatch requires an authoritative StateStore",
            )

        try:
            with _dispatch_linearized_write(self.store):
                current_snapshot_digest: str | None = permit.governance_snapshot_digest
                if permit.governance_applicable:
                    current = GovernanceUseAdmission(self.store).evaluate(request, resolver)
                    current_snapshot_digest = current.snapshot_digest
                    if current.status == "unavailable":
                        return DispatchCommitDecision(
                            status="unavailable",
                            reason=current.reason,
                            current_snapshot_digest=current.snapshot_digest,
                        )
                    if current.status == "stale":
                        return DispatchCommitDecision(
                            status="stale",
                            reason=current.reason,
                            current_snapshot_digest=current.snapshot_digest,
                        )
                    if current.status == "blocked":
                        return DispatchCommitDecision(
                            status="blocked",
                            reason=current.reason,
                            current_snapshot_digest=current.snapshot_digest,
                        )
                    if current.status != "allowed":
                        return DispatchCommitDecision(
                            status="changed",
                            reason="governed dispatch no longer has an applicable allowed judgment",
                            current_snapshot_digest=current.snapshot_digest,
                        )
                    if (
                        current.requirement_digest != permit.governance_requirement_digest
                        or current.snapshot_digest != permit.governance_snapshot_digest
                    ):
                        return DispatchCommitDecision(
                            status="changed",
                            reason="dispatch governance judgment does not match InvocationPermit",
                            current_snapshot_digest=current.snapshot_digest,
                        )

                if provider_registry is None:
                    from portable_runtime.core.registry import consume_execution_target_lookup

                    consumed = consume_execution_target_lookup(permit.provider_id)
                    if consumed is not None:
                        provider_registry, expected_provider = consumed

                execution_binding: ProviderExecutionBinding | None = None
                repeatability_authority: ReconciliationRepeatabilityAuthority | None = None
                if provider_registry is not None:
                    capture_with_repeatability = getattr(
                        provider_registry,
                        "capture_reconciliation_execution_target",
                        None,
                    )
                    if callable(capture_with_repeatability):
                        (
                            captured_provider,
                            execution_binding,
                            repeatability_authority,
                        ) = capture_with_repeatability(
                            permit.provider_id,
                            subject_identity=request.id,
                            expected_provider=expected_provider,
                        )
                    else:
                        capture = getattr(provider_registry, "capture_execution_target", None)
                        if not callable(capture):
                            raise DispatchLinearizationError(
                                "authoritative provider registry lacks coherent execution-target capture"
                            )
                        captured_provider, execution_binding = capture(
                            permit.provider_id,
                            expected_provider=expected_provider,
                        )
                    if expected_provider is not None and captured_provider is not expected_provider:
                        raise DispatchLinearizationError(
                            "configured provider changed between lookup and dispatch"
                        )
                    if execution_binding.provider_id != permit.provider_id:
                        raise DispatchLinearizationError(
                            "provider execution binding does not match InvocationPermit provider"
                        )
                    if repeatability_authority is not None:
                        if (
                            repeatability_authority.provider_execution_binding_ref
                            != execution_binding.id
                        ):
                            raise DispatchLinearizationError(
                                "reconciliation repeatability authority does not match execution binding"
                            )
                        if repeatability_authority.subject_identity != request.id:
                            raise DispatchLinearizationError(
                                "reconciliation repeatability authority does not match dispatch request"
                            )

                specification = self._validate_specification(
                    self.store,
                    invocation_specification_ref,
                    request,
                    permit,
                    execution_binding,
                )

                authorization_use: AuthorizationUse | None = None
                if authorization_use_factory is not None:
                    authorization_use = authorization_use_factory(
                        request,
                        permit,
                        execution_binding,
                        specification,
                    )
                    if not isinstance(authorization_use, AuthorizationUse):
                        raise DispatchLinearizationError(
                            "dispatch authorization factory returned non-AuthorizationUse"
                        )
                    self._validate_authorization_use(authorization_use, request)
                    if not hasattr(self.store, "get_authorization_use") or not hasattr(
                        self.store,
                        "save_authorization_use",
                    ):
                        raise DispatchLinearizationError(
                            "StateStore cannot persist dispatch AuthorizationUse"
                        )
                    if self.store.get_authorization_use(authorization_use.id) is not None:
                        raise DispatchLinearizationError(
                            "dispatch AuthorizationUse already exists; automatic redispatch is forbidden"
                        )
                    self.store.save_authorization_use(authorization_use)

                binding_ref = execution_binding.id if execution_binding is not None else None
                repeatability_ref = (
                    repeatability_authority.id
                    if repeatability_authority is not None
                    else None
                )
                authorization_use_ref = (
                    authorization_use.id if authorization_use is not None else None
                )
                commit_ref = _dispatch_commit_ref(
                    request,
                    permit,
                    attempt_id,
                    binding_ref,
                    repeatability_ref,
                    authorization_use_ref,
                    invocation_specification_ref,
                )
                event_payload: dict[str, Any] = {
                    "schema": DISPATCH_COMMIT_SCHEMA,
                    "request_id": request.id,
                    "provider_id": permit.provider_id,
                    "attempt_ref": attempt_id,
                    "invocation_permit_digest": permit.request_digest,
                    "qualification_digest": permit.qualification_digest,
                    "governance_requirement_digest": permit.governance_requirement_digest,
                    "governance_snapshot_digest": permit.governance_snapshot_digest,
                    "lease_generation": permit.lease_generation,
                    "linearization_domain": "authoritative-state-store",
                }
                if execution_binding is not None:
                    event_payload["provider_execution_binding_ref"] = execution_binding.id
                    event_payload["provider_execution_binding"] = execution_binding.model_dump(mode="json")
                if repeatability_authority is not None:
                    event_payload["reconciliation_repeatability_authority_ref"] = (
                        repeatability_authority.id
                    )
                    event_payload["reconciliation_repeatability_authority"] = (
                        repeatability_authority.model_dump(mode="json")
                    )
                if authorization_use_ref is not None:
                    event_payload["authorization_use_ref"] = authorization_use_ref
                if invocation_specification_ref is not None:
                    event_payload["invocation_specification_ref"] = invocation_specification_ref
                event = Event(
                    id=commit_ref,
                    type=DISPATCH_COMMIT_EVENT,
                    subject_ref=request.id,
                    payload=event_payload,
                )

                if attempt_id is not None:
                    if not hasattr(self.store, "get_attempt") or not hasattr(
                        self.store,
                        "save_attempt",
                    ):
                        raise DispatchLinearizationError(
                            "dispatch attempt binding is unavailable"
                        )
                    attempt = self.store.get_attempt(attempt_id)
                    if attempt is None:
                        raise DispatchLinearizationError(
                            "dispatch commitment references a missing StepAttempt"
                        )
                    metadata = dict(getattr(attempt, "metadata", {}) or {})
                    existing_ref = metadata.get("dispatch_commit_ref")
                    if existing_ref not in {None, commit_ref}:
                        raise DispatchLinearizationError(
                            "StepAttempt dispatch commitment cannot be rebound"
                        )
                    metadata.update(
                        {
                            "dispatch_commit_ref": commit_ref,
                            "governance_requirement_digest": permit.governance_requirement_digest,
                            "governance_snapshot_digest": permit.governance_snapshot_digest,
                            "invocation_permit_digest": permit.request_digest,
                        }
                    )
                    if execution_binding is not None:
                        metadata["provider_execution_binding_ref"] = execution_binding.id
                    if repeatability_authority is not None:
                        metadata["reconciliation_repeatability_authority_ref"] = (
                            repeatability_authority.id
                        )
                    if authorization_use_ref is not None:
                        metadata["authorization_use_ref"] = authorization_use_ref
                    if invocation_specification_ref is not None:
                        metadata["invocation_specification_ref"] = invocation_specification_ref
                    self.store.save_attempt(
                        attempt.model_copy(update={"metadata": metadata})
                    )

                with _provider_binding_dispatch_authority(
                    self.store,
                    bound=execution_binding is not None,
                ):
                    self.store.append_event(event)
                return DispatchCommitDecision(
                    status="committed",
                    commit_ref=commit_ref,
                    reason=(
                        "governed dispatch commitment linearized"
                        if permit.governance_applicable
                        else "action-authority dispatch commitment linearized"
                    ),
                    current_snapshot_digest=current_snapshot_digest,
                    provider_execution_binding_ref=binding_ref,
                    reconciliation_repeatability_authority_ref=repeatability_ref,
                    authorization_use_ref=authorization_use_ref,
                    invocation_specification_ref=invocation_specification_ref,
                )
        except Exception as exc:
            return DispatchCommitDecision(
                status="unavailable",
                reason=f"dispatch commitment failed: {exc}",
            )
