from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from portable_runtime.core.boundary import RealityBoundary
from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)
from portable_runtime.core.models import Checkpoint, Run, Work
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.router import ConstraintRouter
from portable_runtime.records.authorization import AuthorizationGrant
from portable_runtime.records.models import BaseRecord
from portable_runtime.records.relations import RecordRelation
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.workflows.procedure import ProcedureAssessment, ProcedureProfile, check_procedure


class _CountingProvider:
    def __init__(self) -> None:
        self.invoke_count = 0
        self._descriptor = ProviderDescriptor(
            id="pre-action-provider",
            name="pre action provider",
            version="1.0.0",
            capabilities=["code.edit"],
            side_effect_class="idempotent",
            effect_semantics="idempotent",
            reversibility="reversible",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True)

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        self.invoke_count += 1
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
        )

    async def cancel(self, request_id: str) -> None:
        return None

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        return None


def _seed(
    store: InMemoryStateStore,
    *,
    include_failure_stop: bool = True,
) -> tuple[Work, Run, AuthorizationGrant]:
    work = Work(
        id="work-pre-action",
        title="pre-action semantics",
        metadata={
            "purpose": "perform one governed edit",
            "execution_boundary": "provider",
            "candidate": ["perform-edit"],
        },
    )
    run = Run(
        id="run-pre-action",
        work_id=work.id,
        status="running",
    )
    store.save_work(work)
    store.save_run(run)

    evidence = BaseRecord(
        id="record-pre-action-evidence",
        record_type="EvidenceArtifact",
        lifecycle_status="current",
        metadata={
            "qualification_kind": "evidence",
            "target_refs": [work.id],
            "uri": "evidence:pre-action",
        },
    )
    decision = BaseRecord(
        id="record-pre-action-decision",
        record_type="Decision",
        lifecycle_status="current",
        metadata={
            "qualification_kind": "decision",
            "target_refs": [work.id],
        },
    )
    store.save_record(evidence)
    store.save_record(decision)

    proof_refs: list[dict[str, str]] = [
        {"id": evidence.id, "kind": "evidence"},
        {"id": decision.id, "kind": "decision"},
    ]
    if include_failure_stop:
        failure_stop = BaseRecord(
            id="record-pre-action-stop",
            record_type="Policy",
            lifecycle_status="candidate",
            metadata={
                "qualification_kind": "failure-stop",
                "condition": "provider failure",
            },
        )
        store.save_record(failure_stop)
        proof_refs.append({"id": failure_stop.id, "kind": "failure-stop"})

    relation = RecordRelation(
        id="relation-pre-action-evidence",
        relation_type="records",
        subject_ref=work.id,
        object_ref=evidence.id,
    )
    store.save_relation(relation)
    checkpoint = Checkpoint(
        id="checkpoint-pre-action",
        run_id=run.id,
        payload={"condition": "provider failure"},
    )
    store.save_checkpoint(checkpoint)

    work.metadata.update(
        {
            "procedure_proof_refs": proof_refs,
            "relation_refs": [relation.id],
            "checkpoint_refs": [checkpoint.id],
            "decision_refs": [decision.id],
        }
    )
    store.save_work(work)

    grant = AuthorizationGrant(
        id="grant-pre-action",
        principal_ref="human:owner",
        grantee_ref="agent:test",
        allowed_capabilities=["code.edit"],
        resource_scope=["repo:phase"],
        effect_ceiling="write-local",
        subject_version_refs=["git:v1"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store.save_authorization(grant)
    assert store.acquire_lease(run.id, owner="agent:test", ttl_seconds=300)
    current = store.get_run(run.id)
    assert current is not None
    return work, current, grant


def _request(run: Run) -> CapabilityRequest:
    return CapabilityRequest(
        id="request-pre-action",
        capability="code.edit",
        work_id=run.work_id,
        run_id=run.id,
        actor_ref="agent:test",
        resource_ref="repo:phase",
        subject_version_refs=["git:v1"],
        effect_class="write-local",
        lease_owner="agent:test",
        lease_generation=run.lease_generation,
        metadata={"procedure_profile": "standard"},
    )


@pytest.mark.asyncio
async def test_boundary_enters_provider_when_only_post_action_gates_are_open() -> None:
    store = InMemoryStateStore()
    work, run, grant = _seed(store)
    provider = _CountingProvider()
    registry = ProviderRegistry()
    registry.register(provider)

    assessment = check_procedure(
        work,
        run,
        ProcedureProfile.standard,
        proofs={
            "failure_stop_proofs": [{"condition": "provider failure"}],
            "evidence_artifacts": [store.get_record("record-pre-action-evidence")],
            "relations": [store.get_relation("relation-pre-action-evidence")],
            "checkpoints": [store.get_checkpoint("checkpoint-pre-action")],
            "decisions": [store.get_record("record-pre-action-decision")],
        },
        grants=[grant],
    )
    assert isinstance(assessment, ProcedureAssessment)
    assert assessment.executable is True
    assert {
        str(status.obligation)
        for status in assessment
        if status.status == "open"
    } == {"result-confirmation", "verification"}

    boundary = RealityBoundary(
        store=store,
        registry=registry,
        routing=ConstraintRouter(registry=registry),
    )
    result = await boundary.execute(_request(run))

    assert result.status == "succeeded", result.model_dump()
    assert provider.invoke_count == 1


@pytest.mark.asyncio
async def test_boundary_still_blocks_open_pre_action_obligation() -> None:
    store = InMemoryStateStore()
    _work, run, _grant = _seed(store, include_failure_stop=False)
    provider = _CountingProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    boundary = RealityBoundary(
        store=store,
        registry=registry,
        routing=ConstraintRouter(registry=registry),
    )

    result = await boundary.execute(_request(run))

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.get("code") == "ProcedureIncomplete"
    assert provider.invoke_count == 0
