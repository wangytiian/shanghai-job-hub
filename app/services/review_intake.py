"""A-source review routing; never changes strict B-source admission evidence."""

from datetime import date, datetime
import re

from app.services.intake_screening import screen_intake
from app.services.source_candidate_policy import CandidateDecision, SourceCandidate, evaluate_candidate


REVIEW_NOTE = "受众待确认：原文未明确应届生或在校生可投，须人工核实后再决定是否分发。[TARGET_AUDIENCE_UNCLEAR]"
_EXPERIENCE = re.compile(r"(?:[二三四五六七八九十2-9]|\d{2,})\s*年(?:以上|及以上|起)[^。；\n]{0,12}经验")
_SCHOOL_ONLY = re.compile(r"(?:仅限|只限|限|仅面向|只面向|只接受|仅接受)\s*(?:本校|上海商学院)")


def evaluate_review_intake(candidate: SourceCandidate, now: datetime | date | None = None) -> CandidateDecision:
    """Retain only audience-unclear leads; all other missing evidence still blocks.

    This is intentionally called only by the already authorised SBS A-source.
    `review_only` is not `qualified` and must be persisted as C/manual review.
    """
    decision = evaluate_candidate(candidate, now=now)
    combined = f"{candidate.title}\n{candidate.evidence_text}"
    if screen_intake(candidate.title, candidate.evidence_text).grade == "D" or _EXPERIENCE.search(combined):
        return CandidateDecision("excluded", ("UNSUITABLE_FOR_STUDENT_REVIEW",))
    if _SCHOOL_ONLY.search(combined):
        return CandidateDecision("excluded", ("SCHOOL_EXCLUSIVE",))
    if decision.verdict == "needs_evidence" and set(decision.reason_codes) == {"TARGET_AUDIENCE_UNCLEAR"}:
        return CandidateDecision("review_only", decision.reason_codes)
    return decision
