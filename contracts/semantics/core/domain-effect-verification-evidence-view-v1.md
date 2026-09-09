# domain-effect-verification-evidence-view-v1

This view exposes the non-authoritative evidence content produced by Kernel's independent verification of one bounded domain effect.

It exists so a downstream domain can consume the reality that Kernel actually observed without treating an execution receipt, an expected postcondition, or a provider-success record as the observation itself.

## Ownership

Kernel owns the physical effect path, independent verifier execution, canonical `EvidenceArtifact`, confirmed objective `Outcome`, and the stable read projection defined here.

A downstream domain may use this view as evidence for its own semantic completion rules. Reading the view never grants runtime authority, never changes Work/Run state, and never discharges persistent responsibility.

```text
expected postcondition
!= observed postcondition

provider success
!= verification evidence

verification evidence view
!= confirmed Outcome authority

confirmed Kernel Outcome
!= domain obligation completion

Work completed
!= persistent responsibility discharged
```

## Source

The view is projected only from a canonical `EvidenceArtifact` whose metadata schema is `domain-effect-objective-verification-evidence-v1`, proof class is `objective-verification`, and proof kind is `task-objective-proof`.

The view preserves:

- exact evidence, Action, Work and Run identity;
- closed objective result (`pass` or `fail`);
- the postcondition actually observed by the independent verifier;
- the frozen expected postcondition used by Kernel verification;
- verification request/attempt identity;
- exact verifier provider and configured provider-execution-binding identity;
- evidence capture time.

Any missing or rebound required field fails closed. A non-domain-effect evidence record cannot be coerced into this view.

## HTTP read surface

```text
GET /v1/domain-effects/evidence/{evidence_ref}
```

The returned object has schema `domain-effect-verification-evidence-view-v1` and `authority_bearing=false`.

The endpoint is read-only. It may return:

- `404` when the evidence identity is unknown;
- `409` when the referenced record exists but is not a valid canonical domain-effect verification proof.

## Downstream use

A domain such as Administrative Orchestrator should compare its own frozen obligation postcondition against `observed_postcondition`. It must not reconstruct an observation by copying its own expected state merely because Kernel returned a completed execution receipt.

A domain remains responsible for its own obligation semantics and case-completion policy.
