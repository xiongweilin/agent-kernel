from __future__ import annotations

from portable_runtime.responsibility.admission import (
    BoundedLocalResponsibilityAdmissionPolicy,
    ResponsibilityAdmissionPolicy,
)
from portable_runtime.responsibility.models import EffectClass, ResourceVector

BOUNDED_LOCAL_PROFILE = "bounded-local"
BOUNDED_LOCAL_POLICY_REF = "responsibility-admission:bounded-local@1"
ADMINISTRATIVE_PUBLIC_PROFILE = "administrative-public"
ADMINISTRATIVE_PUBLIC_POLICY_REF = "responsibility-admission:administrative-public@1"
ADMINISTRATIVE_PUBLIC_V2_PROFILE = "administrative-public-v2"
ADMINISTRATIVE_PUBLIC_V2_POLICY_REF = "responsibility-admission:administrative-public@2"


def administrative_public_responsibility_admission_policy() -> BoundedLocalResponsibilityAdmissionPolicy:
    """Return the bounded profile for the first administrative public slice.

    Version 1 remains frozen to the original HRIS-only cutover contract. New
    administrative capabilities must use a later explicit profile rather than
    inheriting capacity from this policy.
    """

    return BoundedLocalResponsibilityAdmissionPolicy(
        profile_id=ADMINISTRATIVE_PUBLIC_PROFILE,
        version="1",
        max_request=ResourceVector(
            compute_units=1,
            api_calls=2,
            money_minor=0,
            human_attention_units=1,
            concurrency_slots=1,
            domain_quota={"administrative:hris": 1},
        ),
        capacity=ResourceVector(
            compute_units=4,
            api_calls=8,
            money_minor=0,
            human_attention_units=4,
            concurrency_slots=4,
            domain_quota={"administrative:hris": 4},
        ),
        allowed_effect_classes=(EffectClass.EXTERNAL_EFFECT,),
        reservation_ttl_seconds=300,
    )


def administrative_public_v2_responsibility_admission_policy() -> BoundedLocalResponsibilityAdmissionPolicy:
    """Return the explicit dual-capability Administrative public profile.

    Version 2 adds IAM identity creation capacity while preserving the bounded
    per-domain quota model. This is a deployment admission contract, not IAM
    business policy: Kernel still requires the capability's explicit effect
    contract and bounded execution profile before any physical action can run.
    """

    return BoundedLocalResponsibilityAdmissionPolicy(
        profile_id=ADMINISTRATIVE_PUBLIC_PROFILE,
        version="2",
        max_request=ResourceVector(
            compute_units=1,
            api_calls=2,
            money_minor=0,
            human_attention_units=1,
            concurrency_slots=1,
            domain_quota={
                "administrative:hris": 1,
                "administrative:iam": 1,
            },
        ),
        capacity=ResourceVector(
            compute_units=8,
            api_calls=16,
            money_minor=0,
            human_attention_units=8,
            concurrency_slots=8,
            domain_quota={
                "administrative:hris": 4,
                "administrative:iam": 4,
            },
        ),
        allowed_effect_classes=(EffectClass.EXTERNAL_EFFECT,),
        reservation_ttl_seconds=300,
    )


def responsibility_admission_policy_for_profile(
    profile: str,
) -> ResponsibilityAdmissionPolicy:
    """Resolve one server-owned admission profile or fail closed."""

    normalized = profile.strip()
    if normalized in {BOUNDED_LOCAL_PROFILE, BOUNDED_LOCAL_POLICY_REF}:
        return BoundedLocalResponsibilityAdmissionPolicy()
    if normalized in {
        ADMINISTRATIVE_PUBLIC_PROFILE,
        ADMINISTRATIVE_PUBLIC_POLICY_REF,
    }:
        return administrative_public_responsibility_admission_policy()
    if normalized in {
        ADMINISTRATIVE_PUBLIC_V2_PROFILE,
        ADMINISTRATIVE_PUBLIC_V2_POLICY_REF,
    }:
        return administrative_public_v2_responsibility_admission_policy()
    raise ValueError(f"unknown responsibility admission profile: {profile!r}")


__all__ = [
    "ADMINISTRATIVE_PUBLIC_POLICY_REF",
    "ADMINISTRATIVE_PUBLIC_PROFILE",
    "ADMINISTRATIVE_PUBLIC_V2_POLICY_REF",
    "ADMINISTRATIVE_PUBLIC_V2_PROFILE",
    "BOUNDED_LOCAL_POLICY_REF",
    "BOUNDED_LOCAL_PROFILE",
    "administrative_public_responsibility_admission_policy",
    "administrative_public_v2_responsibility_admission_policy",
    "responsibility_admission_policy_for_profile",
]
