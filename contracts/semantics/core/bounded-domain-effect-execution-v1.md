# Bounded Domain Effect Execution v1

Status: candidate

Owner: `portable-runtime/contracts`

Command: `bounded-domain-effect-execution-v1`

Receipt: `bounded-domain-effect-execution-receipt-v1`

This command asks Kernel to progress one already-admitted Work item through the existing bounded domain-effect runtime. The domain supplies evidence identifying its business grant and effect intent. It does **not** supply runtime action authority.

Kernel remains the sole owner of:

- runtime authorization and `AuthorizationUse`;
- Run/Step/Attempt/Action identity;
- provider selection and configured execution binding;
- provider semantic contract and invocation specification;
- `InvocationPermit` and dispatch commitment;
- the physical `RealityBoundary`;
- independent read-back verification;
- canonical Outcome and Work/Run completion.

The server selects an execution profile for the capability. The command therefore contains no provider ID, verifier ID, runtime lease owner, routing rule, capacity rule, or runtime authorization object.

A successful command may return `completed` only after:

```text
domain evidence
-> Kernel runtime authorization
-> canonical Run/request
-> current qualification
-> procedure readiness
-> exact provider binding
-> durable invocation specification
-> unique RealityBoundary execution
-> independent read-back
-> confirmed objective Outcome(pass)
-> frozen completion-contract coverage
-> Work.completed / Run.succeeded
```

The receipt is explicitly non-authoritative. Its refs point to Kernel-owned durable objects; downstream consumers must not reconstruct authority from the receipt.

`completed` means the bounded Work is complete. It does **not** mean the originating domain business obligation is satisfied in every business context, and it does **not** discharge persistent responsibility.

`verified-fail` means independent read-back produced a confirmed objective failure. `execution-unknown` is not permission to repeat a side effect. A replay must first consume durable Kernel execution state; it must never use request replay as implicit retry authority.

The command is idempotent by deterministic execution identity. Once a durable receipt exists, replay returns that exact receipt. If execution crossed reality before a receipt was persisted, the implementation must re-read the canonical Attempt/Action state rather than blindly dispatching again.

This contract is intentionally high-level. Internal authorization, qualification, provider-binding, invocation-specification and action-authority stages are not public orchestration commands for downstream domains.
