# Responsibility Work admission — v1

Status: candidate
Owner: `portable-runtime/contracts`
Command: `responsibility-work-admission-v1`
Receipt: `responsibility-work-admission-receipt-v1`

This command asks the Kernel to evaluate one already-recorded `WorkProposal`
through the canonical persistent-responsibility admission chain. The caller
provides only the proposal identity and the policy identity it expects the
Kernel to be running.

## Canonical chain position

The command may materialize exactly this suffix:

```text
WorkProposal
  -> PriorityJudgment
  -> ResourcePool
  -> PortfolioAdmissionDecision
  -> ResourceReservation
  -> Commitment
  -> Work
```

Every intermediate remains explicit and persisted. A domain caller may not
submit `admitted=true`, resource capacity, a reservation, a commitment, or a
pre-built Work object through this command.

## Kernel-owned policy

Admission thresholds, allowed effect classes, pool identity and capacity,
reservation TTL, and priority interpretation are server-side Kernel policy.
The command contains `expected_policy_ref` only as an optimistic compatibility
guard. If it does not match the active policy exactly, admission fails closed.

The reference HTTP deployment uses a Kernel-owned bounded-local profile unless
a different `ResponsibilityAdmissionPolicy` is injected by the server.

## Result states

`work-materialized` means the complete admission chain was satisfied and a
canonical Work object exists.

`priority-rejected` means Kernel priority policy rejected the proposal before
resource-pool admission.

`portfolio-rejected` means priority admitted the proposal but current pool
capacity could not satisfy the declared resource request.

Replay under the same policy converges on the same deterministic chain and Work
identity. Reusing a canonical identity with changed semantics is an identity
rebound and fails closed.

## No execution authority

A Work-admission receipt is explicitly non-authoritative. Materialized Work is
responsibility scheduling state, not permission to invoke a provider.

```text
Work
-/-> AuthorizationGrant
-/-> AuthorizationUse
-/-> InvocationPermit
-/-> provider invocation
-/-> Outcome
```

External-effect Work must still pass the separate Kernel runtime authorization
and RealityBoundary path before any physical effect may occur.

## Transport authorization

This mutation surface requires transport authentication/authorization
independently from responsibility semantics. The reference HTTP adapter remains
local-only until a production service-identity boundary is configured.
