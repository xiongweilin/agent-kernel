from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Run, utcnow
from portable_runtime.records.authorization import is_grant_valid
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
    DomainEffectAuthorizationUseContext,
)
from portable_runtime.responsibility.domain_effect_completion_contract import (
    freeze_domain_effect_completion_contract,
    require_domain_effect_completion_contract,
)

DOMAIN_EFFECT_WORKFLOW_ID = "administrative-effect-v1"
DOMAIN_EFFECT_RUN_SCHEMA = "domain-effect-run-v1"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


class DomainEffectRunPreparationInput(BaseModel):
    """Identify the Kernel runtime grant whose Work needs orchestration identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_ref: str = Field(min_length=1)


class DomainEffectRunPreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["prepared"] = "prepared"
    run_ref: str
    work_ref: str
    authorization_ref: str
    evidence_ref: str
    decision_ref: str
    logical_effect_ref: str
    workflow_id: str = DOMAIN_EFFECT_WORKFLOW_ID
    authority_bearing: bool = False


class DomainEffectRunPreparation:
    """Create the unique queued Run for one bounded administrative effect.

    Run preparation is orchestration, not action authority. It deliberately
    does not consume AuthorizationUse, issue InvocationPermit, select a
    provider, create an Attempt/dispatch, or change Work to ``running``.

    Before the first Run is persisted, this stage freezes the exact Work-level
    completion contract that later objective verification and CompletionAuthority
    must consume. Replay validates that contract; it never backfills one after
    execution has already begun.
    """

    def __init__(
        self,
        store: Any,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
    ) -> None:
        self.store = store
        self.contract_registry = contract_registry or CapabilityContractRegistry()
        self.authorization = DomainEffectAuthorizationUseConsumption(
            store,
            contract_registry=self.contract_registry,
        )

    def prepare(
        self,
        value: DomainEffectRunPreparationInput,
        *,
        prepared_at: datetime | None = None,
    ) -> DomainEffectRunPreparationResult:
        with self.store.transaction():
            context = self.authorization._resolve_context(value.authorization_ref)
            run_id = _stable_id(
                "run_domain_effect",
                context.intent.work_ref,
                context.grant.id,
                context.evidence.id,
                context.admission.subject_version_ref,
            )
            logical_effect_ref = _stable_id(
                "logical_effect",
                context.intent.domain_intent_ref,
                context.intent.capability,
                context.intent.subject_ref,
                context.admission.subject_version_ref,
            )
            existing = self.store.get_run(run_id)
            foreign_runs = [
                run
                for run in self.store.list_runs(context.intent.work_ref)
                if run.id != run_id
            ]
            if foreign_runs:
                raise ValueError(
                    "administrative effect Work already has a non-canonical Run"
                )
            if existing is not None:
                current_work = self.store.get_work(context.intent.work_ref)
                if current_work is None:
                    raise ValueError("canonical domain effect Run lost its Work")
                _contract, completion_digest = require_domain_effect_completion_contract(
                    current_work,
                    context,
                )
                self._validate_existing(
                    existing,
                    context,
                    logical_effect_ref,
                    completion_digest,
                )
                return self._result(existing, context, logical_effect_ref)

            at = prepared_at or utcnow()
            if not is_grant_valid(context.grant, now=at):
                raise ValueError(
                    "domain effect runtime grant is not current for Run preparation"
                )
            if any(
                getattr(use, "authorization_ref", None) == context.grant.id
                for use in self.store.list_authorization_uses()
            ):
                raise ValueError(
                    "domain effect authorization was consumed before canonical Run preparation"
                )

            frozen_work = freeze_domain_effect_completion_contract(context.work, context)
            _contract, completion_digest = require_domain_effect_completion_contract(
                frozen_work,
                context,
            )
            run = Run(
                id=run_id,
                created_at=at,
                work_id=context.intent.work_ref,
                status="queued",
                workflow_id=DOMAIN_EFFECT_WORKFLOW_ID,
                metadata=self._metadata(
                    context,
                    logical_effect_ref,
                    completion_digest,
                ),
            )
            self.store.save_work(frozen_work)
            self.store.save_run(run)
            return self._result(run, context, logical_effect_ref)

    @staticmethod
    def _metadata(
        context: DomainEffectAuthorizationUseContext,
        logical_effect_ref: str,
        completion_contract_digest: str,
    ) -> dict[str, Any]:
        return {
            "schema": DOMAIN_EFFECT_RUN_SCHEMA,
            "authority_bearing": False,
            "domain_effect_authorization_ref": context.grant.id,
            "domain_effect_authorization_decision_ref": context.decision.id,
            "domain_effect_intent_evidence_ref": context.evidence.id,
            "domain_effect_completion_contract_digest": completion_contract_digest,
            "responsibility_ref": context.grant.metadata.get("responsibility_ref"),
            "proposal_ref": context.grant.metadata.get("proposal_ref"),
            "capability": context.intent.capability,
            "actor_ref": context.admission.actor_ref,
            "resource_ref": context.admission.resource_ref,
            "subject_version_ref": context.admission.subject_version_ref,
            "logical_effect_ref": logical_effect_ref,
            "authorization_use_requirement": "consume-at-action-boundary",
            "provider_selection": "not-performed",
            "invocation_permit": "not-issued",
        }

    def _validate_existing(
        self,
        run: Run,
        context: DomainEffectAuthorizationUseContext,
        logical_effect_ref: str,
        completion_contract_digest: str,
    ) -> None:
        if run.work_id != context.intent.work_ref:
            raise ValueError("canonical domain effect Run rebound to another Work")
        if run.workflow_id != DOMAIN_EFFECT_WORKFLOW_ID:
            raise ValueError("canonical domain effect Run workflow identity rebound")
        expected = self._metadata(
            context,
            logical_effect_ref,
            completion_contract_digest,
        )
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        for key, expected_value in expected.items():
            if metadata.get(key) != expected_value:
                raise ValueError(
                    f"canonical domain effect Run provenance rebound at {key}"
                )

    @staticmethod
    def _result(
        run: Run,
        context: DomainEffectAuthorizationUseContext,
        logical_effect_ref: str,
    ) -> DomainEffectRunPreparationResult:
        return DomainEffectRunPreparationResult(
            run_ref=run.id,
            work_ref=context.intent.work_ref,
            authorization_ref=context.grant.id,
            evidence_ref=context.evidence.id,
            decision_ref=context.decision.id,
            logical_effect_ref=logical_effect_ref,
        )


__all__ = [
    "DOMAIN_EFFECT_RUN_SCHEMA",
    "DOMAIN_EFFECT_WORKFLOW_ID",
    "DomainEffectRunPreparation",
    "DomainEffectRunPreparationInput",
    "DomainEffectRunPreparationResult",
]
