# Example

A downstream domain receives a completed bounded-execution receipt containing `evidence_ref=evidence_123`, then reads:

```json
{
  "schema": "domain-effect-verification-evidence-view-v1",
  "evidence_ref": "evidence_123",
  "action_ref": "action_123",
  "work_ref": "work_123",
  "run_ref": "run_123",
  "objective_result": "pass",
  "observed_postcondition": {"active": true},
  "expected_postcondition": {"active": true},
  "verification_request_ref": "request_verify_123",
  "verification_attempt_ref": "attempt_verify_123",
  "verifier_provider_id": "provider:verifier",
  "verifier_provider_execution_binding_ref": "binding_123",
  "captured_at": "2026-09-09T06:00:00Z",
  "authority_bearing": false
}
```

The downstream domain may compare `observed_postcondition` with its own current obligation semantics. This view never grants execution authority or discharges responsibility.
