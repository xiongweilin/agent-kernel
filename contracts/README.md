# Agent Kernel canonical contracts

`contracts/` is the sole canonical semantic and interoperability owner for `agent-kernel`.

No external repository, document set, commit SHA, theorem repository, or research note is required to interpret the kernel's supported product semantics. Historical or research material may motivate a contract, but it has no normative authority over this repository unless its semantics are explicitly adopted into this directory.

Contract/version identifiers use the `agent-kernel-*` prefix. The repository is a single-owner personal environment with no external consumers, so the legacy `portable-runtime-*` prefix was retired in one recorded step (2026-09-18, see the repository README) instead of being carried as a compatibility alias. Changing an identifier again still requires an explicit versioned decision; it is never implied by repository or implementation edits.

## Canonical precedence

```text
semantic contracts
> structural schemas
> canonicalization rules
> conformance vectors
> Python reference implementation
> HTTP adapters
> TypeScript client / workflow helpers
> Responsibility Inspector
```

A lower layer that conflicts with a higher layer is defective. Python is the reference execution oracle subject to `contracts/`; it is not a second semantic definition owner.

## Ownership chain

```text
agent-kernel/contracts
    owns canonical product semantics,
    state/transition invariants,
    authority ceilings,
    canonicalization rules,
    public structural contracts,
    and conformance vectors

Python agent_kernel
    is the normative reference implementation / oracle

packages/typescript
    is a non-authoritative conforming consumer

packages/inspector
    is a non-authoritative read/inspection consumer
```

Runtime implementation code MUST NOT create a second semantic owner. Public clients MUST NOT infer authority that the contracts do not grant.

## Current cognitive contracts

The current closed cognitive loop is split across three contracts:

- `cognitive-control-v2` — durable controller state, decision vocabulary and transition guards;
- `cognitive-closure-v1` — temporary closure from open cognition into WorkProposal eligibility;
- `revision-control-v1` — reality-grounded failure/revision assessment before retry, revise, reopen, reconcile, wait or close.

`cognitive-control-v1` remains historical compatibility material only.

## Four contract layers

1. **Structural** — schemas, enums, required fields and versioned wire shapes.
2. **Semantic** — identity, replay, freshness, authority ceilings, non-substitution and failure behavior.
3. **Canonicalization** — exact byte/JSON normalization and digest rules. Existing canonicalizations remain frozen per contract unless deliberately versioned.
4. **Conformance** — executable tests/vectors including expected durable deltas and forbidden effects.

## Responsibility-preserving invariants

Canonical separations include:

- judgment != authorization;
- policy allow != AuthorizationGrant;
- provider/execution success != verified/confirmed objective completion;
- `epistemic_status=supported` != governance `qualification=qualified`;
- governed state application != real-world Action;
- historical provenance != current qualification;
- dependency impact != discharge;
- selecting a repair != realizing a repair;
- current-use admission != execution authority;
- existing adopted assignment use != classification of reality;
- reasoner output != controller selection;
- reasoner output != cognitive closure;
- cognitive closure != WorkProposal / Work admission / authorization;
- controller selection != Work admission;
- failure observation != retry permission;
- revision assessment != retry / Work mutation / reopen / authorization;
- controller close != responsibility discharge.

The complete cross-layer separation set is owned by `semantics/core/responsibility-separation-v1.md`. Cognitive-loop specifics are owned by `semantics/core/cognitive-control-v2.md`, `cognitive-closure-v1.md`, and `revision-control-v1.md`.

## Legacy reopen boundary

Historical record-level reopen objects remain readable, but no reopen assessment may directly mint replacement Work. New Work after cognitive failure must return through explicit controller reopen, a new `CognitiveClosure`, `WorkProposal`, and the normal admission chain.

## Public authority ceiling

Public contracts may expose read-only views of authority-bearing runtime objects, but clients cannot mint or reconstruct authority from those views. `InvocationPermit` and `GovernanceUseRequirement` remain runtime-internal; public surfaces expose non-authority-bearing views only.

## Compatibility

`distinction-governance-1.0`, runtime protocol `2.0`, provider protocol `1`, the `agent-kernel-contracts-v1` catalog identifier, and existing persisted/event tokens remain compatible unless a versioned contract explicitly declares a breaking change.

See `catalog.toml` for the machine-readable contract index and `semantics/core/ownership-v1.md` for the ownership rule.
