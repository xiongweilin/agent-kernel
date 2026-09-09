from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from portable_runtime.core.models import Action, Run, Work
from portable_runtime.records.models import EvidenceArtifact, OutcomeRecord
from portable_runtime.responsibility.domain import record_domain_assessment
from portable_runtime.responsibility.domain_effect_authorization_use import (
    DomainEffectAuthorizationUseConsumption,
)
from portable_runtime.responsibility.domain_effect_completion_contract import (
    require_domain_effect_completion_contract,
)
from portable_runtime.responsibility.domain_effect_verified_outcome import (
    DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA,
)
from portable_runtime.responsibility.models import (
    ResponsibilityAssessment,
    ResponsibilityExpectation,
    ResponsibilityStatus,
)
from portable_runtime.responsibility.service import ResponsibilityKernel

DOMAIN_EFFECT_RESPONSIBILITY_CLEAR = "domain-effect-responsibility-current-clear"
DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED = "domain-effect-responsibility-current-blocked"


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


class DomainEffectResponsibilityReassessmentInput(BaseModel):
    """Identify a verified Outcome whose completed Work should trigger reassessment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_ref: str = Field(min_length=1)
    freshness_seconds: int = Field(default=300, gt=0, le=3600)


class DomainEffectResponsibilityReassessmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["assessed"] = "assessed"
    assessment_ref: str
    responsibility_ref: str
    responsibility_version: int
    blockers_clear: bool
    blocker_refs: list[str]
    authority_bearing: bool = False


class DomainEffectResponsibilityReassessment:
    """Record current responsibility blockers after verified terminal Work.

    This adapter owns no domain discharge policy, discharge decision, or
    lifecycle mutation. It converts a bounded set of current runtime facts into
    one fresh ``ResponsibilityAssessment`` through the existing domain-assessment
    gate.

    For this bounded reference slice it establishes only these generic facts:
    - the exact verified effect Work/Run is terminally successful;
    - the Work still binds the current responsibility version;
    - every Work materialized for that same current responsibility version is
      ``completed``;
    - no current-version ResponsibilityExpectation remains open.

    ``blockers_clear`` is deliberately not a discharge judgment. A downstream
    domain policy may treat failed/cancelled Work, optional expectations, or
    other business facts differently when deciding whether responsibility may
    be discharged.
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.kernel = ResponsibilityKernel(store)
        self.authorization = DomainEffectAuthorizationUseConsumption(store)

    def reassess(
        self,
        value: DomainEffectResponsibilityReassessmentInput,
        *,
        assessed_at: datetime,
    ) -> DomainEffectResponsibilityReassessmentResult:
        outcome = self.store.get_record(value.outcome_ref)
        if not isinstance(outcome, OutcomeRecord):
            raise ValueError("domain effect responsibility reassessment requires canonical Outcome")
        if outcome.lifecycle_status != "confirmed":
            raise ValueError("domain effect responsibility reassessment requires confirmed Outcome")
        if outcome.metadata.get("objective_result") != "pass":
            raise ValueError("domain effect responsibility reassessment requires objective pass Outcome")

        action = self.store.get_action(outcome.action_ref)
        if not isinstance(action, Action):
            raise ValueError("domain effect responsibility reassessment requires durable effect Action")
        work = self.store.get_work(action.work_id)
        run = self.store.get_run(action.run_id)
        if not isinstance(work, Work) or not isinstance(run, Run):
            raise ValueError("domain effect responsibility reassessment requires durable Work and Run")
        if run.work_id != work.id:
            raise ValueError("domain effect responsibility reassessment Work/Run binding mismatch")
        if work.status != "completed" or run.status != "succeeded":
            raise ValueError("responsibility reassessment requires terminally completed Work/Run")

        work_metadata = work.metadata if isinstance(work.metadata, dict) else {}
        responsibility_ref = work_metadata.get("standing_responsibility_ref")
        responsibility_version = work_metadata.get("standing_responsibility_version")
        if not isinstance(responsibility_ref, str) or not responsibility_ref:
            raise ValueError("completed domain effect Work lacks standing responsibility ref")
        if not isinstance(responsibility_version, int):
            raise ValueError("completed domain effect Work lacks standing responsibility version")
        current_version, _statement, scope = self.kernel.current_definition(responsibility_ref)
        if responsibility_version != current_version:
            raise ValueError("completed domain effect Work is bound to stale responsibility version")
        if self.kernel.current_status(responsibility_ref) is not ResponsibilityStatus.ACTIVE:
            raise ValueError("responsibility reassessment requires active standing responsibility")

        evidence_refs = [str(ref) for ref in outcome.evidence_refs if str(ref).strip()]
        if len(evidence_refs) != 1 or len(set(evidence_refs)) != 1:
            raise ValueError("bounded responsibility reassessment requires one exact objective proof")
        proof = self.store.get_record(evidence_refs[0])
        if not isinstance(proof, EvidenceArtifact):
            raise ValueError("responsibility reassessment proof is not EvidenceArtifact")
        proof_metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if proof_metadata.get("schema") != DOMAIN_EFFECT_VERIFICATION_EVIDENCE_SCHEMA:
            raise ValueError("responsibility reassessment proof has wrong schema")
        if proof_metadata.get("action_ref") != action.id or action.id not in proof.source_refs:
            raise ValueError("responsibility reassessment proof Action binding mismatch")
        if proof_metadata.get("work_id") != work.id or proof_metadata.get("run_id") != run.id:
            raise ValueError("responsibility reassessment proof Work/Run binding mismatch")

        run_metadata = run.metadata if isinstance(run.metadata, dict) else {}
        authorization_ref = run_metadata.get("domain_effect_authorization_ref")
        if not isinstance(authorization_ref, str) or not authorization_ref:
            raise ValueError("domain effect Run lacks runtime authorization ref")
        authorization = self.authorization._resolve_context(authorization_ref)
        if action.capability != authorization.intent.capability:
            raise ValueError("Outcome Action capability drifted from runtime authorization")
        contract, contract_digest = require_domain_effect_completion_contract(work, authorization)
        if run_metadata.get("domain_effect_completion_contract_digest") != contract_digest:
            raise ValueError("domain effect Run completion contract binding drifted")
        if proof_metadata.get("domain_effect_completion_contract_digest") != contract_digest:
            raise ValueError("responsibility reassessment proof completion contract binding drifted")
        if outcome.metadata.get("verification_scope") != contract["verification_scope"]:
            raise ValueError("responsibility reassessment Outcome scope drifted from completion contract")
        if outcome.metadata.get("subject_version_refs") != contract["subject_version_refs"]:
            raise ValueError("responsibility reassessment Outcome subject versions drifted")

        blocker_refs = self._blockers(responsibility_ref, current_version)
        blockers_clear = not blocker_refs
        assessment_kind = (
            DOMAIN_EFFECT_RESPONSIBILITY_CLEAR
            if blockers_clear
            else DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED
        )
        subject_ref = scope.get("obligation_id") or responsibility_ref
        basis_refs = [outcome.id, proof.id, action.id, work.id, run.id, *blocker_refs]
        assessment = ResponsibilityAssessment(
            id=_stable_id(
                "assessment_domain_effect_responsibility",
                responsibility_ref,
                current_version,
                outcome.id,
                assessment_kind,
                *blocker_refs,
                assessed_at.isoformat(),
            ),
            created_at=assessed_at,
            responsibility_ref=responsibility_ref,
            responsibility_version=current_version,
            subject_ref=subject_ref,
            assessment_kind=assessment_kind,
            basis_refs=basis_refs,
            assessed_at=assessed_at,
            fresh_until=assessed_at + timedelta(seconds=value.freshness_seconds),
            rationale=(
                "verified terminal effect has no current Work or expectation blockers"
                if blockers_clear
                else "current responsibility blockers remain: " + ", ".join(blocker_refs)
            ),
        )
        recorded = record_domain_assessment(self.kernel, assessment, now=assessed_at)
        return DomainEffectResponsibilityReassessmentResult(
            assessment_ref=recorded.id,
            responsibility_ref=responsibility_ref,
            responsibility_version=current_version,
            blockers_clear=blockers_clear,
            blocker_refs=blocker_refs,
        )

    def _blockers(self, responsibility_ref: str, responsibility_version: int) -> list[str]:
        blockers: set[str] = set()
        for candidate in self.store.list_work():
            metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
            if metadata.get("standing_responsibility_ref") != responsibility_ref:
                continue
            if metadata.get("standing_responsibility_version") != responsibility_version:
                continue
            if candidate.status != "completed":
                blockers.add(candidate.id)

        for value in self.kernel.journal.list("ResponsibilityExpectation", responsibility_ref):
            if not isinstance(value, ResponsibilityExpectation):
                continue
            if value.responsibility_version != responsibility_version:
                continue
            if self.kernel.expectation_open(value.id):
                blockers.add(value.id)
        return sorted(blockers)


__all__ = [
    "DOMAIN_EFFECT_RESPONSIBILITY_BLOCKED",
    "DOMAIN_EFFECT_RESPONSIBILITY_CLEAR",
    "DomainEffectResponsibilityReassessment",
    "DomainEffectResponsibilityReassessmentInput",
    "DomainEffectResponsibilityReassessmentResult",
]
