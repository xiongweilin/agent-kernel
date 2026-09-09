from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.capability_contract import CapabilityContractRegistry
from portable_runtime.core.models import Action, Run, Work
from portable_runtime.records.models import EvidenceArtifact, OutcomeRecord
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_completion_contract import (
    require_domain_effect_completion_contract,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA,
)
from portable_runtime.responsibility.models import ResponsibilityStatus
from portable_runtime.responsibility.service import ResponsibilityKernel
from portable_runtime.workflows.completion import CompletionAuthority


class DomainEffectTerminalCompletionInput(BaseModel):
    """Identify one authoritative objective Outcome; all other refs are re-read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_ref: str = Field(min_length=1)


class DomainEffectTerminalCompletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed"] = "completed"
    outcome_ref: str
    evidence_refs: list[str]
    action_ref: str
    work_ref: str
    run_ref: str
    responsibility_ref: str
    responsibility_status: ResponsibilityStatus
    authority_bearing: bool = False


class DomainEffectTerminalCompletion:
    """Terminalize verified domain-effect Work without discharging responsibility.

    A confirmed Outcome is necessary but not sufficient. This adapter re-reads
    the exact effect Action, Work, Run, pre-effect completion contract and bound
    EvidenceArtifact, then delegates terminal authority to CompletionAuthority.
    No ResponsibilityLifecycleTransition is created here.
    """

    def __init__(
        self,
        store: Any,
        *,
        contract_registry: CapabilityContractRegistry | None = None,
    ) -> None:
        self.store = store
        self.kernel = ResponsibilityKernel(store)
        self.completion = CompletionAuthority(store)
        self.contract_registry = contract_registry or CapabilityContractRegistry()
        self.authorization = DomainEffectAuthorizationUseConsumption(
            store,
            contract_registry=self.contract_registry,
        )

    def complete(
        self,
        value: DomainEffectTerminalCompletionInput,
    ) -> DomainEffectTerminalCompletionResult:
        outcome = self.store.get_record(value.outcome_ref)
        if not isinstance(outcome, OutcomeRecord):
            raise ValueError("domain effect terminal completion requires canonical Outcome")
        if outcome.lifecycle_status != "confirmed":
            raise ValueError("domain effect terminal completion requires confirmed Outcome")
        if outcome.metadata.get("objective_result") != "pass":
            raise ValueError("domain effect terminal completion requires objective pass Outcome")

        action = self.store.get_action(outcome.action_ref)
        if not isinstance(action, Action):
            raise ValueError("domain effect terminal completion requires durable effect Action")
        work = self.store.get_work(action.work_id)
        run = self.store.get_run(action.run_id)
        if not isinstance(work, Work) or not isinstance(run, Run):
            raise ValueError("domain effect terminal completion requires durable Work and Run")
        if run.work_id != work.id:
            raise ValueError("domain effect terminal completion Work/Run binding mismatch")

        evidence_refs = [str(ref) for ref in outcome.evidence_refs if str(ref).strip()]
        if len(evidence_refs) != 1 or len(set(evidence_refs)) != 1:
            raise ValueError("bounded domain effect terminal completion requires one exact proof")
        proof = self.store.get_record(evidence_refs[0])
        if not isinstance(proof, EvidenceArtifact):
            raise ValueError("domain effect terminal completion proof is not EvidenceArtifact")
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if metadata.get("schema") != DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA:
            raise ValueError("domain effect terminal completion proof has wrong schema")
        if metadata.get("action_ref") != action.id or action.id not in proof.source_refs:
            raise ValueError("domain effect terminal completion proof Action binding mismatch")
        if metadata.get("work_id") != work.id or metadata.get("run_id") != run.id:
            raise ValueError("domain effect terminal completion proof Work/Run binding mismatch")

        run_metadata = run.metadata if isinstance(run.metadata, dict) else {}
        authorization_ref = run_metadata.get("domain_effect_authorization_ref")
        if not isinstance(authorization_ref, str) or not authorization_ref:
            raise ValueError("domain effect Run lacks runtime authorization ref")
        authorization = self.authorization._resolve_context(authorization_ref)
        if action.capability != authorization.intent.capability:
            raise ValueError("Outcome Action capability drifted from runtime authorization")
        contract, contract_digest = require_domain_effect_completion_contract(
            work,
            authorization,
        )
        if run_metadata.get("domain_effect_completion_contract_digest") != contract_digest:
            raise ValueError("domain effect Run completion contract binding drifted")
        if metadata.get("domain_effect_completion_contract_digest") != contract_digest:
            raise ValueError("domain effect verification proof completion contract binding drifted")
        if metadata.get("verification_scope") != contract["verification_scope"]:
            raise ValueError("domain effect verification proof scope drifted from completion contract")
        if metadata.get("work_version") != contract["work_version"]:
            raise ValueError("domain effect verification proof Work version drifted")
        if metadata.get("acceptance_criteria") != contract["acceptance_criteria"]:
            raise ValueError("domain effect verification proof acceptance criteria drifted")
        if metadata.get("obligation_refs") != contract["required_obligations"]:
            raise ValueError("domain effect verification proof obligation coverage drifted")
        if metadata.get("subject_version_refs") != contract["subject_version_refs"]:
            raise ValueError("domain effect verification proof subject versions drifted")
        if outcome.metadata.get("verification_scope") != contract["verification_scope"]:
            raise ValueError("confirmed Outcome scope drifted from completion contract")
        if outcome.metadata.get("subject_version_refs") != contract["subject_version_refs"]:
            raise ValueError("confirmed Outcome subject versions drifted from completion contract")

        completed_run = self.completion.authorize(
            work=work,
            run=run,
            verification_refs=evidence_refs,
        )
        completed_work = self.store.get_work(work.id)
        if not isinstance(completed_work, Work) or completed_work.status != "completed":
            raise ValueError("CompletionAuthority did not terminalize domain effect Work")
        if completed_run.status != "succeeded":
            raise ValueError("CompletionAuthority did not terminalize domain effect Run")

        responsibility_ref = completed_work.metadata.get("standing_responsibility_ref")
        if not isinstance(responsibility_ref, str) or not responsibility_ref:
            raise ValueError("completed domain effect Work lacks standing responsibility ref")
        responsibility_status = self.kernel.current_status(responsibility_ref)
        return DomainEffectTerminalCompletionResult(
            outcome_ref=outcome.id,
            evidence_refs=evidence_refs,
            action_ref=action.id,
            work_ref=completed_work.id,
            run_ref=completed_run.id,
            responsibility_ref=responsibility_ref,
            responsibility_status=responsibility_status,
        )


__all__ = [
    "DomainEffectTerminalCompletion",
    "DomainEffectTerminalCompletionInput",
    "DomainEffectTerminalCompletionResult",
]
