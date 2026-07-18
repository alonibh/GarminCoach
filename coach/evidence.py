"""Reviewed rule registry. Only entries here may influence a workout decision."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvidenceRule:
    rule_id: str
    version: str
    conclusion: str
    population: str
    inputs: tuple[str, ...]
    exclusions: tuple[str, ...]
    citation_url: str
    evidence_grade: str
    reviewed_at: str
    review_due: str

    def to_dict(self) -> dict:
        return asdict(self)


GARMIN_READINESS_CATEGORY_V1 = EvidenceRule(
    rule_id="garmin_training_readiness_categories",
    version="1.0.0",
    conclusion=(
        "Use Garmin's official category boundaries. Low adds a warning; Poor "
        "supports advice to skip while preserving the original-workout override."
    ),
    population="Users whose device is known to support Garmin Training Readiness",
    inputs=("fresh Garmin Training Readiness score for the decision date",),
    exclusions=(
        "No performance prediction",
        "No change to sets, reps, weights, exercises, or session identity",
        "No substitution when a supported-device value is missing",
    ),
    citation_url=(
        "https://www8.garmin.com/manuals/webhelp/"
        "GUID-0221611A-992D-495E-8DED-1DD448F7A066/EN-GB/"
        "GUID-C21BE0C8-A08E-4DA1-B6C6-2E0E2DDDB372.html"
    ),
    evidence_grade="manufacturer-defined metric interpretation",
    reviewed_at="2026-07-18",
    review_due="2027-07-18",
)


ACWR_EXCLUSION_V1 = EvidenceRule(
    rule_id="acwr_ui_only_exclusion",
    version="1.0.0",
    conclusion="ACWR has no authority in workout recommendations or injury-risk claims.",
    population="All users",
    inputs=(),
    exclusions=("ACWR may remain visible as a descriptive UI series",),
    citation_url="https://pubmed.ncbi.nlm.nih.gov/32502973/",
    evidence_grade="peer-reviewed critical analysis",
    reviewed_at="2026-07-18",
    review_due="2027-07-18",
)


WEARABLE_SLEEP_STAGE_EXCLUSION_V1 = EvidenceRule(
    rule_id="wearable_sleep_stages_nonprescriptive",
    version="1.0.0",
    conclusion="Consumer wearable sleep stages do not independently change workout advice.",
    population="Adults using consumer wrist-worn sleep trackers",
    inputs=(),
    exclusions=("Total sleep and aggregate Garmin Sleep Score remain descriptive facts",),
    citation_url="https://pubmed.ncbi.nlm.nih.gov/40303381/",
    evidence_grade="laboratory validation study",
    reviewed_at="2026-07-18",
    review_due="2027-07-18",
)


RULE_REGISTRY = {
    rule.rule_id: rule
    for rule in (
        GARMIN_READINESS_CATEGORY_V1,
        ACWR_EXCLUSION_V1,
        WEARABLE_SLEEP_STAGE_EXCLUSION_V1,
    )
}
