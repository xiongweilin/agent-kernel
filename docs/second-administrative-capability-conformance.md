# Second administrative capability conformance

The bounded domain-effect runtime is considered reusable only if a second Administrative capability traverses the same canonical runtime without adding domain business policy to Kernel.

The second conformance capability is `administrative.iam.identity.create.v1`, but IAM is deliberately not added to Kernel's builtin capability contracts. The deployment/test boundary must explicitly provide:

- a `CapabilityContract` with the required write-remote, reconcilable, compensatable, runtime-authorized, resource-bound and subject-version-bound semantics;
- a server-owned `BoundedDomainEffectExecutionProfile`;
- an exact effect provider binding;
- an independent `verify.<effect-capability>` read-back provider.

Kernel derives runtime authority from the current responsibility lineage and the explicit capability contract. Administrative target/operation scope must still derive the exact submitted capability. A configured execution profile controls whether the capability has a physical exit. Every stage that re-reads authorization or prepares execution must use the same Runtime-owned `CapabilityContractRegistry`; recreating a default registry mid-pipeline would silently discard deployment-owned capability authority. The registry is therefore part of bounded-execution authority provenance rather than convenience configuration, and one execution may not silently switch registry identity between authorization, activation, qualification, verification, completion, or reassessment.

The conformance is invalid if Kernel adds IAM-specific approval, obligation, identity, or business-completion semantics. It is also invalid if an unknown or uncontracted capability can reach RealityBoundary.

Passing criteria:

1. IAM obtains an admitted Work through the existing responsibility path.
2. Runtime authorization is minted by Kernel rather than supplied by the domain.
3. The existing Run, qualification, procedure-readiness, provider-binding, invocation-specification and RealityBoundary path is reused unchanged.
4. Independent read-back uses `verify.administrative.iam.identity.create.v1` and creates the same typed evidence/outcome lineage.
5. Work/Run completion uses the same pre-effect completion contract.
6. Replay returns the same durable bounded-execution receipt and invokes both physical effect and verifier exactly once.
7. No IAM capability is present in Kernel builtins solely to make this test pass.
