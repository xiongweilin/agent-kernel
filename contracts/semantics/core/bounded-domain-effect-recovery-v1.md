# Bounded Domain Effect Recovery v1

Status: candidate

Owner: `portable-runtime/contracts`

Command: `bounded-domain-effect-recovery-v1`

Read view: `bounded-domain-effect-resolution-v1`

This contract exists for one specific ambiguity: a bounded external effect has a durable historical dispatch and an immutable `execution-unknown` execution receipt, so request replay must not be interpreted as permission to cross the physical provider boundary again.

The command carries only an exact historical `execution_ref`. It supplies no provider, request, dispatch, RecoveryObservation, RecoveryDisposition, RecoveryApplication, reconciliation protocol, runtime authorization, retry instruction, or claimed outcome.

Kernel reconstructs all recovery authority from durable state.

## Core distinction

```text
execution-unknown
!= retry permission

request replay
!= side-effect retry authority

bounded receipt absent/unknown
!= physical effect absent

reconciliation
!= provider.invoke

RecoveryObservation
!= Outcome
```

The v1 recovery path is:

```text
immutable execution-unknown receipt
→ exact durable InvocationDispatchCommitted
→ generic reported-unknown RecoveryObservation
→ RecoveryDisposition under bounded recovery policy
→ RecoveryApplication
→ exact A/B/C RecoveryReconciliationConsumer
→ provider.reconcile(exact historical request)
→ application-bound RecoveryObservation
→ execution projection repair only when reconciliation reports succeeded/failed
→ independent objective verification
→ confirmed Outcome
→ original Work/Run completion
```

Automatic reconciliation is permitted only when the existing dispatch graph classifies the effect as `reconcile` and the historical/current B/C authority chain remains eligible. Other recovery modes fail closed into manual resolution; this contract does not open generic retry authority.

## Immutable history and current resolution

`bounded-domain-effect-execution-receipt-v1` remains an immutable historical projection. If it recorded `execution-unknown`, recovery never rewrites that fact.

Current downstream state is exposed separately through `bounded-domain-effect-resolution-v1`.

Therefore:

```text
historical receipt.status = execution-unknown

may coexist with

current resolution.status = recovered-completed
```

without contradicting history.

The resolution view is non-authoritative (`authority_bearing=false`). Its references point to durable Kernel-owned recovery, evidence, Outcome, Work and responsibility objects. Downstream domains must not reconstruct authority from the projection.

## Physical boundary guarantee

The recovery service itself never calls `provider.invoke` and never creates a fresh CapabilityRequest, InvocationPermit, dispatch commitment or StepAttempt for the original effect. The only external recovery call is the exact-target reconciliation exit owned by the existing B4 `RecoveryReconciliationConsumer`.

For repeat-safe reconciliation, a crash before the application-bound RecoveryObservation commit may repeat the same reconciliation query under the same exact B/C authority. It still may not redispatch the physical effect.

## Completion

A reconciliation provider result by itself does not complete Work. A reported successful reconciliation may repair the original execution projection, after which the normal independent verifier must observe the frozen postcondition and the existing VerifiedOutcome authority must confirm the objective Outcome. Only then may the original bounded Work/Run complete.

`recovered-completed` therefore means:

```text
exact recovery authority
+ reconciliation completed
+ original execution projection repaired
+ independent verification passed
+ confirmed Outcome exists
+ original Work/Run completed
```

It still does not mean a downstream business obligation is satisfied or that persistent responsibility is discharged.
