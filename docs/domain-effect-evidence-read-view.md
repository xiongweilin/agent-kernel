# Domain-effect evidence read view

`domain-effect-verification-evidence-view-v1` is the downstream read seam for the canonical objective-verification `EvidenceArtifact` created by bounded domain-effect execution.

The view is intentionally non-authoritative. It exposes the postcondition actually observed by the independent Kernel verifier, its frozen expected postcondition, exact Action/Work/Run and verification execution lineage, verifier provider binding, and capture time.

Administrative Orchestrator may use this view as evidence for domain obligation satisfaction. It must not treat the view itself as business completion authority, and it must not reconstruct observed reality from its own expected postcondition.
