# Domain responsibility proposal — v1

Status: candidate
Owner: `portable-runtime/contracts`
Command: `domain-responsibility-proposal-v1`
Receipt: `domain-responsibility-proposal-receipt-v1`

This command is the narrow public mutation boundary by which a domain control
plane may contribute its current interpretation of bounded responsibility to
the portable runtime without taking ownership of runtime work admission or
execution authority.

## Canonical chain position

The command may record exactly this prefix of the persistent-responsibility
chain:

```text
StandingResponsibility
  -> ResponsibilityAdmission
  -> ResponsibilityAssessment
  -> WorkProposal
```

It stops there.

```text
DomainResponsibilityProposal
-/-> PriorityJudgment
-/-> PortfolioAdmissionDecision
-/-> ResourceReservation
-/-> Commitment
-/-> Work
-/-> Run
-/-> AuthorizationGrant
-/-> InvocationPermit
-/-> provider invocation
-/-> Outcome
```

A successful receipt means only that the canonical responsibility identity,
admission, current domain assessment, and WorkProposal were durably recorded.
The receipt is explicitly non-authoritative.

## Domain-owned input

The domain remains responsible for the evidence interpretation behind its
`ResponsibilityAssessment`, including any business policy, organizational
approval, fact provenance, or obligation semantics represented through
`basis_refs` and responsibility scope.

The portable runtime validates canonical responsibility identity, version,
lifecycle activity, assessment freshness, proposal linkage, and append-only
identity. It does not reinterpret domain business policy merely because it
accepts the domain assessment.

## Replay and identity

Replay of the same semantic command with the same object identities and values
is idempotent. Reusing an identity with different semantics is an identity
rebound and must fail closed.

An existing StandingResponsibility is not sufficient by itself: the exact
initial ResponsibilityAdmission must also already exist when replaying this
command.

## Work admission remains Kernel-owned

After a proposal is recorded, the existing `persistent-responsibility-v1`
contract still requires:

```text
current WorkProposal
+ admitted PriorityJudgment
+ admitted PortfolioAdmissionDecision
+ current ResourceReservation
+ Commitment
-> Work
```

No domain command may synthesize or skip those objects. For external-effect
Work, materialization still does not mint execution authority. Runtime
AuthorizationGrant / InvocationPermit and the RealityBoundary remain separate.

## Transport authorization

A public HTTP deployment must authenticate and authorize mutation of this
command surface independently from the domain business authority represented
inside the payload. Transport authentication is not business effect authority.
The reference HTTP adapter is local-only until a production service identity
boundary is configured.
