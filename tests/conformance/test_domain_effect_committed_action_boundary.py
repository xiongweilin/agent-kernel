from datetime import UTC, datetime

from portable_runtime.core.models import Event, Run, Step, StepAttempt
from portable_runtime.governance.dispatch import DISPATCH_COMMIT_EVENT
from portable_runtime.records.authorization import AuthorizationUse
from portable_runtime.responsibility.domain_effect_activation import (
    DOMAIN_EFFECT_ACTIVATION_EVENT,
    DOMAIN_EFFECT_ACTIVATION_SCHEMA,
    DomainEffectRunActivation,
)
from portable_runtime.responsibility.domain_effect_request import DOMAIN_EFFECT_REQUEST_EVENT
from portable_runtime.responsibility.domain_effect_run import DOMAIN_EFFECT_WORKFLOW_ID
from portable_runtime.stores.memory import InMemoryStateStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def test_committed_action_boundary_requires_exact_activation_dispatch_use_attempt_run_lineage() -> None:
    store = InMemoryStateStore()
    run = Run(
        id="run:recovery",
        work_id="work:recovery",
        workflow_id=DOMAIN_EFFECT_WORKFLOW_ID,
        status="running",
        started_at=NOW,
    )
    store.save_run(run)
    request_event = Event(
        id="event:request",
        type=DOMAIN_EFFECT_REQUEST_EVENT,
        subject_ref=run.id,
        payload={"request": {"id": "request:recovery"}},
    )
    store.save_event(request_event)
    use = AuthorizationUse(
        id="authuse:recovery",
        authorization_ref="authz:recovery",
        capability="administrative.hris.employee.create.v1",
        actor_ref="service:administrative-orchestrator",
        resource_ref="employee:recovery",
        effect_class="write-remote",
        subject_version_refs=["authority-epoch:1"],
        authorized_at=NOW,
    )
    store.save_authorization_use(use)

    activation = DomainEffectRunActivation(store)
    assert (
        activation._has_committed_action_boundary(
            run,
            request_event,
            authorization_ref=use.authorization_ref,
            authorization_uses=[use],
        )
        is False
    )

    store.save_event(
        Event(
            id="event:activation:1",
            type=DOMAIN_EFFECT_ACTIVATION_EVENT,
            subject_ref=run.id,
            payload={
                "schema": DOMAIN_EFFECT_ACTIVATION_SCHEMA,
                "authority_bearing": False,
                "work_ref": run.work_id,
                "request_event_ref": request_event.id,
            },
        )
    )
    step = Step(
        id="step:recovery",
        run_id=run.id,
        step_key="domain-effect",
        status="running",
        side_effect_class="reconcilable",
        effect_semantics="reconcilable",
    )
    store.save_step(step)
    attempt = StepAttempt(
        id="attempt:recovery",
        step_id=step.id,
        request_ref="request:recovery",
        provider_id="provider:writer",
        status="unknown",
        metadata={
            "dispatch_commit_ref": "event:dispatch",
            "authorization_use_ref": use.id,
        },
    )
    store.save_attempt(attempt)
    store.save_event(
        Event(
            id="event:dispatch",
            type=DISPATCH_COMMIT_EVENT,
            subject_ref="request:recovery",
            payload={
                "request_id": "request:recovery",
                "attempt_ref": attempt.id,
                "authorization_use_ref": use.id,
            },
        )
    )

    assert (
        activation._has_committed_action_boundary(
            run,
            request_event,
            authorization_ref=use.authorization_ref,
            authorization_uses=[use],
        )
        is True
    )
