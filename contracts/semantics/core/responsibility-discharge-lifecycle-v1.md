# Responsibility discharge lifecycle — v1

Status: candidate
Owner: `portable-runtime/contracts`

This public seam exposes existing generic persistent-responsibility semantics:

```text
ResponsibilityAssessment
-> ResponsibilityDischargeDecision
-> ResponsibilityLifecycleTransition
```

The three records remain separate. Recording an assessment creates no decision,
Work, runtime authority, or lifecycle mutation. Recording a discharge decision
does not change status. Only an explicit lifecycle transition with a typed,
durable discharge decision can move an ACTIVE responsibility to DISCHARGED.

The Kernel validates responsibility identity, current version, current lifecycle
status, assessment ownership and freshness, required basis references, decision
disposition, and append-only replay identity. Domain policy owns why its evidence
supports discharge; this contract contains no domain-specific interpretation.

The status view is read-only and non-authoritative. Command receipts report what
the Kernel recorded but do not grant provider capability or runtime authority.
The reference HTTP mutation routes remain local-only until a deployment supplies
an authenticated service-identity boundary.
