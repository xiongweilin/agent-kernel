# Domain-effect action boundary

This slice extends the existing governed dispatch linearization so a bounded domain effect can bind its runtime action authority to the same durable dispatch fact that names the exact configured provider target.

The action-time ordering is:

```text
pre-action qualification/readiness
-> exact ProviderExecutionBinding
-> DurableInvocationSpecification
-> ordinary Step/Attempt/Action precommit
-> dispatch linearization
   -> re-resolve exact configured provider target
   -> validate InvocationSpecification against that target
   -> create live AuthorizationUse candidate
   -> persist AuthorizationUse
   -> bind AuthorizationUse + InvocationSpecification + ProviderExecutionBinding to StepAttempt
   -> append InvocationDispatchCommitted
-> reality exit (later integration slice)
```

`AuthorizationUse` is deliberately not consumed during Run preparation, qualification, procedure readiness, provider binding, invocation specification capture, or ordinary execution-record precommit. If the process fails before dispatch linearization, the effect remains uncommitted and no action authorization has been consumed.

The linearization transaction is also the rollback boundary. Failure to persist the dispatch commitment must remove the just-created AuthorizationUse and any dispatch/action-authority metadata written to the StepAttempt.

This slice does not add a new provider invocation path. `RealityBoundary` remains the sole reality-exit owner. A following integration slice will supply the bounded domain-effect action authority to that existing boundary and require the provider chosen for execution to match the exact provider identity frozen by the InvocationSpecification.
